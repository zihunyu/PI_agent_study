from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import (  # noqa: E402
    ApprovalResumeCoordinator,
    ApprovalService,
    DurableActionEnvelope,
    IdentityClaim,
    InMemoryOperationEventStore,
    RecoveryCallbacks,
    OutcomeUnknownToolError,
    StartupRecoveryCoordinator,
    StaticIdentityVerifier,
    WriteOperationService,
)
from pi_agent_loop.writes import WriteOutcomeUnknownError  # noqa: E402
from pi_agent_loop.harness.approval import _failed_write_tool_result  # noqa: E402


class TransitionFailingStore(InMemoryOperationEventStore):
    def __init__(self, event_type: str, failures: int) -> None:
        super().__init__()
        self.event_type = event_type
        self.failures = failures

    async def append_batch(
        self,
        session_id,
        operation_id,
        events,
        *,
        expected_last_sequence=None,
        deadline_ms=None,
    ):
        if self.failures and any(
            event_type == self.event_type for event_type, _ in events
        ):
            self.failures -= 1
            raise RuntimeError("模拟持久化故障")
        return await super().append_batch(
            session_id,
            operation_id,
            events,
            expected_last_sequence=expected_last_sequence,
            deadline_ms=deadline_ms,
        )


class AdapterOutcomeUnknown(RuntimeError):
    outcome_unknown = True
    code = "outcome_unknown"
    public_message = "上游结果需要核对"


class WriteOutcomeBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def test_标准unknown异常生成的tool_result保留不确定标志(self) -> None:
        envelope = DurableActionEnvelope(
            operation_id="operation-1",
            tool_call_id="write-call",
            tool_name="external_write",
            arguments={},
            write_id="write-1",
        )
        result = _failed_write_tool_result(
            envelope,
            OutcomeUnknownToolError(
                "内部错误不得直接展示",
                operation_id="upstream-1",
                idempotency_key="secret-key",
                reconciliation_name="lookup_result",
            ),
        )

        self.assertTrue(result["isError"])
        self.assertTrue(result["details"]["outcomeUnknown"])
        self.assertEqual(result["details"]["message"], "写操作结果未知，需要人工核对")
        self.assertNotIn("secret-key", str(result))

    async def identity(self, principal: str = "operator", role: str = "operator"):
        verifier = StaticIdentityVerifier(
            {principal: (f"{principal}-secret", {role})}
        )
        return await verifier.verify(
            IdentityClaim(principal, f"{principal}-secret")
        )

    async def start(self, store, operation_id: str = "operation-1") -> None:
        await store.append(
            "operation_started",
            "session-1",
            operation_id,
            {"configuration": {}, "tools": []},
        )

    async def prepare(self, store, *, key: str = "write-key"):
        await self.start(store)
        actor = await self.identity()
        writes = WriteOperationService(store, ApprovalService(store))
        write = await writes.prepare(
            session_id="session-1",
            operation_id="operation-1",
            tool_name="external_write",
            arguments={"value": 1},
            idempotency_key=key,
            requester=actor,
            requires_approval=False,
        )
        return writes, write, actor

    async def test_副作用成功但成功事件落盘失败时进入unknown而非failed(self) -> None:
        store = TransitionFailingStore("write_succeeded", failures=1)
        writes, write, actor = await self.prepare(store)
        external_effects: list[str] = []

        async def handler(*_args):
            external_effects.append("charged")
            return {"status": "charged"}

        with self.assertRaises(WriteOutcomeUnknownError):
            await writes.execute(
                write.write_id,
                actor=actor,
                idempotency_key="write-key",
                handler=handler,
            )

        self.assertEqual(external_effects, ["charged"])
        self.assertEqual((await writes.get(write.write_id)).state, "outcome_unknown")
        event_types = [event.type for event in await store.load()]
        self.assertNotIn("write_failed", event_types)
        self.assertIn("write_outcome_unknown", event_types)

    async def test_成功和unknown事件都无法落盘时保持submitting(self) -> None:
        store = TransitionFailingStore("write_succeeded", failures=1)
        writes, write, actor = await self.prepare(store)
        original_append = store.append_batch

        async def fail_terminal_events(*args, **kwargs):
            events = args[2]
            if any(
                event_type in {"write_succeeded", "write_outcome_unknown"}
                for event_type, _ in events
            ):
                raise RuntimeError("模拟 Store 持续不可用")
            return await original_append(*args, **kwargs)

        store.append_batch = fail_terminal_events  # type: ignore[method-assign]

        async def handler(*_args):
            return {"status": "charged"}

        with self.assertRaises(WriteOutcomeUnknownError):
            await writes.execute(
                write.write_id,
                actor=actor,
                idempotency_key="write-key",
                handler=handler,
            )

        self.assertEqual((await writes.get(write.write_id)).state, "submitting")
        self.assertNotIn(
            "write_failed",
            [event.type for event in await store.load()],
        )

    async def test_业务adapter通过通用标志声明outcome_unknown(self) -> None:
        store = InMemoryOperationEventStore()
        writes, write, actor = await self.prepare(store)

        async def handler(*_args):
            raise AdapterOutcomeUnknown("不得持久化内部响应")

        with self.assertRaises(AdapterOutcomeUnknown):
            await writes.execute(
                write.write_id,
                actor=actor,
                idempotency_key="write-key",
                handler=handler,
            )

        self.assertEqual((await writes.get(write.write_id)).state, "outcome_unknown")
        events = await store.load()
        self.assertFalse(any(event.type == "write_failed" for event in events))
        unknown = next(event for event in events if event.type == "write_outcome_unknown")
        self.assertEqual(unknown.data["errorCode"], "outcome_unknown")
        self.assertNotIn("不得持久化内部响应", str(unknown.data))

    async def test_approval_resume遇到unknown不写resume_failed(self) -> None:
        store = InMemoryOperationEventStore()
        await self.start(store)
        requester = await self.identity()
        approver = await self.identity("manager", "approver")
        approvals = ApprovalService(store)
        coordinator = ApprovalResumeCoordinator(store, approvals)
        writes = WriteOperationService(store, approvals)
        envelope = DurableActionEnvelope(
            operation_id="operation-1",
            tool_call_id="write-call",
            tool_name="external_write",
            arguments={"value": 1},
            write_id="write-1",
        )
        pending = await coordinator.request(
            session_id="session-1",
            operation_id="operation-1",
            requester=requester,
            action=envelope.to_dict(),
            action_summary="执行外部写操作",
            required_role="approver",
            resume_payload={"envelope": envelope.to_dict()},
            idempotency_key="approval-write-key",
        )

        async def resume(_payload):
            async def handler(*_args):
                raise AdapterOutcomeUnknown("上游连接断开")

            return await writes.execute(
                envelope.write_id,
                actor=requester,
                idempotency_key="approval-write-key",
                handler=handler,
                approval_resume_id=pending.approval.approval_id,
            )

        with self.assertRaises(AdapterOutcomeUnknown):
            await coordinator.approve_and_resume(
                pending.approval.approval_id,
                approver=approver,
                consumer=requester,
                resume=resume,
            )

        events = await store.load()
        self.assertTrue(any(event.type == "write_outcome_unknown" for event in events))
        self.assertFalse(any(event.type == "approval_resume_failed" for event in events))

    async def test_旧版terminal加unknown矛盾日志进入人工核对而非静默跳过(self) -> None:
        store = InMemoryOperationEventStore()
        await self.start(store)
        await store.append_batch(
            "session-1",
            "operation-1",
            [
                (
                    "write_prepared",
                    {
                        "writeId": "write-1",
                        "toolName": "external_write",
                        "arguments": {},
                        "actionHash": "action",
                        "idempotencyKeyHash": "key-hash",
                        "requesterId": "operator",
                    },
                ),
                ("write_approved", {"writeId": "write-1", "approvalId": None}),
                ("write_submitting", {"writeId": "write-1"}),
                ("write_outcome_unknown", {"writeId": "write-1"}),
                ("operation_finished", {"outcome": "failed"}),
            ],
        )

        async def unexpected(*_args):
            raise AssertionError("矛盾终态不能自动执行回调")

        report = await StartupRecoveryCoordinator(
            store,
            RecoveryCallbacks(unexpected, unexpected, unexpected),
        ).recover_all()

        self.assertEqual(report.manual_intervention, ("operation-1",))
        self.assertEqual(report.failed, ())


if __name__ == "__main__":
    unittest.main()
