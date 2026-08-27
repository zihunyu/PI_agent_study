"""幂等业务 Task/Workflow 的有界重试执行器。"""

from __future__ import annotations

import inspect
import math
import random as random_module
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar, cast
from uuid import uuid4

from ..cancellation import CancellationToken
from .backoff import cancellable_sleep, retry_delay_seconds
from .events import RetryEventStore

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class TaskRetryPolicy:
    max_retries: int
    retryable_codes: frozenset[str]
    idempotent: bool
    initial_delay_seconds: float = 0.5
    max_delay_seconds: float = 10.0
    jitter_ratio: float = 0.2
    max_elapsed_seconds: float = 60.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_retries, bool)
            or not isinstance(self.max_retries, int)
            or self.max_retries < 1
        ):
            raise ValueError("task max_retries 必须是大于 0 的整数")
        if not self.idempotent:
            raise ValueError("自动 Task Retry 只允许幂等任务")
        if not self.retryable_codes:
            raise ValueError("task retryable_codes 不能为空")
        if (
            not math.isfinite(self.initial_delay_seconds)
            or not math.isfinite(self.max_delay_seconds)
            or self.initial_delay_seconds < 0
            or self.max_delay_seconds <= 0
        ):
            raise ValueError("task retry delay 配置无效")
        if self.initial_delay_seconds > self.max_delay_seconds:
            raise ValueError("task initial delay 不能超过 max delay")
        if not math.isfinite(self.jitter_ratio) or not 0 <= self.jitter_ratio <= 1:
            raise ValueError("task jitter_ratio 必须在 0 到 1 之间")
        if not math.isfinite(self.max_elapsed_seconds) or self.max_elapsed_seconds <= 0:
            raise ValueError("task max_elapsed_seconds 必须大于 0")


class RetryableTaskError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        if not code.strip():
            raise ValueError("RetryableTaskError code 不能为空")
        if retry_after_seconds is not None and retry_after_seconds < 0:
            raise ValueError("retry_after_seconds 不能小于 0")
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        self.attempts = 1
        self.retry_id: str | None = None


class TaskRetryExecutor:
    """执行一个可由 Host 重建的幂等 Task；不负责业务规划。"""

    def __init__(
        self,
        policy: TaskRetryPolicy,
        *,
        event_store: RetryEventStore | None = None,
        event_sink: Callable[[dict[str, Any]], Any] | None = None,
        random: Callable[[], float] | None = None,
    ) -> None:
        self.policy = policy
        self.event_store = event_store
        self.event_sink = event_sink
        self.random = random or random_module.random

    async def execute(
        self,
        task_id: str,
        operation: Callable[[], Awaitable[T]],
        cancellation: CancellationToken,
    ) -> T:
        retry_id = str(uuid4())
        retries = 0
        started_at = time.monotonic()
        while True:
            cancellation.throw_if_cancelled()
            try:
                result = await operation()
                if retries:
                    await self._emit({
                        "type": "task_retry_finished",
                        "kind": "task",
                        "retryId": retry_id,
                        "taskId": task_id,
                        "attempt": retries,
                        "success": True,
                    })
                return result
            except RetryableTaskError as error:
                if error.code not in self.policy.retryable_codes or retries >= self.policy.max_retries:
                    error.attempts = retries + 1
                    error.retry_id = retry_id if retries else None
                    if retries:
                        await self._emit({
                            "type": "task_retry_finished",
                            "kind": "task",
                            "retryId": retry_id,
                            "taskId": task_id,
                            "attempt": retries,
                            "success": False,
                            "finalError": error.code,
                        })
                    raise
                retry_number = retries + 1
                delay = retry_delay_seconds(
                    retry_number=retry_number,
                    initial_delay_seconds=self.policy.initial_delay_seconds,
                    max_delay_seconds=self.policy.max_delay_seconds,
                    jitter_ratio=self.policy.jitter_ratio,
                    random=self.random,
                    retry_after_seconds=error.retry_after_seconds,
                )
                elapsed = time.monotonic() - started_at
                if delay is None or elapsed + delay > self.policy.max_elapsed_seconds:
                    error.attempts = retries + 1
                    error.retry_id = retry_id if retries else None
                    raise
                retries = retry_number
                await self._emit({
                    "type": "task_retry_scheduled",
                    "kind": "task",
                    "retryId": retry_id,
                    "taskId": task_id,
                    "attempt": retries,
                    "maxAttempts": self.policy.max_retries,
                    "delayMs": round(delay * 1000),
                    "errorCode": error.code,
                })
                await cancellable_sleep(delay, cancellation)
                await self._emit({
                    "type": "task_retry_attempt_start",
                    "kind": "task",
                    "retryId": retry_id,
                    "taskId": task_id,
                    "attempt": retries,
                })

    async def _emit(self, event: dict[str, Any]) -> None:
        if self.event_sink is not None:
            value = self.event_sink(dict(event))
            if inspect.isawaitable(value):
                await cast(Awaitable[Any], value)
        if self.event_store is not None:
            await self.event_store.append(event)
