"""按规范文件路径排队的进程内 mutation lock。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from .path_policy import WorkspacePathPolicy


@dataclass(slots=True)
class _LockEntry:
    lock: asyncio.Lock
    references: int = 0


class FileMutationQueue:
    def __init__(self, policy: WorkspacePathPolicy) -> None:
        self._policy = policy
        self._guard = asyncio.Lock()
        self._entries: dict[str, _LockEntry] = {}

    @asynccontextmanager
    async def acquire(self, path: Path) -> AsyncIterator[None]:
        key = self._policy.key(path)
        async with self._guard:
            entry = self._entries.setdefault(key, _LockEntry(asyncio.Lock()))
            entry.references += 1
        acquired = False
        try:
            await entry.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                entry.lock.release()
            async with self._guard:
                entry.references -= 1
                if entry.references == 0 and not entry.lock.locked():
                    self._entries.pop(key, None)
