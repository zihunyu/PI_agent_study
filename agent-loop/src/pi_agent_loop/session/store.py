"""Runtime Event Store 接口与内存实现。"""

from __future__ import annotations

import asyncio
from typing import Protocol

from ..runtime.events import RuntimeEvent
from .operation_store import ClaimLease


class RuntimeEventStore(Protocol):
    supports_fenced_runtime_append: bool

    async def append(self, event: RuntimeEvent) -> None: ...
    async def append_cas(
        self,
        event: RuntimeEvent,
        *,
        expected_last_sequence: int,
    ) -> None: ...
    async def append_cas_if_fenced_claim(
        self,
        event: RuntimeEvent,
        lease: ClaimLease,
        *,
        renew_lease_seconds: float,
        expected_last_sequence: int,
    ) -> None: ...
    async def load(self) -> list[RuntimeEvent]: ...


class RuntimeStoreConflictError(RuntimeError):
    """Runtime stream changed after projection; caller must reload."""


class RuntimeStoreFencedClaimLostError(RuntimeStoreConflictError):
    """Runtime Recovery lease is stale or belongs to another Session."""


class RuntimeStoreFencedAppendUnsupportedError(RuntimeStoreConflictError):
    """The backend cannot atomically validate a claim and append Runtime state."""


def validate_runtime_fenced_claim_scope(
    lease: ClaimLease,
    *,
    session_id: str,
) -> None:
    """Bind a Runtime Recovery lease to exactly one Session stream."""

    if lease.claim_type not in {
        "runtime_recovery",
        "conversation_session_writer",
    } or lease.resource_id != session_id:
        raise RuntimeStoreFencedClaimLostError(
            "Runtime Recovery Fenced Claim 与目标 Session 不匹配"
        )


class InMemoryRuntimeEventStore:
    # This Store has no durable claim row.  A claim issued by some other Store
    # therefore cannot be checked in the same transaction as this append.
    supports_fenced_runtime_append = False

    def __init__(self) -> None:
        self._events: list[RuntimeEvent] = []
        self._lock = asyncio.Lock()

    async def append(self, event: RuntimeEvent) -> None:
        expected = (self._events[-1].sequence if self._events else -1)
        await self.append_cas(event, expected_last_sequence=expected)

    async def append_cas(
        self,
        event: RuntimeEvent,
        *,
        expected_last_sequence: int,
    ) -> None:
        async with self._lock:
            current = self._events[-1].sequence if self._events else -1
            if current != expected_last_sequence or event.sequence != current + 1:
                raise RuntimeStoreConflictError(
                    f"Runtime Version 冲突：expected={expected_last_sequence}, actual={current}"
                )
            self._events.append(event)

    async def append_cas_if_fenced_claim(
        self,
        event: RuntimeEvent,
        lease: ClaimLease,
        *,
        renew_lease_seconds: float,
        expected_last_sequence: int,
    ) -> None:
        del event, lease, renew_lease_seconds, expected_last_sequence
        raise RuntimeStoreFencedAppendUnsupportedError(
            "内存 Runtime Store 无法与外部 Claim 原子提交"
        )

    async def load(self) -> list[RuntimeEvent]:
        async with self._lock:
            return list(self._events)
