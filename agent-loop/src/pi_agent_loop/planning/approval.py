"""Plan Step 的 Approval Barrier 适配边界。"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, cast

from ..cancellation import CancellationToken
from .types import (
    PlanApprovalDecision,
    PlanExecutionError,
    PlanExecutionState,
    PlanStep,
)


ApprovalResolver = Callable[[PlanStep, PlanExecutionState, CancellationToken], Any]


class DurablePlanApprovalBackend(Protocol):
    """Product-owned durable approval lookup/create boundary.

    Implementations should make ``plan_id + step_id + action_hash`` idempotent
    and persist the approver decision before returning it.
    """

    def authorize_plan_step(
        self,
        *,
        plan_id: str,
        step_id: str,
        action_hash: str,
        state_version: int,
        cancellation: CancellationToken,
    ) -> Any: ...


class ApprovalBarrier:
    """把产品 Approval Service 适配为精确 Action Hash 的 Plan Barrier。"""

    def __init__(self, resolver: ApprovalResolver) -> None:
        self.resolver = resolver

    async def authorize(
        self,
        step: PlanStep,
        state: PlanExecutionState,
        cancellation: CancellationToken,
    ) -> PlanApprovalDecision:
        cancellation.throw_if_cancelled()
        value = self.resolver(step, state, cancellation)
        resolved = await cast(Awaitable[Any], value) if inspect.isawaitable(value) else value
        if not isinstance(resolved, PlanApprovalDecision):
            raise PlanExecutionError(
                "Approval Barrier 必须返回绑定 Action Hash 的 PlanApprovalDecision"
            )
        if resolved.action_hash != step.action_hash:
            raise PlanExecutionError("Approval Barrier 返回的 Action Hash 不匹配")
        cancellation.throw_if_cancelled()
        return resolved


class DurablePlanApprovalAdapter(ApprovalBarrier):
    """Adapt a durable product approval backend and fail closed on tampering."""

    def __init__(self, backend: DurablePlanApprovalBackend) -> None:
        self.backend = backend
        super().__init__(self._resolve)

    async def _resolve(
        self,
        step: PlanStep,
        state: PlanExecutionState,
        cancellation: CancellationToken,
    ) -> PlanApprovalDecision:
        cancellation.throw_if_cancelled()
        value = self.backend.authorize_plan_step(
            plan_id=state.plan_id,
            step_id=step.step_id,
            action_hash=step.action_hash,
            state_version=state.version,
            cancellation=cancellation,
        )
        resolved = await cast(Awaitable[Any], value) if inspect.isawaitable(value) else value
        if isinstance(resolved, Mapping):
            allowed = {"approved", "actionHash", "approvalId", "reason"}
            if set(resolved) - allowed:
                raise PlanExecutionError("Durable Approval 返回了未知字段")
            approved = resolved.get("approved")
            if not isinstance(approved, bool):
                raise PlanExecutionError("Durable Approval approved 必须是布尔值")
            action_hash = resolved.get("actionHash")
            if not isinstance(action_hash, str) or not action_hash:
                raise PlanExecutionError("Durable Approval 缺少 Action Hash")
            approval_id = resolved.get("approvalId")
            reason = resolved.get("reason")
            resolved = PlanApprovalDecision(
                approved=approved,
                action_hash=action_hash,
                approval_id=(approval_id if isinstance(approval_id, str) else None),
                reason=(reason if isinstance(reason, str) else None),
            )
        if not isinstance(resolved, PlanApprovalDecision):
            raise PlanExecutionError(
                "Durable Approval Backend 必须返回 PlanApprovalDecision 或严格对象"
            )
        if resolved.action_hash != step.action_hash:
            raise PlanExecutionError("Durable Approval Action Hash 不匹配")
        cancellation.throw_if_cancelled()
        return resolved


__all__ = [
    "ApprovalBarrier",
    "ApprovalResolver",
    "DurablePlanApprovalAdapter",
    "DurablePlanApprovalBackend",
]
