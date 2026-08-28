"""Approval/Write/Resume 原子边界与跨进程 Claim 的 P0 回归。"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import (  # noqa: E402
    AgentTool,
    ApprovalResumeCoordinator,
    ApprovalService,
    CapabilityRegistry,
    DurableActionEnvelope,
    DurableAgentHost,
    IdentityClaim,
    InMemoryOperationEventStore,
    Model,
    RequestDecision,
    SQLiteOperationEventStore,
    ScriptedProvider,
    StaticIdentityVerifier,
    WriteOperationError,
    WriteOperationService,
    assistant_message,
    replay_operation,
)
from pi_agent_loop.harness.approval_gateway import ApprovalResumeError  # noqa: E402


class FixedRouter:
    def __init__(self, decision: RequestDecision) -> None:
        self.decision = decision

    def route(self, _text: str) -> RequestDecision:
        return self.decision


class P0ApprovalAtomicityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.model = Model(id="p0-atomic", provider="fake", api="fake")

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

    async def request_plan(
        self,
        coordinator,
        operator,
        *,
        operation_id="operation-1",
        write_id="refund-write",
        tool_call_id="refund-call",
        idempotency_key="refund-key",
    ):
        envelope = DurableActionEnvelope(
            operation_id=operation_id,
            tool_call_id=tool_call_id,
            tool_name="refund_order",
            arguments={"order_id": "1001"},
            write_id=write_id,
        )
        pending = await coordinator.request(
            session_id="session-1",
            operation_id=operation_id,
            requester=operator,
            action=envelope.to_dict(),
            action_summary="退款订单 1001",
            required_role="approver",
            resume_payload={},
            idempotency_key=idempotency_key,
        )
        return pending, envelope

    async def execute_and_materialize(
        self,
        store,
        approvals,
        pending,
        operator,
        payload,
        *,
        idempotency_key="refund-key",
        before_execute=None,
    ):
        envelope = DurableActionEnvelope.from_dict(payload["envelope"])
        if before_execute is not None:
            await before_execute()

        async def handler(_arguments, _key, _actor):
            return {"status": "succeeded"}

        write = await WriteOperationService(store, approvals).execute(
            envelope.write_id,
            actor=operator,
            idempotency_key=idempotency_key,
            handler=handler,
            approval_resume_id=pending.approval.approval_id,
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
        return {"status": "resumed"}

    async def make_host(self, directory):
        async def forbidden_direct(_id, _args, _token, _update):
            raise AssertionError("写工具只能由 WriteOperationService 执行")

        tool = AgentTool(
            name="refund_order",
            label="退款",
            description="退款订单",
            parameters={"type": "object"},
            execute=forbidden_direct,
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
        provider = ScriptedProvider([
            assistant_message(
                model=self.model,
                content=[{"type": "text", "text": "退款已完成"}],
            )
        ])
        host = await DurableAgentHost.create(
            session_id="atomic-host",
            state_dir=directory,
            model=self.model,
            stream_fn=provider.stream,
            system_prompt="测试",
            tools=[tool],
            router=FixedRouter(decision),
            capabilities=capabilities,
        )
        return host

    async def test_public_request一次提交完整计划并封存payload(self) -> None:
        store = InMemoryOperationEventStore()
        await self.start_operation(store)
        operator, _manager = await self.identities()
        coordinator = ApprovalResumeCoordinator(store, ApprovalService(store))

        pending, envelope = await self.request_plan(coordinator, operator)
        events = await store.load(operation_id="operation-1")
        planned = events[1:]

        self.assertEqual(
            [event.type for event in planned],
            [
                "message_appended",
                "write_prepared",
                "approval_requested",
                "approval_resume_registered",
                "write_waiting_approval",
                "tool_intent_recorded",
            ],
        )
        self.assertEqual(
            [event.sequence for event in planned],
            list(range(planned[0].sequence, planned[0].sequence + 6)),
        )
        self.assertEqual(
            pending.resume_payload,
            {"envelope": envelope.to_dict()},
        )

    async def test_public_request拒绝旧tool_call和额外payload(self) -> None:
        store = InMemoryOperationEventStore()
        await self.start_operation(store)
        operator, _manager = await self.identities()
        coordinator = ApprovalResumeCoordinator(store, ApprovalService(store))
        envelope = DurableActionEnvelope(
            operation_id="operation-1",
            tool_call_id="refund-call",
            tool_name="refund_order",
            arguments={"order_id": "1001"},
            write_id="refund-write",
        )
        await store.append(
            "message_appended",
            "session-1",
            "operation-1",
            {
                "message": {
                    "role": "assistant",
                    "content": [{
                        "type": "toolCall",
                        "id": "refund-call",
                        "name": "refund_order",
                        "arguments": {"order_id": "1001"},
                    }],
                }
            },
        )
        with self.assertRaises(ApprovalResumeError) as old_call:
            await coordinator.request(
                session_id="session-1",
                operation_id="operation-1",
                requester=operator,
                action=envelope.to_dict(),
                action_summary="退款",
                required_role="approver",
                resume_payload={},
                idempotency_key="refund-key",
            )
        self.assertEqual(old_call.exception.code, "approval_planned_call_conflict")

        store = InMemoryOperationEventStore()
        await self.start_operation(store)
        coordinator = ApprovalResumeCoordinator(store, ApprovalService(store))
        with self.assertRaises(ApprovalResumeError) as payload_error:
            await coordinator.request(
                session_id="session-1",
                operation_id="operation-1",
                requester=operator,
                action=envelope.to_dict(),
                action_summary="退款",
                required_role="approver",
                resume_payload={"order_id": "1001"},
                idempotency_key="refund-key",
            )
        self.assertEqual(
            payload_error.exception.code,
            "approval_resume_payload_not_sealed",
        )

    async def test_write有tool_call但intent不精确时fail_closed(self) -> None:
        store = InMemoryOperationEventStore()
        await self.start_operation(store)
        operator, _manager = await self.identities()
        writes = WriteOperationService(store, ApprovalService(store))
        write = await writes.prepare(
            session_id="session-1",
            operation_id="operation-1",
            tool_name="refund_order",
            arguments={"order_id": "1001"},
            idempotency_key="refund-key",
            requester=operator,
            requires_approval=False,
            tool_call_id="refund-call",
        )
        await store.append(
            "tool_intent_recorded",
            "session-1",
            "operation-1",
            {
                "toolCallId": "refund-call",
                "toolName": "read_order",
                "arguments": {"order_id": "1001"},
                "replayPolicy": "never",
            },
        )
        calls = 0

        async def handler(_arguments, _key, _actor):
            nonlocal calls
            calls += 1
            return {}

        with self.assertRaises(WriteOperationError) as caught:
            await writes.execute(
                write.write_id,
                actor=operator,
                idempotency_key="refund-key",
                handler=handler,
            )
        self.assertEqual(caught.exception.code, "tool_intent_missing")
        self.assertEqual(calls, 0)
        self.assertFalse(
            any(event.type == "write_submitting" for event in await store.load())
        )

    async def test_host批准后的五个claim事件同批连续(self) -> None:
        operator, manager = await self.identities()
        with tempfile.TemporaryDirectory() as directory:
            host = await self.make_host(directory)
            pending = await host.prompt(
                "退款订单 1001",
                requester=operator,
                idempotency_key="refund-host-key",
            )

            async def handler(_arguments, _key, _actor):
                return {"status": "succeeded"}

            await host.approve_and_resume(
                pending.approval_id,
                approver=manager,
                consumer=operator,
                idempotency_key="refund-host-key",
                write_handler=handler,
            )
            events = await host.operation_store.load(
                operation_id=pending.operation_id
            )
            claimed = [
                event
                for event in events
                if event.type in {
                    "approval_consumed",
                    "approval_resume_started",
                    "write_approved",
                    "tool_dispatch_started",
                    "write_submitting",
                }
            ]
            self.assertEqual(
                [event.type for event in claimed],
                [
                    "approval_consumed",
                    "approval_resume_started",
                    "write_approved",
                    "tool_dispatch_started",
                    "write_submitting",
                ],
            )
            self.assertEqual(
                [event.sequence for event in claimed],
                list(range(claimed[0].sequence, claimed[0].sequence + 5)),
            )
            self.assertEqual(replay_operation(events).phase, "completed")

    async def test_resume未闭合tool_result不能completed(self) -> None:
        store = InMemoryOperationEventStore()
        await self.start_operation(store)
        operator, manager = await self.identities()
        approvals = ApprovalService(store)
        coordinator = ApprovalResumeCoordinator(store, approvals)
        pending, envelope = await self.request_plan(coordinator, operator)

        async def incomplete_resume(_payload):
            async def handler(_arguments, _key, _actor):
                return {"status": "succeeded"}

            await WriteOperationService(store, approvals).execute(
                envelope.write_id,
                actor=operator,
                idempotency_key="refund-key",
                handler=handler,
                approval_resume_id=pending.approval.approval_id,
            )

        with self.assertRaises(ApprovalResumeError) as caught:
            await coordinator.approve_and_resume(
                pending.approval.approval_id,
                approver=manager,
                consumer=operator,
                resume=incomplete_resume,
            )
        self.assertEqual(caught.exception.code, "approval_resume_tool_incomplete")
        events = await store.load(operation_id="operation-1")
        self.assertFalse(
            any(event.type == "approval_resume_completed" for event in events)
        )
        self.assertEqual(
            sum(event.type == "approval_resume_failed" for event in events),
            1,
        )

    async def test_sqlite续租阻止第二worker接管慢resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            first_store = SQLiteOperationEventStore(path)
            second_store = SQLiteOperationEventStore(path)
            await self.start_operation(first_store)
            operator, manager = await self.identities()
            first_approvals = ApprovalService(first_store)
            second_approvals = ApprovalService(second_store)
            first = ApprovalResumeCoordinator(
                first_store,
                first_approvals,
                claim_lease_seconds=0.12,
                claim_renew_interval_seconds=0.03,
            )
            second = ApprovalResumeCoordinator(
                second_store,
                second_approvals,
                claim_lease_seconds=0.12,
                claim_renew_interval_seconds=0.03,
            )
            pending, _envelope = await self.request_plan(first, operator)
            entered = asyncio.Event()
            release = asyncio.Event()
            calls = 0

            async def before_execute():
                nonlocal calls
                calls += 1
                entered.set()
                await release.wait()

            async def resume(payload):
                return await self.execute_and_materialize(
                    first_store,
                    first_approvals,
                    pending,
                    operator,
                    payload,
                    before_execute=before_execute,
                )

            first_task = asyncio.create_task(
                first.approve_and_resume(
                    pending.approval.approval_id,
                    approver=manager,
                    consumer=operator,
                    resume=resume,
                )
            )
            await entered.wait()
            await asyncio.sleep(0.2)
            with self.assertRaises(ApprovalResumeError) as caught:
                await second.approve_and_resume(
                    pending.approval.approval_id,
                    approver=manager,
                    consumer=operator,
                    resume=resume,
                )
            self.assertEqual(caught.exception.code, "approval_resume_claimed")
            release.set()
            await first_task
            self.assertEqual(calls, 1)

    async def test_sqlite并发相同幂等键只提交一个完整计划(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            first_store = SQLiteOperationEventStore(path)
            second_store = SQLiteOperationEventStore(path)
            await self.start_operation(first_store, "operation-1")
            await self.start_operation(first_store, "operation-2")
            operator, _manager = await self.identities()
            first = ApprovalResumeCoordinator(
                first_store,
                ApprovalService(first_store),
            )
            second = ApprovalResumeCoordinator(
                second_store,
                ApprovalService(second_store),
            )
            results = await asyncio.gather(
                self.request_plan(
                    first,
                    operator,
                    operation_id="operation-1",
                    write_id="write-1",
                    tool_call_id="call-1",
                    idempotency_key="same-key",
                ),
                self.request_plan(
                    second,
                    operator,
                    operation_id="operation-2",
                    write_id="write-2",
                    tool_call_id="call-2",
                    idempotency_key="same-key",
                ),
                return_exceptions=True,
            )
            events = await first_store.load()
            self.assertEqual(
                sum(event.type == "write_prepared" for event in events),
                1,
            )
            self.assertEqual(
                sum(event.type == "approval_requested" for event in events),
                1,
            )
            self.assertEqual(
                sum(not isinstance(result, BaseException) for result in results),
                1,
            )

    async def test_host_completed快速返回仍补operation_finished(self) -> None:
        operator, manager = await self.identities()
        with tempfile.TemporaryDirectory() as directory:
            host = await self.make_host(directory)
            pending = await host.prompt(
                "退款订单 1001",
                requester=operator,
                idempotency_key="fast-finish-key",
            )
            calls = 0

            async def handler(_arguments, _key, _actor):
                nonlocal calls
                calls += 1
                return {"status": "succeeded"}

            original_finish = host.operation_recorder.finish_operation

            async def simulate_crash_before_finish(_outcome):
                return None

            host.operation_recorder.finish_operation = simulate_crash_before_finish
            await host.approve_and_resume(
                pending.approval_id,
                approver=manager,
                consumer=operator,
                idempotency_key="fast-finish-key",
                write_handler=handler,
            )
            before = await host.operation_store.load(
                operation_id=pending.operation_id
            )
            self.assertFalse(any(event.type == "operation_finished" for event in before))

            host.operation_recorder.finish_operation = original_finish
            await host.approve_and_resume(
                pending.approval_id,
                approver=manager,
                consumer=operator,
                idempotency_key="fast-finish-key",
                write_handler=handler,
            )
            final_events = await host.operation_store.load(
                operation_id=pending.operation_id
            )
            self.assertEqual(calls, 1)
            self.assertEqual(
                sum(event.type == "operation_finished" for event in final_events),
                1,
            )
            self.assertEqual(replay_operation(final_events).phase, "completed")


if __name__ == "__main__":
    unittest.main()
