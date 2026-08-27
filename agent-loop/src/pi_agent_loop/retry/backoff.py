"""指数退避、Jitter、Retry-After 和可取消等待。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from ..cancellation import CancellationToken, OperationCancelledError


def retry_delay_seconds(
    *,
    retry_number: int,
    initial_delay_seconds: float,
    max_delay_seconds: float,
    jitter_ratio: float,
    random: Callable[[], float],
    retry_after_seconds: float | None = None,
) -> float | None:
    """计算第 N 次重试等待；服务端等待超过上限时返回 None。"""

    if retry_after_seconds is not None:
        if retry_after_seconds > max_delay_seconds:
            return None
        return max(0.0, retry_after_seconds)
    exponential = min(
        initial_delay_seconds * (2 ** max(0, retry_number - 1)),
        max_delay_seconds,
    )
    sample = min(1.0, max(0.0, random()))
    jitter = 1 - jitter_ratio + (2 * jitter_ratio * sample)
    return min(exponential * jitter, max_delay_seconds)


async def cancellable_sleep(
    delay_seconds: float,
    cancellation: CancellationToken | None,
) -> None:
    """等待 Backoff；用户取消时立即结束，不遗留 Timer Task。"""

    if cancellation is None:
        await asyncio.sleep(delay_seconds)
        return
    cancellation.throw_if_cancelled()
    sleep_task = asyncio.create_task(asyncio.sleep(delay_seconds))
    cancel_task = asyncio.create_task(cancellation.wait())
    try:
        done, _ = await asyncio.wait(
            {sleep_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_task in done and sleep_task not in done:
            raise OperationCancelledError(cancellation.reason)
    finally:
        for task in (sleep_task, cancel_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(sleep_task, cancel_task, return_exceptions=True)
