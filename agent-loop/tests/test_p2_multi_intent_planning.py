"""Multi Intent Planner、DAG、审批、失败和恢复测试。"""

from __future__ import annotations

import asyncio
import unittest

from pi_agent_loop import CancellationToken, OperationCancelledError
from pi_agent_loop.planning import (
    ApprovalBarrier,
    DependencyGraph,
    HybridRequestPlanner,
    IntentPlanPolicy,
    MultiIntentPlan,
    PlanApprovalDecision,
    PlanEvent,
    PlanExecutionState,
    PlanExecutor,
    PlanStep,
    PlanStepState,
    PlanValidationError,
    PlanValidator,
    TaskStateMachine,
)


class P2MultiIntentPlanningTests(unittest.IsolatedAsyncioTestCase):
    def safe_policy(self, intent):
        return IntentPlanPolicy(intent)

    async def test_hybrid_planner生成多intent并拒绝策略降级和环(self) -> None:
        policies = {
            "order.read": IntentPlanPolicy("order.read", capabilities=("orders.read",)),
            "order.cancel": IntentPlanPolicy(
                "order.cancel",
                requires_approval=True,
                write=True,
                replay_policy="never",
                capabilities=("orders.cancel",),
            ),
            "message.send": IntentPlanPolicy("message.send", capabilities=("messages.send",)),
        }

        async def planner(_request, catalog):
            self.assertEqual(len(catalog), 3)
            return {
                "planId": "plan-1",
                "steps": [
                    {"stepId": "read", "intent": "order.read", "arguments": {"id": "1"}},
                    {
                        "stepId": "cancel",
                        "intent": "order.cancel",
                        "arguments": {"id": "2"},
                        "dependsOn": ["read"],
                    },
                    {
                        "stepId": "send",
                        "intent": "message.send",
                        "dependsOn": ["cancel"],
                    },
                ],
            }

        plan = await HybridRequestPlanner(policies, planner).plan("查询、取消并发送")
        self.assertEqual(
            DependencyGraph(plan).topological_order, ("read", "cancel", "send")
        )
        self.assertTrue(plan.step("cancel").requires_approval)
        self.assertEqual(plan.step("cancel").replay_policy, "never")

        async def downgrade(_request, _catalog):
            return {
                "steps": [
                    {
                        "stepId": "cancel",
                        "intent": "order.cancel",
                        "requiresApproval": False,
                    }
                ]
            }

        with self.assertRaisesRegex(PlanValidationError, "覆盖可信策略"):
            await HybridRequestPlanner(policies, downgrade).plan("取消")

        cyclic = MultiIntentPlan(
            "cycle",
            (
                PlanStep("a", "order.read", depends_on=("b",), capabilities=("orders.read",)),
                PlanStep("b", "order.read", depends_on=("a",), capabilities=("orders.read",)),
            ),
        )
        with self.assertRaisesRegex(PlanValidationError, "存在环"):
            PlanValidator(policies).validate(cyclic)

    async def test_dag并发执行且依赖步骤等待(self) -> None:
        policies = {name: self.safe_policy(name) for name in ("a", "b", "c")}
        plan = MultiIntentPlan(
            "parallel then join",
            (
                PlanStep("a", "a"),
                PlanStep("b", "b"),
                PlanStep("c", "c", depends_on=("a", "b")),
            ),
            plan_id="parallel-plan",
        )
        active = 0
        max_active = 0
        timeline = []

        async def execute(step, _token):
            nonlocal active, max_active
            timeline.append(f"start:{step.step_id}")
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.015 if step.step_id != "c" else 0)
            active -= 1
            timeline.append(f"end:{step.step_id}")
            return {"step": step.step_id}

        result = await PlanExecutor(
            plan, policies, execute, max_parallel_steps=2
        ).execute()

        self.assertEqual(result.state.phase, "completed")
        self.assertEqual(max_active, 2)
        self.assertGreater(timeline.index("start:c"), timeline.index("end:a"))
        self.assertGreater(timeline.index("start:c"), timeline.index("end:b"))
        self.assertEqual(
            [step_id for step_id, _ in result.synthesized.ordered_results],
            ["a", "b", "c"],
        )

    async def test_approval_barrier绑定action_hash后推进依赖(self) -> None:
        policies = {
            "read": IntentPlanPolicy("read"),
            "cancel": IntentPlanPolicy(
                "cancel", requires_approval=True, write=True, replay_policy="never"
            ),
            "notify": IntentPlanPolicy("notify"),
        }
        plan = MultiIntentPlan(
            "read cancel notify",
            (
                PlanStep("read", "read"),
                PlanStep(
                    "cancel",
                    "cancel",
                    depends_on=("read",),
                    requires_approval=True,
                    write=True,
                    replay_policy="never",
                ),
                PlanStep("notify", "notify", depends_on=("cancel",)),
            ),
        )
        approvals = []
        executed = []

        async def authorize(step, _state, _token):
            approvals.append(step.step_id)
            return PlanApprovalDecision.grant(step, "approval-1")

        async def execute(step, _token):
            executed.append(step.step_id)
            return {"ok": step.step_id}

        result = await PlanExecutor(
            plan,
            policies,
            execute,
            approval_barrier=ApprovalBarrier(authorize),
        ).execute()

        self.assertEqual(result.state.phase, "completed")
        self.assertEqual(approvals, ["cancel"])
        self.assertEqual(executed, ["read", "cancel", "notify"])
        self.assertEqual(result.state.steps["cancel"].approval_id, "approval-1")
        event_types = [event.type for event in result.events]
        self.assertLess(
            event_types.index("step_approval_granted"),
            event_types.index("step_started", event_types.index("step_approval_granted")),
        )

    async def test_action_hash同时绑定业务参数和可信策略(self) -> None:
        original = PlanStep(
            "cancel",
            "cancel",
            arguments={"orderId": "1001"},
            requires_approval=True,
            write=True,
            replay_policy="never",
            capabilities=("orders.cancel",),
        )
        changed_capability = PlanStep(
            "cancel",
            "cancel",
            arguments={"orderId": "1001"},
            requires_approval=True,
            write=True,
            replay_policy="never",
            capabilities=("orders.admin",),
        )
        changed_argument = PlanStep(
            "cancel",
            "cancel",
            arguments={"orderId": "1002"},
            requires_approval=True,
            write=True,
            replay_policy="never",
            capabilities=("orders.cancel",),
        )
        self.assertNotEqual(original.action_hash, changed_capability.action_hash)
        self.assertNotEqual(original.action_hash, changed_argument.action_hash)
        persisted = MultiIntentPlan(
            "cancel", (original,), plan_id="persisted-plan"
        ).to_dict()
        persisted["steps"][0]["actionHash"] = "tampered"
        with self.assertRaisesRegex(PlanValidationError, "Action Hash"):
            MultiIntentPlan.from_dict(persisted)

    async def test_失败仅跳过依赖分支并保留独立结果(self) -> None:
        policies = {name: self.safe_policy(name) for name in ("a", "b", "c")}
        plan = MultiIntentPlan(
            "partial failure",
            (
                PlanStep("a", "a"),
                PlanStep("b", "b"),
                PlanStep("c", "c", depends_on=("a",)),
            ),
        )

        async def execute(step, _token):
            if step.step_id == "a":
                raise RuntimeError("A failed")
            return {"ok": step.step_id}

        result = await PlanExecutor(plan, policies, execute).execute()
        self.assertEqual(result.state.phase, "failed")
        self.assertEqual(result.state.steps["a"].status, "failed")
        self.assertEqual(result.state.steps["b"].status, "succeeded")
        self.assertEqual(result.state.steps["c"].status, "skipped")
        self.assertEqual(result.synthesized.ordered_results, (("b", {"ok": "b"}),))

    async def test_recovery只重放safe且never进入人工介入(self) -> None:
        safe_policies = {"read": IntentPlanPolicy("read")}
        safe_plan = MultiIntentPlan(
            "recover read", (PlanStep("read", "read"),), plan_id="safe-plan"
        )
        safe_machine = TaskStateMachine(safe_plan)
        interrupted = safe_machine.apply(
            safe_machine.initial_state(), PlanEvent(1, "step_started", "read", {})
        )
        calls = 0

        async def execute(_step, _token):
            nonlocal calls
            calls += 1
            return {"recovered": True}

        safe_result = await PlanExecutor(
            safe_plan, safe_policies, execute
        ).execute(initial_state=interrupted)
        self.assertEqual(calls, 1)
        self.assertEqual(safe_result.state.steps["read"].attempts, 2)
        self.assertEqual(safe_result.events[0].type, "step_recovered")
        restored = safe_machine.state_from_dict(safe_result.state.to_dict())
        self.assertEqual(restored, safe_result.state)

        never_policy = IntentPlanPolicy(
            "cancel", requires_approval=True, write=True, replay_policy="never"
        )
        never_plan = MultiIntentPlan(
            "recover write",
            (
                PlanStep(
                    "cancel",
                    "cancel",
                    requires_approval=True,
                    write=True,
                    replay_policy="never",
                ),
            ),
            plan_id="never-plan",
        )
        never_machine = TaskStateMachine(never_plan)
        state = never_machine.initial_state()
        step = never_plan.step("cancel")
        for event in (
            PlanEvent(1, "step_waiting_approval", "cancel", {"actionHash": step.action_hash}),
            PlanEvent(
                2,
                "step_approval_granted",
                "cancel",
                {"approvalId": "approval-2", "actionHash": step.action_hash},
            ),
            PlanEvent(3, "step_started", "cancel", {}),
        ):
            state = never_machine.apply(state, event)
        never_calls = 0

        async def should_not_execute(_step, _token):
            nonlocal never_calls
            never_calls += 1

        never_result = await PlanExecutor(
            never_plan, {"cancel": never_policy}, should_not_execute
        ).execute(initial_state=state)
        self.assertEqual(never_calls, 0)
        self.assertEqual(never_result.state.phase, "manual_intervention")
        self.assertEqual(
            never_result.state.steps["cancel"].status, "manual_intervention"
        )

    async def test_waiting_approval快照恢复后重新进入barrier(self) -> None:
        policy = IntentPlanPolicy(
            "cancel", requires_approval=True, write=True, replay_policy="never"
        )
        plan = MultiIntentPlan(
            "resume approval",
            (
                PlanStep(
                    "cancel",
                    "cancel",
                    requires_approval=True,
                    write=True,
                    replay_policy="never",
                ),
            ),
        )
        machine = TaskStateMachine(plan)
        step = plan.step("cancel")
        waiting = machine.apply(
            machine.initial_state(),
            PlanEvent(
                1,
                "step_waiting_approval",
                "cancel",
                {"actionHash": step.action_hash},
            ),
        )
        approvals = 0

        async def authorize(current, _state, _token):
            nonlocal approvals
            approvals += 1
            return PlanApprovalDecision.grant(current, "approval-resumed")

        async def execute(_step, _token):
            return {"cancelled": True}

        result = await PlanExecutor(
            plan,
            {"cancel": policy},
            execute,
            approval_barrier=ApprovalBarrier(authorize),
        ).execute(initial_state=waiting)
        self.assertEqual(approvals, 1)
        self.assertEqual(result.state.phase, "completed")

    async def test_恢复拒绝绕过依赖的伪造snapshot(self) -> None:
        plan = MultiIntentPlan(
            "dependency",
            (
                PlanStep("read", "read"),
                PlanStep("notify", "notify", depends_on=("read",)),
            ),
            plan_id="forged-snapshot",
        )
        forged = PlanExecutionState(
            plan.plan_id,
            {
                "read": PlanStepState(status="pending"),
                "notify": PlanStepState(
                    status="succeeded", attempts=1, result={"sent": True}
                ),
            },
            version=1,
        )
        with self.assertRaisesRegex(PlanValidationError, "绕过了未完成依赖"):
            TaskStateMachine(plan).state_from_dict(forged.to_dict())

    async def test_cancellation主动中断忽略token的异步step(self) -> None:
        plan = MultiIntentPlan("cancel", (PlanStep("read", "read"),))
        token = CancellationToken()
        started = asyncio.Event()
        never = asyncio.Event()

        async def ignores_token(_step, _token):
            started.set()
            await never.wait()

        executor = PlanExecutor(
            plan, {"read": IntentPlanPolicy("read")}, ignores_token
        )
        task = asyncio.create_task(executor.execute(cancellation=token))
        await asyncio.wait_for(started.wait(), timeout=2)
        token.cancel("stop plan")
        with self.assertRaisesRegex(OperationCancelledError, "stop plan"):
            await asyncio.wait_for(task, timeout=2)
        self.assertEqual(executor.state.steps["read"].status, "running")


if __name__ == "__main__":
    unittest.main()
