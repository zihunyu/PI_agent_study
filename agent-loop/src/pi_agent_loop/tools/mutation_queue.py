"""按规范文件路径排队的进程内 mutation lock。"""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator
from _thread import LockType

from .path_policy import WorkspacePathPolicy


@dataclass(slots=True)
class _LockEntry:
    lock: LockType
    references: int = 0


# Shared across ToolServices instances and event loops in this process. Only
# referenced paths are retained, so opening many short-lived workspaces does
# not grow the registry forever. These locks do not coordinate other processes.
_REGISTRY_GUARD = threading.Lock()
_ENTRIES: dict[str, _LockEntry] = {}


class FileMutationQueue:
    def __init__(self, policy: WorkspacePathPolicy) -> None:
        self._policy = policy

    @asynccontextmanager
    async def acquire(self, path: Path) -> AsyncIterator[None]:
        key = self._policy.key(path)
        with _REGISTRY_GUARD:
            entry = _ENTRIES.setdefault(key, _LockEntry(threading.Lock()))
            entry.references += 1
        acquired = False
        try:
            # Never submit a blocking acquire to an executor: a cancelled waiter
            # could acquire the lock later without an owner to release it.
            while not entry.lock.acquire(blocking=False):
                await asyncio.sleep(0.01)
            acquired = True
            yield
        finally:
            if acquired:
                entry.lock.release()
            with _REGISTRY_GUARD:
                entry.references -= 1
                if entry.references == 0 and not entry.lock.locked():
                    _ENTRIES.pop(key, None)
