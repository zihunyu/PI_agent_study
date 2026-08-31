"""Approval Resume 各持久化窗口和幂等恢复测试。"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import (  # noqa: E402
    ApprovalResumeCoordinator,
    ApprovalError,
    ApprovalService,
    DurableActionEnvelope,
    IdentityClaim,
    InMemoryOperationEventStore,
    StaticIdentityVerifier,
    WriteOperationService,
    replay_operation,
)
from pi_agent_loop.harness.approval_gateway import ApprovalResumeError  # noqa: E402
from pi_agent_loop.session.operation_store import (  # noqa: E402
    fenced_claim_resource_id,
)


class ApprovalResumeWindowTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_request拒绝duck身份且不落审批事实(self) -> None:
        store = InMemoryOperationEventStore()
        await store.append(
            "operation_started",
            "session-1",
            "operation-1",
            {"configuration": {}, "tools": []},
        )
        coordinator = ApprovalResumeCoordinator(
            store,
            ApprovalService(store),
        )
        envelope = DurableActionEnvelope(
            operation_id="operation-1",
            tool_call_id="forged-call",
            tool_name="refund",
            arguments={"order_id": "1001"},
            write_id="forged-write",
        )

        class DuckIdentity:
            principal_id = "forged-operator"
            roles = frozenset({"operator"})
            issuer = "forged"
            verification_id = "forged-verification"

        before = await store.load(operation_id="operation-1")
        with self.assertRaises(ApprovalError) as raised:
            await coordinator.request(
                session_id="session-1",
                operation_id="operation-1",
                requester=DuckIdentity(),  # type: ignore[arg-type]
                action=envelope.to_dict(),
                action_summary="伪造退款",
                required_role="approver",
                resume_payload={"envelope": envelope.to_dict()},
                idempotency_key="forged-key",
            )
        self.assertEqual(raised.exception.code, "verified_identity_required")
        self.assertEqual(
            await store.load(operation_id="operation-1"),
            before,
        )

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

    async def test_approval_resume回调收到fencing_generation(self) -> None:
        store, _approvals, coordinator, pending, operator, manager = (
            await self.setup_case()
        )
        observed: list[int] = []

        async def resume(payload, *, fencing_token):
            observed.append(fencing_token)
            return await self.claim_write(
                store,
                pending,
                operator,
                payload,
                result="fenced-result",
            )

        result = await coordinator.approve_and_resume(
            pending.approval.approval_id,
            approver=manager,
            consumer=operator,
            resume=resume,
        )
        self.assertEqual(result, "fenced-result")
        self.assertEqual(len(observed), 1)
        self.assertGreater(observed[0], 0)
        completed = next(
            event
            for event in await store.load()
            if event.type == "approval_resume_completed"
        )
        self.assertEqual(completed.data["fencingToken"], observed[0])

    async def test_approval_heartbeat丢失后旧worker不能迟到完成(self) -> None:
        store, approvals, _coordinator, pending, operator, manager = (
            await self.setup_case()
        )
        coordinator = ApprovalResumeCoordinator(
            store,
            approvals,
            claim_lease_seconds=0.06,
            claim_renew_interval_seconds=0.01,
        )
        entered = asyncio.Event()
        callback_cancelled = asyncio.Event()
        allow_late_return = asyncio.Event()
        lose_heartbeat = asyncio.Event()
        observed: list[int] = []
        original_renew = store.renew_fenced_claim

        async def controlled_renew(lease, *, lease_seconds=300):
            if lose_heartbeat.is_set():
                return False
            return await original_renew(lease, lease_seconds=lease_seconds)

        store.renew_fenced_claim = controlled_renew  # type: ignore[method-assign]

        async def stubborn_resume(_payload, *, fencing_token):
            observed.append(fencing_token)
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                callback_cancelled.set()
                await allow_late_return.wait()
                return "late-result"

        running = asyncio.create_task(
            coordinator.approve_and_resume(
                pending.approval.approval_id,
                approver=manager,
                consumer=operator,
                resume=stubborn_resume,
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2)
        lose_heartbeat.set()
        await asyncio.wait_for(callback_cancelled.wait(), timeout=2)
        await asyncio.sleep(0.08)

        successor = await store.acquire_fenced_claim(
            "approval_resume",
            fenced_claim_resource_id(
                "approval_resume",
                session_id="session-1",
                operation_id="operation-1",
                entity_id=pending.approval.approval_id,
            ),
            "worker-b",
            lease_seconds=1,
        )
        self.assertIsNotNone(successor)
        assert successor is not None
        self.assertGreater(successor.fencing_token, observed[0])

        allow_late_return.set()
        with self.assertRaises(ApprovalResumeError) as caught:
            await running
        self.assertEqual(caught.exception.code, "approval_resume_claim_lost")
        self.assertTrue(await store.verify_fenced_claim(successor))
        self.assertFalse(
            any(
                event.type == "approval_resume_completed"
                for event in await store.load()
            )
        )
        await store.release_fenced_claim(successor)

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
