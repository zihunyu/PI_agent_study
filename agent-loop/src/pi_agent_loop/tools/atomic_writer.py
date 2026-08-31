"""同目录临时文件发布。"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from .errors import WorkspaceToolError


class AtomicFileWriter:
    """使用同目录临时文件和 replace/link 发布完整字节。"""

    def write(self, path: Path, data: bytes, *, create_only: bool) -> None:
        original_mode: int | None = None
        if path.exists():
            original_mode = stat.S_IMODE(path.stat().st_mode)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        published = False
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if original_mode is not None:
                os.chmod(temporary, original_mode)
            if create_only:
                try:
                    os.link(temporary, path)
                except FileExistsError as error:
                    raise WorkspaceToolError(
                        "stale_observation",
                        "目标文件已被并发创建，请先 read",
                        path=str(path),
                    ) from error
                published = True
                try:
                    temporary.unlink()
                except OSError:
                    # Target 已原子发布；清理失败不能谎报“写入失败”。
                    pass
            else:
                os.replace(temporary, path)
                published = True
            self._sync_directory(path.parent)
        except WorkspaceToolError:
            raise
        except OSError as error:
            if published:
                raise WorkspaceToolError(
                    "durability_unknown",
                    "文件内容已发布，但目录持久化状态无法确认",
                    path=str(path),
                    details={"outcomeUnknown": True},
                ) from error
            raise WorkspaceToolError(
                "write_failed",
                "原子写入失败，原文件保持不变",
                path=str(path),
            ) from error
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
