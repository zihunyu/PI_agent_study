"""Durable Action、崩溃窗口、取消与恢复策略的 P0 回归测试。"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import (  # noqa: E402
    Agent,
    AgentTool,
    AgentToolResult,
    ApprovalError,
    ApprovalService,
    CapabilityRegistry,
    DurableActionEnvelope,
    DurableAgentHost,
    DurableOperationRecorder,
    DurableSessionRecovery,
    IdentityClaim,
    InMemoryOperationEventStore,
    Model,
    ModelRequestPolicy,
    ModelRequestPolicyError,
    OperationLogInvariantError,
    OutcomeUnknownToolError,
    RecoverableModelRuntime,
    RecoveryCallbacks,
    RequestDecision,
    ScriptedProvider,
    StartupRecoveryCoordinator,
    StaticIdentityVerifier,
    WriteOperationService,
    assistant_message,
    replay_operation,
    validate_closed_tool_call_transcript,
    validate_model_response_policy,
)


class DelayedGrantStore(InMemoryOperationEventStore):
    async def append_batch(
        self,
        session_id,
        operation_id,
        events,
        *,
        expected_last_sequence=None,
        deadline_ms=None,
    ):
        if any(event_type == "approval_granted" for event_type, _ in events):
            await asyncio.sleep(0.03)
        return await super().append_batch(
            session_id,
            operation_id,
            events,
            expected_last_sequence=expected_last_sequence,
            deadline_ms=deadline_ms,
        )


class FixedRouter:
    def __init__(self, decision):
        self.decision = decision

    def route(self, _text):
        return self.decision


class P0DurableBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.model = Model(id="p0-model", provider="fake", api="fake")

    async def identities(self):
        verifier = StaticIdentityVerifier({
            "operator": ("operator-secret", {"operator"}),
            "manager": ("manager-secret", {"approver"}),
        })
        return (
            await verifier.verify(
                IdentityClaim("operator", "operator-secret")
            ),
            await verifier.verify(
                IdentityClaim("manager", "manager-secret")
            ),
        )

    async def start_operation(self, store, operation_id="operation-1"):
        await store.append(
            "operation_started",
            "session-1",
            operation_id,
            {"configuration": {}, "tools": []},
        )

    async def test_waiting_phase没有approval事实时禁止恢复执行tool(self) -> None:
        store = InMemoryOperationEventStore()
        await self.start_operation(store)
        policy = ModelRequestPolicy(
            visible_tool_names=("refund_order",),
            tool_choice="required",
            allowed_tool_names=("refund_order",),
            expected_tool_arguments={"order_id": "1001"},
            continuation_policy=ModelRequestPolicy.no_tools(),
        )
        call = assistant_message(
            model=self.model,
            stop_reason="toolUse",
            content=[{
                "type": "toolCall",
                "id": "refund-call",
                "name": "refund_order",
                "arguments": {"order_id": "1001"},
            }],
        )
        await store.append("approval_pending", "session-1", "operation-1", {})
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
            {"message": {"role": "user", "content": "退款"}},
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
            {"requestId": "r1", "message": call},
        )

        plan = await DurableSessionRecovery(store).plan(
            session_id="session-1",
            operation_id="operation-1",
        )

        self.assertEqual(plan.actions[0].kind, "manual_intervention")
        self.assertNotIn("execute_tool", [item.kind for item in plan.actions])

    async def test_host原子持久化envelope_approval_write和intent(self) -> None:
        async def forbidden(_id, _args, _token, _update):
            raise AssertionError("写工具不能由普通 Agent 直接执行")

        tool = AgentTool(
            name="refund_order",
            label="退款",
            description="退款订单",
            parameters={"type": "object"},
            execute=forbidden,
            execution_mode="exclusive",
            replay_policy="never",
        )
        capabilities = CapabilityRegistry()
        capabilities.register(
            tool,
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
        operator, manager = await self.identities()
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider([
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "退款已完成"}],
                )
            ])
            host = await DurableAgentHost.create(
                session_id="p0-envelope",
                state_dir=directory,
                model=self.model,
                stream_fn=provider.stream,
                system_prompt="测试",
                tools=[tool],
                router=FixedRouter(decision),
                capabilities=capabilities,
            )
            pending = await host.prompt(
                "退款订单 1001",
                requester=operator,
                idempotency_key="refund-1001",
            )
            events = await host.operation_store.load(
                operation_id=pending.operation_id
            )
            operation = replay_operation(events)
            approval = operation.approvals[pending.approval_id]
            write = operation.writes[approval.write_id]
            envelope = DurableActionEnvelope.from_dict(approval.action)

            self.assertEqual(envelope.operation_id, pending.operation_id)
            self.assertEqual(envelope.tool_call_id, approval.tool_call_id)
            self.assertEqual(envelope.write_id, write.write_id)
            self.assertEqual(envelope.tool_name, write.tool_name)
            self.assertEqual(envelope.arguments, write.arguments)
            self.assertEqual(envelope.action_hash, write.action_hash)
            self.assertEqual(len(operation.tools), 1)
            self.assertEqual(
                sum(event.type == "tool_intent_recorded" for event in events),
                1,
            )

            calls = 0

            async def handler(_arguments, _key, _actor):
                nonlocal calls
                calls += 1
                return {"status": "refunded"}

            await host.approve_and_resume(
                pending.approval_id,
                approver=manager,
                consumer=operator,
                idempotency_key="refund-1001",
                write_handler=handler,
            )
            await host.approve_and_resume(
                pending.approval_id,
                approver=manager,
                consumer=operator,
                idempotency_key="refund-1001",
                write_handler=handler,
            )
            final_events = await host.operation_store.load(
                operation_id=pending.operation_id
            )
            self.assertEqual(calls, 1)
            self.assertEqual(
                sum(
                    event.type == "tool_intent_recorded"
                    for event in final_events
                ),
                1,
            )

    async def test_resume_payload_envelope篡改会被reducer拒绝(self) -> None:
        store = InMemoryOperationEventStore()
        await self.start_operation(store)
        operator, _manager = await self.identities()
        envelope = DurableActionEnvelope(
            operation_id="operation-1",
            tool_call_id="refund-call",
            tool_name="refund_order",
            arguments={"order_id": "1001"},
            write_id="refund-write",
        )
        approval = await ApprovalService(store).request(
            session_id="session-1",
            operation_id="operation-1",
            requester=operator,
            action=envelope.to_dict(),
            action_summary="退款",
            required_role="approver",
        )
        tampered = DurableActionEnvelope(
            operation_id="operation-1",
            tool_call_id="refund-call",
            tool_name="read_order",
            arguments={"order_id": "1001"},
            write_id="refund-write",
        )
        await store.append(
            "approval_resume_registered",
            "session-1",
            "operation-1",
            {
                "approvalId": approval.approval_id,
                "action": envelope.to_dict(),
                "resumePayload": {"envelope": tampered.to_dict()},
            },
        )

        with self.assertRaisesRegex(
            OperationLogInvariantError,
            "Resume Payload Envelope 不一致",
        ):
            replay_operation(await store.load())

    async def test_external_task_cancel仍会闭合transcript和durable_operation(self) -> None:
        started = asyncio.Event()

        async def execute(_id, _args, _token, _update):
            started.set()
            await asyncio.sleep(10)
            return AgentToolResult(content=[], details={})

        tool = AgentTool(
            name="slow_write",
            label="慢写",
            description="取消测试",
            execute=execute,
            execution_mode="exclusive",
            replay_policy="never",
        )
        provider = ScriptedProvider([
            assistant_message(
                model=self.model,
                stop_reason="toolUse",
                content=[{
                    "type": "toolCall",
                    "id": "slow-call",
                    "name": "slow_write",
                    "arguments": {},
                }],
            )
        ])
        store = InMemoryOperationEventStore()
        recorder = DurableOperationRecorder(
            store,
            session_id="cancel-session",
            tools=[tool],
        )
        agent = Agent(model=self.model, stream_fn=provider.stream, tools=[tool])
        agent.subscribe(recorder.listener)
        task = asyncio.create_task(agent.prompt("执行后外部取消"))
        await started.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        validate_closed_tool_call_transcript(agent.state.messages)
        operation = replay_operation(
            await store.load(operation_id=recorder.last_operation_id)
        )
        self.assertEqual(operation.phase, "cancelled")
        validate_closed_tool_call_transcript(list(operation.messages))

    async def test_write关联approval已批准时planner不再返回wait(self) -> None:
        store = InMemoryOperationEventStore()
        await self.start_operation(store)
        operator, manager = await self.identities()
        approvals = ApprovalService(store)
        writes = WriteOperationService(store, approvals)
        write = await writes.prepare(
            session_id="session-1",
            operation_id="operation-1",
            tool_name="refund_order",
            arguments={"order_id": "1001"},
            idempotency_key="refund-1001",
            requester=operator,
            requires_approval=True,
        )
        await approvals.grant(write.approval_id, manager)

        plan = await DurableSessionRecovery(store).plan(
            session_id="session-1",
            operation_id="operation-1",
        )

        self.assertEqual(plan.actions[0].kind, "consume_approval")
        self.assertNotEqual(plan.actions[0].kind, "wait_for_approval")

        async def unexpected_model(_messages, _policy):
            raise AssertionError("不应请求模型")

        async def unexpected_action(_action):
            raise AssertionError("缺少 Consumer 时不应执行动作")

        report = await StartupRecoveryCoordinator(
            store,
            RecoveryCallbacks(
                unexpected_model,
                unexpected_action,
                unexpected_action,
            ),
        ).recover_all()
        self.assertEqual(report.waiting_approval, ())
        self.assertEqual(report.ready_to_resume, ("operation-1",))

    async def test_reconciliation异常后可再次核对(self) -> None:
        store = InMemoryOperationEventStore()
        await self.start_operation(store)
        operator, _manager = await self.identities()
        writes = WriteOperationService(store, ApprovalService(store))
        write = await writes.prepare(
            session_id="session-1",
            operation_id="operation-1",
            tool_name="refund_order",
            arguments={"order_id": "1001"},
            idempotency_key="refund-unknown",
            requester=operator,
            requires_approval=False,
        )

        async def uncertain(*_args):
            raise OutcomeUnknownToolError(
                "未知",
                operation_id="external-1",
                idempotency_key="refund-unknown",
                reconciliation_name="check_refund",
            )

        with self.assertRaises(OutcomeUnknownToolError):
            await writes.execute(
                write.write_id,
                actor=operator,
                idempotency_key="refund-unknown",
                handler=uncertain,
            )

        async def failed(_record):
            raise RuntimeError("核对服务暂时失败")

        with self.assertRaisesRegex(RuntimeError, "暂时失败"):
            await writes.reconcile(write.write_id, failed)
        self.assertEqual(
            (await writes.get(write.write_id)).state,
            "outcome_unknown",
        )

        async def succeeded(_record):
            return {"status": "succeeded", "externalId": "external-1"}

        result = await writes.reconcile(write.write_id, succeeded)
        self.assertEqual(result.state, "succeeded")

    async def test_request缺策略不会继承旧active_policy(self) -> None:
        store = InMemoryOperationEventStore()
        await self.start_operation(store)
        old = ModelRequestPolicy(
            visible_tool_names=("dangerous_write",),
            tool_choice="auto",
            allowed_tool_names=("dangerous_write",),
        )
        await store.append(
            "model_policy_selected",
            "session-1",
            "operation-1",
            {"policy": old.to_dict()},
        )
        await store.append(
            "message_appended",
            "session-1",
            "operation-1",
            {"message": {"role": "user", "content": "新请求"}},
        )
        await store.append(
            "model_request_started",
            "session-1",
            "operation-1",
            {"requestId": "missing-policy"},
        )

        plan = await DurableSessionRecovery(store).plan(
            session_id="session-1",
            operation_id="operation-1",
        )

        self.assertIsNone(plan.operation.model_requests["missing-policy"].policy)
        self.assertIsNone(plan.operation.active_model_policy)
        self.assertEqual(plan.actions[0].kind, "manual_intervention")

    async def test_terminal_operation禁止approval_grant且ttl在事务内检查(self) -> None:
        store = InMemoryOperationEventStore()
        await self.start_operation(store)
        operator, manager = await self.identities()
        approvals = ApprovalService(store)
        envelope = DurableActionEnvelope(
            operation_id="operation-1",
            tool_call_id="call-1",
            tool_name="refund_order",
            arguments={"order_id": "1001"},
            write_id="write-1",
        )
        approval = await approvals.request(
            session_id="session-1",
            operation_id="operation-1",
            requester=operator,
            action=envelope.to_dict(),
            action_summary="退款",
            required_role="approver",
        )
        await store.append(
            "operation_finished",
            "session-1",
            "operation-1",
            {"outcome": "failed"},
        )

        with self.assertRaises(OperationLogInvariantError):
            await approvals.grant(approval.approval_id, manager)
        self.assertEqual(
            sum(
                event.type == "approval_granted"
                for event in await store.load()
            ),
            0,
        )

        ttl_store = DelayedGrantStore()
        await self.start_operation(ttl_store, "ttl-operation")
        ttl_approvals = ApprovalService(ttl_store)
        ttl_envelope = DurableActionEnvelope(
            operation_id="ttl-operation",
            tool_call_id="ttl-call",
            tool_name="refund_order",
            arguments={"order_id": "1001"},
            write_id="ttl-write",
        )
        expiring = await ttl_approvals.request(
            session_id="session-1",
            operation_id="ttl-operation",
            requester=operator,
            action=ttl_envelope.to_dict(),
            action_summary="短 TTL",
            required_role="approver",
            ttl_seconds=0.02,
        )
        # Grant 开始读取时仍是 Waiting；Store 在事务内部延迟后重新检查 TTL。
        with self.assertRaises(ApprovalError):
            await ttl_approvals.grant(expiring.approval_id, manager)
        ttl_events = await ttl_store.load()
        self.assertEqual(
            sum(event.type == "approval_granted" for event in ttl_events),
            0,
        )
        self.assertEqual(
            sum(event.type == "approval_expired" for event in ttl_events),
            1,
        )

    async def test_tool_end_listener异常不会伪造未执行结果(self) -> None:
        side_effects = 0

        async def execute(_id, _args, _token, _update):
            nonlocal side_effects
            side_effects += 1
            return AgentToolResult(
                content=[{"type": "text", "text": "真实写成功"}],
                details={"status": "succeeded"},
            )

        tool = AgentTool(
            name="ship_order",
            label="发货",
            description="发货",
            execute=execute,
            execution_mode="exclusive",
            replay_policy="never",
        )
        provider = ScriptedProvider([
            assistant_message(
                model=self.model,
                stop_reason="toolUse",
                content=[{
                    "type": "toolCall",
                    "id": "ship-call",
                    "name": "ship_order",
                    "arguments": {},
                }],
            ),
            assistant_message(
                model=self.model,
                content=[{"type": "text", "text": "发货完成"}],
            ),
        ])
        agent = Agent(model=self.model, stream_fn=provider.stream, tools=[tool])
        raised = False

        def listener(event, _token):
            nonlocal raised
            if event["type"] == "tool_execution_end" and not raised:
                raised = True
                raise RuntimeError("observer failed after side effect")

        agent.subscribe(listener)
        await agent.prompt("发货")

        results = [
            message
            for message in agent.state.messages
            if message.get("role") == "toolResult"
        ]
        self.assertEqual(side_effects, 1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["details"]["status"], "succeeded")
        self.assertNotEqual(
            results[0]["details"].get("code"),
            "tool_not_executed_due_run_error",
        )
        self.assertTrue(agent.listener_errors)
        validate_closed_tool_call_transcript(agent.state.messages)

    async def test普通恢复拒绝length响应(self) -> None:
        provider = ScriptedProvider([
            assistant_message(
                model=self.model,
                stop_reason="length",
                content=[{"type": "text", "text": "被截断"}],
            )
        ])
        runtime = RecoverableModelRuntime(
            model=self.model,
            stream_fn=provider.stream,
            system_prompt="测试",
            tools=[],
        )

        with self.assertRaisesRegex(RuntimeError, "长度上限"):
            await runtime.request(
                [{"role": "user", "content": "继续"}],
                policy=ModelRequestPolicy.no_tools(),
            )

    def test_expected_arguments必须由单个调用精确匹配(self) -> None:
        policy = ModelRequestPolicy(
            visible_tool_names=("refund_order",),
            tool_choice="required",
            allowed_tool_names=("refund_order",),
            expected_tool_arguments={"order_id": "1001", "amount": 20},
        )
        split = assistant_message(
            model=self.model,
            stop_reason="toolUse",
            content=[
                {
                    "type": "toolCall",
                    "id": "one",
                    "name": "refund_order",
                    "arguments": {"order_id": "1001"},
                },
                {
                    "type": "toolCall",
                    "id": "two",
                    "name": "refund_order",
                    "arguments": {"amount": 20},
                },
            ],
        )
        extra = assistant_message(
            model=self.model,
            stop_reason="toolUse",
            content=[{
                "type": "toolCall",
                "id": "extra",
                "name": "refund_order",
                "arguments": {
                    "order_id": "1001",
                    "amount": 20,
                    "force": True,
                },
            }],
        )

        with self.assertRaises(ModelRequestPolicyError):
            validate_model_response_policy(split, policy)
        with self.assertRaises(ModelRequestPolicyError):
            validate_model_response_policy(extra, policy)


if __name__ == "__main__":
    unittest.main()
