"""持久写操作状态机：可信身份、Approval、幂等和结果核对。"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from ..approval.state_machine import (
    ApprovalError,
    ApprovalService,
    _replay_approval,
    action_digest,
    build_approval_request_event,
)
from ..retry import OutcomeUnknownToolError
from ..security import VerifiedIdentity
from ..session.operation_events import OperationEvent
from ..session.operation_state import replay_operation_with_specs
from ..session.operation_store import (
    OperationEventSpec,
    OperationEventStore,
    OperationStoreConflictError,
    operation_last_sequence,
)

_MAX_CONFLICT_RETRIES = 20


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
    tool_call_id: str | None = None
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
        tool_call_id: str | None = None,
    ) -> WriteOperation:
        key_hash = _hash_secret(idempotency_key)
        action = {"tool": tool_name, "arguments": arguments}
        digest = action_digest(action)
        existing = await self._find_by_idempotency(key_hash)
        if existing is not None:
            return _same_idempotent_action(existing, digest)

        write_id = str(uuid4())
        specs: list[OperationEventSpec] = [
            (
                "write_prepared",
                {
                    "writeId": write_id,
                    "toolName": tool_name,
                    "arguments": arguments,
                    "actionHash": digest,
                    "idempotencyKeyHash": key_hash,
                    "requesterId": requester.principal_id,
                    "requesterVerificationId": requester.verification_id,
                    "toolCallId": tool_call_id,
                },
            )
        ]
        if requires_approval:
            approval_id, approval_data = build_approval_request_event(
                requester=requester,
                action=action,
                action_summary=f"执行写工具 {tool_name}",
                required_role=required_approval_role,
            )
            specs.extend(
                [
                    ("approval_requested", approval_data),
                    (
                        "write_waiting_approval",
                        {"writeId": write_id, "approvalId": approval_id},
                    ),
                ]
            )
        else:
            specs.append(
                (
                    "write_approved",
                    {"writeId": write_id, "approvalId": None},
                )
            )

        for _ in range(_MAX_CONFLICT_RETRIES):
            existing = await self._find_by_idempotency(key_hash)
            if existing is not None:
                return _same_idempotent_action(existing, digest)
            events = await self.store.load(
                session_id=session_id,
                operation_id=operation_id,
            )
            if not events:
                raise WriteOperationError(
                    "operation_not_found",
                    "Write 必须属于已持久化的 Operation",
                )
            replay_operation_with_specs(events, specs)
            try:
                await self.store.append_batch(
                    session_id,
                    operation_id,
                    specs,
                    expected_last_sequence=operation_last_sequence(events),
                )
            except OperationStoreConflictError:
                continue
            return await self.get(write_id)
        raise WriteOperationError("write_conflict", "Write Prepare 并发冲突")

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
            raise WriteOperationError(
                "idempotency_key_mismatch",
                "Idempotency Key 不匹配",
            )

        claimed: WriteOperation | None = None
        for _ in range(_MAX_CONFLICT_RETRIES):
            events = await self.store.load(
                session_id=record.session_id,
                operation_id=record.operation_id,
            )
            current = _replay_write(events, write_id)
            if current.state == "succeeded":
                return current
            specs: list[OperationEventSpec] = []
            if current.state == "waiting_approval":
                if current.approval_id is None:
                    raise WriteOperationError(
                        "approval_missing",
                        "写操作缺少 Approval ID",
                    )
                approval = _replay_approval(events, current.approval_id)
                if approval.state != "approved":
                    raise ApprovalError(
                        "approval_not_approved",
                        "Approval 尚未批准或已消费",
                    )
                if approval.action_hash != current.action_hash:
                    raise WriteOperationError(
                        "approval_action_mismatch",
                        "Approval 与 Write Action 不匹配",
                    )
                specs.extend(
                    [
                        (
                            "approval_consumed",
                            {
                                "approvalId": current.approval_id,
                                "consumerId": actor.principal_id,
                                "consumerVerificationId": actor.verification_id,
                            },
                        ),
                        (
                            "write_approved",
                            {
                                "writeId": write_id,
                                "approvalId": current.approval_id,
                            },
                        ),
                    ]
                )
            elif current.state != "approved":
                raise WriteOperationError(
                    "write_not_executable",
                    f"写操作当前状态 {current.state} 不能执行",
                )

            specs.extend(_tool_dispatch_specs(events, current))
            specs.append(
                (
                    "write_submitting",
                    {
                        "writeId": write_id,
                        "actorId": actor.principal_id,
                        "actorVerificationId": actor.verification_id,
                    },
                )
            )
            replay_operation_with_specs(events, specs)
            try:
                await self.store.append_batch(
                    current.session_id,
                    current.operation_id,
                    specs,
                    expected_last_sequence=operation_last_sequence(events),
                )
            except OperationStoreConflictError:
                continue
            claimed = replace(current, state="submitting")
            break
        if claimed is None:
            raise WriteOperationError("write_conflict", "Write Claim 并发冲突")

        try:
            result = await handler(claimed.arguments, idempotency_key, actor)
        except OutcomeUnknownToolError as error:
            await self._finish_transition(
                claimed,
                required_state="submitting",
                event_type="write_outcome_unknown",
                data={
                    "writeId": write_id,
                    "operationId": error.operation_id,
                    "reconciliationName": error.reconciliation_name,
                },
            )
            raise
        except Exception as error:
            await self._finish_transition(
                claimed,
                required_state="submitting",
                event_type="write_failed",
                data={"writeId": write_id, "error": str(error)},
            )
            raise
        return await self._finish_transition(
            claimed,
            required_state="submitting",
            event_type="write_succeeded",
            data={"writeId": write_id, "result": result},
        )

    async def reconcile(
        self,
        write_id: str,
        handler: ReconcileHandler,
    ) -> WriteOperation:
        record = await self.get(write_id)
        claimed = await self._finish_transition(
            record,
            required_state="outcome_unknown",
            event_type="write_reconciling",
            data={"writeId": write_id},
        )
        result = await handler(claimed)
        success = result.get("status") == "succeeded"
        return await self._finish_transition(
            claimed,
            required_state="reconciling",
            event_type="write_succeeded" if success else "write_failed",
            data={"writeId": write_id, "result": result},
        )

    async def get(self, write_id: str) -> WriteOperation:
        return _replay_write(await self.store.load(), write_id)

    async def _find_by_idempotency(
        self,
        key_hash: str,
    ) -> WriteOperation | None:
        events = await self.store.load()
        write_ids = [
            str(event.data.get("writeId"))
            for event in events
            if event.type == "write_prepared"
            and event.data.get("idempotencyKeyHash") == key_hash
        ]
        return _replay_write(events, write_ids[-1]) if write_ids else None

    async def _finish_transition(
        self,
        record: WriteOperation,
        *,
        required_state: str,
        event_type: str,
        data: dict[str, Any],
    ) -> WriteOperation:
        for _ in range(_MAX_CONFLICT_RETRIES):
            events = await self.store.load(
                session_id=record.session_id,
                operation_id=record.operation_id,
            )
            current = _replay_write(events, record.write_id)
            if event_type == "write_succeeded" and current.state == "succeeded":
                return current
            if current.state != required_state:
                raise WriteOperationError(
                    "invalid_write_transition",
                    f"Write 当前状态 {current.state} 不能执行 {event_type}",
                )
            specs = [(event_type, data)]
            replay_operation_with_specs(events, specs)
            try:
                await self.store.append_batch(
                    current.session_id,
                    current.operation_id,
                    specs,
                    expected_last_sequence=operation_last_sequence(events),
                )
            except OperationStoreConflictError:
                continue
            return _replay_write(
                await self.store.load(
                    session_id=current.session_id,
                    operation_id=current.operation_id,
                ),
                current.write_id,
            )
        raise WriteOperationError("write_conflict", "Write 状态转换并发冲突")


def _tool_dispatch_specs(
    events: list[OperationEvent],
    write: WriteOperation,
) -> list[OperationEventSpec]:
    if write.tool_call_id is None:
        return []
    intent_exists = any(
        event.type == "tool_intent_recorded"
        and event.data.get("toolCallId") == write.tool_call_id
        for event in events
    )
    if not intent_exists:
        return []
    dispatch_exists = any(
        event.type == "tool_dispatch_started"
        and event.data.get("toolCallId") == write.tool_call_id
        for event in events
    )
    if dispatch_exists:
        return []
    return [
        (
            "tool_dispatch_started",
            {"toolCallId": write.tool_call_id, "source": "write_claim"},
        )
    ]


def _same_idempotent_action(
    existing: WriteOperation,
    digest: str,
) -> WriteOperation:
    if existing.action_hash != digest:
        raise WriteOperationError(
            "idempotency_conflict",
            "同一个 Idempotency Key 不能用于不同操作",
        )
    return existing


def _replay_write(
    events: list[OperationEvent],
    write_id: str,
) -> WriteOperation:
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
                idempotency_key_hash=str(
                    event.data.get("idempotencyKeyHash", "")
                ),
                requester_id=str(event.data.get("requesterId", "")),
                state="prepared",
                tool_call_id=(
                    str(event.data.get("toolCallId"))
                    if event.data.get("toolCallId") is not None
                    else None
                ),
            )
        elif record is None:
            raise WriteOperationError(
                "write_event_without_prepare",
                "写事件缺少 Prepared",
            )
        elif event.type == "write_waiting_approval":
            if record.state != "prepared":
                raise WriteOperationError(
                    "invalid_write_transition",
                    "只有 Prepared 可以等待 Approval",
                )
            record = replace(
                record,
                state="waiting_approval",
                approval_id=str(event.data.get("approvalId", "")),
            )
        elif event.type == "write_approved":
            if record.state not in {"prepared", "waiting_approval"}:
                raise WriteOperationError(
                    "invalid_write_transition",
                    "不能进入 Approved",
                )
            record = replace(record, state="approved")
        elif event.type == "write_submitting":
            if record.state != "approved":
                raise WriteOperationError(
                    "invalid_write_transition",
                    "只有 Approved 可以提交",
                )
            record = replace(record, state="submitting")
        elif event.type == "write_succeeded":
            if record.state not in {"submitting", "reconciling"}:
                raise WriteOperationError(
                    "invalid_write_transition",
                    "当前状态不能成功",
                )
            record = replace(
                record,
                state="succeeded",
                result=dict(event.data.get("result", {})),
            )
        elif event.type == "write_failed":
            if record.state not in {"submitting", "reconciling"}:
                raise WriteOperationError(
                    "invalid_write_transition",
                    "当前状态不能失败",
                )
            record = replace(
                record,
                state="failed",
                result=dict(event.data.get("result", {})),
            )
        elif event.type == "write_outcome_unknown":
            if record.state != "submitting":
                raise WriteOperationError(
                    "invalid_write_transition",
                    "只有 Submitting 可变成 Unknown",
                )
            record = replace(record, state="outcome_unknown")
        elif event.type == "write_reconciling":
            if record.state != "outcome_unknown":
                raise WriteOperationError(
                    "invalid_write_transition",
                    "只有 Unknown 可以核对",
                )
            record = replace(record, state="reconciling")
    if record is None:
        raise WriteOperationError(
            "write_not_found",
            f"Write 不存在：{write_id}",
        )
    return record


def _hash_secret(value: str) -> str:
    if not value:
        raise ValueError("Idempotency Key 不能为空")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
