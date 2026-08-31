"""Raw exceptions must not cross public Agent/Plan/telemetry boundaries."""

from __future__ import annotations

import unittest

from pi_agent_loop import (
    AssistantMessageEventStream,
    ClosedLoopAction,
    ClosedLoopExecutor,
    Model,
    ModelAttemptAdmissionScope,
    ResultValidation,
)
from pi_agent_loop.harness.model_runtime_adapter import (
    _push_model_attempt_budget_error,
)
from pi_agent_loop.planning import (
    IntentPlanPolicy,
    MultiIntentPlan,
    PlanExecutor,
    PlanStep,
)


SECRET = "sk-live-super-secret"
PRIVATE_PATH = r"C:\Users\admin\private\customer-export.json"
RAW_ERROR = f"Authorization: Bearer {SECRET}; source={PRIVATE_PATH}"
MODEL = Model(id="boundary-model", provider="test", api="scripted")


def assert_raw_error_absent(test: unittest.TestCase, value: object) -> None:
    rendered = repr(value)
    test.assertNotIn(SECRET, rendered)
    test.assertNotIn(PRIVATE_PATH, rendered)
    test.assertNotIn(RAW_ERROR, rendered)


class PublicExceptionBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_loop_result_and_events_exclude_action_exception_text(
        self,
    ) -> None:
        async def execute(_action, _token):
            raise RuntimeError(RAW_ERROR)

        def validate(_observation, _token):
            self.fail("unsafe failed action must skip validation")

        def replan(_observation, _validation, _token):
            self.fail("unsafe failed action must skip replanning")

        result = await ClosedLoopExecutor(execute, validate, replan).execute(
            ClosedLoopAction("write", replay_policy="never", write=True)
        )

        self.assertEqual(result.status, "manual_intervention")
        assert_raw_error_absent(self, result)
        self.assertIn("RuntimeError", result.reason or "")
        failed = next(
            event
            for event in result.events
            if event.type == "closed_loop_action_outcome_unknown"
        )
        self.assertEqual(failed.data["errorType"], "RuntimeError")
        self.assertEqual(
            failed.data["errorCode"],
            "closed_loop_action_outcome_unknown",
        )

    async def test_closed_loop_observer_error_is_public_and_non_fatal(self) -> None:
        async def execute(_action, _token):
            return {"ok": True}

        def validate(_observation, _token):
            return ResultValidation.valid()

        def replan(_observation, _validation, _token):
            return None

        def broken_sink(_event):
            raise RuntimeError(RAW_ERROR)

        result = await ClosedLoopExecutor(
            execute,
            validate,
            replan,
            event_sink=broken_sink,
        ).execute(ClosedLoopAction("read"))

        self.assertEqual(result.status, "completed")
        assert_raw_error_absent(self, result)
        self.assertTrue(result.event_sink_errors)
        self.assertTrue(
            all("RuntimeError" in item for item in result.event_sink_errors)
        )

    async def test_plan_result_state_and_events_exclude_step_exception_text(
        self,
    ) -> None:
        plan = MultiIntentPlan(
            "public exception boundary",
            (PlanStep("read", "record.read"),),
        )

        async def execute(_step, _token):
            raise RuntimeError(RAW_ERROR)

        result = await PlanExecutor(
            plan,
            {"record.read": IntentPlanPolicy("record.read")},
            execute,
        ).execute()

        assert_raw_error_absent(self, result)
        failed = next(event for event in result.events if event.type == "step_failed")
        self.assertEqual(failed.data["errorCode"], "step_execution_failed")
        self.assertEqual(failed.data["errorType"], "RuntimeError")
        self.assertEqual(failed.data["error"], "Plan step execution failed")

    async def test_model_attempt_error_excludes_exception_text_from_terminal(self) -> None:
        output = AssistantMessageEventStream()
        scope = ModelAttemptAdmissionScope(
            run_id="run-boundary",
            stage="planner",
            reservation_id="reservation-boundary",
            max_model_calls=1,
        )

        _push_model_attempt_budget_error(
            output,
            MODEL,
            scope,
            RuntimeError(RAW_ERROR),
        )
        final = await output.result()

        assert_raw_error_absent(self, final)
        admission = final["modelAttemptAdmission"]
        self.assertEqual(admission["errorType"], "RuntimeError")
        self.assertEqual(
            admission["reason"],
            "Model provider attempt was rejected by the hard budget",
        )


if __name__ == "__main__":
    unittest.main()
