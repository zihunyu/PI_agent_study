"""Bound callback waits without pretending Python can kill arbitrary callbacks."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import inspect
import threading
from collections.abc import Callable
from typing import Any

from .cancellation import CancellationToken

_Completion = asyncio.Future[Any] | concurrent.futures.Future[Any]
_CLEANUP_SECONDS = 0.05


def _consume_error(future: _Completion) -> None:
    try:
        future.exception()
    except (asyncio.CancelledError, concurrent.futures.CancelledError):
        pass


class ContextTransformState:
    """Quarantine callbacks that outlive cancellation, across policy users."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stragglers: set[_Completion] = set()

    def require_available(self) -> None:
        with self._lock:
            if any(not future.done() for future in self._stragglers):
                raise RuntimeError(
                    "context transform is still running after cancellation"
                )

    def track(self, future: _Completion) -> None:
        with self._lock:
            self._stragglers.add(future)

        def settled(completed: _Completion) -> None:
            with self._lock:
                self._stragglers.discard(completed)
            _consume_error(completed)

        future.add_done_callback(settled)


class _Invocation:
    def __init__(self) -> None:
        self.sync: concurrent.futures.Future[Any] | None = None
        self.abandoned = False
        self.claimed = False

    def discard_late_result(self, future: concurrent.futures.Future[Any]) -> None:
        # A legacy sync wrapper may return a coroutine. If the request already
        # timed out, close that unstarted coroutine rather than running it later.
        if self.abandoned and not self.claimed and future.done():
            try:
                value = future.result()
            except BaseException:
                return
            if inspect.iscoroutine(value):
                value.close()

    async def run(
        self, callback: Callable[..., Any], arguments: tuple[Any, ...]
    ) -> Any:
        if inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(
            getattr(callback, "__call__", None)
        ):
            value = callback(*arguments)
        else:
            completion: concurrent.futures.Future[Any] = concurrent.futures.Future()
            self.sync = completion
            completion.add_done_callback(self.discard_late_result)
            context = contextvars.copy_context()

            def run_sync() -> None:
                try:
                    completion.set_result(context.run(callback, *arguments))
                except BaseException as error:
                    completion.set_exception(error)

            # An uncooperative legacy callback must not block the event loop or
            # the loop's default-executor shutdown. Its completion remains owned
            # by the policy quarantine until this daemon worker actually exits.
            threading.Thread(
                target=run_sync, name="pi-context-sync", daemon=True
            ).start()
            wrapped = asyncio.wrap_future(completion)
            wrapped.add_done_callback(_consume_error)
            value = await asyncio.shield(wrapped)
            self.claimed = True
        return await value if inspect.isawaitable(value) else value


async def run_context_transform(
    callback: Callable[..., Any],
    arguments: tuple[Any, ...],
    cancellation: CancellationToken,
    *,
    timeout: float,
    state: ContextTransformState,
) -> Any:
    state.require_available()
    invocation = _Invocation()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    work = asyncio.create_task(
        invocation.run(callback, arguments), name="pi-context-transform"
    )
    work.add_done_callback(_consume_error)
    waiter = asyncio.create_task(cancellation.wait(), name="pi-context-cancellation")
    succeeded = False
    try:
        done, _ = await asyncio.wait(
            {work, waiter}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        cancellation.throw_if_cancelled()
        if work not in done or loop.time() >= deadline:
            raise TimeoutError("context transform timed out")
        result = work.result()
        succeeded = True
        return result
    finally:
        if not succeeded:
            cancellation.cancel("context transform interrupted")
            invocation.abandoned = True
            if invocation.sync is not None:
                state.track(invocation.sync)
                invocation.discard_late_result(invocation.sync)
            state.track(work)
            if not work.done():
                work.cancel()
        if not waiter.done():
            waiter.cancel()
        # asyncio.wait_for/gather would let cancellation-resistant callbacks
        # extend this deadline indefinitely. Cooperative tasks still get drained.
        await asyncio.wait({work, waiter}, timeout=_CLEANUP_SECONDS)
