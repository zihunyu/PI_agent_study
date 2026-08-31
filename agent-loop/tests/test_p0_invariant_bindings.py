"""Durable Action 跨状态绑定与严格 JSON 类型回归。"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import (  # noqa: E402
    ApprovalResumeCoordinator,
    ApprovalService,
    DurableActionEnvelope,
    IdentityClaim,
    InMemoryOperationEventStore,
    JsonlOperationEventStore,
    OutcomeUnknownToolError,
    SQLiteOperationEventStore,
    StaticIdentityVerifier,
    WriteOperationError,
    WriteOperationService,
    replay_operation,
)
from pi_agent_loop.durable_action import (  # noqa: E402
    DurableActionEnvelopeError,
)
from pi_agent_loop.session.operation_state import (  # noqa: E402
    OperationLogInvariantError,
)


class P0InvariantBindingTests(unittest.IsolatedAsyncioTestCase):
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

    async def valid_plan(self, *, arguments=None):
        store = InMemoryOperationEventStore()
        await store.append(
            "operation_started",
            "session-1",
            "operation-1",
            {"configuration": {}, "tools": []},
        )
        operator, manager = await self.identities()
        approvals = ApprovalService(store)
        coordinator = ApprovalResumeCoordinator(store, approvals)
        envelope = DurableActionEnvelope(
            operation_id="operation-1",
            tool_call_id="refund-call",
            tool_name="refund_order",
            arguments=arguments or {"amount": 1},
            write_id="refund-write",
        )
        pending = await coordinator.request(
            session_id="session-1",
            operation_id="operation-1",
            requester=operator,
            action=envelope.to_dict(),
            action_summary="退款",
            required_role="approver",
            resume_payload={},
            idempotency_key="refund-key",
        )
        return store, approvals, pending, envelope, operator, manager

    def test_envelope严格区分bool和int并拒绝宽松version(self) -> None:
        envelope = DurableActionEnvelope(
            operation_id="operation-1",
            tool_call_id="refund-call",
            tool_name="refund_order",
            arguments={"amount": 1},
            write_id="refund-write",
        )
        different = DurableActionEnvelope(
            operation_id="operation-1",
            tool_call_id="refund-call",
            tool_name="refund_order",
            arguments={"amount": True},
            write_id="refund-write",
        )

        self.assertNotEqual(envelope, different)
        with self.assertRaises(DurableActionEnvelopeError):
            envelope.assert_execution(
                operation_id="operation-1",
                tool_call_id="refund-call",
                tool_name="refund_order",
                arguments={"amount": True},
                write_id="refund-write",
            )
        raw = envelope.to_dict()
        raw["version"] = True
        with self.assertRaisesRegex(
            DurableActionEnvelopeError,
            "version 必须是整数",
        ):
            DurableActionEnvelope.from_dict(raw)

    async def test_reducer拒绝assistant_call与envelope类型不一致(self) -> None:
        store, _approvals, _pending, _envelope, _operator, _manager = (
            await self.valid_plan()
        )
        events = await store.load(operation_id="operation-1")
        planned = events[1].data["message"]["content"][0]
        planned["arguments"] = {"amount": True}

        with self.assertRaisesRegex(
            OperationLogInvariantError,
            "Assistant Tool Call 不匹配",
        ):
            replay_operation(events)

    async def test_registered_resume缺少write时fail_closed(self) -> None:
        store = InMemoryOperationEventStore()
        await store.append("operation_started", "session-1", "operation-1")
        operator, _manager = await self.identities()
        envelope = DurableActionEnvelope(
            operation_id="operation-1",
            tool_call_id="refund-call",
            tool_name="refund_order",
            arguments={"amount": 1},
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
                        "id": envelope.tool_call_id,
                        "name": envelope.tool_name,
                        "arguments": envelope.arguments,
                    }],
                }
            },
        )
        approval = await ApprovalService(store).request(
            session_id="session-1",
            operation_id="operation-1",
            requester=operator,
            action=envelope.to_dict(),
            action_summary="退款",
            required_role="approver",
        )
        await store.append(
            "approval_resume_registered",
            "session-1",
            "operation-1",
            {
                "approvalId": approval.approval_id,
                "action": envelope.to_dict(),
                "resumePayload": {"envelope": envelope.to_dict()},
            },
        )

        with self.assertRaisesRegex(
            OperationLogInvariantError,
            "缺少同事务持久化的 Write",
        ):
            replay_operation(await store.load())

    async def test_write不能绕过approval_consume直接approved(self) -> None:
        store, _approvals, _pending, envelope, _operator, _manager = (
            await self.valid_plan()
        )
        await store.append(
            "write_approved",
            "session-1",
            "operation-1",
            {
                "writeId": envelope.write_id,
                "approvalId": _pending.approval.approval_id,
            },
        )

        with self.assertRaisesRegex(
            OperationLogInvariantError,
            "Approval 未消费",
        ):
            replay_operation(await store.load())

    async def test_write_submitting前必须存在对应dispatch(self) -> None:
        store, approvals, pending, envelope, operator, manager = (
            await self.valid_plan()
        )
        await approvals.grant(pending.approval.approval_id, manager)
        await approvals.consume(
            pending.approval.approval_id,
            action=pending.action,
            consumer=operator,
        )
        await store.append_batch(
            "session-1",
            "operation-1",
            [
                (
                    "approval_resume_started",
                    {
                        "approvalId": pending.approval.approval_id,
                        "consumerId": operator.principal_id,
                    },
                ),
                (
                    "write_approved",
                    {
                        "writeId": envelope.write_id,
                        "approvalId": pending.approval.approval_id,
                    },
                ),
                ("write_submitting", {"writeId": envelope.write_id}),
            ],
        )

        with self.assertRaisesRegex(
            OperationLogInvariantError,
            "原子启动对应 Tool Dispatch",
        ):
            replay_operation(await store.load())

    async def test_resume_completed必须闭合write_tool和transcript(self) -> None:
        store, approvals, pending, envelope, operator, manager = (
            await self.valid_plan()
        )
        await approvals.grant(pending.approval.approval_id, manager)
        await approvals.consume(
            pending.approval.approval_id,
            action=pending.action,
            consumer=operator,
        )
        await store.append_batch(
            "session-1",
            "operation-1",
            [
                (
                    "approval_resume_started",
                    {
                        "approvalId": pending.approval.approval_id,
                        "consumerId": operator.principal_id,
                    },
                ),
                (
                    "write_approved",
                    {
                        "writeId": envelope.write_id,
                        "approvalId": pending.approval.approval_id,
                    },
                ),
                (
                    "tool_dispatch_started",
                    {"toolCallId": envelope.tool_call_id},
                ),
                ("write_submitting", {"writeId": envelope.write_id}),
                (
                    "write_succeeded",
                    {"writeId": envelope.write_id, "result": {}},
                ),
                (
                    "approval_resume_completed",
                    {"approvalId": pending.approval.approval_id},
                ),
            ],
        )

        with self.assertRaisesRegex(
            OperationLogInvariantError,
            "Tool 尚未完成",
        ):
            replay_operation(await store.load())

    async def test_write_intent参数严格区分bool和int(self) -> None:
        store = InMemoryOperationEventStore()
        await store.append("operation_started", "session-1", "operation-1")
        operator, _manager = await self.identities()
        writes = WriteOperationService(store, ApprovalService(store))
        write = await writes.prepare(
            session_id="session-1",
            operation_id="operation-1",
            tool_name="refund_order",
            arguments={"amount": 1},
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
                "toolName": "refund_order",
                "arguments": {"amount": True},
                "replayPolicy": "never",
            },
        )
        calls = 0

        async def handler(*_args):
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

    async def test_non_atomic_store拒绝approval_write工作流(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JsonlOperationEventStore(
                Path(directory) / "operation-events.jsonl"
            )
            await store.append("operation_started", "session-1", "operation-1")
            operator, _manager = await self.identities()
            approvals = ApprovalService(store)
            envelope = DurableActionEnvelope(
                operation_id="operation-1",
                tool_call_id="refund-call",
                tool_name="refund_order",
                arguments={"amount": 1},
                write_id="refund-write",
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "原子事务",
            ):
                await ApprovalResumeCoordinator(store, approvals).request(
                    session_id="session-1",
                    operation_id="operation-1",
                    requester=operator,
                    action=envelope.to_dict(),
                    action_summary="退款",
                    required_role="approver",
                    resume_payload={},
                    idempotency_key="refund-key",
                )
            with self.assertRaises(WriteOperationError) as caught:
                await WriteOperationService(store, approvals).prepare(
                    session_id="session-1",
                    operation_id="operation-1",
                    tool_name="refund_order",
                    arguments={"amount": 1},
                    idempotency_key="refund-key",
                    requester=operator,
                    requires_approval=True,
                )
            self.assertEqual(
                caught.exception.code,
                "approval_atomic_store_required",
            )

    async def test_inmemory跨operation并发也保持全局幂等唯一(self) -> None:
        store = InMemoryOperationEventStore()
        for operation_id in ("operation-1", "operation-2"):
            await store.append(
                "operation_started",
                "session-1",
                operation_id,
            )
        operator, _manager = await self.identities()
        first = ApprovalResumeCoordinator(store, ApprovalService(store))
        second = ApprovalResumeCoordinator(store, ApprovalService(store))

        async def request(coordinator, operation_id, suffix):
            envelope = DurableActionEnvelope(
                operation_id=operation_id,
                tool_call_id=f"call-{suffix}",
                tool_name="refund_order",
                arguments={"amount": 1},
                write_id=f"write-{suffix}",
            )
            return await coordinator.request(
                session_id="session-1",
                operation_id=operation_id,
                requester=operator,
                action=envelope.to_dict(),
                action_summary="退款",
                required_role="approver",
                resume_payload={},
                idempotency_key="same-global-key",
            )

        results = await asyncio.gather(
            request(first, "operation-1", "1"),
            request(second, "operation-2", "2"),
            return_exceptions=True,
        )
        events = await store.load()
        self.assertEqual(
            sum(event.type == "write_prepared" for event in events),
            1,
        )
        self.assertEqual(
            sum(not isinstance(result, BaseException) for result in results),
            1,
        )

    async def test_write_handler被取消后持久化outcome_unknown并可核对(self) -> None:
        store = InMemoryOperationEventStore()
        await store.append("operation_started", "session-1", "operation-1")
        operator, _manager = await self.identities()
        writes = WriteOperationService(store, ApprovalService(store))
        write = await writes.prepare(
            session_id="session-1",
            operation_id="operation-1",
            tool_name="refund_order",
            arguments={"amount": 1},
            idempotency_key="cancel-key",
            requester=operator,
            requires_approval=False,
        )
        entered = asyncio.Event()

        async def handler(*_args):
            entered.set()
            await asyncio.sleep(10)

        task = asyncio.create_task(
            writes.execute(
                write.write_id,
                actor=operator,
                idempotency_key="cancel-key",
                handler=handler,
            )
        )
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual((await writes.get(write.write_id)).state, "outcome_unknown")

        reconciled = await writes.reconcile(
            write.write_id,
            lambda _record: _async_result({"status": "succeeded"}),
        )
        self.assertEqual(reconciled.state, "succeeded")

    async def test_sqlite慢reconcile续租阻止第二worker接管(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            first_store = SQLiteOperationEventStore(path)
            second_store = SQLiteOperationEventStore(path)
            await first_store.append(
                "operation_started",
                "session-1",
                "operation-1",
            )
            operator, _manager = await self.identities()
            first = WriteOperationService(
                first_store,
                ApprovalService(first_store),
                reconcile_claim_lease_seconds=1.0,
                reconcile_claim_renew_interval_seconds=0.1,
            )
            second = WriteOperationService(
                second_store,
                ApprovalService(second_store),
                reconcile_claim_lease_seconds=1.0,
                reconcile_claim_renew_interval_seconds=0.1,
            )
            write = await first.prepare(
                session_id="session-1",
                operation_id="operation-1",
                tool_name="refund_order",
                arguments={"amount": 1},
                idempotency_key="unknown-key",
                requester=operator,
                requires_approval=False,
            )

            async def uncertain(*_args):
                raise OutcomeUnknownToolError(
                    "unknown",
                    operation_id="external-1",
                    idempotency_key="unknown-key",
                    reconciliation_name="check_refund",
                )

            with self.assertRaises(OutcomeUnknownToolError):
                await first.execute(
                    write.write_id,
                    actor=operator,
                    idempotency_key="unknown-key",
                    handler=uncertain,
                )
            entered = asyncio.Event()
            release = asyncio.Event()

            async def slow_reconcile(_record):
                entered.set()
                await release.wait()
                return {"status": "succeeded"}

            first_task = asyncio.create_task(
                first.reconcile(write.write_id, slow_reconcile)
            )
            try:
                await asyncio.wait_for(entered.wait(), timeout=10)
                # Wait beyond the original lease to prove that the heartbeat,
                # not merely the first claim TTL, prevents worker takeover.
                await asyncio.sleep(1.25)
                with self.assertRaises(WriteOperationError) as caught:
                    await second.reconcile(
                        write.write_id,
                        lambda _record: _async_result(
                            {"status": "succeeded"}
                        ),
                    )
                self.assertEqual(
                    caught.exception.code,
                    "write_reconcile_claimed",
                )
                release.set()
                self.assertEqual((await first_task).state, "succeeded")
            finally:
                release.set()
                if not first_task.done():
                    first_task.cancel()
                await asyncio.gather(first_task, return_exceptions=True)


async def _async_result(value):
    return value


if __name__ == "__main__":
    unittest.main()
