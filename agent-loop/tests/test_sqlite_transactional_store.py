"""SQLite 单机事务 Store 的持久化、CAS、唯一约束和 Claim 测试。"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import (  # noqa: E402
    ApprovalError,
    ApprovalResumeCoordinator,
    ApprovalService,
    DurableActionEnvelope,
    IdentityClaim,
    RuntimeEvent,
    SQLiteOperationEventStore,
    SQLiteRuntimeEventStore,
    StaticIdentityVerifier,
    WriteOperationError,
    WriteOperationService,
    replay_operation,
)


async def _successful_write(_arguments, _key, _actor):
    return {"status": "succeeded"}


class SQLiteTransactionalStoreTests(unittest.IsolatedAsyncioTestCase):
    async def identities(self):
        verifier = StaticIdentityVerifier({
            "operator": ("operator-secret", {"operator"}),
            "manager-a": ("manager-a-secret", {"approver"}),
            "manager-b": ("manager-b-secret", {"approver"}),
        })
        return (
            await verifier.verify(
                IdentityClaim("operator", "operator-secret")
            ),
            await verifier.verify(
                IdentityClaim("manager-a", "manager-a-secret")
            ),
            await verifier.verify(
                IdentityClaim("manager-b", "manager-b-secret")
            ),
        )

    async def start_operation(self, store, operation_id="operation-1"):
        await store.append(
            "operation_started",
            "session-1",
            operation_id,
            {"configuration": {}, "tools": []},
        )

    async def test_sqlite_operation和runtime重启后保持事件(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            operation_store = SQLiteOperationEventStore(path)
            await self.start_operation(operation_store)
            await operation_store.append(
                "message_appended",
                "session-1",
                "operation-1",
                {"message": {"role": "user", "content": "继续"}},
            )
            runtime_store = SQLiteRuntimeEventStore(path)
            await runtime_store.append(
                RuntimeEvent(type="run_started", run_id="run-1", sequence=0)
            )

            reopened_operations = SQLiteOperationEventStore(path)
            reopened_runtime = SQLiteRuntimeEventStore(path)
            operation = replay_operation(
                await reopened_operations.load(
                    session_id="session-1",
                    operation_id="operation-1",
                )
            )
            runtime_events = await reopened_runtime.load()

            self.assertEqual(operation.messages[0]["role"], "user")
            self.assertEqual(runtime_events[0].run_id, "run-1")

    async def test_两个store并发grant只有一个事务获胜(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            first_store = SQLiteOperationEventStore(path)
            second_store = SQLiteOperationEventStore(path)
            await self.start_operation(first_store)
            operator, manager_a, manager_b = await self.identities()
            first = ApprovalService(first_store)
            second = ApprovalService(second_store)
            approval = await first.request(
                session_id="session-1",
                operation_id="operation-1",
                requester=operator,
                action={
                    "tool": "refund_order",
                    "arguments": {"order_id": "1001"},
                },
                action_summary="退款订单 1001",
                required_role="approver",
            )

            results = await asyncio.gather(
                first.grant(approval.approval_id, manager_a),
                second.grant(approval.approval_id, manager_b),
                return_exceptions=True,
            )
            events = await first_store.load(
                session_id="session-1",
                operation_id="operation-1",
            )

            self.assertEqual(
                sum(event.type == "approval_granted" for event in events),
                1,
            )
            self.assertEqual(
                sum(not isinstance(result, BaseException) for result in results),
                1,
            )
            self.assertTrue(
                any(isinstance(result, ApprovalError) for result in results)
            )
            self.assertEqual(
                (await first.get(approval.approval_id)).state,
                "approved",
            )

    async def test_两个store并发execute外部写只执行一次(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            first_store = SQLiteOperationEventStore(path)
            second_store = SQLiteOperationEventStore(path)
            await self.start_operation(first_store)
            operator, _manager_a, _manager_b = await self.identities()
            first = WriteOperationService(
                first_store,
                ApprovalService(first_store),
            )
            second = WriteOperationService(
                second_store,
                ApprovalService(second_store),
            )
            write = await first.prepare(
                session_id="session-1",
                operation_id="operation-1",
                tool_name="refund_order",
                arguments={"order_id": "1001"},
                idempotency_key="refund-1001",
                requester=operator,
                requires_approval=False,
            )
            calls = 0

            async def handler(_arguments, _key, _actor):
                nonlocal calls
                calls += 1
                await asyncio.sleep(0.05)
                return {"status": "succeeded"}

            results = await asyncio.gather(
                first.execute(
                    write.write_id,
                    actor=operator,
                    idempotency_key="refund-1001",
                    handler=handler,
                ),
                second.execute(
                    write.write_id,
                    actor=operator,
                    idempotency_key="refund-1001",
                    handler=handler,
                ),
                return_exceptions=True,
            )
            events = await first_store.load(
                session_id="session-1",
                operation_id="operation-1",
            )

            self.assertEqual(calls, 1)
            self.assertEqual(
                sum(event.type == "write_submitting" for event in events),
                1,
            )
            self.assertEqual(
                sum(event.type == "write_succeeded" for event in events),
                1,
            )
            self.assertEqual((await first.get(write.write_id)).state, "succeeded")
            self.assertTrue(
                all(
                    not isinstance(result, BaseException)
                    or isinstance(result, WriteOperationError)
                    for result in results
                )
            )

    async def test_approval消费_write_claim和tool_dispatch同一事务提交(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            store = SQLiteOperationEventStore(path)
            await self.start_operation(store)
            operator, manager, _manager_b = await self.identities()
            approvals = ApprovalService(store)
            writes = WriteOperationService(store, approvals)
            write = await writes.prepare(
                session_id="session-1",
                operation_id="operation-1",
                tool_name="refund_order",
                arguments={"order_id": "1001"},
                idempotency_key="transaction-refund-1001",
                requester=operator,
                requires_approval=True,
                tool_call_id="refund-call",
            )
            await store.append(
                "tool_intent_recorded",
                "session-1",
                "operation-1",
                {
                    "toolCallId": "refund-call",
                    "toolName": "refund_order",
                    "arguments": {"order_id": "1001"},
                    "replayPolicy": "never",
                },
            )
            await approvals.grant(write.approval_id, manager)

            async def handler(_arguments, _key, _actor):
                return {"status": "succeeded"}

            result = await writes.execute(
                write.write_id,
                actor=operator,
                idempotency_key="transaction-refund-1001",
                handler=handler,
            )
            events = await store.load(
                session_id="session-1",
                operation_id="operation-1",
            )
            transaction_events = [
                event
                for event in events
                if event.type in {
                    "approval_consumed",
                    "write_approved",
                    "tool_dispatch_started",
                    "write_submitting",
                }
            ]

            self.assertEqual(result.state, "succeeded")
            self.assertEqual(
                [event.type for event in transaction_events],
                [
                    "approval_consumed",
                    "write_approved",
                    "tool_dispatch_started",
                    "write_submitting",
                ],
            )
            self.assertEqual(
                [event.sequence for event in transaction_events],
                list(
                    range(
                        transaction_events[0].sequence,
                        transaction_events[0].sequence + 4,
                    )
                ),
            )

    async def test_跨operation并发prepare由唯一幂等键去重(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            first_store = SQLiteOperationEventStore(path)
            second_store = SQLiteOperationEventStore(path)
            await self.start_operation(first_store, "operation-1")
            await self.start_operation(first_store, "operation-2")
            operator, _manager_a, _manager_b = await self.identities()
            first = WriteOperationService(
                first_store,
                ApprovalService(first_store),
            )
            second = WriteOperationService(
                second_store,
                ApprovalService(second_store),
            )

            first_result, second_result = await asyncio.gather(
                first.prepare(
                    session_id="session-1",
                    operation_id="operation-1",
                    tool_name="refund_order",
                    arguments={"order_id": "1001"},
                    idempotency_key="global-refund-1001",
                    requester=operator,
                    requires_approval=False,
                ),
                second.prepare(
                    session_id="session-1",
                    operation_id="operation-2",
                    tool_name="refund_order",
                    arguments={"order_id": "1001"},
                    idempotency_key="global-refund-1001",
                    requester=operator,
                    requires_approval=False,
                ),
            )
            events = await first_store.load()

            self.assertEqual(first_result.write_id, second_result.write_id)
            self.assertEqual(
                sum(event.type == "write_prepared" for event in events),
                1,
            )

    async def test_两个coordinator不能同时执行同一approval_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            first_store = SQLiteOperationEventStore(path)
            second_store = SQLiteOperationEventStore(path)
            await self.start_operation(first_store)
            operator, manager, _manager_b = await self.identities()
            first_approvals = ApprovalService(first_store)
            second_approvals = ApprovalService(second_store)
            first = ApprovalResumeCoordinator(first_store, first_approvals)
            second = ApprovalResumeCoordinator(second_store, second_approvals)
            envelope = DurableActionEnvelope(
                operation_id="operation-1",
                tool_call_id="refund-call",
                tool_name="refund_order",
                arguments={"order_id": "1001"},
                write_id="refund-write",
            )
            pending = await first.request(
                session_id="session-1",
                operation_id="operation-1",
                requester=operator,
                action=envelope.to_dict(),
                action_summary="退款订单 1001",
                required_role="approver",
                resume_payload={},
                idempotency_key="coordinator-refund-key",
            )
            registered = [
                event
                for event in await first_store.load(
                    session_id="session-1",
                    operation_id="operation-1",
                )
                if event.type in {
                    "approval_requested",
                    "approval_resume_registered",
                }
            ]
            self.assertEqual(
                [event.type for event in registered],
                ["approval_requested", "approval_resume_registered"],
            )
            self.assertEqual(
                registered[1].sequence,
                registered[0].sequence + 1,
            )
            entered = asyncio.Event()
            release = asyncio.Event()
            calls = 0

            async def resume(payload):
                nonlocal calls
                calls += 1
                entered.set()
                await release.wait()
                restored = DurableActionEnvelope.from_dict(payload["envelope"])
                write = await WriteOperationService(
                    first_store,
                    first_approvals,
                ).execute(
                    restored.write_id,
                    actor=operator,
                    idempotency_key="coordinator-refund-key",
                    handler=_successful_write,
                    approval_resume_id=pending.approval.approval_id,
                )
                operation = replay_operation(
                    await first_store.load(operation_id="operation-1")
                )
                specs = []
                if operation.tools[restored.tool_call_id].phase != "completed":
                    specs.append(
                        (
                            "tool_completed",
                            {
                                "toolCallId": restored.tool_call_id,
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
                    and message.get("toolCallId") == restored.tool_call_id
                    for message in operation.messages
                ):
                    specs.append(
                        (
                            "message_appended",
                            {
                                "message": {
                                    "role": "toolResult",
                                    "toolCallId": restored.tool_call_id,
                                    "toolName": restored.tool_name,
                                    "content": [],
                                    "details": write.result or {},
                                    "isError": False,
                                }
                            },
                        )
                    )
                if specs:
                    await first_store.append_batch(
                        "session-1",
                        "operation-1",
                        specs,
                    )
                return {"status": "succeeded"}

            first_task = asyncio.create_task(
                first.approve_and_resume(
                    pending.approval.approval_id,
                    approver=manager,
                    consumer=operator,
                    resume=resume,
                )
            )
            await entered.wait()
            second_result = await asyncio.gather(
                second.approve_and_resume(
                    pending.approval.approval_id,
                    approver=manager,
                    consumer=operator,
                    resume=resume,
                ),
                return_exceptions=True,
            )
            release.set()
            await first_task

            self.assertEqual(calls, 1)
            self.assertIsInstance(second_result[0], RuntimeError)
            self.assertIn("Claim", str(second_result[0]))

    async def test_跨store_claim在release前互斥(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            first = SQLiteOperationEventStore(path)
            second = SQLiteOperationEventStore(path)

            self.assertTrue(
                await first.try_acquire_claim(
                    "operation_recovery",
                    "session-1:operation-1",
                    "worker-a",
                )
            )
            self.assertFalse(
                await second.try_acquire_claim(
                    "operation_recovery",
                    "session-1:operation-1",
                    "worker-b",
                )
            )
            await first.release_claim(
                "operation_recovery",
                "session-1:operation-1",
                "worker-a",
            )
            self.assertTrue(
                await second.try_acquire_claim(
                    "operation_recovery",
                    "session-1:operation-1",
                    "worker-b",
                )
            )


if __name__ == "__main__":
    unittest.main()
