"""Runtime Event 的 JSONL 持久存储。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ..async_utils import durable_to_thread
from ..runtime.events import RuntimeEvent


class JsonlRuntimeEventStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()

    async def append(self, event: RuntimeEvent) -> None:
        encoded = json.dumps(
            event.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        async with self._lock:
            await durable_to_thread(self._append_line, encoded)

    def _append_line(self, encoded: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as file:
            file.write(encoded + "\n")
            file.flush()

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
