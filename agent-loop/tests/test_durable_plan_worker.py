"""Distributed durable Plan worker regressions."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from pi_agent_loop import (
    CancellationToken,
    DurablePlanWorker,
    DurablePlanWorkflow,
    IntentPlanPolicy,
    JournalPrincipal,
    MultiIntentPlan,
    PlanStep,
    SQLiteSessionEventJournal,
    SessionJournalPlanStore,
    StaticJournalKeyProvider,
)
from pi_agent_loop.planning import PlanParameterContract


class DurablePlanWorkerTests(unittest.IsolatedAsyncioTestCase):
    def make_store(self, directory: str, session_id: str = "worker-session"):
        journal = SQLiteSessionEventJournal(
            Path(directory) / "worker.sqlite3",
            key_provider=StaticJournalKeyProvider(
                {"test": b"w" * 32}, active_key_id="test"
            ),
        )
        return SessionJournalPlanStore(
            journal,
            JournalPrincipal.system("tenant"),
            session_id=session_id,
        )

    def make_workflow(
        self,
        store: SessionJournalPlanStore,
        policies: dict[str, IntentPlanPolicy],
        executor,
        *,
        lease_seconds: float = 1,
    ) -> DurablePlanWorkflow:
        return DurablePlanWorkflow(
            store=store,
            planner=None,
            policies=policies,
            step_executor=executor,
            approval_barrier=None,
            lease_seconds=lease_seconds,
        )

    async def test_two_workers_compete_once_and_conflict_is_not_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policies = {"read": IntentPlanPolicy("read")}
            first_store = self.make_store(directory)
            await first_store.initialize(
                MultiIntentPlan(
                    "read",
                    (PlanStep("read", "read"),),
                    plan_id="contended-plan",
                )
            )
            entered = asyncio.Event()
            release = asyncio.Event()
            calls = 0

            async def slow(_step, _token, *, fencing_token=None):
                nonlocal calls
                self.assertIsInstance(fencing_token, int)
                calls += 1
                entered.set()
                await release.wait()
                return "only-once"

            first = DurablePlanWorker(
                self.make_workflow(first_store, policies, slow),
                worker_id="worker-a",
            )
            second_store = self.make_store(directory)
            second = DurablePlanWorker(
                self.make_workflow(second_store, policies, slow),
                worker_id="worker-b",
            )

            running = asyncio.create_task(first.run_once())
            await asyncio.wait_for(entered.wait(), timeout=2)
            lost = await second.run_once()
            self.assertEqual(lost.conflict_count, 1)
            self.assertEqual(lost.worker_error_count, 0)
            self.assertEqual(lost.items[0].status, "conflict")

            release.set()
            won = await asyncio.wait_for(running, timeout=2)
            self.assertEqual(won.items[0].status, "completed")
            self.assertEqual(calls, 1)

    async def test_cancelled_worker_leaves_recovery_anchor_for_new_fenced_worker(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policies = {"read": IntentPlanPolicy("read")}
            first_store = self.make_store(directory)
            await first_store.initialize(
                MultiIntentPlan(
                    "recover",
                    (PlanStep("read", "read"),),
                    plan_id="recoverable-plan",
                )
            )
            entered = asyncio.Event()
            fences: list[int] = []

            async def interrupted(_step, token, *, fencing_token):
                fences.append(fencing_token)
                entered.set()
                await token.wait()
                token.throw_if_cancelled()

            token = CancellationToken()
            first = DurablePlanWorker(
                self.make_workflow(first_store, policies, interrupted),
                worker_id="crashed-worker",
            )
            running = asyncio.create_task(first.run_once(cancellation=token))
            await asyncio.wait_for(entered.wait(), timeout=2)
            token.cancel("worker shutdown")
            interrupted_batch = await asyncio.wait_for(running, timeout=2)
            self.assertEqual(interrupted_batch.items[0].status, "cancelled")
            self.assertEqual(token.child_count, 0)
            interrupted_record = await first_store.load("recoverable-plan")
            self.assertEqual(interrupted_record.state.phase, "running")

            async def recovered(_step, _token, *, fencing_token):
                fences.append(fencing_token)
                return "recovered"

            successor_store = self.make_store(directory)
            successor = DurablePlanWorker(
                self.make_workflow(successor_store, policies, recovered),
                worker_id="successor-worker",
            )
            recovered_batch = await successor.run_once()
            self.assertEqual(recovered_batch.items[0].status, "completed")
            self.assertGreater(fences[1], fences[0])
            record = await successor_store.load("recoverable-plan")
            self.assertEqual(record.state.steps["read"].attempts, 2)
            self.assertEqual(
                [event.type for event in record.events],
                [
                    "step_started",
                    "step_recovered",
                    "step_started",
                    "step_succeeded",
                ],
            )

    async def test_unsafe_callback_and_terminal_plans_are_not_executed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policy = IntentPlanPolicy(
                "write",
                requires_approval=True,
                write=True,
                replay_policy="never",
                approval_roles=("approver",),
                parameter_contract=PlanParameterContract(allow_empty=True),
            )
            policies = {
                "read": IntentPlanPolicy("read"),
                "write": policy,
            }
            store = self.make_store(directory)
            await store.initialize(
                MultiIntentPlan(
                    "read",
                    (PlanStep("read", "read"),),
                    plan_id="completed-plan",
                )
            )
            completed_calls = 0

            async def complete_once(_step, _token):
                nonlocal completed_calls
                completed_calls += 1
                return "done"

            completed_workflow = self.make_workflow(
                store, policies, complete_once
            )
            completed = await completed_workflow.execute("completed-plan")
            self.assertEqual(completed.state.phase, "completed")
            self.assertEqual(completed_calls, 1)

            await store.initialize(
                MultiIntentPlan(
                    "write",
                    (
                        PlanStep(
                            "write",
                            "write",
                            requires_approval=True,
                            write=True,
                            replay_policy="never",
                            approval_roles=("approver",),
                            parameter_contract=PlanParameterContract(
                                allow_empty=True
                            ),
                        ),
                    ),
                    plan_id="approval-plan",
                )
            )
            calls = 0

            async def must_not_run(_step, _token):
                nonlocal calls
                calls += 1
                return "forbidden"

            worker = DurablePlanWorker(
                self.make_workflow(store, policies, must_not_run),
                worker_id="approval-worker",
            )
            first = await worker.run_once()
            self.assertEqual(
                first.items[0].status,
                "error",
                first,
            )
            self.assertEqual(calls, 0)
            self.assertEqual(
                [item.plan.plan_id for item in await store.list_runnable_plans()],
                ["approval-plan"],
            )

            second = await worker.run_once()
            self.assertEqual(second.discovered_plan_ids, ("approval-plan",))
            self.assertEqual(second.items[0].status, "error")
            self.assertEqual(calls, 0)
            self.assertEqual(completed_calls, 1)

    async def test_worker_enforces_plan_concurrency_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policies = {"read": IntentPlanPolicy("read")}
            store = self.make_store(directory)
            for number in range(5):
                await store.initialize(
                    MultiIntentPlan(
                        f"read-{number}",
                        (PlanStep("read", "read"),),
                        plan_id=f"plan-{number}",
                    )
                )
            active = maximum = 0
            two_entered = asyncio.Event()
            release = asyncio.Event()

            async def execute(_step, _token):
                nonlocal active, maximum
                active += 1
                maximum = max(maximum, active)
                if active == 2:
                    two_entered.set()
                try:
                    await release.wait()
                    return "ok"
                finally:
                    active -= 1

            worker = DurablePlanWorker(
                self.make_workflow(store, policies, execute),
                max_concurrent_plans=2,
                worker_id="bounded-worker",
            )
            running = asyncio.create_task(worker.run_once())
            await asyncio.wait_for(two_entered.wait(), timeout=2)
            release.set()
            batch = await asyncio.wait_for(running, timeout=10)
            self.assertEqual(batch.executed_count, 5)
            self.assertEqual(maximum, 2)

    async def test_one_plan_losing_lease_does_not_cancel_sibling_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policies = {"read": IntentPlanPolicy("read")}
            store = self.make_store(directory)
            for plan_id in ("lose-plan", "healthy-plan"):
                await store.initialize(
                    MultiIntentPlan(
                        plan_id,
                        (PlanStep("read", "read"),),
                        plan_id=plan_id,
                    )
                )

            original_renew = store.renew_execution

            async def selective_renew(lease, *, lease_seconds):
                if json.loads(lease.resource_id).get("entityId") == "lose-plan":
                    return False
                return await original_renew(lease, lease_seconds=lease_seconds)

            store.renew_execution = selective_renew  # type: ignore[method-assign]
            executed: list[str] = []

            async def execute(_step, _token):
                executed.append("healthy")
                return "ok"

            parent_token = CancellationToken()
            worker = DurablePlanWorker(
                self.make_workflow(store, policies, execute),
                max_concurrent_plans=2,
                worker_id="isolated-cancellation-worker",
            )
            batch = await worker.run_once(cancellation=parent_token)
            statuses = {item.plan_id: item.status for item in batch.items}
            self.assertEqual(statuses["lose-plan"], "lease_lost")
            self.assertEqual(statuses["healthy-plan"], "completed")
            self.assertFalse(parent_token.cancelled)
            self.assertEqual(parent_token.child_count, 0)
            self.assertEqual(executed, ["healthy"])

    async def test_bounded_batches_rotate_past_a_contended_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policies = {"read": IntentPlanPolicy("read")}
            store = self.make_store(directory)
            for plan_id in ("plan-0", "plan-1"):
                await store.initialize(
                    MultiIntentPlan(
                        plan_id,
                        (PlanStep("read", "read"),),
                        plan_id=plan_id,
                    )
                )
            blocker = await store.acquire_execution(
                "plan-0",
                "other-worker",
                lease_seconds=10,
            )
            self.assertIsNotNone(blocker)
            calls = 0

            async def execute(_step, _token):
                nonlocal calls
                calls += 1
                return "ok"

            worker = DurablePlanWorker(
                self.make_workflow(store, policies, execute),
                worker_id="fair-worker",
                max_batch_size=1,
            )
            first = await worker.run_once()
            self.assertEqual(first.items[0].plan_id, "plan-0")
            self.assertEqual(first.items[0].status, "conflict")

            second = await worker.run_once()
            self.assertEqual(second.items[0].plan_id, "plan-1")
            self.assertEqual(second.items[0].status, "completed")
            self.assertEqual(calls, 1)
            assert blocker is not None
            await store.release_execution_lease(blocker)

    async def test_serve_poll_wait_is_cancellable_without_leaked_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)

            async def should_not_run(_step, _token):
                raise AssertionError("empty queue must not execute")

            worker = DurablePlanWorker(
                self.make_workflow(
                    store,
                    {"read": IntentPlanPolicy("read")},
                    should_not_run,
                ),
                worker_id="service-worker",
                poll_interval_seconds=10,
            )
            token = CancellationToken()
            observed = asyncio.Event()

            def on_batch(_batch):
                observed.set()

            serving = asyncio.create_task(
                worker.serve(cancellation=token, on_batch=on_batch)
            )
            await asyncio.wait_for(observed.wait(), timeout=2)
            token.cancel("shutdown")
            result = await asyncio.wait_for(serving, timeout=2)
            self.assertTrue(result.cancelled)
            self.assertEqual(result.batches, 1)
            self.assertEqual(result.executed, 0)


if __name__ == "__main__":
    unittest.main()
