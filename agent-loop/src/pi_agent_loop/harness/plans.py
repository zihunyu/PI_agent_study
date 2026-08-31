"""Durable Host orchestration for Multi-Intent plans."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any
from uuid import uuid4

from ..cancellation import CancellationToken, OperationCancelledError
from ..planning import (
    ApprovalBarrier,
    DurablePlanStore,
    HybridRequestPlanner,
    IntentPlanPolicy,
    MultiIntentPlan,
    PlanBudgetExceeded,
    PlanExecutionConflictError,
    PlanExecutionLeaseLostError,
    PlanExecutionResult,
    PlanExecutor,
    PlanStepResourceReserver,
    validate_durable_plan_store,
)
from ..routing.types import TaskDecision
from ..session.operation_store import ClaimLease
from ..types import ToolDispatchContext


class PlanWorkflowUnavailableError(RuntimeError):
    """A plan boundary is unavailable, with optional reviewed public text."""

    def __init__(self, message: str, *, public_message: str | None = None) -> None:
        super().__init__(message)
        if public_message is not None and (
            not isinstance(public_message, str) or not public_message.strip()
        ):
            raise ValueError("PlanWorkflowUnavailableError public_message 不能为空")
        self.public_message = (
            public_message.strip() if public_message is not None else None
        )


class DurablePlanWorkflow:
    """Facade helper that binds planner execution to Journal CAS and leases."""

    supports_resource_admission = True

    def __init__(
        self,
        *,
        store: DurablePlanStore | None,
        planner: HybridRequestPlanner | None,
        policies: Mapping[str, IntentPlanPolicy] | None,
        step_executor: Any | None,
        approval_barrier: ApprovalBarrier | None,
        max_parallel_steps: int = 4,
        lease_seconds: float = 30,
        dispatch_context_provider: Callable[[], ToolDispatchContext] | None = None,
        distributed_execution: bool = False,
    ) -> None:
        if (
            isinstance(max_parallel_steps, bool)
            or not isinstance(max_parallel_steps, int)
            or max_parallel_steps < 1
        ):
            raise ValueError("max_parallel_plan_steps 必须是大于 0 的整数")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, (int, float)):
            raise ValueError("plan_lease_seconds 必须是正数")
        if lease_seconds <= 0:
            raise ValueError("plan_lease_seconds 必须是正数")
        if type(distributed_execution) is not bool:
            raise TypeError("distributed_execution 必须是布尔值")
        self.store = (
            validate_durable_plan_store(
                store,
                require_multi_host=distributed_execution,
            )
            if store is not None
            else None
        )
        self.planner = planner
        self.policies = dict(policies or (planner.policies if planner is not None else {}))
        self.step_executor = step_executor
        self.approval_barrier = approval_barrier
        self.max_parallel_steps = max_parallel_steps
        self.lease_seconds = float(lease_seconds)
        self.dispatch_context_provider = dispatch_context_provider
        self.distributed_execution = distributed_execution

    async def plan(
        self,
        request: str,
        *,
        task_decision: TaskDecision | None = None,
        persist: bool = True,
        dispatchable: bool = True,
        max_steps: int | None = None,
    ) -> MultiIntentPlan:
        store = self._require_store()
        if type(persist) is not bool or type(dispatchable) is not bool:
            raise TypeError("persist 和 dispatchable 必须是布尔值")
        if self.planner is None:
            raise PlanWorkflowUnavailableError("Host 未配置 HybridRequestPlanner")
        plan = await self.planner.plan(request, task_decision=task_decision)
        if max_steps is not None:
            if (
                isinstance(max_steps, bool)
                or not isinstance(max_steps, int)
                or max_steps < 0
            ):
                raise ValueError("max_steps 必须是非负整数或 None")
            if len(plan.steps) > max_steps:
                raise PlanBudgetExceeded(
                    f"Plan 包含 {len(plan.steps)} 个 Step，超过预算 {max_steps}"
                )
        if persist:
            await store.initialize(plan, dispatchable=dispatchable)
        return plan

    async def execute(
        self,
        plan_id: str,
        *,
        cancellation: CancellationToken | None = None,
        resource_reserver: PlanStepResourceReserver | None = None,
    ) -> PlanExecutionResult:
        store = self._require_store()
        if self.step_executor is None:
            raise PlanWorkflowUnavailableError("Host 未配置 Plan Step Executor")
        if not self.policies:
            raise PlanWorkflowUnavailableError("Host 未配置可信 Intent Plan Policy")
        token = cancellation or CancellationToken()
        token.throw_if_cancelled()
        # Load before acquiring the lease.  Subsequent atomic append uses the
        # exact Journal head as CAS, so a transition committed between this
        # snapshot and lease acquisition is still rejected.  This also avoids
        # spending a short execution lease on decrypting/replaying the plan.
        record = await store.load(plan_id)
        if not record.dispatchable:
            raise PlanWorkflowUnavailableError(
                f"Plan 尚未绑定 Durable Run/Conversation，禁止派发：{plan_id}"
            )
        dangerous_steps = tuple(
            step
            for step in record.plan.steps
            if (
                step.write
                or step.requires_approval
                or step.replay_policy == "never"
            )
        )
        if dangerous_steps:
            # Import locally to keep the Plan facade independent of the Tool
            # adapter at module-import time.
            from .plan_tool_runtime import ToolRuntimePlanStepExecutor

            if not isinstance(self.step_executor, ToolRuntimePlanStepExecutor):
                raise PlanWorkflowUnavailableError(
                    "危险 Durable Plan 必须使用框架 "
                    "ToolRuntimePlanStepExecutor，禁止直接回调执行"
                )
            if any(
                step.write or step.replay_policy == "never"
                for step in dangerous_steps
            ) and not self.step_executor.durable_write_boundary_configured:
                raise PlanWorkflowUnavailableError(
                    "写/不可重放 Durable Plan 缺少 WriteOperationService "
                    "或可信 Write Metadata Provider"
                )
        owner_token = store.new_owner_token()
        lease = await store.acquire_execution(
            plan_id,
            owner_token,
            lease_seconds=self.lease_seconds,
        )
        if lease is None:
            raise PlanExecutionConflictError(f"Plan 正由另一个 Worker 执行：{plan_id}")
        # One synchronous ownership check before any Handler can run makes a
        # worker that already lost/cannot renew its lease fail closed.  Later
        # event commits use the stronger atomic append+renew boundary.
        if not await store.renew_execution(
            lease,
            lease_seconds=self.lease_seconds,
        ):
            await store.release_execution_lease(lease)
            raise PlanExecutionLeaseLostError(
                f"Plan Execution Lease 在派发前已丢失：{plan_id}"
            )

        lease_lost = asyncio.Event()
        renewal = asyncio.create_task(
            self._renew_lease(store, lease, lease_lost),
            name=f"plan-lease:{plan_id}",
        )
        execution: asyncio.Task[PlanExecutionResult] | None = None
        cancellation_wait: asyncio.Task[None] | None = None
        lease_wait: asyncio.Task[bool] | None = None
        try:
            head = record.last_journal_sequence
            persisted_record = record
            run_id = str(uuid4())

            async def persist(event):
                nonlocal head, persisted_record
                # 每一个 Durable Transition 都是一次提交边界。精确代际续租
                # 与事件追加在同一个 SQLite 事务中完成，避免 renew→append
                # 之间发生接管；后台 heartbeat 只负责长耗时 Handler 期间续租。
                if lease_lost.is_set():
                    lease_lost.set()
                    raise PlanExecutionLeaseLostError(
                        f"Plan Transition 提交时已失去 Execution Lease：{plan_id}"
                    )
                updated = await store.append_event(
                    plan_id,
                    event,
                    expected_last_sequence=head,
                    run_id=run_id,
                    lease=lease,
                    lease_seconds=self.lease_seconds,
                    validated_current=persisted_record,
                )
                head = updated.last_journal_sequence
                persisted_record = updated

            executor = PlanExecutor(
                record.plan,
                self.policies,
                self.step_executor,
                approval_barrier=self.approval_barrier,
                event_sink=persist,
                max_parallel_steps=self.max_parallel_steps,
                fencing_token=lease.fencing_token,
                fencing_scope=(
                    lease.resource_id
                ),
                identity=(
                    self._dispatch_context().identity
                    if self.dispatch_context_provider is not None
                    else None
                ),
                fenced_claim=lease,
                fenced_claim_lease_seconds=self.lease_seconds,
                resource_reserver=resource_reserver,
            )
            execution = asyncio.create_task(
                executor.execute(initial_state=record.state, cancellation=token),
                name=f"plan-execution:{plan_id}",
            )
            cancellation_wait = asyncio.create_task(
                token.wait(), name=f"plan-cancellation:{plan_id}"
            )
            lease_wait = asyncio.create_task(
                lease_lost.wait(), name=f"plan-lease-loss:{plan_id}"
            )
            await asyncio.wait(
                {execution, cancellation_wait, lease_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if token.cancelled and not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
                raise OperationCancelledError(token.reason)
            if lease_lost.is_set():
                token.cancel("Plan Execution Lease 已丢失")
                if not execution.done():
                    execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
                raise PlanExecutionLeaseLostError(
                    f"Plan Execution Lease 已丢失：{plan_id}"
                )
            result = await execution
            # Fence the result against lease expiry/takeover before exposing it.
            if not await store.renew_execution(
                lease,
                lease_seconds=self.lease_seconds,
            ):
                raise PlanExecutionLeaseLostError(
                    f"Plan 完成时已失去 Execution Lease：{plan_id}"
                )
            return result
        finally:
            for task in (cancellation_wait, lease_wait, renewal):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (cancellation_wait, lease_wait, renewal) if task is not None),
                return_exceptions=True,
            )
            if execution is not None and not execution.done():
                execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
            await store.release_execution_lease(lease)

    async def resume(
        self,
        plan_id: str,
        *,
        cancellation: CancellationToken | None = None,
    ) -> PlanExecutionResult:
        return await self.execute(plan_id, cancellation=cancellation)

    async def _renew_lease(
        self,
        store: DurablePlanStore,
        lease: ClaimLease,
        lease_lost: asyncio.Event,
    ) -> None:
        interval = max(0.001, min(self.lease_seconds / 3, 5.0))
        try:
            while True:
                await asyncio.sleep(interval)
                if not await store.renew_execution(
                    lease,
                    lease_seconds=self.lease_seconds,
                ):
                    lease_lost.set()
                    return
        except asyncio.CancelledError:
            raise
        except BaseException:
            # Store/network failures cannot be treated as proof that the lease is
            # still ours. Fail closed and cancel execution.
            lease_lost.set()

    def _require_store(self) -> DurablePlanStore:
        if self.store is None:
            raise PlanWorkflowUnavailableError(
                "Multi Intent Plan 需要 store_backend='journal'"
            )
        return self.store

    def _dispatch_context(self) -> ToolDispatchContext:
        if self.dispatch_context_provider is None:
            return ToolDispatchContext()
        value = self.dispatch_context_provider()
        if not isinstance(value, ToolDispatchContext):
            raise TypeError(
                "Plan dispatch_context_provider 必须返回 ToolDispatchContext"
            )
        return value


__all__ = ["DurablePlanWorkflow", "PlanWorkflowUnavailableError"]
