"""同一逻辑 Assistant Turn 的有界模型重试 StreamFn。"""

from __future__ import annotations

import asyncio
import inspect
import random as random_module
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, cast
from uuid import uuid4

from ..cancellation import (
    CancellationToken,
    OperationCancelledError,
)
from ..event_stream import AssistantMessageEventStream
from ..messages import assistant_message, public_error_message
from ..model_attempts import (
    ModelAttemptBudgetExceeded,
    current_model_attempt_admission_scope,
    model_attempt_usage,
    model_attempt_usage_known,
)
from ..types import Model, StreamFn
from .backoff import cancellable_sleep, retry_delay_seconds
from .circuit_breaker import CircuitBreaker, CircuitOpenError
from .classifier import classify_model_error
from .events import RetryEventStore
from .types import ModelRetryPolicy


_PRODUCER_TASK_ATTRIBUTE = "_pi_agent_loop_producer_task"


def bind_stream_producer(stream: Any, task: asyncio.Task[None]) -> None:
    """Bind a background producer to its public stream for structured teardown.

    Retry/compaction adapters return an ``EventStream`` immediately and therefore
    need a background task.  Keeping the task on the stream lets an enclosing
    runtime cancel and drain the whole nested pipeline instead of abandoning an
    inner producer when its consumer is cancelled.
    """

    setattr(stream, _PRODUCER_TASK_ATTRIBUTE, task)


async def settle_stream_producer(stream: Any, *, cancel: bool) -> None:
    """Cancel (when requested) and await a producer previously bound to a stream."""

    task = getattr(stream, _PRODUCER_TASK_ATTRIBUTE, None)
    if not isinstance(task, asyncio.Task) or task is asyncio.current_task():
        return
    if cancel and not task.done():
        task.cancel("上层模型调用已结束")
    await asyncio.gather(task, return_exceptions=True)


class ProducerOwnedAssistantMessageEventStream(AssistantMessageEventStream):
    """Assistant stream whose consumer owns cancellation of its producer."""

    async def _iterate(self) -> AsyncIterator[dict]:
        completed = False
        try:
            async for event in super()._iterate():
                yield event
            completed = True
        finally:
            await settle_stream_producer(self, cancel=not completed)


class RetryingStreamFn:
    """缓冲每次 Attempt；失败 Attempt 不进入 Agent Context。"""

    def __init__(
        self,
        stream_fn: StreamFn,
        policy: ModelRetryPolicy,
        *,
        random: Callable[[], float] | None = None,
        event_store: RetryEventStore | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        physical_attempt_admission: bool = False,
    ) -> None:
        self.stream_fn = stream_fn
        self.policy = policy
        self.random = random or random_module.random
        self.event_store = event_store
        self.circuit_breaker = circuit_breaker or CircuitBreaker(
            policy.circuit_breaker
        )
        if type(physical_attempt_admission) is not bool:
            raise TypeError("physical_attempt_admission must be a bool")
        self.physical_attempt_admission = physical_attempt_admission

    def __call__(
        self,
        model: Model,
        context: dict[str, Any],
        options: dict[str, Any],
    ) -> Any:
        if not self.policy.enabled and not self.physical_attempt_admission:
            return self.stream_fn(model, context, options)
        output = ProducerOwnedAssistantMessageEventStream()
        task = asyncio.create_task(
            self._run(output, model, context, options),
            name=f"pi-model-retry:{model.provider}:{model.id}",
        )
        bind_stream_producer(output, task)
        return output

    async def _run(
        self,
        output: AssistantMessageEventStream,
        model: Model,
        context: dict[str, Any],
        options: dict[str, Any],
    ) -> None:
        retry_id = str(uuid4())
        retries = 0
        started_at = time.monotonic()
        cancellation = options.get("cancellation_token")
        if not isinstance(cancellation, CancellationToken):
            cancellation = None

        try:
            while True:
                circuit_rejected = False
                try:
                    await self.circuit_breaker.before_call()
                except CircuitOpenError as error:
                    circuit_rejected = True
                    final = assistant_message(
                        model=model,
                        stop_reason="error",
                        error_message=public_error_message(
                            error,
                            fallback="Model provider circuit is open",
                        ),
                    )
                    final["providerError"] = {
                        "code": "provider_circuit_open",
                        "statusCode": None,
                        "retryAfterMs": None,
                        "retryable": False,
                    }
                    buffered = [
                        {"type": "error", "reason": "error", "error": final}
                    ]
                else:
                    buffered, final = await self._one_attempt(
                        model,
                        context,
                        options,
                    )
                decision = classify_model_error(final, self.policy)
                if not circuit_rejected:
                    if decision.retryable:
                        await self.circuit_breaker.record_failure()
                    else:
                        await self.circuit_breaker.record_success()
                can_retry = decision.retryable and retries < self.policy.max_retries
                if can_retry:
                    retry_number = retries + 1
                    delay = retry_delay_seconds(
                        retry_number=retry_number,
                        initial_delay_seconds=self.policy.initial_delay_seconds,
                        max_delay_seconds=self.policy.max_delay_seconds,
                        jitter_ratio=self.policy.jitter_ratio,
                        random=self.random,
                        retry_after_seconds=decision.retry_after_seconds,
                    )
                    elapsed = time.monotonic() - started_at
                    if (
                        delay is not None
                        and elapsed + delay <= self.policy.max_elapsed_seconds
                    ):
                        retries = retry_number
                        await self._emit_retry_event(
                            output,
                            options,
                            {
                                "type": "model_retry_scheduled",
                                "kind": "model",
                                "retryId": retry_id,
                                "attempt": retries,
                                "maxAttempts": self.policy.max_retries,
                                "delayMs": round(delay * 1000),
                                "errorCode": decision.code,
                                "statusCode": decision.status_code,
                            },
                        )
                        try:
                            await cancellable_sleep(delay, cancellation)
                        except OperationCancelledError as error:
                            if retries:
                                await self._emit_retry_event(
                                    output,
                                    options,
                                    {
                                        "type": "model_retry_finished",
                                        "kind": "model",
                                        "retryId": retry_id,
                                        "success": False,
                                        "attempt": retries,
                                        "finalError": "retry_cancelled",
                                    },
                                )
                            aborted = assistant_message(
                                model=model,
                                stop_reason="aborted",
                                error_message=public_error_message(
                                    error,
                                    fallback="Model retry was cancelled",
                                ),
                            )
                            output.push(
                                {
                                    "type": "error",
                                    "reason": "aborted",
                                    "error": aborted,
                                }
                            )
                            return
                        await self._emit_retry_event(
                            output,
                            options,
                            {
                                "type": "model_retry_attempt_start",
                                "kind": "model",
                                "retryId": retry_id,
                                "attempt": retries,
                            },
                        )
                        continue

                if retries:
                    await self._emit_retry_event(
                        output,
                        options,
                        {
                            "type": "model_retry_finished",
                            "kind": "model",
                            "retryId": retry_id,
                            "success": final.get("stopReason")
                            not in {"error", "aborted"},
                            "attempt": retries,
                            **(
                                {"finalError": decision.code}
                                if final.get("stopReason") == "error"
                                else {}
                            ),
                        },
                    )
                for event in buffered:
                    output.push(event)
                return
        except BaseException as error:
            output.fail(error)

    async def _one_attempt(
        self,
        model: Model,
        context: dict[str, Any],
        options: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        scope = (
            current_model_attempt_admission_scope()
            if self.physical_attempt_admission
            else None
        )
        identity = None
        if scope is not None:
            try:
                identity = await scope.begin_attempt()
            except ModelAttemptBudgetExceeded as error:
                final = _model_attempt_budget_error(model, scope, error)
                return [{"type": "error", "reason": "error", "error": final}], final
        stream: Any = None
        events: list[dict[str, Any]] = []
        completed = False
        settled = False
        try:
            value = self.stream_fn(model, context, options)
            stream = (
                await cast(Awaitable[Any], value)
                if inspect.isawaitable(value)
                else value
            )
            if not hasattr(stream, "__aiter__") or not hasattr(stream, "result"):
                raise TypeError("RetryingStreamFn 收到不符合契约的事件流")
            async for event in stream:
                events.append(event)
            final = await stream.result()
            if identity is not None and scope is not None:
                metered_final = final
                usage_transform = options.get("_model_attempt_usage_transform")
                if callable(usage_transform):
                    transformed = usage_transform(final)
                    if not isinstance(transformed, dict):
                        raise TypeError(
                            "_model_attempt_usage_transform must return a dict"
                        )
                    metered_final = transformed
                tokens, cost = model_attempt_usage(metered_final)
                try:
                    await scope.finish_attempt(identity, tokens=tokens, cost=cost, usage_unknown=not model_attempt_usage_known(metered_final))
                    settled = True
                except ModelAttemptBudgetExceeded as error:
                    settled = True
                    final = _model_attempt_budget_error(model, scope, error)
                    return [
                        {"type": "error", "reason": "error", "error": final}
                    ], final
            if not events or events[-1].get("type") not in {"done", "error"}:
                raise RuntimeError("模型 Attempt 没有产生终止事件")
            completed = True
            return events, final
        except BaseException:
            if identity is not None and scope is not None and not settled:
                try:
                    await scope.finish_attempt(identity, usage_unknown=True)
                except BaseException:
                    pass
            raise
        finally:
            if stream is not None:
                await settle_stream_producer(stream, cancel=not completed)

    async def _emit_retry_event(
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
        if self.event_store is not None:
            await self.event_store.append(event)
        output.push(event)


def retry_model_stream(
    stream_fn: StreamFn,
    policy: ModelRetryPolicy,
    *,
    random: Callable[[], float] | None = None,
    event_store: RetryEventStore | None = None,
    circuit_breaker: CircuitBreaker | None = None,
    physical_attempt_admission: bool = False,
) -> StreamFn:
    """函数式工厂，便于注入 Agent 或 Provider。"""

    return cast(
        StreamFn,
        RetryingStreamFn(
            stream_fn,
            policy,
            random=random,
            event_store=event_store,
            circuit_breaker=circuit_breaker,
            physical_attempt_admission=physical_attempt_admission,
        ),
    )


def _model_attempt_budget_error(model, scope, error):
    final = assistant_message(
        model=model,
        stop_reason="error",
        error_message="Model Provider attempt was rejected by the hard budget",
    )
    final["providerError"] = {
        "code": ModelAttemptBudgetExceeded.code,
        "statusCode": None,
        "retryAfterMs": None,
        "retryable": False,
    }
    final["modelAttemptAdmission"] = {
        "runId": scope.run_id,
        "stage": scope.stage,
        "reservationId": scope.reservation_id,
        "reason": public_error_message(
            error,
            fallback="Model provider attempt was rejected by the hard budget",
        ),
        "errorType": type(error).__name__,
    }
    return final
