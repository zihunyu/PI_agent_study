"""Operation Event Store：完整消息、模型请求和工具执行事实。"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Protocol

from ..async_utils import durable_to_thread
from .operation_events import OperationEvent

OperationEventSpec = tuple[str, dict[str, Any]]


class OperationStoreConflictError(RuntimeError):
    """条件追加或唯一约束失败；调用方必须重新读取并重新判断。"""


class OperationStoreDeadlineExceeded(OperationStoreConflictError):
    """事务开始后发现业务截止时间已经到期。"""


class OperationEventStore(Protocol):
    supports_atomic_transactions: bool

    async def append(
        self,
        event_type: str,
        session_id: str,
        operation_id: str,
        data: dict[str, Any] | None = None,
    ) -> OperationEvent: ...

    async def append_batch(
        self,
        session_id: str,
        operation_id: str,
        events: list[OperationEventSpec],
        *,
        expected_last_sequence: int | None = None,
        deadline_ms: int | None = None,
    ) -> list[OperationEvent]: ...

    async def load(
        self,
        *,
        session_id: str | None = None,
        operation_id: str | None = None,
    ) -> list[OperationEvent]: ...

    async def try_acquire_claim(
        self,
        claim_type: str,
        resource_id: str,
        owner_token: str,
        *,
        lease_seconds: float = 300,
    ) -> bool: ...

    async def release_claim(
        self,
        claim_type: str,
        resource_id: str,
        owner_token: str,
    ) -> None: ...


class InMemoryOperationEventStore:
    supports_atomic_transactions = True

    def __init__(self) -> None:
        self._events: list[OperationEvent] = []
        self._lock = asyncio.Lock()
        self._claims: dict[tuple[str, str], tuple[str, float]] = {}

    async def append(self, event_type, session_id, operation_id, data=None):
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
        _validate_batch(events)
        async with self._lock:
            current = _last_operation_sequence(
                self._events,
                session_id,
                operation_id,
            )
            _check_expected(current, expected_last_sequence)
            _check_deadline(deadline_ms)
            _check_unique_operation_facts(self._events, events)
            appended: list[OperationEvent] = []
            for event_type, data in events:
                event = OperationEvent(
                    type=event_type,
                    session_id=session_id,
                    operation_id=operation_id,
                    sequence=len(self._events),
                    data=dict(data),
                )
                self._events.append(event)
                appended.append(event)
            return appended

    async def load(self, *, session_id=None, operation_id=None):
        async with self._lock:
            return [
                event
                for event in self._events
                if (session_id is None or event.session_id == session_id)
                and (operation_id is None or event.operation_id == operation_id)
            ]

    async def try_acquire_claim(
        self,
        claim_type: str,
        resource_id: str,
        owner_token: str,
        *,
        lease_seconds: float = 300,
    ) -> bool:
        _validate_claim(claim_type, resource_id, owner_token, lease_seconds)
        async with self._lock:
            now = time.monotonic()
            key = (claim_type, resource_id)
            current = self._claims.get(key)
            if (
                current is not None
                and current[0] != owner_token
                and current[1] > now
            ):
                return False
            self._claims[key] = (owner_token, now + lease_seconds)
            return True

    async def release_claim(
        self,
        claim_type: str,
        resource_id: str,
        owner_token: str,
    ) -> None:
        async with self._lock:
            key = (claim_type, resource_id)
            current = self._claims.get(key)
            if current is not None and current[0] == owner_token:
                self._claims.pop(key, None)


class JsonlOperationEventStore:
    """单实例、单进程 JSONL 兼容实现；不承诺崩溃时批次原子性。"""

    supports_atomic_transactions = False

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()
        self._next_sequence: int | None = None
        # Claim 只在当前 Store 实例内有效；跨进程 Claim 由 SQLite 实现。
        self._claims: dict[tuple[str, str], tuple[str, float]] = {}

    async def append(self, event_type, session_id, operation_id, data=None):
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
        _validate_batch(events)
        async with self._lock:
            existing = await durable_to_thread(self._load_sync)
            current = _last_operation_sequence(
                existing,
                session_id,
                operation_id,
            )
            _check_expected(current, expected_last_sequence)
            _check_deadline(deadline_ms)
            _check_unique_operation_facts(existing, events)
            if self._next_sequence is None:
                self._next_sequence = (
                    existing[-1].sequence + 1 if existing else 0
                )
            appended: list[OperationEvent] = []
            for event_type, data in events:
                appended.append(
                    OperationEvent(
                        type=event_type,
                        session_id=session_id,
                        operation_id=operation_id,
                        sequence=self._next_sequence,
                        data=dict(data),
                    )
                )
                self._next_sequence += 1
            await durable_to_thread(self._append_many_sync, appended)
            return appended

    def _append_many_sync(self, events: list[OperationEvent]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = [
            json.dumps(
                event.to_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            for event in events
        ]
        with self.path.open("a", encoding="utf-8", newline="\n") as file:
            file.write("".join(line + "\n" for line in encoded))
            file.flush()
            os.fsync(file.fileno())

    async def load(self, *, session_id=None, operation_id=None):
        async with self._lock:
            events = await durable_to_thread(self._load_sync)
        return [
            event
            for event in events
            if (session_id is None or event.session_id == session_id)
            and (operation_id is None or event.operation_id == operation_id)
        ]

    def _load_sync(self) -> list[OperationEvent]:
        if not self.path.exists():
            return []
        events: list[OperationEvent] = []
        previous = -1
        for line_number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                event = OperationEvent.from_dict(raw)
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                raise ValueError(
                    f"Operation JSONL 第 {line_number} 行无效"
                ) from error
            if event.sequence <= previous:
                raise ValueError("Operation JSONL sequence 必须严格递增")
            previous = event.sequence
            events.append(event)
        return events

    async def try_acquire_claim(
        self,
        claim_type: str,
        resource_id: str,
        owner_token: str,
        *,
        lease_seconds: float = 300,
    ) -> bool:
        _validate_claim(claim_type, resource_id, owner_token, lease_seconds)
        async with self._lock:
            now = time.monotonic()
            key = (claim_type, resource_id)
            current = self._claims.get(key)
            if (
                current is not None
                and current[0] != owner_token
                and current[1] > now
            ):
                return False
            self._claims[key] = (owner_token, now + lease_seconds)
            return True

    async def release_claim(
        self,
        claim_type: str,
        resource_id: str,
        owner_token: str,
    ) -> None:
        async with self._lock:
            key = (claim_type, resource_id)
            current = self._claims.get(key)
            if current is not None and current[0] == owner_token:
                self._claims.pop(key, None)


def operation_last_sequence(events: list[OperationEvent]) -> int:
    """返回已按 Operation 过滤的 Event 版本；空 Operation 为 -1。"""

    return events[-1].sequence if events else -1


def _last_operation_sequence(
    events: list[OperationEvent],
    session_id: str,
    operation_id: str,
) -> int:
    return next(
        (
            event.sequence
            for event in reversed(events)
            if event.session_id == session_id
            and event.operation_id == operation_id
        ),
        -1,
    )


def _check_expected(current: int, expected: int | None) -> None:
    if expected is not None and current != expected:
        raise OperationStoreConflictError(
            f"Operation Version 冲突：expected={expected}, actual={current}"
        )


def _check_deadline(deadline_ms: int | None) -> None:
    if deadline_ms is not None and int(time.time() * 1000) >= deadline_ms:
        raise OperationStoreDeadlineExceeded(
            f"事务截止时间已到：deadline={deadline_ms}"
        )


def _validate_batch(events: list[OperationEventSpec]) -> None:
    if not events:
        raise ValueError("Operation Event Batch 不能为空")
    for event_type, data in events:
        if not event_type or not isinstance(data, dict):
            raise ValueError("Operation Event Batch 包含无效事件")


def _validate_claim(
    claim_type: str,
    resource_id: str,
    owner_token: str,
    lease_seconds: float,
) -> None:
    if not claim_type or not resource_id or not owner_token:
        raise ValueError("Claim 必须包含 type/resource/owner")
    if lease_seconds <= 0:
        raise ValueError("Claim lease_seconds 必须大于 0")


def _check_unique_operation_facts(
    existing: list[OperationEvent],
    specs: list[OperationEventSpec],
) -> None:
    """让内存/JSONL Store 与 SQLite 的关键全局唯一约束一致。"""

    constraints = (
        ("approval_requested", "approvalId"),
        ("write_prepared", "writeId"),
        ("write_prepared", "idempotencyKeyHash"),
    )
    for event_type, key in constraints:
        known = {
            str(event.data.get(key))
            for event in existing
            if event.type == event_type and event.data.get(key) is not None
        }
        incoming = [
            str(data.get(key))
            for candidate_type, data in specs
            if candidate_type == event_type and data.get(key) is not None
        ]
        if len(incoming) != len(set(incoming)) or any(
            value in known for value in incoming
        ):
            raise OperationStoreConflictError(
                f"Operation 唯一事实冲突：{event_type}.{key}"
            )
