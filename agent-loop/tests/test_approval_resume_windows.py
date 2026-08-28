"""Approval Resume 各持久化窗口和幂等恢复测试。"""

from __future__ import annotations

import asyncio
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
    StaticIdentityVerifier,
    WriteOperationService,
    replay_operation,
)


class ApprovalResumeWindowTests(unittest.IsolatedAsyncioTestCase):
    async def setup_case(self):
        store = InMemoryOperationEventStore()
        await store.append(
            "operation_started",
            "session-1",
            "operation-1",
            {"configuration": {}, "tools": []},
        )
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
        approvals = ApprovalService(store)
        coordinator = ApprovalResumeCoordinator(store, approvals)
        envelope = DurableActionEnvelope(
            operation_id="operation-1",
            tool_call_id="refund-call",
            tool_name="refund",
            arguments={"order_id": "1001"},
            write_id="refund-write",
        )
        pending = await coordinator.request(
            session_id="session-1",
            operation_id="operation-1",
            requester=operator,
            action=envelope.to_dict(),
            action_summary="退款订单 1001",
            required_role="approver",
            resume_payload={},
            idempotency_key="refund-write",
        )
        return store, approvals, coordinator, pending, operator, manager

    async def claim_write(
        self,
        store,
        pending,
        operator,
        payload,
        *,
        result=None,
        handler=None,
    ):
        envelope = DurableActionEnvelope.from_dict(payload["envelope"])
        writes = WriteOperationService(store, ApprovalService(store))

        async def default_handler(_arguments, _key, _actor):
            return {"status": "succeeded"}

        write = await writes.execute(
            envelope.write_id,
            actor=operator,
            idempotency_key="refund-write",
            handler=handler or default_handler,
            approval_resume_id=pending.approval.approval_id,
        )
        events = await store.load(
            session_id=pending.approval.session_id,
            operation_id=pending.approval.operation_id,
        )
        operation = replay_operation(events)
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
                pending.approval.session_id,
                pending.approval.operation_id,
                specs,
            )
        return result

    async def test_registered_waiting_不会自动执行(self) -> None:
        _store, _approvals, coordinator, _pending, operator, _manager = (
            await self.setup_case()
        )
        calls = 0

        async def resume(_payload):
            nonlocal calls
            calls += 1

        recovered = await coordinator.recover_incomplete(
            resume,
            consumer_resolver=lambda _pending: operator,
        )
        self.assertEqual(recovered, [])
        self.assertEqual(calls, 0)

    async def test_granted_未_consumed_未_started_可以恢复(self) -> None:
        store, approvals, _coordinator, pending, operator, manager = (
            await self.setup_case()
        )
        await approvals.grant(pending.approval.approval_id, manager)
        restarted = ApprovalResumeCoordinator(store, approvals)
        payloads: list[dict] = []

        async def resume(payload):
            payloads.append(payload)
            return await self.claim_write(
                store,
                pending,
                operator,
                payload,
                result="grant-window-recovered",
            )

        recovered = await restarted.recover_incomplete(
            resume,
            consumer_resolver=lambda _pending: operator,
        )
        self.assertEqual(recovered, [pending.approval.approval_id])
        restored = DurableActionEnvelope.from_dict(payloads[0]["envelope"])
        self.assertEqual(restored.tool_name, "refund")
        self.assertEqual(restored.arguments, {"order_id": "1001"})
        self.assertEqual(
            (await approvals.get(pending.approval.approval_id)).state,
            "consumed",
        )

    async def test_consumed_未_started_无需再次消费即可恢复(self) -> None:
        store, approvals, _coordinator, pending, operator, manager = (
            await self.setup_case()
        )
        await approvals.grant(pending.approval.approval_id, manager)
        await approvals.consume(
            pending.approval.approval_id,
            action=pending.action,
            consumer=operator,
        )
        restarted = ApprovalResumeCoordinator(store, approvals)
        calls = 0

        async def resume(payload):
            nonlocal calls
            calls += 1
            return await self.claim_write(
                store,
                pending,
                operator,
                payload,
                result="consumed-window-recovered",
            )

        recovered = await restarted.recover_incomplete(resume)
        self.assertEqual(recovered, [pending.approval.approval_id])
        self.assertEqual(calls, 1)
        events = await store.load()
        self.assertTrue(any(event.type == "approval_resume_started" for event in events))

    async def test_started_未_completed_可以恢复(self) -> None:
        store, approvals, _coordinator, pending, operator, manager = (
            await self.setup_case()
        )
        await approvals.grant(pending.approval.approval_id, manager)
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
        restarted = ApprovalResumeCoordinator(store, approvals)
        async def resume(payload):
            return await self.claim_write(
                store,
                pending,
                operator,
                payload,
                result=payload,
            )

        recovered = await restarted.recover_incomplete(resume)
        self.assertEqual(recovered, [pending.approval.approval_id])

    async def test_completed_重复调用不会再次执行(self) -> None:
        store, _approvals, coordinator, pending, operator, manager = (
            await self.setup_case()
        )
        calls = 0

        async def resume(payload):
            nonlocal calls
            calls += 1
            return await self.claim_write(
                store,
                pending,
                operator,
                payload,
                result={"status": "ok"},
            )

        first = await coordinator.approve_and_resume(
            pending.approval.approval_id,
            approver=manager,
            consumer=operator,
            resume=resume,
        )
        second = await coordinator.approve_and_resume(
            pending.approval.approval_id,
            approver=manager,
            consumer=operator,
            resume=resume,
        )
        self.assertEqual(first, {"status": "ok"})
        self.assertEqual(second, {"status": "ok"})
        self.assertEqual(calls, 1)

    async def test_rejected_不会恢复并写_cancelled(self) -> None:
        store, approvals, coordinator, pending, _operator, manager = (
            await self.setup_case()
        )
        await approvals.reject(
            pending.approval.approval_id,
            manager,
            reason="业务拒绝",
        )
        calls = 0

        async def resume(_payload):
            nonlocal calls
            calls += 1

        recovered = await coordinator.recover_incomplete(resume)
        self.assertEqual(recovered, [])
        self.assertEqual(calls, 0)
        self.assertTrue(
            any(
                event.type == "approval_resume_cancelled"
                for event in await store.load()
            )
        )

    async def test_同进程两个并发恢复只执行一次(self) -> None:
        _store, _approvals, coordinator, pending, operator, manager = (
            await self.setup_case()
        )
        calls = 0
        entered = asyncio.Event()

        async def resume(payload):
            nonlocal calls
            calls += 1
            entered.set()
            await asyncio.sleep(0.01)
            return await self.claim_write(
                _store,
                pending,
                operator,
                payload,
                result="one-result",
            )

        first = asyncio.create_task(
            coordinator.approve_and_resume(
                pending.approval.approval_id,
                approver=manager,
                consumer=operator,
                resume=resume,
            )
        )
        await entered.wait()
        second = asyncio.create_task(
            coordinator.approve_and_resume(
                pending.approval.approval_id,
                approver=manager,
                consumer=operator,
                resume=resume,
            )
        )
        self.assertEqual(await first, "one-result")
        self.assertEqual(await second, "one-result")
        self.assertEqual(calls, 1)

    async def test_approved_缺少_consumer_resolver_保持可恢复(self) -> None:
        store, approvals, _coordinator, pending, operator, manager = (
            await self.setup_case()
        )
        await approvals.grant(pending.approval.approval_id, manager)
        restarted = ApprovalResumeCoordinator(store, approvals)
        self.assertEqual(
            await restarted.recover_incomplete(lambda payload: asyncio.sleep(0)),
            [],
        )
        async def resume(payload):
            return await self.claim_write(
                store,
                pending,
                operator,
                payload,
                result=payload,
            )

        recovered = await restarted.recover_incomplete(
            resume,
            consumer_resolver=lambda _pending: operator,
        )
        self.assertEqual(recovered, [pending.approval.approval_id])

    async def test_started_后副作用重复依靠_idempotency_去重(self) -> None:
        store, approvals, _coordinator, pending, operator, manager = (
            await self.setup_case()
        )
        await approvals.grant(pending.approval.approval_id, manager)
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
        # 模拟副作用已成功，但进程在 Completed Event 前崩溃。
        applied_keys = {"refund:1001"}
        actual_side_effects = 1

        async def resume(payload):
            nonlocal actual_side_effects
            async def handler(_arguments, _key, _actor):
                nonlocal actual_side_effects
                key = "refund:1001"
                if key not in applied_keys:
                    applied_keys.add(key)
                    actual_side_effects += 1
                return {"status": "succeeded"}

            return await self.claim_write(
                store,
                pending,
                operator,
                payload,
                result={"status": "already-or-now-completed"},
                handler=handler,
            )

        restarted = ApprovalResumeCoordinator(store, approvals)
        await restarted.recover_incomplete(resume)
        self.assertEqual(actual_side_effects, 1)


if __name__ == "__main__":
    unittest.main()
