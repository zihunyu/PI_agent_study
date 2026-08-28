"""P1 DurableAgentHost、Recovery Runtime 和 Approval Resume 测试。"""

from __future__ import annotations

import asyncio
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
    AssistantMessageEventStream,
    CapabilityRegistry,
    DurableActionEnvelope,
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
    SessionJournalOperationEventStore,
    StartupRecoveryCoordinator,
    StaticIdentityVerifier,
    VerifiedIdentity,
    WriteOperationService,
    assistant_message,
    create_add_tool,
    create_divide_tool,
    replay_operation,
)


async def _async_result(value):
    return value


async def _execute_and_materialize(
    store,
    approvals,
    envelope,
    *,
    operator,
    approval_id,
    idempotency_key,
):
    write = await WriteOperationService(store, approvals).execute(
        envelope.write_id,
        actor=operator,
        idempotency_key=idempotency_key,
        handler=lambda _arguments, _key, _actor: _async_result(
            {"status": "succeeded"}
        ),
        approval_resume_id=approval_id,
    )
    operation = replay_operation(
        await store.load(operation_id=envelope.operation_id)
    )
    specs = []
    if operation.tools[envelope.tool_call_id].phase != "completed":
        specs.append(
            (
                "tool_completed",
                {
                    "toolCallId": envelope.tool_call_id,
                    "result": {
                        "content": [],
                        "details": write.result or {},
                        "isError": False,
                    },
                },
            )
        )
    if not any(
        message.get("role") == "toolResult"
        and message.get("toolCallId") == envelope.tool_call_id
        for message in operation.messages
    ):
        specs.append(
            (
                "message_appended",
                {
                    "message": {
                        "role": "toolResult",
                        "toolCallId": envelope.tool_call_id,
                        "toolName": envelope.tool_name,
                        "content": [],
                        "details": write.result or {},
                        "isError": False,
                    }
                },
            )
        )
    if specs:
        await store.append_batch(
            "session-1",
            envelope.operation_id,
            specs,
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
        envelope = DurableActionEnvelope(
            operation_id="operation-1",
            tool_call_id="cancel-call",
            tool_name="cancel",
            arguments={"id": "1"},
            write_id="cancel-write",
        )
        pending = await coordinator.request(
            session_id="session-1",
            operation_id="operation-1",
            requester=operator,
            action=envelope.to_dict(),
            action_summary="取消 1",
            required_role="approver",
            resume_payload={},
            idempotency_key="cancel-write-key",
        )
        payloads: list[dict] = []

        async def resume(payload):
            payloads.append(payload)
            restored = DurableActionEnvelope.from_dict(payload["envelope"])
            await _execute_and_materialize(
                store,
                approvals,
                restored,
                operator=operator,
                approval_id=pending.approval.approval_id,
                idempotency_key="cancel-write-key",
            )
            return "resumed"

        result = await coordinator.approve_and_resume(
            pending.approval.approval_id,
            approver=approver,
            consumer=operator,
            resume=resume,
        )

        self.assertEqual(result, "resumed")
        restored = DurableActionEnvelope.from_dict(payloads[0]["envelope"])
        self.assertEqual(restored.tool_name, "cancel")
        self.assertEqual(restored.arguments, {"id": "1"})
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
        envelope = DurableActionEnvelope(
            operation_id="operation-1",
            tool_call_id="write-call",
            tool_name="write",
            arguments={},
            write_id="write-1",
        )
        pending = await coordinator.request(
            session_id="session-1",
            operation_id="operation-1",
            requester=operator,
            action=envelope.to_dict(),
            action_summary="恢复写操作",
            required_role="approver",
            resume_payload={},
            idempotency_key="write-recovery-key",
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
            restored = DurableActionEnvelope.from_dict(payload["envelope"])
            await _execute_and_materialize(
                store,
                approvals,
                restored,
                operator=operator,
                approval_id=pending.approval.approval_id,
                idempotency_key="write-recovery-key",
            )
            return "ok"

        recovered = await coordinator.recover_incomplete(resume)
        self.assertEqual(recovered, [pending.approval.approval_id])
        self.assertEqual(
            DurableActionEnvelope.from_dict(payloads[0]["envelope"]).write_id,
            "write-1",
        )

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
                SessionJournalOperationEventStore,
            )

    async def test_host把可信tenant传入runtime且拒绝模型参数伪造(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calls = 0

            async def execute(_id, _args, _token, _update):
                nonlocal calls
                calls += 1
                return AgentToolResult(content=[])

            tool = AgentTool(
                name="tenant_write",
                label="tenant write",
                description="tenant write",
                execute=execute,
                resolve_tenant_id=lambda args: args.get("tenant"),
            )
            host = await DurableAgentHost.create(
                session_id="trusted-tenant",
                state_dir=directory,
                model=self.model,
                stream_fn=ScriptedProvider([]).stream,
                system_prompt="Tenant 测试",
                tools=[tool],
                tenant_id="acme",
                tenant_tool_limits={"acme": 1},
                auto_recover=False,
            )
            try:
                forged = await host.tool_dispatch_runtime.dispatch(
                    {
                        "type": "toolCall",
                        "id": "forged",
                        "name": "tenant_write",
                        "arguments": {"tenant": "attacker"},
                    }
                )
                success = await host.tool_dispatch_runtime.dispatch(
                    {
                        "type": "toolCall",
                        "id": "valid",
                        "name": "tenant_write",
                        "arguments": {"tenant": "acme"},
                    }
                )
            finally:
                await host.close()

            self.assertEqual(
                forged.result.details["code"],
                "tenant_context_mismatch",
            )
            self.assertFalse(success.is_error)
            self.assertEqual(calls, 1)

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
            pending = await host.prompt(
                "取消订单 1001",
                requester=operator,
                idempotency_key="cancel-1001",
            )
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
            approval_request = next(
                event
                for event in events
                if event.type == "model_request_started"
                and event.data.get("source") == "approval_resume"
            )
            boundary = [
                event
                for event in await host.resources.retry_store.load()
                if event.get("source") == "approval_resume"
            ]
            self.assertEqual(
                [event["type"] for event in boundary],
                ["model_request_started", "model_request_completed"],
            )
            self.assertEqual(
                {event["requestId"] for event in boundary},
                {approval_request.data["requestId"]},
            )
            self.assertEqual(
                {event["operationId"] for event in boundary},
                {pending.operation_id},
            )
            await host.close()

    async def test_host_approval_最终模型外部取消后复用_pending_request(self) -> None:
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
            first_model_started = asyncio.Event()
            release_first_model = asyncio.Event()
            provider_calls = 0
            producer_tasks: list[asyncio.Task] = []

            def stream(model, _context, _options):
                nonlocal provider_calls
                provider_calls += 1
                call_number = provider_calls
                result_stream = AssistantMessageEventStream()

                async def produce() -> None:
                    partial = assistant_message(
                        model=model,
                        stop_reason="pending",
                        content=[],
                    )
                    result_stream.push({"type": "start", "partial": partial})
                    if call_number == 1:
                        first_model_started.set()
                        await release_first_model.wait()
                    final = assistant_message(
                        model=model,
                        content=[{"type": "text", "text": "订单已取消"}],
                    )
                    result_stream.push(
                        {"type": "done", "reason": "stop", "message": final}
                    )

                producer_tasks.append(asyncio.create_task(produce()))
                return result_stream

            host = await DurableAgentHost.create(
                session_id="approval-final-reentry",
                state_dir=directory,
                model=self.model,
                stream_fn=stream,
                system_prompt="审批恢复重入测试",
                tools=[write_tool],
                router=FixedRouter(decision),
                capabilities=capabilities,
            )
            operator, approver = await self.identities()
            pending = await host.prompt(
                "取消订单 1001",
                requester=operator,
                idempotency_key="cancel-reentry-1001",
            )
            write_calls = 0

            async def handler(arguments, _key, _actor):
                nonlocal write_calls
                write_calls += 1
                return {
                    "status": "cancelled",
                    "orderId": arguments["order_id"],
                }

            first_attempt = asyncio.create_task(
                host.approve_and_resume(
                    pending.approval_id,
                    approver=approver,
                    consumer=operator,
                    idempotency_key="cancel-reentry-1001",
                    write_handler=handler,
                )
            )
            await asyncio.wait_for(first_model_started.wait(), timeout=2)
            first_attempt.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first_attempt
            release_first_model.set()

            interrupted_events = await host.operation_store.load(
                operation_id=pending.operation_id
            )
            interrupted_starts = [
                event
                for event in interrupted_events
                if event.type == "model_request_started"
                and event.data.get("source") == "approval_resume"
            ]
            self.assertEqual(len(interrupted_starts), 1)
            request_id = interrupted_starts[0].data["requestId"]
            interrupted = replay_operation(interrupted_events)
            self.assertEqual(
                interrupted.model_requests[request_id].phase,
                "started",
            )

            final = await host.approve_and_resume(
                pending.approval_id,
                approver=approver,
                consumer=operator,
                idempotency_key="cancel-reentry-1001",
                write_handler=handler,
            )
            await asyncio.gather(*producer_tasks)

            events = await host.operation_store.load(
                operation_id=pending.operation_id
            )
            operation = replay_operation(events)
            final_starts = [
                event
                for event in events
                if event.type == "model_request_started"
                and event.data.get("source") == "approval_resume"
            ]
            self.assertEqual(final["content"][0]["text"], "订单已取消")
            self.assertEqual(write_calls, 1)
            self.assertEqual(provider_calls, 2)
            self.assertEqual(len(final_starts), 1)
            self.assertEqual(final_starts[0].data["requestId"], request_id)
            self.assertFalse(
                any(
                    request.phase == "started"
                    for request in operation.model_requests.values()
                )
            )
            self.assertEqual(operation.phase, "completed")

    async def test_host_approval_最终模型已持久时不再请求_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider([])
            host = await DurableAgentHost.create(
                session_id="approval-final-persisted",
                state_dir=directory,
                model=self.model,
                stream_fn=provider.stream,
                system_prompt="已持久最终响应测试",
                tools=[],
                auto_recover=False,
            )
            operation_id = "operation-final-persisted"
            approval_id = "approval-final-persisted"
            request_id = "request-final-persisted"
            policy = ModelRequestPolicy.no_tools()
            persisted = assistant_message(
                model=self.model,
                content=[{"type": "text", "text": "持久响应"}],
            )
            await host.operation_store.append(
                "operation_started",
                host.session_id,
                operation_id,
                {"configuration": {}, "tools": []},
            )
            await host.operation_store.append_batch(
                host.session_id,
                operation_id,
                [
                    (
                        "model_request_started",
                        {
                            "requestId": request_id,
                            "source": "approval_resume",
                            "approvalId": approval_id,
                            "requestPolicy": policy.to_dict(),
                        },
                    ),
                    (
                        "model_request_completed",
                        {
                            "requestId": request_id,
                            "source": "approval_resume",
                            "approvalId": approval_id,
                            "message": persisted,
                        },
                    ),
                ],
            )

            result = await host._request_approval_final_model(
                operation_id=operation_id,
                approval_id=approval_id,
                policy=policy,
            )

            self.assertEqual(result, persisted)
            self.assertEqual(provider.call_count, 0)
            events = await host.operation_store.load(
                operation_id=operation_id
            )
            self.assertFalse(
                any(
                    event.type == "approval_resume_completed"
                    for event in events
                )
            )

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
            pending = await host.prompt(
                "退款订单 1001",
                requester=operator,
                idempotency_key="refund-1001",
            )
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
