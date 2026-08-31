"""P0 regression tests for durable autonomous resource admission."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pi_agent_loop import (
    AgentTool,
    AgentToolResult,
    AutonomousConversationProjector,
    AutonomousPlanRunner,
    AutonomousRunStoreError,
    ClosedLoopBudget,
    DurablePlanWorkflow,
    HybridRequestPlanner,
    IntentPlanPolicy,
    JournalPrincipal,
    MultiIntentPlan,
    PlanBudgetExceeded,
    PlanExecutionResult,
    PlanExecutionState,
    PlanResourceUsage,
    PlanNotFoundError,
    PlanStep,
    PlanStepState,
    PlanUsageReservation,
    RetryableToolError,
    ResultValidation,
    SessionJournalAutonomousRunStore,
    SessionJournalOperationEventStore,
    SessionJournalPlanStore,
    SQLiteSessionEventJournal,
    StaticJournalKeyProvider,
    SynthesizedPlanResult,
    ToolDispatchContext,
    ToolDispatchRuntime,
    ToolRetryPolicy,
    ToolRuntimePlanStepExecutor,
    Model,
)


def _journal_store(path: Path) -> SessionJournalAutonomousRunStore:
    return SessionJournalAutonomousRunStore(
        SQLiteSessionEventJournal(
            path,
            key_provider=StaticJournalKeyProvider(
                {"test": b"r" * 32}, active_key_id="test"
            ),
        ),
        JournalPrincipal.system("tenant"),
        session_id="budget-session",
    )


class _AdmissionMeter:
    def __init__(self) -> None:
        self.stages: list[str] = []
        self.cancelled: list[str] = []
        self.dispatched: list[str] = []

    def reserve(self, *, run_id, reservation_id, stage, remaining):
        del run_id
        self.stages.append(stage)
        return PlanUsageReservation(
            reservation_id,
            stage,
            PlanResourceUsage(model_calls=1, tokens=min(10, remaining.tokens), cost=0.5),
        )

    def settle(self, reservation):
        return PlanResourceUsage(model_calls=1, tokens=10, cost=0.5)

    def cancel(self, reservation):
        self.cancelled.append(reservation.reservation_id)

    async def dispatch(self, _reservation, callback):
        self.dispatched.append(_reservation.stage)
        return await callback()


class _NoDispatchMeter:
    reserve = _AdmissionMeter.reserve
    settle = _AdmissionMeter.settle
    cancel = _AdmissionMeter.cancel


class _CrashingWorkflow:
    store = None
    policies = {"read": IntentPlanPolicy("read")}
    planner = None

    def __init__(self) -> None:
        self.calls = 0

    async def plan(self, _request, **_kwargs):
        self.calls += 1
        raise RuntimeError("planner response lost")


class _SlowWorkflow(_CrashingWorkflow):
    async def plan(self, _request, **_kwargs):
        self.calls += 1
        await asyncio.sleep(2)
        raise AssertionError("Planner deadline should cancel this callback")


class _SuccessfulWorkflow:
    planner = None
    supports_resource_admission = True

    def __init__(self) -> None:
        self.plan_value = MultiIntentPlan(
            "read",
            (PlanStep("read", "read"),),
            plan_id="budget-plan",
        )
        self.policies = {"read": IntentPlanPolicy("read")}
        self.store = _MemoryPlanStore(self.plan_value)

    async def plan(self, _request, **_kwargs):
        return self.plan_value

    async def execute(self, plan_id, **kwargs):
        step = self.plan_value.steps[0]
        resource_reserver = kwargs.get("resource_reserver")
        if resource_reserver is not None:
            await resource_reserver(step, 1, None)
        state = PlanExecutionState(
            plan_id,
            {step.step_id: PlanStepState("succeeded", attempts=1, result="ok")},
            version=1,
        )
        return PlanExecutionResult(
            state,
            SynthesizedPlanResult(plan_id, "completed", (("read", "ok"),), (), ()),
            (),
        )


class _MemoryPlanStore:
    def __init__(self, plan: MultiIntentPlan) -> None:
        self.plan = plan

    async def initialize(self, plan, **_kwargs):
        self.plan = plan

    async def load(self, _plan_id):
        return SimpleNamespace(plan=self.plan)


def _trusted_success_validator(observation, _token) -> ResultValidation:
    latest = observation.latest_execution
    assert latest is not None
    if latest.state.phase == "completed":
        return ResultValidation.valid()
    return ResultValidation.invalid(f"fixture ended in {latest.state.phase}")


def _durable_tool_runner(
    path: Path,
    *,
    budget: ClosedLoopBudget,
    execute,
    retry_policy: ToolRetryPolicy | None = None,
):
    journal = SQLiteSessionEventJournal(
        path,
        key_provider=StaticJournalKeyProvider(
            {"test": b"u" * 32}, active_key_id="test"
        ),
    )
    principal = JournalPrincipal.system("tenant")
    plan_store = SessionJournalPlanStore(
        journal,
        principal,
        session_id="budget-session",
    )
    run_store = SessionJournalAutonomousRunStore(
        journal,
        principal,
        session_id="budget-session",
    )
    policy = IntentPlanPolicy("read")
    planner = HybridRequestPlanner(
        {"read": policy},
        lambda *_args: {
            "planId": "budget-tool-plan",
            "steps": [{"stepId": "read", "intent": "read"}],
        },
    )
    tool = AgentTool(
        "read_tool",
        "read",
        "read",
        execute,
        replay_policy="safe",
        retry_policy=retry_policy,
    )
    runtime = ToolDispatchRuntime([tool])
    step_executor = ToolRuntimePlanStepExecutor(
        runtime_provider=lambda: runtime,
        tools=[tool],
        intent_tools={"read": tool.name},
        model=Model(id="budget", provider="test", api="scripted"),
        session_id="budget-session",
        dispatch_context_provider=ToolDispatchContext,
        policies={"read": policy},
    )
    workflow = DurablePlanWorkflow(
        store=plan_store,
        planner=planner,
        policies={"read": policy},
        step_executor=step_executor,
        approval_barrier=None,
    )
    return AutonomousPlanRunner(
        workflow,
        budget=budget,
        run_store=run_store,
    ), run_store


async def _run_bootstrapped(
    runner: AutonomousPlanRunner,
    run_store: SessionJournalAutonomousRunStore,
    request: str = "read",
):
    prepared = await runner.prepare(
        request,
        defer_persistence_for_conversation=True,
    )
    projector = AutonomousConversationProjector(
        SessionJournalOperationEventStore(
            run_store.journal,
            run_store.principal,
        ),
        session_id=run_store.session_id,
        model=Model(id="budget", provider="test", api="scripted"),
    )
    plan_store = runner.workflow.store
    if not isinstance(plan_store, SessionJournalPlanStore):
        raise AssertionError("test runner requires SessionJournalPlanStore")
    await projector.bootstrap(
        request,
        run_id=prepared.run_id,
        plan=prepared.plan,
        budget=runner.budget,
        initial_messages=[],
        run_store=run_store,
        plan_store=plan_store,
    )
    return await runner.run_prepared(prepared)


class AutonomousResourceBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_max_plan_steps_rejected_before_plan_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = SQLiteSessionEventJournal(
                Path(directory) / "state.sqlite3",
                key_provider=StaticJournalKeyProvider(
                    {"test": b"s" * 32}, active_key_id="test"
                ),
            )
            principal = JournalPrincipal.system("tenant")
            store = SessionJournalPlanStore(
                journal, principal, session_id="budget-session"
            )
            policies = {"read": IntentPlanPolicy("read")}
            planner = HybridRequestPlanner(
                policies,
                lambda *_args: {
                    "planId": "oversized-plan",
                    "steps": [
                        {"stepId": "one", "intent": "read"},
                        {"stepId": "two", "intent": "read"},
                    ],
                },
            )
            workflow = DurablePlanWorkflow(
                store=store,
                planner=planner,
                policies=policies,
                step_executor=None,
                approval_barrier=None,
            )

            with self.assertRaises(PlanBudgetExceeded):
                await workflow.plan("read", max_steps=1)
            with self.assertRaises(PlanNotFoundError):
                await store.load("oversized-plan")

    async def test_step_reservation_is_durable_and_cannot_reset_after_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            budget = ClosedLoopBudget(
                max_step_attempts=1,
                max_tool_calls=1,
            )
            store = _journal_store(path)
            await store.open_admission("read", budget, run_id="run-1")
            await store.reserve_resources(
                "run-1",
                reservation_id="step-1",
                stage="plan_step:read",
                reserved=PlanResourceUsage(step_attempts=1, tool_calls=1),
            )

            reopened = _journal_store(path)
            record = await reopened.load("run-1")
            self.assertEqual(record.resource_usage.step_attempts, 1)
            self.assertEqual(record.resource_usage.tool_calls, 1)
            with self.assertRaises(PlanBudgetExceeded):
                await reopened.reserve_resources(
                    "run-1",
                    reservation_id="step-2",
                    stage="plan_step:read",
                    reserved=PlanResourceUsage(step_attempts=1, tool_calls=1),
                )

    async def test_durable_run_without_atomic_conversation_link_is_not_dispatchable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            handler_calls = 0

            async def execute(_call_id, _args, _token, _update):
                nonlocal handler_calls
                handler_calls += 1
                return AgentToolResult(content=[])

            runner, run_store = _durable_tool_runner(
                Path(directory) / "state.sqlite3",
                budget=ClosedLoopBudget(),
                execute=execute,
            )
            prepared = await runner.prepare("read")

            with self.assertRaisesRegex(
                AutonomousRunStoreError,
                "Conversation",
            ):
                await runner.run_prepared(prepared)

            self.assertEqual(handler_calls, 0)
            self.assertEqual(
                await runner.workflow.store.list_runnable_plans(),  # type: ignore[union-attr]
                (),
            )
            with self.assertRaises(PlanNotFoundError):
                await runner.workflow.store.load(prepared.plan.plan_id)  # type: ignore[union-attr]
            durable = await run_store.load(prepared.run_id)
            self.assertFalse(durable.initial_plan_bound)
            self.assertFalse(durable.linked)
            self.assertFalse(durable.dispatchable)

    async def test_durable_run_shortcut_rejected_before_planner_and_journal(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            planner_calls = 0
            handler_calls = 0

            async def execute(_call_id, _args, _token, _update):
                nonlocal handler_calls
                handler_calls += 1
                return AgentToolResult(content=[])

            runner, run_store = _durable_tool_runner(
                Path(directory) / "state.sqlite3",
                budget=ClosedLoopBudget(),
                execute=execute,
            )
            original_plan = runner.workflow.plan

            async def counted_plan(*args, **kwargs):
                nonlocal planner_calls
                planner_calls += 1
                return await original_plan(*args, **kwargs)

            runner.workflow.plan = counted_plan  # type: ignore[method-assign]
            with self.assertRaisesRegex(AutonomousRunStoreError, "bootstrap"):
                await runner.run("read")

            self.assertEqual(planner_calls, 0)
            self.assertEqual(handler_calls, 0)
            self.assertEqual(
                await run_store.journal.load_events(
                    run_store.principal,
                    session_id=run_store.session_id,
                ),
                [],
            )

    async def test_plan_run_binding_without_conversation_stays_quarantined(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            handler_calls = 0

            async def execute(_call_id, _args, _token, _update):
                nonlocal handler_calls
                handler_calls += 1
                return AgentToolResult(content=[])

            runner, run_store = _durable_tool_runner(
                Path(directory) / "state.sqlite3",
                budget=ClosedLoopBudget(),
                execute=execute,
            )
            prepared = await runner.prepare("read")
            plan_store = runner.workflow.store
            assert isinstance(plan_store, SessionJournalPlanStore)
            await run_store.bind_initial_plan_atomic(
                prepared.run_id,
                prepared.plan,
                plan_store,
            )

            stored = await plan_store.load(prepared.plan.plan_id)
            self.assertFalse(stored.dispatchable)
            self.assertEqual(await plan_store.list_runnable_plans(), ())
            with self.assertRaisesRegex(AutonomousRunStoreError, "Conversation"):
                await runner.run_prepared(prepared)
            with self.assertRaisesRegex(AutonomousRunStoreError, "Conversation"):
                await runner.resume(prepared.plan.plan_id)
            self.assertEqual(handler_calls, 0)

    async def test_model_budget_without_admission_meter_fails_at_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "usage_meter"):
            AutonomousPlanRunner(
                _SuccessfulWorkflow(),  # type: ignore[arg-type]
                budget=ClosedLoopBudget(max_tokens=10),
            )

        with self.assertRaisesRegex(TypeError, "usage_meter.dispatch"):
            AutonomousPlanRunner(
                _SuccessfulWorkflow(),  # type: ignore[arg-type]
                budget=ClosedLoopBudget(max_tokens=10),
                usage_meter=_NoDispatchMeter(),
            )

        with self.assertRaisesRegex(ValueError, "Durable Run Store"):
            AutonomousPlanRunner(
                _SuccessfulWorkflow(),  # type: ignore[arg-type]
                budget=ClosedLoopBudget(max_model_calls=1),
                usage_meter=_AdmissionMeter(),
            )

    async def test_durable_custom_execute_cannot_bypass_resource_admission(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bypass_calls = 0

            async def tool_handler(_call_id, _args, _token, _update):
                raise AssertionError("Tool Handler must not run")

            base, run_store = _durable_tool_runner(
                Path(directory) / "state.sqlite3",
                budget=ClosedLoopBudget(max_step_attempts=1, max_tool_calls=1),
                execute=tool_handler,
            )

            class _BypassWorkflow(DurablePlanWorkflow):
                async def execute(
                    self,
                    plan_id,
                    *,
                    cancellation=None,
                    resource_reserver=None,
                ):
                    del plan_id, cancellation, resource_reserver
                    nonlocal bypass_calls
                    bypass_calls += 1
                    raise AssertionError("custom execute must not run")

            workflow = _BypassWorkflow(
                store=base.workflow.store,
                planner=base.workflow.planner,
                policies=base.workflow.policies,
                step_executor=base.workflow.step_executor,
                approval_barrier=None,
            )
            runner = AutonomousPlanRunner(
                workflow,
                budget=base.budget,
                run_store=run_store,
            )

            result = await _run_bootstrapped(runner, run_store)

            self.assertEqual(result.status, "failed")
            self.assertEqual(bypass_calls, 0)
            self.assertIn(
                "DurablePlanWorkflow.execute",
                result.closed_loop.attempts[0].error or "",
            )

    async def test_non_durable_custom_execute_cannot_ignore_explicit_budget(
        self,
    ) -> None:
        bypass_calls = 0

        class _IgnoringWorkflow(_SuccessfulWorkflow):
            async def execute(self, plan_id, **_kwargs):
                del plan_id
                nonlocal bypass_calls
                bypass_calls += 1
                return await _SuccessfulWorkflow().execute("budget-plan")

        runner = AutonomousPlanRunner(
            _IgnoringWorkflow(),  # type: ignore[arg-type]
            budget=ClosedLoopBudget(max_step_attempts=0, max_tool_calls=0),
        )

        result = await runner.run("read")

        self.assertEqual(result.status, "failed")
        self.assertEqual(bypass_calls, 0)
        self.assertIn(
            "DurablePlanWorkflow.execute",
            result.closed_loop.attempts[0].error or "",
        )

    async def test_step_attempt_budget_blocks_before_tool_handler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            handler_calls = 0

            async def execute(_call_id, _args, _token, _update):
                nonlocal handler_calls
                handler_calls += 1
                return AgentToolResult(content=[])

            runner, run_store = _durable_tool_runner(
                Path(directory) / "state.sqlite3",
                budget=ClosedLoopBudget(max_step_attempts=0, max_tool_calls=1),
                execute=execute,
            )
            result = await _run_bootstrapped(runner, run_store)

            self.assertEqual(handler_calls, 0)
            durable = await run_store.load(result.closed_loop_run_id or "")
            self.assertEqual(durable.resource_usage.step_attempts, 0)
            self.assertEqual(durable.resource_usage.tool_calls, 0)

    async def test_each_tool_retry_attempt_needs_durable_budget_before_handler(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            handler_calls = 0

            async def execute(_call_id, _args, _token, _update):
                nonlocal handler_calls
                handler_calls += 1
                raise RetryableToolError("retry", code="temporary")

            runner, run_store = _durable_tool_runner(
                Path(directory) / "state.sqlite3",
                budget=ClosedLoopBudget(max_step_attempts=1, max_tool_calls=1),
                execute=execute,
                retry_policy=ToolRetryPolicy(
                    max_retries=2,
                    retryable_codes=frozenset({"temporary"}),
                    idempotent=True,
                    initial_delay_seconds=0,
                    max_delay_seconds=1,
                    jitter_ratio=0,
                    max_elapsed_seconds=5,
                ),
            )
            result = await _run_bootstrapped(runner, run_store)

            self.assertEqual(handler_calls, 1)
            durable = await run_store.load(result.closed_loop_run_id or "")
            self.assertEqual(durable.resource_usage.step_attempts, 1)
            self.assertEqual(durable.resource_usage.tool_calls, 1)

    async def test_settlement_above_reserved_upper_bound_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = _journal_store(Path(directory) / "state.sqlite3")
            await store.open_admission(
                "read",
                ClosedLoopBudget(max_model_calls=1, max_tokens=10),
                run_id="settle-run",
            )
            await store.reserve_resources(
                "settle-run",
                reservation_id="model-1",
                stage="model:test",
                reserved=PlanResourceUsage(model_calls=1, tokens=10),
            )

            with self.assertRaises(PlanBudgetExceeded):
                await store.settle_resources(
                    "settle-run",
                    reservation_id="model-1",
                    actual=PlanResourceUsage(model_calls=1, tokens=11),
                )
            durable = await store.load("settle-run")
            self.assertEqual(durable.resource_usage.tokens, 10)
            self.assertEqual(len(durable.active_reservations), 1)

    async def test_correction_plan_steps_use_cumulative_durable_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            handler_calls = 0

            async def execute(_call_id, _args, _token, _update):
                nonlocal handler_calls
                handler_calls += 1
                return AgentToolResult(content=[])

            base, run_store = _durable_tool_runner(
                Path(directory) / "state.sqlite3",
                budget=ClosedLoopBudget(max_plan_steps=1),
                execute=execute,
            )
            corrected = MultiIntentPlan(
                "read",
                (PlanStep("corrected", "read"),),
                plan_id="budget-corrected-plan",
            )
            runner = AutonomousPlanRunner(
                base.workflow,
                result_validator=lambda *_args: ResultValidation.invalid(
                    "force correction"
                ),
                replanner=lambda *_args: corrected,
                budget=base.budget,
                run_store=run_store,
            )

            result = await _run_bootstrapped(runner, run_store)
            self.assertEqual(result.status, "failed")
            self.assertEqual(handler_calls, 1)
            with self.assertRaises(PlanNotFoundError):
                await base.workflow.store.load(corrected.plan_id)  # type: ignore[union-attr]

    async def test_planner_crash_keeps_durable_usage_and_blocks_second_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workflow = _CrashingWorkflow()
            meter = _AdmissionMeter()
            store = _journal_store(Path(directory) / "state.sqlite3")
            runner = AutonomousPlanRunner(
                workflow,  # type: ignore[arg-type]
                budget=ClosedLoopBudget(
                    max_model_calls=1,
                    max_tokens=10,
                    max_cost=0.5,
                ),
                usage_meter=meter,
                run_store=store,
            )

            with self.assertRaisesRegex(RuntimeError, "response lost"):
                await runner.prepare("read", run_id="crash-run")
            durable = await store.load("crash-run")
            self.assertEqual(durable.resource_usage.model_calls, 1)
            self.assertEqual(durable.resource_usage.tokens, 10)

            reopened_runner = AutonomousPlanRunner(
                workflow,  # type: ignore[arg-type]
                budget=runner.budget,
                usage_meter=meter,
                run_store=_journal_store(Path(directory) / "state.sqlite3"),
            )
            with self.assertRaises(PlanBudgetExceeded):
                await reopened_runner.prepare("read", run_id="crash-run")
            self.assertEqual(workflow.calls, 1)

    async def test_planner_is_inside_absolute_deadline_and_resume_cannot_reset_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            workflow = _SlowWorkflow()
            budget = ClosedLoopBudget(max_duration_seconds=0.5)
            runner = AutonomousPlanRunner(
                workflow,  # type: ignore[arg-type]
                budget=budget,
                run_store=_journal_store(path),
            )

            with self.assertRaisesRegex(PlanBudgetExceeded, "Planner"):
                await runner.prepare("read", run_id="deadline-run")
            self.assertEqual(workflow.calls, 1)

            reopened = AutonomousPlanRunner(
                workflow,  # type: ignore[arg-type]
                budget=budget,
                run_store=_journal_store(path),
            )
            with self.assertRaisesRegex(PlanBudgetExceeded, "deadline"):
                await reopened.prepare("read", run_id="deadline-run")
            self.assertEqual(workflow.calls, 1)

    async def test_opaque_result_validator_is_reserved_and_settled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            async def execute(_call_id, _args, _token, _update):
                return AgentToolResult(content=[])

            base, run_store = _durable_tool_runner(
                Path(directory) / "state.sqlite3",
                budget=ClosedLoopBudget(),
                execute=execute,
            )
            meter = _AdmissionMeter()
            runner = AutonomousPlanRunner(
                base.workflow,
                result_validator=lambda *_args: ResultValidation.valid(),
                result_synthesizer=lambda *_args: "metered result",
                budget=ClosedLoopBudget(
                    max_model_calls=3,
                    max_tokens=30,
                    max_cost=1.5,
                ),
                usage_meter=meter,
                run_store=run_store,
            )
            result = await _run_bootstrapped(runner, run_store)

            self.assertEqual(result.status, "completed")
            self.assertEqual(
                meter.stages,
                ["planner", "result_validator", "synthesizer"],
            )
            self.assertEqual(meter.dispatched, meter.stages)

    async def test_replanner_and_every_followup_model_stage_are_metered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            async def execute(_call_id, _args, _token, _update):
                return AgentToolResult(content=[])

            base, run_store = _durable_tool_runner(
                Path(directory) / "state.sqlite3",
                budget=ClosedLoopBudget(max_plan_steps=2),
                execute=execute,
            )
            corrected = MultiIntentPlan(
                "read",
                (PlanStep("corrected", "read"),),
                plan_id="metered-corrected-plan",
            )
            meter = _AdmissionMeter()

            def validate(observation, _token):
                return (
                    ResultValidation.valid()
                    if observation.latest_plan.plan_id == corrected.plan_id
                    else ResultValidation.invalid("retry once")
                )

            runner = AutonomousPlanRunner(
                base.workflow,
                result_validator=validate,
                replanner=lambda *_args: corrected,
                result_synthesizer=lambda *_args: "done",
                budget=ClosedLoopBudget(
                    max_plan_steps=2,
                    max_model_calls=5,
                    max_tokens=50,
                    max_cost=2.5,
                ),
                usage_meter=meter,
                run_store=run_store,
            )
            result = await _run_bootstrapped(runner, run_store)

            self.assertEqual(result.status, "completed")
            self.assertEqual(
                meter.stages,
                [
                    "planner",
                    "result_validator",
                    "replanner",
                    "result_validator",
                    "synthesizer",
                ],
            )
            self.assertEqual(meter.dispatched, meter.stages)

    async def test_synthesizer_timeout_uses_deterministic_terminal_fallback(self) -> None:
        async def slow_synthesis(*_args):
            await asyncio.sleep(2)
            return "too late"

        runner = AutonomousPlanRunner(
            _SuccessfulWorkflow(),  # type: ignore[arg-type]
            result_validator=_trusted_success_validator,
            result_synthesizer=slow_synthesis,
            budget=ClosedLoopBudget(max_duration_seconds=0.2),
        )

        result = await runner.run("read")

        self.assertEqual(result.status, "completed")
        self.assertIn("确定性汇总", result.response_text)
        self.assertNotIn("too late", result.response_text)

    async def test_durable_synthesis_budget_exhaustion_finalizes_and_resumes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            async def execute(_call_id, _args, _token, _update):
                return AgentToolResult(content=[])

            base, run_store = _durable_tool_runner(
                Path(directory) / "state.sqlite3",
                budget=ClosedLoopBudget(),
                execute=execute,
            )
            synthesis_calls = 0

            def synthesize(*_args):
                nonlocal synthesis_calls
                synthesis_calls += 1
                return "must not be called"

            runner = AutonomousPlanRunner(
                base.workflow,
                result_validator=_trusted_success_validator,
                result_synthesizer=synthesize,
                budget=ClosedLoopBudget(
                    # Planner + trusted validator consume the budget; synthesis
                    # is the next stage and must take the deterministic fallback.
                    max_model_calls=2,
                    max_tokens=20,
                    max_cost=1.0,
                ),
                usage_meter=_AdmissionMeter(),
                run_store=run_store,
            )
            result = await _run_bootstrapped(runner, run_store)

            self.assertEqual(result.status, "completed")
            self.assertEqual(synthesis_calls, 0)
            self.assertIn("确定性汇总", result.response_text)
            durable = await run_store.load(result.closed_loop_run_id or "")
            self.assertEqual(durable.status, "completed")
            resumed = await runner.resume(result.plans[-1].plan_id)
            self.assertEqual(resumed.status, "completed")
            self.assertEqual(resumed.response_text, result.response_text)
            self.assertEqual(synthesis_calls, 0)


if __name__ == "__main__":
    unittest.main()
