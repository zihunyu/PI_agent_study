"""Retry 生命周期 JSONL 持久化与进程恢复协调。"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..async_utils import durable_to_thread

_TERMINAL_EVENTS = {
    "model_retry_finished",
    "tool_retry_finished",
    "task_retry_finished",
    "retry_recovery_finished",
}
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "password",
    "token",
    "cookie",
    "idempotency_key",
}


class RetryEventStore(Protocol):
    async def append(self, event: dict[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class RetryChain:
    retry_id: str
    kind: str
    logical_id: str | None
    last_event: str
    attempt: int
    events: tuple[dict[str, Any], ...]


class JsonlRetryEventStore:
    """只持久化脱敏 Retry 元数据，不持久化 Prompt、工具参数或 API Key。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()

    async def append(self, event: dict[str, Any]) -> None:
        record = _sanitize(event)
        record.setdefault("timestamp", int(time.time() * 1000))
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        async with self._lock:
            await durable_to_thread(self._append_line, encoded)

    def _append_line(self, encoded: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as file:
            file.write(encoded + "\n")
            file.flush()

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line_number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Retry JSONL 第 {line_number} 行格式错误"
                ) from error
            if not isinstance(value, dict):
                raise ValueError(f"Retry JSONL 第 {line_number} 行必须是对象")
            records.append(value)
        return records

    def incomplete_chains(self) -> list[RetryChain]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for event in self.load():
            retry_id = event.get("retryId")
            if isinstance(retry_id, str) and retry_id:
                grouped.setdefault(retry_id, []).append(event)

        chains: list[RetryChain] = []
        for retry_id, events in grouped.items():
            last = events[-1]
            last_type = str(last.get("type", ""))
            if last_type in _TERMINAL_EVENTS:
                continue
            chains.append(
                RetryChain(
                    retry_id=retry_id,
                    kind=str(last.get("kind", _kind_from_event(last_type))),
                    logical_id=_logical_id(last),
                    last_event=last_type,
                    attempt=int(last.get("attempt", 0)),
                    events=tuple(events),
                )
            )
        return chains


class RetryRecoveryManager:
    """启动时发现未完成 Retry Chain，并由 Host 提供安全恢复处理器。"""

    def __init__(self, store: RetryEventStore) -> None:
        self.store = store

    async def recover(
        self,
        handler: Callable[[RetryChain], Awaitable[bool]],
    ) -> list[RetryChain]:
        recovered: list[RetryChain] = []
        discover = getattr(self.store, "incomplete_chains_async", None)
        if callable(discover):
            chains = await discover()
        else:
            legacy = getattr(self.store, "incomplete_chains", None)
            if not callable(legacy):
                raise TypeError("Retry recovery requires incomplete_chains_async or the legacy synchronous discovery method")
            chains = await durable_to_thread(legacy)
        for chain in chains:
            await self.store.append(
                {
                    "type": "retry_recovery_started",
                    "kind": chain.kind,
                    "retryId": chain.retry_id,
                    "logicalId": chain.logical_id,
                    "attempt": chain.attempt,
                }
            )
            success = await handler(chain)
            await self.store.append(
                {
                    "type": "retry_recovery_finished",
                    "kind": chain.kind,
                    "retryId": chain.retry_id,
                    "logicalId": chain.logical_id,
                    "attempt": chain.attempt,
                    "success": bool(success),
                }
            )
            recovered.append(chain)
        return recovered


def _sanitize(value: Any, *, key: str = "") -> Any:
    if key.casefold() in _SENSITIVE_KEYS:
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): _sanitize(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > 500:
            return value[:500] + "<truncated>"
        return value
    return repr(value)[:500]


def _kind_from_event(event_type: str) -> str:
    if event_type.startswith("model_"):
        return "model"
    if event_type.startswith("tool_"):
        return "tool"
    if event_type.startswith("task_"):
        return "task"
    return "unknown"


def _logical_id(event: dict[str, Any]) -> str | None:
    for key in ("logicalId", "toolCallId", "taskId", "turnId"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return None
