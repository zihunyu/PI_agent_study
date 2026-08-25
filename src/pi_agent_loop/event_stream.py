"""异步事件流。

Pi 的 TypeScript EventStream 同时支持：

1. ``async for`` 持续消费中间事件；
2. ``await stream.result()`` 获取最终结果。

本实现保留这两个特点，并额外提供 ``fail()``。这样低层后台任务若出现
契约外异常，Python 调用者会得到明确异常，而不是永远等待最终结果。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Generic, TypeVar, cast

TEvent = TypeVar("TEvent")
TResult = TypeVar("TResult")

# 使用唯一对象作为队列结束标记，避免和合法事件冲突。
_END = object()


class EventStream(Generic[TEvent, TResult]):
    """既可异步迭代、又能返回最终结果的事件流。"""

    def __init__(
        self,
        is_complete: Callable[[TEvent], bool],
        extract_result: Callable[[TEvent], TResult],
    ) -> None:
        self._queue: asyncio.Queue[TEvent | object] = asyncio.Queue()
        self._is_complete = is_complete
        self._extract_result = extract_result
        self._done = False
        self._result: asyncio.Future[TResult] = (
            asyncio.get_running_loop().create_future()
        )

    def push(self, event: TEvent) -> None:
        """推送事件。

        与 Pi 原实现一样，这里使用无界内存队列。生产系统如果存在不可信的
        高速生产者，应在外层加入限流、合并或有界队列策略。
        """

        if self._done:
            return
        if self._is_complete(event):
            self._done = True
            if not self._result.done():
                self._result.set_result(self._extract_result(event))
        self._queue.put_nowait(event)
        if self._done:
            self._queue.put_nowait(_END)

    def end(self, result: TResult | None = None) -> None:
        """结束事件流；通常 terminal event 已经自动结束流。"""

        if self._done:
            return
        self._done = True
        if result is not None and not self._result.done():
            self._result.set_result(result)
        self._queue.put_nowait(_END)

    def fail(self, error: BaseException) -> None:
        """让事件流以异常结束。"""

        if self._done:
            return
        self._done = True
        if not self._result.done():
            self._result.set_exception(error)
            # 即使调用者只使用 async for，不调用 result()，也要取走 Future 异常，
            # 防止 asyncio 输出“Future exception was never retrieved”。
            self._result.add_done_callback(lambda future: future.exception())
        self._queue.put_nowait(_END)

    async def result(self) -> TResult:
        """等待并返回 terminal event 对应的最终结果。"""

        return await self._result

    def __aiter__(self) -> AsyncIterator[TEvent]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[TEvent]:
        while True:
            item = await self._queue.get()
            if item is _END:
                return
            yield cast(TEvent, item)


class AssistantMessageEventStream(EventStream[dict, dict]):
    """assistant 流的专用 EventStream。

    ``done`` 事件的最终值来自 ``message``；``error`` 事件来自 ``error``。
    """

    def __init__(self) -> None:
        super().__init__(
            lambda event: event.get("type") in {"done", "error"},
            lambda event: event["message"]
            if event.get("type") == "done"
            else event["error"],
        )


class AgentEventStream(EventStream[dict, list[dict]]):
    """低层 Agent Loop 的事件流。"""

    def __init__(self) -> None:
        super().__init__(
            lambda event: event.get("type") == "agent_end",
            lambda event: event["messages"],
        )
