"""Approval 批准后恢复原 Operation 的持久、幂等协调器。"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid4

from ..approval import ApprovalRecord, ApprovalService
from ..approval.state_machine import build_approval_request_event
from ..security import VerifiedIdentity
from ..session.operation_events import OperationEvent
from ..session.operation_state import replay_operation_with_specs
from ..session.operation_store import (
    OperationEventStore,
    OperationStoreConflictError,
    operation_last_sequence,
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


class ApprovalResumeCoordinator:
    """以 Registered 为恢复锚点，按 Approval 当前状态幂等推进。"""

    def __init__(
        self,
        store: OperationEventStore,
        approvals: ApprovalService,
    ) -> None:
        self.store = store
        self.approvals = approvals
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
        ttl_seconds: float = 300,
    ) -> PendingApprovalResume:
        approval_id, approval_data = build_approval_request_event(
            requester=requester,
            action=action,
            action_summary=action_summary,
            required_role=required_role,
            ttl_seconds=ttl_seconds,
        )
        specs = [
            ("approval_requested", approval_data),
            (
                "approval_resume_registered",
                {
                    "approvalId": approval_id,
                    "action": action,
                    "resumePayload": resume_payload,
                },
            ),
        ]
        # Request 与 Durable Resume Intent 必须原子存在，彻底关闭
        # approval_requested 已写但 registered 尚未写的崩溃窗口。
        for _ in range(20):
            events = await self.store.load(
                session_id=session_id,
                operation_id=operation_id,
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
            approval = await self.approvals.get(approval_id)
            return PendingApprovalResume(approval, action, resume_payload)
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
        resume: Callable[[dict[str, Any]], Awaitable[Any]],
    ) -> Any:
        """Waiting/Approved/Consumed/Started 均可安全重复调用。"""

        completed = await self._completed_result(approval_id)
        if completed[0]:
            return completed[1]
        task = await self._get_or_create_task(
            approval_id,
            approver=approver,
            consumer=consumer,
            resume=resume,
            allow_grant=True,
        )
        return await self._await_active(approval_id, task)

    async def pending_recovery_ids(self) -> list[str]:
        """列出已批准/已消费但尚未完成的 Registered Resume。"""

        events = await self.store.load()
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
        resume: Callable[[dict[str, Any]], Awaitable[Any]],
        *,
        consumer_resolver: IdentityResolver | None = None,
    ) -> list[str]:
        """从 Registered 扫描，覆盖 Granted/Consumed 到 Started 的崩溃窗口。"""

        events = await self.store.load()
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
            if state in {"rejected", "expired"}:
                await self._append_cancelled_if_missing(pending, state)
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
        resume: Callable[[dict[str, Any]], Awaitable[Any]],
        allow_grant: bool,
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
        resume: Callable[[dict[str, Any]], Awaitable[Any]],
        allow_grant: bool,
    ) -> Any:
        owner_token = str(uuid4())
        acquired = await self.store.try_acquire_claim(
            "approval_resume",
            approval_id,
            owner_token,
        )
        if not acquired:
            raise ApprovalResumeError(
                "approval_resume_claimed",
                "Approval Resume 已被另一个 Worker Claim",
            )
        try:
            return await self._advance_and_resume_claimed(
                approval_id,
                approver=approver,
                consumer=consumer,
                resume=resume,
                allow_grant=allow_grant,
            )
        finally:
            await self.store.release_claim(
                "approval_resume",
                approval_id,
                owner_token,
            )

    async def _advance_and_resume_claimed(
        self,
        approval_id: str,
        *,
        approver: VerifiedIdentity | None,
        consumer: VerifiedIdentity | None,
        resume: Callable[[dict[str, Any]], Awaitable[Any]],
        allow_grant: bool,
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
            approval = await self.approvals.grant(approval_id, approver)
        if approval.state == "approved":
            if consumer is None:
                raise ApprovalResumeError(
                    "approval_consumer_missing",
                    "已批准 Approval 缺少可信 Consumer，暂不能恢复",
                )
            approval = await self.approvals.consume(
                approval_id,
                action=pending.action,
                consumer=consumer,
            )
        if approval.state in {"rejected", "expired"}:
            await self._append_cancelled_if_missing(pending, approval.state)
            raise ApprovalResumeError(
                "approval_not_resumable",
                f"Approval 状态 {approval.state} 不能恢复",
            )
        if approval.state != "consumed":
            raise ApprovalResumeError(
                "approval_not_consumed",
                f"Approval 状态 {approval.state} 不能开始 Resume",
            )

        events = await self._events_for(pending)
        started = any(
            event.type == "approval_resume_started"
            and event.data.get("approvalId") == approval_id
            for event in events
        )
        if not started:
            consumer_id = consumer.principal_id if consumer is not None else _consumer_id(events, approval_id)
            await self.store.append(
                "approval_resume_started",
                approval.session_id,
                approval.operation_id,
                {"approvalId": approval_id, "consumerId": consumer_id},
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
        snapshot, stored = _snapshot_result(result)
        await self.store.append(
            "approval_resume_completed",
            approval.session_id,
            approval.operation_id,
            {
                "approvalId": approval_id,
                "result": snapshot,
                "resultStored": stored,
            },
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
        return PendingApprovalResume(
            approval=approval,
            action=dict(event.data.get("action", {})),
            resume_payload=dict(event.data.get("resumePayload", {})),
        )

    async def _events_for(self, pending: PendingApprovalResume) -> list[OperationEvent]:
        return await self.store.load(
            session_id=pending.approval.session_id,
            operation_id=pending.approval.operation_id,
        )

    async def _completed_result(self, approval_id: str) -> tuple[bool, Any]:
        events = await self.store.load()
        event = next(
            (
                item
                for item in reversed(events)
                if item.type == "approval_resume_completed"
                and item.data.get("approvalId") == approval_id
            ),
            None,
        )
        return (True, event.data.get("result")) if event is not None else (False, None)

    async def _has_terminal_failure(self, approval_id: str) -> bool:
        events = await self.store.load()
        return any(
            event.data.get("approvalId") == approval_id
            and event.type in {"approval_resume_failed", "approval_resume_cancelled"}
            for event in events
        )

    async def _append_cancelled_if_missing(
        self,
        pending: PendingApprovalResume,
        reason: str,
    ) -> None:
        events = await self._events_for(pending)
        if any(
            event.type == "approval_resume_cancelled"
            and event.data.get("approvalId") == pending.approval.approval_id
            for event in events
        ):
            return
        await self.store.append(
            "approval_resume_cancelled",
            pending.approval.session_id,
            pending.approval.operation_id,
            {"approvalId": pending.approval.approval_id, "reason": reason},
        )


def _consumer_id(events: list[OperationEvent], approval_id: str) -> str | None:
    event = next(
        (
            item
            for item in reversed(events)
            if item.type == "approval_consumed"
            and item.data.get("approvalId") == approval_id
        ),
        None,
    )
    return str(event.data.get("consumerId")) if event is not None else None


def _snapshot_result(result: Any) -> tuple[Any, bool]:
    try:
        return json.loads(json.dumps(result, ensure_ascii=False)), True
    except (TypeError, ValueError):
        # 副作用已经完成时不能因为返回值不可序列化而重放 Resume。
        return None, False
