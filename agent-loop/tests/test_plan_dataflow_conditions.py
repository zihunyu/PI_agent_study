from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from pi_agent_loop import (
    JournalPrincipal,
    SQLiteSessionEventJournal,
    StaticJournalKeyProvider,
    VerifiedIdentity,
)
from pi_agent_loop.planning import (
    ApprovalBarrier,
    HybridRequestPlanner,
    IntentPlanPolicy,
    MultiIntentPlan,
    PlanApprovalDecision,
    PlanApprovalReceipt,
    PlanExecutor,
    PlanEvent,
    PlanIntentArgumentBinding,
    PlanIntentCondition,
    PlanIntentResultReference,
    PlanParameterContract,
    PlanResultContract,
    PlanResultRule,
    PlanStep,
    PlanValidationError,
    PlanValidator,
    SessionJournalPlanStore,
)


NOW = 1000.0
APPROVER = VerifiedIdentity(
    principal_id="approver",
    roles=frozenset({"plan.approver"}),
    issuer="test",
    verification_id="identity-proof",
)


class ReceiptConsumer:
    def __call__(self, receipt, _step, _state, _token):
        return receipt.consumed(NOW)


def issue_receipt(step, state):
    return PlanApprovalReceipt.issue(
        approval_id="approval-1",
        receipt_id="receipt-1",
        plan_id=state.plan_id,
        step=step,
        state_version=state.version,
        approver=APPROVER,
        verification_id="approval-proof",
        issued_at=NOW - 1,
        expires_at=NOW + 10,
    )


class PlanDataflowConditionTests(unittest.IsolatedAsyncioTestCase):
    def _conditional_policies(self):
        return {
            "balance.read": IntentPlanPolicy("balance.read"),
            "inventory.read": IntentPlanPolicy("inventory.read"),
            "order.create": IntentPlanPolicy(
                "order.create",
                requires_approval=True,
                write=True,
                replay_policy="never",
                approval_roles=("plan.approver",),
                parameter_contract=PlanParameterContract(
                    required={
                        "product_id": "string",
                        "quantity": "integer",
                        "price": "number",
                    }
                ),
                required_predecessor_intents=(
                    "balance.read",
                    "inventory.read",
                ),
                argument_bindings=(
                    PlanIntentArgumentBinding(
                        "price",
                        PlanIntentResultReference("inventory.read", ("price",)),
                    ),
                ),
                preconditions=(
                    PlanIntentCondition(
                        PlanIntentResultReference("balance.read", ("balance",)),
                        "gte",
                        expected_from=PlanIntentResultReference(
                            "inventory.read", ("price",)
                        ),
                    ),
                ),
            ),
        }

    async def _conditional_plan(self, policies):
        async def planner_fn(_request, _catalog):
            return {
                "planId": "conditional-order",
                "steps": [
                    {"stepId": "balance", "intent": "balance.read"},
                    {"stepId": "inventory", "intent": "inventory.read"},
                    {
                        "stepId": "create",
                        "intent": "order.create",
                        "arguments": {"product_id": "shoe", "quantity": 1},
                    },
                ],
            }

        return await HybridRequestPlanner(policies, planner_fn).plan("余额足够就下单")

    async def test_plan_roundtrip_keeps_binding_condition_and_action_hash(self):
        plan = await self._conditional_plan(self._conditional_policies())
        restored = MultiIntentPlan.from_dict(plan.to_dict())

        self.assertEqual(restored, plan)
        create = restored.step("create")
        self.assertEqual(set(create.depends_on), {"balance", "inventory"})
        self.assertEqual(create.argument_bindings[0].source.step_id, "inventory")
        self.assertEqual(create.preconditions[0].left.step_id, "balance")
        self.assertEqual(create.action_hash, plan.step("create").action_hash)

    async def test_durable_store_roundtrip_keeps_validation_state(self):
        contract = PlanResultContract((PlanResultRule(("ok",), "eq", True),))
        plan = MultiIntentPlan(
            "durable validation",
            (PlanStep("read", "read", result_contract=contract),),
            plan_id="durable-validation",
        )
        with tempfile.TemporaryDirectory() as directory:
            journal = SQLiteSessionEventJournal(
                Path(directory) / "state.sqlite3",
                key_provider=StaticJournalKeyProvider(
                    {"test": b"d" * 32}, active_key_id="test"
                ),
            )
            store = SessionJournalPlanStore(
                journal,
                JournalPrincipal.system("tenant"),
                session_id="session",
            )
            record = await store.initialize(plan)
            for event in (
                PlanEvent(1, "step_started", "read", {}),
                PlanEvent(
                    2,
                    "step_validation_passed",
                    "read",
                    {"resultDigest": "a" * 64},
                ),
                PlanEvent(3, "step_succeeded", "read", {"result": {"ok": True}}),
            ):
                record = await store.append_event(
                    plan.plan_id,
                    event,
                    expected_last_sequence=record.last_journal_sequence,
                )

            restored = await store.load(plan.plan_id)
            self.assertEqual(restored.state.phase, "completed")
            self.assertEqual(
                restored.state.steps["read"].validation_status, "passed"
            )
            self.assertEqual(restored.plan, plan)

    async def test_false_precondition_blocks_write_before_approval_and_dispatch(self):
        policies = self._conditional_policies()
        plan = await self._conditional_plan(policies)
        approvals = 0
        write_calls = 0

        async def authorize(step, state, _token):
            nonlocal approvals
            approvals += 1
            return PlanApprovalDecision.grant(step, issue_receipt(step, state))

        async def execute(step, _token, *, context):
            nonlocal write_calls
            if step.intent == "balance.read":
                return {"balance": 100}
            if step.intent == "inventory.read":
                return {"price": 200, "stock": 1}
            write_calls += 1
            return {"created": True}

        result = await PlanExecutor(
            plan,
            policies,
            execute,
            approval_barrier=ApprovalBarrier(
                authorize,
                receipt_consumer=ReceiptConsumer(),
                clock=lambda: NOW,
            ),
            clock=lambda: NOW,
        ).execute()

        self.assertEqual(approvals, 0)
        self.assertEqual(write_calls, 0)
        self.assertEqual(result.state.steps["create"].status, "not_applicable")
        self.assertIn(
            "step_not_applicable", [event.type for event in result.events]
        )

    async def test_true_precondition_dispatches_resolved_arguments_in_context(self):
        policies = self._conditional_policies()
        plan = await self._conditional_plan(policies)
        observed = None

        async def authorize(step, state, _token):
            return PlanApprovalDecision.grant(step, issue_receipt(step, state))

        async def execute(step, _token, *, context):
            nonlocal observed
            if step.intent == "balance.read":
                return {"balance": 400}
            if step.intent == "inventory.read":
                return {"price": 200, "stock": 1}
            observed = context
            return {"created": True}

        result = await PlanExecutor(
            plan,
            policies,
            execute,
            approval_barrier=ApprovalBarrier(
                authorize,
                receipt_consumer=ReceiptConsumer(),
                clock=lambda: NOW,
            ),
            fencing_token=7,
            fencing_scope="tenant:t:plan:conditional-order",
            clock=lambda: NOW,
        ).execute()

        self.assertEqual(result.state.phase, "completed")
        self.assertIsNotNone(observed)
        self.assertEqual(
            dict(observed.resolved_arguments),
            {"product_id": "shoe", "quantity": 1, "price": 200},
        )
        self.assertEqual(
            set(observed.dependency_results), {"balance", "inventory"}
        )
        self.assertIsNotNone(observed.approval_receipt)
        self.assertEqual(observed.fencing_token, 7)
        self.assertEqual(
            observed.fencing_scope, "tenant:t:plan:conditional-order"
        )

    async def test_result_contract_is_persisted_before_downstream_unlock(self):
        contract = PlanResultContract(
            (PlanResultRule(("ok",), "eq", True),)
        )
        policies = {
            "source": IntentPlanPolicy("source", result_contract=contract),
            "downstream": IntentPlanPolicy("downstream"),
        }
        plan = MultiIntentPlan(
            "validate then continue",
            (
                PlanStep("source", "source", result_contract=contract),
                PlanStep("downstream", "downstream", depends_on=("source",)),
            ),
        )
        persisted = []

        async def execute(step, _token, *, context):
            return {"ok": True, "step": step.step_id}

        async def persist(event):
            persisted.append((event.type, event.step_id))

        result = await PlanExecutor(
            plan,
            policies,
            execute,
            event_sink=persist,
        ).execute()

        self.assertEqual(result.state.phase, "completed")
        self.assertLess(
            persisted.index(("step_validation_passed", "source")),
            persisted.index(("step_started", "downstream")),
        )

    async def test_failed_postcondition_blocks_downstream(self):
        contract = PlanResultContract(
            (PlanResultRule(("ok",), "eq", True),)
        )
        policies = {
            "source": IntentPlanPolicy("source", result_contract=contract),
            "downstream": IntentPlanPolicy("downstream"),
        }
        plan = MultiIntentPlan(
            "invalid result",
            (
                PlanStep("source", "source", result_contract=contract),
                PlanStep("downstream", "downstream", depends_on=("source",)),
            ),
        )
        called = []

        async def execute(step, _token, *, context):
            called.append(step.step_id)
            return {"ok": False}

        result = await PlanExecutor(plan, policies, execute).execute()

        self.assertEqual(called, ["source"])
        self.assertEqual(result.state.steps["source"].status, "failed")
        self.assertEqual(result.state.steps["downstream"].status, "skipped")
        self.assertIn(
            "step_validation_failed", [event.type for event in result.events]
        )

    def test_direct_plan_missing_trusted_predecessor_fails_closed(self):
        policies = self._conditional_policies()
        create_policy = policies["order.create"]
        plan = MultiIntentPlan(
            "unsafe missing reads",
            (
                PlanStep(
                    "create",
                    "order.create",
                    arguments={"product_id": "shoe", "quantity": 1},
                    requires_approval=True,
                    write=True,
                    replay_policy="never",
                    approval_roles=create_policy.approval_roles,
                    parameter_contract=create_policy.parameter_contract,
                    required_predecessor_intents=(
                        "balance.read",
                        "inventory.read",
                    ),
                ),
            ),
        )

        with self.assertRaises(PlanValidationError):
            PlanValidator(policies).validate(plan)

    def test_independent_dangerous_steps_are_rejected_without_opt_in(self):
        empty = PlanParameterContract(allow_empty=True)
        policies = {
            name: IntentPlanPolicy(
                name,
                requires_approval=True,
                write=True,
                replay_policy="never",
                approval_roles=("plan.approver",),
                parameter_contract=empty,
            )
            for name in ("write.a", "write.b")
        }
        plan = MultiIntentPlan(
            "two writes",
            tuple(
                PlanStep(
                    name,
                    name,
                    requires_approval=True,
                    write=True,
                    replay_policy="never",
                    approval_roles=("plan.approver",),
                    parameter_contract=empty,
                )
                for name in policies
            ),
        )

        with self.assertRaisesRegex(PlanValidationError, "副作用 Step"):
            PlanValidator(policies).validate(plan)

    async def test_planner_serializes_dangerous_steps_from_trusted_policy(self):
        empty = PlanParameterContract(allow_empty=True)
        policies = {
            name: IntentPlanPolicy(
                name,
                requires_approval=True,
                write=True,
                replay_policy="never",
                approval_roles=("plan.approver",),
                parameter_contract=empty,
            )
            for name in ("write.a", "write.b")
        }

        async def planner_fn(_request, _catalog):
            return {
                "steps": [
                    {"stepId": "a", "intent": "write.a"},
                    {"stepId": "b", "intent": "write.b"},
                ]
            }

        plan = await HybridRequestPlanner(policies, planner_fn).plan("two writes")
        self.assertEqual(plan.step("b").depends_on, ("a",))


if __name__ == "__main__":
    unittest.main()
