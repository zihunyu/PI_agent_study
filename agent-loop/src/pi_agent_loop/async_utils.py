"""Async helpers for durable synchronous boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")


async def durable_to_thread(
    func: Callable[P, T],
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """Run ``func`` in a thread and never abandon it at cancellation.

    ``asyncio.to_thread`` cannot stop a synchronous function once it has
    started.  Letting cancellation escape immediately would therefore tell a
    caller (and, in particular, a Host ``close()``) that a durable operation
    has settled while the worker can still mutate disk state.  This helper
    shields and drains the worker through repeated caller cancellation, then
    delivers the cancellation only after the synchronous boundary is final.
    """

    worker = asyncio.create_task(
        asyncio.to_thread(func, *args, **kwargs),
        name="pi-durable-to-thread",
    )
    cancellation: asyncio.CancelledError | None = None
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError as error:
            # A task may be cancelled repeatedly while cleanup is in flight.
            # Keep the first reason and continue draining the same worker.
            if cancellation is None:
                cancellation = error

    try:
        result = worker.result()
    except BaseException as worker_error:
        if cancellation is not None:
            raise BaseExceptionGroup(
                "Durable thread work failed after caller cancellation",
                [cancellation, worker_error],
            )
        raise
    if cancellation is not None:
        raise cancellation
    return result


__all__ = ["durable_to_thread"]
