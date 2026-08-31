"""Tenant/session-scoped pending clarification state contracts.

The built-in store is deliberately in-memory.  Durable applications can inject
the same small protocol using their transactional database; the Router never
falls back to a process-global, unscoped pending intent.
"""

from __future__ import annotations

import asyncio
import copy
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .types import JsonValue


@dataclass(frozen=True, slots=True)
class ClarificationState:
    """One unresolved intent and its trusted, already-extracted slots."""

    tenant_id: str
    session_id: str
    pending_intent: str
    extracted_fields: Mapping[str, JsonValue]
    missing_fields: tuple[str, ...]
    created_at: float
    expires_at: float

    def __post_init__(self) -> None:
        for name in ("tenant_id", "session_id", "pending_intent"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise ValueError(f"ClarificationState.{name} 必须是非空短字符串")
        if not isinstance(self.extracted_fields, Mapping):
            raise TypeError("ClarificationState.extracted_fields 必须是 Mapping")
        if not isinstance(self.missing_fields, tuple) or not self.missing_fields:
            raise ValueError("ClarificationState.missing_fields 必须是非空 tuple")
        if len(self.missing_fields) != len(set(self.missing_fields)) or any(
            not isinstance(field, str) or not field or len(field) > 200
            for field in self.missing_fields
        ):
            raise ValueError("ClarificationState.missing_fields 包含无效字段")
        if any(field in self.extracted_fields for field in self.missing_fields):
            raise ValueError("已提取字段不能同时出现在 missing_fields")
        fields = _strict_json_object(self.extracted_fields)
        object.__setattr__(self, "tenant_id", self.tenant_id.strip())
        object.__setattr__(self, "session_id", self.session_id.strip())
        object.__setattr__(self, "pending_intent", self.pending_intent.strip())
        object.__setattr__(self, "extracted_fields", fields)
        for name in ("created_at", "expires_at"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"ClarificationState.{name} 必须是时间戳")
            if not math.isfinite(float(value)):
                raise ValueError(f"ClarificationState.{name} 必须是有限时间戳")
        if self.expires_at <= self.created_at:
            raise ValueError("ClarificationState.expires_at 必须晚于 created_at")


class ClarificationStateStore(Protocol):
    """Injectable persistence boundary; keys always include tenant and session."""

    async def load(
        self,
        tenant_id: str,
        session_id: str,
    ) -> ClarificationState | None: ...

    async def save(self, state: ClarificationState) -> None: ...

    async def clear(self, tenant_id: str, session_id: str) -> None: ...


class InMemoryClarificationStateStore:
    """Concurrency-safe development store with deterministic TTL testing."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        if not callable(clock):
            raise TypeError("clock 必须可调用")
        self._clock = clock
        self._states: dict[tuple[str, str], ClarificationState] = {}
        self._lock = asyncio.Lock()

    async def load(
        self,
        tenant_id: str,
        session_id: str,
    ) -> ClarificationState | None:
        key = _scope_key(tenant_id, session_id)
        async with self._lock:
            state = self._states.get(key)
            if state is None:
                return None
            if state.expires_at <= float(self._clock()):
                self._states.pop(key, None)
                return None
            return _clone_state(state)

    async def save(self, state: ClarificationState) -> None:
        if not isinstance(state, ClarificationState):
            raise TypeError("state 必须是 ClarificationState")
        key = _scope_key(state.tenant_id, state.session_id)
        async with self._lock:
            if state.expires_at <= float(self._clock()):
                self._states.pop(key, None)
                return
            self._states[key] = _clone_state(state)

    async def clear(self, tenant_id: str, session_id: str) -> None:
        key = _scope_key(tenant_id, session_id)
        async with self._lock:
            self._states.pop(key, None)


def _scope_key(tenant_id: str, session_id: str) -> tuple[str, str]:
    values: list[str] = []
    for name, value in (("tenant_id", tenant_id), ("session_id", session_id)):
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise ValueError(f"{name} 必须是非空短字符串")
        values.append(value.strip())
    return values[0], values[1]


def _clone_state(state: ClarificationState) -> ClarificationState:
    return ClarificationState(
        tenant_id=state.tenant_id,
        session_id=state.session_id,
        pending_intent=state.pending_intent,
        extracted_fields=copy.deepcopy(dict(state.extracted_fields)),
        missing_fields=tuple(state.missing_fields),
        created_at=state.created_at,
        expires_at=state.expires_at,
    )


def _strict_json_object(value: Mapping[str, Any]) -> dict[str, JsonValue]:
    if len(value) > 100:
        raise ValueError("ClarificationState.extracted_fields 字段过多")
    output: dict[str, JsonValue] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > 200:
            raise ValueError("ClarificationState.extracted_fields 字段名无效")
        output[key] = _strict_json_value(item)
    return output


def _strict_json_value(value: Any, *, depth: int = 0) -> JsonValue:
    if depth > 8:
        raise ValueError("ClarificationState JSON 嵌套过深")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("ClarificationState JSON 数字必须有限")
        return value
    if isinstance(value, list):
        if len(value) > 100:
            raise ValueError("ClarificationState JSON 数组过长")
        return [_strict_json_value(item, depth=depth + 1) for item in value]
    if isinstance(value, Mapping):
        if len(value) > 100:
            raise ValueError("ClarificationState JSON 对象过大")
        output: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 200:
                raise ValueError("ClarificationState JSON 字段名无效")
            output[key] = _strict_json_value(item, depth=depth + 1)
        return output
    raise TypeError("ClarificationState 只接受严格 JSON 值")
