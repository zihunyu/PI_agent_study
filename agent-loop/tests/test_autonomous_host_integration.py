"""Durable Host integration tests for autonomous complex-request execution."""

from __future__ import annotations

import asyncio
import tempfile
import unittest

from pi_agent_loop import (
    CapabilityRegistry,
    ClosedLoopBudget,
    DurableAgentHost,
    HybridModelRouter,
    HybridRequestPlanner,
    IntentPlanPolicy,
    Model,
    PlanBudgetExceeded,
    PlanResourceUsage,
    PlanUsageReservation,
    RequestDecision,
    ResultValidation,
    ScriptedProvider,
    TaskDecision,
    TaskDependencyHint,
    assistant_message,
)


MODEL = Model(id="autonomous-host-model", provider="test", api="scripted")


class _FixedRouter:
    def __init__(self, decision: RequestDecision) -> None:
        self.decision = decision
        self.calls: list[str] = []

    def route(self, text: str) -> RequestDecision:
        self.calls.append(text)
        return self.decision


class _HybridRouterStub(HybridModelRouter):
    def __init__(self, decision: RequestDecision) -> None:
        self.decision = decision
        self.call_count = 0
        self._evaluation_metrics = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cost": 0.0,
        }

    async def route(self, text: str) -> RequestDecision:
        del text
        self.call_count += 1
        self._evaluation_metrics = {
            "input_tokens": 2,
            "output_tokens": 1,
            "cost": 0.2,
        }
        return self.decision


class _UsageMeter:
    def __init__(self) -> None:
        self.stages: list[str] = []
        self.dispatched: list[str] = []

    def reserve(self, *, run_id, reservation_id, stage, remaining):
        del run_id, remaining
        self.stages.append(stage)
        return PlanUsageReservation(
            reservation_id,
            stage,
            PlanResourceUsage(model_calls=1, tokens=10, cost=0.5),
        )

    async def dispatch(self, reservation, callback):
        self.dispatched.append(reservation.stage)
        return await callback()

    def settle(self, _reservation):
        return PlanResourceUsage(model_calls=1, tokens=10, cost=0.5)

    def cancel(self, _reservation):
        return None


def _complex_decision() -> RequestDecision:
    record = RequestDecision(
        status="in_scope_tool_ready",
        reason="trusted record-read task",
        message="读取主记录",
        domain="generic",
        intent="record.read",
        required_capabilities=("records.read",),
        task_id="record",
    )
    detail = RequestDecision(
        status="in_scope_tool_ready",
        reason="trusted dependent detail-read task",
        message="读取明细",
        domain="generic",
        intent="detail.read",
        required_capabilities=("details.read",),
        task_id="detail",
        depends_on=("record",),
    )
    task_decision = TaskDecision(
        components=(record, detail),
        dependencies=(TaskDependencyHint("detail", ("record",)),),
    )
    return RequestDecision(
        status="in_scope_plan_required",
        reason="request contains dependent tasks",
        message="该请求需要先形成并执行任务计划。",
        domain="generic",
        intent="multi_intent",
        component_decisions=(record, detail),
        task_decision=task_decision,
    )


def _message_text(message: dict) -> str:
    return "".join(
        str(part.get("text", ""))
        for part in message.get("content", [])
        if isinstance(part, dict) and part.get("type") == "text"
    )


def _trusted_fixture_validator(observation, _token) -> ResultValidation:
    latest = observation.latest_execution
    assert latest is not None
    if latest.state.phase == "completed":
        return ResultValidation.valid()
    return ResultValidation.invalid(f"fixture ended in {latest.state.phase}")


class AutonomousHostIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_autonomous_plan复用终态且不重复步骤(self) -> None:
        request = "先读取主记录，再读取明细"
        policies = {
            "record.read": IntentPlanPolicy("record.read"),
            "detail.read": IntentPlanPolicy("detail.read"),
        }
        executed: list[str] = []

        async def execute_step(step, _token, *, fencing_token=None):
            self.assertIsInstance(fencing_token, int)
            executed.append(step.step_id)
            return step.step_id

        with tempfile.TemporaryDirectory() as directory:
            host = await DurableAgentHost.create(
                session_id="autonomous-host-resume",
                state_dir=directory,
                model=MODEL,
                stream_fn=ScriptedProvider([]).stream,
                system_prompt="autonomous resume",
                tools=[],
                router=_FixedRouter(_complex_decision()),
                capabilities=CapabilityRegistry(),
                auto_recover=False,
                planner=HybridRequestPlanner(policies, lambda *_args: None),
                plan_policies=policies,
                plan_step_executor=execute_step,
                plan_result_validator=_trusted_fixture_validator,
            )
            try:
                initial = await host.prompt(request)
                assert initial.plan_id is not None
                resumed = await host.resume_autonomous_plan(initial.plan_id)

                self.assertEqual(resumed.autonomous_result.status, "completed")
                self.assertEqual(resumed.plan_id, initial.plan_id)
                self.assertEqual(executed, ["record", "detail"])
                projected = await host.context_projection.project(host.session_id)
                self.assertEqual(
                    [message.get("role") for message in projected.messages[-2:]],
                    ["user", "assistant"],
                )
                self.assertEqual(
                    _message_text(projected.messages[-1]),
                    resumed.autonomous_result.response_text,
                )
            finally:
                await host.close()

    async def test_plan_required由host自动规划执行并持久化最终对话(self) -> None:
        request = "先读取主记录，再根据主记录读取明细"
        policies = {
            "record.read": IntentPlanPolicy(
                "record.read",
                capabilities=("records.read",),
            ),
            "detail.read": IntentPlanPolicy(
                "detail.read",
                capabilities=("details.read",),
            ),
        }
        planner_calls = 0

        async def planner_fn(received_request, _catalog):
            nonlocal planner_calls
            planner_calls += 1
            self.assertEqual(received_request, request)
            return {
                "planId": "host-autonomous-plan",
                "steps": [
                    {
                        "stepId": "record",
                        "intent": "record.read",
                        "arguments": {"recordId": "R-1"},
                        "capabilities": ["records.read"],
                    },
                    {
                        "stepId": "detail",
                        "intent": "detail.read",
                        "arguments": {"recordId": "R-1"},
                        "dependsOn": ["record"],
                        "capabilities": ["details.read"],
                    },
                ],
            }

        executed: list[str] = []

        async def execute_step(step, _token, *, fencing_token=None):
            self.assertIsInstance(fencing_token, int)
            executed.append(step.step_id)
            return {"step": step.step_id, "ok": True}

        router = _FixedRouter(_complex_decision())
        provider = ScriptedProvider([])
        with tempfile.TemporaryDirectory() as directory:
            host = await DurableAgentHost.create(
                session_id="autonomous-host-session",
                state_dir=directory,
                model=MODEL,
                stream_fn=provider.stream,
                system_prompt="autonomous host integration",
                tools=[],
                router=router,
                capabilities=CapabilityRegistry(),
                auto_recover=False,
                planner=HybridRequestPlanner(policies, planner_fn),
                plan_policies=policies,
                plan_step_executor=execute_step,
                plan_result_validator=_trusted_fixture_validator,
            )
            try:
                result = await host.prompt(request)

                self.assertIsNotNone(result.autonomous_result)
                assert result.autonomous_result is not None
                self.assertEqual(result.autonomous_result.status, "completed")
                self.assertEqual(result.plan_id, result.autonomous_result.plan_id)
                self.assertTrue(result.plan_id)
                assert host.autonomous_run_store is not None
                durable_run = await host.autonomous_run_store.find_by_plan_id(
                    result.plan_id
                )
                self.assertIsNotNone(durable_run)
                assert durable_run is not None
                self.assertTrue(durable_run.initial_plan_bound)
                self.assertTrue(durable_run.linked)
                self.assertTrue(durable_run.dispatchable)
                run_rows = await host.autonomous_run_store.journal.load_events(
                    host.autonomous_run_store.principal,
                    session_id=host.session_id,
                    operation_id=f"autonomous-run:{durable_run.run_id}",
                    journal_kind="retry",
                )
                self.assertEqual(
                    [row.event_type for row in run_rows].count(
                        "autonomous_run_initialized"
                    ),
                    1,
                )
                self.assertEqual(
                    [row.event_type for row in run_rows].count(
                        "autonomous_initial_plan_bound"
                    ),
                    1,
                )
                self.assertEqual(result.pending_approval_ids, ())
                self.assertEqual(executed, ["record", "detail"])
                # A structured Router decision is the execution input.  The
                # free-form fallback planner must not classify the same request
                # a second time or silently change its trusted dependency graph.
                self.assertEqual(planner_calls, 0)
                self.assertEqual(provider.call_count, 0)
                self.assertEqual(result.result.response_text, result.autonomous_result.response_text)
                self.assertIsNotNone(result.result.decision.task_decision)
                self.assertEqual(
                    result.result.decision.task_decision.dependencies,
                    (TaskDependencyHint("detail", ("record",)),),
                )

                projected = await host.context_projection.project(host.session_id)
                self.assertEqual(
                    [message.get("role") for message in projected.messages[-2:]],
                    ["user", "assistant"],
                )
                self.assertEqual(_message_text(projected.messages[-2]), request)
                self.assertEqual(
                    _message_text(projected.messages[-1]),
                    result.autonomous_result.response_text,
                )
                self.assertEqual(
                    host.agent.state.messages,
                    list(projected.messages),
                )
            finally:
                await host.close()

            # A new Host must recover the same conversation; an in-memory append
            # alone is not sufficient for a durable Session contract.
            reopened = await DurableAgentHost.create(
                session_id="autonomous-host-session",
                state_dir=directory,
                model=MODEL,
                stream_fn=ScriptedProvider([]).stream,
                system_prompt="autonomous host integration",
                tools=[],
                router=_FixedRouter(_complex_decision()),
                capabilities=CapabilityRegistry(),
                auto_recover=False,
                planner=HybridRequestPlanner(policies, planner_fn),
                plan_policies=policies,
                plan_step_executor=execute_step,
                plan_result_validator=_trusted_fixture_validator,
            )
            try:
                self.assertEqual(
                    [_message_text(message) for message in reopened.agent.state.messages[-2:]],
                    [request, result.autonomous_result.response_text],
                )
            finally:
                await reopened.close()

    async def test_plan_required但未配置planner时显式失败且零执行(self) -> None:
        router = _FixedRouter(_complex_decision())
        provider = ScriptedProvider([])
        with tempfile.TemporaryDirectory() as directory:
            host = await DurableAgentHost.create(
                session_id="autonomous-host-no-planner",
                state_dir=directory,
                model=MODEL,
                stream_fn=provider.stream,
                system_prompt="autonomous host fail closed",
                tools=[],
                router=router,
                capabilities=CapabilityRegistry(),
                auto_recover=False,
            )
            try:
                result = await host.prompt("执行一个复杂任务")

                self.assertIsNone(result.autonomous_result)
                self.assertIsNone(result.plan_id)
                self.assertFalse(result.result.model_called)
                self.assertEqual(
                    result.result.error_code,
                    "plan_workflow_unavailable",
                )
                self.assertEqual(
                    result.result.decision.status,
                    "in_scope_need_clarification",
                )
                self.assertEqual(provider.call_count, 0)
                self.assertIn("未配置", result.result.response_text)
            finally:
                await host.close()

    async def test_plan_required缺少结构化task_decision时不回退到文本规划(self) -> None:
        unstructured = RequestDecision(
            status="in_scope_plan_required",
            reason="classifier only",
            message="需要计划",
            domain="generic",
            intent="multi_intent",
        )
        planner_calls = 0
        execution_calls = 0

        async def planner_fn(_request, _catalog):
            nonlocal planner_calls
            planner_calls += 1
            return {
                "steps": [{"stepId": "read", "intent": "record.read"}],
            }

        async def execute_step(_step, _token):
            nonlocal execution_calls
            execution_calls += 1
            return "must not execute"

        policies = {"record.read": IntentPlanPolicy("record.read")}
        provider = ScriptedProvider([])
        with tempfile.TemporaryDirectory() as directory:
            host = await DurableAgentHost.create(
                session_id="autonomous-host-no-task-decision",
                state_dir=directory,
                model=MODEL,
                stream_fn=provider.stream,
                system_prompt="structured task decision required",
                tools=[],
                router=_FixedRouter(unstructured),
                capabilities=CapabilityRegistry(),
                auto_recover=False,
                planner=HybridRequestPlanner(policies, planner_fn),
                plan_policies=policies,
                plan_step_executor=execute_step,
            )
            try:
                result = await host.prompt("先查记录，再查明细")

                self.assertEqual(result.result.error_code, "task_decision_missing")
                self.assertEqual(
                    result.result.decision.status,
                    "in_scope_need_clarification",
                )
                self.assertEqual(planner_calls, 0)
                self.assertEqual(execution_calls, 0)
                self.assertEqual(provider.call_count, 0)
                self.assertIsNone(result.autonomous_result)
            finally:
                await host.close()

    async def test_hard_budget在router前预留并沿用同一run执行plan(self) -> None:
        policies = {
            "record.read": IntentPlanPolicy(
                "record.read", capabilities=("records.read",)
            ),
            "detail.read": IntentPlanPolicy(
                "detail.read", capabilities=("details.read",)
            ),
        }
        meter = _UsageMeter()
        executed: list[str] = []

        async def execute_step(step, _token, *, fencing_token=None):
            self.assertIsNotNone(fencing_token)
            executed.append(step.step_id)
            return step.step_id

        with tempfile.TemporaryDirectory() as directory:
            host = await DurableAgentHost.create(
                session_id="autonomous-hard-router-budget",
                state_dir=directory,
                model=MODEL,
                stream_fn=ScriptedProvider([]).stream,
                system_prompt="hard budget",
                tools=[],
                router=_HybridRouterStub(_complex_decision()),
                capabilities=CapabilityRegistry(),
                auto_recover=False,
                planner=HybridRequestPlanner(policies, lambda *_args: None),
                plan_policies=policies,
                plan_step_executor=execute_step,
                plan_result_validator=_trusted_fixture_validator,
                plan_correction_budget=ClosedLoopBudget(
                    max_model_calls=2,
                    max_tokens=20,
                    max_cost=1.0,
                ),
                plan_usage_meter=meter,
            )
            try:
                result = await host.prompt("read record then detail")

                self.assertEqual(result.autonomous_result.status, "completed")
                self.assertEqual(executed, ["record", "detail"])
                self.assertEqual(meter.stages, ["router", "result_validator"])
                self.assertEqual(meter.dispatched, ["router", "result_validator"])
                assert host.autonomous_run_store is not None
                durable = await host.autonomous_run_store.find_by_plan_id(
                    result.plan_id
                )
                assert durable is not None
                self.assertEqual(durable.resource_usage.model_calls, 2)
                self.assertEqual(durable.resource_usage.tokens, 20)
            finally:
                await host.close()

    async def test_hard_plan_budget结算router后不阻断普通agent(self) -> None:
        direct = RequestDecision(
            status="in_scope_no_tool",
            reason="direct answer",
            message="direct",
            domain="generic",
            intent="record.read",
            task_id="record",
        )
        policies = {"record.read": IntentPlanPolicy("record.read")}
        meter = _UsageMeter()
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=MODEL,
                    content=[{"type": "text", "text": "ordinary answer"}],
                )
            ]
        )

        async def execute_step(step, _token, *, fencing_token=None):
            del step, fencing_token
            return None

        with tempfile.TemporaryDirectory() as directory:
            host = await DurableAgentHost.create(
                session_id="autonomous-hard-direct-block",
                state_dir=directory,
                model=MODEL,
                stream_fn=provider.stream,
                system_prompt="hard budget",
                tools=[],
                router=_HybridRouterStub(direct),
                capabilities=CapabilityRegistry(),
                auto_recover=False,
                planner=HybridRequestPlanner(
                    policies,
                    lambda *_args: {
                        "steps": [{"stepId": "record", "intent": "record.read"}]
                    },
                ),
                plan_policies=policies,
                plan_step_executor=execute_step,
                plan_correction_budget=ClosedLoopBudget(
                    max_model_calls=1,
                    max_tokens=10,
                    max_cost=0.5,
                ),
                plan_usage_meter=meter,
            )
            try:
                result = await host.prompt("answer directly")

                self.assertTrue(result.result.model_called)
                self.assertEqual(result.result.response_text, "ordinary answer")
                self.assertIsNone(result.result.error_code)
                self.assertEqual(provider.call_count, 1)
                self.assertEqual(meter.stages, ["router"])
                self.assertEqual(meter.dispatched, ["router"])
                rows = await host.autonomous_run_store.journal.load_events(
                    host.autonomous_run_store.principal,
                    session_id=host.session_id,
                    journal_kind="retry",
                )
                self.assertEqual(
                    [row.event_type for row in rows].count(
                        "autonomous_admission_closed"
                    ),
                    1,
                )
            finally:
                await host.close()

    async def test_hard_budget拒绝没有pre_route_admission的自定义router(self) -> None:
        policies = {"record.read": IntentPlanPolicy("record.read")}

        async def execute_step(step, _token, *, fencing_token=None):
            del step, fencing_token
            return None

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "pre-route"):
                await DurableAgentHost.create(
                    session_id="autonomous-hard-custom-router",
                    state_dir=directory,
                    model=MODEL,
                    stream_fn=ScriptedProvider([]).stream,
                    system_prompt="hard budget",
                    tools=[],
                    router=_FixedRouter(_complex_decision()),
                    capabilities=CapabilityRegistry(),
                    auto_recover=False,
                    planner=HybridRequestPlanner(policies, lambda *_args: None),
                    plan_policies=policies,
                    plan_step_executor=execute_step,
                    plan_correction_budget=ClosedLoopBudget(max_model_calls=1),
                    plan_usage_meter=_UsageMeter(),
                )

    async def test_duration_budget从router前的durable_deadline开始(self) -> None:
        policies = {"record.read": IntentPlanPolicy("record.read")}
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=MODEL,
                    content=[{"type": "text", "text": "too late"}],
                )
            ]
        )

        class _SlowRouter:
            async def route(self, _text):
                await asyncio.sleep(1)
                return RequestDecision(
                    status="in_scope_no_tool",
                    reason="late",
                    message="late",
                    domain="generic",
                    intent="record.read",
                )

        async def execute_step(step, _token, *, fencing_token=None):
            del step, fencing_token
            return None

        with tempfile.TemporaryDirectory() as directory:
            host = await DurableAgentHost.create(
                session_id="autonomous-router-deadline",
                state_dir=directory,
                model=MODEL,
                stream_fn=provider.stream,
                system_prompt="router deadline",
                tools=[],
                router=_SlowRouter(),
                capabilities=CapabilityRegistry(),
                auto_recover=False,
                planner=HybridRequestPlanner(
                    policies,
                    lambda *_args: {
                        "steps": [{"stepId": "record", "intent": "record.read"}]
                    },
                ),
                plan_policies=policies,
                plan_step_executor=execute_step,
                plan_correction_budget=ClosedLoopBudget(
                    max_duration_seconds=0.2,
                ),
            )
            try:
                with self.assertRaisesRegex(
                    PlanBudgetExceeded,
                    "Router|deadline|wall-clock",
                ):
                    await host.prompt("slow route")
                self.assertEqual(provider.call_count, 0)
                rows = await host.autonomous_run_store.journal.load_events(
                    host.autonomous_run_store.principal,
                    session_id=host.session_id,
                    journal_kind="retry",
                )
                self.assertEqual(
                    [row.event_type for row in rows].count(
                        "autonomous_admission_closed"
                    ),
                    1,
                )
            finally:
                await host.close()


if __name__ == "__main__":
    unittest.main()
