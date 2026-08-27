"""单个幂等逻辑 Tool Call 的失败 Attempt 重试包装。"""

from __future__ import annotations

import inspect
import random as random_module
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast
from uuid import uuid4

from ..cancellation import CancellationToken
from ..types import AgentToolResult
from .backoff import cancellable_sleep, retry_delay_seconds
from .errors import RetryableToolError
from .types import ToolRetryPolicy


async def execute_tool_with_retry(
    *,
    execute: Callable[[], Awaitable[AgentToolResult]],
    policy: ToolRetryPolicy | None,
    cancellation: CancellationToken,
    tool_call_id: str,
    tool_name: str,
    emit: Callable[[dict[str, Any]], Any],
    random: Callable[[], float] | None = None,
) -> AgentToolResult:
    """只重试当前逻辑 Tool Call，成功兄弟工具不会被重新执行。"""

    if policy is None:
        return await execute()
    sample = random or random_module.random
    retry_id = str(uuid4())
    retries = 0
    started_at = time.monotonic()

    while True:
        cancellation.throw_if_cancelled()
        try:
            result = await execute()
            if retries:
                await _maybe_await(
                    emit(
                        {
                            "type": "tool_retry_finished",
                            "retryId": retry_id,
                            "toolCallId": tool_call_id,
                            "toolName": tool_name,
                            "success": True,
                            "attempt": retries,
                        }
                    )
                )
            return result
        except RetryableToolError as error:
            can_retry = (
                error.code in policy.retryable_codes
                and retries < policy.max_retries
            )
            if not can_retry:
                error.attempts = retries + 1
                error.retry_id = retry_id if retries else None
                if retries:
                    await _maybe_await(
                        emit(
                            {
                                "type": "tool_retry_finished",
                                "retryId": retry_id,
                                "toolCallId": tool_call_id,
                                "toolName": tool_name,
                                "success": False,
                                "attempt": retries,
                                "finalError": error.code,
                            }
                        )
                    )
                raise

            retry_number = retries + 1
            delay = retry_delay_seconds(
                retry_number=retry_number,
                initial_delay_seconds=policy.initial_delay_seconds,
                max_delay_seconds=policy.max_delay_seconds,
                jitter_ratio=policy.jitter_ratio,
                random=sample,
                retry_after_seconds=error.retry_after_seconds,
            )
            elapsed = time.monotonic() - started_at
            if (
                delay is None
                or elapsed + delay > policy.max_elapsed_seconds
            ):
                error.attempts = retries + 1
                error.retry_id = retry_id if retries else None
                raise

            retries = retry_number
            await _maybe_await(
                emit(
                    {
                        "type": "tool_retry_scheduled",
                        "retryId": retry_id,
                        "toolCallId": tool_call_id,
                        "toolName": tool_name,
                        "attempt": retries,
                        "maxAttempts": policy.max_retries,
                        "delayMs": round(delay * 1000),
                        "errorCode": error.code,
                    }
                )
            )
            await cancellable_sleep(delay, cancellation)
            await _maybe_await(
                emit(
                    {
                        "type": "tool_retry_attempt_start",
                        "retryId": retry_id,
                        "toolCallId": tool_call_id,
                        "toolName": tool_name,
                        "attempt": retries,
                    }
                )
            )


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await cast(Awaitable[Any], value)
    return value
