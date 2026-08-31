"""Trusted execute-validate-correct closed-loop tests."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import (  # noqa: E402
    CancellationToken,
    ClosedLoopAction,
    ClosedLoopBudget,
    ClosedLoopExecutor,
    CorrectionPlan,
    OperationCancelledError,
    ResultValidation,
)


class ClosedLoopExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_initial_result_completes_without_replanning(self) -> None:
        calls: list[str] = []
        planner_calls = 0

        async def execute(action, _token):
            calls.append(action.action_id)
            return {"total": 42}

        def validate(observation, _token):
            self.assertEqual(observation.latest_attempt.result, {"total": 42})
            return ResultValidation.valid()

        def replan(_observation, _validation, _token):
            nonlocal planner_calls
            planner_calls += 1
            return None

        result = await ClosedLoopExecutor(execute, validate, replan).execute(
            ClosedLoopAction("initial")
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(calls, ["initial"])
        self.assertEqual(planner_calls, 0)
        self.assertEqual(result.correction_rounds, 0)
        self.assertEqual(result.events[-1].type, "closed_loop_completed")

    async def test_invalid_safe_result_is_corrected_and_revalidated(self) -> None:
        calls: list[str] = []

        async def execute(action, _token):
            calls.append(action.action_id)
            return {"ok": action.action_id == "fix"}

        def validate(observation, _token):
            if observation.latest_attempt.result["ok"]:
                return ResultValidation.valid()
            return ResultValidation.invalid("result is not ready")

        def replan(_observation, _validation, _token):
            return CorrectionPlan(
                (ClosedLoopAction("fix", {"mode": "repair"}),),
                "repair invalid result",
            )

        result = await ClosedLoopExecutor(execute, validate, replan).execute(
            ClosedLoopAction("initial")
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(calls, ["initial", "fix"])
        self.assertEqual(result.correction_rounds, 1)
        self.assertEqual(result.correction_actions, 1)
        self.assertEqual(
            [item.status for item in result.validations],
            ["invalid", "valid"],
        )

    async def test_round_budget_exhaustion_is_a_failed_terminal_state(self) -> None:
        calls: list[str] = []

        async def execute(action, _token):
            calls.append(action.action_id)
            return {"ok": False}

        def validate(_observation, _token):
            return ResultValidation.invalid("still invalid")

        def replan(observation, _validation, _token):
            return CorrectionPlan(
                (ClosedLoopAction(f"fix-{observation.correction_rounds + 1}"),),
                "try again",
            )

        result = await ClosedLoopExecutor(
            execute,
            validate,
            replan,
            budget=ClosedLoopBudget(max_correction_rounds=1, max_correction_actions=5),
        ).execute(ClosedLoopAction("initial"))

        self.assertEqual(result.status, "failed")
        self.assertIn("rounds", result.reason or "")
        self.assertEqual(calls, ["initial", "fix-1"])
        self.assertIn("closed_loop_budget_exhausted", [e.type for e in result.events])

    async def test_action_budget_rejects_whole_batch_before_partial_execution(self) -> None:
        calls: list[str] = []

        async def execute(action, _token):
            calls.append(action.action_id)
            return {"ok": False}

        def validate(_observation, _token):
            return ResultValidation.invalid("needs two corrections")

        def replan(_observation, _validation, _token):
            return CorrectionPlan(
                (ClosedLoopAction("fix-a"), ClosedLoopAction("fix-b")),
                "two-part repair",
            )

        result = await ClosedLoopExecutor(
            execute,
            validate,
            replan,
            budget=ClosedLoopBudget(max_correction_rounds=2, max_correction_actions=1),
        ).execute(ClosedLoopAction("initial"))

        self.assertEqual(result.status, "failed")
        self.assertEqual(calls, ["initial"])
        self.assertEqual(result.correction_actions, 0)

    async def test_invalid_never_replay_initial_action_goes_directly_to_manual(self) -> None:
        calls: list[str] = []
        planner_calls = 0

        async def execute(action, _token):
            calls.append(action.action_id)
            return {"businessState": "unexpected"}

        def validate(_observation, _token):
            return ResultValidation.invalid("business state did not match")

        def replan(_observation, _validation, _token):
            nonlocal planner_calls
            planner_calls += 1
            return CorrectionPlan((ClosedLoopAction("retry"),), "retry")

        result = await ClosedLoopExecutor(execute, validate, replan).execute(
            ClosedLoopAction("charge", replay_policy="never", write=True)
        )

        self.assertEqual(result.status, "manual_intervention")
        self.assertEqual(calls, ["charge"])
        self.assertEqual(planner_calls, 0)
        self.assertIn("automatic correction is prohibited", result.reason or "")

    async def test_never_replay_validator_failure_is_manual_not_failed(self) -> None:
        effects = []

        async def execute(_action, _token):
            effects.append("written")
            return {"committed": True}

        def validate(_observation, _token):
            raise RuntimeError("validator unavailable")

        def replan(_observation, _validation, _token):
            self.fail("never-replay validation failure must not be replanned")

        result = await ClosedLoopExecutor(execute, validate, replan).execute(
            ClosedLoopAction("write", replay_policy="never", write=True)
        )

        self.assertEqual(result.status, "manual_intervention")
        self.assertEqual(effects, ["written"])
        self.assertIn("Result validator failed", result.reason or "")
        self.assertIn("RuntimeError", result.reason or "")
        self.assertNotIn("validator unavailable", result.reason or "")

    async def test_unsafe_correction_rejects_entire_plan(self) -> None:
        calls: list[str] = []

        async def execute(action, _token):
            calls.append(action.action_id)
            return {"ok": False}

        def validate(_observation, _token):
            return ResultValidation.invalid("needs correction")

        def replan(_observation, _validation, _token):
            return CorrectionPlan(
                (
                    ClosedLoopAction("safe-first"),
                    ClosedLoopAction(
                        "write-second",
                        replay_policy="never",
                        write=True,
                    ),
                ),
                "mixed plan",
            )

        result = await ClosedLoopExecutor(execute, validate, replan).execute(
            ClosedLoopAction("initial")
        )

        self.assertEqual(result.status, "manual_intervention")
        self.assertEqual(calls, ["initial"])
        rejected = next(
            event
            for event in result.events
            if event.type == "closed_loop_correction_rejected"
        )
        self.assertEqual(rejected.data["executedActionCount"], 0)

    async def test_outcome_unknown_result_skips_validator_and_replanner(self) -> None:
        validator_calls = 0
        planner_calls = 0

        async def execute(_action, _token):
            return {
                "details": {
                    "code": "outcome_unknown",
                    "outcomeUnknown": True,
                }
            }

        def validate(_observation, _token):
            nonlocal validator_calls
            validator_calls += 1
            return ResultValidation.valid()

        def replan(_observation, _validation, _token):
            nonlocal planner_calls
            planner_calls += 1
            return None

        result = await ClosedLoopExecutor(execute, validate, replan).execute(
            ClosedLoopAction("read")
        )

        self.assertEqual(result.status, "manual_intervention")
        self.assertEqual(validator_calls, 0)
        self.assertEqual(planner_calls, 0)

    async def test_waiting_approval_validation_suspends_without_replanning(self) -> None:
        calls: list[str] = []
        planner_calls = 0

        async def execute(action, _token):
            calls.append(action.action_id)
            return {"phase": "waiting_approval", "approvalId": "approval-1"}

        def validate(observation, _token):
            self.assertEqual(
                observation.latest_attempt.result["phase"],
                "waiting_approval",
            )
            return ResultValidation.suspended(
                "waiting for approval",
                details={"approvalId": "approval-1"},
            )

        def replan(_observation, _validation, _token):
            nonlocal planner_calls
            planner_calls += 1
            return CorrectionPlan((ClosedLoopAction("must-not-run"),), "unsafe")

        result = await ClosedLoopExecutor(execute, validate, replan).execute(
            # An already submitted write can legitimately suspend for approval;
            # suspension must win over the generic never-replay correction guard.
            ClosedLoopAction("submit-write", replay_policy="never", write=True)
        )

        self.assertEqual(result.status, "suspended")
        self.assertEqual(result.reason, "waiting for approval")
        self.assertEqual(calls, ["submit-write"])
        self.assertEqual(planner_calls, 0)
        self.assertEqual(result.events[-1].type, "closed_loop_suspended")

    async def test_safe_action_failure_can_be_replanned_within_budget(self) -> None:
        calls: list[str] = []

        async def execute(action, _token):
            calls.append(action.action_id)
            if action.action_id == "initial":
                raise ConnectionError("temporary read failure")
            return {"ok": True}

        def validate(observation, _token):
            if observation.latest_attempt.error is not None:
                return ResultValidation.invalid("read failed")
            return ResultValidation.valid()

        def replan(_observation, _validation, _token):
            return CorrectionPlan((ClosedLoopAction("retry-read"),), "safe retry")

        result = await ClosedLoopExecutor(execute, validate, replan).execute(
            ClosedLoopAction("initial")
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(calls, ["initial", "retry-read"])

    async def test_cancellation_stops_safe_action_and_emits_terminal_event(self) -> None:
        token = CancellationToken()
        entered = asyncio.Event()
        events = []

        async def execute(_action, _token):
            entered.set()
            await asyncio.Event().wait()

        def validate(_observation, _token):
            self.fail("validator must not run after cancellation")

        def replan(_observation, _validation, _token):
            self.fail("replanner must not run after cancellation")

        executor = ClosedLoopExecutor(
            execute,
            validate,
            replan,
            event_sink=events.append,
        )
        task = asyncio.create_task(
            executor.execute(ClosedLoopAction("read"), cancellation=token)
        )
        await asyncio.wait_for(entered.wait(), timeout=1)
        token.cancel("user stopped")

        with self.assertRaises(OperationCancelledError):
            await asyncio.wait_for(task, timeout=1)
        self.assertEqual(events[-1].type, "closed_loop_cancelled")

    async def test_cancelled_never_action_returns_manual_not_an_automatic_retry(self) -> None:
        token = CancellationToken()
        entered = asyncio.Event()
        calls = 0

        async def execute(_action, _token):
            nonlocal calls
            calls += 1
            entered.set()
            await asyncio.Event().wait()

        def validate(_observation, _token):
            self.fail("validator must not run for an uncertain write")

        def replan(_observation, _validation, _token):
            self.fail("replanner must not run for an uncertain write")

        executor = ClosedLoopExecutor(execute, validate, replan)
        task = asyncio.create_task(
            executor.execute(
                ClosedLoopAction("write", replay_policy="never", write=True),
                cancellation=token,
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=1)
        token.cancel("user stopped write")
        result = await asyncio.wait_for(task, timeout=1)

        self.assertEqual(result.status, "manual_intervention")
        self.assertEqual(calls, 1)

    async def test_callback_snapshots_cannot_mutate_internal_attempt_history(self) -> None:
        planner_seen = None

        async def execute(_action, _token):
            return {"value": "original"}

        def validate(observation, _token):
            observation.latest_attempt.result["value"] = "mutated-by-validator"
            return ResultValidation.invalid("force planning")

        def replan(observation, _validation, _token):
            nonlocal planner_seen
            planner_seen = observation.latest_attempt.result["value"]
            return None

        result = await ClosedLoopExecutor(execute, validate, replan).execute(
            ClosedLoopAction("initial")
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(planner_seen, "original")
        self.assertEqual(result.attempts[0].result["value"], "original")

    async def test_event_sequences_are_contiguous_and_payloads_are_isolated(self) -> None:
        events = []

        async def execute(_action, _token):
            return {"ok": True}

        def validate(_observation, _token):
            return ResultValidation.valid()

        def replan(_observation, _validation, _token):
            return None

        result = await ClosedLoopExecutor(
            execute,
            validate,
            replan,
            event_sink=events.append,
        ).execute(ClosedLoopAction("initial", {"secret": "kept-out-of-events"}))

        self.assertEqual(
            [event.sequence for event in result.events],
            list(range(1, len(result.events) + 1)),
        )
        self.assertEqual(events, list(result.events))
        self.assertNotIn("secret", str([event.to_dict() for event in result.events]))

    async def test_event_observer_failure_cannot_rewrite_successful_write(self) -> None:
        effects = []

        async def execute(_action, _token):
            effects.append("charged")
            return {"committed": True}

        def validate(_observation, _token):
            return ResultValidation.valid()

        def replan(_observation, _validation, _token):
            self.fail("a valid write must not be replanned")

        def broken_sink(event):
            if event.type == "closed_loop_action_succeeded":
                raise RuntimeError("UI unavailable")

        result = await ClosedLoopExecutor(
            execute,
            validate,
            replan,
            event_sink=broken_sink,
        ).execute(ClosedLoopAction("write", replay_policy="never", write=True))

        self.assertEqual(result.status, "completed")
        self.assertEqual(effects, ["charged"])
        self.assertEqual(len(result.event_sink_errors), 1)
        self.assertIn("Closed-loop event sink failed", result.event_sink_errors[0])
        self.assertIn("RuntimeError", result.event_sink_errors[0])
        self.assertNotIn("UI unavailable", result.event_sink_errors[0])


if __name__ == "__main__":
    unittest.main()
