"""工作区内置工具的结构化错误。"""

from __future__ import annotations

import copy
from typing import Any

from ..retry import DefinitelyNotCommittedToolError


class WorkspaceToolError(RuntimeError):
    """可行动、可审计且不泄露底层异常细节的工具错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.details = copy.deepcopy(details or {})
        if path is not None:
            self.details.setdefault("path", path)

    def to_details(self) -> dict[str, Any]:
        return {"code": self.code, **copy.deepcopy(self.details)}


class WorkspaceToolPreconditionError(
    DefinitelyNotCommittedToolError,  # type: ignore[misc]
    WorkspaceToolError,
):
    """写 Handler 已证明尚未进入文件发布步骤的结构化失败。

    该类型只能用于路径、CAS、文本校验等提交前失败。原子写开始后发生的异常
    必须保持普通 ``WorkspaceToolError``/底层异常，让 Runtime 保守归类为
    ``outcome_unknown``。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        # 两个基类均有带参数的 __init__，这里直接初始化 Exception 并建立两边
        # Runtime 所需的字段，避免协作式 MRO 把参数误传给另一基类。
        Exception.__init__(self, message)
        self.code = code
        self.public_message = message
        self.path = path
        self.details = copy.deepcopy(details or {})
        if path is not None:
            self.details.setdefault("path", path)

    @classmethod
    def from_workspace_error(
        cls,
        error: WorkspaceToolError,
    ) -> "WorkspaceToolPreconditionError":
        return cls(
            error.code,
            str(error),
            path=error.path,
            details=error.details,
        )
