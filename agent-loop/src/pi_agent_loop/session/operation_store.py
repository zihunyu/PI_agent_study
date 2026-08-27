"""Operation Event Store：完整消息、模型请求和工具执行事实。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Protocol

from .operation_events import OperationEvent


class OperationEventStore(Protocol):
    async def append(
        self,
        event_type: str,
        session_id: str,
        operation_id: str,
        data: dict[str, Any] | None = None,
    ) -> OperationEvent: ...

    async def load(
        self,
        *,
        session_id: str | None = None,
        operation_id: str | None = None,
    ) -> list[OperationEvent]: ...


class InMemoryOperationEventStore:
    def __init__(self) -> None:
        self._events: list[OperationEvent] = []
        self._lock = asyncio.Lock()

    async def append(self, event_type, session_id, operation_id, data=None):
        async with self._lock:
            event = OperationEvent(
                type=event_type,
                session_id=session_id,
                operation_id=operation_id,
                sequence=len(self._events),
                data=data or {},
            )
            self._events.append(event)
            return event

    async def load(self, *, session_id=None, operation_id=None):
        async with self._lock:
            return [
                event
                for event in self._events
                if (session_id is None or event.session_id == session_id)
                and (operation_id is None or event.operation_id == operation_id)
            ]


class JsonlOperationEventStore:
    """单进程 JSONL 实现；生产多写者应替换为事务数据库。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()
        self._next_sequence: int | None = None

    async def append(self, event_type, session_id, operation_id, data=None):
        async with self._lock:
            if self._next_sequence is None:
                existing = await asyncio.to_thread(self._load_sync)
                self._next_sequence = (
                    existing[-1].sequence + 1 if existing else 0
                )
            event = OperationEvent(
                type=event_type,
                session_id=session_id,
                operation_id=operation_id,
                sequence=self._next_sequence,
                data=data or {},
            )
            await asyncio.to_thread(self._append_sync, event)
            self._next_sequence += 1
            return event

    def _append_sync(self, event: OperationEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8", newline="\n") as file:
            file.write(encoded + "\n")
            file.flush()
            os.fsync(file.fileno())

    async def load(self, *, session_id=None, operation_id=None):
        async with self._lock:
            events = await asyncio.to_thread(self._load_sync)
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
