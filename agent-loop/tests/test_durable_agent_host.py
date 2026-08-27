"""P1 DurableAgentHost、Recovery Runtime 和 Approval Resume 测试。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import (  # noqa: E402
    AgentTool,
    AgentToolResult,
    ApprovalResumeCoordinator,
    ApprovalService,
    CapabilityRegistry,
    DurableAgentHost,
    IdentityClaim,
    InMemoryOperationEventStore,
    Model,
    ModelRequestPolicy,
    RecoverableModelRuntime,
    RecoverableToolRuntime,
    RecoveryAction,
    RecoveryCallbacks,
    RequestDecision,
    ScriptedProvider,
    SQLiteOperationEventStore,
    StartupRecoveryCoordinator,
    StaticIdentityVerifier,
    VerifiedIdentity,
    assistant_message,
    create_add_tool,
    create_divide_tool,
    replay_operation,
)


class FixedRouter:
    def __init__(self, decision: RequestDecision) -> None:
        self.decision = decision

    def route(self, _text: str) -> RequestDecision:
        return self.decision


class DurableAgentHostTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.model = Model(id="host-model", provider="fake", api="fake")

    async def identities(self) -> tuple[VerifiedIdentity, VerifiedIdentity]:
        verifier = StaticIdentityVerifier({
            "operator": ("operator-secret", {"operator"}),
            "approver": ("approver-secret", {"approver"}),
        })
        operator = await verifier.verify(IdentityClaim("operator", "operator-secret"))
        approver = await verifier.verify(IdentityClaim("approver", "approver-secret"))
        return operator, approver

    async def test_recoverable_model_runtime_严格恢复持久请求策略(self) -> None:
        captured_options: dict = {}

        def response(_context, options):
            captured_options.update(options)
            return assistant_message(
                model=self.model,
                stop_reason="toolUse",
                content=[{
                    "type": "toolCall",
                    "id": "divide-policy-call",
                    "name": "divide",
                    "arguments": {"a": 10, "b": 2},
                }],
            )

        provider = ScriptedProvider([response])
        runtime = RecoverableModelRuntime(
            model=self.model,
            stream_fn=provider.stream,
            system_prompt="恢复测试",
            tools=[create_divide_tool(), create_add_tool()],
        )
        policy = ModelRequestPolicy(
            visible_tool_names=("divide",),
            tool_choice="required",
            required_capabilities=("calculator.divide",),
            allowed_tool_names=("divide",),
            expected_tool_arguments={"a": 10, "b": 2},
            continuation_policy=ModelRequestPolicy.no_tools(),
        )

        result = await runtime.request(
            [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "继续"}],
                }
            ],
            policy=policy,
        )

        self.assertEqual(result["content"][0]["name"], "divide")
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(
            [tool["name"] for tool in provider.contexts[0]["tools"]],
            ["divide"],
        )
        self.assertEqual(captured_options["tool_choice"], "required")
        self.assertEqual(
            captured_options["allowed_tool_names"],
            ["divide"],
        )
        self.assertEqual(
            captured_options["expected_tool_arguments"],
            {"a": 10, "b": 2},
        )

    async def test_recoverable_tool_runtime_复用参数校验_timeout_retry管线(self) -> None:
        runtime = RecoverableToolRuntime(
            model=self.model,
            tools=[create_divide_tool()],
        )
        result = await runtime.execute(
            RecoveryAction(
                kind="replay_safe_tool",
                tool_call_id="divide-recovery",
                tool_name="divide",
                arguments={"a": 10, "b": 2},
            )
        )
        self.assertFalse(result["isError"])
        self.assertEqual(result["content"][0]["text"], "5.0")

    async def test_recoverable_tool_runtime_拒绝未授权_never_tool(self) -> None:
        async def write(_id, _args, _token, _update):
            return AgentToolResult(content=[], details={})

        tool = AgentTool(
            name="dangerous_write",
            label="危险写操作",
            description="需要审批",
            execute=write,
            execution_mode="exclusive",
            replay_policy="never",
        )
        runtime = RecoverableToolRuntime(model=self.model, tools=[tool])
        with self.assertRaisesRegex(PermissionError, "Approval"):
            await runtime.execute(
                RecoveryAction(
                    kind="execute_tool",
                    tool_call_id="write-1",
                    tool_name="dangerous_write",
                )
            )

    async def test_startup_recovery_扫描并完成未结束_operation(self) -> None:
        store = InMemoryOperationEventStore()
        await store.append(
            "operation_started",
            "session-1",
            "operation-1",
            {"configuration": {}, "tools": []},
        )
        policy = ModelRequestPolicy.no_tools()
        await store.append(
            "model_policy_selected",
            "session-1",
            "operation-1",
            {"policy": policy.to_dict()},
        )
        await store.append(
            "message_appended",
            "session-1",
            "operation-1",
            {"message": {"role": "user", "content": [{"type": "text", "text": "继续"}]}},
        )

        async def request_model(_messages, recovered_policy):
            self.assertEqual(recovered_policy, policy)
            return assistant_message(
                model=self.model,
                content=[{"type": "text", "text": "恢复完成"}],
            )

        async def unexpected(_action):
            raise AssertionError("不应执行工具")

        coordinator = StartupRecoveryCoordinator(
            store,
            RecoveryCallbacks(request_model, unexpected, unexpected),
        )
        report = await coordinator.recover_all()

        self.assertEqual(report.completed, ("operation-1",))
        state = replay_operation(
            await store.load(session_id="session-1", operation_id="operation-1")
        )
        self.assertEqual(state.phase, "completed")
        self.assertEqual(state.messages[-1]["content"][0]["text"], "恢复完成")

    async def test_approval_resume_coordinator_批准后恢复_payload(self) -> None:
        store = InMemoryOperationEventStore()
        await store.append(
            "operation_started",
            "session-1",
            "operation-1",
            {"configuration": {}, "tools": []},
        )
        operator, approver = await self.identities()
        approvals = ApprovalService(store)
        coordinator = ApprovalResumeCoordinator(store, approvals)
        pending = await coordinator.request(
            session_id="session-1",
            operation_id="operation-1",
            requester=operator,
            action={"tool": "cancel", "arguments": {"id": "1"}},
            action_summary="取消 1",
            required_role="approver",
            resume_payload={"value": "resume-me"},
        )
        payloads: list[dict] = []

        async def resume(payload):
            payloads.append(payload)
            return "resumed"

        result = await coordinator.approve_and_resume(
            pending.approval.approval_id,
            approver=approver,
            consumer=operator,
            resume=resume,
        )

        self.assertEqual(result, "resumed")
        self.assertEqual(payloads, [{"value": "resume-me"}])
        self.assertEqual(
            (await approvals.get(pending.approval.approval_id)).state,
            "consumed",
        )

    async def test_approval_resume_进程中断后可继续已消费审批(self) -> None:
        store = InMemoryOperationEventStore()
        await store.append(
            "operation_started",
            "session-1",
            "operation-1",
            {"configuration": {}, "tools": []},
        )
        operator, approver = await self.identities()
        approvals = ApprovalService(store)
        coordinator = ApprovalResumeCoordinator(store, approvals)
        pending = await coordinator.request(
            session_id="session-1",
            operation_id="operation-1",
            requester=operator,
            action={"tool": "write", "arguments": {}},
            action_summary="恢复写操作",
            required_role="approver",
            resume_payload={"operation": "resume"},
        )
        await approvals.grant(pending.approval.approval_id, approver)
        await approvals.consume(
            pending.approval.approval_id,
            action=pending.action,
            consumer=operator,
        )
        await store.append(
            "approval_resume_started",
            "session-1",
            "operation-1",
            {"approvalId": pending.approval.approval_id},
        )
        payloads: list[dict] = []

        async def resume(payload):
            payloads.append(payload)
            return "ok"

        recovered = await coordinator.recover_incomplete(resume)
        self.assertEqual(recovered, [pending.approval.approval_id])
        self.assertEqual(payloads, [{"operation": "resume"}])

    async def test_durable_agent_host_自动装配普通_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider([
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "Host 回答"}],
                )
            ])
            host = await DurableAgentHost.create(
                session_id="host-session",
                state_dir=directory,
                model=self.model,
                stream_fn=provider.stream,
                system_prompt="Host 测试",
                tools=[create_divide_tool()],
            )

            await host.prompt("你好")
            await host.close()

            self.assertEqual(host.agent.state.messages[-1]["content"][0]["text"], "Host 回答")
            self.assertEqual(host.runtime_tracker.state.phase, "completed")
            self.assertIsNotNone(host.operation_recorder.last_operation_id)
            self.assertIsNotNone(host.startup_recovery_report)
            self.assertIsInstance(
                host.operation_store,
                SQLiteOperationEventStore,
            )

    async def test_host_approval_缺少可信申请人时安全结束_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            async def execute(_id, _args, _token, _update):
                return AgentToolResult(content=[], details={})

            tool = AgentTool(
                name="write",
                label="写操作",
                description="需要审批",
                execute=execute,
                execution_mode="exclusive",
                replay_policy="never",
            )
            capabilities = CapabilityRegistry()
            capabilities.register(
                tool,
                capabilities={"demo.write"},
                domain="demo",
                operation="write",
                requires_approval=True,
            )
            decision = RequestDecision(
                status="in_scope_approval_required",
                reason="需要审批",
                message="等待审批",
                selected_tools=("write",),
                requires_approval=True,
            )
            host = await DurableAgentHost.create(
                session_id="missing-identity",
                state_dir=directory,
                model=self.model,
                stream_fn=ScriptedProvider([]).stream,
                system_prompt="测试",
                tools=[tool],
                router=FixedRouter(decision),
                capabilities=capabilities,
            )

            with self.assertRaisesRegex(PermissionError, "VerifiedIdentity"):
                await host.prompt("执行写操作")

            self.assertEqual(host.runtime_tracker.state.phase, "failed")
            self.assertIsNone(host.operation_recorder.operation_id)

    async def test_host_approval_批准后执行幂等写并继续模型(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            async def never_called_directly(_id, _args, _token, _update):
                raise AssertionError("写工具必须由 WriteOperationService 执行")

            write_tool = AgentTool(
                name="cancel_order",
                label="取消订单",
                description="取消订单",
                parameters={"type": "object"},
                execute=never_called_directly,
                execution_mode="exclusive",
                replay_policy="never",
            )
            capabilities = CapabilityRegistry()
            capabilities.register(
                write_tool,
                capabilities={"orders.cancel"},
                domain="orders",
                operation="write",
                requires_approval=True,
            )
            decision = RequestDecision(
                status="in_scope_approval_required",
                reason="需要审批",
                message="等待审批",
                domain="orders",
                intent="order.cancel",
                extracted_fields={"order_id": "1001"},
                required_capabilities=("orders.cancel",),
                selected_tools=("cancel_order",),
                requires_approval=True,
            )
            provider = ScriptedProvider([
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "订单已取消"}],
                )
            ])
            host = await DurableAgentHost.create(
                session_id="approval-host",
                state_dir=directory,
                model=self.model,
                stream_fn=provider.stream,
                system_prompt="审批恢复测试",
                tools=[write_tool],
                router=FixedRouter(decision),
                capabilities=capabilities,
            )
            operator, approver = await self.identities()
            pending = await host.prompt("取消订单 1001", requester=operator)
            self.assertIsNotNone(pending.approval_id)
            self.assertEqual(host.runtime_tracker.state.phase, "waiting_approval")
            calls = 0

            async def handler(arguments, _key, actor):
                nonlocal calls
                calls += 1
                return {
                    "status": "cancelled",
                    "orderId": arguments["order_id"],
                    "actor": actor.principal_id,
                }

            final = await host.approve_and_resume(
                pending.approval_id,
                approver=approver,
                consumer=operator,
                idempotency_key="cancel-1001",
                write_handler=handler,
            )

            self.assertEqual(calls, 1)
            self.assertEqual(final["content"][0]["text"], "订单已取消")
            self.assertEqual(host.runtime_tracker.state.phase, "completed")
            events = await host.operation_store.load(
                operation_id=pending.operation_id
            )
            operation = replay_operation(events)
            self.assertEqual(operation.phase, "completed")
            self.assertEqual(
                [message["role"] for message in operation.messages],
                ["user", "assistant", "toolResult", "assistant"],
            )
            self.assertEqual(provider.contexts[0]["tools"], [])

    async def test_host_approval_最终模型_tool_call_被拒绝且日志仍可重放(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            async def never_called_directly(_id, _args, _token, _update):
                raise AssertionError("写工具必须由 WriteOperationService 执行")

            write_tool = AgentTool(
                name="refund_order",
                label="退款",
                description="退款订单",
                parameters={"type": "object"},
                execute=never_called_directly,
                execution_mode="exclusive",
                replay_policy="never",
            )
            divide = create_divide_tool()
            capabilities = CapabilityRegistry()
            capabilities.register(
                write_tool,
                capabilities={"orders.refund"},
                domain="orders",
                operation="write",
                requires_approval=True,
            )
            decision = RequestDecision(
                status="in_scope_approval_required",
                reason="需要审批",
                message="等待审批",
                intent="order.refund",
                extracted_fields={"order_id": "1001"},
                required_capabilities=("orders.refund",),
                selected_tools=("refund_order",),
                requires_approval=True,
            )
            provider = ScriptedProvider([
                assistant_message(
                    model=self.model,
                    stop_reason="toolUse",
                    content=[{
                        "type": "toolCall",
                        "id": "unexpected-call",
                        "name": "divide",
                        "arguments": {"a": 10, "b": 2},
                    }],
                )
            ])
            host = await DurableAgentHost.create(
                session_id="approval-final-closure",
                state_dir=directory,
                model=self.model,
                stream_fn=provider.stream,
                system_prompt="审批闭合测试",
                tools=[write_tool, divide],
                router=FixedRouter(decision),
                capabilities=capabilities,
            )
            operator, approver = await self.identities()
            pending = await host.prompt("退款订单 1001", requester=operator)
            write_calls = 0

            async def handler(arguments, _key, _actor):
                nonlocal write_calls
                write_calls += 1
                return {"status": "refunded", "orderId": arguments["order_id"]}

            with self.assertRaisesRegex(RuntimeError, "禁止使用工具"):
                await host.approve_and_resume(
                    pending.approval_id,
                    approver=approver,
                    consumer=operator,
                    idempotency_key="refund-1001",
                    write_handler=handler,
                )

            self.assertEqual(write_calls, 1)
            events = await host.operation_store.load(
                operation_id=pending.operation_id
            )
            operation = replay_operation(events)
            self.assertEqual(operation.phase, "failed")
            self.assertFalse(
                any(
                    event.type == "operation_finished"
                    and event.data.get("outcome") == "completed"
                    for event in events
                )
            )
            self.assertEqual(provider.contexts[0]["tools"], [])


if __name__ == "__main__":
    unittest.main()
