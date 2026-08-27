"""Operation Reducer/Recovery Planner 原生理解 Approval 和 Write 状态测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import (  # noqa: E402
    ApprovalResumeCoordinator,
    ApprovalService,
    DurableSessionRecovery,
    IdentityClaim,
    InMemoryOperationEventStore,
    Model,
    ModelRequestPolicy,
    OperationLogInvariantError,
    OutcomeUnknownToolError,
    RecoveryCallbacks,
    StartupRecoveryCoordinator,
    StaticIdentityVerifier,
    WriteOperationService,
    assistant_message,
    replay_operation,
)


class ApprovalAwareRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.model = Model(id="approval-aware", provider="fake", api="fake")

    async def identities(self):
        verifier = StaticIdentityVerifier({
            "operator": ("operator-secret", {"operator"}),
            "manager": ("manager-secret", {"approver"}),
        })
        operator = await verifier.verify(
            IdentityClaim("operator", "operator-secret")
        )
        manager = await verifier.verify(
            IdentityClaim("manager", "manager-secret")
        )
        return operator, manager

    async def approval_case(self):
        store = InMemoryOperationEventStore()
        sid, oid = "session-1", "operation-1"
        await store.append(
            "operation_started",
            sid,
            oid,
            {"configuration": {}, "tools": [{"name": "refund", "replayPolicy": "never"}]},
        )
        await store.append(
            "message_appended",
            sid,
            oid,
            {"message": {"role": "user", "content": [{"type": "text", "text": "退款"}]}},
        )
        assistant = assistant_message(
            model=self.model,
            stop_reason="toolUse",
            content=[{
                "type": "toolCall",
                "id": "refund-call",
                "name": "refund",
                "arguments": {"order_id": "1001"},
            }],
        )
        policy = ModelRequestPolicy(
            visible_tool_names=("refund",),
            tool_choice="required",
            allowed_tool_names=("refund",),
            expected_tool_arguments={"order_id": "1001"},
            continuation_policy=ModelRequestPolicy.no_tools(),
        )
        await store.append(
            "model_policy_selected",
            sid,
            oid,
            {"policy": policy.to_dict()},
        )
        await store.append(
            "model_request_started",
            sid,
            oid,
            {"requestId": "r1", "requestPolicy": policy.to_dict()},
        )
        await store.append(
            "model_request_completed",
            sid,
            oid,
            {"requestId": "r1", "message": assistant},
        )
        operator, manager = await self.identities()
        approvals = ApprovalService(store)
        coordinator = ApprovalResumeCoordinator(store, approvals)
        pending = await coordinator.request(
            session_id=sid,
            operation_id=oid,
            requester=operator,
            action={"tool": "refund", "arguments": {"order_id": "1001"}},
            action_summary="退款订单 1001",
            required_role="approver",
            resume_payload={
                "toolCallId": "refund-call",
                "toolName": "refund",
                "arguments": {"order_id": "1001"},
            },
        )
        return store, approvals, pending, operator, manager

    @staticmethod
    def callbacks(request_model=None, execute_tool=None, reconcile_tool=None):
        async def unexpected_model(_messages, _policy):
            raise AssertionError("该 Model Callback 不应执行")

        async def unexpected_action(_value):
            raise AssertionError("该 Tool Callback 不应执行")

        return RecoveryCallbacks(
            request_model=request_model or unexpected_model,
            execute_tool=execute_tool or unexpected_action,
            reconcile_tool=reconcile_tool or unexpected_action,
        )

    async def test_waiting_approval_计划等待而不是_execute_tool(self) -> None:
        store, _approvals, pending, _operator, _manager = await self.approval_case()
        plan = await DurableSessionRecovery(store).plan(
            session_id="session-1",
            operation_id="operation-1",
        )
        self.assertEqual(plan.operation.phase, "waiting_approval")
        self.assertEqual(plan.actions[0].kind, "wait_for_approval")
        self.assertEqual(plan.actions[0].approval_id, pending.approval.approval_id)
        self.assertNotIn("execute_tool", [action.kind for action in plan.actions])

    async def test_direct_recovery_waiting_approval_不会调用工具(self) -> None:
        store, _approvals, _pending, _operator, _manager = await self.approval_case()
        tool_calls = 0

        async def execute(_action):
            nonlocal tool_calls
            tool_calls += 1
            return {}

        result = await DurableSessionRecovery(store).resume(
            session_id="session-1",
            operation_id="operation-1",
            callbacks=self.callbacks(execute_tool=execute),
        )
        self.assertEqual(result.status, "waiting_approval")
        self.assertEqual(tool_calls, 0)

    async def test_approved_缺少_consumer_返回明确状态(self) -> None:
        store, approvals, pending, _operator, manager = await self.approval_case()
        await approvals.grant(pending.approval.approval_id, manager)
        plan = await DurableSessionRecovery(store).plan(
            session_id="session-1",
            operation_id="operation-1",
        )
        self.assertEqual(plan.actions[0].kind, "consume_approval")
        result = await DurableSessionRecovery(store).resume(
            session_id="session-1",
            operation_id="operation-1",
            callbacks=self.callbacks(),
        )
        self.assertEqual(result.status, "approval_consumer_required")

    async def test_consumed_approval_计划_resume_approved_write(self) -> None:
        store, approvals, pending, operator, manager = await self.approval_case()
        await approvals.grant(pending.approval.approval_id, manager)
        await approvals.consume(
            pending.approval.approval_id,
            action=pending.action,
            consumer=operator,
        )
        plan = await DurableSessionRecovery(store).plan(
            session_id="session-1",
            operation_id="operation-1",
        )
        self.assertEqual(plan.actions[0].kind, "resume_approved_write")
        self.assertEqual(plan.actions[0].tool_call_id, "refund-call")

    async def test_rejected_approval_生成_denied_result_并完成(self) -> None:
        store, approvals, pending, _operator, manager = await self.approval_case()
        await approvals.reject(
            pending.approval.approval_id,
            manager,
            reason="经理拒绝",
        )

        async def request_model(_messages, policy):
            self.assertEqual(policy, ModelRequestPolicy.no_tools())
            return assistant_message(
                model=self.model,
                content=[{"type": "text", "text": "退款未执行"}],
            )

        result = await DurableSessionRecovery(store).resume(
            session_id="session-1",
            operation_id="operation-1",
            callbacks=self.callbacks(request_model=request_model),
        )
        self.assertEqual(result.status, "completed")
        tool_result = next(
            message
            for message in result.operation.messages
            if message.get("role") == "toolResult"
        )
        self.assertEqual(tool_result["details"]["code"], "approval_not_granted")

    async def test_write_outcome_unknown_优先计划_reconcile_write(self) -> None:
        store = InMemoryOperationEventStore()
        await store.append(
            "operation_started",
            "session-1",
            "operation-1",
            {"configuration": {}, "tools": []},
        )
        operator, _manager = await self.identities()
        writes = WriteOperationService(store, ApprovalService(store))
        write = await writes.prepare(
            session_id="session-1",
            operation_id="operation-1",
            tool_name="refund",
            arguments={"order_id": "1001"},
            idempotency_key="refund-1001",
            requester=operator,
            requires_approval=False,
            tool_call_id="refund-call",
        )

        async def uncertain(*_args):
            raise OutcomeUnknownToolError(
                "结果未知",
                operation_id="external-1",
                idempotency_key="refund-1001",
                reconciliation_name="check_refund",
            )

        with self.assertRaises(OutcomeUnknownToolError):
            await writes.execute(
                write.write_id,
                actor=operator,
                idempotency_key="refund-1001",
                handler=uncertain,
            )
        plan = await DurableSessionRecovery(store).plan(
            session_id="session-1",
            operation_id="operation-1",
        )
        self.assertEqual(plan.actions[0].kind, "reconcile_write")
        self.assertEqual(plan.actions[0].write_id, write.write_id)

    async def test_waiting_approval_禁止_tool_dispatch(self) -> None:
        store, _approvals, _pending, _operator, _manager = await self.approval_case()
        await store.append(
            "tool_intent_recorded",
            "session-1",
            "operation-1",
            {
                "toolCallId": "refund-call",
                "toolName": "refund",
                "arguments": {"order_id": "1001"},
                "replayPolicy": "never",
            },
        )
        await store.append(
            "tool_dispatch_started",
            "session-1",
            "operation-1",
            {"toolCallId": "refund-call"},
        )
        with self.assertRaisesRegex(OperationLogInvariantError, "等待 Approval"):
            replay_operation(await store.load())

    async def test_approval_action_hash_与_tool_call_不匹配会拒绝(self) -> None:
        store = InMemoryOperationEventStore()
        sid, oid = "session-1", "operation-1"
        await store.append("operation_started", sid, oid, {"configuration": {}, "tools": []})
        operator, _manager = await self.identities()
        approval = await ApprovalService(store).request(
            session_id=sid,
            operation_id=oid,
            requester=operator,
            action={"tool": "refund", "arguments": {"order_id": "1001"}},
            action_summary="退款",
            required_role="approver",
        )
        await store.append(
            "approval_resume_registered",
            sid,
            oid,
            {
                "approvalId": approval.approval_id,
                "action": {"tool": "refund", "arguments": {"order_id": "9999"}},
                "resumePayload": {"toolCallId": "refund-call"},
            },
        )
        with self.assertRaisesRegex(OperationLogInvariantError, "Action Hash"):
            replay_operation(await store.load())

    async def test_startup_recovery_报告_waiting_approval_而非_failed(self) -> None:
        store, _approvals, _pending, _operator, _manager = await self.approval_case()
        coordinator = StartupRecoveryCoordinator(store, self.callbacks())
        report = await coordinator.recover_all()
        self.assertEqual(report.waiting_approval, ("operation-1",))
        self.assertEqual(report.failed, ())

    async def test_never_tool_即使授权回调过宽_planner_也不会_execute(self) -> None:
        store, _approvals, _pending, _operator, _manager = await self.approval_case()
        plan = await DurableSessionRecovery(store).plan(
            session_id="session-1",
            operation_id="operation-1",
        )
        # Planner 在 RecoverableToolRuntime 之前就阻止了 execute_tool。
        self.assertEqual(plan.actions[0].kind, "wait_for_approval")
        self.assertNotIn("execute_tool", [action.kind for action in plan.actions])


if __name__ == "__main__":
    unittest.main()
