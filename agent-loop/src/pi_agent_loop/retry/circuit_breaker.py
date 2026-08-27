"""Provider Circuit Breaker：连续瞬时失败时快速失败并支持半开探测。"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class CircuitBreakerPolicy:
    enabled: bool = False
    failure_threshold: int = 3
    recovery_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("circuit breaker enabled 必须是布尔值")
        if (
            isinstance(self.failure_threshold, bool)
            or not isinstance(self.failure_threshold, int)
            or self.failure_threshold < 1
        ):
            raise ValueError("failure_threshold 必须是大于 0 的整数")
        if (
            isinstance(self.recovery_timeout_seconds, bool)
            or not isinstance(self.recovery_timeout_seconds, (int, float))
            or not math.isfinite(self.recovery_timeout_seconds)
            or self.recovery_timeout_seconds <= 0
        ):
            raise ValueError("recovery_timeout_seconds 必须大于 0")


class CircuitOpenError(Exception):
    """Circuit 仍处于 Open，当前 Attempt 不应访问 Provider。"""


class CircuitBreaker:
    def __init__(
        self,
        policy: CircuitBreakerPolicy,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.policy = policy
        self._clock = clock or time.monotonic
        self._state: Literal["closed", "open", "half_open"] = "closed"
        self._failures = 0
        self._opened_at = 0.0
        self._half_open_in_flight = False
        self._lock = asyncio.Lock()

    @property
    def state(self) -> str:
        return self._state

    async def before_call(self) -> None:
        if not self.policy.enabled:
            return
        async with self._lock:
            if self._state == "closed":
                return
            if self._state == "open":
                elapsed = self._clock() - self._opened_at
                if elapsed < self.policy.recovery_timeout_seconds:
                    raise CircuitOpenError("Provider Circuit Breaker 处于 Open")
                self._state = "half_open"
                self._half_open_in_flight = False
            if self._half_open_in_flight:
                raise CircuitOpenError("Provider Circuit Breaker 正在半开探测")
            self._half_open_in_flight = True

    async def record_success(self) -> None:
        if not self.policy.enabled:
            return
        async with self._lock:
            self._state = "closed"
            self._failures = 0
            self._half_open_in_flight = False

    async def record_failure(self) -> None:
        if not self.policy.enabled:
            return
        async with self._lock:
            self._half_open_in_flight = False
            self._failures += 1
            if (
                self._state == "half_open"
                or self._failures >= self.policy.failure_threshold
            ):
                self._state = "open"
                self._opened_at = self._clock()
