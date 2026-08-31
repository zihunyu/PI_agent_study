"""Autonomous multi-intent plan execution with a bounded correction loop."""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

from ..cancellation import CancellationToken, OperationCancelledError
from ..planning import (
    ClosedLoopAction,
    ClosedLoopBudget,
    ClosedLoopEvent,
    ClosedLoopExecutor,
    ClosedLoopObservation,
    ClosedLoopResult,
    CorrectionPlan,
    CorrectionPlanner,
    MultiIntentPlan,
    PlanBudgetExceeded,
    PlanExecutionResult,
    PlanResourceUsage,
    PlanUsageMeter,
    PlanUsageReservation,
    PlanValidator,
    ResultValidation,
    ResultValidator,
    SessionJournalPlanStore,
)
from ..model_attempts import (
    ModelAttemptAdmissionScope,
    ModelAttemptAdmissionSnapshot,
    activate_model_attempt_admission,
)
from ..routing.types import TaskDecision
from ..session.operation_store import ClaimLease
from .autonomous_durability import (
    AutonomousRunControllerConflictError,
    AutonomousRunControllerLeaseLostError,
    AutonomousRunRecord,
    AutonomousRunStoreError,
    SessionJournalAutonomousRunStore,
)
from .plans import DurablePlanWorkflow, PlanWorkflowUnavailableError


AutonomousPlanStatus = Literal[
    "completed",
    "waiting_approval",
    "failed",
    "manual_intervention",
]


@dataclass(frozen=True, slots=True)
class AutonomousPlanObservation:
    """Immutable plan-level view passed to trusted validators and replanners."""

    request: str
    plans: tuple[MultiIntentPlan, ...]
    executions: tuple[PlanExecutionResult, ...]
    latest_error: str | None
    correction_rounds: int

    @property
    def latest_plan(self) -> MultiIntentPlan:
        if not self.plans:
            raise RuntimeError("Autonomous Plan Observation 没有 Plan")
        return self.plans[-1]

    @property
    def latest_execution(self) -> PlanExecutionResult | None:
        return self.executions[-1] if self.executions else None


class PlanResultValidator(Protocol):
    def __call__(
        self,
        observation: AutonomousPlanObservation,
        cancellation: CancellationToken,
    ) -> ResultValidation | Awaitable[ResultValidation]: ...


class PlanReplanner(Protocol):
    def __call__(
        self,
        observation: AutonomousPlanObservation,
        validation: ResultValidation,
        cancellation: CancellationToken,
    ) -> MultiIntentPlan | None | Awaitable[MultiIntentPlan | None]: ...


class PlanResultSynthesizer(Protocol):
    def __call__(
        self,
        observation: AutonomousPlanObservation,
        status: AutonomousPlanStatus,
        cancellation: CancellationToken,
    ) -> str | Awaitable[str]: ...


@dataclass(frozen=True, slots=True)
class AutonomousPlanResult:
    request: str
    status: AutonomousPlanStatus
    response_text: str
    plans: tuple[MultiIntentPlan, ...]
    executions: tuple[PlanExecutionResult, ...]
    closed_loop: ClosedLoopResult
    pending_approval_ids: tuple[str, ...] = ()
    closed_loop_run_id: str | None = None

    @property
    def plan_id(self) -> str:
        return self.plans[-1].plan_id


@dataclass(frozen=True, slots=True)
class AutonomousPreparedRun:
    """A durably initialized plan/run pair that has not been dispatched yet."""

    request: str
    plan: MultiIntentPlan
    run_id: str
    requires_conversation_bootstrap: bool = False
    started_at: float = 0.0
    initial_usage: PlanResourceUsage = PlanResourceUsage()


class AutonomousPlanRunner:
    """Plan, execute, validate and safely correct one complex request.

    Model output is never trusted to alter ``IntentPlanPolicy``.  Corrections are
    accepted only after ``PlanValidator`` and the closed-loop safety boundary
    agree that every automatically repeated action is read-only and replay-safe.
    """

    def __init__(
        self,
        workflow: DurablePlanWorkflow,
        *,
        result_validator: PlanResultValidator | None = None,
        replanner: PlanReplanner | None = None,
        result_synthesizer: PlanResultSynthesizer | None = None,
        budget: ClosedLoopBudget | None = None,
        event_sink: Callable[[ClosedLoopEvent], Any] | None = None,
        run_store: SessionJournalAutonomousRunStore | None = None,
        usage_meter: PlanUsageMeter | None = None,
    ) -> None:
        self.workflow = workflow
        self.result_validator = result_validator
        self.replanner = replanner
        self.result_synthesizer = result_synthesizer
        self.budget = budget or ClosedLoopBudget()
        default_budget = ClosedLoopBudget()
        self._explicit_step_tool_budget = budget is not None and (
            budget.max_step_attempts != default_budget.max_step_attempts
            or budget.max_tool_calls != default_budget.max_tool_calls
        )
        self.event_sink = event_sink
        self.run_store = run_store
        self._model_attempt_snapshots: dict[
            str, ModelAttemptAdmissionSnapshot
        ] = {}
        if usage_meter is not None and not (
            callable(getattr(usage_meter, "reserve", None))
            and callable(getattr(usage_meter, "settle", None))
            and callable(getattr(usage_meter, "cancel", None))
        ):
            raise TypeError(
                "usage_meter 必须实现 reserve/settle/cancel Admission 协议"
            )
        if (
            usage_meter is not None
            and any(
                value is not None
                for value in (
                    self.budget.max_model_calls,
                    self.budget.max_tokens,
                    self.budget.max_cost,
                )
            )
            and not callable(getattr(usage_meter, "dispatch", None))
        ):
            raise TypeError(
                "硬 model/token/cost 预算要求 usage_meter.dispatch 在 "
                "Provider 派发边界内施加整个 Retry Tree 的 Reservation 上限"
            )
        if usage_meter is None and any(
            value is not None
            for value in (
                self.budget.max_model_calls,
                self.budget.max_tokens,
                self.budget.max_cost,
            )
        ):
            raise ValueError(
                "配置 model/token/cost 预算时必须提供可预留并结算的 usage_meter"
            )
        if run_store is None and any(
            value is not None
            for value in (
                self.budget.max_model_calls,
                self.budget.max_tokens,
                self.budget.max_cost,
            )
        ):
            raise ValueError(
                "model/token/cost 硬预算必须使用 Durable Run Store，"
                "否则 Planner/Validator/Replanner/Synthesizer 会分别从零计数"
            )
        self.usage_meter = usage_meter

    @property
    def hard_model_budget_enabled(self) -> bool:
        """Whether every model-like stage must enter durable admission."""

        return any(
            value is not None
            for value in (
                self.budget.max_model_calls,
                self.budget.max_tokens,
                self.budget.max_cost,
            )
        )

    @property
    def pre_route_budget_enabled(self) -> bool:
        """Whether routing must share the request's durable deadline/ledger."""

        return (
            self.hard_model_budget_enabled
            or self.budget.max_duration_seconds is not None
        )

    async def open_pre_route_admission(
        self,
        request: str,
    ) -> tuple[str, PlanUsageReservation | None]:
        """Reserve the Router model call before the Router can reach Provider."""

        if not self.pre_route_budget_enabled:
            raise PlanBudgetExceeded("未配置 Router 所需的硬预算")
        if self.run_store is None:
            raise PlanBudgetExceeded("Router 硬预算需要 Durable Autonomous Run Store")
        run_id = str(uuid4())
        await self.run_store.open_admission(
            request,
            self.budget,
            run_id=run_id,
            started_at=time.time(),
        )
        ticket: PlanUsageReservation | None = None
        if self.hard_model_budget_enabled:
            try:
                ticket = await self._reserve_model_stage(
                    run_id,
                    "router",
                    durable=True,
                )
            except BaseException:
                await self.run_store.close_admission(
                    run_id,
                    reason="router_admission_rejected",
                )
                raise
        return run_id, ticket

    async def settle_pre_route_admission(
        self,
        run_id: str,
        ticket: PlanUsageReservation,
        *,
        reported_usage: PlanResourceUsage,
    ) -> PlanResourceUsage:
        """Settle Router usage and cross-check the Router's provider report."""

        return await self._settle_model_stage(
            run_id,
            ticket,
            durable=True,
            reported_usage=reported_usage,
        )

    async def dispatch_pre_route_admission(
        self,
        run_id: str,
        ticket: PlanUsageReservation | None,
        callback: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Run Router inside the same hard Provider-admission wrapper."""

        if self.run_store is None:
            raise PlanBudgetExceeded("Router Admission 缺少 Durable Run Store")
        record = await self.run_store.load(run_id)
        self._assert_deadline(record)

        async def invoke() -> Any:
            if ticket is None:
                return await callback()
            return await self._dispatch_reserved_model_callback(
                run_id,
                ticket,
                callback,
            )

        if record.deadline_at is None:
            return await invoke()
        remaining = record.deadline_at - time.time()
        if remaining <= 0:
            raise PlanBudgetExceeded("Router 派发前 wall-clock deadline 已到")
        try:
            async with asyncio.timeout(remaining):
                return await invoke()
        except TimeoutError as error:
            raise PlanBudgetExceeded(
                "Autonomous Run wall-clock duration 在 Router 阶段耗尽"
            ) from error

    async def release_pre_route_admission(
        self,
        run_id: str,
        ticket: PlanUsageReservation,
    ) -> None:
        """Release Router capacity when rule routing made no model call."""

        if self.usage_meter is None or self.run_store is None:
            raise PlanBudgetExceeded("Router Admission 缺少 Usage Meter 或 Run Store")
        value = self.usage_meter.cancel(ticket)
        if inspect.isawaitable(value):
            await cast(Awaitable[Any], value)
        self._model_attempt_snapshots.pop(ticket.reservation_id, None)
        await self.run_store.release_resources(
            run_id,
            reservation_id=ticket.reservation_id,
            reason="router_rule_path_not_dispatched",
        )

    async def close_pre_route_admission(
        self,
        run_id: str,
        *,
        reason: str,
    ) -> None:
        if self.run_store is None:
            raise PlanBudgetExceeded("Router Admission 缺少 Durable Run Store")
        await self.run_store.close_admission(run_id, reason=reason)

    async def prepare(
        self,
        request: str,
        *,
        task_decision: TaskDecision | None = None,
        run_id: str | None = None,
        defer_persistence_for_conversation: bool = False,
        cancellation: CancellationToken | None = None,
    ) -> AutonomousPreparedRun:
        """Plan and persist a stable closed-loop identity before any execution."""

        token = cancellation or CancellationToken()
        token.throw_if_cancelled()
        if type(defer_persistence_for_conversation) is not bool:
            raise TypeError("defer_persistence_for_conversation 必须是布尔值")
        resolved_run_id = run_id or str(uuid4())
        started_at = time.time()
        admission: AutonomousRunRecord | None = None
        if self.run_store is not None:
            admission = await self.run_store.open_admission(
                request,
                self.budget,
                run_id=resolved_run_id,
                started_at=started_at,
            )
            started_at = admission.started_at
            if admission.initial_plan_bound:
                store = self.workflow.store
                if store is None:
                    raise PlanWorkflowUnavailableError(
                        "已绑定 Autonomous Run 缺少 Durable Plan Store"
                    )
                existing = await store.load(admission.initial_plan_id)
                return AutonomousPreparedRun(
                    request,
                    existing.plan,
                    resolved_run_id,
                    requires_conversation_bootstrap=(
                        not admission.dispatchable
                    ),
                    started_at=started_at,
                    initial_usage=admission.resource_usage,
                )
        planner_deadline_at = (
            admission.deadline_at
            if admission is not None
            else (
                None
                if self.budget.max_duration_seconds is None
                else started_at + float(self.budget.max_duration_seconds)
            )
        )
        if planner_deadline_at is not None and time.time() >= planner_deadline_at:
            raise PlanBudgetExceeded(
                "Autonomous Run wall-clock deadline 在 Planner 派发前已到"
            )
        planner_ticket: PlanUsageReservation | None = None
        planner_usage = PlanResourceUsage()
        if task_decision is None and self.usage_meter is not None:
            planner_ticket = await self._reserve_model_stage(
                resolved_run_id,
                "planner",
                durable=self.run_store is not None,
            )
        try:
            plan_kwargs: dict[str, Any] = {}
            if task_decision is not None:
                plan_kwargs["task_decision"] = task_decision
            if defer_persistence_for_conversation:
                plan_kwargs["persist"] = False
            elif self.run_store is not None:
                # Persist only after the provisional Run has charged Planner
                # usage and reserved the exact Plan step count.
                plan_kwargs["persist"] = False
            if _accepts_keyword(self.workflow.plan, "max_steps"):
                plan_kwargs["max_steps"] = self.budget.max_plan_steps
            async def invoke_planner() -> MultiIntentPlan:
                return await self.workflow.plan(request, **plan_kwargs)

            async def invoke_admitted_planner() -> MultiIntentPlan:
                if planner_ticket is None:
                    return await invoke_planner()
                value = await self._dispatch_reserved_model_callback(
                    resolved_run_id,
                    planner_ticket,
                    invoke_planner,
                )
                if not isinstance(value, MultiIntentPlan):
                    raise TypeError("Planner 必须返回 MultiIntentPlan")
                return value

            if planner_deadline_at is None:
                initial_plan = await invoke_admitted_planner()
            else:
                remaining = planner_deadline_at - time.time()
                if remaining <= 0:
                    raise PlanBudgetExceeded(
                        "Autonomous Run wall-clock deadline 在 Planner 派发前已到"
                    )
                async with asyncio.timeout(remaining):
                    initial_plan = await invoke_admitted_planner()
        except BaseException as error:
            if planner_ticket is not None:
                await self._settle_model_stage(
                    resolved_run_id,
                    planner_ticket,
                    durable=self.run_store is not None,
                )
            if (
                isinstance(error, TimeoutError)
                and planner_deadline_at is not None
            ):
                raise PlanBudgetExceeded(
                    "Autonomous Run wall-clock duration 在 Planner 阶段耗尽"
                ) from error
            raise
        if len(initial_plan.steps) > self.budget.max_plan_steps:
            if planner_ticket is not None:
                await self._settle_model_stage(
                    resolved_run_id, planner_ticket, durable=False
                    if self.run_store is None
                    else True
                )
            raise PlanBudgetExceeded(
                f"Plan 包含 {len(initial_plan.steps)} 个 Step，"
                f"超过预算 {self.budget.max_plan_steps}"
            )
        if planner_ticket is not None:
            planner_usage = await self._settle_model_stage(
                resolved_run_id,
                planner_ticket,
                durable=self.run_store is not None,
            )
        initial_usage = planner_usage + PlanResourceUsage(
            plan_steps=len(initial_plan.steps)
        )
        _assert_runner_usage(initial_usage, self.budget)
        if self.run_store is not None:
            admitted = await self.run_store.reserve_resources(
                resolved_run_id,
                reservation_id=f"plan-steps:{initial_plan.plan_id}",
                stage="initial_plan",
                reserved=PlanResourceUsage(plan_steps=len(initial_plan.steps)),
            )
            initial_usage = admitted.resource_usage
        return AutonomousPreparedRun(
            request,
            initial_plan,
            resolved_run_id,
            requires_conversation_bootstrap=self.run_store is not None,
            started_at=started_at,
            initial_usage=initial_usage,
        )

    async def run(
        self,
        request: str,
        *,
        task_decision: TaskDecision | None = None,
        cancellation: CancellationToken | None = None,
    ) -> AutonomousPlanResult:
        if self.run_store is not None:
            raise AutonomousRunStoreError(
                "Durable Autonomous Run 禁止使用 run() 跳过三流事务；"
                "请使用 prepare(defer_persistence_for_conversation=True) → "
                "AutonomousConversationProjector.bootstrap → run_prepared"
            )
        prepared = await self.prepare(
            request,
            task_decision=task_decision,
            cancellation=cancellation,
        )
        return await self.run_prepared(prepared, cancellation=cancellation)

    async def run_prepared(
        self,
        prepared: AutonomousPreparedRun,
        *,
        cancellation: CancellationToken | None = None,
    ) -> AutonomousPlanResult:
        """Execute a prepared run; callers may persist conversation linkage first."""

        if not isinstance(prepared, AutonomousPreparedRun):
            raise TypeError("prepared 必须是 AutonomousPreparedRun")
        token = cancellation or CancellationToken()
        token.throw_if_cancelled()
        durable = (
            await self.run_store.load(prepared.run_id)
            if self.run_store is not None
            else None
        )
        if durable is not None:
            self._assert_deadline(durable)
            _assert_runner_usage(durable.resource_usage, durable.budget)
        if durable is not None:
            if (
                not durable.initial_plan_bound
                or not durable.linked
                or not durable.dispatchable
                or durable.initial_plan_id != prepared.plan.plan_id
            ):
                raise AutonomousRunStoreError(
                    "Autonomous Plan 尚未与 Durable Conversation 原子绑定"
                )
            store = self.workflow.store
            if store is None:
                raise PlanWorkflowUnavailableError(
                    "自主 Plan 执行需要 store_backend='journal'"
                )
            plan_record = await store.load(prepared.plan.plan_id)
            if not plan_record.dispatchable:
                raise AutonomousRunStoreError(
                    "Autonomous Plan 尚未进入 dispatchable 状态"
                )
        return await self._execute_closed_loop(
            prepared.request,
            prepared.plan,
            token,
            run_id=prepared.run_id,
            durable=durable,
            started_at=prepared.started_at,
        )

    async def resume(
        self,
        plan_id: str,
        *,
        cancellation: CancellationToken | None = None,
    ) -> AutonomousPlanResult:
        """Resume a persisted plan and run the same validation/correction loop."""

        token = cancellation or CancellationToken()
        token.throw_if_cancelled()
        store = self.workflow.store
        if store is None:
            raise PlanWorkflowUnavailableError(
                "自主 Plan 恢复需要 store_backend='journal'"
            )
        record = await store.load(plan_id)
        durable: AutonomousRunRecord | None = None
        if self.run_store is not None:
            durable = await self.run_store.find_by_plan_id(plan_id)
            if durable is None:
                durable = await self.run_store.initialize(
                    record.plan.request,
                    record.plan.plan_id,
                    self.budget,
                    initial_usage=PlanResourceUsage(
                        plan_steps=len(record.plan.steps)
                    ),
                )
            # A correction may have been registered immediately before a crash.
            # Resume the newest durable plan, not the stale caller bookmark.
            record = await store.load(durable.latest_plan_id)
            if durable.status is not None:
                return _restored_autonomous_result(durable, record.plan)
            if (
                not durable.initial_plan_bound
                or not durable.linked
                or not durable.dispatchable
                or not record.dispatchable
            ):
                raise AutonomousRunStoreError(
                    "Autonomous Plan 未与 Run/Conversation 原子发布，"
                    "禁止恢复派发"
                )
            self._assert_deadline(durable)
            _assert_runner_usage(durable.resource_usage, durable.budget)
        return await self._execute_closed_loop(
            record.plan.request,
            record.plan,
            token,
            run_id=durable.run_id if durable is not None else str(uuid4()),
            durable=durable,
            started_at=(durable.started_at if durable is not None else time.time()),
        )

    async def acknowledge_completion(
        self,
        plan_id: str,
        *,
        lease_seconds: float = 30.0,
    ) -> bool:
        """Acknowledge a terminal plan only after its consumer projection commits."""

        store = self.workflow.store
        if store is None:
            return False
        record = await store.load(plan_id)
        if not record.completion_pending:
            return True
        owner = store.new_owner_token()
        lease = await store.acquire_completion(
            plan_id,
            owner,
            lease_seconds=lease_seconds,
        )
        if lease is None:
            return False
        try:
            claimed = await store.load(plan_id)
            envelope = claimed.completion_envelope
            if not claimed.completion_pending or envelope is None:
                return not claimed.completion_pending
            await store.ack_completion(
                plan_id,
                lease,
                lease_seconds=lease_seconds,
                envelope=envelope,
            )
            return True
        finally:
            await store.release_completion_lease(lease)

    async def acknowledge_run_completions(
        self,
        run_id: str,
        *,
        exclude_plan_id: str | None = None,
        lease_seconds: float = 30.0,
    ) -> bool:
        """Ack every consumed Plan envelope after the run projection commits.

        A correction run can leave completion envelopes on its earlier Plans.
        They are controller observations, not independent user-visible answers.
        Clearing them only after the final/waiting Conversation projection keeps
        recovery live without allowing an intermediate Plan to publish twice.
        """

        if self.run_store is None:
            return False
        durable = await self.run_store.load(run_id)
        acknowledged = True
        for plan_id in durable.plan_ids:
            if plan_id == exclude_plan_id:
                continue
            if not await self.acknowledge_completion(
                plan_id,
                lease_seconds=lease_seconds,
            ):
                acknowledged = False
        return acknowledged

    async def _execute_closed_loop(
        self,
        request: str,
        initial_plan: MultiIntentPlan,
        token: CancellationToken,
        *,
        run_id: str,
        durable: AutonomousRunRecord | None,
        started_at: float,
    ) -> AutonomousPlanResult:
        if self.run_store is None:
            return await self._execute_closed_loop_owned(
                request,
                initial_plan,
                token,
                run_id=run_id,
                durable=durable,
                started_at=started_at,
                controller_lease=None,
                controller_lease_seconds=None,
            )
        run_store = self.run_store
        lease_seconds = float(self.workflow.lease_seconds)
        lease = await run_store.acquire_controller(
            run_id,
            str(uuid4()),
            lease_seconds=lease_seconds,
        )
        if lease is None:
            raise AutonomousRunControllerConflictError(
                f"Autonomous Run 正由其他 Controller 执行：{run_id}"
            )
        controller_token = token.create_child()
        lease_lost = asyncio.Event()

        async def heartbeat() -> None:
            interval = max(0.001, min(lease_seconds / 3, 5.0))
            try:
                while True:
                    await asyncio.sleep(interval)
                    if not await run_store.renew_controller(
                        run_id,
                        lease,
                        lease_seconds=lease_seconds,
                    ):
                        lease_lost.set()
                        controller_token.cancel(
                            "Autonomous Run Controller Lease 已丢失"
                        )
                        return
            except asyncio.CancelledError:
                raise
            except BaseException:
                lease_lost.set()
                controller_token.cancel(
                    "Autonomous Run Controller Lease 续租失败"
                )

        renewal = asyncio.create_task(
            heartbeat(),
            name=f"autonomous-run-controller:{run_id}",
        )
        try:
            # The caller's Run/Plan snapshot was loaded before controller
            # acquisition.  A previous owner may have registered a correction
            # or finalized in that gap, so the new owner must reload under its
            # lease before any Plan/model/tool callback.
            if not await self.run_store.renew_controller(
                run_id,
                lease,
                lease_seconds=lease_seconds,
            ):
                lease_lost.set()
                raise AutonomousRunControllerLeaseLostError(
                    f"Autonomous Run Controller Lease 在重载前已丢失：{run_id}"
                )
            refreshed = await self.run_store.load(run_id)
            plan_store = self.workflow.store
            if plan_store is None:
                raise PlanWorkflowUnavailableError(
                    "自主 Plan 恢复需要 store_backend='journal'"
                )
            refreshed_plan = (
                await plan_store.load(refreshed.latest_plan_id)
            ).plan
            if not await self.run_store.renew_controller(
                run_id,
                lease,
                lease_seconds=lease_seconds,
            ):
                lease_lost.set()
                raise AutonomousRunControllerLeaseLostError(
                    f"Autonomous Run Controller Lease 在重载后已丢失：{run_id}"
                )
            durable = refreshed
            request = refreshed.request
            initial_plan = refreshed_plan
            if refreshed.status is not None:
                return _restored_autonomous_result(refreshed, refreshed_plan)
            self._assert_deadline(refreshed)
            _assert_runner_usage(refreshed.resource_usage, refreshed.budget)
            try:
                result = await self._execute_closed_loop_owned(
                    request,
                    initial_plan,
                    controller_token,
                    run_id=run_id,
                    durable=durable,
                    started_at=started_at,
                    controller_lease=lease,
                    controller_lease_seconds=lease_seconds,
                )
            except OperationCancelledError as error:
                if lease_lost.is_set():
                    raise AutonomousRunControllerLeaseLostError(
                        f"Autonomous Run Controller Lease 已丢失：{run_id}"
                    ) from error
                raise
            if lease_lost.is_set():
                raise AutonomousRunControllerLeaseLostError(
                    f"Autonomous Run Controller Lease 已丢失：{run_id}"
                )
            return result
        finally:
            if not renewal.done():
                renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)
            await self.run_store.release_controller(run_id, lease)
            controller_token.detach()

    async def _execute_closed_loop_owned(
        self,
        request: str,
        initial_plan: MultiIntentPlan,
        token: CancellationToken,
        *,
        run_id: str,
        durable: AutonomousRunRecord | None,
        started_at: float,
        controller_lease: ClaimLease | None,
        controller_lease_seconds: float | None,
    ) -> AutonomousPlanResult:
        plans_by_id: dict[str, MultiIntentPlan] = {initial_plan.plan_id: initial_plan}
        segment_id = str(uuid4())
        executing_plan_id = initial_plan.plan_id
        local_usage = (
            PlanResourceUsage(plan_steps=len(initial_plan.steps))
            if durable is None
            else durable.resource_usage
        )

        async def reserve_step(
            step,
            step_attempt: int,
            tool_attempt: int | None,
        ) -> None:
            nonlocal local_usage
            reserved = PlanResourceUsage(
                step_attempts=1 if tool_attempt is None else 0,
                tool_calls=0 if tool_attempt is None else 1,
            )
            reservation_id = (
                f"plan:{executing_plan_id}:step:{step.step_id}:"
                f"attempt:{step_attempt}:"
                + (
                    "step"
                    if tool_attempt is None
                    else f"tool-attempt:{tool_attempt}"
                )
            )
            if self.run_store is not None:
                current = await self.run_store.reserve_resources(
                    run_id,
                    reservation_id=reservation_id,
                    stage=(
                        f"plan_step:{step.step_id}"
                        if tool_attempt is None
                        else f"plan_tool_attempt:{step.step_id}:{tool_attempt}"
                    ),
                    reserved=reserved,
                    controller_lease=controller_lease,
                    controller_lease_seconds=controller_lease_seconds,
                )
                local_usage = current.resource_usage
            else:
                _assert_runner_usage(local_usage + reserved, self.budget)
                local_usage = local_usage + reserved

        async def execute_action(
            action: ClosedLoopAction,
            action_token: CancellationToken,
        ) -> PlanExecutionResult:
            nonlocal executing_plan_id
            plan = _plan_from_action(action)
            existing = plans_by_id.get(plan.plan_id)
            if existing is not None and existing.to_dict() != plan.to_dict():
                raise ValueError(f"Plan ID 已绑定到不同纠正计划：{plan.plan_id}")
            plans_by_id[plan.plan_id] = plan
            executing_plan_id = plan.plan_id
            store = self.workflow.store
            if store is None:
                raise PlanWorkflowUnavailableError(
                    "自主 Plan 执行需要 store_backend='journal'"
                )
            if (
                self.run_store is not None or self._explicit_step_tool_budget
            ) and (
                not isinstance(self.workflow, DurablePlanWorkflow)
                or getattr(self.workflow.execute, "__func__", None)
                is not DurablePlanWorkflow.execute
            ):
                public_reason = (
                    "Durable Autonomous Run 只能使用框架原生 "
                    "DurablePlanWorkflow.execute 边界；自定义或覆写 "
                    "execute 可绕过 Step/Tool Durable Admission，已按失败关闭"
                )
                raise PlanWorkflowUnavailableError(
                    public_reason,
                    public_message=public_reason,
                )
            if not bool(
                getattr(self.workflow, "supports_resource_admission", False)
            ):
                raise PlanWorkflowUnavailableError(
                    "Autonomous workflow 必须显式声明并实现 "
                    "supports_resource_admission；否则无法证明每次派发"
                    "都经过 Step/Tool Admission"
                )
            if not _accepts_keyword(self.workflow.execute, "resource_reserver"):
                raise PlanWorkflowUnavailableError(
                    "Autonomous workflow.execute 必须接受 resource_reserver，"
                    "否则 Step/Tool 预算可被绕过"
                )
            await store.initialize(plan)
            execute_kwargs: dict[str, Any] = {"cancellation": action_token}
            execute_kwargs["resource_reserver"] = reserve_step
            return await self.workflow.execute(plan.plan_id, **execute_kwargs)

        async def validate(
            observation: ClosedLoopObservation,
            validation_token: CancellationToken,
        ) -> ResultValidation:
            plan_observation = _plan_observation(
                request,
                observation,
                plans_by_id,
            )
            phase = _latest_phase(plan_observation)
            if phase == "waiting_approval":
                return ResultValidation.suspended("plan is waiting for approval")
            if phase == "manual_intervention":
                return ResultValidation.unknown(
                    "plan contains an uncertain or non-replayable step"
                )
            if self.result_validator is None:
                return _default_validation(plan_observation)

            async def invoke_validator() -> ResultValidation:
                assert self.result_validator is not None
                value = self.result_validator(plan_observation, validation_token)
                return (
                    await cast(Awaitable[ResultValidation], value)
                    if inspect.isawaitable(value)
                    else value
                )

            result = (
                await self._invoke_model_stage(
                    run_id,
                    "result_validator",
                    invoke_validator,
                    controller_lease=controller_lease,
                    controller_lease_seconds=controller_lease_seconds,
                )
                if self.usage_meter is not None
                else await invoke_validator()
            )
            if not isinstance(result, ResultValidation):
                raise TypeError("Plan Result Validator 必须返回 ResultValidation")
            if phase != "completed" and result.status == "valid":
                raise ValueError("未完成的 Plan 不能被 Validator 标记为 valid")
            return result

        async def correct(
            observation: ClosedLoopObservation,
            validation: ResultValidation,
            correction_token: CancellationToken,
        ) -> CorrectionPlan | None:
            nonlocal local_usage
            plan_observation = _plan_observation(
                request,
                observation,
                plans_by_id,
            )
            corrected = await self._replan(
                plan_observation,
                validation,
                correction_token,
                run_id=run_id,
                controller_lease=controller_lease,
                controller_lease_seconds=controller_lease_seconds,
            )
            if corrected is None:
                return None
            if corrected.request != request:
                raise ValueError("Correction Plan 不得替换原始用户请求")
            if corrected.plan_id in plans_by_id:
                raise ValueError("Correction Plan 必须使用新的 planId")
            PlanValidator(self.workflow.policies).validate(corrected)
            cumulative_steps = local_usage.plan_steps + len(corrected.steps)
            if cumulative_steps > self.budget.max_plan_steps:
                raise PlanBudgetExceeded(
                    "Correction Plan 会使累计 Step 超过预算："
                    f"{cumulative_steps} > {self.budget.max_plan_steps}"
                )
            step_reservation = PlanResourceUsage(
                plan_steps=len(corrected.steps)
            )
            if self.run_store is not None:
                current = await self.run_store.reserve_resources(
                    run_id,
                    reservation_id=f"plan-steps:{corrected.plan_id}",
                    stage="correction_plan",
                    reserved=step_reservation,
                    controller_lease=controller_lease,
                    controller_lease_seconds=controller_lease_seconds,
                )
                local_usage = current.resource_usage
            else:
                _assert_runner_usage(local_usage + step_reservation, self.budget)
                local_usage = local_usage + step_reservation
            plans_by_id[corrected.plan_id] = corrected
            store = self.workflow.store
            if store is None:
                raise PlanWorkflowUnavailableError(
                    "自主 Plan 执行需要 store_backend='journal'"
                )
            if (
                self.run_store is not None
                and isinstance(store, SessionJournalPlanStore)
            ):
                await self.run_store.register_plan_atomic(
                    run_id,
                    corrected,
                    store,
                    controller_lease=controller_lease,
                    controller_lease_seconds=controller_lease_seconds,
                )
            else:
                # Non-Journal adapters must make the plan durable before its ID
                # can be published.  It may remain as a harmless orphan after a
                # crash, but resume can never point at a missing Plan.
                await store.initialize(corrected)
                if self.run_store is not None:
                    await self.run_store.register_plan(
                        run_id,
                        corrected.plan_id,
                        controller_lease=controller_lease,
                        controller_lease_seconds=controller_lease_seconds,
                    )
            return CorrectionPlan(
                (_action_from_plan(corrected),),
                "; ".join(validation.issues),
            )

        async def persist_event(event: ClosedLoopEvent) -> None:
            if self.run_store is not None:
                await self.run_store.append_closed_loop_event(
                    run_id,
                    segment_id,
                    event,
                    controller_lease=controller_lease,
                    controller_lease_seconds=controller_lease_seconds,
                )
            if self.event_sink is not None:
                value = self.event_sink(copy.deepcopy(event))
                if inspect.isawaitable(value):
                    await cast(Awaitable[Any], value)

        effective_budget = (
            durable.remaining_budget if durable is not None else self.budget
        )
        loop_executor = ClosedLoopExecutor(
            execute_action,
            cast(ResultValidator, validate),
            cast(CorrectionPlanner, correct),
            budget=effective_budget,
            event_sink=persist_event,
        )
        remaining_duration = self._remaining_duration(durable, started_at)
        try:
            if remaining_duration is None:
                loop = await loop_executor.execute(
                    _action_from_plan(initial_plan),
                    cancellation=token,
                )
            else:
                async with asyncio.timeout(remaining_duration):
                    loop = await loop_executor.execute(
                        _action_from_plan(initial_plan),
                        cancellation=token,
                    )
        except TimeoutError as error:
            raise PlanBudgetExceeded(
                "Autonomous Run wall-clock duration 预算已耗尽"
            ) from error
        observation = _plan_observation(request, None, plans_by_id, loop=loop)
        status = _autonomous_status(loop, observation)
        if self.run_store is not None and loop.event_sink_errors:
            # The execution fact may already exist, therefore a missing durable
            # controller event is never reported as ordinary failure/success.
            status = "manual_intervention"
        synthesis_budget_error: PlanBudgetExceeded | None = None
        try:
            synthesis_remaining = self._remaining_duration(durable, started_at)
            if synthesis_remaining is None:
                response_text = await self._synthesize(
                    observation,
                    status,
                    token,
                    run_id=run_id,
                    controller_lease=controller_lease,
                    controller_lease_seconds=controller_lease_seconds,
                )
            else:
                async with asyncio.timeout(synthesis_remaining):
                    response_text = await self._synthesize(
                        observation,
                        status,
                        token,
                        run_id=run_id,
                        controller_lease=controller_lease,
                        controller_lease_seconds=controller_lease_seconds,
                    )
        except TimeoutError:
            synthesis_budget_error = PlanBudgetExceeded(
                "Autonomous Run wall-clock duration 在结果合成阶段耗尽"
            )
        except PlanBudgetExceeded as error:
            synthesis_budget_error = error
        if synthesis_budget_error is not None:
            # Execution facts already exist and may include real side effects.
            # A depleted model/deadline budget must never strand the Run in an
            # eternally resumable state.  Do not call another model: render the
            # trusted structured result deterministically and finalize it.
            response_text = _default_synthesis(observation, status)
            response_text += (
                "\n（模型结果合成预算已耗尽，已使用确定性汇总："
                f"{synthesis_budget_error}）"
            )
        pending = _pending_approval_ids(observation)
        result = AutonomousPlanResult(
            request=request,
            status=status,
            response_text=response_text,
            plans=observation.plans,
            executions=observation.executions,
            closed_loop=loop,
            pending_approval_ids=pending,
            closed_loop_run_id=run_id,
        )
        if self.run_store is not None:
            await self.run_store.finish_segment(
                run_id,
                segment_id,
                status,
                controller_lease=controller_lease,
                controller_lease_seconds=controller_lease_seconds,
            )
            if status != "waiting_approval":
                await self.run_store.finalize(
                    run_id,
                    status=status,
                    response_text=response_text,
                    pending_approval_ids=pending,
                    controller_lease=controller_lease,
                    controller_lease_seconds=controller_lease_seconds,
                )
        return result

    async def _replan(
        self,
        observation: AutonomousPlanObservation,
        validation: ResultValidation,
        cancellation: CancellationToken,
        *,
        run_id: str,
        controller_lease: ClaimLease | None,
        controller_lease_seconds: float | None,
    ) -> MultiIntentPlan | None:
        async def invoke_replanner() -> MultiIntentPlan | None:
            if self.replanner is not None:
                value = self.replanner(observation, validation, cancellation)
                return (
                    await cast(Awaitable[MultiIntentPlan | None], value)
                    if inspect.isawaitable(value)
                    else value
                )
            planner = self.workflow.planner
            if planner is None or planner.replanner_fn is None:
                return None
            latest = observation.latest_execution
            summary = (
                {"phase": "execution_error", "error": observation.latest_error}
                if latest is None
                else latest.state.to_dict()
            )
            return await planner.replan(
                observation.request,
                observation.latest_plan,
                summary,
                validation.issues,
            )

        has_replanner = self.replanner is not None or (
            self.workflow.planner is not None
            and self.workflow.planner.replanner_fn is not None
        )
        corrected = (
            await self._invoke_model_stage(
                run_id,
                "replanner",
                invoke_replanner,
                controller_lease=controller_lease,
                controller_lease_seconds=controller_lease_seconds,
            )
            if has_replanner and self.usage_meter is not None
            else await invoke_replanner()
        )
        if corrected is not None and not isinstance(corrected, MultiIntentPlan):
            raise TypeError("Plan Replanner 必须返回 MultiIntentPlan 或 None")
        return corrected

    async def _synthesize(
        self,
        observation: AutonomousPlanObservation,
        status: AutonomousPlanStatus,
        cancellation: CancellationToken,
        *,
        run_id: str,
        controller_lease: ClaimLease | None,
        controller_lease_seconds: float | None,
    ) -> str:
        if self.result_synthesizer is None:
            return _default_synthesis(observation, status)
        async def invoke_synthesizer() -> str:
            assert self.result_synthesizer is not None
            value = self.result_synthesizer(observation, status, cancellation)
            return (
                await cast(Awaitable[str], value)
                if inspect.isawaitable(value)
                else value
            )

        text = (
            await self._invoke_model_stage(
                run_id,
                "synthesizer",
                invoke_synthesizer,
                controller_lease=controller_lease,
                controller_lease_seconds=controller_lease_seconds,
            )
            if self.usage_meter is not None
            else await invoke_synthesizer()
        )
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Plan Result Synthesizer 必须返回非空文本")
        return text.strip()

    async def _invoke_model_stage(
        self,
        run_id: str,
        stage: str,
        callback: Callable[[], Awaitable[Any]],
        *,
        controller_lease: ClaimLease | None,
        controller_lease_seconds: float | None,
    ) -> Any:
        ticket = await self._reserve_model_stage(
            run_id,
            stage,
            durable=True,
            controller_lease=controller_lease,
            controller_lease_seconds=controller_lease_seconds,
        )
        try:
            value = await self._dispatch_reserved_model_callback(
                run_id,
                ticket,
                callback,
            )
        except BaseException:
            # A dispatched provider call remains billable even when its result
            # fails. Unknown settlement keeps the durable reservation charged.
            await self._settle_model_stage(
                run_id,
                ticket,
                durable=True,
                controller_lease=controller_lease,
                controller_lease_seconds=controller_lease_seconds,
            )
            raise
        await self._settle_model_stage(
            run_id,
            ticket,
            durable=True,
            controller_lease=controller_lease,
            controller_lease_seconds=controller_lease_seconds,
        )
        return value

    async def _reserve_model_stage(
        self,
        run_id: str,
        stage: str,
        *,
        durable: bool,
        controller_lease: ClaimLease | None = None,
        controller_lease_seconds: float | None = None,
    ) -> PlanUsageReservation:
        if self.usage_meter is None:
            raise PlanBudgetExceeded("模型阶段缺少 Usage Admission Meter")
        usage = PlanResourceUsage()
        budget = self.budget
        if durable and self.run_store is not None:
            current = await self.run_store.load(run_id)
            self._assert_deadline(current)
            usage = current.resource_usage
            budget = current.budget
        remaining = _remaining_usage_capacity(usage, budget)
        if remaining.model_calls < 1:
            raise PlanBudgetExceeded("Autonomous Run model_calls 预算已耗尽")
        reservation_id = f"model:{stage}:{uuid4()}"
        value = self.usage_meter.reserve(
            run_id=run_id,
            reservation_id=reservation_id,
            stage=stage,
            remaining=remaining,
        )
        ticket = (
            await cast(Awaitable[PlanUsageReservation], value)
            if inspect.isawaitable(value)
            else value
        )
        if not isinstance(ticket, PlanUsageReservation):
            raise TypeError("Usage Meter reserve 必须返回 PlanUsageReservation")
        if ticket.reservation_id != reservation_id or ticket.stage != stage:
            raise ValueError("Usage Meter 返回了不匹配的 Reservation")
        _assert_model_reservation(ticket.reserved, remaining, budget)
        if durable and self.run_store is not None:
            try:
                await self.run_store.reserve_resources(
                    run_id,
                    reservation_id=ticket.reservation_id,
                    stage=f"model:{stage}",
                    reserved=ticket.reserved,
                    controller_lease=controller_lease,
                    controller_lease_seconds=controller_lease_seconds,
                )
            except BaseException:
                cancelled = self.usage_meter.cancel(ticket)
                if inspect.isawaitable(cancelled):
                    await cast(Awaitable[Any], cancelled)
                raise
        return ticket

    async def _settle_model_stage(
        self,
        run_id: str,
        ticket: PlanUsageReservation,
        *,
        durable: bool,
        controller_lease: ClaimLease | None = None,
        controller_lease_seconds: float | None = None,
        reported_usage: PlanResourceUsage | None = None,
    ) -> PlanResourceUsage:
        if self.usage_meter is None:
            raise PlanBudgetExceeded("模型阶段缺少 Usage Settlement Meter")
        value = self.usage_meter.settle(ticket)
        actual = (
            await cast(Awaitable[PlanResourceUsage], value)
            if inspect.isawaitable(value)
            else value
        )
        if not isinstance(actual, PlanResourceUsage):
            raise TypeError("Usage Meter settle 必须返回 PlanResourceUsage")
        _assert_model_settlement(actual, ticket.reserved)
        attempt_snapshot = self._model_attempt_snapshots.pop(
            ticket.reservation_id,
            None,
        )
        if attempt_snapshot is not None:
            _assert_physical_attempt_usage_covered(attempt_snapshot, actual)
        if reported_usage is not None:
            _assert_reported_model_usage_covered(reported_usage, actual)
        if durable and self.run_store is not None:
            await self.run_store.settle_resources(
                run_id,
                reservation_id=ticket.reservation_id,
                actual=actual,
                controller_lease=controller_lease,
                controller_lease_seconds=controller_lease_seconds,
            )
        return actual

    async def _dispatch_reserved_model_callback(
        self,
        run_id: str,
        ticket: PlanUsageReservation,
        callback: Callable[[], Awaitable[Any]],
    ) -> Any:
        if self.usage_meter is None:
            raise PlanBudgetExceeded("模型阶段缺少 Usage Admission Meter")
        dispatcher = getattr(self.usage_meter, "dispatch", None)
        if not callable(dispatcher):
            if self.hard_model_budget_enabled:
                raise PlanBudgetExceeded(
                    "硬 model/token/cost 预算缺少 Provider Admission Dispatcher"
                )
            return await callback()
        scope = ModelAttemptAdmissionScope(
            run_id=run_id,
            stage=ticket.stage,
            reservation_id=ticket.reservation_id,
            max_model_calls=ticket.reserved.model_calls,
            max_tokens=(
                ticket.reserved.tokens
                if self.budget.max_tokens is not None
                else None
            ),
            max_cost=(
                ticket.reserved.cost
                if self.budget.max_cost is not None
                else None
            ),
        )
        try:
            async with activate_model_attempt_admission(scope):
                value = dispatcher(ticket, callback)
                return (
                    await cast(Awaitable[Any], value)
                    if inspect.isawaitable(value)
                    else value
                )
        finally:
            self._model_attempt_snapshots[ticket.reservation_id] = (
                await scope.snapshot()
            )

    @staticmethod
    def _assert_deadline(record: AutonomousRunRecord) -> None:
        if record.deadline_at is not None and time.time() >= record.deadline_at:
            raise PlanBudgetExceeded("Autonomous Run wall-clock deadline 已到")

    def _remaining_duration(
        self,
        durable: AutonomousRunRecord | None,
        started_at: float,
    ) -> float | None:
        if durable is not None:
            if durable.deadline_at is None:
                return None
            remaining = durable.deadline_at - time.time()
        elif self.budget.max_duration_seconds is None:
            return None
        else:
            remaining = (
                started_at
                + float(self.budget.max_duration_seconds)
                - time.time()
            )
        if remaining <= 0:
            raise PlanBudgetExceeded("Autonomous Run wall-clock deadline 已到")
        return remaining


def _action_from_plan(plan: MultiIntentPlan) -> ClosedLoopAction:
    unsafe = any(step.write or step.replay_policy == "never" for step in plan.steps)
    return ClosedLoopAction(
        action_id=f"plan:{plan.plan_id}",
        payload={"plan": plan.to_dict()},
        replay_policy="never" if unsafe else "safe",
        write=any(step.write for step in plan.steps),
    )


def _plan_from_action(action: ClosedLoopAction) -> MultiIntentPlan:
    raw = action.payload.get("plan")
    if not isinstance(raw, dict):
        raise ValueError("Closed-loop Plan Action 缺少 plan payload")
    plan = MultiIntentPlan.from_dict(raw)
    if action.action_id != f"plan:{plan.plan_id}":
        raise ValueError("Closed-loop Action 与 Plan ID 不匹配")
    return plan


def _plan_observation(
    request: str,
    observation: ClosedLoopObservation | None,
    plans_by_id: dict[str, MultiIntentPlan],
    *,
    loop: ClosedLoopResult | None = None,
) -> AutonomousPlanObservation:
    attempts = loop.attempts if loop is not None else observation.attempts  # type: ignore[union-attr]
    plans: list[MultiIntentPlan] = []
    executions: list[PlanExecutionResult] = []
    latest_error: str | None = None
    for attempt in attempts:
        plan_id = attempt.action.action_id.removeprefix("plan:")
        plan = plans_by_id.get(plan_id)
        if plan is not None:
            plans.append(copy.deepcopy(plan))
        if isinstance(attempt.result, PlanExecutionResult):
            executions.append(copy.deepcopy(attempt.result))
        if attempt.error is not None:
            latest_error = attempt.error
    rounds = loop.correction_rounds if loop is not None else observation.correction_rounds  # type: ignore[union-attr]
    return AutonomousPlanObservation(
        request=request,
        plans=tuple(plans),
        executions=tuple(executions),
        latest_error=latest_error,
        correction_rounds=rounds,
    )


def _latest_phase(observation: AutonomousPlanObservation) -> str:
    latest = observation.latest_execution
    return "failed" if latest is None else latest.state.phase


def _default_validation(observation: AutonomousPlanObservation) -> ResultValidation:
    latest = observation.latest_execution
    if latest is None:
        return ResultValidation.invalid(
            observation.latest_error or "plan execution produced no result"
        )
    phase = latest.state.phase
    if phase == "completed":
        return ResultValidation.unknown(
            "plan reached structural completion but no trusted semantic "
            "result validator was configured"
        )
    if phase == "waiting_approval":
        return ResultValidation.suspended("plan is waiting for approval")
    if phase == "manual_intervention":
        return ResultValidation.unknown("plan requires manual intervention")
    issues = tuple(error for _step_id, error in latest.synthesized.failures)
    return ResultValidation.invalid(*(issues or (f"plan ended in phase {phase}",)))


def _autonomous_status(
    loop: ClosedLoopResult,
    observation: AutonomousPlanObservation,
) -> AutonomousPlanStatus:
    if loop.status == "suspended" or _latest_phase(observation) == "waiting_approval":
        return "waiting_approval"
    if loop.status == "completed":
        return "completed"
    if loop.status == "manual_intervention":
        return "manual_intervention"
    return "failed"


def _pending_approval_ids(
    observation: AutonomousPlanObservation,
) -> tuple[str, ...]:
    latest = observation.latest_execution
    if latest is None:
        return ()
    return tuple(
        state.approval_id
        for state in latest.state.steps.values()
        if state.status == "waiting_approval" and state.approval_id is not None
    )


def _restored_autonomous_result(
    durable: AutonomousRunRecord,
    latest_plan: MultiIntentPlan,
) -> AutonomousPlanResult:
    if durable.status is None or durable.response_text is None:
        raise AutonomousRunStoreError(
            "只有已持久终态的 Autonomous Run 可以直接恢复结果"
        )
    return AutonomousPlanResult(
        request=durable.request,
        status=durable.status,
        response_text=durable.response_text,
        plans=(latest_plan,),
        executions=(),
        closed_loop=ClosedLoopResult(
            status=(
                "completed"
                if durable.status == "completed"
                else (
                    "manual_intervention"
                    if durable.status == "manual_intervention"
                    else "failed"
                )
            ),
            attempts=(),
            validations=(),
            events=(),
            correction_rounds=durable.correction_rounds_spent,
            correction_actions=durable.correction_actions_spent,
            reason="restored from finalized autonomous run",
        ),
        pending_approval_ids=durable.pending_approval_ids,
        closed_loop_run_id=durable.run_id,
    )


def _accepts_keyword(callback: Callable[..., Any], keyword: str) -> bool:
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return False
    parameter = signature.parameters.get(keyword)
    if parameter is not None and parameter.kind in {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }:
        return True
    return any(
        item.kind == inspect.Parameter.VAR_KEYWORD
        for item in signature.parameters.values()
    )


def _remaining_usage_capacity(
    usage: PlanResourceUsage,
    budget: ClosedLoopBudget,
) -> PlanResourceUsage:
    unlimited_int = (1 << 63) - 1
    unlimited_cost = 1e300
    return PlanResourceUsage(
        model_calls=(
            unlimited_int
            if budget.max_model_calls is None
            else max(0, budget.max_model_calls - usage.model_calls)
        ),
        tokens=(
            unlimited_int
            if budget.max_tokens is None
            else max(0, budget.max_tokens - usage.tokens)
        ),
        cost=(
            unlimited_cost
            if budget.max_cost is None
            else max(0.0, budget.max_cost - usage.cost)
        ),
    )


def _assert_model_reservation(
    reserved: PlanResourceUsage,
    remaining: PlanResourceUsage,
    budget: ClosedLoopBudget,
) -> None:
    if any(
        value
        for value in (
            reserved.plan_steps,
            reserved.step_attempts,
            reserved.tool_calls,
        )
    ):
        raise ValueError("Model Usage Reservation 不能预留 Plan/Step/Tool 资源")
    if reserved.model_calls < 1:
        raise ValueError("opaque callback 必须至少预留 1 次 model_call")
    if (
        reserved.model_calls > remaining.model_calls
        or reserved.tokens > remaining.tokens
        or reserved.cost > remaining.cost
    ):
        raise PlanBudgetExceeded(
            "Model Usage Reservation 超出剩余 model/token/cost 预算"
        )
    if budget.max_tokens is not None and reserved.tokens < 1:
        raise ValueError("配置 token 预算后必须在调用前预留非零 token 上界")
    if budget.max_cost is not None and reserved.cost <= 0:
        raise ValueError("配置 cost 预算后必须在调用前预留非零费用上界")


def _assert_model_settlement(
    actual: PlanResourceUsage,
    reserved: PlanResourceUsage,
) -> None:
    if actual.model_calls < 1:
        raise ValueError("Opaque callback 的 actual usage 必须包含 model_call")
    for name in (
        "plan_steps",
        "step_attempts",
        "tool_calls",
    ):
        if getattr(actual, name) != 0:
            raise ValueError("Model Usage Settlement 包含非模型资源")
    if (
        actual.tokens > reserved.tokens
        or actual.cost > reserved.cost + 1e-12
        or actual.model_calls > reserved.model_calls
    ):
        raise PlanBudgetExceeded("Model Usage Settlement 超出预留上界")


def _assert_reported_model_usage_covered(
    reported: PlanResourceUsage,
    metered: PlanResourceUsage,
) -> None:
    """Reject a Usage Meter that reports less than the Provider-facing stage."""

    if not isinstance(reported, PlanResourceUsage):
        raise TypeError("reported_usage 必须是 PlanResourceUsage")
    if any(
        getattr(reported, name)
        for name in ("plan_steps", "step_attempts", "tool_calls")
    ) or reported.model_calls != 1:
        raise ValueError("Router reported_usage 必须只包含一次模型调用")
    if (
        metered.model_calls < reported.model_calls
        or metered.tokens < reported.tokens
        or metered.cost + 1e-12 < reported.cost
    ):
        raise PlanBudgetExceeded(
            "Usage Meter 结算低于 Router Provider 报告，保留上界 Reservation"
        )


def _assert_physical_attempt_usage_covered(
    snapshot: ModelAttemptAdmissionSnapshot,
    metered: PlanResourceUsage,
) -> None:
    """Cross-check the trusted meter against raw Provider-boundary facts."""

    if snapshot.unknown_attempts or snapshot.in_flight_attempts:
        raise PlanBudgetExceeded(
            "Model Provider Attempt 用量未知或仍在执行，保留 Retry Tree 上界 Reservation"
        )
    # A trusted Usage Meter may own a Provider adapter that does not use the
    # framework ModelCallRuntime. Its dispatch contract remains responsible for
    # the retry tree. Whenever the framework boundary observed attempts, the
    # meter is not allowed to under-report any physical call, token or cost.
    if snapshot.model_calls == 0:
        return
    if (
        metered.model_calls < snapshot.model_calls
        or metered.tokens < snapshot.tokens
        or metered.cost + 1e-12 < snapshot.cost
    ):
        raise PlanBudgetExceeded(
            "Usage Meter 结算低于 ModelCallRuntime 观察到的物理 Provider "
            "Attempt，用量上界 Reservation 保持占用"
        )


def _assert_runner_usage(
    usage: PlanResourceUsage,
    budget: ClosedLoopBudget,
) -> None:
    checks = (
        ("plan_steps", usage.plan_steps, budget.max_plan_steps),
        ("step_attempts", usage.step_attempts, budget.max_step_attempts),
        ("tool_calls", usage.tool_calls, budget.max_tool_calls),
        ("model_calls", usage.model_calls, budget.max_model_calls),
        ("tokens", usage.tokens, budget.max_tokens),
        ("cost", usage.cost, budget.max_cost),
    )
    for name, actual, limit in checks:
        if limit is not None and actual > limit:
            raise PlanBudgetExceeded(
                f"Autonomous Run {name} 预算不足：{actual} > {limit}"
            )


def _default_synthesis(
    observation: AutonomousPlanObservation,
    status: AutonomousPlanStatus,
) -> str:
    latest = observation.latest_execution
    if status == "waiting_approval":
        return "任务已完成规划并暂停，正在等待所需审批。"
    if status == "manual_intervention":
        return "任务包含结果不确定或不可安全重放的操作，需要人工核对后继续。"
    if status == "failed" or latest is None:
        reason = observation.latest_error or "没有可安全执行的纠正方案"
        return f"任务执行失败：{reason}"
    payload = {
        step_id: result
        for step_id, result in latest.synthesized.ordered_results
    }
    return "任务已完成：" + json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        default=lambda value: f"<{type(value).__name__}>",
    )


__all__ = [
    "AutonomousPlanObservation",
    "AutonomousPreparedRun",
    "AutonomousPlanResult",
    "AutonomousPlanRunner",
    "AutonomousPlanStatus",
    "PlanReplanner",
    "PlanResultSynthesizer",
    "PlanResultValidator",
]
