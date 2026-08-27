"""写操作 outcome_unknown 的显式状态核对注册表。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from ..cancellation import CancellationToken
from ..types import AgentToolResult
from .errors import OutcomeUnknownToolError

Reconciler = Callable[
    [OutcomeUnknownToolError, CancellationToken],
    Awaitable[AgentToolResult],
]


class OutcomeReconciliationRegistry:
    """按名称注册状态核对器；核对不是重放原写操作。"""

    def __init__(self) -> None:
        self._reconcilers: dict[str, Reconciler] = {}

    def register(self, name: str, reconciler: Reconciler) -> None:
        if not name.strip():
            raise ValueError("reconciliation name 不能为空")
        if name in self._reconcilers:
            raise ValueError(f"状态核对器已经注册：{name}")
        self._reconcilers[name] = reconciler

    async def reconcile(
        self,
        error: OutcomeUnknownToolError,
        cancellation: CancellationToken,
    ) -> AgentToolResult:
        reconciler = self._reconcilers.get(error.reconciliation_name)
        if reconciler is None:
            raise KeyError(f"状态核对器不存在：{error.reconciliation_name}")
        cancellation.throw_if_cancelled()
        result = await reconciler(error, cancellation)
        cancellation.throw_if_cancelled()
        return result
