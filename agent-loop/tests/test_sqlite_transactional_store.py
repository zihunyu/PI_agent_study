"""SQLite 单机事务 Store 的持久化、CAS、唯一约束和 Claim 测试。"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import (  # noqa: E402
    ApprovalError,
    ApprovalResumeCoordinator,
    ApprovalService,
    DurableAgentHost,
    DurableActionEnvelope,
    IdentityClaim,
    RuntimeEvent,
    RuntimeStateTracker,
    SQLiteOperationEventStore,
    SQLiteRuntimeEventStore,
    SQLiteRuntimeStoreMigrationRequiredError,
    StaticIdentityVerifier,
    WriteOperationError,
    WriteOperationService,
    Model,
    OperationStoreConflictError,
    ScriptedProvider,
    assistant_message,
    migrate_legacy_sqlite_runtime_events,
    replay_operation,
    replay_runtime_events,
)


async def _successful_write(_arguments, _key, _actor):
    return {"status": "succeeded"}


class SQLiteTransactionalStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_writer_fence与operation追加同一事务(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            first = SQLiteOperationEventStore(path)
            second = SQLiteOperationEventStore(path)
            stale = await first.acquire_fenced_claim(
                "conversation_session_writer",
                "session-fenced",
                "host-a",
                lease_seconds=0.3,
            )
            assert stale is not None
            await first.append_batch_if_fenced_claim(
                "session-fenced",
                "operation-a",
                [("operation_started", {"configuration": {}, "tools": []})],
                stale,
                renew_lease_seconds=0.3,
                expected_last_sequence=-1,
            )
            await asyncio.sleep(0.35)
            successor = await second.acquire_fenced_claim(
                "conversation_session_writer",
                "session-fenced",
                "host-b",
                lease_seconds=1,
            )
            assert successor is not None

            with self.assertRaises(OperationStoreConflictError):
                await first.append_batch_if_fenced_claim(
                    "session-fenced",
                    "operation-a",
                    [("message_appended", {"message": {"role": "user"}})],
                    stale,
                    renew_lease_seconds=0.3,
                )
            await second.append_batch_if_fenced_claim(
                "session-fenced",
                "operation-b",
                [("operation_started", {"configuration": {}, "tools": []})],
                successor,
                renew_lease_seconds=1,
                expected_last_sequence=-1,
            )
            self.assertEqual(
                len(await second.load(session_id="session-fenced")),
                2,
            )

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
            runtime_store = SQLiteRuntimeEventStore(
                path,
                session_id="session-1",
            )
            await runtime_store.append(
                RuntimeEvent(type="run_started", run_id="run-1", sequence=0)
            )

            reopened_operations = SQLiteOperationEventStore(path)
            reopened_runtime = SQLiteRuntimeEventStore(
                path,
                session_id="session-1",
            )
            operation = replay_operation(
                await reopened_operations.load(
                    session_id="session-1",
                    operation_id="operation-1",
                )
            )
            runtime_events = await reopened_runtime.load()

            self.assertEqual(operation.messages[0]["role"], "user")
            self.assertEqual(runtime_events[0].run_id, "run-1")

    async def test_两个session的runtime_tracker共享sqlite仍各自从零开始(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            first_store = SQLiteRuntimeEventStore(
                path,
                session_id="session-a",
            )
            second_store = SQLiteRuntimeEventStore(
                path,
                session_id="session-b",
            )
            first = await RuntimeStateTracker.create(first_store)
            second = await RuntimeStateTracker.create(second_store)

            first_state, second_state = await asyncio.gather(
                first.start_run(),
                second.start_run(),
            )

            self.assertEqual(first_state.sequence, 0)
            self.assertEqual(second_state.sequence, 0)
            self.assertNotEqual(first_state.run_id, second_state.run_id)
            self.assertEqual(
                [event.run_id for event in await first_store.load()],
                [first_state.run_id],
            )
            self.assertEqual(
                [event.run_id for event in await second_store.load()],
                [second_state.run_id],
            )

    async def test_两个session的sqlite_host可并发运行且不会互相重放(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model = Model(id="session-partition-model", provider="test", api="test")
            first_provider = ScriptedProvider([
                assistant_message(
                    model=model,
                    content=[{"type": "text", "text": "first"}],
                )
            ])
            second_provider = ScriptedProvider([
                assistant_message(
                    model=model,
                    content=[{"type": "text", "text": "second"}],
                )
            ])
            first, second = await asyncio.gather(
                DurableAgentHost.create(
                    session_id="host-session-a",
                    state_dir=directory,
                    model=model,
                    stream_fn=first_provider.stream,
                    system_prompt="test",
                    tools=[],
                    store_backend="sqlite",
                ),
                DurableAgentHost.create(
                    session_id="host-session-b",
                    state_dir=directory,
                    model=model,
                    stream_fn=second_provider.stream,
                    system_prompt="test",
                    tools=[],
                    store_backend="sqlite",
                ),
            )
            try:
                await asyncio.gather(
                    first.prompt("first request"),
                    second.prompt("second request"),
                )
                self.assertEqual(first.runtime_tracker.state.phase, "completed")
                self.assertEqual(second.runtime_tracker.state.phase, "completed")
                self.assertNotEqual(
                    first.runtime_tracker.state.run_id,
                    second.runtime_tracker.state.run_id,
                )
                self.assertNotEqual(
                    first.resources.retry_store.path,
                    second.resources.retry_store.path,
                )
            finally:
                await asyncio.gather(first.close(), second.close())

            path = Path(directory) / "agent-state.sqlite3"
            first_events = await SQLiteRuntimeEventStore(
                path,
                session_id="host-session-a",
            ).load()
            second_events = await SQLiteRuntimeEventStore(
                path,
                session_id="host-session-b",
            ).load()
            self.assertEqual(replay_runtime_events(first_events).phase, "completed")
            self.assertEqual(replay_runtime_events(second_events).phase, "completed")
            self.assertTrue(first_events)
            self.assertTrue(second_events)
            self.assertTrue(
                {event.run_id for event in first_events}.isdisjoint(
                    {event.run_id for event in second_events}
                )
            )

    async def test_旧runtime表有事件时必须显式绑定session后迁移(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    """
                    CREATE TABLE runtime_events (
                        sequence INTEGER PRIMARY KEY,
                        type TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        timestamp INTEGER NOT NULL,
                        data_json TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO runtime_events(
                        sequence, type, run_id, timestamp, data_json
                    ) VALUES (0, 'run_started', 'legacy-run', 1, '{}')
                    """
                )
                connection.execute("PRAGMA user_version=1")
                connection.commit()

            with self.assertRaises(SQLiteRuntimeStoreMigrationRequiredError):
                SQLiteRuntimeEventStore(path, session_id="legacy-session")

            self.assertEqual(
                migrate_legacy_sqlite_runtime_events(
                    path,
                    session_id="legacy-session",
                ),
                1,
            )
            migrated = await SQLiteRuntimeEventStore(
                path,
                session_id="legacy-session",
            ).load()
            unrelated = await SQLiteRuntimeEventStore(
                path,
                session_id="other-session",
            ).load()
            self.assertEqual([event.run_id for event in migrated], ["legacy-run"])
            self.assertEqual(unrelated, [])

    async def test_durable_host明确拒绝jsonl兼容store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model = Model(id="jsonl-rejected", provider="test", api="test")
            with self.assertRaisesRegex(ValueError, "不支持 JSONL Backend"):
                await DurableAgentHost.create(
                    session_id="jsonl-session",
                    state_dir=directory,
                    model=model,
                    stream_fn=ScriptedProvider([]).stream,
                    system_prompt="test",
                    tools=[],
                    store_backend="jsonl",
                )

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

            async def resume(payload, *, fencing_token, fenced_claim):
                nonlocal calls
                calls += 1
                entered.set()
                await release.wait()
                restored = DurableActionEnvelope.from_dict(payload["envelope"])

                async def fenced_write(context):
                    self.assertEqual(context.fencing_token, fencing_token)
                    self.assertEqual(
                        context.fencing_scope,
                        fenced_claim.resource_id,
                    )
                    return await _successful_write(
                        context.arguments,
                        context.idempotency_key,
                        context.actor,
                    )

                write = await WriteOperationService(
                    first_store,
                    first_approvals,
                ).execute(
                    restored.write_id,
                    actor=operator,
                    idempotency_key="coordinator-refund-key",
                    context_handler=fenced_write,
                    approval_resume_id=pending.approval.approval_id,
                    fencing_token=fencing_token,
                    fenced_claim=fenced_claim,
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
                    latest = await first_store.load(
                        session_id="session-1",
                        operation_id="operation-1",
                    )
                    await first_store.append_batch_if_fenced_claim(
                        "session-1",
                        "operation-1",
                        specs,
                        fenced_claim,
                        renew_lease_seconds=300,
                        expected_last_sequence=latest[-1].sequence,
                        expected_claim_entity_id=pending.approval.approval_id,
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
