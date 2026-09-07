"""Physical model Provider-attempt admission shared by Runtime and Providers."""

from __future__ import annotations

import asyncio
import contextvars
import math
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any


class ModelAttemptBudgetExceeded(RuntimeError):
    """A physical Provider attempt was rejected before dispatch."""

    code = "model_attempt_budget_exceeded"


@dataclass(frozen=True, slots=True)
class ModelAttemptIdentity:
    """Stable identity for one physical Provider attempt in a model stage."""

    run_id: str
    stage: str
    reservation_id: str
    attempt: int

    @property
    def attempt_id(self) -> str:
        return (
            f"{self.run_id}:{self.stage}:{self.reservation_id}:"
            f"attempt:{self.attempt}"
        )


@dataclass(frozen=True, slots=True)
class ModelAttemptAdmissionSnapshot:
    """Immutable aggregate observed at the raw Provider boundary."""

    run_id: str
    stage: str
    reservation_id: str
    model_calls: int
    tokens: int
    cost: float
    unknown_attempts: int
    in_flight_attempts: int
    attempt_ids: tuple[str, ...]


class ModelAttemptAdmissionScope:
    """Concurrency-safe admission for every physical call in one retry tree."""

    def __init__(
        self,
        *,
        run_id: str,
        stage: str,
        reservation_id: str,
        max_model_calls: int,
        max_tokens: int | None = None,
        max_cost: float | None = None,
    ) -> None:
        self.run_id = _required_text(run_id, "run_id")
        self.stage = _required_text(stage, "stage")
        self.reservation_id = _required_text(reservation_id, "reservation_id")
        if (
            isinstance(max_model_calls, bool)
            or not isinstance(max_model_calls, int)
            or max_model_calls < 1
        ):
            raise ValueError("max_model_calls must be a positive integer")
        if max_tokens is not None and (
            isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            or max_tokens < 1
        ):
            raise ValueError("max_tokens must be a positive integer or None")
        if max_cost is not None and (
            isinstance(max_cost, bool)
            or not isinstance(max_cost, (int, float))
            or not math.isfinite(float(max_cost))
            or max_cost <= 0
        ):
            raise ValueError("max_cost must be a finite positive number or None")
        self.max_model_calls = max_model_calls
        self.max_tokens = max_tokens
        self.max_cost = None if max_cost is None else float(max_cost)
        self._lock = asyncio.Lock()
        self._active = False
        self._closed = False
        self._attempt_count = 0
        self._tokens = 0
        self._cost = 0.0
        self._unknown_attempts = 0
        self._in_flight: set[str] = set()
        self._attempt_ids: list[str] = []

    async def _activate(self) -> None:
        async with self._lock:
            if self._active or self._closed:
                raise RuntimeError("Model Attempt Admission Scope cannot be reused")
            self._active = True

    async def _close(self) -> None:
        async with self._lock:
            self._active = False
            self._closed = True

    async def begin_attempt(self) -> ModelAttemptIdentity:
        """Atomically admit an attempt before the raw StreamFn is invoked."""

        async with self._lock:
            if not self._active or self._closed:
                raise ModelAttemptBudgetExceeded(
                    "Model Attempt Admission Scope is not active"
                )
            if self._unknown_attempts:
                raise ModelAttemptBudgetExceeded("previous model usage is unknown")
            if self._attempt_count >= self.max_model_calls:
                raise ModelAttemptBudgetExceeded(
                    "physical model-call budget is exhausted"
                )
            if self.max_tokens is not None and self._tokens >= self.max_tokens:
                raise ModelAttemptBudgetExceeded(
                    "physical model token budget is exhausted"
                )
            if self.max_cost is not None and self._cost + 1e-12 >= self.max_cost:
                raise ModelAttemptBudgetExceeded(
                    "physical model cost budget is exhausted"
                )
            attempt = self._attempt_count + 1
            identity = ModelAttemptIdentity(
                self.run_id,
                self.stage,
                self.reservation_id,
                attempt,
            )
            self._attempt_count = attempt
            self._attempt_ids.append(identity.attempt_id)
            self._in_flight.add(identity.attempt_id)
            return identity

    async def finish_attempt(
        self,
        identity: ModelAttemptIdentity,
        *,
        tokens: int = 0,
        cost: float = 0.0,
        usage_unknown: bool = False,
    ) -> None:
        """Settle reported usage for success and failure terminal messages."""

        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            raise ValueError("attempt tokens must be a non-negative integer")
        if (
            isinstance(cost, bool)
            or not isinstance(cost, (int, float))
            or not math.isfinite(float(cost))
            or cost < 0
        ):
            raise ValueError("attempt cost must be a finite non-negative number")
        async with self._lock:
            if identity.attempt_id not in self._in_flight:
                raise RuntimeError("Model attempt is not active in this scope")
            self._in_flight.remove(identity.attempt_id)
            self._tokens += tokens
            self._cost += float(cost)
            if usage_unknown:
                self._unknown_attempts += 1
            if self.max_tokens is not None and self._tokens > self.max_tokens:
                raise ModelAttemptBudgetExceeded(
                    "physical model token usage exceeded its reserved upper bound"
                )
            if self.max_cost is not None and self._cost > self.max_cost + 1e-12:
                raise ModelAttemptBudgetExceeded(
                    "physical model cost exceeded its reserved upper bound"
                )

    async def snapshot(self) -> ModelAttemptAdmissionSnapshot:
        async with self._lock:
            return ModelAttemptAdmissionSnapshot(
                self.run_id,
                self.stage,
                self.reservation_id,
                self._attempt_count,
                self._tokens,
                self._cost,
                self._unknown_attempts,
                len(self._in_flight),
                tuple(self._attempt_ids),
            )


_MODEL_ATTEMPT_SCOPE: contextvars.ContextVar[
    ModelAttemptAdmissionScope | None
] = contextvars.ContextVar("pi_model_attempt_admission_scope", default=None)


def current_model_attempt_admission_scope() -> ModelAttemptAdmissionScope | None:
    return _MODEL_ATTEMPT_SCOPE.get()


@asynccontextmanager
async def activate_model_attempt_admission(
    scope: ModelAttemptAdmissionScope,
) -> AsyncIterator[ModelAttemptAdmissionScope]:
    """Activate one explicit retry-tree reservation for nested model calls."""

    if not isinstance(scope, ModelAttemptAdmissionScope):
        raise TypeError("scope must be ModelAttemptAdmissionScope")
    if _MODEL_ATTEMPT_SCOPE.get() is not None:
        raise RuntimeError("nested Model Attempt Admission scopes are forbidden")
    await scope._activate()
    context_token = _MODEL_ATTEMPT_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _MODEL_ATTEMPT_SCOPE.reset(context_token)
        await scope._close()


def model_attempt_usage_known(message: Mapping[str, Any]) -> bool:
    """Explicitly missing usage must never settle an admission as free."""
    if message.get("usageObserved") is False:
        return False
    usage = message.get("usage")
    if not isinstance(usage, Mapping):
        return False
    values = (
        [usage.get("totalTokens")]
        if "totalTokens" in usage
        else [usage.get("input"), usage.get("output")]
    )
    return all(type(value) is int and value >= 0 for value in values)


def model_attempt_usage(message: Mapping[str, Any]) -> tuple[int, float]:
    usage = message.get("usage")
    if not isinstance(usage, Mapping):
        return 0, 0.0
    total = usage.get("totalTokens")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        token_values = tuple(usage.get(name, 0) for name in ("input", "output"))
        total = sum(
            value
            for value in token_values
            if isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
        )
    cost_value = usage.get("cost")
    cost = cost_value.get("total", 0.0) if isinstance(cost_value, Mapping) else 0.0
    if (
        isinstance(cost, bool)
        or not isinstance(cost, (int, float))
        or not math.isfinite(float(cost))
        or cost < 0
    ):
        cost = 0.0
    return total, float(cost)


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


__all__ = [
    "ModelAttemptAdmissionScope",
    "ModelAttemptAdmissionSnapshot",
    "ModelAttemptBudgetExceeded",
    "ModelAttemptIdentity",
    "activate_model_attempt_admission",
    "current_model_attempt_admission_scope",
    "model_attempt_usage",
]
