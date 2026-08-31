"""UTF-8 文本文件解码，显式保留 BOM。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import WorkspaceToolError


UTF8_BOM = b"\xef\xbb\xbf"


@dataclass(frozen=True, slots=True)
class DecodedTextFile:
    text: str
    had_bom: bool

    def encode(self) -> bytes:
        payload = self.text.encode("utf-8")
        return UTF8_BOM + payload if self.had_bom else payload


def decode_text_file(data: bytes, *, path: str) -> DecodedTextFile:
    if b"\x00" in data:
        raise WorkspaceToolError(
            "binary_file",
            "目标包含 NUL，不作为 UTF-8 文本读取",
            path=path,
        )
    had_bom = data.startswith(UTF8_BOM)
    payload = data[len(UTF8_BOM) :] if had_bom else data
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise WorkspaceToolError(
            "binary_file",
            "目标不是有效 UTF-8 文本",
            path=path,
        ) from error
    return DecodedTextFile(text=text, had_bom=had_bom)


def read_bytes_limited(path: Path, maximum_bytes: int) -> bytes:
    """以硬上限读取文件，避免 stat/read 竞态把进程拖入无界内存。"""

    if maximum_bytes < 1:
        raise ValueError("maximum_bytes 必须是正整数")
    try:
        with path.open("rb") as stream:
            data = stream.read(maximum_bytes + 1)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise WorkspaceToolError(
            "read_error",
            "无法读取目标文件",
            path=str(path),
        ) from error
    if len(data) > maximum_bytes:
        raise WorkspaceToolError(
            "file_too_large",
            f"文件超过 {maximum_bytes} 字节读取上限",
            path=str(path),
            details={"maximumBytes": maximum_bytes},
        )
    return data
