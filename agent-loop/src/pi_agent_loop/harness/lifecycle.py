"""Durable Host 启停边界。"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any


class DurableHostClosedError(RuntimeError):
    """Host has stopped accepting new work."""


class DurableHostLifecycle:
    def __init__(self, agent: Any, resources: Any) -> None:
        self.agent = agent
        self.resources = resources
        self.closed = False
        self._accepting = True
        self._active_tasks: dict[asyncio.Task[Any], int] = {}
        self._state_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None

    @property
    def accepting(self) -> bool:
        return self._accepting and not self.closed

    @property
    def active_operation_count(self) -> int:
        return len(self._active_tasks)

    async def run(self, operation: Callable[[], Any]) -> Any:
        """Admit one public Host operation and keep it visible to ``close``."""

        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("DurableAgentHost 操作必须在 asyncio Task 中运行")
        async with self._state_lock:
            if not self._accepting or self.closed:
                raise DurableHostClosedError("DurableAgentHost 已关闭或正在关闭")
            self._active_tasks[task] = self._active_tasks.get(task, 0) + 1
        try:
            value = operation()
            return await value if inspect.isawaitable(value) else value
        finally:
            async with self._state_lock:
                depth = self._active_tasks.get(task, 0)
                if depth <= 1:
                    self._active_tasks.pop(task, None)
                else:
                    self._active_tasks[task] = depth - 1

    async def close(self) -> None:
        caller = asyncio.current_task()
        # Check on every public close() call, including callers that would
        # otherwise reuse an already-running canonical close task. An admitted
        # operation waiting for that task would deadlock because _close_once is
        # simultaneously waiting for the operation to settle.
        if caller in self._active_tasks:
            raise RuntimeError("不能在 DurableAgentHost 活动操作内关闭 Host")
        close_task = self._close_task
        if close_task is None or close_task.done():
            close_task = asyncio.create_task(
                self._close_once(caller),
                name="pi-durable-host-close",
            )
            self._close_task = close_task

        cancellation: asyncio.CancelledError | None = None
        while not close_task.done():
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError as error:
                # Closing is a commit boundary. Keep waiting for the same
                # canonical close task even when the caller is cancelled more
                # than once; shield prevents either cancellation from reaching
                # resource cleanup.
                cancellation = error
        # Reading a completed task through result() cannot itself be cancelled.
        # Preserve cleanup failures, otherwise deliver the caller cancellation
        # only after the Host has reached its terminal closed state.
        close_task.result()
        if cancellation is not None:
            raise cancellation

    async def _close_once(self, caller: asyncio.Task[Any] | None) -> None:
        async with self._close_lock:
            if self.closed:
                return
            async with self._state_lock:
                if caller in self._active_tasks:
                    raise RuntimeError(
                        "不能在 DurableAgentHost 活动操作内关闭 Host"
                    )
                self._accepting = False
                active = tuple(self._active_tasks)

            for task in active:
                task.cancel("DurableAgentHost 正在关闭")
            errors: list[BaseException] = []
            if active:
                settled = await asyncio.gather(*active, return_exceptions=True)
                errors.extend(
                    error
                    for error in settled
                    if isinstance(error, BaseException)
                    and not isinstance(error, asyncio.CancelledError)
                )
            try:
                if self.agent.state.is_streaming:
                    self.agent.abort("DurableAgentHost 正在关闭")
                await self.agent.wait_for_idle()
            except BaseException as error:
                errors.append(error)
            try:
                await self.resources.close()
            except BaseException as error:
                errors.append(error)
            if errors:
                # Admission remains closed, but callers may retry close() so a
                # transient resource failure can be compensated.
                raise BaseExceptionGroup("DurableAgentHost 关闭失败", errors)
            self.closed = True


__all__ = ["DurableHostClosedError", "DurableHostLifecycle"]
