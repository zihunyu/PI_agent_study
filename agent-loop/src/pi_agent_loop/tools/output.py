"""有界输出预览和流式 spill。"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal


_MAX_CONFIGURED_LINES = 100_000
_MAX_CONFIGURED_PREVIEW_BYTES = 10 * 1024 * 1024
_MAX_CONFIGURED_SPILL_BYTES = 1024 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class OutputPolicy:
    max_lines: int = 2_000
    max_bytes: int = 50 * 1024
    max_spill_bytes: int = 20 * 1024 * 1024

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("max_lines", self.max_lines, _MAX_CONFIGURED_LINES),
            ("max_bytes", self.max_bytes, _MAX_CONFIGURED_PREVIEW_BYTES),
            ("max_spill_bytes", self.max_spill_bytes, _MAX_CONFIGURED_SPILL_BYTES),
        ):
            if type(value) is not int or value < 1 or value > maximum:
                raise ValueError(f"{name} 必须是 1 到 {maximum} 之间的整数")
        if self.max_spill_bytes < self.max_bytes:
            raise ValueError("max_spill_bytes 不能小于 max_bytes")


@dataclass(frozen=True, slots=True)
class AccumulatedOutput:
    text: str
    truncated: bool
    total_bytes: int
    total_lines: int
    spill_path: str | None
    spill_complete: bool = True
    dropped_bytes: int = 0


class OutputAccumulator:
    """只保留有界 head/tail，超过限制时把完整原始字节流式落盘。"""

    def __init__(
        self,
        policy: OutputPolicy,
        *,
        mode: Literal["head", "tail"],
        spill_directory: Path,
        prefix: str,
    ) -> None:
        self.policy = policy
        self.mode = mode
        self.spill_directory = spill_directory
        self.prefix = prefix
        self._preview = bytearray()
        self._total_bytes = 0
        self._total_lines = 0
        self._ends_with_newline = False
        # Spill 必须保存原始字节。Preview 可能因行/字节限制而裁剪，不能拿它
        # 回填刚创建的 spill；否则跨 chunk UTF-8 或行边界会永久丢字节。
        self._pre_spill = bytearray()
        self._spill: BinaryIO | None = None
        self._spill_path: Path | None = None
        self._spill_bytes = 0
        self._dropped_bytes = 0
        self._finished: AccumulatedOutput | None = None

    def feed(self, chunk: bytes) -> None:
        if self._finished is not None:
            raise RuntimeError("OutputAccumulator finish 后不能继续 feed")
        if not chunk:
            return
        self._total_bytes += len(chunk)
        self._total_lines += chunk.count(b"\n")
        self._ends_with_newline = chunk.endswith(b"\n")
        actual_lines = self._total_lines + (
            1 if self._total_bytes and not self._ends_with_newline else 0
        )
        overflowing = (
            self._total_bytes > self.policy.max_bytes
            or actual_lines > self.policy.max_lines
        )
        if overflowing and self._spill is None:
            self.spill_directory.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(
                prefix=self.prefix,
                suffix=".log",
                dir=self.spill_directory,
            )
            try:
                os.chmod(name, 0o600)
                spill = os.fdopen(descriptor, "wb")
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                try:
                    os.unlink(name)
                except OSError:
                    pass
                raise
            self._spill_path = Path(name)
            self._spill = spill
            if self._pre_spill:
                self._write_spill(self._pre_spill)
            self._pre_spill.clear()
        if self._spill is not None:
            self._write_spill(chunk)
        else:
            self._pre_spill.extend(chunk)

        self._preview.extend(chunk)
        if self.mode == "head":
            self._trim_head()
        else:
            self._trim_tail()

    def finish(self) -> AccumulatedOutput:
        if self._finished is not None:
            return self._finished
        if self._spill is not None:
            spill = self._spill
            primary: BaseException | None = None
            try:
                spill.flush()
                os.fsync(spill.fileno())
            except BaseException as error:
                primary = error
            try:
                spill.close()
            except BaseException as error:
                if primary is None:
                    primary = error
            finally:
                self._spill = None
            if primary is not None:
                raise primary
        preview = _decode_bounded_utf8(bytes(self._preview), mode=self.mode)
        self._finished = AccumulatedOutput(
            text=preview,
            truncated=self._spill_path is not None,
            total_bytes=self._total_bytes,
            total_lines=self._total_lines
            + (1 if self._total_bytes and not self._ends_with_newline else 0),
            spill_path=str(self._spill_path) if self._spill_path is not None else None,
            spill_complete=self._dropped_bytes == 0,
            dropped_bytes=self._dropped_bytes,
        )
        return self._finished

    def close(self) -> None:
        """异常路径关闭 spill fd；不尝试把不完整输出伪装成成功结果。"""

        if self._spill is None:
            return
        spill = self._spill
        self._spill = None
        try:
            spill.close()
        except OSError:
            pass

    def _trim_head(self) -> None:
        if len(self._preview) > self.policy.max_bytes:
            del self._preview[self.policy.max_bytes :]
        lines = self._preview.splitlines(keepends=True)
        if len(lines) > self.policy.max_lines:
            self._preview = bytearray(b"".join(lines[: self.policy.max_lines]))

    def _trim_tail(self) -> None:
        if len(self._preview) > self.policy.max_bytes:
            del self._preview[: len(self._preview) - self.policy.max_bytes]
        lines = self._preview.splitlines(keepends=True)
        if len(lines) > self.policy.max_lines:
            self._preview = bytearray(b"".join(lines[-self.policy.max_lines :]))

    def _write_spill(self, chunk: bytes | bytearray) -> None:
        if self._spill is None or not chunk:
            return
        remaining = self.policy.max_spill_bytes - self._spill_bytes
        if remaining > 0:
            selected = chunk[:remaining]
            self._spill.write(selected)
            self._spill_bytes += len(selected)
        self._dropped_bytes += max(0, len(chunk) - max(0, remaining))


def truncate_text(
    value: str,
    policy: OutputPolicy,
    *,
    mode: Literal["head", "tail"] = "head",
) -> tuple[str, bool]:
    data = value.encode("utf-8")
    lines = value.splitlines(keepends=True)
    truncated = len(data) > policy.max_bytes or len(lines) > policy.max_lines
    if not truncated:
        return value, False
    if mode == "head":
        selected = "".join(lines[: policy.max_lines]).encode("utf-8")[
            : policy.max_bytes
        ]
        while selected:
            try:
                return selected.decode("utf-8"), True
            except UnicodeDecodeError:
                selected = selected[:-1]
    else:
        selected = "".join(lines[-policy.max_lines :]).encode("utf-8")[
            -policy.max_bytes :
        ]
        while selected:
            try:
                return selected.decode("utf-8"), True
            except UnicodeDecodeError:
                selected = selected[1:]
    return "", True


def _decode_bounded_utf8(data: bytes, *, mode: Literal["head", "tail"]) -> str:
    """只在最终 preview 边界移除不完整码点，内部无效字节仍安全替换。"""

    if not data:
        return ""
    bounded = data
    if mode == "head":
        try:
            return bounded.decode("utf-8")
        except UnicodeDecodeError as error:
            if error.end == len(bounded):
                bounded = bounded[: error.start]
        return bounded.decode("utf-8", errors="replace")
    for _ in range(min(4, len(bounded)) + 1):
        try:
            return bounded.decode("utf-8")
        except UnicodeDecodeError as error:
            if error.start > 3:
                return bounded.decode("utf-8", errors="replace")
            bounded = bounded[1:]
    return bounded.decode("utf-8", errors="replace")
