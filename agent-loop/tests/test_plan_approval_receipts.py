"""Plan write approvals require trusted, action-bound, one-time receipts."""

from __future__ import annotations

import unittest

from pi_agent_loop import CancellationToken, VerifiedIdentity
from pi_agent_loop.planning import (
    ApprovalBarrier,
    MultiIntentPlan,
    PlanApprovalDecision,
    PlanApprovalReceipt,
    PlanEvent,
    PlanExecutionError,
    PlanStep,
    PlanValidationError,
    TaskStateMachine,
)


NOW = 2_000.0


class RecordingConsumer:
    def __init__(self) -> None:
        self.receipt_ids: set[str] = set()
        self.calls = 0

    def __call__(self, receipt, _step, _state, _cancellation):
        self.calls += 1
        if receipt.receipt_id in self.receipt_ids:
            raise RuntimeError("receipt already consumed")
        self.receipt_ids.add(receipt.receipt_id)
        return receipt.consumed(NOW)


class PlanApprovalReceiptSecurityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.step = PlanStep(
            "write",
            "resource.write",
            arguments={"resourceId": "r-1"},
            requires_approval=True,
            write=True,
            replay_policy="never",
            approval_roles=("plan.approver",),
        )
        self.plan = MultiIntentPlan(
            "write resource",
            (self.step,),
            plan_id="receipt-plan",
        )
        self.machine = TaskStateMachine(self.plan)
        self.waiting = self.machine.apply(
            self.machine.initial_state(),
            PlanEvent(
                1,
                "step_waiting_approval",
                self.step.step_id,
                {"actionHash": self.step.action_hash},
            ),
        )

    def receipt(
        self,
        *,
        roles=frozenset({"plan.approver"}),
        action_step=None,
        issued_at=NOW - 10,
        expires_at=NOW + 10,
    ) -> PlanApprovalReceipt:
        approver = VerifiedIdentity(
            principal_id="approver-1",
            roles=roles,
            issuer="test-issuer",
            verification_id="identity-proof",
        )
        return PlanApprovalReceipt.issue(
            approval_id="approval-1",
            receipt_id="receipt-1",
            plan_id=self.plan.plan_id,
            step=action_step or self.step,
            state_version=self.waiting.version,
            approver=approver,
            verification_id="receipt-proof",
            issued_at=issued_at,
            expires_at=expires_at,
        )

    async def test_legacy_approved_bool_and_arbitrary_id_cannot_approve(self) -> None:
        consumer = RecordingConsumer()

        async def resolve(step, _state, _cancellation):
            return PlanApprovalDecision.grant(step, "arbitrary-approval-id")

        barrier = ApprovalBarrier(
            resolve,
            receipt_consumer=consumer,
            clock=lambda: NOW,
        )
        with self.assertRaisesRegex(PlanExecutionError, "不能替代"):
            await barrier.authorize(
                self.step,
                self.waiting,
                CancellationToken(),
            )
        self.assertEqual(consumer.calls, 0)

        with self.assertRaisesRegex(PlanValidationError, "Receipt"):
            self.machine.apply(
                self.waiting,
                PlanEvent(
                    2,
                    "step_approval_granted",
                    self.step.step_id,
                    {
                        "approvalId": "arbitrary-approval-id",
                        "actionHash": self.step.action_hash,
                    },
                ),
            )

    async def test_receipt_action_hash_mismatch_fails_before_consume(self) -> None:
        other = PlanStep(
            "write",
            "resource.write",
            arguments={"resourceId": "r-2"},
            requires_approval=True,
            write=True,
            replay_policy="never",
            approval_roles=("plan.approver",),
        )
        receipt = self.receipt(action_step=other)
        consumer = RecordingConsumer()

        async def resolve(_step, _state, _cancellation):
            return PlanApprovalDecision(
                True,
                receipt.action_hash,
                approval_id=receipt.approval_id,
                receipt=receipt,
            )

        barrier = ApprovalBarrier(
            resolve,
            receipt_consumer=consumer,
            clock=lambda: NOW,
        )
        with self.assertRaisesRegex(PlanExecutionError, "Action Hash"):
            await barrier.authorize(self.step, self.waiting, CancellationToken())
        self.assertEqual(consumer.calls, 0)

    async def test_expired_receipt_fails_before_consume(self) -> None:
        receipt = self.receipt(issued_at=NOW - 20, expires_at=NOW - 1)
        consumer = RecordingConsumer()

        async def resolve(step, _state, _cancellation):
            return PlanApprovalDecision.grant(step, receipt)

        barrier = ApprovalBarrier(
            resolve,
            receipt_consumer=consumer,
            clock=lambda: NOW,
        )
        with self.assertRaisesRegex(PlanExecutionError, "过期"):
            await barrier.authorize(self.step, self.waiting, CancellationToken())
        self.assertEqual(consumer.calls, 0)

    async def test_approver_role_must_match_trusted_step_policy(self) -> None:
        receipt = self.receipt(roles=frozenset({"plan.auditor"}))
        consumer = RecordingConsumer()

        async def resolve(step, _state, _cancellation):
            return PlanApprovalDecision.grant(step, receipt)

        barrier = ApprovalBarrier(
            resolve,
            receipt_consumer=consumer,
            clock=lambda: NOW,
        )
        with self.assertRaisesRegex(PlanExecutionError, "角色"):
            await barrier.authorize(self.step, self.waiting, CancellationToken())
        self.assertEqual(consumer.calls, 0)

    async def test_receipt_can_be_consumed_only_once(self) -> None:
        receipt = self.receipt()
        consumer = RecordingConsumer()

        async def resolve(step, _state, _cancellation):
            return PlanApprovalDecision.grant(step, receipt)

        barrier = ApprovalBarrier(
            resolve,
            receipt_consumer=consumer,
            clock=lambda: NOW,
        )
        decision = await barrier.authorize(
            self.step,
            self.waiting,
            CancellationToken(),
        )
        self.assertIsNotNone(decision.receipt)
        self.assertEqual(decision.receipt.consumed_at, NOW)
        with self.assertRaisesRegex(PlanExecutionError, "重复使用"):
            await barrier.authorize(
                self.step,
                self.waiting,
                CancellationToken(),
            )
        self.assertEqual(consumer.calls, 1)


if __name__ == "__main__":
    unittest.main()
