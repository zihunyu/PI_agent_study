"""Runtime Event 的 JSONL 持久存储。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from ..async_utils import durable_to_thread
from ..runtime.events import RuntimeEvent
from .operation_store import ClaimLease
from .store import (
    RuntimeStoreConflictError,
    RuntimeStoreFencedAppendUnsupportedError,
)


class JsonlRuntimeEventStore:
    # A process-local asyncio.Lock cannot make a JSONL append atomic with a
    # claim row owned by another Store/process.
    supports_fenced_runtime_append = False

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()

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
        encoded = json.dumps(
            event.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        async with self._lock:
            events = await durable_to_thread(self._load_sync)
            current = events[-1].sequence if events else -1
            if current != expected_last_sequence or event.sequence != current + 1:
                raise RuntimeStoreConflictError(
                    f"Runtime Version 冲突：expected={expected_last_sequence}, actual={current}"
                )
            await durable_to_thread(self._append_line, encoded)

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
            "JSONL Runtime Store 不支持跨进程原子 Fenced Append"
        )

    def _append_line(self, encoded: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as file:
            file.write(encoded + "\n")
            file.flush()
            os.fsync(file.fileno())

    async def load(self) -> list[RuntimeEvent]:
        async with self._lock:
            return await durable_to_thread(self._load_sync)

    def _load_sync(self) -> list[RuntimeEvent]:
        if not self.path.exists():
            return []
        events: list[RuntimeEvent] = []
        for line_number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                event = RuntimeEvent.from_dict(value)
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                raise ValueError(
                    f"Runtime Event JSONL 第 {line_number} 行无效"
                ) from error
            events.append(event)
        return events
