"""所有工作区工具共享的路径边界。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import WorkspaceToolError

_DEFAULT_RESERVED_NAMES = frozenset({".pi-agent-output"})


@dataclass(frozen=True, slots=True, init=False)
class WorkspacePathPolicy:
    """解析 realpath，并拒绝相对路径、symlink 或新父路径逃逸。

    这是稳定文件系统布局下的应用层边界，不是用来抵抗拥有本机文件系统
    修改权限的攻击者的 OS 沙箱。调用者应在实际 I/O 前重新解析路径。
    """

    workspace_root: Path
    reserved_names: frozenset[str]

    def __init__(
        self,
        workspace_root: str | os.PathLike[str],
        *,
        reserved_names: frozenset[str] = _DEFAULT_RESERVED_NAMES,
    ) -> None:
        try:
            root = Path(workspace_root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ValueError("workspace 必须是可访问的现有目录") from error
        if not root.is_dir():
            raise ValueError("workspace 必须是目录")
        object.__setattr__(self, "workspace_root", root)
        object.__setattr__(
            self,
            "reserved_names",
            frozenset(name.casefold() for name in reserved_names),
        )

    def resolve(
        self,
        value: str | os.PathLike[str],
        *,
        must_exist: bool,
    ) -> Path:
        if not isinstance(value, (str, os.PathLike)):
            raise WorkspaceToolError("invalid_args", "path 必须是字符串")
        raw = os.fspath(value)
        if not raw or "\x00" in raw:
            raise WorkspaceToolError("invalid_args", "path 不能为空")
        supplied = Path(raw).expanduser()
        candidate = (
            supplied if supplied.is_absolute() else self.workspace_root / supplied
        )
        candidate = Path(os.path.abspath(candidate))
        if os.name == "nt" and any(":" in part for part in candidate.parts[1:]):
            raise WorkspaceToolError(
                "path_invalid",
                "Windows 路径不允许 Alternate Data Stream",
                path=raw,
            )
        self._assert_inside(candidate, original=raw)
        self._assert_not_reserved(candidate, original=raw)

        try:
            if os.path.lexists(candidate):
                resolved = candidate.resolve(strict=True)
            else:
                if must_exist:
                    raise WorkspaceToolError(
                        "not_found",
                        "目标路径不存在",
                        path=str(candidate),
                    )
                resolved = self._resolve_new_path(candidate)
        except WorkspaceToolError:
            raise
        except (OSError, RuntimeError) as error:
            raise WorkspaceToolError(
                "not_found" if must_exist else "path_invalid",
                "无法安全解析目标路径",
                path=str(candidate),
            ) from error
        self._assert_inside(resolved, original=raw)
        self._assert_not_reserved(resolved, original=raw)
        return resolved

    def revalidate(self, raw: str, expected: Path, *, must_exist: bool) -> Path:
        current = self.resolve(raw, must_exist=must_exist)
        if self.key(current) != self.key(expected):
            raise WorkspaceToolError(
                "path_changed",
                "路径在校验后发生变化，请重试",
                path=str(current),
            )
        return current

    def relative(self, path: Path) -> str:
        self._assert_inside(path, original=str(path))
        relative = path.relative_to(self.workspace_root)
        return "." if not relative.parts else relative.as_posix()

    @staticmethod
    def key(path: Path) -> str:
        return os.path.normcase(os.path.normpath(str(path)))

    def _resolve_new_path(self, candidate: Path) -> Path:
        ancestor = candidate
        suffix: list[str] = []
        while not os.path.lexists(ancestor):
            if ancestor.parent == ancestor:
                raise WorkspaceToolError(
                    "path_invalid",
                    "无法找到目标路径的现有父目录",
                    path=str(candidate),
                )
            suffix.append(ancestor.name)
            ancestor = ancestor.parent
        real_ancestor = ancestor.resolve(strict=True)
        self._assert_inside(real_ancestor, original=str(candidate))
        return real_ancestor.joinpath(*reversed(suffix))

    def _assert_inside(self, path: Path, *, original: str) -> None:
        try:
            common = os.path.commonpath((self.key(self.workspace_root), self.key(path)))
        except ValueError as error:
            raise WorkspaceToolError(
                "path_outside_workspace",
                "路径超出工作区",
                path=original,
            ) from error
        if common != self.key(self.workspace_root):
            raise WorkspaceToolError(
                "path_outside_workspace",
                "路径超出工作区",
                path=original,
            )

    def _assert_not_reserved(self, path: Path, *, original: str) -> None:
        try:
            relative = path.relative_to(self.workspace_root)
        except ValueError:
            return
        if relative.parts and relative.parts[0].casefold() in self.reserved_names:
            raise WorkspaceToolError(
                "path_reserved",
                "该路径由 Tool Runtime 保留，不能通过工作区文件工具访问",
                path=original,
            )
