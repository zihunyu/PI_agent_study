"""完整 Context、工具恢复、可信 Approval、幂等写操作测试。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import (  # noqa: E402
    Agent,
    ApprovalError,
    ApprovalService,
    CapabilityRegistry,
    DurableOperationRecorder,
    DurableSessionRecovery,
    IdentityClaim,
    IdentityVerificationError,
    InMemoryOperationEventStore,
    JsonlOperationEventStore,
    Model,
    ModelRequestPolicy,
    OutcomeUnknownToolError,
    RecoveryCallbacks,
    RequestDecision,
    RoutedAgent,
    ScriptedProvider,
    StaticIdentityVerifier,
    VerifiedIdentity,
    WriteOperationService,
    assistant_message,
    create_divide_tool,
    replay_operation,
)


class DurableSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.model = Model(id="durable-model", provider="fake", api="fake")

    async def start_operation(self, store, operation_id="operation-1"):
        await store.append(
            "operation_started",
            "session-1",
            operation_id,
            {"configuration": {"model": self.model.id}, "tools": []},
        )

    @staticmethod
    def no_tool_policy() -> ModelRequestPolicy:
        return ModelRequestPolicy.no_tools()

    async def test_recorder_持久化完整消息和工具执行事实(self) -> None:
        tool = create_divide_tool()
        provider = ScriptedProvider([
            assistant_message(
                model=self.model,
                stop_reason="toolUse",
                content=[{
                    "type": "toolCall",
                    "id": "divide-durable",
                    "name": "divide",
                    "arguments": {"a": 10, "b": 2},
                }],
            ),
            assistant_message(
                model=self.model,
                content=[{"type": "text", "text": "结果是 5"}],
            ),
        ])
        store = InMemoryOperationEventStore()
        recorder = DurableOperationRecorder(
            store,
            session_id="session-1",
            tools=[tool],
            configuration={"model": self.model.id},
        )
        agent = Agent(model=self.model, stream_fn=provider.stream, tools=[tool])
        agent.subscribe(recorder.listener)

        await agent.prompt("计算 10÷2")

        events = await store.load(operation_id=recorder.last_operation_id)
        state = replay_operation(events)
        self.assertEqual(state.phase, "completed")
        self.assertEqual(
            [message["role"] for message in state.messages],
            ["user", "assistant", "toolResult", "assistant"],
        )
        invocation = state.tools["divide-durable"]
        self.assertEqual(invocation.phase, "completed")
        self.assertEqual(invocation.attempts, 1)
        self.assertEqual(invocation.replay_policy, "safe")

    async def test_routed_agent_无模型结果也持久化完整_operation(self) -> None:
        class FixedRouter:
            def route(self, _text):
                return RequestDecision(
                    status="in_scope_capability_missing",
                    reason="缺少能力",
                    message="当前缺少能力",
                )

        store = InMemoryOperationEventStore()
        recorder = DurableOperationRecorder(
            store,
            session_id="session-routing",
            tools=[],
        )
        routed = RoutedAgent(
            Agent(
                model=self.model,
                stream_fn=ScriptedProvider([]).stream,
            ),
            FixedRouter(),
            CapabilityRegistry(),
            operation_recorder=recorder,
        )

        result = await routed.prompt("执行缺失能力")

        self.assertFalse(result.model_called)
        events = await store.load(operation_id=recorder.last_operation_id)
        state = replay_operation(events)
        self.assertEqual(state.phase, "completed")
        self.assertTrue(any(event.type == "routing_finished" for event in events))

    async def test_recovery_plan_识别未完成模型请求(self) -> None:
        store = InMemoryOperationEventStore()
        await self.start_operation(store)
        await store.append(
            "message_appended",
            "session-1",
            "operation-1",
            {"message": {"role": "user", "content": [{"type": "text", "text": "问题"}]}},
        )
        policy = self.no_tool_policy()
        await store.append(
            "model_policy_selected",
            "session-1",
            "operation-1",
            {"policy": policy.to_dict()},
        )
        await store.append(
            "model_request_started",
            "session-1",
            "operation-1",
            {
                "requestId": "request-1",
                "requestPolicy": policy.to_dict(),
            },
        )

        plan = await DurableSessionRecovery(store).plan(
            session_id="session-1",
            operation_id="operation-1",
        )

        self.assertEqual(plan.actions[0].kind, "retry_model_request")
        self.assertEqual(plan.actions[0].request_id, "request-1")
        self.assertEqual(plan.actions[0].request_policy, policy)

    async def test_recovery缺少模型策略时进入人工处理而不扩大工具(self) -> None:
        store = InMemoryOperationEventStore()
        await self.start_operation(store)
        await store.append(
            "message_appended",
            "session-1",
            "operation-1",
            {
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "问题"}],
                }
            },
        )
        await store.append(
            "model_request_started",
            "session-1",
            "operation-1",
            {"requestId": "legacy-without-policy"},
        )

        plan = await DurableSessionRecovery(store).plan(
            session_id="session-1",
            operation_id="operation-1",
        )

        self.assertEqual(plan.actions[0].kind, "manual_intervention")
        self.assertIn("缺少持久化请求策略", plan.actions[0].reason)

    async def test_recovery_plan_区分_safe_replay_和_reconcile(self) -> None:
        async def plan_for(replay_policy: str):
            store = InMemoryOperationEventStore()
            await self.start_operation(store)
            policy = ModelRequestPolicy(
                visible_tool_names=("write-or-read",),
                tool_choice="required",
                allowed_tool_names=("write-or-read",),
                expected_tool_arguments={"value": 1},
            )
            assistant = assistant_message(
                model=self.model,
                stop_reason="toolUse",
                content=[{
                    "type": "toolCall",
                    "id": "tool-1",
                    "name": "write-or-read",
                    "arguments": {"value": 1},
                }],
            )
            await store.append(
                "model_request_started",
                "session-1",
                "operation-1",
                {
                    "requestId": "request-1",
                    "requestPolicy": policy.to_dict(),
                },
            )
            await store.append(
                "model_request_completed",
                "session-1",
                "operation-1",
                {"requestId": "request-1", "message": assistant},
            )
            await store.append(
                "tool_intent_recorded",
                "session-1",
                "operation-1",
                {
                    "toolCallId": "tool-1",
                    "toolName": "write-or-read",
                    "arguments": {"value": 1},
                    "replayPolicy": replay_policy,
                    "securityContractDigest": "a" * 64,
                },
            )
            await store.append(
                "tool_dispatch_started",
                "session-1",
                "operation-1",
                {"toolCallId": "tool-1"},
            )
            return await DurableSessionRecovery(store).plan(
                session_id="session-1",
                operation_id="operation-1",
            )

        safe = await plan_for("safe")
        unsafe = await plan_for("never")
        self.assertEqual(safe.actions[0].kind, "replay_safe_tool")
        self.assertEqual(unsafe.actions[0].kind, "reconcile_tool")

    async def test_recovery缺少_tool_intent时_fail_closed(self) -> None:
        store = InMemoryOperationEventStore()
        await self.start_operation(store)
        policy = ModelRequestPolicy(
            visible_tool_names=("write-or-read",),
            tool_choice="required",
            allowed_tool_names=("write-or-read",),
            expected_tool_arguments={"value": 1},
        )
        assistant = assistant_message(
            model=self.model,
            stop_reason="toolUse",
            content=[
                {
                    "type": "toolCall",
                    "id": "missing-intent",
                    "name": "write-or-read",
                    "arguments": {"value": 1},
                }
            ],
        )
        await store.append(
            "model_request_started",
            "session-1",
            "operation-1",
            {"requestId": "request-1", "requestPolicy": policy.to_dict()},
        )
        await store.append(
            "model_request_completed",
            "session-1",
            "operation-1",
            {"requestId": "request-1", "message": assistant},
        )

        plan = await DurableSessionRecovery(store).plan(
            session_id="session-1",
            operation_id="operation-1",
        )

        self.assertEqual([action.kind for action in plan.actions], ["manual_intervention"])
        self.assertIn("缺少可信 Dispatch Intent", plan.actions[0].reason)
        self.assertFalse(any(action.kind == "execute_tool" for action in plan.actions))

    async def test_recovery_安全重放工具后继续模型并完成(self) -> None:
        store = InMemoryOperationEventStore()
        await self.start_operation(store)
        assistant = assistant_message(
            model=self.model,
            stop_reason="toolUse",
            content=[{
                "type": "toolCall",
                "id": "divide-recover",
                "name": "divide",
                "arguments": {"a": 10, "b": 2},
            }],
        )
        policy = ModelRequestPolicy(
            visible_tool_names=("divide",),
            tool_choice="required",
            allowed_tool_names=("divide",),
            expected_tool_arguments={"a": 10, "b": 2},
            continuation_policy=self.no_tool_policy(),
        )
        await store.append(
            "model_policy_selected",
            "session-1",
            "operation-1",
            {"policy": policy.to_dict()},
        )
        await store.append(
            "model_request_started",
            "session-1",
            "operation-1",
            {"requestId": "r1", "requestPolicy": policy.to_dict()},
        )
        await store.append(
            "model_request_completed",
            "session-1",
            "operation-1",
            {"requestId": "r1", "message": assistant},
        )
        await store.append(
            "tool_intent_recorded",
            "session-1",
            "operation-1",
            {
                "toolCallId": "divide-recover",
                "toolName": "divide",
                "arguments": {"a": 10, "b": 2},
                "replayPolicy": "safe",
                "securityContractDigest": "b" * 64,
            },
        )
        await store.append(
            "tool_dispatch_started",
            "session-1",
            "operation-1",
            {"toolCallId": "divide-recover"},
        )
        tool_calls = 0

        async def execute_tool(action):
            nonlocal tool_calls
            tool_calls += 1
            return {
                "role": "toolResult",
                "toolCallId": action.tool_call_id,
                "toolName": action.tool_name,
                "content": [{"type": "text", "text": "5.0"}],
                "details": {},
                "isError": False,
            }

        async def request_model(_messages, recovered_policy):
            self.assertEqual(recovered_policy, self.no_tool_policy())
            return assistant_message(
                model=self.model,
                content=[{"type": "text", "text": "结果是 5"}],
            )

        async def reconcile(_action):
            raise AssertionError("safe 工具不应进入 reconcile")

        result = await DurableSessionRecovery(store).resume(
            session_id="session-1",
            operation_id="operation-1",
            callbacks=RecoveryCallbacks(request_model, execute_tool, reconcile),
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(tool_calls, 1)
        self.assertEqual(result.operation.messages[-1]["role"], "assistant")

    async def identities(self):
        verifier = StaticIdentityVerifier({
            "requester": ("request-secret", {"operator", "approver"}),
            "approver": ("approve-secret", {"approver"}),
        })
        requester = await verifier.verify(IdentityClaim("requester", "request-secret"))
        approver = await verifier.verify(IdentityClaim("approver", "approve-secret"))
        return verifier, requester, approver

    async def test_可信身份验证拒绝错误凭证(self) -> None:
        verifier, requester, _approver = await self.identities()
        self.assertEqual(requester.principal_id, "requester")
        with self.assertRaises(IdentityVerificationError):
            await verifier.verify(IdentityClaim("requester", "wrong"))

    async def test_approval_角色_自审_操作绑定和一次性消费(self) -> None:
        store = InMemoryOperationEventStore()
        await self.start_operation(store)
        _verifier, requester, approver = await self.identities()
        service = ApprovalService(store)
        action = {"tool": "cancel_order", "arguments": {"order_id": "1001"}}
        approval = await service.request(
            session_id="session-1",
            operation_id="operation-1",
            requester=requester,
            action=action,
            action_summary="取消订单 1001",
            required_role="approver",
        )
        with self.assertRaisesRegex(ApprovalError, "自己的操作"):
            await service.grant(approval.approval_id, requester)
        approval = await service.grant(approval.approval_id, approver)
        self.assertEqual(approval.state, "approved")
        with self.assertRaisesRegex(ApprovalError, "不匹配"):
            await service.consume(
                approval.approval_id,
                action={"tool": "cancel_order", "arguments": {"order_id": "9999"}},
                consumer=requester,
            )
        approval = await service.consume(
            approval.approval_id,
            action=action,
            consumer=requester,
        )
        self.assertEqual(approval.state, "consumed")
        with self.assertRaises(ApprovalError):
            await service.consume(approval.approval_id, action=action, consumer=requester)

    async def test_approval拒绝duck对象和未经签发的身份(self) -> None:
        store = InMemoryOperationEventStore()
        await self.start_operation(store)
        _verifier, requester, approver = await self.identities()
        service = ApprovalService(store)
        action = {"tool": "cancel_order", "arguments": {"order_id": "1001"}}

        class DuckIdentity:
            principal_id = "forged-manager"
            roles = frozenset({"approver"})
            issuer = "forged"
            verification_id = "forged-verification"

        duck = DuckIdentity()
        with self.assertRaises(ApprovalError) as request_error:
            await service.request(
                session_id="session-1",
                operation_id="operation-1",
                requester=duck,  # type: ignore[arg-type]
                action=action,
                action_summary="取消订单 1001",
                required_role="approver",
            )
        self.assertEqual(request_error.exception.code, "verified_identity_required")

        directly_constructed = VerifiedIdentity(
            principal_id="forged-manager",
            roles=frozenset({"approver"}),
            issuer="forged",
            verification_id="forged-verification",
        )
        with self.assertRaises(ApprovalError) as provenance_error:
            await service.request(
                session_id="session-1",
                operation_id="operation-1",
                requester=directly_constructed,
                action=action,
                action_summary="取消订单 1001",
                required_role="approver",
            )
        self.assertEqual(
            provenance_error.exception.code,
            "identity_provenance_invalid",
        )

        approval = await service.request(
            session_id="session-1",
            operation_id="operation-1",
            requester=requester,
            action=action,
            action_summary="取消订单 1001",
            required_role="approver",
        )
        for transition in (
            lambda: service.grant(approval.approval_id, duck),
            lambda: service.reject(
                approval.approval_id,
                duck,
                reason="forged",
            ),
        ):
            with self.assertRaises(ApprovalError) as transition_error:
                await transition()  # type: ignore[misc]
            self.assertEqual(
                transition_error.exception.code,
                "verified_identity_required",
            )

        await service.grant(approval.approval_id, approver)
        with self.assertRaises(ApprovalError) as consume_error:
            await service.consume(
                approval.approval_id,
                action=action,
                consumer=duck,  # type: ignore[arg-type]
            )
        self.assertEqual(consume_error.exception.code, "verified_identity_required")

    async def test_approval支持注入生产身份来源验证器(self) -> None:
        store = InMemoryOperationEventStore()
        await self.start_operation(store)
        requester = VerifiedIdentity(
            principal_id="oidc-operator",
            roles=frozenset({"operator"}),
            issuer="enterprise-oidc",
            verification_id="oidc-verification-1",
        )
        approver = VerifiedIdentity(
            principal_id="oidc-approver",
            roles=frozenset({"approver"}),
            issuer="enterprise-oidc",
            verification_id="oidc-verification-2",
        )
        issued_objects = {id(requester): requester, id(approver): approver}

        def validate_runtime_issuance(identity: VerifiedIdentity) -> bool:
            return issued_objects.get(id(identity)) is identity

        service = ApprovalService(
            store,
            identity_validator=validate_runtime_issuance,
        )
        action = {"tool": "cancel_order", "arguments": {"order_id": "1001"}}
        approval = await service.request(
            session_id="session-1",
            operation_id="operation-1",
            requester=requester,
            action=action,
            action_summary="取消订单 1001",
            required_role="approver",
        )
        await service.grant(approval.approval_id, approver)
        consumed = await service.consume(
            approval.approval_id,
            action=action,
            consumer=requester,
        )
        self.assertEqual(consumed.state, "consumed")

    async def test_write_审批_幂等_执行和重复请求去重(self) -> None:
        store = InMemoryOperationEventStore()
        await self.start_operation(store)
        _verifier, requester, approver = await self.identities()
        approvals = ApprovalService(store)
        writes = WriteOperationService(store, approvals)
        write = await writes.prepare(
            session_id="session-1",
            operation_id="operation-1",
            tool_name="cancel_order",
            arguments={"order_id": "1001"},
            idempotency_key="idem-1",
            requester=requester,
            requires_approval=True,
        )
        self.assertEqual(write.state, "waiting_approval")
        with self.assertRaises(ApprovalError):
            await writes.execute(
                write.write_id,
                actor=requester,
                idempotency_key="idem-1",
                handler=lambda *_args: None,
            )
        await approvals.grant(write.approval_id, approver)
        calls = 0

        async def handler(arguments, idempotency_key, actor):
            nonlocal calls
            calls += 1
            self.assertEqual(idempotency_key, "idem-1")
            self.assertEqual(actor.principal_id, "requester")
            return {"status": "cancelled", "orderId": arguments["order_id"]}

        write = await writes.execute(
            write.write_id,
            actor=requester,
            idempotency_key="idem-1",
            handler=handler,
        )
        duplicate = await writes.prepare(
            session_id="session-1",
            operation_id="operation-1",
            tool_name="cancel_order",
            arguments={"order_id": "1001"},
            idempotency_key="idem-1",
            requester=requester,
            requires_approval=True,
        )
        duplicate = await writes.execute(
            duplicate.write_id,
            actor=requester,
            idempotency_key="idem-1",
            handler=handler,
        )
        self.assertEqual(write.state, "succeeded")
        self.assertEqual(duplicate.write_id, write.write_id)
        self.assertEqual(calls, 1)

    async def test_write_outcome_unknown_通过核对完成(self) -> None:
        store = InMemoryOperationEventStore()
        await self.start_operation(store)
        _verifier, requester, _approver = await self.identities()
        writes = WriteOperationService(store, ApprovalService(store))
        write = await writes.prepare(
            session_id="session-1",
            operation_id="operation-1",
            tool_name="write_external",
            arguments={"value": "x"},
            idempotency_key="idem-unknown",
            requester=requester,
            requires_approval=False,
        )

        async def uncertain(*_args):
            raise OutcomeUnknownToolError(
                "结果不确定",
                operation_id="external-1",
                idempotency_key="idem-unknown",
                reconciliation_name="check_external",
            )

        with self.assertRaises(OutcomeUnknownToolError):
            await writes.execute(
                write.write_id,
                actor=requester,
                idempotency_key="idem-unknown",
                handler=uncertain,
            )
        self.assertEqual((await writes.get(write.write_id)).state, "outcome_unknown")

        async def reconcile(_record):
            return {"status": "succeeded", "externalId": "external-1"}

        write = await writes.reconcile(write.write_id, reconcile)
        self.assertEqual(write.state, "succeeded")

    async def test_jsonl_store_重启后仍可恢复消息_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operation.jsonl"
            store = JsonlOperationEventStore(path)
            await self.start_operation(store)
            await store.append(
                "model_policy_selected",
                "session-1",
                "operation-1",
                {"policy": self.no_tool_policy().to_dict()},
            )
            await store.append(
                "message_appended",
                "session-1",
                "operation-1",
                {"message": {"role": "user", "content": [{"type": "text", "text": "恢复我"}]}},
            )
            restarted = JsonlOperationEventStore(path)
            plan = await DurableSessionRecovery(restarted).plan(
                session_id="session-1",
                operation_id="operation-1",
            )
            self.assertEqual(plan.operation.messages[0]["role"], "user")
            self.assertEqual(plan.actions[0].kind, "continue_model")


if __name__ == "__main__":
    unittest.main()
