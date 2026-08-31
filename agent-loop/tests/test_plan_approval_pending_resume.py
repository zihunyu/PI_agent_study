"""Plan approval 的 pending/approved/denied 恢复边界。"""

from __future__ import annotations

import unittest

from pi_agent_loop import CancellationToken, VerifiedIdentity
from pi_agent_loop.planning import (
    ApprovalBarrier,
    DurablePlanApprovalAdapter,
    IntentPlanPolicy,
    MultiIntentPlan,
    PlanApprovalDecision,
    PlanApprovalReceipt,
    PlanExecutionState,
    PlanExecutor,
    PlanParameterContract,
    PlanStep,
    PlanStepState,
    PlanValidationError,
    TaskStateMachine,
)


NOW = 4_000.0


class PlanApprovalPendingResumeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        contract = PlanParameterContract(allow_empty=True)
        self.policy = IntentPlanPolicy(
            "orders.cancel",
            requires_approval=True,
            write=True,
            replay_policy="never",
            approval_roles=("plan.approver",),
            parameter_contract=contract,
        )
        self.step = PlanStep(
            "cancel",
            "orders.cancel",
            requires_approval=True,
            write=True,
            replay_policy="never",
            approval_roles=("plan.approver",),
            parameter_contract=contract,
        )
        self.plan = MultiIntentPlan(
            "取消订单",
            (self.step,),
            plan_id="pending-resume-plan",
        )

    def _receipt(self, state: PlanExecutionState) -> PlanApprovalReceipt:
        return PlanApprovalReceipt.issue(
            approval_id="approval-real-1",
            receipt_id="receipt-real-1",
            plan_id=self.plan.plan_id,
            step=self.step,
            state_version=state.version,
            approver=VerifiedIdentity(
                principal_id="manager-1",
                roles=frozenset({"plan.approver"}),
                issuer="tests",
                verification_id="manager-proof",
            ),
            verification_id="approval-proof",
            issued_at=NOW - 1,
            expires_at=NOW + 60,
        )

    async def test_pending持久等待且resume批准后只执行一次(self) -> None:
        mode = "pending"
        resolver_states: list[PlanExecutionState] = []
        handler_calls = 0
        consumed = 0

        async def resolver(step, state, _cancellation):
            resolver_states.append(state)
            if mode == "pending":
                return PlanApprovalDecision.pending(step, "approval-real-1")
            return PlanApprovalDecision.grant(step, self._receipt(state))

        def consume(receipt, _step, _state, _cancellation):
            nonlocal consumed
            consumed += 1
            return receipt.consumed(NOW)

        async def execute(_step, _cancellation, *, context):
            nonlocal handler_calls
            self.assertIsNotNone(context.approval_receipt)
            handler_calls += 1
            return {"cancelled": True}

        barrier = ApprovalBarrier(
            resolver,
            receipt_consumer=consume,
            clock=lambda: NOW,
        )
        first = await PlanExecutor(
            self.plan,
            {self.policy.intent: self.policy},
            execute,
            approval_barrier=barrier,
            clock=lambda: NOW,
        ).execute()

        self.assertEqual(first.state.phase, "waiting_approval")
        self.assertEqual(first.state.steps["cancel"].approval_id, "approval-real-1")
        self.assertIsNone(first.state.steps["cancel"].approval_receipt)
        self.assertEqual(handler_calls, 0)
        self.assertEqual(consumed, 0)
        self.assertEqual([event.type for event in first.events], ["step_waiting_approval"])
        self.assertEqual(first.events[0].data["approvalId"], "approval-real-1")
        self.assertEqual(
            TaskStateMachine(self.plan)
            .replay(first.events)
            .steps["cancel"]
            .approval_id,
            "approval-real-1",
        )

        mode = "approved"
        resumed = await PlanExecutor(
            self.plan,
            {self.policy.intent: self.policy},
            execute,
            approval_barrier=barrier,
            clock=lambda: NOW,
        ).execute(initial_state=first.state)

        self.assertEqual(resumed.state.phase, "completed")
        self.assertEqual(handler_calls, 1)
        self.assertEqual(consumed, 1)
        self.assertEqual(resolver_states[-1].steps["cancel"].approval_id, "approval-real-1")
        resumed_types = [event.type for event in resumed.events]
        self.assertIn("step_approval_granted", resumed_types)
        self.assertIn("step_started", resumed_types)
        self.assertIn("step_succeeded", resumed_types)
        self.assertLess(
            resumed_types.index("step_approval_granted"),
            resumed_types.index("step_started"),
        )
        self.assertEqual(
            TaskStateMachine(self.plan)
            .replay((*first.events, *resumed.events))
            .phase,
            "completed",
        )

    async def test_pending后拒绝进入failed且不执行(self) -> None:
        mode = "pending"
        handler_calls = 0

        async def resolver(step, _state, _cancellation):
            if mode == "pending":
                return PlanApprovalDecision.pending(step, "approval-real-1")
            return PlanApprovalDecision.deny(
                step,
                "经理拒绝",
                approval_id="approval-real-1",
            )

        async def execute(_step, _cancellation, *, context):
            nonlocal handler_calls
            del context
            handler_calls += 1

        barrier = ApprovalBarrier(resolver)
        first = await PlanExecutor(
            self.plan,
            {self.policy.intent: self.policy},
            execute,
            approval_barrier=barrier,
        ).execute()
        mode = "denied"
        denied = await PlanExecutor(
            self.plan,
            {self.policy.intent: self.policy},
            execute,
            approval_barrier=barrier,
        ).execute(initial_state=first.state)

        self.assertEqual(denied.state.phase, "failed")
        self.assertEqual(denied.state.steps["cancel"].error, "经理拒绝")
        self.assertEqual(handler_calls, 0)

    async def test伪造approval_id但无receipt绝不能执行(self) -> None:
        forged = PlanExecutionState(
            self.plan.plan_id,
            {
                "cancel": PlanStepState(
                    status="waiting_approval",
                    approval_id="forged-approval-id",
                )
            },
            version=1,
        )
        handler_calls = 0

        async def execute(_step, _cancellation, *, context):
            nonlocal handler_calls
            del context
            handler_calls += 1

        result = await PlanExecutor(
            self.plan,
            {self.policy.intent: self.policy},
            execute,
        ).execute(initial_state=forged)

        self.assertEqual(result.state.phase, "waiting_approval")
        self.assertEqual(handler_calls, 0)

    async def test无barrier生成稳定请求id但不执行(self) -> None:
        calls = 0

        async def execute(_step, _cancellation, *, context):
            nonlocal calls
            del context
            calls += 1

        first = await PlanExecutor(
            self.plan,
            {self.policy.intent: self.policy},
            execute,
        ).execute()
        second = await PlanExecutor(
            self.plan,
            {self.policy.intent: self.policy},
            execute,
        ).execute()
        first_id = first.state.steps["cancel"].approval_id
        self.assertIsNotNone(first_id)
        self.assertTrue(first_id.startswith("plan-approval-request-"))
        self.assertEqual(first_id, second.state.steps["cancel"].approval_id)
        self.assertEqual(calls, 0)

    def test_pending决定严格校验(self) -> None:
        with self.assertRaisesRegex(PlanValidationError, "Approval ID"):
            PlanApprovalDecision.pending(self.step, "")
        with self.assertRaisesRegex(PlanValidationError, "原因"):
            PlanApprovalDecision(
                None,
                self.step.action_hash,
                approval_id="approval-real-1",
                reason="不允许",
            )

    async def test_durable_adapter接受显式pending状态(self) -> None:
        class Backend:
            def authorize_plan_step(self, **values):
                return {
                    "status": "pending",
                    "actionHash": values["action_hash"],
                    "approvalId": "approval-real-1",
                }

        decision = await DurablePlanApprovalAdapter(Backend()).authorize(
            self.step,
            TaskStateMachine(self.plan).initial_state(),
            CancellationToken(),
        )
        self.assertEqual(decision.status, "pending")
        self.assertEqual(decision.approval_id, "approval-real-1")


if __name__ == "__main__":
    unittest.main()
