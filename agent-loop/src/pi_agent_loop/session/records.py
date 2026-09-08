"""Small versioned records over public Journal CAS/fencing operations."""

from __future__ import annotations

import copy
import re
from typing import Any

from .journal import (
    JournalPrincipal,
    SessionEvent,
    SessionEventJournal,
    SessionEventSpec,
    SessionStreamKey,
)
from .operation_store import ClaimLease


class JournalRecordStore:
    def __init__(
        self,
        journal: SessionEventJournal,
        principal: JournalPrincipal,
        session_id: str,
        namespace: str,
    ) -> None:
        if not session_id or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", namespace):
            raise ValueError("invalid record scope")
        self.journal = journal
        self.principal = principal
        self.session_id = session_id
        self.namespace = namespace

    def operation_id(self, key: str) -> str:
        if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,160}", key):
            raise ValueError("record key must be a bounded identifier")
        return f"records:{self.namespace}:{key}"

    async def read(self, key: str) -> tuple[SessionEvent, ...]:
        return tuple(
            await self.journal.load_events(
                self.principal,
                session_id=self.session_id,
                operation_id=self.operation_id(key),
                journal_kind="audit",
            )
        )

    async def all(self) -> dict[str, tuple[SessionEvent, ...]]:
        events = await self.journal.load_events(
            self.principal, session_id=self.session_id, journal_kind="audit"
        )
        prefix = f"records:{self.namespace}:"
        grouped: dict[str, list[SessionEvent]] = {}
        for event in events:
            if event.operation_id is not None and event.operation_id.startswith(prefix):
                grouped.setdefault(event.operation_id[len(prefix) :], []).append(event)
        return {key: tuple(value) for key, value in grouped.items()}

    async def append(
        self,
        key: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        expected_version: int,
        claim: ClaimLease | None = None,
        lease_seconds: float = 30,
    ) -> SessionEvent:
        if type(expected_version) is not int or expected_version < -1:
            raise ValueError("expected_version must be an integer >= -1")
        operation_id = self.operation_id(key)
        # Journal CAS uses the last *global* event sequence in this stream;
        # it is not the number of events returned by a filtered read.
        specs = [
            SessionEventSpec(
                journal_kind="audit",
                event_type=event_type,
                session_id=self.session_id,
                operation_id=operation_id,
                payload=copy.deepcopy(payload),
            )
        ]
        expected: dict[SessionStreamKey, int] = {
            ("audit", self.session_id, operation_id): expected_version
        }
        if claim is None:
            events = await self.journal.append_events(
                self.principal, specs, expected_stream_sequences=expected
            )
        else:
            events = await self.journal.append_events_if_fenced_claim(
                self.principal,
                specs,
                claim,
                renew_lease_seconds=lease_seconds,
                expected_stream_sequences=expected,
            )
        return events[0]
