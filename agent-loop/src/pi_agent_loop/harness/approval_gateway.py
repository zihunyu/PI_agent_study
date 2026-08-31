"""Approval 批准后恢复原 Operation 的持久、幂等协调器。"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid4

from ..approval import ApprovalRecord, ApprovalService
from ..approval.state_machine import build_approval_request_event
from ..durable_action import (
    DurableActionEnvelope,
    DurableActionEnvelopeError,
    strict_json_equal,
)
from ..security import VerifiedIdentity
from ..session.operation_events import OperationEvent
from ..session.operation_state import replay_operation, replay_operation_with_specs
from ..session.operation_store import (
    ClaimLease,
    OperationEventStore,
    OperationStoreConflictError,
    OperationStoreFencedClaimLostError,
    fenced_claim_resource_id,
    operation_last_sequence,
)
from ..writes import (
    hash_idempotency_key,
    hash_scoped_idempotency_key,
    is_outcome_unknown_error,
)


class ApprovalResumeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PendingApprovalResume:
    approval: ApprovalRecord
    action: dict[str, Any]
    resume_payload: dict[str, Any]


IdentityResolver = Callable[
    [PendingApprovalResume],
    VerifiedIdentity | Awaitable[VerifiedIdentity],
]
ResumeCallback = Callable[..., Any]


class ApprovalResumeCoordinator:
    """以 Registered 为恢复锚点，按 Approval 当前状态幂等推进。"""

    def __init__(
        self,
        store: OperationEventStore,
        approvals: ApprovalService,
        *,
        session_id: str | None = None,
        claim_lease_seconds: float = 300,
        claim_renew_interval_seconds: float | None = None,
    ) -> None:
        if claim_lease_seconds <= 0:
            raise ValueError("claim_lease_seconds 必须大于 0")
        renew_interval = (
            claim_renew_interval_seconds
            if claim_renew_interval_seconds is not None
            else min(60.0, claim_lease_seconds / 3)
        )
        if renew_interval <= 0 or renew_interval >= claim_lease_seconds:
            raise ValueError(
                "Claim 续租间隔必须大于 0 且小于 Lease 时长"
            )
        self.store = store
        self.approvals = approvals
        self.session_id = session_id
        self.claim_lease_seconds = claim_lease_seconds
        self.claim_renew_interval_seconds = renew_interval
        self._lock = asyncio.Lock()
        self._active: dict[str, asyncio.Task[Any]] = {}

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
        idempotency_key: str,
        ttl_seconds: float = 300,
        fenced_claim: ClaimLease | None = None,
        fenced_claim_lease_seconds: float = 300,
    ) -> PendingApprovalResume:
        self._require_atomic_store()
        requester = self.approvals.assert_trusted_identity(
            requester,
            purpose="Approval resume requester",
        )
        try:
            envelope = DurableActionEnvelope.from_dict(action)
        except DurableActionEnvelopeError as error:
            raise ApprovalResumeError(
                "approval_action_envelope_invalid",
                "Approval Action Envelope 无效",
            ) from error
        if envelope.operation_id != operation_id:
            raise ApprovalResumeError(
                "approval_operation_mismatch",
                "Approval Envelope Operation ID 不匹配",
            )
        payload_keys = set(resume_payload)
        if payload_keys - {"envelope"}:
            raise ApprovalResumeError(
                "approval_resume_payload_not_sealed",
                "Resume Payload 只能携带 Durable Action Envelope",
            )
        if "envelope" in resume_payload:
            try:
                supplied = DurableActionEnvelope.from_dict(
                    resume_payload["envelope"]
                )
            except DurableActionEnvelopeError as error:
                raise ApprovalResumeError(
                    "approval_resume_envelope_invalid",
                    "Approval Resume Envelope 无效",
                ) from error
            if supplied != envelope:
                raise ApprovalResumeError(
                    "approval_resume_envelope_mismatch",
                    "Approval Action 与 Resume Payload 不一致",
                )
        trusted_payload = {"envelope": envelope.to_dict()}
        if not idempotency_key:
            raise ApprovalResumeError(
                "approval_idempotency_key_missing",
                "Approval Write 必须提供 Idempotency Key",
            )
        idempotency_key_hash = hash_scoped_idempotency_key(
            idempotency_key,
            session_id=session_id,
            principal_id=requester.principal_id,
            tool_name=envelope.tool_name,
        )
        legacy_idempotency_key_hash = hash_idempotency_key(idempotency_key)
        approval_id, approval_data = build_approval_request_event(
            requester=requester,
            action=envelope.to_dict(),
            action_summary=action_summary,
            required_role=required_role,
            ttl_seconds=ttl_seconds,
        )
        # Request 自己创建 Write/Intent；禁止调用者先写一批“半计划”事实。
        # Assistant Tool Call、Approval、Write 与 Intent 必须在本批事务内
        # 同时落盘，不能复用前一事务留下的 Assistant Call。
        for _ in range(20):
            events = await self.store.load(
                session_id=session_id,
                operation_id=operation_id,
            )
            if not events:
                raise ApprovalResumeError(
                    "approval_operation_not_found",
                    "Approval 必须属于已持久化的 Operation",
                )
            _validate_requestable_action(events, envelope)
            all_events = await self.store.load()
            if any(
                event.type == "write_prepared"
                and event.data.get("idempotencyKeyHash")
                in {idempotency_key_hash, legacy_idempotency_key_hash}
                and event.session_id == session_id
                and event.data.get("requesterId") == requester.principal_id
                and event.data.get("toolName") == envelope.tool_name
                for event in all_events
            ):
                raise ApprovalResumeError(
                    "approval_idempotency_conflict",
                    "Idempotency Key 已绑定其他 Durable Write",
                )
            specs: list[tuple[str, dict[str, Any]]] = [
                (
                    "message_appended",
                    {
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "toolCall",
                                    "id": envelope.tool_call_id,
                                    "name": envelope.tool_name,
                                    "arguments": envelope.arguments,
                                }
                            ],
                        },
                        "source": "approval_request_plan",
                    },
                ),
                (
                    "write_prepared",
                    {
                        "writeId": envelope.write_id,
                        "toolName": envelope.tool_name,
                        "arguments": envelope.arguments,
                        "actionHash": envelope.action_hash,
                        "idempotencyKeyHash": idempotency_key_hash,
                        "idempotencyHashVersion": 2,
                        "requesterId": requester.principal_id,
                        "requesterVerificationId": requester.verification_id,
                        "toolCallId": envelope.tool_call_id,
                        "entityId": envelope.entity_id,
                        "expectedEntityVersion": envelope.expected_entity_version,
                        "businessPreconditions": copy.deepcopy(
                            envelope.business_preconditions
                        ),
                    },
                ),
                ("approval_requested", approval_data),
                (
                    "approval_resume_registered",
                    {
                        "approvalId": approval_id,
                        "action": envelope.to_dict(),
                        "resumePayload": trusted_payload,
                    },
                ),
                (
                    "write_waiting_approval",
                    {
                        "writeId": envelope.write_id,
                        "approvalId": approval_id,
                    },
                ),
                (
                    "tool_intent_recorded",
                    {
                        "toolCallId": envelope.tool_call_id,
                        "toolName": envelope.tool_name,
                        "arguments": envelope.arguments,
                        "replayPolicy": "never",
                        "source": "approval_request_plan",
                    },
                ),
            ]
            replay_operation_with_specs(events, specs)
            try:
                if fenced_claim is None:
                    await self.store.append_batch(
                        session_id,
                        operation_id,
                        specs,
                        expected_last_sequence=operation_last_sequence(events),
                    )
                else:
                    await self.store.append_batch_if_fenced_claim(
                        session_id,
                        operation_id,
                        specs,
                        fenced_claim,
                        renew_lease_seconds=fenced_claim_lease_seconds,
                        expected_last_sequence=operation_last_sequence(events),
                    )
            except OperationStoreFencedClaimLostError as error:
                raise ApprovalResumeError(
                    "approval_request_claim_lost",
                    "Session Writer Lease 已丢失，禁止注册 Approval Resume",
                ) from error
            except OperationStoreConflictError:
                continue
            approval = await self.approvals.get(approval_id)
            return PendingApprovalResume(
                approval,
                envelope.to_dict(),
                trusted_payload,
            )
        raise ApprovalResumeError(
            "approval_request_conflict",
            "Approval Request/Resume Registered 并发冲突",
        )

    async def approve_and_resume(
        self,
        approval_id: str,
        *,
        approver: VerifiedIdentity,
        consumer: VerifiedIdentity,
        resume: ResumeCallback,
        atomic_write_start: bool = True,
    ) -> Any:
        """Waiting/Approved/Consumed/Started 均可安全重复调用。"""

        if not atomic_write_start:
            raise ApprovalResumeError(
                "approval_resume_requires_atomic_write",
                "Approval Resume 必须由 Write Claim 原子启动",
            )
        self._require_atomic_store()

        completed = await self._completed_result(approval_id)
        if completed[0]:
            return completed[1]
        task = await self._get_or_create_task(
            approval_id,
            approver=approver,
            consumer=consumer,
            resume=resume,
            allow_grant=True,
            atomic_write_start=atomic_write_start,
        )
        return await self._await_active(approval_id, task)

    async def pending_recovery_ids(self) -> list[str]:
        """列出已批准/已消费但尚未完成的 Registered Resume。"""

        events = await self._load_scope()
        registered_ids = list(dict.fromkeys(
            str(event.data.get("approvalId"))
            for event in events
            if event.type == "approval_resume_registered"
            and event.data.get("approvalId")
        ))
        terminal = {
            str(event.data.get("approvalId"))
            for event in events
            if event.type in {
                "approval_resume_completed",
                "approval_resume_failed",
                "approval_resume_cancelled",
            }
        }
        pending: list[str] = []
        for approval_id in registered_ids:
            if approval_id in terminal:
                continue
            state = (await self.get_pending(approval_id)).approval.state
            if state in {"approved", "consumed"}:
                pending.append(approval_id)
        return pending

    async def recover_incomplete(
        self,
        resume: ResumeCallback,
        *,
        consumer_resolver: IdentityResolver | None = None,
    ) -> list[str]:
        """从 Registered 扫描，覆盖 Granted/Consumed 到 Started 的崩溃窗口。"""

        self._require_atomic_store()
        events = await self._load_scope()
        registered_ids = list(dict.fromkeys(
            str(event.data.get("approvalId"))
            for event in events
            if event.type == "approval_resume_registered"
            and event.data.get("approvalId")
        ))
        terminal = {
            str(event.data.get("approvalId"))
            for event in events
            if event.type in {
                "approval_resume_completed",
                "approval_resume_failed",
                "approval_resume_cancelled",
            }
        }
        recovered: list[str] = []
        for approval_id in registered_ids:
            if approval_id in terminal:
                continue
            pending = await self.get_pending(approval_id)
            state = pending.approval.state
            if state == "waiting":
                continue
            consumer: VerifiedIdentity | None = None
            if state == "approved":
                if consumer_resolver is None:
                    # 已发现但缺少可信 Consumer，保持可恢复，不伪造身份。
                    continue
                value = consumer_resolver(pending)
                consumer = (
                    await cast(Awaitable[VerifiedIdentity], value)
                    if inspect.isawaitable(value)
                    else value
                )
            task = await self._get_or_create_task(
                approval_id,
                approver=None,
                consumer=consumer,
                resume=resume,
                allow_grant=False,
                atomic_write_start=True,
            )
            try:
                await self._await_active(approval_id, task)
            except Exception:
                continue
            recovered.append(approval_id)
        return recovered

    async def _get_or_create_task(
        self,
        approval_id: str,
        *,
        approver: VerifiedIdentity | None,
        consumer: VerifiedIdentity | None,
        resume: ResumeCallback,
        allow_grant: bool,
        atomic_write_start: bool,
    ) -> asyncio.Task[Any]:
        async with self._lock:
            existing = self._active.get(approval_id)
            if existing is not None:
                return existing
            task = asyncio.create_task(
                self._advance_and_resume(
                    approval_id,
                    approver=approver,
                    consumer=consumer,
                    resume=resume,
                    allow_grant=allow_grant,
                    atomic_write_start=atomic_write_start,
                ),
                name=f"approval-resume:{approval_id}",
            )
            self._active[approval_id] = task
            return task

    async def _await_active(
        self,
        approval_id: str,
        task: asyncio.Task[Any],
    ) -> Any:
        try:
            return await task
        finally:
            async with self._lock:
                if self._active.get(approval_id) is task:
                    self._active.pop(approval_id, None)

    async def _advance_and_resume(
        self,
        approval_id: str,
        *,
        approver: VerifiedIdentity | None,
        consumer: VerifiedIdentity | None,
        resume: ResumeCallback,
        allow_grant: bool,
        atomic_write_start: bool,
    ) -> Any:
        owner_token = str(uuid4())
        scoped_pending = await self.get_pending(approval_id)
        resource_id = fenced_claim_resource_id(
            "approval_resume",
            session_id=scoped_pending.approval.session_id,
            operation_id=scoped_pending.approval.operation_id,
            entity_id=approval_id,
        )
        lease = await self.store.acquire_fenced_claim(
            "approval_resume",
            resource_id,
            owner_token,
            lease_seconds=self.claim_lease_seconds,
        )
        if lease is None:
            raise ApprovalResumeError(
                "approval_resume_claimed",
                "Approval Resume 已被另一个 Worker Claim",
            )
        claim_lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._renew_claim(lease, claim_lost),
            name=f"approval-resume-heartbeat:{approval_id}",
        )
        execution = asyncio.create_task(
            self._advance_and_resume_claimed(
                approval_id,
                approver=approver,
                consumer=consumer,
                resume=resume,
                allow_grant=allow_grant,
                atomic_write_start=atomic_write_start,
                lease=lease,
                claim_lost=claim_lost,
            ),
            name=f"approval-resume-execution:{approval_id}",
        )
        lease_wait = asyncio.create_task(
            claim_lost.wait(),
            name=f"approval-resume-lease-loss:{approval_id}",
        )
        try:
            await asyncio.wait(
                {execution, lease_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if claim_lost.is_set():
                if not execution.done():
                    execution.cancel()
                await asyncio.gather(execution, return_exceptions=True)
                raise ApprovalResumeError(
                    "approval_resume_claim_lost",
                    "Approval Resume Lease 已丢失，已取消旧 Worker",
                )
            return await execution
        finally:
            for task in (execution, lease_wait):
                if not task.done():
                    task.cancel()
            await asyncio.gather(execution, lease_wait, return_exceptions=True)
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            await self.store.release_fenced_claim(lease)

    async def _advance_and_resume_claimed(
        self,
        approval_id: str,
        *,
        approver: VerifiedIdentity | None,
        consumer: VerifiedIdentity | None,
        resume: ResumeCallback,
        allow_grant: bool,
        atomic_write_start: bool,
        lease: ClaimLease,
        claim_lost: asyncio.Event,
    ) -> Any:
        completed = await self._completed_result(approval_id)
        if completed[0]:
            return completed[1]
        if await self._has_terminal_failure(approval_id):
            raise ApprovalResumeError(
                "approval_resume_terminal",
                "Approval Resume 已失败或取消，不能自动重复执行",
            )

        pending = await self.get_pending(approval_id)
        approval = pending.approval
        if approval.state == "waiting":
            if not allow_grant or approver is None:
                raise ApprovalResumeError(
                    "approval_still_waiting",
                    "Approval 仍在等待人工批准",
                )
            approval = await self.approvals.grant(
                approval_id,
                approver,
                fenced_claim=lease,
                renew_lease_seconds=self.claim_lease_seconds,
            )
        if approval.state in {"rejected", "expired"}:
            await self._append_cancelled_if_missing(
                pending,
                approval.state,
                lease=lease,
                claim_lost=claim_lost,
            )
            raise ApprovalResumeError(
                "approval_not_resumable",
                f"Approval 状态 {approval.state} 不能恢复",
            )
        if approval.state not in {"approved", "consumed"}:
            raise ApprovalResumeError(
                "approval_not_consumed",
                f"Approval 状态 {approval.state} 不能开始 Resume",
            )

        await self._assert_claim(lease, claim_lost)
        preconsumed = approval.state == "consumed"
        if await self._has_resume_started(pending):
            await self._append_resume_transition(
                pending,
                "approval_resume_reentered",
                {
                    "approvalId": approval_id,
                    "fencingToken": lease.fencing_token,
                },
                lease=lease,
                claim_lost=claim_lost,
            )
        if (
            self.store.supports_cross_process_claims
            and not _callback_accepts_keyword(resume, "fenced_claim")
        ):
            raise ApprovalResumeError(
                "approval_resume_fenced_callback_required",
                "跨进程 Approval Resume Callback 必须接收完整 Fenced Claim",
            )
        try:
            result = await invoke_fenced_callback(
                resume,
                pending.resume_payload,
                fencing_token=lease.fencing_token,
                fenced_claim=lease,
            )
            # Callback 返回是第一个迟到提交边界；先核对精确 generation，
            # 再读取/验证其持久化结果，避免旧 Worker 被新租约接管后收敛终态。
            await self._assert_claim(lease, claim_lost)
            await self._validate_atomic_write_claim(
                pending,
                preconsumed=preconsumed,
            )
            await self._validate_resume_completion(pending)
        except Exception as error:
            if (
                await self._has_resume_started(pending)
                and not is_outcome_unknown_error(error)
            ):
                await self._assert_claim(
                    lease,
                    claim_lost,
                )
                await self._append_resume_transition(
                    pending,
                    "approval_resume_failed",
                    {
                        "approvalId": approval_id,
                        "errorCode": "approval_resume_execution_failed",
                        "error": "Approval Resume 执行失败",
                        "fencingToken": lease.fencing_token,
                    },
                    lease=lease,
                    claim_lost=claim_lost,
                )
            raise
        await self._assert_claim(lease, claim_lost)
        snapshot, stored = _snapshot_result(result)
        await self._append_resume_transition(
            pending,
            "approval_resume_completed",
            {
                "approvalId": approval_id,
                "result": snapshot,
                "resultStored": stored,
                "fencingToken": lease.fencing_token,
            },
            lease=lease,
            claim_lost=claim_lost,
        )
        return result

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
        action = dict(event.data.get("action", {}))
        resume_payload = dict(event.data.get("resumePayload", {}))
        if set(resume_payload) != {"envelope"}:
            raise ApprovalResumeError(
                "approval_resume_payload_not_sealed",
                "Resume Payload 只能携带 Durable Action Envelope",
            )
        try:
            envelope = DurableActionEnvelope.from_dict(action)
            payload_envelope = DurableActionEnvelope.from_dict(
                resume_payload.get("envelope")
            )
        except DurableActionEnvelopeError as error:
            raise ApprovalResumeError(
                "approval_resume_envelope_invalid",
                "Approval Resume Envelope 无效",
            ) from error
        if envelope != payload_envelope:
            raise ApprovalResumeError(
                "approval_resume_envelope_mismatch",
                "Approval Action 与 Resume Payload 不一致",
            )
        return PendingApprovalResume(
            approval=approval,
            action=envelope.to_dict(),
            resume_payload={**resume_payload, "envelope": envelope.to_dict()},
        )

    async def _events_for(self, pending: PendingApprovalResume) -> list[OperationEvent]:
        return await self.store.load(
            session_id=pending.approval.session_id,
            operation_id=pending.approval.operation_id,
        )

    async def _completed_result(self, approval_id: str) -> tuple[bool, Any]:
        events = await self._load_scope()
        event = next(
            (
                item
                for item in reversed(events)
                if item.type == "approval_resume_completed"
                and item.data.get("approvalId") == approval_id
            ),
            None,
        )
        if event is None:
            return False, None
        replay_operation([
            item
            for item in events
            if item.session_id == event.session_id
            and item.operation_id == event.operation_id
        ])
        return True, event.data.get("result")

    async def _has_terminal_failure(self, approval_id: str) -> bool:
        events = await self._load_scope()
        return any(
            event.data.get("approvalId") == approval_id
            and event.type in {"approval_resume_failed", "approval_resume_cancelled"}
            for event in events
        )

    async def _append_cancelled_if_missing(
        self,
        pending: PendingApprovalResume,
        reason: str,
        *,
        lease: ClaimLease,
        claim_lost: asyncio.Event,
    ) -> None:
        await self._append_resume_transition(
            pending,
            "approval_resume_cancelled",
            {"approvalId": pending.approval.approval_id, "reason": reason},
            lease=lease,
            claim_lost=claim_lost,
        )

    async def _load_scope(self) -> list[OperationEvent]:
        return await self.store.load(session_id=self.session_id)

    async def _append_resume_transition(
        self,
        pending: PendingApprovalResume,
        event_type: str,
        data: dict[str, Any],
        *,
        lease: ClaimLease,
        claim_lost: asyncio.Event,
    ) -> None:
        approval_id = pending.approval.approval_id
        for _ in range(20):
            events = await self._events_for(pending)
            if any(
                event.type == event_type
                and event.data.get("approvalId") == approval_id
                and (
                    event_type != "approval_resume_reentered"
                    or event.data.get("fencingToken")
                    == data.get("fencingToken")
                )
                for event in events
            ):
                return
            terminal = next(
                (
                    event.type
                    for event in events
                    if event.data.get("approvalId") == approval_id
                    and event.type in {
                        "approval_resume_completed",
                        "approval_resume_failed",
                        "approval_resume_cancelled",
                    }
                ),
                None,
            )
            if terminal is not None:
                raise ApprovalResumeError(
                    "approval_resume_terminal",
                    f"Approval Resume 已进入终态：{terminal}",
                )
            specs = [(event_type, data)]
            replay_operation_with_specs(events, specs)
            try:
                await self.store.append_batch_if_fenced_claim(
                    pending.approval.session_id,
                    pending.approval.operation_id,
                    specs,
                    lease,
                    renew_lease_seconds=self.claim_lease_seconds,
                    expected_last_sequence=operation_last_sequence(events),
                    expected_claim_entity_id=approval_id,
                )
            except OperationStoreFencedClaimLostError as error:
                claim_lost.set()
                raise ApprovalResumeError(
                    "approval_resume_claim_lost",
                    "Approval Resume Lease 已丢失，禁止提交状态",
                ) from error
            except OperationStoreConflictError:
                continue
            return
        raise ApprovalResumeError(
            "approval_resume_conflict",
            f"Approval Resume 状态转换并发冲突：{event_type}",
        )

    async def _has_resume_started(
        self,
        pending: PendingApprovalResume,
    ) -> bool:
        return any(
            event.type == "approval_resume_started"
            and event.data.get("approvalId") == pending.approval.approval_id
            for event in await self._events_for(pending)
        )

    async def _validate_atomic_write_claim(
        self,
        pending: PendingApprovalResume,
        *,
        preconsumed: bool,
    ) -> None:
        events = await self._events_for(pending)
        envelope = DurableActionEnvelope.from_dict(pending.action)
        approval_id = pending.approval.approval_id

        def matches(index: int, event_type: str, **data: str) -> bool:
            if index < 0 or index >= len(events):
                return False
            event = events[index]
            return event.type == event_type and all(
                event.data.get(key) == value for key, value in data.items()
            )

        if not preconsumed:
            for index in range(len(events) - 4):
                if (
                    matches(
                        index,
                        "approval_consumed",
                        approvalId=approval_id,
                    )
                    and matches(
                        index + 1,
                        "approval_resume_started",
                        approvalId=approval_id,
                    )
                    and matches(
                        index + 2,
                        "write_approved",
                        writeId=envelope.write_id,
                        approvalId=approval_id,
                    )
                    and matches(
                        index + 3,
                        "tool_dispatch_started",
                        toolCallId=envelope.tool_call_id,
                    )
                    and matches(
                        index + 4,
                        "write_submitting",
                        writeId=envelope.write_id,
                    )
                ):
                    return
        else:
            started_indexes = [
                index
                for index, event in enumerate(events)
                if event.type == "approval_resume_started"
                and event.data.get("approvalId") == approval_id
            ]
            if started_indexes:
                for index in range(len(events) - 2):
                    if (
                        index > started_indexes[0]
                        and matches(
                            index,
                            "write_approved",
                            writeId=envelope.write_id,
                            approvalId=approval_id,
                        )
                        and matches(
                            index + 1,
                            "tool_dispatch_started",
                            toolCallId=envelope.tool_call_id,
                        )
                        and matches(
                            index + 2,
                            "write_submitting",
                            writeId=envelope.write_id,
                        )
                    ):
                        return
                # Write Claim 与 Started 同一重入批次。
                index = started_indexes[0]
                if (
                    matches(
                        index + 1,
                        "write_approved",
                        writeId=envelope.write_id,
                        approvalId=approval_id,
                    )
                    and matches(
                        index + 2,
                        "tool_dispatch_started",
                        toolCallId=envelope.tool_call_id,
                    )
                    and matches(
                        index + 3,
                        "write_submitting",
                        writeId=envelope.write_id,
                    )
                ):
                    return
                # 已成功 Write 的重入只补 Resume Completed，不再创建 Claim。
                succeeded = any(
                    event.type == "write_succeeded"
                    and event.data.get("writeId") == envelope.write_id
                    for event in events
                )
                if succeeded:
                    return
        raise ApprovalResumeError(
            "approval_resume_not_atomically_started",
            "Resume Callback 未原子提交 Approval Consume、Resume Started 与 Write Claim",
        )

    async def _validate_resume_completion(
        self,
        pending: PendingApprovalResume,
    ) -> None:
        envelope = DurableActionEnvelope.from_dict(pending.action)
        operation = replay_operation(await self._events_for(pending))
        write = operation.writes.get(envelope.write_id)
        if write is None or write.state != "succeeded":
            raise ApprovalResumeError(
                "approval_resume_write_incomplete",
                "Resume Callback 返回前 Write 必须已成功",
            )
        invocation = operation.tools.get(envelope.tool_call_id)
        if invocation is None or invocation.phase != "completed":
            raise ApprovalResumeError(
                "approval_resume_tool_incomplete",
                "Resume Callback 返回前 Tool Invocation 必须已完成",
            )
        results = [
            message
            for message in operation.messages
            if message.get("role") == "toolResult"
            and message.get("toolCallId") == envelope.tool_call_id
        ]
        if len(results) != 1:
            raise ApprovalResumeError(
                "approval_resume_transcript_open",
                "Resume Callback 返回前必须持久化且只能持久化一个 Tool Result",
            )

    async def _renew_claim(
        self,
        lease: ClaimLease,
        claim_lost: asyncio.Event,
    ) -> None:
        while True:
            await asyncio.sleep(self.claim_renew_interval_seconds)
            try:
                renewed = await self.store.renew_fenced_claim(
                    lease,
                    lease_seconds=self.claim_lease_seconds,
                )
            except Exception:
                claim_lost.set()
                return
            if not renewed:
                claim_lost.set()
                return

    async def _assert_claim(
        self,
        lease: ClaimLease,
        claim_lost: asyncio.Event,
    ) -> None:
        if claim_lost.is_set() or not await self.store.renew_fenced_claim(
            lease,
            lease_seconds=self.claim_lease_seconds,
        ):
            claim_lost.set()
            raise ApprovalResumeError(
                "approval_resume_claim_lost",
                "Approval Resume Lease 已丢失，禁止提交后续状态",
            )

    def _require_atomic_store(self) -> None:
        if not self.store.supports_atomic_transactions:
            raise ApprovalResumeError(
                "approval_atomic_store_required",
                "Approval/Write/Resume 必须使用支持原子事务的 Operation Store",
            )


async def invoke_fenced_callback(
    callback: Callable[..., Any],
    *args: Any,
    fencing_token: int,
    fenced_claim: ClaimLease | None = None,
) -> Any:
    """Invoke the additive fenced callback API without breaking old handlers."""

    parameters: Mapping[str, inspect.Parameter]
    try:
        parameters = inspect.signature(callback).parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    kwargs: dict[str, Any] = {}
    for name, value in (
        ("fencing_token", fencing_token),
        ("fenced_claim", fenced_claim),
    ):
        explicit = parameters.get(name)
        if accepts_kwargs or (
            explicit is not None
            and explicit.kind
            in {
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            }
        ):
            kwargs[name] = value
    value = callback(*args, **kwargs)
    return await cast(Awaitable[Any], value) if inspect.isawaitable(value) else value


def _callback_accepts_keyword(callback: Callable[..., Any], name: str) -> bool:
    try:
        parameters = inspect.signature(callback).parameters
    except (TypeError, ValueError):
        return False
    explicit = parameters.get(name)
    return bool(
        (
            explicit is not None
            and explicit.kind
            in {
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            }
        )
        or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
    )


def _validate_requestable_action(
    events: list[OperationEvent],
    envelope: DurableActionEnvelope,
) -> None:
    for event in events:
        if event.type in {"model_request_completed", "message_appended"}:
            message = event.data.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            for block in message.get("content", []):
                if (
                    isinstance(block, dict)
                    and block.get("type") == "toolCall"
                    and block.get("id") == envelope.tool_call_id
                ):
                    raise ApprovalResumeError(
                        "approval_planned_call_conflict",
                        "Coordinator.request 禁止复用前一事务的 Tool Call",
                    )
        if (
            event.type == "write_prepared"
            and (
                event.data.get("writeId") == envelope.write_id
                or event.data.get("toolCallId") == envelope.tool_call_id
            )
        ) or (
            event.type == "tool_intent_recorded"
            and event.data.get("toolCallId") == envelope.tool_call_id
        ):
            raise ApprovalResumeError(
                "approval_durable_action_already_planned",
                "Coordinator.request 必须原子创建 Write 与 Tool Intent，禁止复用半计划事实",
            )
    if any(
        event.type == "approval_resume_registered"
        and strict_json_equal(event.data.get("action"), envelope.to_dict())
        for event in events
    ):
        raise ApprovalResumeError(
            "approval_durable_action_already_registered",
            "Durable Action 已注册 Approval Resume",
        )


def _snapshot_result(result: Any) -> tuple[Any, bool]:
    try:
        return json.loads(json.dumps(result, ensure_ascii=False)), True
    except (TypeError, ValueError):
        # 副作用已经完成时不能因为返回值不可序列化而重放 Resume。
        return None, False
