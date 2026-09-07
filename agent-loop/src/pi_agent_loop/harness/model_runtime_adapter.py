"""Unified model-call boundary for Agent, Router and durable recovery."""

from __future__ import annotations

import asyncio
import copy
import inspect
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid4

from ..cancellation import CancellationToken
from ..execution_policy import ExecutionPolicy, ExecutionContext
from ..event_stream import AssistantMessageEventStream
from ..messages import assistant_message, empty_usage, public_error_message
from ..model_attempts import (
    ModelAttemptAdmissionScope,
    ModelAttemptBudgetExceeded,
    ModelAttemptIdentity,
    current_model_attempt_admission_scope,
    model_attempt_usage,
    model_attempt_usage_known,
)
from ..model_policy import (
    ModelRequestPolicy,
    ModelRequestPolicyError,
    capture_model_request_policy,
    validate_recoverable_model_response,
)
from ..retry.circuit_breaker import CircuitBreaker
from ..retry.compaction import (
    CompactionRetryPolicy,
    ContextReplacement,
    compact_on_context_overflow,
)
from ..retry.events import RetryEventStore
from ..retry.model import (
    ProducerOwnedAssistantMessageEventStream,
    bind_stream_producer,
    retry_model_stream,
    settle_stream_producer,
)
from ..retry.types import ModelRetryPolicy
from ..runtime.telemetry import Telemetry
from ..types import AgentTool, Model, StreamFn

ModelEventSink = Callable[[dict[str, Any]], Any]


@dataclass(frozen=True, slots=True)
class TokenPricing:
    """Model price in currency units per one million tokens."""

    input_per_million: float = 0.0
    output_per_million: float = 0.0
    cache_read_per_million: float = 0.0
    cache_write_per_million: float = 0.0

    def __post_init__(self) -> None:
        for name, value in (
            ("input_per_million", self.input_per_million),
            ("output_per_million", self.output_per_million),
            ("cache_read_per_million", self.cache_read_per_million),
            ("cache_write_per_million", self.cache_write_per_million),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value < 0
            ):
                raise ValueError(f"{name} must be a finite non-negative number")


class ModelCallRuntime:
    """One model boundary shared by normal Agent, Router and recovery calls.

    Pass :attr:`stream` anywhere a regular ``StreamFn`` is accepted. Optional retry,
    circuit and compaction policies are composed once here, preventing each caller
    from building a subtly different provider pipeline. Every invocation records
    a request policy snapshot, cancellation outcome, usage/cost, durable events,
    metrics, a trace span and structured logs.
    """

    def __init__(
        self,
        stream_fn: StreamFn,
        *,
        retry_policy: ModelRetryPolicy | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        retry_event_store: RetryEventStore | None = None,
        compaction_policy: CompactionRetryPolicy | None = None,
        compactor: Callable[
            [list[dict[str, Any]]],
            Awaitable[list[dict[str, Any]] | ContextReplacement],
        ]
        | None = None,
        retry_event_sink: ModelEventSink | None = None,
        durable_event_sink: ModelEventSink | None = None,
        telemetry: Telemetry | None = None,
        pricing: TokenPricing | Mapping[str, TokenPricing] | None = None,
        max_buffer_size: int = 256,
        max_buffer_bytes: int = 4 * 1024 * 1024,
        execution_policy: ExecutionPolicy | None = None,
        tenant_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        if execution_policy is not None and not isinstance(execution_policy, ExecutionPolicy):
            raise TypeError("execution_policy must be ExecutionPolicy")
        self.execution_policy = execution_policy
        self.tenant_id = tenant_id
        self.session_id = session_id
        self.upstream_stream_fn = stream_fn
        # Admission wraps the raw Provider before Retry and Compaction. Thus
        # every physical attempt crosses the same reserved retry-tree scope.
        upstream_owner = getattr(stream_fn, "__self__", None)
        self.upstream_provides_physical_attempt_admission = bool(
            getattr(
                upstream_owner,
                "provides_physical_attempt_admission",
                False,
            )
        )
        effective: StreamFn = (
            stream_fn
            if self.upstream_provides_physical_attempt_admission
            else self._admitted_upstream_stream
        )
        # Inspect the actual context at the Provider boundary, inside Runtime
        # retry/compaction wrappers. Context transformation remains once per
        # logical request; compaction must not reuse a verdict on older input.
        if execution_policy is not None and execution_policy.content_safety is not None:
            dispatch = effective

            async def inspected_stream(
                model: Model, context: dict[str, Any], options: dict[str, Any]
            ) -> Any:
                token = options.get("cancellation_token") or CancellationToken()
                scope = current_model_attempt_admission_scope()
                phase = scope.stage if scope is not None else str(options.get("_execution_phase", "agent"))
                messages = await execution_policy.inspect_input(
                    context.get("messages", []), token,
                    ExecutionContext(phase, self.tenant_id, self.session_id),
                )
                token.throw_if_cancelled()
                value = dispatch(model, {**context, "messages": messages}, options)
                return await value if inspect.isawaitable(value) else value

            effective = inspected_stream
        if retry_policy is not None:
            effective = retry_model_stream(
                effective,
                retry_policy,
                event_store=retry_event_store,
                circuit_breaker=circuit_breaker,
            )
        elif circuit_breaker is not None or retry_event_store is not None:
            raise ValueError(
                "circuit_breaker/retry_event_store require retry_policy"
            )
        if compaction_policy is not None:
            effective = compact_on_context_overflow(
                effective,
                compaction_policy,
                compactor=compactor,
            )
        elif compactor is not None:
            raise ValueError("compactor requires compaction_policy")

        self.effective_stream_fn = effective
        self.retry_event_sink = retry_event_sink
        self.durable_event_sink = durable_event_sink
        self.telemetry = telemetry or Telemetry()
        self.pricing = pricing
        self.max_buffer_size = max_buffer_size
        self.max_buffer_bytes = max_buffer_bytes
        self._accepting = True
        self._closed = False
        self._active_tasks: set[asyncio.Task[None]] = set()
        self._close_lock = asyncio.Lock()
        # ``stream_fn`` is kept as a compatibility alias for callers that inspect
        # or pass through a StreamFn attribute.
        self.stream_fn: StreamFn = self.stream

    def stream(
        self,
        model: Model,
        context: dict[str, Any],
        options: dict[str, Any],
    ) -> AssistantMessageEventStream:
        """Return a standard stream and execute the unified boundary in background."""

        if not self._accepting or self._closed:
            raise RuntimeError("ModelCallRuntime is closed or closing")

        output = ProducerOwnedAssistantMessageEventStream(
            max_buffer_size=self.max_buffer_size,
            max_buffer_bytes=self.max_buffer_bytes,
            on_backpressure=lambda payload: self.telemetry.record_backpressure(
                "model_call",
                payload,
            ),
            on_queue_change=lambda stats: self.telemetry.record_queue_depth(
                "model_call",
                events=stats.queued_events,
                retained_bytes=stats.queued_bytes,
            ),
        )
        task = asyncio.create_task(
            self._run(output, model, copy.deepcopy(context), dict(options)),
            name=f"pi-model-call:{model.provider}:{model.id}",
        )
        self._active_tasks.add(task)
        bind_stream_producer(output, task)

        def settled(completed: asyncio.Task[None]) -> None:
            self._active_tasks.discard(completed)
            _close_unhandled_task(completed, output)

        task.add_done_callback(settled)
        return output

    @property
    def active_call_count(self) -> int:
        return len(self._active_tasks)

    @property
    def closed(self) -> bool:
        return self._closed

    async def aclose(self) -> None:
        """Stop admission and settle every model/provider pump before returning."""

        async with self._close_lock:
            if self._closed:
                return
            self._accepting = False
            active = tuple(self._active_tasks)
            for task in active:
                task.cancel("ModelCallRuntime is closing")
            if active:
                await asyncio.gather(*active, return_exceptions=True)
            self._closed = True

    async def invoke(
        self,
        model: Model,
        context: dict[str, Any],
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Convenience API for non-streaming callers such as recovery planners."""

        stream = self.stream(model, context, options or {})
        completed = False
        try:
            async for _event in stream:
                pass
            result = await stream.result()
            completed = True
            return result
        finally:
            # ``invoke`` is itself the consumer boundary.  If its caller goes
            # away, the background ModelCallRuntime producer and every nested
            # retry/compaction/provider producer must be cancelled and drained.
            await settle_stream_producer(stream, cancel=not completed)

    def _admitted_upstream_stream(
        self,
        model: Model,
        context: dict[str, Any],
        options: dict[str, Any],
    ) -> Any:
        """Dispatch the raw Provider through the active physical-attempt scope."""

        scope = current_model_attempt_admission_scope()
        if scope is None:
            # Preserve the generic StreamFn contract and its exact return value
            # when no autonomous hard-budget scope is active.
            return self.upstream_stream_fn(model, context, options)
        output = ProducerOwnedAssistantMessageEventStream(
            max_buffer_size=self.max_buffer_size,
            max_buffer_bytes=self.max_buffer_bytes,
        )
        task = asyncio.create_task(
            self._run_admitted_upstream(
                output,
                scope,
                model,
                copy.deepcopy(context),
                dict(options),
            ),
            name=f"pi-model-attempt:{scope.run_id}:{scope.stage}",
        )
        bind_stream_producer(output, task)
        return output

    async def _run_admitted_upstream(
        self,
        output: AssistantMessageEventStream,
        scope: ModelAttemptAdmissionScope,
        model: Model,
        context: dict[str, Any],
        options: dict[str, Any],
    ) -> None:
        identity: ModelAttemptIdentity | None = None
        upstream: Any = None
        completed = False
        settled = False
        try:
            try:
                identity = await scope.begin_attempt()
            except ModelAttemptBudgetExceeded as error:
                _push_model_attempt_budget_error(output, model, scope, error)
                return
            value = self.upstream_stream_fn(model, context, options)
            upstream = (
                await cast(Awaitable[Any], value)
                if inspect.isawaitable(value)
                else value
            )
            if not hasattr(upstream, "__aiter__") or not hasattr(upstream, "result"):
                raise TypeError("raw Provider returned an invalid StreamFn result")
            terminal_event: dict[str, Any] | None = None
            async for event in upstream:
                if event.get("type") in {"done", "error"}:
                    terminal_event = dict(event)
                else:
                    output.push(event)
            final = _apply_pricing(
                await upstream.result(),
                self._pricing_for(model),
            )
            tokens, cost = model_attempt_usage(final)
            try:
                await scope.finish_attempt(identity, tokens=tokens, cost=cost, usage_unknown=not model_attempt_usage_known(final))
                settled = True
            except ModelAttemptBudgetExceeded as error:
                settled = True
                _push_model_attempt_budget_error(output, model, scope, error)
                completed = True
                return
            if terminal_event is None:
                event_type = (
                    "error"
                    if final.get("stopReason") in {"error", "aborted"}
                    else "done"
                )
                terminal_event = {
                    "type": event_type,
                    "reason": final.get("stopReason"),
                }
            if terminal_event.get("type") == "error":
                terminal_event["error"] = final
            else:
                terminal_event["message"] = final
            output.push(terminal_event)
            completed = True
        except BaseException as error:
            if identity is not None and not settled:
                try:
                    await scope.finish_attempt(identity, usage_unknown=True)
                except BaseException:
                    pass
            output.fail(error)
        finally:
            if upstream is not None:
                await settle_stream_producer(upstream, cancel=not completed)

    async def _run(
        self,
        output: AssistantMessageEventStream,
        model: Model,
        context: dict[str, Any],
        options: dict[str, Any],
    ) -> None:
        started = time.monotonic()
        request_id = _request_id(options)
        source = str(options.pop("model_request_source", "agent"))
        options["_execution_phase"] = source
        trace_id = _optional_string(options.pop("trace_id", None))
        parent_span_id = _optional_string(options.pop("parent_span_id", None))
        durable_metadata = _durable_metadata(options.pop("durable_metadata", {}))
        preserve_started_on_cancel = options.pop(
            "_preserve_durable_started_on_cancel",
            False,
        )
        if not isinstance(preserve_started_on_cancel, bool):
            raise TypeError("_preserve_durable_started_on_cancel must be a bool")
        explicit_policy = options.pop("request_policy", None)
        policy = _request_policy(context, options, explicit_policy)
        configured_retry_sink = options.get("retry_event_sink")
        if configured_retry_sink is None:
            configured_retry_sink = self.retry_event_sink

        async def observed_retry_sink(event: dict[str, Any]) -> None:
            await self._observe_retry_event(model, source, event)
            if callable(configured_retry_sink):
                value = configured_retry_sink(copy.deepcopy(event))
                if inspect.isawaitable(value):
                    await cast(Awaitable[Any], value)

        # Provider-level and Runtime-level retry wrappers both receive the same
        # observed sink. This keeps retry/circuit telemetry consistent even when
        # the provider owns its retry implementation.
        options["retry_event_sink"] = observed_retry_sink
        # Internal Provider retry adapters use this trusted transform to price
        # every failed/successful physical attempt before aggregating it.
        options["_model_attempt_usage_transform"] = lambda message: _apply_pricing(
            message,
            self._pricing_for(model),
        )
        token = options.get("cancellation_token")
        if token is not None and not isinstance(token, CancellationToken):
            token = None

        span = self.telemetry.start_span(
            "model.call",
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            attributes={
                "requestId": request_id,
                "provider": model.provider,
                "model": model.id,
                "source": source,
            },
        )
        started_event = {
            **copy.deepcopy(durable_metadata),
            "type": "model_request_started",
            "requestId": request_id,
            "timestamp": int(time.time() * 1000),
            "provider": model.provider,
            "model": model.id,
            "source": source,
            "requestPolicy": policy.to_dict(),
        }
        terminal_attempted = False
        terminal_recorded = False
        try:
            await self._emit_durable(started_event)
            await self.telemetry.log(
                "info",
                "model_request_started",
                trace_id=span.trace_id,
                span_id=span.span_id,
                requestId=request_id,
                provider=model.provider,
                model=model.id,
                source=source,
            )
            if token is not None and token.cancelled:
                final = assistant_message(
                    model=model,
                    stop_reason="aborted",
                    error_message=token.reason,
                )
                terminal_event = {
                    "type": "error",
                    "reason": "aborted",
                    "error": final,
                }
            else:
                terminal_event, final = await self._consume_with_cancellation(
                    output,
                    model,
                    context,
                    options,
                    token,
                )
            duration_ms = max(0.0, (time.monotonic() - started) * 1000)
            terminal_attempted = True
            outcome, stop_reason = await self._commit_terminal(
                model=model,
                source=source,
                request_id=request_id,
                final=final,
                duration_ms=duration_ms,
                durable_metadata=durable_metadata,
            )
            # The durable terminal is the commit boundary.  Mark and publish it
            # before any best-effort telemetry await so shutdown cancellation can
            # never append a second, contradictory terminal for this request.
            terminal_recorded = True
            output.push(terminal_event)
            await self._finish_call(
                span=span,
                model=model,
                source=source,
                request_id=request_id,
                final=final,
                duration_ms=duration_ms,
                outcome=outcome,
                stop_reason=stop_reason,
            )
        except asyncio.CancelledError as error:
            if terminal_recorded:
                return
            # A durable recovery coordinator deliberately reuses an unfinished
            # request id after process/task interruption.  Before the terminal
            # commit boundary, preserve its existing ``started`` record so the
            # next recovery attempt can finish the same logical request.  Host
            # shutdown for ordinary calls still records an aborted terminal.
            if preserve_started_on_cancel and not terminal_attempted:
                try:
                    self.telemetry.metrics.increment(
                        "model_requests_interrupted_total",
                        labels={
                            "provider": model.provider,
                            "model": model.id,
                            "source": source,
                        },
                    )
                    await self.telemetry.log(
                        "warning",
                        "model_request_interrupted",
                        trace_id=span.trace_id,
                        span_id=span.span_id,
                        requestId=request_id,
                        provider=model.provider,
                        model=model.id,
                        source=source,
                    )
                    await span.finish(status="error", error=error)
                except BaseException:
                    pass
                return
            final = assistant_message(
                model=model,
                stop_reason="aborted",
                error_message="模型请求已取消",
            )
            if not terminal_attempted:
                duration_ms = max(0.0, (time.monotonic() - started) * 1000)
                terminal_attempted = True
                outcome, stop_reason = await self._commit_terminal(
                    model=model,
                    source=source,
                    request_id=request_id,
                    final=final,
                    duration_ms=duration_ms,
                    durable_metadata=durable_metadata,
                    error=error,
                )
                terminal_recorded = True
                output.push({"type": "error", "reason": "aborted", "error": final})
                await self._finish_call(
                    span=span,
                    model=model,
                    source=source,
                    request_id=request_id,
                    final=final,
                    duration_ms=duration_ms,
                    outcome=outcome,
                    stop_reason=stop_reason,
                    error=error,
                )
            elif not terminal_recorded:
                output.fail(error)
        except BaseException as error:
            if terminal_recorded:
                try:
                    await span.finish(status="error", error=error)
                except BaseException:
                    pass
                return
            final = assistant_message(
                model=model,
                stop_reason="error",
                error_message="模型调用边界发生内部错误",
            )
            if not terminal_attempted:
                try:
                    duration_ms = max(0.0, (time.monotonic() - started) * 1000)
                    terminal_attempted = True
                    outcome, stop_reason = await self._commit_terminal(
                        model=model,
                        source=source,
                        request_id=request_id,
                        final=final,
                        duration_ms=duration_ms,
                        durable_metadata=durable_metadata,
                        error=error,
                    )
                    terminal_recorded = True
                    output.push({"type": "error", "reason": "error", "error": final})
                    await self._finish_call(
                        span=span,
                        model=model,
                        source=source,
                        request_id=request_id,
                        final=final,
                        duration_ms=duration_ms,
                        outcome=outcome,
                        stop_reason=stop_reason,
                        error=error,
                    )
                except BaseException:
                    await span.finish(status="error", error=error)
            elif not terminal_recorded:
                output.fail(error)

    async def _consume_with_cancellation(
        self,
        output: AssistantMessageEventStream,
        model: Model,
        context: dict[str, Any],
        options: dict[str, Any],
        token: CancellationToken | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Consume one upstream call while enforcing the Runtime cancellation boundary.

        Providers still receive the cooperative token, but the Runtime also owns a
        pump task and races it against ``token.wait()``. Therefore a provider that
        never observes the token cannot leave the public stream waiting forever.
        If the model pump and cancellation finish in the same event-loop turn, a
        completed model result wins; its terminal response is already authoritative.
        """

        pump = asyncio.create_task(
            self._consume_upstream(output, model, context, options),
            name=f"pi-model-upstream:{model.provider}:{model.id}",
        )
        cancellation_waiter = (
            asyncio.create_task(
                token.wait(),
                name=f"pi-model-cancellation:{model.provider}:{model.id}",
            )
            if token is not None
            else None
        )
        try:
            if cancellation_waiter is None:
                return await pump
            await asyncio.wait(
                {pump, cancellation_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Check current task state instead of the wait() snapshot so a terminal
            # result completed in the same loop turn deterministically wins.
            if pump.done():
                return await pump

            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
            final = assistant_message(
                model=model,
                stop_reason="aborted",
                error_message=(
                    token.reason if token is not None else "模型请求已取消"
                ),
            )
            return (
                {"type": "error", "reason": "aborted", "error": final},
                final,
            )
        finally:
            if not pump.done():
                pump.cancel()
            if cancellation_waiter is not None and not cancellation_waiter.done():
                cancellation_waiter.cancel()
            waits: list[asyncio.Task[Any]] = [pump]
            if cancellation_waiter is not None:
                waits.append(cancellation_waiter)
            await asyncio.gather(*waits, return_exceptions=True)

    async def _consume_upstream(
        self,
        output: AssistantMessageEventStream,
        model: Model,
        context: dict[str, Any],
        options: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        token = options.get("cancellation_token") or CancellationToken()
        phase = str(options.get("_execution_phase", "agent"))
        scope = current_model_attempt_admission_scope()
        if scope is not None:
            phase = scope.stage
        execution_context = ExecutionContext(phase, self.tenant_id, self.session_id)
        if self.execution_policy is not None:
            messages = await self.execution_policy.transform(context.get("messages", []), token, execution_context)
            context = {**context, "messages": messages}
        buffer_output = self.execution_policy is not None and self.execution_policy.content_safety is not None
        value = self.effective_stream_fn(model, context, options)
        upstream = (
            await cast(Awaitable[Any], value)
            if inspect.isawaitable(value)
            else value
        )
        if not hasattr(upstream, "__aiter__") or not hasattr(upstream, "result"):
            raise TypeError("ModelCallRuntime received an invalid StreamFn result")
        completed = False
        try:
            terminal_seen = False
            terminal_event: dict[str, Any] | None = None
            final: dict[str, Any] | None = None
            async for event in upstream:
                if event.get("type") in {"done", "error"}:
                    final = _apply_pricing(
                        await upstream.result(),
                        self._pricing_for(model),
                    )
                    terminal = dict(event)
                    if event.get("type") == "done":
                        terminal["message"] = final
                    else:
                        terminal["error"] = final
                    terminal_event = terminal
                    terminal_seen = True
                elif not buffer_output:
                    output.push(event)
            if not terminal_seen:
                final = _apply_pricing(
                    await upstream.result(),
                    self._pricing_for(model),
                )
                event_type = (
                    "error"
                    if final.get("stopReason") in {"error", "aborted"}
                    else "done"
                )
                terminal_event = {
                    "type": event_type,
                    "reason": final.get("stopReason"),
                    "error" if event_type == "error" else "message": final,
                }
            if terminal_event is None or final is None:
                raise RuntimeError("model stream did not produce a terminal result")
            if self.execution_policy is not None:
                final = await self.execution_policy.inspect_output(final, token, execution_context)
                terminal_event["error" if terminal_event.get("type") == "error" else "message"] = final
            completed = True
            return terminal_event, final
        finally:
            await settle_stream_producer(upstream, cancel=not completed)

    async def _commit_terminal(
        self,
        *,
        model: Model,
        source: str,
        request_id: str,
        final: dict[str, Any],
        duration_ms: float,
        durable_metadata: dict[str, Any],
        error: BaseException | None = None,
    ) -> tuple[str, str]:
        """Append exactly one terminal event through an uncancellable boundary."""

        stop_reason = str(final.get("stopReason", "error"))
        outcome = _model_outcome(stop_reason, error)
        event_type = (
            "model_request_completed"
            if outcome == "completed"
            else "model_request_failed"
        )
        commit = asyncio.create_task(
            self._emit_durable(
                {
                    **copy.deepcopy(durable_metadata),
                    "type": event_type,
                    "requestId": request_id,
                    "timestamp": int(time.time() * 1000),
                    "provider": model.provider,
                    "model": model.id,
                    "source": source,
                    "outcome": outcome,
                    "durationMs": duration_ms,
                    "usage": copy.deepcopy(final.get("usage", empty_usage())),
                    "message": copy.deepcopy(final),
                }
            ),
            name=f"pi-model-terminal-commit:{request_id}",
        )
        await _await_uncancellable(commit)
        return outcome, stop_reason

    async def _finish_call(
        self,
        *,
        span: Any,
        model: Model,
        source: str,
        request_id: str,
        final: dict[str, Any],
        duration_ms: float,
        outcome: str,
        stop_reason: str,
        error: BaseException | None = None,
    ) -> None:
        """Export best-effort observability after the durable terminal commit."""

        await self.telemetry.record_model_finished(
            provider=model.provider,
            model=model.id,
            source=source,
            outcome=outcome,
            duration_ms=duration_ms,
            usage=final.get("usage") if isinstance(final.get("usage"), dict) else None,
        )
        await self.telemetry.log(
            "error" if outcome == "failed" else "info",
            "model_request_finished",
            trace_id=span.trace_id,
            span_id=span.span_id,
            requestId=request_id,
            provider=model.provider,
            model=model.id,
            source=source,
            outcome=outcome,
            durationMs=duration_ms,
            usage=final.get("usage"),
        )
        if outcome == "failed":
            provider_error = final.get("providerError")
            if (
                isinstance(provider_error, Mapping)
                and provider_error.get("code") == "provider_circuit_open"
            ):
                self.telemetry.metrics.increment(
                    "model_circuit_open_total",
                    labels={
                        "provider": model.provider,
                        "model": model.id,
                        "source": source,
                    },
                )
                await self.telemetry.alert(
                    "model_circuit_open",
                    severity="warning",
                    requestId=request_id,
                    provider=model.provider,
                    model=model.id,
                    source=source,
                )
            await self.telemetry.alert(
                "model_request_failed",
                severity="warning",
                requestId=request_id,
                provider=model.provider,
                model=model.id,
                source=source,
            )
        await span.finish(
            status=cast(
                Any,
                "ok"
                if outcome == "completed"
                else "cancelled"
                if outcome == "cancelled"
                else "error",
            ),
            error=error,
            attributes={"outcome": outcome, "stopReason": stop_reason},
        )

    async def _observe_retry_event(
        self,
        model: Model,
        source: str,
        event: dict[str, Any],
    ) -> None:
        event_type = event.get("type")
        if event_type == "model_retry_scheduled":
            labels = {
                "provider": model.provider,
                "model": model.id,
                "source": source,
                "error": str(event.get("errorCode", "unknown")),
            }
            self.telemetry.metrics.increment(
                "model_retries_total",
                labels=labels,
            )
            delay = event.get("delayMs")
            if isinstance(delay, (int, float)) and not isinstance(delay, bool):
                self.telemetry.metrics.observe(
                    "model_retry_delay_ms",
                    max(0.0, float(delay)),
                    labels=labels,
                )
        elif event_type == "model_retry_finished":
            self.telemetry.metrics.increment(
                "model_retry_sequences_total",
                labels={
                    "provider": model.provider,
                    "model": model.id,
                    "source": source,
                    "outcome": "succeeded" if event.get("success") else "failed",
                },
            )

    async def _emit_durable(self, event: dict[str, Any]) -> None:
        if self.durable_event_sink is None:
            return
        value = self.durable_event_sink(copy.deepcopy(event))
        if inspect.isawaitable(value):
            await cast(Awaitable[Any], value)

    def _pricing_for(self, model: Model) -> TokenPricing | None:
        if self.pricing is None or isinstance(self.pricing, TokenPricing):
            return self.pricing
        return self.pricing.get(model.id) or self.pricing.get(
            f"{model.provider}/{model.id}"
        )


class RecoverableModelRuntime(ModelCallRuntime):
    """Policy-strict recovery adapter built on the unified model runtime."""

    def __init__(
        self,
        *,
        model: Model,
        stream_fn: StreamFn,
        system_prompt: str,
        tools: list[AgentTool],
        retry_policy: ModelRetryPolicy | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        retry_event_store: RetryEventStore | None = None,
        compaction_policy: CompactionRetryPolicy | None = None,
        compactor: Callable[
            [list[dict[str, Any]]],
            Awaitable[list[dict[str, Any]] | ContextReplacement],
        ]
        | None = None,
        retry_event_sink: Any | None = None,
        durable_event_sink: ModelEventSink | None = None,
        telemetry: Telemetry | None = None,
        pricing: TokenPricing | Mapping[str, TokenPricing] | None = None,
        max_buffer_size: int = 256,
        max_buffer_bytes: int = 4 * 1024 * 1024,
        execution_policy: ExecutionPolicy | None = None,
        tenant_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        super().__init__(
            stream_fn,
            retry_policy=retry_policy,
            circuit_breaker=circuit_breaker,
            retry_event_store=retry_event_store,
            compaction_policy=compaction_policy,
            compactor=compactor,
            retry_event_sink=retry_event_sink,
            durable_event_sink=durable_event_sink,
            telemetry=telemetry,
            pricing=pricing,
            max_buffer_size=max_buffer_size,
            max_buffer_bytes=max_buffer_bytes,
            execution_policy=execution_policy,
            tenant_id=tenant_id,
            session_id=session_id,
        )
        self.model = model
        self.system_prompt = system_prompt
        self.tools = list(tools)
        self._tools_by_name = {tool.name: tool for tool in self.tools}

    async def request(
        self,
        messages: list[dict[str, Any]],
        *,
        policy: ModelRequestPolicy,
        cancellation: CancellationToken | None = None,
        request_id: str | None = None,
        durable_metadata: Mapping[str, Any] | None = None,
        source: str = "recovery",
    ) -> dict[str, Any]:
        if not isinstance(policy, ModelRequestPolicy):
            raise ModelRequestPolicyError(
                "恢复模型请求必须提供持久化 ModelRequestPolicy"
            )
        missing = [
            name for name in policy.visible_tool_names if name not in self._tools_by_name
        ]
        if missing:
            raise ModelRequestPolicyError(
                "当前 Runtime 缺少策略要求的工具：" + "、".join(missing)
            )
        selected_tools = [self._tools_by_name[name] for name in policy.visible_tool_names]
        context = {
            "systemPrompt": self.system_prompt,
            "messages": list(messages),
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                }
                for tool in selected_tools
            ],
        }
        if not isinstance(source, str) or not source:
            raise ValueError("模型请求 source 必须是非空字符串")
        options: dict[str, Any] = {
            "cancellation_token": cancellation or CancellationToken(),
            "tool_choice": policy.tool_choice,
            "required_capabilities": list(policy.required_capabilities),
            "allowed_tool_names": list(policy.allowed_tool_names),
            "request_policy": policy,
            "model_request_source": source,
        }
        if request_id is not None:
            options["model_request_id"] = request_id
            options["_preserve_durable_started_on_cancel"] = True
        if durable_metadata is not None:
            options["durable_metadata"] = dict(durable_metadata)
        if self.retry_event_sink is not None:
            options["retry_event_sink"] = self.retry_event_sink
        if policy.expected_tool_arguments is not None:
            options["expected_tool_arguments"] = copy.deepcopy(
                policy.expected_tool_arguments
            )
        message = await self.invoke(self.model, context, options)
        stop_reason = message.get("stopReason")
        if stop_reason in {"error", "aborted"}:
            raise RuntimeError(str(message.get("errorMessage", "恢复模型请求失败")))
        if stop_reason == "length":
            raise RuntimeError("恢复模型响应达到长度上限，禁止完成 Operation")
        if stop_reason not in {"stop", "toolUse"}:
            raise RuntimeError(f"恢复模型响应终止原因无效：{stop_reason}")
        validate_recoverable_model_response(message, policy)
        return message


def _request_id(options: dict[str, Any]) -> str:
    raw = options.pop("model_request_id", None)
    if raw is None:
        return str(uuid4())
    if not isinstance(raw, str) or not raw:
        raise ValueError("model_request_id must be a non-empty string")
    return raw


def _model_outcome(stop_reason: str, error: BaseException | None) -> str:
    if stop_reason == "aborted":
        return "cancelled"
    if stop_reason == "error" or error is not None:
        return "failed"
    return "completed"


async def _await_uncancellable(task: asyncio.Task[Any]) -> Any:
    """Wait for one durable commit even when the enclosing call is cancelled.

    ``asyncio.shield`` prevents cancellation from reaching the commit task.  A
    caller can issue cancellation more than once, so keep joining the same task
    until it has a definitive result.  Retrying the sink itself would be unsafe:
    a sink may have committed immediately before raising.
    """

    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


def _close_unhandled_task(
    task: asyncio.Task[None],
    output: AssistantMessageEventStream,
) -> None:
    """Retrieve task exceptions and guarantee callers never wait forever."""

    if task.cancelled():
        output.fail(asyncio.CancelledError())
        return
    error = task.exception()
    if error is not None:
        output.fail(error)


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


_DURABLE_RESERVED_FIELDS = frozenset(
    {
        "type",
        "requestId",
        "timestamp",
        "provider",
        "model",
        "source",
        "requestPolicy",
        "outcome",
        "durationMs",
        "usage",
        "message",
        "error",
    }
)


def _durable_metadata(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError("durable_metadata must be a dict")
    conflicts = sorted(set(value).intersection(_DURABLE_RESERVED_FIELDS))
    if conflicts:
        raise ValueError(
            "durable_metadata contains reserved fields: " + ", ".join(conflicts)
        )
    return copy.deepcopy(value)


def _request_policy(
    context: dict[str, Any],
    options: dict[str, Any],
    explicit: Any,
) -> ModelRequestPolicy:
    if explicit is not None:
        if isinstance(explicit, ModelRequestPolicy):
            return explicit
        if isinstance(explicit, dict):
            return ModelRequestPolicy.from_dict(explicit)
        raise ModelRequestPolicyError("request_policy must be ModelRequestPolicy or dict")
    names: list[str] = []
    for tool in context.get("tools", []):
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not isinstance(name, str):
            function = tool.get("function")
            name = function.get("name") if isinstance(function, dict) else None
        if isinstance(name, str) and name:
            names.append(name)
    return capture_model_request_policy(names, options)


def _apply_pricing(
    message: dict[str, Any],
    pricing: TokenPricing | None,
) -> dict[str, Any]:
    output = copy.deepcopy(message)
    usage = output.get("usage")
    if not isinstance(usage, dict):
        usage = empty_usage()
        output["usage"] = usage
    if pricing is None:
        return output

    def tokens(name: str) -> int:
        value = usage.get(name, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return 0
        return value

    costs = {
        "input": tokens("input") * pricing.input_per_million / 1_000_000,
        "output": tokens("output") * pricing.output_per_million / 1_000_000,
        "cacheRead": tokens("cacheRead")
        * pricing.cache_read_per_million
        / 1_000_000,
        "cacheWrite": tokens("cacheWrite")
        * pricing.cache_write_per_million
        / 1_000_000,
    }
    costs["total"] = sum(costs.values())
    usage["cost"] = costs
    return output


def _push_model_attempt_budget_error(
    output: AssistantMessageEventStream,
    model: Model,
    scope: ModelAttemptAdmissionScope,
    error: BaseException,
) -> None:
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
    output.push({"type": "error", "reason": "error", "error": final})
