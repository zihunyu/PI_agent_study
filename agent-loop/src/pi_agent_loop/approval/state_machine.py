"""持久 Approval 状态机：请求、批准、拒绝、过期和一次性消费。"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from ..security import VerifiedIdentity
from ..session.operation_events import OperationEvent
from ..session.operation_store import (
    OperationEventStore,
    OperationStoreConflictError,
    operation_last_sequence,
)

_MAX_CONFLICT_RETRIES = 20


class ApprovalError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    approval_id: str
    session_id: str
    operation_id: str
    state: str
    action_hash: str
    action_summary: str
    requester_id: str
    required_role: str
    expires_at: int
    approver_id: str | None = None


class ApprovalService:
    def __init__(self, store: OperationEventStore) -> None:
        self.store = store

    async def request(
        self,
        *,
        session_id: str,
        operation_id: str,
        requester: VerifiedIdentity,
        action: dict[str, Any],
        action_summary: str,
        required_role: str,
        ttl_seconds: float = 300,
    ) -> ApprovalRecord:
        approval_id, data = build_approval_request_event(
            requester=requester,
            action=action,
            action_summary=action_summary,
            required_role=required_role,
            ttl_seconds=ttl_seconds,
        )
        for _ in range(_MAX_CONFLICT_RETRIES):
            events = await self.store.load(
                session_id=session_id,
                operation_id=operation_id,
            )
            if not events:
                raise ApprovalError(
                    "operation_not_found",
                    "Approval 必须属于已持久化的 Operation",
                )
            try:
                await self.store.append_batch(
                    session_id,
                    operation_id,
                    [("approval_requested", data)],
                    expected_last_sequence=operation_last_sequence(events),
                )
                return await self.get(approval_id)
            except OperationStoreConflictError:
                continue
        raise ApprovalError("approval_conflict", "Approval Request 并发冲突")

    async def grant(
        self,
        approval_id: str,
        approver: VerifiedIdentity,
        *,
        allow_self_approval: bool = False,
    ) -> ApprovalRecord:
        record = await self.get(approval_id)
        if record.required_role not in approver.roles:
            raise ApprovalError("approval_role_missing", "审批人缺少所需角色")
        if (
            not allow_self_approval
            and record.requester_id == approver.principal_id
        ):
            raise ApprovalError(
                "self_approval_forbidden",
                "申请人不能审批自己的操作",
            )
        return await self._append_transition(
            record,
            "approval_granted",
            {
                "approvalId": approval_id,
                "approverId": approver.principal_id,
                "approverVerificationId": approver.verification_id,
            },
            required_state="waiting",
            invalid_code="approval_not_waiting",
            invalid_message="Approval 已不在等待状态",
        )

    async def reject(
        self,
        approval_id: str,
        approver: VerifiedIdentity,
        *,
        reason: str,
    ) -> ApprovalRecord:
        record = await self.get(approval_id)
        if record.required_role not in approver.roles:
            raise ApprovalError("approval_role_missing", "审批人缺少所需角色")
        return await self._append_transition(
            record,
            "approval_rejected",
            {
                "approvalId": approval_id,
                "approverId": approver.principal_id,
                "approverVerificationId": approver.verification_id,
                "reason": reason,
            },
            required_state="waiting",
            invalid_code="approval_not_waiting",
            invalid_message="Approval 已不在等待状态",
        )

    async def consume(
        self,
        approval_id: str,
        *,
        action: dict[str, Any],
        consumer: VerifiedIdentity,
    ) -> ApprovalRecord:
        record = await self.get(approval_id)
        if record.action_hash != action_digest(action):
            raise ApprovalError(
                "approval_action_mismatch",
                "Approval 与待执行操作不匹配",
            )
        return await self._append_transition(
            record,
            "approval_consumed",
            {
                "approvalId": approval_id,
                "consumerId": consumer.principal_id,
                "consumerVerificationId": consumer.verification_id,
            },
            required_state="approved",
            invalid_code="approval_not_approved",
            invalid_message="Approval 尚未批准或已消费",
        )

    async def get(self, approval_id: str) -> ApprovalRecord:
        events = await self.store.load()
        record = _replay_approval(events, approval_id)
        if (
            record.state == "waiting"
            and record.expires_at <= int(time.time() * 1000)
        ):
            return await self._expire(record)
        return record

    async def _expire(self, record: ApprovalRecord) -> ApprovalRecord:
        for _ in range(_MAX_CONFLICT_RETRIES):
            events = await self.store.load(
                session_id=record.session_id,
                operation_id=record.operation_id,
            )
            current = _replay_approval(events, record.approval_id)
            if current.state != "waiting":
                return current
            if current.expires_at > int(time.time() * 1000):
                return current
            try:
                await self.store.append_batch(
                    current.session_id,
                    current.operation_id,
                    [
                        (
                            "approval_expired",
                            {"approvalId": current.approval_id},
                        )
                    ],
                    expected_last_sequence=operation_last_sequence(events),
                )
            except OperationStoreConflictError:
                continue
            return _replay_approval(
                await self.store.load(
                    session_id=current.session_id,
                    operation_id=current.operation_id,
                ),
                current.approval_id,
            )
        raise ApprovalError("approval_conflict", "Approval Expire 并发冲突")

    async def _append_transition(
        self,
        record: ApprovalRecord,
        event_type: str,
        data: dict[str, Any],
        *,
        required_state: str,
        invalid_code: str,
        invalid_message: str,
    ) -> ApprovalRecord:
        for _ in range(_MAX_CONFLICT_RETRIES):
            events = await self.store.load(
                session_id=record.session_id,
                operation_id=record.operation_id,
            )
            current = _replay_approval(events, record.approval_id)
            if current.state != required_state:
                raise ApprovalError(invalid_code, invalid_message)
            try:
                await self.store.append_batch(
                    current.session_id,
                    current.operation_id,
                    [(event_type, data)],
                    expected_last_sequence=operation_last_sequence(events),
                )
            except OperationStoreConflictError:
                continue
            return _replay_approval(
                await self.store.load(
                    session_id=current.session_id,
                    operation_id=current.operation_id,
                ),
                current.approval_id,
            )
        raise ApprovalError("approval_conflict", "Approval 状态转换并发冲突")


def build_approval_request_event(
    *,
    requester: VerifiedIdentity,
    action: dict[str, Any],
    action_summary: str,
    required_role: str,
    ttl_seconds: float = 300,
) -> tuple[str, dict[str, Any]]:
    if ttl_seconds <= 0:
        raise ValueError("Approval ttl_seconds 必须大于 0")
    approval_id = str(uuid4())
    expires_at = int(time.time() * 1000 + ttl_seconds * 1000)
    return approval_id, {
        "approvalId": approval_id,
        "actionHash": action_digest(action),
        "actionSummary": action_summary,
        "requesterId": requester.principal_id,
        "requesterVerificationId": requester.verification_id,
        "requiredRole": required_role,
        "expiresAt": expires_at,
    }


def action_digest(action: dict[str, Any]) -> str:
    canonical = json.dumps(
        action,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _replay_approval(
    events: list[OperationEvent],
    approval_id: str,
) -> ApprovalRecord:
    record: ApprovalRecord | None = None
    for event in events:
        if event.data.get("approvalId") != approval_id:
            continue
        if event.type == "approval_requested":
            if record is not None:
                raise ApprovalError(
                    "duplicate_approval",
                    "Approval Request 重复",
                )
            record = ApprovalRecord(
                approval_id=approval_id,
                session_id=event.session_id,
                operation_id=event.operation_id,
                state="waiting",
                action_hash=str(event.data.get("actionHash", "")),
                action_summary=str(event.data.get("actionSummary", "")),
                requester_id=str(event.data.get("requesterId", "")),
                required_role=str(event.data.get("requiredRole", "")),
                expires_at=int(event.data.get("expiresAt", 0)),
            )
        elif record is None:
            raise ApprovalError(
                "approval_event_without_request",
                "Approval 事件缺少 Request",
            )
        elif event.type == "approval_granted":
            if record.state != "waiting":
                raise ApprovalError(
                    "invalid_approval_transition",
                    "Approval 不能重复批准",
                )
            record = replace(
                record,
                state="approved",
                approver_id=str(event.data.get("approverId", "")),
            )
        elif event.type == "approval_rejected":
            if record.state != "waiting":
                raise ApprovalError(
                    "invalid_approval_transition",
                    "Approval 不能重复拒绝",
                )
            record = replace(record, state="rejected")
        elif event.type == "approval_expired":
            if record.state != "waiting":
                raise ApprovalError(
                    "invalid_approval_transition",
                    "只有 Waiting Approval 可以过期",
                )
            record = replace(record, state="expired")
        elif event.type == "approval_consumed":
            if record.state != "approved":
                raise ApprovalError(
                    "invalid_approval_transition",
                    "只有 Approved 可以消费",
                )
            record = replace(record, state="consumed")
    if record is None:
        raise ApprovalError(
            "approval_not_found",
            f"Approval 不存在：{approval_id}",
        )
    return record
