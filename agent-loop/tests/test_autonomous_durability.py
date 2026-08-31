"""Crash/recovery contracts for autonomous run and conversation durability."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pi_agent_loop import (
    AutonomousConversationProjector,
    AutonomousRunStoreError,
    CancellationToken,
    ClosedLoopBudget,
    ClosedLoopEvent,
    InMemoryOperationEventStore,
    JournalPrincipal,
    Model,
    MultiIntentPlan,
    PlanExecutionResult,
    PlanExecutionState,
    PlanStep,
    PlanStepState,
    SessionContextProjection,
    SessionJournalAutonomousRunStore,
    SQLiteSessionEventJournal,
    StaticJournalKeyProvider,
    StartupRecoveryCoordinator,
    SynthesizedPlanResult,
)
from pi_agent_loop.harness import DurablePlanWorker


MODEL = Model(id="durability-model", provider="test", api="scripted")


def _text(message: dict) -> str:
    return "".join(
        str(part.get("text", ""))
        for part in message.get("content", [])
        if isinstance(part, dict) and part.get("type") == "text"
    )


class AutonomousDurabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_and_plan_linkage_exist_before_execution_and_final_is_idempotent(
        self,
    ) -> None:
        store = InMemoryOperationEventStore()
        projector = AutonomousConversationProjector(
            store,
            session_id="session",
            model=MODEL,
        )

        link = await projector.begin(
            "先查询再处理",
            run_id="run-1",
            plan_id="plan-1",
            initial_messages=[],
        )
        before_execution = await SessionContextProjection(store).project("session")
        self.assertEqual([item["role"] for item in before_execution.messages], ["user"])
        self.assertEqual(_text(before_execution.messages[0]), "先查询再处理")
        self.assertEqual(
            before_execution.messages[0]["autonomousRun"],
            {"runId": "run-1", "planId": "plan-1"},
        )
        self.assertEqual(await projector.find_unfinished(), link)

        same = await projector.begin(
            "先查询再处理",
            run_id="run-1",
            plan_id="plan-1",
            initial_messages=[],
        )
        self.assertEqual(same, link)
        first = await projector.finalize(
            link,
            response_text="处理完成",
            status="completed",
        )
        second = await projector.finalize(
            link,
            response_text="处理完成",
            status="completed",
        )
        self.assertEqual(first, second)
        final = await SessionContextProjection(store).project("session")
        self.assertEqual([item["role"] for item in final.messages], ["user", "assistant"])
        self.assertEqual(_text(final.messages[-1]), "处理完成")
        self.assertIsNone(await projector.find_unfinished())

    async def test_closed_loop_budget_survives_store_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            principal = JournalPrincipal.system("tenant")

            def make_store() -> SessionJournalAutonomousRunStore:
                journal = SQLiteSessionEventJournal(
                    path,
                    key_provider=StaticJournalKeyProvider(
                        {"test": b"a" * 32},
                        active_key_id="test",
                    ),
                )
                return SessionJournalAutonomousRunStore(
                    journal,
                    principal,
                    session_id="session",
                )

            store = make_store()
            await store.initialize(
                "读取并纠正",
                "plan-1",
                ClosedLoopBudget(
                    max_correction_rounds=2,
                    max_correction_actions=3,
                ),
                run_id="run-1",
            )
            await store.register_plan("run-1", "plan-2")
            await store.append_closed_loop_event(
                "run-1",
                "segment-1",
                ClosedLoopEvent(
                    1,
                    "closed_loop_correction_planned",
                    1,
                    data={"actionIds": ["plan:plan-2"]},
                ),
            )
            await store.append_closed_loop_event(
                "run-1",
                "segment-1",
                ClosedLoopEvent(
                    2,
                    "closed_loop_action_started",
                    1,
                    action_id="plan:plan-2",
                ),
            )

            reopened = make_store()
            record = await reopened.load("run-1")
            self.assertEqual(record.plan_ids, ("plan-1", "plan-2"))
            self.assertEqual(record.correction_rounds_spent, 1)
            self.assertEqual(record.correction_actions_spent, 1)
            self.assertEqual(record.remaining_budget, ClosedLoopBudget(1, 2))
            self.assertEqual(
                (await reopened.find_by_plan_id("plan-2")).run_id,  # type: ignore[union-attr]
                "run-1",
            )

            await reopened.finalize(
                "run-1",
                status="completed",
                response_text="done",
            )
            await reopened.finalize(
                "run-1",
                status="completed",
                response_text="done",
            )
            with self.assertRaises(AutonomousRunStoreError):
                await reopened.finalize(
                    "run-1",
                    status="failed",
                    response_text="different",
                )

    async def test_startup_recovery_uses_autonomous_callback_instead_of_finishing_empty_turn(
        self,
    ) -> None:
        store = InMemoryOperationEventStore()
        projector = AutonomousConversationProjector(
            store,
            session_id="session",
            model=MODEL,
        )
        link = await projector.begin(
            "崩溃前已经接收的问题",
            run_id="run-crash",
            plan_id="plan-crash",
            initial_messages=[],
        )
        calls: list[tuple[str, str]] = []

        async def resume(plan_id: str, run_id: str) -> str:
            calls.append((plan_id, run_id))
            await projector.finalize(
                link,
                response_text="恢复后完成",
                status="completed",
            )
            return "completed"

        coordinator = StartupRecoveryCoordinator(
            store,
            object(),  # generic recovery callbacks are not used for this stream
            session_id="session",
            autonomous_resume=resume,
        )
        inspection = await coordinator.inspect_all()
        self.assertEqual(inspection.auto_recoverable, (link.operation_id,))
        result = await coordinator.recover_all()
        self.assertEqual(result.completed, (link.operation_id,))
        self.assertEqual(calls, [("plan-crash", "run-crash")])
        projected = await SessionContextProjection(store).project("session")
        self.assertEqual(
            [_text(message) for message in projected.messages],
            ["崩溃前已经接收的问题", "恢复后完成"],
        )

    async def test_worker_completion_callback_is_structured_and_not_silently_dropped(
        self,
    ) -> None:
        plan = MultiIntentPlan(
            "read",
            (PlanStep("read", "read"),),
            plan_id="worker-plan",
        )
        pending = PlanExecutionState(
            plan.plan_id,
            {"read": PlanStepState("pending")},
        )
        completed = PlanExecutionState(
            plan.plan_id,
            {"read": PlanStepState("succeeded", attempts=1, result="ok")},
            version=1,
        )
        execution = PlanExecutionResult(
            completed,
            SynthesizedPlanResult(
                plan.plan_id,
                "completed",
                (("read", "ok"),),
                (),
                (),
            ),
            (),
        )

        class Store:
            def __init__(self) -> None:
                self.done = False

            async def list_runnable_plans(self):
                return () if self.done else (SimpleNamespace(plan=plan, state=pending),)

            async def load(self, _plan_id):
                state = completed if self.done else pending
                return SimpleNamespace(plan=plan, state=state)

        fake_store = Store()

        class Workflow:
            store = fake_store

            async def execute(self, _plan_id, *, cancellation=None):
                if cancellation is not None:
                    cancellation.throw_if_cancelled()
                fake_store.done = True
                return execution

        callbacks: list[str] = []

        async def complete(
            result: PlanExecutionResult,
            token: CancellationToken,
        ) -> str:
            token.throw_if_cancelled()
            callbacks.append(result.state.plan_id)
            return "completed_and_projected"

        batch = await DurablePlanWorker(
            Workflow(),  # type: ignore[arg-type]
            worker_id="worker",
            completion_handler=complete,
        ).run_once()
        self.assertEqual(batch.items[0].status, "completed")
        self.assertEqual(
            batch.items[0].completion_status,
            "completed_and_projected",
        )
        self.assertEqual(callbacks, ["worker-plan"])


if __name__ == "__main__":
    unittest.main()
