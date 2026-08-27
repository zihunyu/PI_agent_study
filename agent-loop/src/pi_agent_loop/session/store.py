"""Runtime Event Store 接口与内存实现。"""

from __future__ import annotations

import asyncio
from typing import Protocol

from ..runtime.events import RuntimeEvent


class RuntimeEventStore(Protocol):
    async def append(self, event: RuntimeEvent) -> None: ...
    async def load(self) -> list[RuntimeEvent]: ...


class InMemoryRuntimeEventStore:
    def __init__(self) -> None:
        self._events: list[RuntimeEvent] = []
        self._lock = asyncio.Lock()

    async def append(self, event: RuntimeEvent) -> None:
        async with self._lock:
            if self._events and event.sequence <= self._events[-1].sequence:
                raise ValueError("Runtime Event sequence 必须单调递增")
            self._events.append(event)

    async def load(self) -> list[RuntimeEvent]:
        async with self._lock:
            return list(self._events)
