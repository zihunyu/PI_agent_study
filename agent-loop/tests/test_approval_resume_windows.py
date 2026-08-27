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
    IdentityClaim,
    InMemoryOperationEventStore,
    StaticIdentityVerifier,
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
        pending = await coordinator.request(
            session_id="session-1",
            operation_id="operation-1",
            requester=operator,
            action={"tool": "refund", "arguments": {"order_id": "1001"}},
            action_summary="退款订单 1001",
            required_role="approver",
            resume_payload={"order_id": "1001"},
        )
        return store, approvals, coordinator, pending, operator, manager

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
            return "grant-window-recovered"

        recovered = await restarted.recover_incomplete(
            resume,
            consumer_resolver=lambda _pending: operator,
        )
        self.assertEqual(recovered, [pending.approval.approval_id])
        self.assertEqual(payloads, [{"order_id": "1001"}])
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

        async def resume(_payload):
            nonlocal calls
            calls += 1
            return "consumed-window-recovered"

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
        recovered = await restarted.recover_incomplete(
            lambda payload: asyncio.sleep(0, result=payload)
        )
        self.assertEqual(recovered, [pending.approval.approval_id])

    async def test_completed_重复调用不会再次执行(self) -> None:
        _store, _approvals, coordinator, pending, operator, manager = (
            await self.setup_case()
        )
        calls = 0

        async def resume(_payload):
            nonlocal calls
            calls += 1
            return {"status": "ok"}

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

        async def resume(_payload):
            nonlocal calls
            calls += 1
            entered.set()
            await asyncio.sleep(0.01)
            return "one-result"

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
        recovered = await restarted.recover_incomplete(
            lambda payload: asyncio.sleep(0, result=payload),
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

        async def resume(_payload):
            nonlocal actual_side_effects
            key = "refund:1001"
            if key not in applied_keys:
                applied_keys.add(key)
                actual_side_effects += 1
            return {"status": "already-or-now-completed"}

        restarted = ApprovalResumeCoordinator(store, approvals)
        await restarted.recover_incomplete(resume)
        self.assertEqual(actual_side_effects, 1)


if __name__ == "__main__":
    unittest.main()
