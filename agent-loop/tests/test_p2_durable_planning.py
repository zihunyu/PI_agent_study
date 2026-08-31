"""Unified Journal and DurableAgentHost integration for Multi-Intent plans."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from pi_agent_loop import (
    AgentTool,
    AgentToolResult,
    DurableAgentHost,
    DurablePlanApprovalAdapter,
    DurablePlanWorkflow,
    HybridRequestPlanner,
    IdentityClaim,
    IntentPlanPolicy,
    Model,
    MultiIntentPlan,
    PlanEvent,
    PlanExecutionConflictError,
    PlanExecutionError,
    PlanExecutionLeaseLostError,
    PlanApprovalReceipt,
    PlanStep,
    PlanWriteMetadata,
    PlanStoreConflictError,
    ScriptedProvider,
    SessionAlreadyOpenError,
    SessionJournalPlanStore,
    SQLiteResourceLockBackend,
    SQLiteSessionEventJournal,
    StaticIdentityVerifier,
    StaticJournalKeyProvider,
    JournalPrincipal,
    VerifiedIdentity,
)


MODEL = Model(id="plan-host-model", provider="fake", api="fake")


class P2DurablePlanningTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan事件追加与fenced_lease在同一事务校验(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            plan = MultiIntentPlan(
                "fenced append",
                (PlanStep("read", "read"),),
                plan_id="atomic-fenced-append",
            )
            record = await store.initialize(plan)
            stale = await store.acquire_execution(
                plan.plan_id,
                "worker-a",
                lease_seconds=0.03,
            )
            assert stale is not None
            await asyncio.sleep(0.06)

            self.assertFalse(
                await store.renew_execution(stale, lease_seconds=0.03)
            )
            successor = await store.acquire_execution(
                plan.plan_id,
                "worker-b",
                lease_seconds=1,
            )
            assert successor is not None
            self.assertGreater(successor.fencing_token, stale.fencing_token)

            with self.assertRaises(PlanExecutionLeaseLostError):
                await store.append_event(
                    plan.plan_id,
                    PlanEvent(1, "step_started", "read", {}),
                    expected_last_sequence=record.last_journal_sequence,
                    lease=stale,
                    lease_seconds=0.03,
                )

            updated = await store.append_event(
                plan.plan_id,
                PlanEvent(1, "step_started", "read", {}),
                expected_last_sequence=record.last_journal_sequence,
                lease=successor,
                lease_seconds=1,
            )
            self.assertEqual(updated.state.steps["read"].status, "running")

    async def test_plan_lease不能写入同session的另一个plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            plan_a = MultiIntentPlan(
                "a", (PlanStep("read-a", "read"),), plan_id="plan-a"
            )
            plan_b = MultiIntentPlan(
                "b", (PlanStep("read-b", "read"),), plan_id="plan-b"
            )
            await store.initialize(plan_a)
            record_b = await store.initialize(plan_b)
            lease_a = await store.acquire_execution(
                plan_a.plan_id, "worker-a", lease_seconds=1
            )
            assert lease_a is not None

            with self.assertRaisesRegex(ValueError, "目标 Plan"):
                await store.append_event(
                    plan_b.plan_id,
                    PlanEvent(1, "step_started", "read-b", {}),
                    expected_last_sequence=record_b.last_journal_sequence,
                    lease=lease_a,
                    lease_seconds=1,
                )

    def make_store(self, directory: str, session_id: str = "session"):
        journal = SQLiteSessionEventJournal(
            Path(directory) / "state.sqlite3",
            key_provider=StaticJournalKeyProvider(
                {"test": b"p" * 32}, active_key_id="test"
            ),
        )
        principal = JournalPrincipal.system("tenant")
        return SessionJournalPlanStore(journal, principal, session_id=session_id)

    async def make_host(
        self,
        directory: str,
        *,
        session_id: str,
        policies,
        step_executor=None,
        planner=None,
        approval_barrier=None,
        tools=None,
        plan_tool_bindings=None,
        tool_identity=None,
        plan_write_metadata_provider=None,
    ):
        return await DurableAgentHost.create(
            session_id=session_id,
            state_dir=directory,
            model=MODEL,
            stream_fn=ScriptedProvider([]).stream,
            system_prompt="plan test",
            tools=list(tools or []),
            auto_recover=False,
            planner=planner,
            plan_policies=policies,
            plan_step_executor=step_executor,
            plan_tool_bindings=plan_tool_bindings,
            plan_write_metadata_provider=plan_write_metadata_provider,
            plan_approval_barrier=approval_barrier,
            plan_lease_seconds=2,
            tool_identity=tool_identity,
            require_tool_identity=tool_identity is not None,
            resource_lock_backend=(
                SQLiteResourceLockBackend(
                    Path(directory) / "plan-resource-locks.sqlite3"
                )
                if plan_tool_bindings is not None
                else None
            ),
        )

    async def test_plan_store初始化重放并用cas拒绝不同并发事件(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            plan = MultiIntentPlan(
                "parallel",
                (PlanStep("a", "read"), PlanStep("b", "read")),
                plan_id="cas-plan",
            )
            initialized = await store.initialize(plan)
            restored = await store.initialize(plan)
            self.assertEqual(restored.last_journal_sequence, initialized.last_journal_sequence)
            with self.assertRaisesRegex(PlanStoreConflictError, "不同计划"):
                await store.initialize(
                    MultiIntentPlan(
                        "different",
                        (PlanStep("other", "read"),),
                        plan_id=plan.plan_id,
                    )
                )

            results = await asyncio.gather(
                store.append_event(
                    plan.plan_id,
                    PlanEvent(1, "step_started", "a", {}),
                    expected_last_sequence=initialized.last_journal_sequence,
                ),
                store.append_event(
                    plan.plan_id,
                    PlanEvent(1, "step_started", "b", {}),
                    expected_last_sequence=initialized.last_journal_sequence,
                ),
                return_exceptions=True,
            )
            self.assertEqual(sum(not isinstance(item, BaseException) for item in results), 1)
            self.assertEqual(sum(isinstance(item, PlanStoreConflictError) for item in results), 1)
            record = await store.load(plan.plan_id)
            self.assertEqual(record.state.version, 1)
            self.assertEqual(len(record.events), 1)

    async def test_host真链路在崩溃后重放running_safe_step(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policies = {"order.read": IntentPlanPolicy("order.read")}

            async def planner_fn(_request, _catalog):
                return {
                    "planId": "recover-plan",
                    "steps": [{"stepId": "read", "intent": "order.read"}],
                }

            planner = HybridRequestPlanner(policies, planner_fn)
            started = asyncio.Event()
            never = asyncio.Event()

            async def interrupted(_step, _token):
                started.set()
                await never.wait()

            first = await self.make_host(
                directory,
                session_id="recover-session",
                policies=policies,
                step_executor=interrupted,
                planner=planner,
            )
            plan = await first.plan("读取订单")
            execution = asyncio.create_task(first.execute_plan(plan.plan_id))
            await asyncio.wait_for(started.wait(), timeout=2)
            execution.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await execution
            interrupted_record = await first.plan_store.load(plan.plan_id)
            self.assertEqual(interrupted_record.state.steps["read"].status, "running")
            await first.close()

            calls = 0

            async def recovered(_step, _token):
                nonlocal calls
                calls += 1
                return {"recovered": True}

            second = await self.make_host(
                directory,
                session_id="recover-session",
                policies=policies,
                step_executor=recovered,
            )
            result = await second.resume_plan(plan.plan_id)
            self.assertEqual(result.state.phase, "completed")
            self.assertEqual(result.state.steps["read"].attempts, 2)
            self.assertEqual(calls, 1)
            durable = await second.plan_store.load(plan.plan_id)
            self.assertEqual(
                [event.type for event in durable.events],
                ["step_started", "step_recovered", "step_started", "step_succeeded"],
            )
            await second.close()

    async def test_两个workflow不能同时执行同一个plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policies = {"read": IntentPlanPolicy("read")}

            async def planner_fn(_request, _catalog):
                return {
                    "planId": "leased-plan",
                    "steps": [{"stepId": "read", "intent": "read"}],
                }

            started = asyncio.Event()
            blocked = asyncio.Event()

            async def slow(_step, _token):
                started.set()
                await blocked.wait()

            async def fast(_step, _token):
                return "should-not-run"

            first = DurablePlanWorkflow(
                store=self.make_store(directory, "lease-session"),
                planner=HybridRequestPlanner(policies, planner_fn),
                policies=policies,
                step_executor=slow,
                approval_barrier=None,
            )
            second = DurablePlanWorkflow(
                store=self.make_store(directory, "lease-session"),
                planner=None,
                policies=policies,
                step_executor=fast,
                approval_barrier=None,
            )
            plan = await first.plan("read")
            running = asyncio.create_task(first.execute(plan.plan_id))
            await asyncio.wait_for(started.wait(), timeout=2)
            with self.assertRaises(PlanExecutionConflictError):
                await second.execute(plan.plan_id)
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await running

    async def test_plan_worker传递fence且旧generation不能复活(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policies = {"read": IntentPlanPolicy("read")}

            async def planner_fn(_request, _catalog):
                return {
                    "planId": "fenced-plan",
                    "steps": [{"stepId": "read", "intent": "read"}],
                }

            observed: list[int | None] = []

            async def execute(_step, _token, *, fencing_token=None):
                observed.append(fencing_token)
                return "ok"

            store = self.make_store(directory, "fenced-session")
            workflow = DurablePlanWorkflow(
                store=store,
                planner=HybridRequestPlanner(policies, planner_fn),
                policies=policies,
                step_executor=execute,
                approval_barrier=None,
                lease_seconds=0.2,
            )
            plan = await workflow.plan("read")
            result = await workflow.execute(plan.plan_id)
            self.assertEqual(result.state.phase, "completed")
            self.assertEqual(len(observed), 1)
            self.assertIsInstance(observed[0], int)

            old = await store.acquire_execution(
                "generation-check",
                "same-worker",
                lease_seconds=1,
            )
            self.assertIsNotNone(old)
            assert old is not None
            await store.release_execution_lease(old)
            current = await store.acquire_execution(
                "generation-check",
                "same-worker",
                lease_seconds=1,
            )
            self.assertIsNotNone(current)
            assert current is not None
            self.assertGreater(current.generation, old.generation)
            self.assertFalse(
                await store.renew_execution(old, lease_seconds=1)
            )
            self.assertTrue(await store.verify_execution(current))
            await store.release_execution_lease(current)

    async def test_plan_heartbeat丢失后取消旧worker且拒绝迟到成功(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policies = {"read": IntentPlanPolicy("read")}

            async def planner_fn(_request, _catalog):
                return {
                    "planId": "late-plan",
                    "steps": [{"stepId": "read", "intent": "read"}],
                }

            entered = asyncio.Event()
            callback_cancelled = asyncio.Event()
            allow_late_return = asyncio.Event()
            observed: list[int] = []

            async def stubborn(_step, _token, *, fencing_token):
                observed.append(fencing_token)
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    callback_cancelled.set()
                    await allow_late_return.wait()
                    return "late-result"

            store = self.make_store(directory, "late-session")
            workflow = DurablePlanWorkflow(
                store=store,
                planner=HybridRequestPlanner(policies, planner_fn),
                policies=policies,
                step_executor=stubborn,
                approval_barrier=None,
                # Windows asyncio debug mode and encrypted Journal startup can
                # legitimately take more than 60 ms before the first Handler.
                # 300 ms remains short enough to exercise prompt lease loss
                # without making the test scheduler-dependent.
                lease_seconds=0.3,
            )
            plan = await workflow.plan("read")
            original_renew = store.renew_execution
            lose_heartbeat = asyncio.Event()

            async def controlled_renew(lease, *, lease_seconds):
                if lose_heartbeat.is_set():
                    return False
                return await original_renew(lease, lease_seconds=lease_seconds)

            store.renew_execution = controlled_renew  # type: ignore[method-assign]
            running = asyncio.create_task(workflow.execute(plan.plan_id))
            await asyncio.wait_for(entered.wait(), timeout=2)
            lose_heartbeat.set()
            await asyncio.wait_for(callback_cancelled.wait(), timeout=2)

            # 等旧 Lease 自然过期，再由另一个 Worker 取得更高 generation。
            await asyncio.sleep(0.35)
            successor_store = self.make_store(directory, "late-session")
            successor = await successor_store.acquire_execution(
                plan.plan_id,
                "worker-b",
                lease_seconds=1,
            )
            self.assertIsNotNone(successor)
            assert successor is not None
            self.assertGreater(successor.fencing_token, observed[0])

            allow_late_return.set()
            with self.assertRaises(PlanExecutionLeaseLostError):
                await running

            # 旧 Worker 的 finally 只能释放旧 generation，不能碰到新 Lease。
            self.assertTrue(await successor_store.verify_execution(successor))
            record = await successor_store.load(plan.plan_id)
            self.assertEqual(
                [event.type for event in record.events],
                ["step_started"],
            )
            await successor_store.release_execution_lease(successor)

    async def test_第二个host在plan之前就被session租约拒绝(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policies = {"read": IntentPlanPolicy("read")}

            async def execute(_step, _token):
                return "ok"

            first = await self.make_host(
                directory,
                session_id="lease-session",
                policies=policies,
                step_executor=execute,
            )
            try:
                with self.assertRaises(SessionAlreadyOpenError):
                    await self.make_host(
                        directory,
                        session_id="lease-session",
                        policies=policies,
                        step_executor=execute,
                    )
            finally:
                await first.close()

    async def test_durable_approval_adapter错误hash拒绝且可重启后批准(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policy = IntentPlanPolicy(
                "order.cancel",
                requires_approval=True,
                write=True,
                replay_policy="never",
                approval_roles=("plan.approver",),
            )
            policies = {policy.intent: policy}

            async def planner_fn(_request, _catalog):
                return {
                    "planId": "approval-plan",
                    "steps": [
                        {
                            "stepId": "cancel",
                            "intent": "order.cancel",
                            "arguments": {"orderId": "1001"},
                        }
                    ],
                }

            class WrongBackend:
                def authorize_plan_step(self, **_bindings):
                    return {
                        "approved": True,
                        "actionHash": "tampered",
                        "approvalId": "approval-wrong",
                    }

            executed = 0

            async def execute(_call_id, _arguments, _context, _token, _update):
                nonlocal executed
                executed += 1
                return AgentToolResult(
                    content=[{"type": "text", "text": "cancelled"}],
                    details={"cancelled": True},
                )

            cancel_tool = AgentTool(
                name="cancel_order",
                label="cancel",
                description="cancel one order",
                execute=None,
                execute_with_context=execute,
                validate_args=lambda value: value,
                replay_policy="never",
                execution_mode="resource_locked",
                resolve_resource_keys=lambda value: (
                    f"order:{value['orderId']}"
                ),
                supports_resource_fencing=True,
            )
            verifier = StaticIdentityVerifier(
                {
                    "plan-worker": (
                        "plan-worker-test-credential",
                        {"operator"},
                    )
                }
            )
            worker_identity = await verifier.verify(
                IdentityClaim(
                    "plan-worker",
                    "plan-worker-test-credential",
                )
            )

            first = await self.make_host(
                directory,
                session_id="approval-session",
                policies=policies,
                planner=HybridRequestPlanner(policies, planner_fn),
                approval_barrier=DurablePlanApprovalAdapter(WrongBackend()),
                tools=[cancel_tool],
                plan_tool_bindings={"order.cancel": "cancel_order"},
                tool_identity=worker_identity,
                plan_write_metadata_provider=lambda *_args: PlanWriteMetadata(
                    operation_id="approval-write-operation",
                    idempotency_key="cancel-order-1001",
                    entity_id="1001",
                    expected_entity_version=1,
                ),
            )
            await first.operation_store.append(
                "operation_started",
                "approval-session",
                "approval-write-operation",
                {"configuration": {}, "tools": []},
            )
            plan = await first.plan("取消订单")
            with self.assertRaisesRegex(PlanExecutionError, "Action Hash"):
                await first.execute_plan(plan.plan_id)
            self.assertEqual(executed, 0)
            waiting = await first.plan_store.load(plan.plan_id)
            self.assertEqual(waiting.state.steps["cancel"].status, "waiting_approval")
            await first.close()

            bindings = []

            class CorrectBackend:
                consumed: set[str] = set()

                def authorize_plan_step(self, **values):
                    bindings.append(values)
                    now = time.time()
                    receipt = PlanApprovalReceipt.issue(
                        approval_id="approval-good",
                        receipt_id="receipt-good",
                        plan_id=values["plan_id"],
                        step=replace(
                            plan.step(values["step_id"]),
                            _execution_action_hash=values["action_hash"],
                        ),
                        state_version=values["state_version"],
                        approver=VerifiedIdentity(
                            principal_id="manager-1",
                            roles=frozenset({"plan.approver"}),
                            issuer="test-identity",
                            verification_id="identity-proof",
                        ),
                        verification_id="approval-proof",
                        issued_at=now - 1,
                        expires_at=now + 60,
                    )
                    return {
                        "approved": True,
                        "actionHash": values["action_hash"],
                        "approvalId": "approval-good",
                        "receipt": receipt.to_dict(),
                    }

                def consume_plan_approval_receipt(self, **values):
                    receipt = PlanApprovalReceipt.from_dict(values["receipt"])
                    if receipt.receipt_id in self.consumed:
                        raise RuntimeError("receipt already consumed")
                    self.consumed.add(receipt.receipt_id)
                    return receipt.consumed(time.time()).to_dict()

            second = await self.make_host(
                directory,
                session_id="approval-session",
                policies=policies,
                approval_barrier=DurablePlanApprovalAdapter(CorrectBackend()),
                tools=[cancel_tool],
                plan_tool_bindings={"order.cancel": "cancel_order"},
                tool_identity=worker_identity,
                plan_write_metadata_provider=lambda *_args: PlanWriteMetadata(
                    operation_id="approval-write-operation",
                    idempotency_key="cancel-order-1001",
                    entity_id="1001",
                    expected_entity_version=1,
                ),
            )
            result = await second.resume_plan(plan.plan_id)
            self.assertEqual(result.state.phase, "completed")
            self.assertEqual(executed, 1)
            self.assertEqual(bindings[0]["plan_id"], plan.plan_id)
            self.assertEqual(bindings[0]["step_id"], "cancel")
            self.assertNotEqual(
                bindings[0]["action_hash"], plan.step("cancel").action_hash
            )
            self.assertEqual(result.state.steps["cancel"].approval_id, "approval-good")
            await second.close()


if __name__ == "__main__":
    unittest.main()
