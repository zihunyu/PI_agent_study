"""P0 crash-window regressions for autonomous bootstrap and completion delivery."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pi_agent_loop import (
    AutonomousConversationProjector,
    AutonomousPlanCompletionProjector,
    AutonomousPlanRunner,
    AutonomousRunControllerLeaseLostError,
    ClosedLoopBudget,
    DurablePlanWorker,
    DurablePlanWorkflow,
    IntentPlanPolicy,
    JournalPrincipal,
    Model,
    MultiIntentPlan,
    PlanEvent,
    PlanResourceUsage,
    PlanStep,
    ResultValidation,
    SessionEventSpec,
    SessionJournalAutonomousRunStore,
    SessionJournalPlanStore,
    SQLiteSessionEventJournal,
    StaticJournalKeyProvider,
)
from pi_agent_loop.planning import PlanParameterContract
from pi_agent_loop.session.operation_state import replay_operation
from pi_agent_loop.session.journal_adapters import SessionJournalOperationEventStore


MODEL = Model(id="p0-autonomous", provider="test", api="scripted")


class P0AutonomousAtomicOutboxTests(unittest.IsolatedAsyncioTestCase):
    def make_components(self, directory: str, *, session_id: str = "session"):
        journal = SQLiteSessionEventJournal(
            Path(directory) / "state.sqlite3",
            key_provider=StaticJournalKeyProvider(
                {"test": b"z" * 32},
                active_key_id="test",
            ),
        )
        principal = JournalPrincipal.system("tenant")
        plan_store = SessionJournalPlanStore(
            journal,
            principal,
            session_id=session_id,
        )
        run_store = SessionJournalAutonomousRunStore(
            journal,
            principal,
            session_id=session_id,
        )
        projector = AutonomousConversationProjector(
            SessionJournalOperationEventStore(journal, principal),
            session_id=session_id,
            model=MODEL,
        )
        return journal, principal, plan_store, run_store, projector

    async def test_bootstrap中途异常会回滚plan_run_conversation全部事实(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal, principal, plan_store, run_store, projector = (
                self.make_components(directory)
            )
            plan = MultiIntentPlan(
                "read",
                (PlanStep("read", "read"),),
                plan_id="atomic-bootstrap-plan",
            )
            original = journal._insert_specs_sync

            def fail_after_two(connection, actor, specs):
                original(connection, actor, specs[:2])
                raise RuntimeError("fault after partial insert")

            journal._insert_specs_sync = fail_after_two  # type: ignore[method-assign]
            with self.assertRaisesRegex(RuntimeError, "partial insert"):
                await projector.bootstrap(
                    "read",
                    run_id="atomic-bootstrap-run",
                    plan=plan,
                    budget=ClosedLoopBudget(),
                    initial_messages=[],
                    run_store=run_store,
                    plan_store=plan_store,
                )
            journal._insert_specs_sync = original  # type: ignore[method-assign]

            rows = await journal.load_events(
                principal,
                session_id="session",
            )
            self.assertEqual(rows, [])
            self.assertEqual(await plan_store.list_runnable_plans(), ())
            self.assertIsNone(await projector.find("atomic-bootstrap-run"))

    async def test_bootstrap一次事务发布且同run并发begin只有稳定operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _journal, _principal, plan_store, run_store, projector = (
                self.make_components(directory)
            )
            plan = MultiIntentPlan(
                "read",
                (PlanStep("read", "read"),),
                plan_id="linked-plan",
            )
            link = await projector.bootstrap(
                "read",
                run_id="linked-run",
                plan=plan,
                budget=ClosedLoopBudget(),
                initial_messages=[],
                run_store=run_store,
                plan_store=plan_store,
            )
            durable = await run_store.load("linked-run")
            stored_plan = await plan_store.load("linked-plan")
            self.assertTrue(durable.linked)
            self.assertTrue(durable.dispatchable)
            self.assertEqual(durable.operation_id, link.operation_id)
            self.assertTrue(stored_plan.dispatchable)
            self.assertEqual(
                [record.plan.plan_id for record in await plan_store.list_runnable_plans()],
                ["linked-plan"],
            )
            await projector.finalize(
                link,
                response_text="linked",
                status="completed",
            )

            first, second = await asyncio.gather(
                projector.begin(
                    "read",
                    run_id="same-run",
                    plan_id="same-plan",
                    initial_messages=[],
                ),
                projector.begin(
                    "read",
                    run_id="same-run",
                    plan_id="same-plan",
                    initial_messages=[],
                ),
            )
            self.assertEqual(first, second)
            self.assertEqual((await projector.find("same-run")).operation_id, first.operation_id)  # type: ignore[union-attr]

    async def test_同一provisional_run并发绑定不同plan只有一个完整事务胜出(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal, _principal, plan_store, run_store, projector = (
                self.make_components(directory)
            )
            budget = ClosedLoopBudget()
            await run_store.open_admission(
                "read",
                budget,
                run_id="provisional-race-run",
            )
            await run_store.reserve_resources(
                "provisional-race-run",
                reservation_id="initial-plan-steps",
                stage="initial_plan",
                reserved=PlanResourceUsage(plan_steps=1),
            )
            plans = (
                MultiIntentPlan(
                    "read",
                    (PlanStep("read-a", "read"),),
                    plan_id="provisional-race-plan-a",
                ),
                MultiIntentPlan(
                    "read",
                    (PlanStep("read-b", "read"),),
                    plan_id="provisional-race-plan-b",
                ),
            )
            original_append = journal.append_events
            both_ready = asyncio.Event()
            arrived = 0
            arrival_lock = asyncio.Lock()

            async def barrier_append(principal, specs, **kwargs):
                nonlocal arrived
                if kwargs.get("expected_stream_sequences") is not None:
                    async with arrival_lock:
                        arrived += 1
                        if arrived == 2:
                            both_ready.set()
                    await asyncio.wait_for(both_ready.wait(), timeout=2)
                return await original_append(principal, specs, **kwargs)

            journal.append_events = barrier_append  # type: ignore[method-assign]
            outcomes = await asyncio.gather(
                *(
                    projector.bootstrap(
                        "read",
                        run_id="provisional-race-run",
                        plan=plan,
                        budget=budget,
                        initial_messages=[],
                        run_store=run_store,
                        plan_store=plan_store,
                    )
                    for plan in plans
                ),
                return_exceptions=True,
            )
            journal.append_events = original_append  # type: ignore[method-assign]

            successes = [item for item in outcomes if not isinstance(item, Exception)]
            failures = [item for item in outcomes if isinstance(item, Exception)]
            self.assertEqual(len(successes), 1)
            self.assertEqual(len(failures), 1)
            durable = await run_store.load("provisional-race-run")
            link = await projector.find("provisional-race-run")
            self.assertIsNotNone(link)
            assert link is not None
            self.assertEqual(durable.initial_plan_id, link.plan_id)
            self.assertEqual(durable.plan_ids, (link.plan_id,))
            self.assertTrue(durable.linked)
            self.assertTrue(durable.dispatchable)
            for plan in plans:
                if plan.plan_id == link.plan_id:
                    self.assertEqual(
                        (await plan_store.load(plan.plan_id)).plan.to_dict(),
                        plan.to_dict(),
                    )
                else:
                    with self.assertRaisesRegex(Exception, "Plan 不存在"):
                        await plan_store.load(plan.plan_id)

    async def test_multi_stream_cas漏掉任一batch_stream会整体拒绝(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal, principal, _plan_store, _run_store, _projector = (
                self.make_components(directory)
            )
            specs = [
                SessionEventSpec(
                    "retry",
                    "test_first",
                    "session",
                    {"value": 1},
                    operation_id="stream-a",
                ),
                SessionEventSpec(
                    "retry",
                    "test_second",
                    "session",
                    {"value": 2},
                    operation_id="stream-b",
                ),
            ]
            with self.assertRaisesRegex(ValueError, "精确覆盖"):
                await journal.append_events(
                    principal,
                    specs,
                    expected_stream_sequences={
                        ("retry", "session", "stream-a"): -1,
                    },
                )
            self.assertEqual(
                await journal.load_events(principal, session_id="session"),
                [],
            )

    async def test_stale_run_controller不能写预算纠正plan或终态(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _journal, _principal, plan_store, run_store, projector = (
                self.make_components(directory)
            )
            plan = MultiIntentPlan(
                "read",
                (PlanStep("read", "read"),),
                plan_id="controller-fencing-initial",
            )
            await projector.bootstrap(
                "read",
                run_id="controller-fencing-run",
                plan=plan,
                budget=ClosedLoopBudget(),
                initial_messages=[],
                run_store=run_store,
                plan_store=plan_store,
            )
            stale = await run_store.acquire_controller(
                "controller-fencing-run",
                "old-controller",
                lease_seconds=0.01,
            )
            self.assertIsNotNone(stale)
            assert stale is not None
            await asyncio.sleep(0.05)
            successor = await run_store.acquire_controller(
                "controller-fencing-run",
                "new-controller",
                lease_seconds=1,
            )
            self.assertIsNotNone(successor)
            assert successor is not None
            self.assertGreater(successor.generation, stale.generation)

            with self.assertRaises(AutonomousRunControllerLeaseLostError):
                await run_store.reserve_resources(
                    "controller-fencing-run",
                    reservation_id="stale-budget",
                    stage="stale",
                    reserved=PlanResourceUsage(step_attempts=1),
                    controller_lease=stale,
                    controller_lease_seconds=1,
                )
            correction = MultiIntentPlan(
                "read",
                (PlanStep("retry", "read"),),
                plan_id="controller-fencing-correction",
            )
            with self.assertRaises(AutonomousRunControllerLeaseLostError):
                await run_store.register_plan_atomic(
                    "controller-fencing-run",
                    correction,
                    plan_store,
                    controller_lease=stale,
                    controller_lease_seconds=1,
                )
            with self.assertRaises(AutonomousRunControllerLeaseLostError):
                await run_store.finalize(
                    "controller-fencing-run",
                    status="completed",
                    response_text="stale",
                    controller_lease=stale,
                    controller_lease_seconds=1,
                )

            durable = await run_store.load("controller-fencing-run")
            self.assertEqual(durable.plan_ids, (plan.plan_id,))
            self.assertEqual(durable.active_reservations, ())
            self.assertIsNone(durable.status)
            with self.assertRaisesRegex(Exception, "Plan 不存在"):
                await plan_store.load(correction.plan_id)
            await run_store.release_controller(
                "controller-fencing-run",
                successor,
            )

    async def test_controller_lease不能跨run预留结算或提交终态(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _journal, _principal, _plan_store, run_store, _projector = (
                self.make_components(directory)
            )
            budget = ClosedLoopBudget(max_step_attempts=5)
            await run_store.initialize(
                "run a",
                "plan-a",
                budget,
                run_id="run-a",
            )
            await run_store.initialize(
                "run b",
                "plan-b",
                budget,
                run_id="run-b",
            )
            await run_store.reserve_resources(
                "run-b",
                reservation_id="b-reservation",
                stage="test",
                reserved=PlanResourceUsage(step_attempts=1),
            )
            lease_a = await run_store.acquire_controller(
                "run-a",
                "controller-a",
                lease_seconds=30,
            )
            assert lease_a is not None
            with self.assertRaisesRegex(ValueError, "目标 Autonomous Run"):
                await run_store.reserve_resources(
                    "run-b",
                    reservation_id="cross-run",
                    stage="test",
                    reserved=PlanResourceUsage(step_attempts=1),
                    controller_lease=lease_a,
                    controller_lease_seconds=30,
                )
            with self.assertRaisesRegex(ValueError, "目标 Autonomous Run"):
                await run_store.settle_resources(
                    "run-b",
                    reservation_id="b-reservation",
                    actual=PlanResourceUsage(step_attempts=1),
                    controller_lease=lease_a,
                    controller_lease_seconds=30,
                )
            with self.assertRaisesRegex(ValueError, "目标 Autonomous Run"):
                await run_store.register_plan(
                    "run-b",
                    "cross-run-plan",
                    controller_lease=lease_a,
                    controller_lease_seconds=30,
                )
            with self.assertRaisesRegex(ValueError, "目标 Autonomous Run"):
                await run_store.finalize(
                    "run-b",
                    status="completed",
                    response_text="cross-run",
                    controller_lease=lease_a,
                    controller_lease_seconds=30,
                )
            run_b = await run_store.load("run-b")
            self.assertEqual(
                run_b.active_reservations,
                (("b-reservation", PlanResourceUsage(step_attempts=1)),),
            )
            self.assertIsNone(run_b.status)
            self.assertEqual(run_b.plan_ids, ("plan-b",))
            await run_store.finalize(
                "run-b",
                status="completed",
                response_text="legitimate",
            )
            with self.assertRaisesRegex(ValueError, "目标 Autonomous Run"):
                await run_store.finalize(
                    "run-b",
                    status="completed",
                    response_text="legitimate",
                    controller_lease=lease_a,
                    controller_lease_seconds=30,
                )
            self.assertEqual(
                (await run_store.load("run-b")).response_text,
                "legitimate",
            )
            await run_store.release_controller("run-a", lease_a)

    async def test_resume取controller后重载最新correction_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal, principal, plan_store, _run_store, projector = (
                self.make_components(directory)
            )
            initial = MultiIntentPlan(
                "read",
                (PlanStep("initial", "read"),),
                plan_id="stale-initial-plan",
            )
            correction = MultiIntentPlan(
                "read",
                (PlanStep("corrected", "read"),),
                plan_id="latest-correction-plan",
            )
            class InjectingRunStore(SessionJournalAutonomousRunStore):
                async def acquire_controller(
                    inner_self,
                    run_id,
                    owner_token,
                    *,
                    lease_seconds,
                ):
                    await inner_self.register_plan_atomic(
                        run_id,
                        correction,
                        plan_store,
                    )
                    return await super().acquire_controller(
                        run_id,
                        owner_token,
                        lease_seconds=lease_seconds,
                    )

            run_store = InjectingRunStore(
                journal,
                principal,
                session_id="session",
            )
            await projector.bootstrap(
                initial.request,
                run_id="snapshot-gap-run",
                plan=initial,
                budget=ClosedLoopBudget(),
                initial_messages=[],
                run_store=run_store,
                plan_store=plan_store,
            )
            workflow = DurablePlanWorkflow(
                store=plan_store,
                planner=None,
                policies={"read": IntentPlanPolicy("read")},
                step_executor=lambda *_args: None,
                approval_barrier=None,
            )
            observed: list[str] = []

            class CapturingRunner(AutonomousPlanRunner):
                async def _execute_closed_loop_owned(
                    inner_self,
                    _request,
                    selected_plan,
                    _token,
                    **_kwargs,
                ):
                    observed.append(selected_plan.plan_id)
                    return selected_plan.plan_id

            runner = CapturingRunner(workflow, run_store=run_store)
            selected = await runner.resume(initial.plan_id)

            self.assertEqual(selected, correction.plan_id)
            self.assertEqual(observed, [correction.plan_id])

    async def test_correction_plan与run注册中途异常全部回滚(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal, _principal, plan_store, run_store, projector = (
                self.make_components(directory)
            )
            initial = MultiIntentPlan(
                "read",
                (PlanStep("read", "read"),),
                plan_id="initial-plan",
            )
            await projector.bootstrap(
                "read",
                run_id="correction-run",
                plan=initial,
                budget=ClosedLoopBudget(),
                initial_messages=[],
                run_store=run_store,
                plan_store=plan_store,
            )
            corrected = MultiIntentPlan(
                "read",
                (PlanStep("retry", "read"),),
                plan_id="corrected-plan",
            )
            original = journal._insert_specs_sync

            def fail_after_plan(connection, actor, specs):
                original(connection, actor, specs[:1])
                raise RuntimeError("fault between plan and run")

            journal._insert_specs_sync = fail_after_plan  # type: ignore[method-assign]
            with self.assertRaisesRegex(RuntimeError, "between plan and run"):
                await run_store.register_plan_atomic(
                    "correction-run",
                    corrected,
                    plan_store,
                )
            journal._insert_specs_sync = original  # type: ignore[method-assign]
            self.assertEqual(
                (await run_store.load("correction-run")).plan_ids,
                ("initial-plan",),
            )
            with self.assertRaisesRegex(Exception, "Plan 不存在"):
                await plan_store.load("corrected-plan")

            await run_store.register_plan_atomic(
                "correction-run",
                corrected,
                plan_store,
            )
            self.assertEqual(
                (await run_store.load("correction-run")).latest_plan_id,
                "corrected-plan",
            )
            self.assertTrue((await plan_store.load("corrected-plan")).dispatchable)

    async def test_terminal与completion_pending同事务且新worker可重试ack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _journal, _principal, store, _run_store, _projector = (
                self.make_components(directory)
            )
            policy = IntentPlanPolicy("read")
            plan = MultiIntentPlan(
                "read",
                (PlanStep("read", "read"),),
                plan_id="completion-plan",
            )
            await store.initialize(plan)

            async def execute(_step, _token, *, fencing_token=None):
                self.assertIsInstance(fencing_token, int)
                return "ok"

            workflow = DurablePlanWorkflow(
                store=store,
                planner=None,
                policies={"read": policy},
                step_executor=execute,
                approval_barrier=None,
                lease_seconds=1,
            )
            first_calls = 0

            async def fail_completion(_result, _token, _envelope):
                nonlocal first_calls
                first_calls += 1
                raise RuntimeError("projection unavailable")

            failed = await DurablePlanWorker(
                workflow,
                worker_id="first-worker",
                completion_handler=fail_completion,
            ).run_once()
            self.assertEqual(failed.items[0].status, "completion_error")
            self.assertEqual(first_calls, 1)
            terminal = await store.load(plan.plan_id)
            self.assertEqual(terminal.state.phase, "completed")
            self.assertTrue(terminal.completion_pending)

            # Re-open every adapter to model a replacement process.
            _journal2, _principal2, reopened, _run2, _projector2 = (
                self.make_components(directory)
            )
            successor_calls = 0

            async def complete(_result, _token, _envelope):
                nonlocal successor_calls
                successor_calls += 1
                return "projected"

            successor_workflow = DurablePlanWorkflow(
                store=reopened,
                planner=None,
                policies={"read": policy},
                step_executor=execute,
                approval_barrier=None,
                lease_seconds=1,
            )
            succeeded = await DurablePlanWorker(
                successor_workflow,
                worker_id="successor-worker",
                completion_handler=complete,
            ).run_once()
            self.assertEqual(succeeded.items[0].status, "completed")
            self.assertEqual(succeeded.items[0].completion_status, "projected")
            self.assertEqual(successor_calls, 1)
            self.assertFalse((await reopened.load(plan.plan_id)).completion_pending)

            empty = await DurablePlanWorker(
                successor_workflow,
                worker_id="third-worker",
                completion_handler=complete,
            ).run_once()
            self.assertEqual(empty.items, ())
            self.assertEqual(successor_calls, 1)

    async def test_durable_outbox拒绝不接收envelope的两参数handler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _journal, _principal, store, _run_store, _projector = (
                self.make_components(directory)
            )
            plan = MultiIntentPlan(
                "read",
                (PlanStep("read", "read"),),
                plan_id="handler-contract-plan",
            )
            await store.initialize(plan)
            calls = 0

            async def execute(_step, _token, *, fencing_token=None):
                return "ok"

            async def unsafe_two_argument_handler(_result, _token):
                nonlocal calls
                calls += 1
                return "projected"

            workflow = DurablePlanWorkflow(
                store=store,
                planner=None,
                policies={"read": IntentPlanPolicy("read")},
                step_executor=execute,
                approval_barrier=None,
            )
            with self.assertRaisesRegex(TypeError, "必须接收.*envelope"):
                DurablePlanWorker(
                    workflow,
                    worker_id="unsafe-handler-worker",
                    completion_handler=unsafe_two_argument_handler,
                )
            self.assertEqual(calls, 0)
            self.assertEqual((await store.load(plan.plan_id)).state.phase, "pending")

    async def test_worker绝不执行尚未linked发布的prepared_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _journal, _principal, store, _run_store, _projector = (
                self.make_components(directory)
            )
            plan = MultiIntentPlan(
                "read",
                (PlanStep("read", "read"),),
                plan_id="prepared-only-plan",
            )
            prepared = await store.initialize(plan, dispatchable=False)
            self.assertFalse(prepared.dispatchable)
            calls = 0

            async def execute(_step, _token, *, fencing_token=None):
                nonlocal calls
                calls += 1
                return "ok"

            workflow = DurablePlanWorkflow(
                store=store,
                planner=None,
                policies={"read": IntentPlanPolicy("read")},
                step_executor=execute,
                approval_barrier=None,
            )
            before_link = await DurablePlanWorker(
                workflow,
                worker_id="prepared-worker",
            ).run_once()
            self.assertEqual(before_link.items, ())
            self.assertEqual(calls, 0)

            await store.mark_dispatchable(plan.plan_id)
            after_link = await DurablePlanWorker(
                workflow,
                worker_id="linked-worker",
            ).run_once()
            self.assertEqual(after_link.items[0].status, "completed")
            self.assertEqual(calls, 1)

    async def test_completion_claim_fencing阻止两个worker同时投影(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _journal, _principal, store, _run_store, _projector = (
                self.make_components(directory)
            )
            plan = MultiIntentPlan(
                "read",
                (PlanStep("read", "read"),),
                plan_id="completion-fenced-plan",
            )
            await store.initialize(plan)

            async def execute(_step, _token, *, fencing_token=None):
                return "ok"

            policies = {"read": IntentPlanPolicy("read")}
            workflow = DurablePlanWorkflow(
                store=store,
                planner=None,
                policies=policies,
                step_executor=execute,
                approval_barrier=None,
                lease_seconds=1,
            )
            # Execute without a completion consumer. The durable pending row is
            # intentionally left for two replacement workers to contend over.
            executed = await DurablePlanWorker(
                workflow,
                worker_id="execution-only",
            ).run_once()
            self.assertEqual(executed.items[0].status, "completed")
            self.assertTrue((await store.load(plan.plan_id)).completion_pending)

            _j2, _p2, store_a, _r2, _c2 = self.make_components(directory)
            _j3, _p3, store_b, _r3, _c3 = self.make_components(directory)
            workflow_a = DurablePlanWorkflow(
                store=store_a,
                planner=None,
                policies=policies,
                step_executor=execute,
                approval_barrier=None,
                lease_seconds=1,
            )
            workflow_b = DurablePlanWorkflow(
                store=store_b,
                planner=None,
                policies=policies,
                step_executor=execute,
                approval_barrier=None,
                lease_seconds=1,
            )
            entered = asyncio.Event()
            release = asyncio.Event()
            calls = 0

            async def project(_result, _token, _envelope):
                nonlocal calls
                calls += 1
                entered.set()
                await release.wait()
                return "projected"

            worker_a = DurablePlanWorker(
                workflow_a,
                worker_id="completion-a",
                completion_handler=project,
            )
            worker_b = DurablePlanWorker(
                workflow_b,
                worker_id="completion-b",
                completion_handler=project,
            )
            active = asyncio.create_task(worker_a.run_once())
            await asyncio.wait_for(entered.wait(), timeout=2)
            contended = await worker_b.run_once()
            self.assertEqual(contended.items[0].status, "conflict")
            self.assertEqual(calls, 1)
            release.set()
            completed = await asyncio.wait_for(active, timeout=2)
            self.assertEqual(completed.items[0].status, "completed")
            self.assertEqual(calls, 1)
            self.assertFalse((await store.load(plan.plan_id)).completion_pending)

    async def test_同run两个pending并发worker只有一个controller且最终全部ack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _journal, _principal, store, run_store, projector = (
                self.make_components(directory)
            )
            budget = ClosedLoopBudget()
            initial = MultiIntentPlan(
                "read",
                (PlanStep("primary", "read"),),
                plan_id="multi-pending-initial",
            )
            corrected = MultiIntentPlan(
                "read",
                (PlanStep("replica", "read"),),
                plan_id="multi-pending-corrected",
            )
            await run_store.open_admission(
                "read",
                budget,
                run_id="multi-pending-run",
            )
            await run_store.reserve_resources(
                "multi-pending-run",
                reservation_id="initial-plan-steps",
                stage="initial_plan",
                reserved=PlanResourceUsage(plan_steps=1),
            )
            link = await projector.bootstrap(
                "read",
                run_id="multi-pending-run",
                plan=initial,
                budget=budget,
                initial_messages=[],
                run_store=run_store,
                plan_store=store,
            )
            await run_store.reserve_resources(
                "multi-pending-run",
                reservation_id="correction-plan-steps",
                stage="correction_plan",
                reserved=PlanResourceUsage(plan_steps=1),
            )
            await run_store.register_plan_atomic(
                "multi-pending-run",
                corrected,
                store,
            )

            async def execute(step, _token, *, fencing_token=None):
                if step.step_id == "primary":
                    raise RuntimeError("primary unavailable")
                return {"source": "replica"}

            policy = IntentPlanPolicy("read")
            workflow = DurablePlanWorkflow(
                store=store,
                planner=None,
                policies={"read": policy},
                step_executor=execute,
                approval_barrier=None,
                lease_seconds=1,
            )
            self.assertEqual(
                (await workflow.execute(initial.plan_id)).state.phase,
                "failed",
            )
            self.assertEqual(
                (await workflow.execute(corrected.plan_id)).state.phase,
                "completed",
            )
            self.assertTrue((await store.load(initial.plan_id)).completion_pending)
            self.assertTrue((await store.load(corrected.plan_id)).completion_pending)

            validator_entered = asyncio.Event()
            release_validator = asyncio.Event()
            validator_calls = 0

            async def validate(_observation, _token):
                nonlocal validator_calls
                validator_calls += 1
                validator_entered.set()
                await release_validator.wait()
                return ResultValidation.valid()

            runner_a = AutonomousPlanRunner(
                workflow,
                result_validator=validate,
                budget=budget,
                run_store=run_store,
            )
            runner_b = AutonomousPlanRunner(
                workflow,
                result_validator=validate,
                budget=budget,
                run_store=run_store,
            )
            worker_a = DurablePlanWorker(
                workflow,
                worker_id="multi-pending-worker-a",
                max_batch_size=1,
                completion_handler=AutonomousPlanCompletionProjector(
                    runner_a,
                    projector,
                ),
            )
            worker_b = DurablePlanWorker(
                workflow,
                worker_id="multi-pending-worker-b",
                max_batch_size=1,
                completion_handler=AutonomousPlanCompletionProjector(
                    runner_b,
                    projector,
                ),
            )
            worker_b._scan_offset = 1
            first = asyncio.create_task(worker_a.run_once())
            try:
                # Coverage instrumentation and Windows SQLite thread hand-offs
                # can legitimately exceed the old two-second scheduling window.
                await asyncio.wait_for(validator_entered.wait(), timeout=10)
                contended = await worker_b.run_once()
                self.assertEqual(contended.items[0].status, "completion_error")
                self.assertEqual(validator_calls, 1)
                release_validator.set()
                completed = await asyncio.wait_for(first, timeout=10)
                self.assertEqual(completed.items[0].completion_status, "completed")
                self.assertEqual(validator_calls, 1)
                self.assertFalse(
                    (await store.load(initial.plan_id)).completion_pending
                )
                self.assertFalse(
                    (await store.load(corrected.plan_id)).completion_pending
                )
            finally:
                release_validator.set()
                if not first.done():
                    first.cancel()
                await asyncio.gather(first, return_exceptions=True)

            events = await projector.store.load(
                session_id="session",
                operation_id=link.operation_id,
            )
            state = replay_operation(events)
            final_assistants = [
                message
                for message in state.messages
                if message.get("role") == "assistant"
                and message.get("autonomousRun", {}).get("projection") == "final"
            ]
            self.assertEqual(len(final_assistants), 1)

    async def test_terminal_event写入后pending失败会整体回滚(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal, _principal, store, _run_store, _projector = (
                self.make_components(directory)
            )
            plan = MultiIntentPlan(
                "read",
                (PlanStep("read", "read"),),
                plan_id="terminal-atomic-plan",
            )
            record = await store.initialize(plan)
            running = await store.append_event(
                plan.plan_id,
                PlanEvent(1, "step_started", "read", {}),
                expected_last_sequence=record.last_journal_sequence,
            )
            original = journal._insert_specs_sync

            def fail_after_terminal(connection, actor, specs):
                self.assertEqual(len(specs), 2)
                original(connection, actor, specs[:1])
                raise RuntimeError("pending insert failed")

            journal._insert_specs_sync = fail_after_terminal  # type: ignore[method-assign]
            with self.assertRaisesRegex(RuntimeError, "pending insert failed"):
                await store.append_event(
                    plan.plan_id,
                    PlanEvent(2, "step_succeeded", "read", {"result": "ok"}),
                    expected_last_sequence=running.last_journal_sequence,
                )
            journal._insert_specs_sync = original  # type: ignore[method-assign]
            restored = await store.load(plan.plan_id)
            self.assertEqual(restored.state.phase, "running")
            self.assertFalse(restored.completion_pending)

    async def test_projection成功但completion_ack失败或响应丢失仍只有一个final助手(self) -> None:
        for response_lost_after_commit in (False, True):
            with self.subTest(response_lost_after_commit=response_lost_after_commit):
                with tempfile.TemporaryDirectory() as directory:
                    _journal, _principal, store, run_store, projector = (
                        self.make_components(directory)
                    )
                    plan = MultiIntentPlan(
                        "read",
                        (PlanStep("read", "read"),),
                        plan_id="ack-window-plan",
                    )
                    link = await projector.bootstrap(
                        "read",
                        run_id="ack-window-run",
                        plan=plan,
                        budget=ClosedLoopBudget(),
                        initial_messages=[],
                        run_store=run_store,
                        plan_store=store,
                    )

                    async def execute(_step, _token, *, fencing_token=None):
                        return "ok"

                    workflow = DurablePlanWorkflow(
                        store=store,
                        planner=None,
                        policies={"read": IntentPlanPolicy("read")},
                        step_executor=execute,
                        approval_barrier=None,
                        lease_seconds=1,
                    )

                    async def project(result, _token, _envelope):
                        await projector.finalize(
                            link,
                            response_text="done",
                            status=result.state.phase,
                        )
                        return "projected"

                    original_ack = store.ack_completion

                    async def uncertain_ack(*args, **kwargs):
                        if response_lost_after_commit:
                            await original_ack(*args, **kwargs)
                        raise RuntimeError("ack response unavailable")

                    store.ack_completion = uncertain_ack  # type: ignore[method-assign]
                    first = await DurablePlanWorker(
                        workflow,
                        worker_id="uncertain-ack-worker",
                        completion_handler=project,
                    ).run_once()
                    self.assertEqual(first.items[0].status, "completion_error")
                    store.ack_completion = original_ack  # type: ignore[method-assign]

                    _j2, _p2, reopened, _r2, reopened_projector = (
                        self.make_components(directory)
                    )
                    reopened_workflow = DurablePlanWorkflow(
                        store=reopened,
                        planner=None,
                        policies={"read": IntentPlanPolicy("read")},
                        step_executor=execute,
                        approval_barrier=None,
                        lease_seconds=1,
                    )

                    async def retry_projection(result, _token, _envelope):
                        reopened_link = await reopened_projector.find("ack-window-run")
                        assert reopened_link is not None
                        await reopened_projector.finalize(
                            reopened_link,
                            response_text="done",
                            status=result.state.phase,
                        )
                        return "projected"

                    retried = await DurablePlanWorker(
                        reopened_workflow,
                        worker_id="ack-retry-worker",
                        completion_handler=retry_projection,
                    ).run_once()
                    if response_lost_after_commit:
                        self.assertEqual(retried.items, ())
                    else:
                        self.assertEqual(retried.items[0].status, "completed")
                    events = await reopened_projector.store.load(
                        session_id="session",
                        operation_id=link.operation_id,
                    )
                    state = replay_operation(events)
                    final_assistants = [
                        message
                        for message in state.messages
                        if message.get("role") == "assistant"
                        and message.get("autonomousRun", {}).get("projection")
                        == "final"
                    ]
                    self.assertEqual(len(final_assistants), 1)
                    self.assertFalse(
                        (await reopened.load(plan.plan_id)).completion_pending
                    )

    async def test_waiting_completion只有durable_status_projection成功后才能ack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _journal, _principal, store, run_store, projector = (
                self.make_components(directory)
            )
            contract = PlanParameterContract(allow_empty=True)
            step = PlanStep(
                "approve",
                "write",
                requires_approval=True,
                write=True,
                replay_policy="never",
                approval_roles=("approver",),
                parameter_contract=contract,
            )
            plan = MultiIntentPlan(
                "approve",
                (step,),
                plan_id="waiting-outbox-plan",
            )
            link = await projector.bootstrap(
                "approve",
                run_id="waiting-outbox-run",
                plan=plan,
                budget=ClosedLoopBudget(),
                initial_messages=[],
                run_store=run_store,
                plan_store=store,
            )
            initialized = await store.load(plan.plan_id)
            waiting = await store.append_event(
                plan.plan_id,
                PlanEvent(
                    1,
                    "step_waiting_approval",
                    step.step_id,
                    {
                        "approvalId": "approval-1",
                        "actionHash": step.action_hash,
                    },
                ),
                expected_last_sequence=initialized.last_journal_sequence,
            )
            self.assertTrue(waiting.completion_pending)

            policy = IntentPlanPolicy(
                "write",
                requires_approval=True,
                write=True,
                replay_policy="never",
                approval_roles=("approver",),
                parameter_contract=contract,
            )
            workflow = DurablePlanWorkflow(
                store=store,
                planner=None,
                policies={"write": policy},
                step_executor=lambda *_args, **_kwargs: None,
                approval_barrier=None,
                lease_seconds=1,
            )

            class WaitingRunner:
                def __init__(self):
                    self.workflow = workflow

                async def resume(self, _plan_id, *, cancellation=None):
                    return SimpleNamespace(
                        closed_loop_run_id="waiting-outbox-run",
                        status="waiting_approval",
                        plan_id=plan.plan_id,
                        response_text="等待审批",
                    )

                async def acknowledge_run_completions(
                    self,
                    _run_id,
                    *,
                    exclude_plan_id=None,
                ):
                    return True

            completion = AutonomousPlanCompletionProjector(  # type: ignore[arg-type]
                WaitingRunner(),
                projector,
            )
            original_project = projector.project_waiting

            async def fail_projection(*_args, **_kwargs):
                raise RuntimeError("notification store unavailable")

            projector.project_waiting = fail_projection  # type: ignore[method-assign]
            failed = await DurablePlanWorker(
                workflow,
                worker_id="waiting-projection-fails",
                completion_handler=completion,
            ).run_once()
            self.assertEqual(failed.items[0].status, "completion_error")
            self.assertTrue((await store.load(plan.plan_id)).completion_pending)

            projector.project_waiting = original_project  # type: ignore[method-assign]
            succeeded = await DurablePlanWorker(
                workflow,
                worker_id="waiting-projection-retry",
                completion_handler=completion,
            ).run_once()
            self.assertEqual(succeeded.items[0].status, "waiting_approval")
            self.assertFalse((await store.load(plan.plan_id)).completion_pending)
            events = await projector.store.load(
                session_id="session",
                operation_id=link.operation_id,
            )
            state = replay_operation(events)
            status_messages = [
                message
                for message in state.messages
                if message.get("role") == "assistant"
                and message.get("autonomousRun", {}).get("projection") == "status"
            ]
            self.assertEqual(len(status_messages), 1)
            self.assertEqual(state.phase, "running")

    async def test_waiting投递进行中转terminal会生成新envelope且旧ack不能清除(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _journal, _principal, store, _run_store, _projector = (
                self.make_components(directory)
            )
            contract = PlanParameterContract(allow_empty=True)
            step = PlanStep(
                "approve",
                "write",
                requires_approval=True,
                write=True,
                replay_policy="never",
                approval_roles=("approver",),
                parameter_contract=contract,
            )
            plan = MultiIntentPlan(
                "approve",
                (step,),
                plan_id="completion-generation-race-plan",
            )
            initialized = await store.initialize(plan)
            waiting = await store.append_event(
                plan.plan_id,
                PlanEvent(
                    1,
                    "step_waiting_approval",
                    step.step_id,
                    {
                        "approvalId": "approval-generation-1",
                        "actionHash": step.action_hash,
                    },
                ),
                expected_last_sequence=initialized.last_journal_sequence,
            )
            first_envelope = waiting.completion_envelope
            self.assertIsNotNone(first_envelope)
            assert first_envelope is not None

            policy = IntentPlanPolicy(
                "write",
                requires_approval=True,
                write=True,
                replay_policy="never",
                approval_roles=("approver",),
                parameter_contract=contract,
            )
            workflow = DurablePlanWorkflow(
                store=store,
                planner=None,
                policies={"write": policy},
                step_executor=lambda *_args, **_kwargs: None,
                approval_barrier=None,
                lease_seconds=1,
            )
            entered = asyncio.Event()
            release = asyncio.Event()
            delivered = []

            async def blocked_projection(_result, _token, envelope):
                delivered.append(envelope)
                entered.set()
                await release.wait()
                return "waiting-projected"

            old_delivery = asyncio.create_task(
                DurablePlanWorker(
                    workflow,
                    worker_id="generation-one-worker",
                    completion_handler=blocked_projection,
                ).run_once()
            )
            await asyncio.wait_for(entered.wait(), timeout=2)
            terminal = await store.append_event(
                plan.plan_id,
                PlanEvent(
                    2,
                    "step_approval_denied",
                    step.step_id,
                    {
                        "approvalId": "approval-generation-1",
                        "actionHash": step.action_hash,
                        "reason": "denied",
                    },
                ),
                expected_last_sequence=(await store.load(plan.plan_id)).last_journal_sequence,
            )
            second_envelope = terminal.completion_envelope
            self.assertIsNotNone(second_envelope)
            assert second_envelope is not None
            self.assertEqual(second_envelope.generation, first_envelope.generation + 1)
            self.assertEqual(second_envelope.target_phase, "failed")
            self.assertNotEqual(second_envelope.delivery_id, first_envelope.delivery_id)

            release.set()
            stale = await asyncio.wait_for(old_delivery, timeout=2)
            self.assertEqual(stale.items[0].status, "completion_error")
            preserved = await store.load(plan.plan_id)
            self.assertTrue(preserved.completion_pending)
            self.assertEqual(preserved.completion_envelope, second_envelope)
            self.assertEqual(delivered, [first_envelope])

            successor_envelopes = []

            async def project_terminal(_result, _token, envelope):
                successor_envelopes.append(envelope)
                return "terminal-projected"

            successor = await DurablePlanWorker(
                workflow,
                worker_id="generation-two-worker",
                completion_handler=project_terminal,
            ).run_once()
            self.assertEqual(successor.items[0].status, "failed")
            self.assertEqual(successor_envelopes, [second_envelope])
            self.assertFalse((await store.load(plan.plan_id)).completion_pending)


if __name__ == "__main__":
    unittest.main()
