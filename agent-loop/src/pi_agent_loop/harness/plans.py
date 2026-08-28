"""Durable Host orchestration for Multi-Intent plans."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from ..cancellation import CancellationToken, OperationCancelledError
from ..planning import (
    ApprovalBarrier,
    HybridRequestPlanner,
    IntentPlanPolicy,
    MultiIntentPlan,
    PlanExecutionConflictError,
    PlanExecutionLeaseLostError,
    PlanExecutionResult,
    PlanExecutor,
    SessionJournalPlanStore,
)


class PlanWorkflowUnavailableError(RuntimeError):
    pass


class DurablePlanWorkflow:
    """Facade helper that binds planner execution to Journal CAS and leases."""

    def __init__(
        self,
        *,
        store: SessionJournalPlanStore | None,
        planner: HybridRequestPlanner | None,
        policies: Mapping[str, IntentPlanPolicy] | None,
        step_executor: Any | None,
        approval_barrier: ApprovalBarrier | None,
        max_parallel_steps: int = 4,
        lease_seconds: float = 30,
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
        self.store = store
        self.planner = planner
        self.policies = dict(policies or (planner.policies if planner is not None else {}))
        self.step_executor = step_executor
        self.approval_barrier = approval_barrier
        self.max_parallel_steps = max_parallel_steps
        self.lease_seconds = float(lease_seconds)

    async def plan(self, request: str) -> MultiIntentPlan:
        store = self._require_store()
        if self.planner is None:
            raise PlanWorkflowUnavailableError("Host 未配置 HybridRequestPlanner")
        plan = await self.planner.plan(request)
        await store.initialize(plan)
        return plan

    async def execute(
        self,
        plan_id: str,
        *,
        cancellation: CancellationToken | None = None,
    ) -> PlanExecutionResult:
        store = self._require_store()
        if self.step_executor is None:
            raise PlanWorkflowUnavailableError("Host 未配置 Plan Step Executor")
        if not self.policies:
            raise PlanWorkflowUnavailableError("Host 未配置可信 Intent Plan Policy")
        token = cancellation or CancellationToken()
        token.throw_if_cancelled()
        owner_token = store.new_owner_token()
        acquired = await store.try_acquire_execution(
            plan_id,
            owner_token,
            lease_seconds=self.lease_seconds,
        )
        if not acquired:
            raise PlanExecutionConflictError(f"Plan 正由另一个 Worker 执行：{plan_id}")

        lease_lost = asyncio.Event()
        renewal = asyncio.create_task(
            self._renew_lease(store, plan_id, owner_token, lease_lost),
            name=f"plan-lease:{plan_id}",
        )
        execution: asyncio.Task[PlanExecutionResult] | None = None
        cancellation_wait: asyncio.Task[None] | None = None
        lease_wait: asyncio.Task[bool] | None = None
        try:
            record = await store.load(plan_id)
            head = record.last_journal_sequence
            run_id = str(uuid4())

            async def persist(event):
                nonlocal head
                updated = await store.append_event(
                    plan_id,
                    event,
                    expected_last_sequence=head,
                    run_id=run_id,
                )
                head = updated.last_journal_sequence

            executor = PlanExecutor(
                record.plan,
                self.policies,
                self.step_executor,
                approval_barrier=self.approval_barrier,
                event_sink=persist,
                max_parallel_steps=self.max_parallel_steps,
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
            if not await store.try_acquire_execution(
                plan_id,
                owner_token,
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
            await store.release_execution(plan_id, owner_token)

    async def resume(
        self,
        plan_id: str,
        *,
        cancellation: CancellationToken | None = None,
    ) -> PlanExecutionResult:
        return await self.execute(plan_id, cancellation=cancellation)

    async def _renew_lease(
        self,
        store: SessionJournalPlanStore,
        plan_id: str,
        owner_token: str,
        lease_lost: asyncio.Event,
    ) -> None:
        interval = max(0.001, min(self.lease_seconds / 3, 5.0))
        try:
            while True:
                await asyncio.sleep(interval)
                if not await store.try_acquire_execution(
                    plan_id,
                    owner_token,
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

    def _require_store(self) -> SessionJournalPlanStore:
        if self.store is None:
            raise PlanWorkflowUnavailableError(
                "Multi Intent Plan 需要 store_backend='journal'"
            )
        return self.store


__all__ = ["DurablePlanWorkflow", "PlanWorkflowUnavailableError"]
