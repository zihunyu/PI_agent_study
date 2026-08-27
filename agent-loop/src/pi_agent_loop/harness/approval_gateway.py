"""Approval 批准后恢复原 Operation 的持久协调器。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..approval import ApprovalRecord, ApprovalService
from ..security import VerifiedIdentity
from ..session.operation_store import OperationEventStore


@dataclass(frozen=True, slots=True)
class PendingApprovalResume:
    approval: ApprovalRecord
    action: dict[str, Any]
    resume_payload: dict[str, Any]


class ApprovalResumeCoordinator:
    def __init__(
        self,
        store: OperationEventStore,
        approvals: ApprovalService,
    ) -> None:
        self.store = store
        self.approvals = approvals

    async def request(
        self,
        *,
        session_id: str,
        operation_id: str,
        requester: VerifiedIdentity,
        action: dict[str, Any],
        action_summary: str,
        required_role: str,
        resume_payload: dict[str, Any],
        ttl_seconds: float = 300,
    ) -> PendingApprovalResume:
        approval = await self.approvals.request(
            session_id=session_id,
            operation_id=operation_id,
            requester=requester,
            action=action,
            action_summary=action_summary,
            required_role=required_role,
            ttl_seconds=ttl_seconds,
        )
        await self.store.append(
            "approval_resume_registered",
            session_id,
            operation_id,
            {
                "approvalId": approval.approval_id,
                "action": action,
                "resumePayload": resume_payload,
            },
        )
        return PendingApprovalResume(approval, action, resume_payload)

    async def approve_and_resume(
        self,
        approval_id: str,
        *,
        approver: VerifiedIdentity,
        consumer: VerifiedIdentity,
        resume: Callable[[dict[str, Any]], Awaitable[Any]],
    ) -> Any:
        approval = await self.approvals.grant(approval_id, approver)
        pending = await self.get_pending(approval_id)
        await self.approvals.consume(
            approval_id,
            action=pending.action,
            consumer=consumer,
        )
        await self.store.append(
            "approval_resume_started",
            approval.session_id,
            approval.operation_id,
            {"approvalId": approval_id, "consumerId": consumer.principal_id},
        )
        try:
            result = await resume(pending.resume_payload)
        except Exception as error:
            await self.store.append(
                "approval_resume_failed",
                approval.session_id,
                approval.operation_id,
                {"approvalId": approval_id, "error": str(error)},
            )
            raise
        await self.store.append(
            "approval_resume_completed",
            approval.session_id,
            approval.operation_id,
            {"approvalId": approval_id},
        )
        return result

    async def recover_incomplete(
        self,
        resume: Callable[[dict[str, Any]], Awaitable[Any]],
    ) -> list[str]:
        """进程重启后继续已消费 Approval 但未完成的 Resume。"""

        events = await self.store.load()
        started = {
            str(event.data.get("approvalId")): event
            for event in events
            if event.type == "approval_resume_started"
        }
        terminal = {
            str(event.data.get("approvalId"))
            for event in events
            if event.type in {"approval_resume_completed", "approval_resume_failed"}
        }
        recovered: list[str] = []
        for approval_id, started_event in started.items():
            if not approval_id or approval_id in terminal:
                continue
            pending = await self.get_pending(approval_id)
            try:
                await resume(pending.resume_payload)
            except Exception as error:
                await self.store.append(
                    "approval_resume_failed",
                    started_event.session_id,
                    started_event.operation_id,
                    {"approvalId": approval_id, "error": str(error), "recovery": True},
                )
                continue
            await self.store.append(
                "approval_resume_completed",
                started_event.session_id,
                started_event.operation_id,
                {"approvalId": approval_id, "recovery": True},
            )
            recovered.append(approval_id)
        return recovered

    async def get_pending(self, approval_id: str) -> PendingApprovalResume:
        approval = await self.approvals.get(approval_id)
        events = await self.store.load(
            session_id=approval.session_id,
            operation_id=approval.operation_id,
        )
        event = next(
            (
                item
                for item in reversed(events)
                if item.type == "approval_resume_registered"
                and item.data.get("approvalId") == approval_id
            ),
            None,
        )
        if event is None:
            raise KeyError(f"Approval Resume Payload 不存在：{approval_id}")
        return PendingApprovalResume(
            approval=approval,
            action=dict(event.data.get("action", {})),
            resume_payload=dict(event.data.get("resumePayload", {})),
        )
