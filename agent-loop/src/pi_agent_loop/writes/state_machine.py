"""持久写操作状态机：可信身份、Approval、幂等和结果核对。"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from ..approval import ApprovalService, action_digest
from ..retry import OutcomeUnknownToolError
from ..security import VerifiedIdentity
from ..session.operation_events import OperationEvent
from ..session.operation_store import OperationEventStore


class WriteOperationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class WriteOperation:
    write_id: str
    session_id: str
    operation_id: str
    tool_name: str
    arguments: dict[str, Any]
    action_hash: str
    idempotency_key_hash: str
    requester_id: str
    state: str
    approval_id: str | None = None
    result: dict[str, Any] | None = None


WriteHandler = Callable[
    [dict[str, Any], str, VerifiedIdentity],
    Awaitable[dict[str, Any]],
]
ReconcileHandler = Callable[[WriteOperation], Awaitable[dict[str, Any]]]


class WriteOperationService:
    def __init__(
        self,
        store: OperationEventStore,
        approvals: ApprovalService,
    ) -> None:
        self.store = store
        self.approvals = approvals

    async def prepare(
        self,
        *,
        session_id: str,
        operation_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        idempotency_key: str,
        requester: VerifiedIdentity,
        requires_approval: bool,
        required_approval_role: str = "approver",
    ) -> WriteOperation:
        key_hash = _hash_secret(idempotency_key)
        action = {"tool": tool_name, "arguments": arguments}
        digest = action_digest(action)
        existing = await self._find_by_idempotency(key_hash)
        if existing is not None:
            if existing.action_hash != digest:
                raise WriteOperationError(
                    "idempotency_conflict",
                    "同一个 Idempotency Key 不能用于不同操作",
                )
            return existing

        write_id = str(uuid4())
        await self.store.append(
            "write_prepared",
            session_id,
            operation_id,
            {
                "writeId": write_id,
                "toolName": tool_name,
                "arguments": arguments,
                "actionHash": digest,
                "idempotencyKeyHash": key_hash,
                "requesterId": requester.principal_id,
                "requesterVerificationId": requester.verification_id,
            },
        )
        if requires_approval:
            approval = await self.approvals.request(
                session_id=session_id,
                operation_id=operation_id,
                requester=requester,
                action=action,
                action_summary=f"执行写工具 {tool_name}",
                required_role=required_approval_role,
            )
            await self.store.append(
                "write_waiting_approval",
                session_id,
                operation_id,
                {"writeId": write_id, "approvalId": approval.approval_id},
            )
        else:
            await self.store.append(
                "write_approved",
                session_id,
                operation_id,
                {"writeId": write_id, "approvalId": None},
            )
        return await self.get(write_id)

    async def execute(
        self,
        write_id: str,
        *,
        actor: VerifiedIdentity,
        idempotency_key: str,
        handler: WriteHandler,
    ) -> WriteOperation:
        record = await self.get(write_id)
        if record.idempotency_key_hash != _hash_secret(idempotency_key):
            raise WriteOperationError("idempotency_key_mismatch", "Idempotency Key 不匹配")
        if record.state == "succeeded":
            return record
        if record.state == "waiting_approval":
            if record.approval_id is None:
                raise WriteOperationError("approval_missing", "写操作缺少 Approval ID")
            await self.approvals.consume(
                record.approval_id,
                action={"tool": record.tool_name, "arguments": record.arguments},
                consumer=actor,
            )
            await self.store.append(
                "write_approved",
                record.session_id,
                record.operation_id,
                {"writeId": write_id, "approvalId": record.approval_id},
            )
            record = await self.get(write_id)
        if record.state != "approved":
            raise WriteOperationError(
                "write_not_executable",
                f"写操作当前状态 {record.state} 不能执行",
            )

        await self.store.append(
            "write_submitting",
            record.session_id,
            record.operation_id,
            {
                "writeId": write_id,
                "actorId": actor.principal_id,
                "actorVerificationId": actor.verification_id,
            },
        )
        try:
            result = await handler(record.arguments, idempotency_key, actor)
        except OutcomeUnknownToolError as error:
            await self.store.append(
                "write_outcome_unknown",
                record.session_id,
                record.operation_id,
                {
                    "writeId": write_id,
                    "operationId": error.operation_id,
                    "reconciliationName": error.reconciliation_name,
                },
            )
            raise
        except Exception as error:
            await self.store.append(
                "write_failed",
                record.session_id,
                record.operation_id,
                {"writeId": write_id, "error": str(error)},
            )
            raise
        await self.store.append(
            "write_succeeded",
            record.session_id,
            record.operation_id,
            {"writeId": write_id, "result": result},
        )
        return await self.get(write_id)

    async def reconcile(
        self,
        write_id: str,
        handler: ReconcileHandler,
    ) -> WriteOperation:
        record = await self.get(write_id)
        if record.state != "outcome_unknown":
            raise WriteOperationError("write_not_uncertain", "只有 outcome_unknown 可以核对")
        await self.store.append(
            "write_reconciling",
            record.session_id,
            record.operation_id,
            {"writeId": write_id},
        )
        result = await handler(record)
        success = result.get("status") == "succeeded"
        await self.store.append(
            "write_succeeded" if success else "write_failed",
            record.session_id,
            record.operation_id,
            {"writeId": write_id, "result": result},
        )
        return await self.get(write_id)

    async def get(self, write_id: str) -> WriteOperation:
        return _replay_write(await self.store.load(), write_id)

    async def _find_by_idempotency(self, key_hash: str) -> WriteOperation | None:
        events = await self.store.load()
        write_ids = [
            str(event.data.get("writeId"))
            for event in events
            if event.type == "write_prepared"
            and event.data.get("idempotencyKeyHash") == key_hash
        ]
        return _replay_write(events, write_ids[-1]) if write_ids else None


def _replay_write(events: list[OperationEvent], write_id: str) -> WriteOperation:
    record: WriteOperation | None = None
    for event in events:
        if event.data.get("writeId") != write_id:
            continue
        if event.type == "write_prepared":
            if record is not None:
                raise WriteOperationError("duplicate_write", "Write Prepared 重复")
            record = WriteOperation(
                write_id=write_id,
                session_id=event.session_id,
                operation_id=event.operation_id,
                tool_name=str(event.data.get("toolName", "")),
                arguments=dict(event.data.get("arguments", {})),
                action_hash=str(event.data.get("actionHash", "")),
                idempotency_key_hash=str(event.data.get("idempotencyKeyHash", "")),
                requester_id=str(event.data.get("requesterId", "")),
                state="prepared",
            )
        elif record is None:
            raise WriteOperationError("write_event_without_prepare", "写事件缺少 Prepared")
        elif event.type == "write_waiting_approval":
            record = replace(
                record,
                state="waiting_approval",
                approval_id=str(event.data.get("approvalId", "")),
            )
        elif event.type == "write_approved":
            if record.state not in {"prepared", "waiting_approval"}:
                raise WriteOperationError("invalid_write_transition", "不能进入 Approved")
            record = replace(record, state="approved")
        elif event.type == "write_submitting":
            if record.state != "approved":
                raise WriteOperationError("invalid_write_transition", "只有 Approved 可以提交")
            record = replace(record, state="submitting")
        elif event.type == "write_succeeded":
            if record.state not in {"submitting", "reconciling"}:
                raise WriteOperationError("invalid_write_transition", "当前状态不能成功")
            record = replace(record, state="succeeded", result=dict(event.data.get("result", {})))
        elif event.type == "write_failed":
            if record.state not in {"submitting", "reconciling"}:
                raise WriteOperationError("invalid_write_transition", "当前状态不能失败")
            record = replace(record, state="failed", result=dict(event.data.get("result", {})))
        elif event.type == "write_outcome_unknown":
            if record.state != "submitting":
                raise WriteOperationError("invalid_write_transition", "只有 Submitting 可变成 Unknown")
            record = replace(record, state="outcome_unknown")
        elif event.type == "write_reconciling":
            if record.state != "outcome_unknown":
                raise WriteOperationError("invalid_write_transition", "只有 Unknown 可以核对")
            record = replace(record, state="reconciling")
    if record is None:
        raise WriteOperationError("write_not_found", f"Write 不存在：{write_id}")
    return record


def _hash_secret(value: str) -> str:
    if not value:
        raise ValueError("Idempotency Key 不能为空")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
