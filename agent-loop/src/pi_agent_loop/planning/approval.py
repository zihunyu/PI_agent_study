"""Plan Step 的 Approval Barrier 适配边界。"""

from __future__ import annotations

import asyncio
import inspect
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, cast

from ..cancellation import CancellationToken
from .types import (
    PlanApprovalDecision,
    PlanApprovalReceipt,
    PlanExecutionError,
    PlanExecutionState,
    PlanStep,
)


ApprovalResolver = Callable[[PlanStep, PlanExecutionState, CancellationToken], Any]
PlanApprovalReceiptConsumer = Callable[
    [PlanApprovalReceipt, PlanStep, PlanExecutionState, CancellationToken], Any
]


class DurablePlanApprovalBackend(Protocol):
    """Product-owned durable approval lookup/create boundary.

    Implementations should make ``plan_id + step_id + action_hash`` idempotent,
    persist the approver decision, and atomically mark a receipt consumed with
    CAS/transaction semantics.  A process-local boolean is not sufficient.
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

    def consume_plan_approval_receipt(
        self,
        *,
        receipt: Mapping[str, Any],
        plan_id: str,
        step_id: str,
        action_hash: str,
        required_roles: tuple[str, ...],
        state_version: int,
        cancellation: CancellationToken,
    ) -> Any: ...


class ApprovalBarrier:
    """把产品 Approval Service 适配为精确 Action Hash 的 Plan Barrier。"""

    def __init__(
        self,
        resolver: ApprovalResolver,
        *,
        receipt_consumer: PlanApprovalReceiptConsumer | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.resolver = resolver
        self.receipt_consumer = receipt_consumer
        self.clock = clock
        self._consume_lock = asyncio.Lock()
        self._consumed_receipt_ids: set[str] = set()

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
        current = state.steps.get(step.step_id)
        if current is None:
            raise PlanExecutionError("Approval Barrier 找不到目标 Plan Step 状态")
        if current.approval_id is not None and resolved.approval_id is not None:
            request_id_changed = current.approval_id != resolved.approval_id
            framework_placeholder = current.approval_id.startswith(
                "plan-approval-request-"
            )
            if request_id_changed and not framework_placeholder:
                raise PlanExecutionError(
                    "Approval Barrier 返回的 Approval ID 与等待中的请求不匹配"
                )
        if resolved.status == "approved":
            resolved = await self._consume_receipt(
                resolved,
                step,
                state,
                cancellation,
            )
        cancellation.throw_if_cancelled()
        return resolved

    async def _consume_receipt(
        self,
        decision: PlanApprovalDecision,
        step: PlanStep,
        state: PlanExecutionState,
        cancellation: CancellationToken,
    ) -> PlanApprovalDecision:
        receipt = decision.receipt
        if receipt is None:
            raise PlanExecutionError(
                "approved=True 或 Approval ID 不能替代已验证、一次性消费的 "
                "PlanApprovalReceipt"
            )
        if self.receipt_consumer is None:
            raise PlanExecutionError(
                "Plan Approval 缺少可信 PlanApprovalReceipt consumer"
            )
        now = _clock_value(self.clock)
        self._validate_issued_receipt(receipt, step, state, now)
        async with self._consume_lock:
            if receipt.receipt_id in self._consumed_receipt_ids:
                raise PlanExecutionError("PlanApprovalReceipt 已经消费，不能重复使用")
            cancellation.throw_if_cancelled()
            value = self.receipt_consumer(receipt, step, state, cancellation)
            consumed = (
                await cast(Awaitable[Any], value)
                if inspect.isawaitable(value)
                else value
            )
            if not isinstance(consumed, PlanApprovalReceipt):
                raise PlanExecutionError(
                    "PlanApprovalReceipt consumer 必须返回已消费的 Receipt"
                )
            now = _clock_value(self.clock)
            self._validate_consumed_receipt(receipt, consumed, step, state, now)
            self._consumed_receipt_ids.add(consumed.receipt_id)
        return PlanApprovalDecision.grant(step, consumed)

    @staticmethod
    def _validate_issued_receipt(
        receipt: PlanApprovalReceipt,
        step: PlanStep,
        state: PlanExecutionState,
        now: float,
    ) -> None:
        if receipt.consumed_at is not None:
            raise PlanExecutionError(
                "Resolver 必须返回未消费的 PlanApprovalReceipt；已消费状态需要人工核对"
            )
        if (
            receipt.plan_id != state.plan_id
            or receipt.step_id != step.step_id
            or receipt.state_version != state.version
        ):
            raise PlanExecutionError("PlanApprovalReceipt 与当前 Plan/Step/Version 不匹配")
        if receipt.action_hash != step.action_hash:
            raise PlanExecutionError("PlanApprovalReceipt Action Hash 不匹配")
        required_roles = frozenset(step.approval_roles)
        if not required_roles:
            raise PlanExecutionError("Plan Step 缺少可信 approval_roles 策略")
        if required_roles.isdisjoint(receipt.approver_roles):
            raise PlanExecutionError("PlanApprovalReceipt 审批人缺少所需角色")
        if now < receipt.issued_at:
            raise PlanExecutionError("PlanApprovalReceipt 尚未生效")
        if now > receipt.expires_at:
            raise PlanExecutionError("PlanApprovalReceipt 已经过期")

    @classmethod
    def _validate_consumed_receipt(
        cls,
        issued: PlanApprovalReceipt,
        consumed: PlanApprovalReceipt,
        step: PlanStep,
        state: PlanExecutionState,
        now: float,
    ) -> None:
        if consumed.authorization_binding != issued.authorization_binding:
            raise PlanExecutionError(
                "PlanApprovalReceipt consumer 篡改了审批绑定字段"
            )
        if consumed.consumed_at is None:
            raise PlanExecutionError("PlanApprovalReceipt 尚未完成一次性消费")
        if consumed.consumed_at > now:
            raise PlanExecutionError("PlanApprovalReceipt 消费时间来自未来")
        if now > consumed.expires_at:
            raise PlanExecutionError("PlanApprovalReceipt 在消费完成前已经过期")
        cls._validate_issued_receipt(
            issued,
            step,
            state,
            now,
        )


class DurablePlanApprovalAdapter(ApprovalBarrier):
    """Adapt a durable product approval backend and fail closed on tampering."""

    def __init__(
        self,
        backend: DurablePlanApprovalBackend,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.backend = backend
        super().__init__(
            self._resolve,
            receipt_consumer=self._consume,
            clock=clock,
        )

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
            allowed = {
                "status",
                "approved",
                "actionHash",
                "approvalId",
                "reason",
                "receipt",
            }
            if set(resolved) - allowed:
                raise PlanExecutionError("Durable Approval 返回了未知字段")
            if "status" in resolved:
                if "approved" in resolved:
                    raise PlanExecutionError(
                        "Durable Approval 不能同时返回 status 和 approved"
                    )
                status = resolved.get("status")
                if status not in {"pending", "approved", "denied"}:
                    raise PlanExecutionError(
                        "Durable Approval status 必须是 pending/approved/denied"
                    )
                approved: bool | None = {
                    "pending": None,
                    "approved": True,
                    "denied": False,
                }[cast(str, status)]
            else:
                approved = resolved.get("approved")
                if not isinstance(approved, bool):
                    raise PlanExecutionError(
                        "Durable Approval approved 必须是布尔值"
                    )
            action_hash = resolved.get("actionHash")
            if not isinstance(action_hash, str) or not action_hash:
                raise PlanExecutionError("Durable Approval 缺少 Action Hash")
            approval_id = resolved.get("approvalId")
            reason = resolved.get("reason")
            receipt = resolved.get("receipt")
            if isinstance(receipt, Mapping):
                try:
                    receipt = PlanApprovalReceipt.from_dict(receipt)
                except (TypeError, ValueError) as error:
                    raise PlanExecutionError(
                        f"Durable Approval Receipt 无效：{error}"
                    ) from error
            elif receipt is not None:
                raise PlanExecutionError("Durable Approval receipt 必须是严格对象")
            resolved = PlanApprovalDecision(
                approved=approved,
                action_hash=action_hash,
                approval_id=(approval_id if isinstance(approval_id, str) else None),
                reason=(reason if isinstance(reason, str) else None),
                receipt=cast(PlanApprovalReceipt | None, receipt),
            )
        if not isinstance(resolved, PlanApprovalDecision):
            raise PlanExecutionError(
                "Durable Approval Backend 必须返回 PlanApprovalDecision 或严格对象"
            )
        if resolved.action_hash != step.action_hash:
            raise PlanExecutionError("Durable Approval Action Hash 不匹配")
        cancellation.throw_if_cancelled()
        return resolved

    async def _consume(
        self,
        receipt: PlanApprovalReceipt,
        step: PlanStep,
        state: PlanExecutionState,
        cancellation: CancellationToken,
    ) -> PlanApprovalReceipt:
        consumer = getattr(self.backend, "consume_plan_approval_receipt", None)
        if not callable(consumer):
            raise PlanExecutionError(
                "Durable Approval Backend 必须实现原子 Receipt 消费接口"
            )
        value = consumer(
            receipt=receipt.to_dict(),
            plan_id=state.plan_id,
            step_id=step.step_id,
            action_hash=step.action_hash,
            required_roles=step.approval_roles,
            state_version=state.version,
            cancellation=cancellation,
        )
        resolved = await cast(Awaitable[Any], value) if inspect.isawaitable(value) else value
        if isinstance(resolved, Mapping):
            try:
                return PlanApprovalReceipt.from_dict(resolved)
            except (TypeError, ValueError) as error:
                raise PlanExecutionError(
                    f"Durable Approval 消费结果无效：{error}"
                ) from error
        if not isinstance(resolved, PlanApprovalReceipt):
            raise PlanExecutionError(
                "Durable Approval 消费接口必须返回 PlanApprovalReceipt"
            )
        return resolved


def _clock_value(clock: Callable[[], float]) -> float:
    value = clock()
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise PlanExecutionError("Plan Approval clock 必须返回有限时间戳")
    return float(value)


__all__ = [
    "ApprovalBarrier",
    "ApprovalResolver",
    "DurablePlanApprovalAdapter",
    "DurablePlanApprovalBackend",
    "PlanApprovalReceiptConsumer",
]
