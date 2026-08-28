"""依赖感知、审批感知且可恢复的 Multi Intent PlanExecutor。"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, cast

from ..cancellation import CancellationToken, OperationCancelledError
from .approval import ApprovalBarrier
from .graph import DependencyGraph, PlanValidator
from .state_machine import TaskStateMachine
from .synthesizer import ResultSynthesizer
from .types import (
    IntentPlanPolicy,
    MultiIntentPlan,
    PlanEvent,
    PlanExecutionError,
    PlanExecutionResult,
    PlanExecutionState,
    PlanStep,
)


StepExecutor = Callable[[PlanStep, CancellationToken], Any]
PlanEventSink = Callable[[PlanEvent], Any]


class PlanExecutor:
    def __init__(
        self,
        plan: MultiIntentPlan,
        policies: dict[str, IntentPlanPolicy],
        step_executor: StepExecutor,
        *,
        approval_barrier: ApprovalBarrier | None = None,
        result_synthesizer: ResultSynthesizer | None = None,
        event_sink: PlanEventSink | None = None,
        max_parallel_steps: int = 4,
        fail_fast: bool = False,
    ) -> None:
        if (
            isinstance(max_parallel_steps, bool)
            or not isinstance(max_parallel_steps, int)
            or max_parallel_steps < 1
        ):
            raise ValueError("max_parallel_steps 必须是大于 0 的整数")
        self.plan = plan
        self.graph: DependencyGraph = PlanValidator(policies).validate(plan)
        self.machine = TaskStateMachine(plan, self.graph)
        self.step_executor = step_executor
        self.approval_barrier = approval_barrier
        self.result_synthesizer = result_synthesizer or ResultSynthesizer()
        self.event_sink = event_sink
        self.max_parallel_steps = max_parallel_steps
        self.fail_fast = fail_fast
        self._transition_lock = asyncio.Lock()
        self._run_lock = asyncio.Lock()
        self._state = self.machine.initial_state()
        self._events: list[PlanEvent] = []

    @property
    def state(self) -> PlanExecutionState:
        return self._state

    async def execute(
        self,
        *,
        initial_state: PlanExecutionState | None = None,
        cancellation: CancellationToken | None = None,
    ) -> PlanExecutionResult:
        token = cancellation or CancellationToken()
        async with self._run_lock:
            self._events = []
            self._state = (
                self.machine.initial_state()
                if initial_state is None
                else self.machine.state_from_dict(initial_state.to_dict())
            )
            await self._recover_running_steps()
            while True:
                token.throw_if_cancelled()
                changed = await self._skip_blocked_steps()
                if self.fail_fast and any(
                    value.status == "failed" for value in self._state.steps.values()
                ):
                    changed = await self._skip_all_pending("Plan fail_fast") or changed

                if self.approval_barrier is not None:
                    for step_id in self.graph.topological_order:
                        if self._state.steps[step_id].status != "waiting_approval":
                            continue
                        await self._resolve_approval(
                            self.plan.step(step_id), token
                        )
                        changed = True

                ready = list(self.graph.ready_steps(self._state))
                for step_id in ready:
                    step = self.plan.step(step_id)
                    current = self._state.steps[step_id]
                    if not step.requires_approval or current.approval_id is not None:
                        continue
                    await self._transition(
                        "step_waiting_approval",
                        step_id,
                        {"actionHash": step.action_hash},
                    )
                    changed = True
                    if self.approval_barrier is None:
                        continue
                    await self._resolve_approval(step, token)

                executable = [
                    step_id
                    for step_id in self.graph.ready_steps(self._state)
                    if (
                        not self.plan.step(step_id).requires_approval
                        or self._state.steps[step_id].approval_id is not None
                    )
                ]
                if executable:
                    await self._execute_batch(executable[: self.max_parallel_steps], token)
                    continue
                if not changed:
                    break
                # 本轮只推进到 Waiting Approval、Failed 或 Skipped，重新计算一次；
                # 如果没有新的 Ready Step，下一轮会自然结束。
                changed = False
            synthesized = self.result_synthesizer.synthesize(
                self.plan, self._state, self.graph
            )
            return PlanExecutionResult(
                state=self._state,
                synthesized=synthesized,
                events=tuple(self._events),
            )

    async def _resolve_approval(
        self,
        step: PlanStep,
        cancellation: CancellationToken,
    ) -> None:
        if self.approval_barrier is None:
            raise PlanExecutionError("Plan 缺少 Approval Barrier")
        decision = await self.approval_barrier.authorize(
            step, self._state, cancellation
        )
        if decision.approved:
            await self._transition(
                "step_approval_granted",
                step.step_id,
                {
                    "approvalId": decision.approval_id,
                    "actionHash": decision.action_hash,
                },
            )
        else:
            await self._transition(
                "step_approval_denied",
                step.step_id,
                {
                    "reason": decision.reason,
                    "actionHash": decision.action_hash,
                },
            )

    async def _recover_running_steps(self) -> None:
        for step_id in self.graph.topological_order:
            if self._state.steps[step_id].status != "running":
                continue
            step = self.plan.step(step_id)
            await self._transition(
                "step_recovered",
                step_id,
                {
                    "targetStatus": (
                        "pending"
                        if step.replay_policy == "safe"
                        else "manual_intervention"
                    )
                },
            )

    async def _skip_blocked_steps(self) -> bool:
        changed = False
        while True:
            blocked = self.graph.blocked_steps(self._state)
            if not blocked:
                return changed
            for step_id in blocked:
                failed_dependencies = [
                    dependency
                    for dependency in self.graph.dependencies[step_id]
                    if self._state.steps[dependency].status
                    in {"failed", "skipped", "manual_intervention"}
                ]
                await self._transition(
                    "step_skipped",
                    step_id,
                    {
                        "reason": (
                            "依赖未成功：" + ",".join(sorted(failed_dependencies))
                        )
                    },
                )
                changed = True

    async def _skip_all_pending(self, reason: str) -> bool:
        changed = False
        for step_id in self.graph.topological_order:
            if self._state.steps[step_id].status not in {
                "pending",
                "waiting_approval",
            }:
                continue
            await self._transition("step_skipped", step_id, {"reason": reason})
            changed = True
        return changed

    async def _execute_batch(
        self,
        step_ids: list[str],
        cancellation: CancellationToken,
    ) -> None:
        tasks = [
            asyncio.create_task(
                self._execute_one(self.plan.step(step_id), cancellation),
                name=f"plan-step:{self.plan.plan_id}:{step_id}",
            )
            for step_id in step_ids
        ]
        batch = asyncio.create_task(
            _wait_for_all(tasks),
            name=f"plan-batch:{self.plan.plan_id}",
        )
        cancellation_wait = asyncio.create_task(
            cancellation.wait(),
            name=f"plan-batch-cancellation:{self.plan.plan_id}",
        )
        try:
            await asyncio.wait(
                {batch, cancellation_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation.cancelled and not batch.done():
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                cancellation.throw_if_cancelled()
            await batch
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if not batch.done():
                batch.cancel()
            await asyncio.gather(batch, return_exceptions=True)
            raise
        finally:
            if not cancellation_wait.done():
                cancellation_wait.cancel()
            await asyncio.gather(cancellation_wait, return_exceptions=True)

    async def _execute_one(
        self,
        step: PlanStep,
        cancellation: CancellationToken,
    ) -> None:
        cancellation.throw_if_cancelled()
        await self._transition("step_started", step.step_id, {})
        try:
            value = self.step_executor(step, cancellation)
            result = await cast(Awaitable[Any], value) if inspect.isawaitable(value) else value
            cancellation.throw_if_cancelled()
            await self._transition(
                "step_succeeded",
                step.step_id,
                {"result": result},
            )
        except (asyncio.CancelledError, OperationCancelledError):
            # Started 已经是恢复锚点；安全重放或人工介入由下一次 execute 决定。
            raise
        except Exception as error:
            await self._transition(
                "step_failed",
                step.step_id,
                {"error": str(error) or type(error).__name__},
            )

    async def _transition(
        self,
        event_type: str,
        step_id: str,
        data: dict[str, Any],
    ) -> None:
        async with self._transition_lock:
            event = PlanEvent(
                sequence=self._state.version + 1,
                type=event_type,
                step_id=step_id,
                data=data,
            )
            next_state = self.machine.apply(self._state, event)
            if self.event_sink is not None:
                value = self.event_sink(event)
                if inspect.isawaitable(value):
                    await cast(Awaitable[Any], value)
            self._state = next_state
            self._events.append(event)


async def _wait_for_all(tasks: list[asyncio.Task[None]]) -> None:
    await asyncio.gather(*tasks)
