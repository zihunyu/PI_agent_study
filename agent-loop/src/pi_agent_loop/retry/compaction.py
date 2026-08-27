"""Context Overflow 时压缩消息并在同一逻辑 Turn 重试一次。"""

from __future__ import annotations

import asyncio
import copy
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from ..event_stream import AssistantMessageEventStream
from ..types import Model, StreamFn


@dataclass(frozen=True, slots=True)
class CompactionRetryPolicy:
    enabled: bool = True
    max_retries: int = 1
    keep_recent_messages: int = 20

    def __post_init__(self) -> None:
        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int) or self.max_retries < 1:
            raise ValueError("compaction max_retries 必须是大于 0 的整数")
        if isinstance(self.keep_recent_messages, bool) or not isinstance(self.keep_recent_messages, int) or self.keep_recent_messages < 1:
            raise ValueError("keep_recent_messages 必须是大于 0 的整数")


class SlidingWindowCompactor:
    """教学用滑动窗口；生产系统应替换为 token-aware 摘要压缩。"""

    def __init__(self, keep_recent_messages: int) -> None:
        self.keep_recent_messages = keep_recent_messages

    async def __call__(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(messages) <= self.keep_recent_messages:
            return list(messages)
        return copy.deepcopy(messages[-self.keep_recent_messages :])


class ContextOverflowCompactingStreamFn:
    def __init__(
        self,
        stream_fn: StreamFn,
        policy: CompactionRetryPolicy,
        *,
        compactor: Callable[
            [list[dict[str, Any]]],
            Awaitable[list[dict[str, Any]]],
        ] | None = None,
    ) -> None:
        self.stream_fn = stream_fn
        self.policy = policy
        self.compactor = compactor or SlidingWindowCompactor(
            policy.keep_recent_messages
        )

    def __call__(self, model: Model, context: dict[str, Any], options: dict[str, Any]) -> Any:
        if not self.policy.enabled:
            return self.stream_fn(model, context, options)
        output = AssistantMessageEventStream()
        asyncio.create_task(self._run(output, model, context, options))
        return output

    async def _run(
        self,
        output: AssistantMessageEventStream,
        model: Model,
        context: dict[str, Any],
        options: dict[str, Any],
    ) -> None:
        working = copy.deepcopy(context)
        retries = 0
        try:
            while True:
                events, final = await _consume(
                    self.stream_fn,
                    model,
                    working,
                    options,
                )
                if not _is_context_overflow(final) or retries >= self.policy.max_retries:
                    for event in events:
                        output.push(event)
                    return
                retries += 1
                old_messages = list(working.get("messages", []))
                compacted = await self.compactor(old_messages)
                if len(compacted) >= len(old_messages):
                    for event in events:
                        output.push(event)
                    return
                await self._emit_event(output, options, {
                    "type": "context_compaction_started",
                    "kind": "model",
                    "attempt": retries,
                    "beforeMessages": len(old_messages),
                })
                working["messages"] = compacted
                await self._emit_event(output, options, {
                    "type": "context_compaction_finished",
                    "kind": "model",
                    "attempt": retries,
                    "beforeMessages": len(old_messages),
                    "afterMessages": len(compacted),
                })
        except BaseException as error:
            output.fail(error)

    async def _emit_event(
        self,
        output: AssistantMessageEventStream,
        options: dict[str, Any],
        event: dict[str, Any],
    ) -> None:
        sink = options.get("retry_event_sink")
        if callable(sink):
            value = sink(dict(event))
            if inspect.isawaitable(value):
                await cast(Awaitable[Any], value)
        output.push(event)


async def _consume(
    stream_fn: StreamFn,
    model: Model,
    context: dict[str, Any],
    options: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    value = stream_fn(model, context, options)
    stream = await cast(Awaitable[Any], value) if inspect.isawaitable(value) else value
    events = [event async for event in stream]
    final = await stream.result()
    return events, final


def _is_context_overflow(message: dict[str, Any]) -> bool:
    details = message.get("providerError")
    if isinstance(details, dict) and details.get("code") in {
        "context_overflow",
        "context_length_exceeded",
    }:
        return True
    text = str(message.get("errorMessage", "")).casefold()
    return "context overflow" in text or "context length" in text


def compact_on_context_overflow(
    stream_fn: StreamFn,
    policy: CompactionRetryPolicy,
    *,
    compactor: Callable[
        [list[dict[str, Any]]], Awaitable[list[dict[str, Any]]]
    ] | None = None,
) -> StreamFn:
    return cast(StreamFn, ContextOverflowCompactingStreamFn(stream_fn, policy, compactor=compactor))
