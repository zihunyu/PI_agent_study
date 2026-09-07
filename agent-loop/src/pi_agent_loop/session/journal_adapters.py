"""统一 Session Journal 到现有 Runtime/Operation/Retry Store API 的适配器。"""

from __future__ import annotations

from .journal import SessionEventJournal, validate_session_event_journal, SynchronousSessionEventJournal

import hashlib
import json
from typing import Any, Protocol, runtime_checkable

from ..retry.events import RetryChain
from ..runtime.events import RuntimeEvent
from .journal import (
    JournalConflictError,
    JournalDeadlineExceeded,
    JournalFencedClaimLostError,
    JournalPrincipal,
    SessionEventSpec,
)
from .operation_events import OperationEvent
from .operation_store import (
    ClaimLease,
    OperationEventStore,
    OperationEventSpec,
    OperationStoreConflictError,
    OperationStoreDeadlineExceeded,
    OperationStoreFencedClaimLostError,
    validate_fenced_claim_scope,
)
from .store import (
    RuntimeStoreConflictError,
    RuntimeStoreFencedClaimLostError,
    validate_runtime_fenced_claim_scope,
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
    supports_cross_process_claims = True

    def __init__(
        self,
        journal: SessionEventJournal,
        principal: JournalPrincipal,
    ) -> None:
        self.journal = validate_session_event_journal(journal)
        self.principal = principal

    def event_spec(self, event_type: str, session_id: str, operation_id: str, data: dict[str, Any], *, event_id: str | None = None) -> SessionEventSpec:
        """Build a conversation fact for the shared atomic Journal transaction."""
        return SessionEventSpec("operation", event_type, session_id, dict(data), operation_id=operation_id, event_id=event_id)

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

    async def append_batch_if_fenced_claim(
        self,
        session_id: str,
        operation_id: str,
        events: list[OperationEventSpec],
        lease: ClaimLease,
        *,
        renew_lease_seconds: float,
        expected_last_sequence: int | None = None,
        deadline_ms: int | None = None,
        expected_claim_entity_id: str | None = None,
    ) -> list[OperationEvent]:
        validate_fenced_claim_scope(
            lease,
            session_id=session_id,
            operation_id=operation_id,
            expected_entity_id=expected_claim_entity_id,
        )
        try:
            appended = await self.journal.append_events_if_fenced_claim(
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
                lease,
                renew_lease_seconds=renew_lease_seconds,
                expected_last_sequence=expected_last_sequence,
                deadline_ms=deadline_ms,
            )
        except JournalDeadlineExceeded as error:
            raise OperationStoreDeadlineExceeded(str(error)) from error
        except JournalFencedClaimLostError as error:
            raise OperationStoreFencedClaimLostError(str(error)) from error
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

    async def acquire_fenced_claim(
        self,
        claim_type: str,
        resource_id: str,
        owner_token: str,
        *,
        lease_seconds: float = 300,
    ) -> ClaimLease | None:
        return await self.journal.acquire_fenced_claim(
            self.principal,
            claim_type,
            resource_id,
            owner_token,
            lease_seconds=lease_seconds,
        )

    async def renew_fenced_claim(
        self,
        lease: ClaimLease,
        *,
        lease_seconds: float = 300,
    ) -> bool:
        return await self.journal.renew_fenced_claim(
            self.principal,
            lease,
            lease_seconds=lease_seconds,
        )

    async def verify_fenced_claim(self, lease: ClaimLease) -> bool:
        return await self.journal.verify_fenced_claim(self.principal, lease)

    async def release_fenced_claim(self, lease: ClaimLease) -> None:
        await self.journal.release_fenced_claim(self.principal, lease)


class SessionJournalRuntimeEventStore:
    """兼容 RuntimeStateTracker/RuntimeRecoveryManager 的统一 Journal 视图。"""

    supports_fenced_runtime_append = True

    def __init__(
        self,
        journal: SessionEventJournal,
        principal: JournalPrincipal,
        *,
        session_id: str,
    ) -> None:
        if not session_id:
            raise ValueError("Runtime Adapter session_id 不能为空")
        self.journal = validate_session_event_journal(journal)
        self.principal = principal
        self.session_id = session_id

    async def append(self, event: RuntimeEvent) -> None:
        events = await self.load()
        expected = events[-1].sequence if events else -1
        await self.append_cas(event, expected_last_sequence=expected)

    async def append_cas(
        self,
        event: RuntimeEvent,
        *,
        expected_last_sequence: int,
    ) -> None:
        rows = await self._load_rows()
        current = rows[-1].source_sequence if rows else -1
        if current != expected_last_sequence or event.sequence != current + 1:
            raise RuntimeStoreConflictError(
                f"Runtime Version 冲突：expected={expected_last_sequence}, actual={current}"
            )
        try:
            await self.journal.append_events(
                self.principal,
                [self._spec(event)],
                expected_last_sequence=(rows[-1].sequence if rows else -1),
            )
        except JournalConflictError as error:
            raise RuntimeStoreConflictError(str(error)) from error

    async def append_cas_if_fenced_claim(
        self,
        event: RuntimeEvent,
        lease: ClaimLease,
        *,
        renew_lease_seconds: float,
        expected_last_sequence: int,
    ) -> None:
        validate_runtime_fenced_claim_scope(
            lease,
            session_id=self.session_id,
        )
        rows = await self._load_rows()
        current = rows[-1].source_sequence if rows else -1
        if current != expected_last_sequence or event.sequence != current + 1:
            raise RuntimeStoreConflictError(
                f"Runtime Version 冲突：expected={expected_last_sequence}, actual={current}"
            )
        try:
            await self.journal.append_events_if_fenced_claim(
                self.principal,
                [self._spec(event)],
                lease,
                renew_lease_seconds=renew_lease_seconds,
                expected_last_sequence=(rows[-1].sequence if rows else -1),
            )
        except JournalFencedClaimLostError as error:
            raise RuntimeStoreFencedClaimLostError(str(error)) from error
        except JournalConflictError as error:
            raise RuntimeStoreConflictError(str(error)) from error

    async def load(self) -> list[RuntimeEvent]:
        events = await self._load_rows()
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

    async def _load_rows(self):
        return await self.journal.load_events(
            self.principal,
            session_id=self.session_id,
            journal_kind="runtime",
        )

    def _spec(self, event: RuntimeEvent) -> SessionEventSpec:
        return SessionEventSpec(
            "runtime",
            event.type,
            self.session_id,
            dict(event.data),
            run_id=event.run_id,
            source_sequence=event.sequence,
            timestamp=event.timestamp,
        )


class SessionJournalRetryEventStore:
    """兼容 RetryRecoveryManager 的统一 Journal 视图。"""

    def __init__(
        self,
        journal: SessionEventJournal,
        principal: JournalPrincipal,
        *,
        session_id: str,
    ) -> None:
        if not session_id:
            raise ValueError("Retry Adapter session_id 不能为空")
        self.journal = validate_session_event_journal(journal)
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
        if not isinstance(self.journal, SynchronousSessionEventJournal):
            raise TypeError("Async-only Journal: use incomplete_chains_async()")
        events = self.journal.load_events_sync(self.principal, session_id=self.session_id, journal_kind="retry")
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


@runtime_checkable
class JournalOperationStore(OperationEventStore, Protocol):
    """Conversation/operation participant; session scope is explicit per call."""

    @property
    def journal(self) -> SessionEventJournal: ...
    @property
    def principal(self) -> JournalPrincipal: ...

    def event_spec(
        self,
        event_type: str,
        session_id: str,
        operation_id: str,
        data: dict[str, Any],
        *,
        event_id: str | None = None,
    ) -> SessionEventSpec: ...
