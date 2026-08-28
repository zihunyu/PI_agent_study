"""统一 Session Journal 到现有 Runtime/Operation/Retry Store API 的适配器。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..retry.events import RetryChain
from ..runtime.events import RuntimeEvent
from .journal import (
    JournalConflictError,
    JournalDeadlineExceeded,
    JournalPrincipal,
    SQLiteSessionEventJournal,
    SessionEventSpec,
)
from .operation_events import OperationEvent
from .operation_store import (
    OperationEventSpec,
    OperationStoreConflictError,
    OperationStoreDeadlineExceeded,
)

_RETRY_TERMINAL_EVENTS = frozenset(
    {
        "model_retry_finished",
        "tool_retry_finished",
        "task_retry_finished",
        "retry_recovery_finished",
    }
)


class SessionJournalOperationEventStore:
    """让现有 Approval/Write/Recovery 直接使用统一加密 Journal。"""

    supports_atomic_transactions = True

    def __init__(
        self,
        journal: SQLiteSessionEventJournal,
        principal: JournalPrincipal,
    ) -> None:
        self.journal = journal
        self.principal = principal

    async def append(
        self,
        event_type: str,
        session_id: str,
        operation_id: str,
        data: dict[str, Any] | None = None,
    ) -> OperationEvent:
        return (
            await self.append_batch(
                session_id,
                operation_id,
                [(event_type, data or {})],
            )
        )[0]

    async def append_batch(
        self,
        session_id: str,
        operation_id: str,
        events: list[OperationEventSpec],
        *,
        expected_last_sequence: int | None = None,
        deadline_ms: int | None = None,
    ) -> list[OperationEvent]:
        try:
            appended = await self.journal.append_events(
                self.principal,
                [
                    SessionEventSpec(
                        "operation",
                        event_type,
                        session_id,
                        dict(data),
                        operation_id=operation_id,
                    )
                    for event_type, data in events
                ],
                expected_last_sequence=expected_last_sequence,
                deadline_ms=deadline_ms,
            )
        except JournalDeadlineExceeded as error:
            raise OperationStoreDeadlineExceeded(str(error)) from error
        except JournalConflictError as error:
            raise OperationStoreConflictError(str(error)) from error
        return [
            OperationEvent(
                type=event.event_type,
                session_id=event.session_id,
                operation_id=event.operation_id or operation_id,
                sequence=event.sequence,
                timestamp=event.timestamp,
                data=event.payload,
            )
            for event in appended
        ]

    async def load(
        self,
        *,
        session_id: str | None = None,
        operation_id: str | None = None,
    ) -> list[OperationEvent]:
        events = await self.journal.load_events(
            self.principal,
            session_id=session_id,
            operation_id=operation_id,
            journal_kind="operation",
        )
        return [
            OperationEvent(
                type=event.event_type,
                session_id=event.session_id,
                operation_id=event.operation_id or "",
                sequence=event.sequence,
                timestamp=event.timestamp,
                data=event.payload,
            )
            for event in events
        ]

    async def try_acquire_claim(
        self,
        claim_type: str,
        resource_id: str,
        owner_token: str,
        *,
        lease_seconds: float = 300,
    ) -> bool:
        return await self.journal.try_acquire_claim(
            self.principal,
            claim_type,
            resource_id,
            owner_token,
            lease_seconds=lease_seconds,
        )

    async def release_claim(
        self,
        claim_type: str,
        resource_id: str,
        owner_token: str,
    ) -> None:
        await self.journal.release_claim(
            self.principal,
            claim_type,
            resource_id,
            owner_token,
        )


class SessionJournalRuntimeEventStore:
    """兼容 RuntimeStateTracker/RuntimeRecoveryManager 的统一 Journal 视图。"""

    def __init__(
        self,
        journal: SQLiteSessionEventJournal,
        principal: JournalPrincipal,
        *,
        session_id: str,
    ) -> None:
        if not session_id:
            raise ValueError("Runtime Adapter session_id 不能为空")
        self.journal = journal
        self.principal = principal
        self.session_id = session_id

    async def append(self, event: RuntimeEvent) -> None:
        try:
            await self.journal.append_events(
                self.principal,
                [
                    SessionEventSpec(
                        "runtime",
                        event.type,
                        self.session_id,
                        dict(event.data),
                        run_id=event.run_id,
                        source_sequence=event.sequence,
                        timestamp=event.timestamp,
                    )
                ],
            )
        except JournalConflictError as error:
            raise ValueError(str(error)) from error

    async def load(self) -> list[RuntimeEvent]:
        events = await self.journal.load_events(
            self.principal,
            session_id=self.session_id,
            journal_kind="runtime",
        )
        return [
            RuntimeEvent(
                type=event.event_type,  # type: ignore[arg-type]
                run_id=event.run_id or "",
                sequence=(
                    event.source_sequence
                    if event.source_sequence is not None
                    else event.sequence
                ),
                timestamp=event.timestamp,
                data=event.payload,
            )
            for event in events
        ]


class SessionJournalRetryEventStore:
    """兼容 RetryRecoveryManager 的统一 Journal 视图。"""

    def __init__(
        self,
        journal: SQLiteSessionEventJournal,
        principal: JournalPrincipal,
        *,
        session_id: str,
    ) -> None:
        if not session_id:
            raise ValueError("Retry Adapter session_id 不能为空")
        self.journal = journal
        self.principal = principal
        self.session_id = session_id

    async def append(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise ValueError("Retry Event 缺少 type")
        timestamp = event.get("timestamp")
        event_id = _model_terminal_event_id(
            tenant_id=self.principal.tenant_id,
            session_id=self.session_id,
            event=event,
        )
        await self.journal.append_events(
            self.principal,
            [
                SessionEventSpec(
                    "retry",
                    event_type,
                    self.session_id,
                    dict(event),
                    operation_id=_optional_text(event.get("operationId")),
                    run_id=_optional_text(event.get("runId")),
                    timestamp=(
                        timestamp
                        if isinstance(timestamp, int) and not isinstance(timestamp, bool)
                        else None
                    ),
                    event_id=event_id,
                )
            ],
        )

    async def load(self) -> list[dict[str, Any]]:
        events = await self.journal.load_events(
            self.principal,
            session_id=self.session_id,
            journal_kind="retry",
        )
        records: list[dict[str, Any]] = []
        for event in events:
            record = dict(event.payload)
            record.setdefault("type", event.event_type)
            record.setdefault("timestamp", event.timestamp)
            records.append(record)
        return records

    async def incomplete_chains_async(self) -> list[RetryChain]:
        return _incomplete_retry_chains(await self.load())

    def incomplete_chains(self) -> list[RetryChain]:
        # 兼容现有 RetryRecoveryManager 的同步发现接口。旧 JSONL Store 的
        # incomplete_chains() 同样执行同步磁盘读取；新代码优先使用异步版本。
        self.journal.access_policy.authorize(self.principal, "read")
        events = self.journal._load_events_sync(
            self.principal,
            self.session_id,
            None,
            None,
            "retry",
            None,
            False,
            None,
            None,
        )
        records: list[dict[str, Any]] = []
        for event in events:
            record = dict(event.payload)
            record.setdefault("type", event.event_type)
            record.setdefault("timestamp", event.timestamp)
            records.append(record)
        return _incomplete_retry_chains(records)


def _model_terminal_event_id(
    *,
    tenant_id: str,
    session_id: str,
    event: dict[str, Any],
) -> str | None:
    """Reserve one durable terminal slot for every logical model request."""

    if event.get("type") not in {
        "model_request_completed",
        "model_request_failed",
    }:
        return None
    request_id = event.get("requestId")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("Model Terminal Event 缺少 requestId")
    digest = hashlib.sha256(
        (
            "pi-agent-loop/model-terminal/v1\0"
            + json.dumps(
                [tenant_id, session_id, request_id],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        ).encode("utf-8")
    ).hexdigest()
    return f"model-terminal:{digest}"


def _incomplete_retry_chains(events: list[dict[str, Any]]) -> list[RetryChain]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        retry_id = event.get("retryId")
        if isinstance(retry_id, str) and retry_id:
            grouped.setdefault(retry_id, []).append(event)
    chains: list[RetryChain] = []
    for retry_id, chain_events in grouped.items():
        last = chain_events[-1]
        last_type = str(last.get("type", ""))
        if last_type in _RETRY_TERMINAL_EVENTS:
            continue
        chains.append(
            RetryChain(
                retry_id=retry_id,
                kind=str(last.get("kind", _retry_kind(last_type))),
                logical_id=_retry_logical_id(last),
                last_event=last_type,
                attempt=int(last.get("attempt", 0)),
                events=tuple(chain_events),
            )
        )
    return chains


def _retry_kind(event_type: str) -> str:
    for prefix in ("model", "tool", "task"):
        if event_type.startswith(prefix + "_"):
            return prefix
    return "unknown"


def _retry_logical_id(event: dict[str, Any]) -> str | None:
    for key in ("logicalId", "toolCallId", "taskId", "turnId"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _optional_text(value: Any) -> str | None:
    return str(value) if value is not None and str(value) else None
