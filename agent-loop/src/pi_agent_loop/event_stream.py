"""Bounded asynchronous event streams with synchronous producer semantics.

The public ``push()`` API intentionally remains synchronous because providers and
tools emit updates from callbacks that cannot await. Backpressure is therefore
implemented with a bounded buffer, update coalescing and an explicit slow-consumer
policy instead of an unbounded ``asyncio.Queue``.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar, cast

TEvent = TypeVar("TEvent")
TResult = TypeVar("TResult")

SlowConsumerPolicy = Literal["drop_oldest", "error"]

# A unique marker cannot collide with a legitimate event.
_END = object()


class EventStreamBackpressureError(RuntimeError):
    """Raised when the configured slow-consumer policy is ``error``."""


@dataclass(frozen=True, slots=True)
class EventStreamStats:
    """A point-in-time, allocation-safe view of stream buffer pressure."""

    queued_events: int
    queued_bytes: int
    high_watermark_events: int
    high_watermark_bytes: int
    dropped_events: int
    coalesced_events: int


@dataclass(slots=True)
class _QueuedEvent(Generic[TEvent]):
    value: TEvent
    size: int
    coalesce_key: object | None = None
    droppable: bool = True


def _estimate_size(value: object) -> int:
    """Estimate retained bytes without serializing values into diagnostics."""

    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            default=repr,
        )
    except (TypeError, ValueError, OverflowError):
        rendered = repr(value)
    return len(rendered.encode("utf-8", errors="replace"))


def _default_event_key(event: Any) -> object | None:
    """Return a key only for updates whose newest state supersedes older state."""

    if not isinstance(event, dict):
        return None
    event_type = event.get("type")
    if event_type in {"text_delta", "thinking_delta", "toolcall_delta"}:
        return (event_type, event.get("contentIndex"))
    if event_type == "message_update":
        return event_type
    if event_type == "tool_execution_update":
        return (event_type, event.get("toolCallId"))
    return None


def _default_event_droppable(event: Any) -> bool:
    """Only high-frequency progress snapshots may be evicted.

    Lifecycle, Tool commit-boundary and terminal events are structural facts and
    must either be delivered or make the stream fail explicitly; they are never
    silently replaced by a newer UI update.
    """

    if not isinstance(event, dict):
        return False
    return event.get("type") in {
        "text_delta",
        "thinking_delta",
        "toolcall_delta",
        "message_update",
        "tool_execution_update",
    }


def _is_async_callable(callback: Callable[..., Any]) -> bool:
    return inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(
        getattr(callback, "__call__", None)
    )


def _merge_updates(previous: Any, current: Any) -> Any:
    """Merge delta events while retaining the newest authoritative partial."""

    if not isinstance(previous, dict) or not isinstance(current, dict):
        return current
    merged = copy.deepcopy(current)
    if (
        previous.get("type") == current.get("type")
        and current.get("type") in {"text_delta", "thinking_delta", "toolcall_delta"}
        and isinstance(previous.get("delta"), str)
        and isinstance(current.get("delta"), str)
    ):
        merged["delta"] = previous["delta"] + current["delta"]
    return merged


class EventStream(Generic[TEvent, TResult]):
    """An async iterator with a separately awaitable terminal result.

    ``max_buffer_size`` and ``max_buffer_bytes`` bound retained updates. Because
    ``push`` cannot await, ``drop_oldest`` keeps recent state when a consumer is
    slow. ``error`` instead terminates with an explicit backpressure error.

    Terminal events always win space over buffered updates. An individual event,
    including a terminal event, may not exceed ``max_buffer_bytes``; an oversized
    terminal fails the stream explicitly instead of silently breaking the memory
    bound or leaving consumers waiting forever.
    """

    def __init__(
        self,
        is_complete: Callable[[TEvent], bool],
        extract_result: Callable[[TEvent], TResult],
        *,
        max_buffer_size: int = 256,
        max_buffer_bytes: int = 4 * 1024 * 1024,
        slow_consumer_policy: SlowConsumerPolicy = "drop_oldest",
        coalesce_key: Callable[[TEvent], object | None] | None = None,
        merge_updates: Callable[[TEvent, TEvent], TEvent] | None = None,
        is_droppable: Callable[[TEvent], bool] | None = None,
        on_backpressure: Callable[[dict[str, Any]], Any] | None = None,
        on_queue_change: Callable[[EventStreamStats], Any] | None = None,
    ) -> None:
        if (
            isinstance(max_buffer_size, bool)
            or not isinstance(max_buffer_size, int)
            or max_buffer_size < 2
        ):
            raise ValueError("max_buffer_size must be an integer greater than one")
        if (
            isinstance(max_buffer_bytes, bool)
            or not isinstance(max_buffer_bytes, int)
            or max_buffer_bytes <= 0
        ):
            raise ValueError("max_buffer_bytes must be a positive integer")
        if slow_consumer_policy not in {"drop_oldest", "error"}:
            raise ValueError("slow_consumer_policy must be drop_oldest or error")

        # The extra slot belongs exclusively to the wake-up marker. It is not
        # counted as buffered event capacity, so error/close never has to evict
        # an already accepted structural event merely to wake the iterator.
        self._queue: asyncio.Queue[_QueuedEvent[TEvent] | object] = asyncio.Queue(
            maxsize=max_buffer_size + 1
        )
        self._is_complete = is_complete
        self._extract_result = extract_result
        self._max_buffer_size = max_buffer_size
        self._max_buffer_bytes = max_buffer_bytes
        self._slow_consumer_policy = slow_consumer_policy
        self._coalesce_key = coalesce_key
        self._merge_updates = merge_updates or (lambda _old, new: new)
        self._is_droppable = is_droppable or _default_event_droppable
        self._on_backpressure = on_backpressure
        self._on_queue_change = on_queue_change
        self._pending_by_key: dict[object, _QueuedEvent[TEvent]] = {}
        self._queued_event_count = 0
        self._queued_bytes = 0
        self._high_watermark_events = 0
        self._high_watermark_bytes = 0
        self._dropped_events = 0
        self._coalesced_events = 0
        self._done = False
        self._backpressure_task: asyncio.Task[None] | None = None
        self._pending_backpressure: dict[str, Any] | None = None
        self._backpressure_closed = False
        self._result: asyncio.Future[TResult] = (
            asyncio.get_running_loop().create_future()
        )

    @property
    def stats(self) -> EventStreamStats:
        return EventStreamStats(
            queued_events=self._queued_event_count,
            queued_bytes=self._queued_bytes,
            high_watermark_events=self._high_watermark_events,
            high_watermark_bytes=self._high_watermark_bytes,
            dropped_events=self._dropped_events,
            coalesced_events=self._coalesced_events,
        )

    def push(self, event: TEvent) -> None:
        """Push one event without blocking the producer."""

        if self._done:
            return
        try:
            self._push_open(event)
        except BaseException as caught:
            # Event classification and coalescing are extension points. A bad
            # provider event or user callback must fail-close both consumers;
            # otherwise async iteration/result() can wait forever.
            if not self._done:
                self._terminate_with_error(caught)
            raise

    def _push_open(self, event: TEvent) -> None:
        terminal = self._is_complete(event)
        terminal_result: TResult | None = None
        if terminal:
            try:
                terminal_result = self._extract_result(event)
            except BaseException as caught:
                # 即使 Provider 给出畸形终止事件，也必须唤醒 iterator/result；
                # 不能先置 done 再因 extractor 异常留下永久等待者。
                self._terminate_with_error(caught)
                raise

        key = self._coalesce_key(event) if self._coalesce_key is not None else None
        size = _estimate_size(event)
        droppable = bool(self._is_droppable(event)) and not terminal
        existing = self._pending_by_key.get(key) if key is not None else None
        pressure = self._would_overflow(size, terminal=terminal)
        # Clearing older events cannot make an individually oversized event fit.
        if size > self._max_buffer_bytes:
            error = EventStreamBackpressureError(
                "event exceeds the stream byte limit"
            )
            if terminal or not droppable or self._slow_consumer_policy == "error":
                self._terminate_with_error(error)
                raise error
            self._dropped_events += 1
            self._notify_backpressure("oversized_dropped")
            return
        if existing is not None and pressure and not terminal:
            merged = self._merge_updates(existing.value, event)
            merged_size = _estimate_size(merged)
            if merged_size > self._max_buffer_bytes:
                if self._slow_consumer_policy == "error":
                    error = EventStreamBackpressureError(
                        "coalesced event exceeds the stream byte limit"
                    )
                    self._terminate_with_error(error)
                    raise error
                # The current update itself fits. Replace the protected aggregate
                # with the newest authoritative update rather than allowing one
                # ever-growing delta to bypass the byte cap.
                self._queued_bytes += size - existing.size
                existing.value = event
                existing.size = size
                self._dropped_events += 1
                action = "coalesced_replaced"
            else:
                self._queued_bytes += merged_size - existing.size
                existing.value = merged
                existing.size = merged_size
                action = "coalesced"
            self._coalesced_events += 1
            self._notify_backpressure(action)
            self._trim_bytes(protected=existing)
            self._update_high_watermarks()
            self._notify_queue_change()
            return

        if pressure:
            if self._slow_consumer_policy == "error" and not terminal:
                error = EventStreamBackpressureError(
                    "event stream buffer exceeded its slow-consumer limit"
                )
                self._terminate_with_error(error)
                raise error
            made_room = self._make_room(size, terminal=terminal)
            if not made_room:
                if droppable and not terminal:
                    self._dropped_events += 1
                    self._notify_backpressure("incoming_update_dropped")
                    self._notify_queue_change()
                    return
                error = EventStreamBackpressureError(
                    "structural event buffer is full; no progress update can be evicted"
                )
                self._terminate_with_error(error)
                raise error

        if terminal:
            self._done = True
            if not self._result.done():
                self._result.set_result(cast(TResult, terminal_result))
        queued = _QueuedEvent(event, size, key, droppable)
        self._queue.put_nowait(queued)
        self._queued_event_count += 1
        self._queued_bytes += size
        if key is not None:
            self._pending_by_key[key] = queued
        self._update_high_watermarks()
        self._notify_queue_change()
        if terminal:
            self._queue.put_nowait(_END)
            self._close_backpressure_notifications()

    def end(self, result: TResult | None = None) -> None:
        """End a stream that does not use a terminal event."""

        if self._done:
            return
        self._done = True
        if not self._result.done():
            self._result.set_result(cast(TResult, result))
        self._queue.put_nowait(_END)
        self._notify_queue_change()
        self._close_backpressure_notifications()

    def fail(self, error: BaseException) -> None:
        """End the stream and make ``result()`` raise ``error``."""

        if self._done:
            return
        self._terminate_with_error(error)

    async def result(self) -> TResult:
        return await self._result

    def __aiter__(self) -> AsyncIterator[TEvent]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[TEvent]:
        try:
            while True:
                item = await self._queue.get()
                if item is _END:
                    return
                queued = cast(_QueuedEvent[TEvent], item)
                self._queued_event_count = max(0, self._queued_event_count - 1)
                self._queued_bytes = max(0, self._queued_bytes - queued.size)
                if (
                    queued.coalesce_key is not None
                    and self._pending_by_key.get(queued.coalesce_key) is queued
                ):
                    self._pending_by_key.pop(queued.coalesce_key, None)
                self._notify_queue_change()
                yield queued.value
        finally:
            self._close_backpressure_notifications()
            task = self._backpressure_task
            if task is not None and task is not asyncio.current_task():
                await asyncio.gather(task, return_exceptions=True)

    def _terminate_with_error(self, error: BaseException) -> None:
        self._done = True
        if not self._result.done():
            self._result.set_exception(error)
            self._result.add_done_callback(lambda future: future.exception())
        self._queue.put_nowait(_END)
        self._notify_backpressure("error")
        self._notify_queue_change()
        self._close_backpressure_notifications()

    def _would_overflow(self, incoming_size: int, *, terminal: bool) -> bool:
        # Terminal events get priority by evicting only droppable progress
        # updates. If the buffer contains structural facts exclusively, the
        # stream fails explicitly instead of silently deleting one of them.
        event_limit = self._max_buffer_size
        return (
            self._queued_event_count + 1 > event_limit
            or self._queued_bytes + incoming_size > self._max_buffer_bytes
        )

    def _make_room(self, incoming_size: int, *, terminal: bool) -> bool:
        while self._would_overflow(incoming_size, terminal=terminal):
            if not self._drop_oldest():
                return False
        return True

    def _trim_bytes(self, *, protected: _QueuedEvent[TEvent]) -> None:
        # If the single protected update is oversized, retain it as the newest
        # authoritative snapshot instead of emptying the stream.
        while self._queued_bytes > self._max_buffer_bytes and self._queued_event_count > 1:
            if not self._drop_oldest(protected=protected):
                break
        if self._queued_bytes > self._max_buffer_bytes and protected.droppable:
            self._remove_queued(protected)

    def _drop_oldest(self, *, protected: _QueuedEvent[TEvent] | None = None) -> bool:
        # asyncio.Queue does not support indexed removal. Drain and restore in
        # exactly the same order so protecting a commit event never reorders it.
        items: list[_QueuedEvent[TEvent] | object] = []
        while not self._queue.empty():
            items.append(self._queue.get_nowait())
        candidate: _QueuedEvent[TEvent] | None = None
        for item in items:
            if item is _END or item is protected:
                continue
            queued = cast(_QueuedEvent[TEvent], item)
            if queued.droppable:
                candidate = queued
                break
        for item in items:
            if item is not candidate:
                self._queue.put_nowait(item)
        if candidate is None:
            return False
        self._discard(candidate)
        return True

    def _remove_queued(self, target: _QueuedEvent[TEvent]) -> None:
        items: list[_QueuedEvent[TEvent] | object] = []
        removed = False
        while not self._queue.empty():
            item = self._queue.get_nowait()
            if item is target and not removed:
                removed = True
                continue
            items.append(item)
        for item in items:
            self._queue.put_nowait(item)
        if removed:
            self._discard(target)

    def _discard(self, queued: _QueuedEvent[TEvent]) -> None:
        self._queued_event_count = max(0, self._queued_event_count - 1)
        self._queued_bytes = max(0, self._queued_bytes - queued.size)
        if (
            queued.coalesce_key is not None
            and self._pending_by_key.get(queued.coalesce_key) is queued
        ):
            self._pending_by_key.pop(queued.coalesce_key, None)
        self._dropped_events += 1
        self._notify_backpressure("dropped")
        self._notify_queue_change()

    def _update_high_watermarks(self) -> None:
        self._high_watermark_events = max(
            self._high_watermark_events,
            self._queued_event_count,
        )
        self._high_watermark_bytes = max(
            self._high_watermark_bytes,
            self._queued_bytes,
        )

    def _notify_backpressure(self, action: str) -> None:
        if self._on_backpressure is None or self._backpressure_closed:
            return
        payload = {
            "type": "event_stream_backpressure",
            "action": action,
            "queuedEvents": self._queue.qsize(),
            "queuedBytes": self._queued_bytes,
            "droppedEvents": self._dropped_events,
            "coalescedEvents": self._coalesced_events,
        }
        callback = self._on_backpressure
        if _is_async_callable(callback):
            self._queue_backpressure_notification(payload)
            return
        task = self._backpressure_task
        if task is not None and not task.done():
            self._pending_backpressure = payload
            return
        try:
            value = callback(payload)
            if inspect.isawaitable(value):
                self._start_backpressure_worker(cast(Any, value))
        except Exception:
            # Observability hooks must never break the stream they observe.
            return

    def _notify_queue_change(self) -> None:
        """Publish the current queue gauges on every enqueue/dequeue mutation."""

        callback = self._on_queue_change
        if callback is None:
            return
        try:
            value = callback(self.stats)
            if inspect.isawaitable(value):
                # Queue depth reporting is intentionally synchronous: spawning
                # one task per token/update would itself become an unbounded
                # observability queue. Close an accidentally returned coroutine
                # to avoid a RuntimeWarning and ignore the unsupported value.
                closer = getattr(value, "close", None)
                if callable(closer):
                    closer()
        except Exception:
            # Observability must remain isolated from the workload.
            return

    def _queue_backpressure_notification(self, payload: dict[str, Any]) -> None:
        self._pending_backpressure = payload
        task = self._backpressure_task
        if task is None or task.done():
            self._start_backpressure_worker()

    def _start_backpressure_worker(self, initial: Any = None) -> None:
        task = asyncio.create_task(
            self._drain_backpressure_notifications(initial),
            name="event-stream-backpressure",
        )
        self._backpressure_task = task

        def done(completed: asyncio.Task[None]) -> None:
            if self._backpressure_task is completed:
                self._backpressure_task = None
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(done)

    async def _drain_backpressure_notifications(self, initial: Any = None) -> None:
        current = initial
        while not self._backpressure_closed:
            if current is not None:
                try:
                    await cast(Any, current)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
                current = None
            payload = self._pending_backpressure
            self._pending_backpressure = None
            if payload is None:
                return
            try:
                value = cast(Callable[[dict[str, Any]], Any], self._on_backpressure)(
                    payload
                )
                if inspect.isawaitable(value):
                    current = value
            except Exception:
                continue

    def _close_backpressure_notifications(self) -> None:
        if self._backpressure_closed:
            return
        self._backpressure_closed = True
        self._pending_backpressure = None
        task = self._backpressure_task
        if task is not None and not task.done():
            task.cancel()


class AssistantMessageEventStream(EventStream[dict, dict]):
    """Assistant stream with bounded, delta-aware buffering."""

    def __init__(
        self,
        *,
        max_buffer_size: int = 256,
        max_buffer_bytes: int = 4 * 1024 * 1024,
        slow_consumer_policy: SlowConsumerPolicy = "drop_oldest",
        on_backpressure: Callable[[dict[str, Any]], Any] | None = None,
        on_queue_change: Callable[[EventStreamStats], Any] | None = None,
    ) -> None:
        super().__init__(
            lambda event: event.get("type") in {"done", "error"},
            lambda event: event["message"]
            if event.get("type") == "done"
            else event["error"],
            max_buffer_size=max_buffer_size,
            max_buffer_bytes=max_buffer_bytes,
            slow_consumer_policy=slow_consumer_policy,
            coalesce_key=_default_event_key,
            merge_updates=_merge_updates,
            is_droppable=_default_event_droppable,
            on_backpressure=on_backpressure,
            on_queue_change=on_queue_change,
        )


class AgentEventStream(EventStream[dict, list[dict]]):
    """Agent stream with bounded UI/tool update buffering."""

    def __init__(
        self,
        *,
        max_buffer_size: int = 256,
        max_buffer_bytes: int = 4 * 1024 * 1024,
        slow_consumer_policy: SlowConsumerPolicy = "drop_oldest",
        on_backpressure: Callable[[dict[str, Any]], Any] | None = None,
        on_queue_change: Callable[[EventStreamStats], Any] | None = None,
    ) -> None:
        super().__init__(
            lambda event: event.get("type") == "agent_end",
            lambda event: event["messages"],
            max_buffer_size=max_buffer_size,
            max_buffer_bytes=max_buffer_bytes,
            slow_consumer_policy=slow_consumer_policy,
            coalesce_key=_default_event_key,
            merge_updates=_merge_updates,
            is_droppable=_default_event_droppable,
            on_backpressure=on_backpressure,
            on_queue_change=on_queue_change,
        )
