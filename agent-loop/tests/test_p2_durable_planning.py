"""Unified Journal and DurableAgentHost integration for Multi-Intent plans."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from pi_agent_loop import (
    DurableAgentHost,
    DurablePlanApprovalAdapter,
    HybridRequestPlanner,
    IntentPlanPolicy,
    Model,
    MultiIntentPlan,
    PlanEvent,
    PlanExecutionConflictError,
    PlanExecutionError,
    PlanStep,
    PlanStoreConflictError,
    ScriptedProvider,
    SessionJournalPlanStore,
    SQLiteSessionEventJournal,
    StaticJournalKeyProvider,
    JournalPrincipal,
)


MODEL = Model(id="plan-host-model", provider="fake", api="fake")


class P2DurablePlanningTests(unittest.IsolatedAsyncioTestCase):
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
        step_executor,
        planner=None,
        approval_barrier=None,
    ):
        return await DurableAgentHost.create(
            session_id=session_id,
            state_dir=directory,
            model=MODEL,
            stream_fn=ScriptedProvider([]).stream,
            system_prompt="plan test",
            tools=[],
            auto_recover=False,
            planner=planner,
            plan_policies=policies,
            plan_step_executor=step_executor,
            plan_approval_barrier=approval_barrier,
            plan_lease_seconds=2,
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

    async def test_两个host不能同时执行同一个plan(self) -> None:
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

            first = await self.make_host(
                directory,
                session_id="lease-session",
                policies=policies,
                step_executor=slow,
                planner=HybridRequestPlanner(policies, planner_fn),
            )
            second = await self.make_host(
                directory,
                session_id="lease-session",
                policies=policies,
                step_executor=fast,
            )
            plan = await first.plan("read")
            running = asyncio.create_task(first.execute_plan(plan.plan_id))
            await asyncio.wait_for(started.wait(), timeout=2)
            with self.assertRaises(PlanExecutionConflictError):
                await second.execute_plan(plan.plan_id)
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await running
            await first.close()
            await second.close()

    async def test_durable_approval_adapter错误hash拒绝且可重启后批准(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            policy = IntentPlanPolicy(
                "order.cancel",
                requires_approval=True,
                write=True,
                replay_policy="never",
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

            async def execute(_step, _token):
                nonlocal executed
                executed += 1
                return {"cancelled": True}

            first = await self.make_host(
                directory,
                session_id="approval-session",
                policies=policies,
                step_executor=execute,
                planner=HybridRequestPlanner(policies, planner_fn),
                approval_barrier=DurablePlanApprovalAdapter(WrongBackend()),
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
                def authorize_plan_step(self, **values):
                    bindings.append(values)
                    return {
                        "approved": True,
                        "actionHash": values["action_hash"],
                        "approvalId": "approval-good",
                    }

            second = await self.make_host(
                directory,
                session_id="approval-session",
                policies=policies,
                step_executor=execute,
                approval_barrier=DurablePlanApprovalAdapter(CorrectBackend()),
            )
            result = await second.resume_plan(plan.plan_id)
            self.assertEqual(result.state.phase, "completed")
            self.assertEqual(executed, 1)
            self.assertEqual(bindings[0]["plan_id"], plan.plan_id)
            self.assertEqual(bindings[0]["step_id"], "cancel")
            self.assertEqual(bindings[0]["action_hash"], plan.step("cancel").action_hash)
            self.assertEqual(result.state.steps["cancel"].approval_id, "approval-good")
            await second.close()


if __name__ == "__main__":
    unittest.main()
