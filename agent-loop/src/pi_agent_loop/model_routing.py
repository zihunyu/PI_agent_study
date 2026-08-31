"""Deterministic multi-model selection and pre-output provider failover.

``ResilientModelRouter.stream`` is a real :class:`~pi_agent_loop.types.StreamFn`
and can be passed directly to ``Agent``.  Each provider attempt is held behind a
commit barrier until assistant text or a tool call becomes visible.  A retryable
provider failure may select another candidate only before that barrier opens.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, cast
from uuid import uuid4

from .cancellation import CancellationToken, OperationCancelledError
from .event_stream import AssistantMessageEventStream
from .messages import (
    assistant_message,
    public_error_message,
    redact_sensitive_data,
)
from .providers.errors import ProviderError
from .retry.model import (
    ProducerOwnedAssistantMessageEventStream,
    bind_stream_producer,
    settle_stream_producer,
)
from .types import Model, StreamFn

HealthStatus = Literal["closed", "open", "half_open"]
AttemptOutcome = Literal[
    "success",
    "retryable_error",
    "non_retryable_error",
    "cancelled",
    "deadline_exceeded",
]
TelemetrySink = Callable[[dict[str, Any]], Any]
RequirementsResolver = Callable[
    [dict[str, Any], dict[str, Any]],
    "TaskRequirements | Awaitable[TaskRequirements]",
]
_KNOWN_PROVIDER_ERROR_CODES = frozenset(
    {
        "provider_error",
        "provider_config_error",
        "provider_authentication_error",
        "provider_rate_limit_error",
        "provider_timeout_error",
        "provider_model_not_found",
        "provider_http_error",
        "provider_protocol_error",
        "provider_circuit_open",
        "model_attempt_budget_exceeded",
    }
)


@dataclass(frozen=True, slots=True)
class ModelCandidate:
    """One concrete model/provider runtime and its trusted routing metadata."""

    model: Model
    stream_fn: StreamFn = field(repr=False, compare=False)
    capabilities: frozenset[str] = frozenset()
    quality_score: float = 0.5
    input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0
    expected_latency_ms: float = 0.0
    candidate_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.model, Model):
            raise TypeError("ModelCandidate.model 必须是 Model")
        if not self.model.id or not self.model.provider:
            raise ValueError("候选模型必须包含非空 model.id 和 provider")
        if not callable(self.stream_fn):
            raise TypeError("ModelCandidate.stream_fn 必须可调用")
        normalized_capabilities: set[str] = set()
        for capability in self.capabilities:
            if not isinstance(capability, str) or not capability.strip():
                raise ValueError("候选 capability 必须是非空字符串")
            normalized_capabilities.add(capability.strip())
        if self.model.reasoning:
            normalized_capabilities.add("reasoning")
        object.__setattr__(self, "capabilities", frozenset(normalized_capabilities))
        _finite_range("quality_score", self.quality_score, minimum=0.0, maximum=1.0)
        for name in (
            "input_cost_per_million",
            "output_cost_per_million",
            "expected_latency_ms",
        ):
            _finite_range(name, getattr(self, name), minimum=0.0)
        if self.candidate_id is not None:
            if (
                not isinstance(self.candidate_id, str)
                or not self.candidate_id.strip()
                or len(self.candidate_id) > 256
            ):
                raise ValueError("candidate_id 必须是非空短字符串或 None")
            object.__setattr__(self, "candidate_id", self.candidate_id.strip())

    @property
    def key(self) -> str:
        return self.candidate_id or f"{self.model.provider}:{self.model.id}"

    def estimated_cost(self, requirements: "TaskRequirements") -> float:
        if requirements.expected_input_tokens or requirements.expected_output_tokens:
            return (
                self.input_cost_per_million * requirements.expected_input_tokens
                + self.output_cost_per_million * requirements.expected_output_tokens
            ) / 1_000_000
        # Without a task token estimate, rate itself remains a deterministic
        # relative cost signal instead of incorrectly treating every model as 0.
        return self.input_cost_per_million + self.output_cost_per_million


@dataclass(frozen=True, slots=True)
class TaskRequirements:
    """Hard task constraints used before any provider is contacted."""

    required_capabilities: frozenset[str] = frozenset()
    required_context_tokens: int = 0
    minimum_quality_score: float = 0.0
    maximum_estimated_cost: float | None = None
    maximum_latency_ms: float | None = None
    expected_input_tokens: int = 0
    expected_output_tokens: int = 0

    def __post_init__(self) -> None:
        capabilities: set[str] = set()
        for capability in self.required_capabilities:
            if not isinstance(capability, str) or not capability.strip():
                raise ValueError("required_capabilities 必须包含非空字符串")
            capabilities.add(capability.strip())
        object.__setattr__(self, "required_capabilities", frozenset(capabilities))
        for name in (
            "required_context_tokens",
            "expected_input_tokens",
            "expected_output_tokens",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数")
        _finite_range(
            "minimum_quality_score",
            self.minimum_quality_score,
            minimum=0.0,
            maximum=1.0,
        )
        for name in ("maximum_estimated_cost", "maximum_latency_ms"):
            value = getattr(self, name)
            if value is not None:
                _finite_range(name, value, minimum=0.0)


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    candidate_id: str
    status: HealthStatus = "closed"
    consecutive_failures: int = 0
    probe_in_flight: bool = False


@dataclass(frozen=True, slots=True)
class SelectionPolicy:
    """Weighted deterministic ranking after hard capability/context filters."""

    quality_weight: float = 1.0
    cost_weight: float = 0.25
    latency_weight: float = 0.15
    half_open_penalty: float = 0.1

    def __post_init__(self) -> None:
        for name in (
            "quality_weight",
            "cost_weight",
            "latency_weight",
            "half_open_penalty",
        ):
            _finite_range(name, getattr(self, name), minimum=0.0)
        if not any(
            value > 0
            for value in (
                self.quality_weight,
                self.cost_weight,
                self.latency_weight,
            )
        ):
            raise ValueError("质量、成本和延迟权重不能全部为 0")

    def rank(
        self,
        candidates: Sequence[ModelCandidate],
        requirements: TaskRequirements,
        health: Mapping[str, HealthSnapshot] | None = None,
    ) -> tuple[ModelCandidate, ...]:
        """Return a stable best-first order; no randomness or mutable tie-breaks."""

        health = health or {}
        eligible: list[tuple[ModelCandidate, float, HealthSnapshot]] = []
        for candidate in candidates:
            snapshot = health.get(
                candidate.key,
                HealthSnapshot(candidate_id=candidate.key),
            )
            if snapshot.status == "open":
                continue
            if not requirements.required_capabilities.issubset(
                candidate.capabilities
            ):
                continue
            if requirements.required_context_tokens > 0 and (
                candidate.model.context_window <= 0
                or candidate.model.context_window
                < requirements.required_context_tokens
            ):
                continue
            if candidate.quality_score < requirements.minimum_quality_score:
                continue
            estimated_cost = candidate.estimated_cost(requirements)
            if (
                requirements.maximum_estimated_cost is not None
                and estimated_cost > requirements.maximum_estimated_cost
            ):
                continue
            if (
                requirements.maximum_latency_ms is not None
                and candidate.expected_latency_ms > requirements.maximum_latency_ms
            ):
                continue
            eligible.append((candidate, estimated_cost, snapshot))
        if not eligible:
            return ()

        maximum_cost = max(cost for _candidate, cost, _snapshot in eligible) or 1.0
        maximum_latency = (
            max(candidate.expected_latency_ms for candidate, _cost, _snapshot in eligible)
            or 1.0
        )

        def sort_key(
            item: tuple[ModelCandidate, float, HealthSnapshot],
        ) -> tuple[float, float, float, float, str, str]:
            candidate, estimated_cost, snapshot = item
            score = (
                self.quality_weight * candidate.quality_score
                - self.cost_weight * (estimated_cost / maximum_cost)
                - self.latency_weight
                * (candidate.expected_latency_ms / maximum_latency)
                - (
                    self.half_open_penalty
                    if snapshot.status == "half_open"
                    else 0.0
                )
            )
            return (
                -score,
                -candidate.quality_score,
                estimated_cost,
                candidate.expected_latency_ms,
                candidate.model.provider,
                candidate.model.id,
            )

        return tuple(item[0] for item in sorted(eligible, key=sort_key))


@dataclass(slots=True)
class _MutableHealth:
    status: HealthStatus = "closed"
    consecutive_failures: int = 0
    opened_at: float | None = None
    probe_in_flight: bool = False


class HealthRegistry:
    """Coroutine-safe closed/open/half-open health state per candidate."""

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            isinstance(failure_threshold, bool)
            or not isinstance(failure_threshold, int)
            or failure_threshold <= 0
        ):
            raise ValueError("failure_threshold 必须是正整数")
        _finite_range("cooldown_seconds", cooldown_seconds, minimum=0.0)
        if not callable(clock):
            raise TypeError("clock 必须可调用")
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = float(cooldown_seconds)
        self._clock = clock
        self._states: dict[str, _MutableHealth] = {}
        self._lock = asyncio.Lock()

    async def snapshots(
        self,
        candidate_ids: Sequence[str],
    ) -> dict[str, HealthSnapshot]:
        async with self._lock:
            now = float(self._clock())
            return {
                candidate_id: self._snapshot(
                    candidate_id,
                    self._refresh(candidate_id, now),
                )
                for candidate_id in candidate_ids
            }

    async def snapshot(self, candidate_id: str) -> HealthSnapshot:
        return (await self.snapshots((candidate_id,)))[candidate_id]

    async def try_acquire(self, candidate_id: str) -> bool:
        _validate_candidate_id(candidate_id)
        async with self._lock:
            state = self._refresh(candidate_id, float(self._clock()))
            if state.status == "open":
                return False
            if state.status == "half_open":
                if state.probe_in_flight:
                    return False
                state.probe_in_flight = True
            return True

    async def record_success(self, candidate_id: str) -> None:
        _validate_candidate_id(candidate_id)
        async with self._lock:
            state = self._states.setdefault(candidate_id, _MutableHealth())
            state.status = "closed"
            state.consecutive_failures = 0
            state.opened_at = None
            state.probe_in_flight = False

    async def record_failure(self, candidate_id: str) -> None:
        _validate_candidate_id(candidate_id)
        async with self._lock:
            now = float(self._clock())
            state = self._refresh(candidate_id, now)
            if state.status == "half_open":
                state.consecutive_failures = max(
                    state.consecutive_failures + 1,
                    self.failure_threshold,
                )
                self._open(state, now)
                return
            if state.status == "open":
                self._open(state, now)
                return
            state.consecutive_failures += 1
            state.probe_in_flight = False
            if state.consecutive_failures >= self.failure_threshold:
                self._open(state, now)

    async def record_cancelled(self, candidate_id: str) -> None:
        """Release a half-open probe without treating caller cancellation as failure."""

        _validate_candidate_id(candidate_id)
        async with self._lock:
            state = self._states.setdefault(candidate_id, _MutableHealth())
            state.probe_in_flight = False

    def _refresh(self, candidate_id: str, now: float) -> _MutableHealth:
        _validate_candidate_id(candidate_id)
        state = self._states.setdefault(candidate_id, _MutableHealth())
        if (
            state.status == "open"
            and state.opened_at is not None
            and now - state.opened_at >= self.cooldown_seconds
        ):
            state.status = "half_open"
            state.probe_in_flight = False
        return state

    @staticmethod
    def _snapshot(candidate_id: str, state: _MutableHealth) -> HealthSnapshot:
        return HealthSnapshot(
            candidate_id=candidate_id,
            status=state.status,
            consecutive_failures=state.consecutive_failures,
            probe_in_flight=state.probe_in_flight,
        )

    @staticmethod
    def _open(state: _MutableHealth, now: float) -> None:
        state.status = "open"
        state.opened_at = now
        state.probe_in_flight = False


@dataclass(frozen=True, slots=True)
class ModelAttemptTelemetry:
    attempt: int
    candidate_id: str
    provider: str
    model: str
    outcome: AttemptOutcome
    reason: str
    duration_ms: float
    visible_output: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "candidateId": self.candidate_id,
            "provider": self.provider,
            "model": self.model,
            "outcome": self.outcome,
            "reason": self.reason,
            "durationMs": round(self.duration_ms, 3),
            "visibleOutput": self.visible_output,
        }


@dataclass(frozen=True, slots=True)
class ModelRoutingTelemetry:
    routing_id: str
    attempts: tuple[ModelAttemptTelemetry, ...]
    selected_candidate_id: str | None
    selected_provider: str | None
    selected_model: str | None
    terminal_reason: str
    duration_ms: float

    def to_dict(self) -> dict[str, Any]:
        selected = None
        if self.selected_candidate_id is not None:
            selected = {
                "candidateId": self.selected_candidate_id,
                "provider": self.selected_provider,
                "model": self.selected_model,
            }
        return {
            "routingId": self.routing_id,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "selected": selected,
            "terminalReason": self.terminal_reason,
            "durationMs": round(self.duration_ms, 3),
        }


class _RoutingDeadlineExceeded(TimeoutError):
    pass


@dataclass(slots=True)
class _AttemptResult:
    buffered_events: list[dict[str, Any]]
    final: dict[str, Any] | None
    terminal_type: Literal["done", "error"] | None
    visible_output: bool
    error: BaseException | None = None


class ResilientModelRouter:
    """Agent-compatible StreamFn with deterministic selection and safe failover."""

    def __init__(
        self,
        candidates: Sequence[ModelCandidate],
        *,
        requirements: TaskRequirements | None = None,
        requirements_resolver: RequirementsResolver | None = None,
        selection_policy: SelectionPolicy | None = None,
        health_registry: HealthRegistry | None = None,
        max_attempts: int = 3,
        max_elapsed_seconds: float = 60.0,
        telemetry_sink: TelemetrySink | None = None,
        telemetry_timeout_seconds: float = 0.1,
        telemetry_history_size: int = 256,
        max_buffered_events: int = 1024,
        max_buffered_bytes: int = 4 * 1024 * 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not candidates:
            raise ValueError("ResilientModelRouter 至少需要一个候选模型")
        self.candidates = tuple(candidates)
        if any(not isinstance(candidate, ModelCandidate) for candidate in self.candidates):
            raise TypeError("candidates 必须全部是 ModelCandidate")
        keys = [candidate.key for candidate in self.candidates]
        if len(keys) != len(set(keys)):
            raise ValueError("ModelCandidate key 不能重复")
        if requirements is not None and not isinstance(requirements, TaskRequirements):
            raise TypeError("requirements 必须是 TaskRequirements 或 None")
        if requirements_resolver is not None and not callable(requirements_resolver):
            raise TypeError("requirements_resolver 必须可调用或为 None")
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or max_attempts <= 0
        ):
            raise ValueError("max_attempts 必须是正整数")
        _finite_range("max_elapsed_seconds", max_elapsed_seconds, minimum=0.000001)
        _finite_range(
            "telemetry_timeout_seconds",
            telemetry_timeout_seconds,
            minimum=0.000001,
        )
        for name, value, minimum in (
            ("telemetry_history_size", telemetry_history_size, 1),
            ("max_buffered_events", max_buffered_events, 1),
            ("max_buffered_bytes", max_buffered_bytes, 1),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} 必须是不小于 {minimum} 的整数")
        if telemetry_sink is not None and not callable(telemetry_sink):
            raise TypeError("telemetry_sink 必须可调用或为 None")
        if not callable(clock):
            raise TypeError("clock 必须可调用")
        self.requirements = requirements or TaskRequirements()
        self.requirements_resolver = requirements_resolver
        self.selection_policy = selection_policy or SelectionPolicy()
        self.health_registry = health_registry or HealthRegistry()
        self.max_attempts = max_attempts
        self.max_elapsed_seconds = float(max_elapsed_seconds)
        self.telemetry_sink = telemetry_sink
        self.telemetry_timeout_seconds = float(telemetry_timeout_seconds)
        self.max_buffered_events = max_buffered_events
        self.max_buffered_bytes = max_buffered_bytes
        self._clock = clock
        self._telemetry_history: deque[ModelRoutingTelemetry] = deque(
            maxlen=telemetry_history_size
        )
        self._detached_operations: set[asyncio.Future[Any]] = set()
        self._telemetry_task: asyncio.Task[None] | None = None

    @property
    def telemetry_history(self) -> tuple[ModelRoutingTelemetry, ...]:
        return tuple(self._telemetry_history)

    @property
    def last_telemetry(self) -> ModelRoutingTelemetry | None:
        return self._telemetry_history[-1] if self._telemetry_history else None

    def __call__(
        self,
        model: Model,
        context: dict[str, Any],
        options: dict[str, Any],
    ) -> AssistantMessageEventStream:
        return self.stream(model, context, options)

    def stream(
        self,
        model: Model,
        context: dict[str, Any],
        options: dict[str, Any],
    ) -> AssistantMessageEventStream:
        output = ProducerOwnedAssistantMessageEventStream()
        task = asyncio.create_task(
            self._run(output, model, context, dict(options)),
            name=f"pi-model-route:{model.provider}:{model.id}",
        )
        bind_stream_producer(output, task)
        return output

    async def _run(
        self,
        output: AssistantMessageEventStream,
        requested_model: Model,
        context: dict[str, Any],
        options: dict[str, Any],
    ) -> None:
        routing_id = str(uuid4())
        started_at = float(self._clock())
        deadline = started_at + self.max_elapsed_seconds
        attempts: list[ModelAttemptTelemetry] = []
        cancellation = options.get("cancellation_token")
        if not isinstance(cancellation, CancellationToken):
            cancellation = None
        try:
            if cancellation is not None:
                cancellation.throw_if_cancelled()
            attempt_limit = min(
                self.max_attempts,
                _bounded_option(options, "model_route_max_attempts", self.max_attempts),
            )
            elapsed_limit = min(
                self.max_elapsed_seconds,
                _bounded_float_option(
                    options,
                    "model_route_deadline_seconds",
                    self.max_elapsed_seconds,
                ),
            )
            deadline = started_at + elapsed_limit
            if self.requirements_resolver is not None:
                requirements = await self._await_guarded(
                    self._resolve_requirements(context, options),
                    cancellation,
                    deadline,
                )
            else:
                requirements = await self._resolve_requirements(context, options)
            health = await self.health_registry.snapshots(
                [candidate.key for candidate in self.candidates]
            )
            ranked = self.selection_policy.rank(
                self.candidates,
                requirements,
                health,
            )
            deadline_exceeded = False
            for candidate in ranked:
                if len(attempts) >= attempt_limit:
                    break
                if cancellation is not None:
                    cancellation.throw_if_cancelled()
                if float(self._clock()) >= deadline:
                    deadline_exceeded = True
                    break
                if not await self.health_registry.try_acquire(candidate.key):
                    continue
                attempt_started = float(self._clock())
                result = await self._one_attempt(
                    output,
                    candidate,
                    context,
                    options,
                    cancellation,
                    deadline,
                )
                duration_ms = max(
                    0.0,
                    (float(self._clock()) - attempt_started) * 1000,
                )
                outcome, reason, retryable = _attempt_outcome(result, cancellation)
                attempt = ModelAttemptTelemetry(
                    attempt=len(attempts) + 1,
                    candidate_id=candidate.key,
                    provider=candidate.model.provider,
                    model=candidate.model.id,
                    outcome=outcome,
                    reason=reason,
                    duration_ms=duration_ms,
                    visible_output=result.visible_output,
                )
                attempts.append(attempt)
                await self._emit_telemetry(
                    {
                        "type": "model_route_attempt",
                        "routingId": routing_id,
                        **attempt.to_dict(),
                    },
                    deadline=deadline,
                )

                if outcome == "success":
                    await self.health_registry.record_success(candidate.key)
                    assert result.final is not None
                    await self._finish_with_final(
                        output,
                        routing_id,
                        started_at,
                        attempts,
                        candidate,
                        "completed",
                        result.buffered_events,
                        result.final,
                        "done",
                        deadline=deadline,
                    )
                    return
                if outcome == "cancelled":
                    await self.health_registry.record_cancelled(candidate.key)
                    await self._finish_with_generated_error(
                        output,
                        routing_id,
                        started_at,
                        attempts,
                        candidate,
                        "cancelled",
                        "模型路由已取消",
                        "model_route_cancelled",
                        aborted=True,
                        buffered_events=(
                            result.buffered_events if result.visible_output else []
                        ),
                        deadline=deadline,
                    )
                    return
                await self.health_registry.record_failure(candidate.key)
                if outcome == "deadline_exceeded":
                    deadline_exceeded = True
                    if result.visible_output:
                        await self._finish_with_generated_error(
                            output,
                            routing_id,
                            started_at,
                            attempts,
                            candidate,
                            "deadline_exceeded",
                            "模型路由超过总 deadline",
                            "model_route_deadline_exceeded",
                            buffered_events=result.buffered_events,
                            deadline=deadline,
                        )
                        return
                    break
                if result.visible_output or not retryable:
                    if result.final is None:
                        await self._finish_with_generated_error(
                            output,
                            routing_id,
                            started_at,
                            attempts,
                            candidate,
                            reason,
                            "模型 Provider 调用失败",
                            "model_provider_failure",
                            buffered_events=result.buffered_events,
                            deadline=deadline,
                        )
                    else:
                        await self._finish_with_final(
                            output,
                            routing_id,
                            started_at,
                            attempts,
                            candidate,
                            reason,
                            result.buffered_events,
                            _sanitize_failed_final(result.final),
                            "error",
                            deadline=deadline,
                        )
                    return
                if deadline_exceeded:
                    break

            terminal_reason = (
                "deadline_exceeded"
                if deadline_exceeded or float(self._clock()) >= deadline
                else "attempts_exhausted"
                if attempts
                else "no_eligible_candidate"
            )
            code = (
                "model_route_deadline_exceeded"
                if terminal_reason == "deadline_exceeded"
                else "model_failover_exhausted"
                if attempts
                else "model_selection_unavailable"
            )
            await self._finish_with_generated_error(
                output,
                routing_id,
                started_at,
                attempts,
                None,
                terminal_reason,
                "没有候选模型能够在安全预算内完成请求",
                code,
                fallback_model=requested_model,
                deadline=deadline,
            )
        except _RoutingDeadlineExceeded:
            await self._finish_with_generated_error(
                output,
                routing_id,
                started_at,
                attempts,
                None,
                "deadline_exceeded",
                "模型路由超过总 deadline",
                "model_route_deadline_exceeded",
                fallback_model=requested_model,
                deadline=deadline,
            )
        except OperationCancelledError:
            await self._finish_with_generated_error(
                output,
                routing_id,
                started_at,
                attempts,
                None,
                "cancelled",
                "模型路由已取消",
                "model_route_cancelled",
                fallback_model=requested_model,
                aborted=True,
                deadline=deadline,
            )
        except asyncio.CancelledError:
            raise
        except BaseException:
            await self._finish_with_generated_error(
                output,
                routing_id,
                started_at,
                attempts,
                None,
                "routing_error",
                "模型路由发生内部错误",
                "model_routing_error",
                fallback_model=requested_model,
                deadline=deadline,
            )

    async def _resolve_requirements(
        self,
        context: dict[str, Any],
        options: dict[str, Any],
    ) -> TaskRequirements:
        explicit = options.get("task_requirements")
        if explicit is not None:
            if not isinstance(explicit, TaskRequirements):
                raise TypeError("task_requirements 必须是 TaskRequirements")
            requirements = explicit
        elif self.requirements_resolver is not None:
            resolver = self.requirements_resolver
            if inspect.iscoroutinefunction(resolver):
                value = resolver(context, options)
            else:
                # A synchronous resolver is untrusted integration code.  Keep
                # it off the event loop so the outer route deadline can fire.
                sync_resolver = cast(
                    Callable[[dict[str, Any], dict[str, Any]], Any],
                    resolver,
                )
                value = await asyncio.to_thread(
                    sync_resolver,
                    context,
                    options,
                )
            requirements = await value if inspect.isawaitable(value) else value
            if not isinstance(requirements, TaskRequirements):
                raise TypeError("requirements_resolver 必须返回 TaskRequirements")
        else:
            requirements = self.requirements

        inferred = set(requirements.required_capabilities)
        if _context_has_images(context):
            inferred.add("vision")
        if context.get("tools"):
            inferred.add("tools")
        if options.get("reasoning") not in {None, "", "off"}:
            inferred.add("reasoning")
        estimated_context = options.get("estimated_context_tokens", 0)
        if isinstance(estimated_context, bool) or not isinstance(estimated_context, int):
            raise TypeError("estimated_context_tokens 必须是非负整数")
        if estimated_context < 0:
            raise ValueError("estimated_context_tokens 必须是非负整数")
        return replace(
            requirements,
            required_capabilities=frozenset(inferred),
            required_context_tokens=max(
                requirements.required_context_tokens,
                estimated_context,
            ),
        )

    async def _one_attempt(
        self,
        output: AssistantMessageEventStream,
        candidate: ModelCandidate,
        context: dict[str, Any],
        options: dict[str, Any],
        cancellation: CancellationToken | None,
        deadline: float,
    ) -> _AttemptResult:
        stream: Any = None
        buffered: list[dict[str, Any]] = []
        buffered_bytes = 0
        visible = False
        completed = False
        try:
            value = candidate.stream_fn(
                candidate.model,
                copy.deepcopy(context),
                dict(options),
            )
            stream = (
                await self._await_guarded(
                    value,
                    cancellation,
                    deadline,
                )
                if inspect.isawaitable(value)
                else value
            )
            if not hasattr(stream, "__aiter__") or not hasattr(stream, "result"):
                raise TypeError("候选 stream_fn 未返回 AssistantMessageEventStream")
            iterator = stream.__aiter__()
            while True:
                try:
                    event = await self._await_guarded(
                        iterator.__anext__(),
                        cancellation,
                        deadline,
                    )
                except StopAsyncIteration:
                    raise RuntimeError("候选模型流未产生终止事件") from None
                if not isinstance(event, dict):
                    raise TypeError("候选模型流事件必须是 dict")
                event_type = event.get("type")
                if event_type in {"done", "error"}:
                    final = await self._await_guarded(
                        stream.result(),
                        cancellation,
                        deadline,
                    )
                    if not isinstance(final, dict):
                        raise TypeError("候选模型终止结果必须是 dict")
                    completed = True
                    return _AttemptResult(
                        buffered,
                        final,
                        cast(Literal["done", "error"], event_type),
                        visible,
                    )
                if visible:
                    output.push(copy.deepcopy(event))
                    continue
                event_copy = copy.deepcopy(event)
                buffered.append(event_copy)
                buffered_bytes += _event_bytes(event_copy)
                if (
                    len(buffered) > self.max_buffered_events
                    or buffered_bytes > self.max_buffered_bytes
                ):
                    raise RuntimeError("候选模型提交前事件缓冲超过限制")
                if _event_has_visible_output(event_copy):
                    visible = True
                    for buffered_event in buffered:
                        output.push(buffered_event)
                    buffered = []
            # Unreachable; the loop returns only on a terminal event.
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            return _AttemptResult(
                buffered,
                None,
                None,
                visible,
                error=error,
            )
        finally:
            if stream is not None:
                await settle_stream_producer(stream, cancel=not completed)

    async def _await_guarded(
        self,
        value: Awaitable[Any],
        cancellation: CancellationToken | None,
        deadline: float,
    ) -> Any:
        if cancellation is not None:
            cancellation.throw_if_cancelled()
        remaining = deadline - float(self._clock())
        if remaining <= 0:
            if inspect.iscoroutine(value):
                value.close()
            raise _RoutingDeadlineExceeded("模型路由超过总 deadline")
        if len(self._detached_operations) >= 16:
            if inspect.iscoroutine(value):
                value.close()
            raise RuntimeError("模型路由未完成的外部任务超过安全上限")
        operation = asyncio.ensure_future(value)
        cancellation_waiter = (
            asyncio.create_task(cancellation.wait())
            if cancellation is not None
            else None
        )
        waiters = {operation}
        if cancellation_waiter is not None:
            waiters.add(cancellation_waiter)
        try:
            done, _pending = await asyncio.wait(
                waiters,
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if operation in done:
                return await operation
            operation.cancel()
            self._track_detached_operation(operation)
            if cancellation_waiter is not None and cancellation_waiter in done:
                assert cancellation is not None
                cancellation.throw_if_cancelled()
            raise _RoutingDeadlineExceeded("模型路由超过总 deadline")
        except asyncio.CancelledError:
            if not operation.done():
                operation.cancel()
                self._track_detached_operation(operation)
            raise
        finally:
            if cancellation_waiter is not None and not cancellation_waiter.done():
                cancellation_waiter.cancel()
            if cancellation_waiter is not None:
                await asyncio.gather(cancellation_waiter, return_exceptions=True)

    def _track_detached_operation(self, operation: asyncio.Future[Any]) -> None:
        if operation.done():
            _consume_future_exception(operation)
            return
        self._detached_operations.add(operation)

        def completed(done: asyncio.Future[Any]) -> None:
            self._detached_operations.discard(done)
            _consume_future_exception(done)

        operation.add_done_callback(completed)

    async def _finish_with_final(
        self,
        output: AssistantMessageEventStream,
        routing_id: str,
        started_at: float,
        attempts: list[ModelAttemptTelemetry],
        selected: ModelCandidate | None,
        terminal_reason: str,
        buffered_events: list[dict[str, Any]],
        final: dict[str, Any],
        terminal_type: Literal["done", "error"],
        *,
        deadline: float | None = None,
    ) -> None:
        telemetry = await self._complete_telemetry(
            routing_id,
            started_at,
            attempts,
            selected,
            terminal_reason,
            deadline=deadline,
        )
        final = copy.deepcopy(final)
        final["modelRouting"] = telemetry.to_dict()
        for event in buffered_events:
            output.push(copy.deepcopy(event))
        if terminal_type == "done":
            output.push(
                {
                    "type": "done",
                    "reason": final.get("stopReason", "stop"),
                    "message": final,
                }
            )
        else:
            output.push(
                {
                    "type": "error",
                    "reason": final.get("stopReason", "error"),
                    "error": final,
                }
            )

    async def _finish_with_generated_error(
        self,
        output: AssistantMessageEventStream,
        routing_id: str,
        started_at: float,
        attempts: list[ModelAttemptTelemetry],
        selected: ModelCandidate | None,
        terminal_reason: str,
        message: str,
        code: str,
        *,
        fallback_model: Model | None = None,
        aborted: bool = False,
        buffered_events: list[dict[str, Any]] | None = None,
        deadline: float | None = None,
    ) -> None:
        model = selected.model if selected is not None else fallback_model
        if model is None:
            model = self.candidates[0].model
        final = assistant_message(
            model=model,
            stop_reason="aborted" if aborted else "error",
            error_message=message,
        )
        final["providerError"] = {
            "code": code,
            "statusCode": None,
            "retryAfterMs": None,
            "retryable": False,
            "failures": [
                {
                    "attempt": attempt.attempt,
                    "candidateId": attempt.candidate_id,
                    "provider": attempt.provider,
                    "model": attempt.model,
                    "reason": attempt.reason,
                    "outcome": attempt.outcome,
                }
                for attempt in attempts
            ],
        }
        await self._finish_with_final(
            output,
            routing_id,
            started_at,
            attempts,
            selected,
            terminal_reason,
            buffered_events or [],
            final,
            "error",
            deadline=deadline,
        )

    async def _complete_telemetry(
        self,
        routing_id: str,
        started_at: float,
        attempts: list[ModelAttemptTelemetry],
        selected: ModelCandidate | None,
        terminal_reason: str,
        *,
        deadline: float | None = None,
    ) -> ModelRoutingTelemetry:
        telemetry = ModelRoutingTelemetry(
            routing_id=routing_id,
            attempts=tuple(attempts),
            selected_candidate_id=selected.key if selected is not None else None,
            selected_provider=(
                selected.model.provider if selected is not None else None
            ),
            selected_model=selected.model.id if selected is not None else None,
            terminal_reason=terminal_reason,
            duration_ms=max(0.0, (float(self._clock()) - started_at) * 1000),
        )
        self._telemetry_history.append(telemetry)
        await self._emit_telemetry(
            {
                "type": "model_route_finished",
                **telemetry.to_dict(),
            },
            deadline=deadline,
        )
        return telemetry

    async def _emit_telemetry(
        self,
        event: dict[str, Any],
        *,
        deadline: float | None = None,
    ) -> None:
        if self.telemetry_sink is None:
            return
        timeout = self.telemetry_timeout_seconds
        if deadline is not None:
            timeout = min(timeout, deadline - float(self._clock()))
        if timeout <= 0:
            return
        active = self._telemetry_task
        if active is not None and not active.done():
            return

        async def invoke() -> None:
            assert self.telemetry_sink is not None
            sink = self.telemetry_sink
            snapshot = copy.deepcopy(event)
            if inspect.iscoroutinefunction(sink):
                value = sink(snapshot)
            else:
                # A blocking observer must never stall routing or its deadline.
                value = await asyncio.to_thread(sink, snapshot)
            if inspect.isawaitable(value):
                await value

        task = asyncio.create_task(invoke(), name="model-routing-telemetry")
        self._telemetry_task = task

        def completed(done: asyncio.Future[None]) -> None:
            if self._telemetry_task is done:
                self._telemetry_task = None
            _consume_future_exception(done)

        task.add_done_callback(completed)
        try:
            done, _pending = await asyncio.wait({task}, timeout=timeout)
            if task in done:
                task.result()
        except Exception:
            # Observability is isolated from model execution.
            return


def _attempt_outcome(
    result: _AttemptResult,
    cancellation: CancellationToken | None,
) -> tuple[AttemptOutcome, str, bool]:
    if cancellation is not None and cancellation.cancelled:
        return "cancelled", "cancelled", False
    if result.error is not None:
        if isinstance(result.error, OperationCancelledError):
            return "cancelled", "cancelled", False
        if isinstance(result.error, (asyncio.CancelledError,)):
            return "cancelled", "cancelled", False
        if isinstance(result.error, _RoutingDeadlineExceeded):
            return "deadline_exceeded", "deadline_exceeded", False
        if isinstance(result.error, ProviderError):
            code = _safe_code(getattr(result.error, "code", None))
            retryable = result.error.retryable is True
            return (
                "retryable_error" if retryable else "non_retryable_error",
                code,
                retryable,
            )
        return "non_retryable_error", "provider_stream_contract_error", False
    final = result.final
    if final is None:
        return "non_retryable_error", "provider_result_missing", False
    stop_reason = final.get("stopReason")
    if stop_reason not in {"error", "aborted"}:
        return "success", "completed", False
    if stop_reason == "aborted":
        return "cancelled", "cancelled", False
    provider_error = final.get("providerError")
    code = "provider_error"
    retryable = False
    if isinstance(provider_error, Mapping):
        code = _safe_code(provider_error.get("code"))
        retryable = provider_error.get("retryable") is True
    return (
        "retryable_error" if retryable else "non_retryable_error",
        code,
        retryable,
    )


def _consume_future_exception(future: asyncio.Future[Any]) -> None:
    try:
        future.exception()
    except asyncio.CancelledError:
        pass


def _sanitize_failed_final(final: dict[str, Any]) -> dict[str, Any]:
    sanitized = redact_sensitive_data(final)
    if not isinstance(sanitized, dict):
        raise TypeError("Provider failure must sanitize to a dict")
    if isinstance(sanitized.get("errorMessage"), str):
        sanitized["errorMessage"] = public_error_message(
            sanitized["errorMessage"],
            fallback="模型 Provider 调用失败",
        )
    provider_error = sanitized.get("providerError")
    if isinstance(provider_error, dict):
        sanitized["providerError"] = {
            "code": _safe_code(provider_error.get("code")),
            "statusCode": (
                provider_error.get("statusCode")
                if isinstance(provider_error.get("statusCode"), int)
                else None
            ),
            "retryAfterMs": (
                provider_error.get("retryAfterMs")
                if isinstance(provider_error.get("retryAfterMs"), int)
                else None
            ),
            "retryable": provider_error.get("retryable") is True,
        }
    return sanitized


def _event_has_visible_output(event: Mapping[str, Any]) -> bool:
    event_type = event.get("type")
    if event_type in {"toolcall_start", "toolcall_delta", "toolcall_end"}:
        return True
    if event_type == "text_delta":
        return bool(event.get("delta"))
    if event_type == "text_end":
        return bool(event.get("content"))
    return False


def _context_has_images(context: Mapping[str, Any]) -> bool:
    messages = context.get("messages")
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        if any(
            isinstance(block, Mapping) and block.get("type") == "image_url"
            for block in content
        ):
            return True
    return False


def _event_bytes(event: Mapping[str, Any]) -> int:
    try:
        return len(
            json.dumps(
                event,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
    except (TypeError, ValueError):
        return 2**63 - 1


def _safe_code(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "provider_error"
    normalized = "".join(
        character
        for character in value.casefold()
        if character.isalnum() or character in {"_", "-"}
    )
    return normalized if normalized in _KNOWN_PROVIDER_ERROR_CODES else "provider_error"


def _bounded_option(options: Mapping[str, Any], name: str, default: int) -> int:
    value = options.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} 必须是正整数")
    return int(value)


def _bounded_float_option(
    options: Mapping[str, Any],
    name: str,
    default: float,
) -> float:
    value = options.get(name, default)
    _finite_range(name, value, minimum=0.000001)
    return float(value)


def _finite_range(
    name: str,
    value: Any,
    *,
    minimum: float,
    maximum: float | None = None,
) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} 必须是有限数字")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise ValueError(f"{name} 超出允许范围")
    if maximum is not None and number > maximum:
        raise ValueError(f"{name} 超出允许范围")


def _validate_candidate_id(candidate_id: str) -> None:
    if (
        not isinstance(candidate_id, str)
        or not candidate_id
        or len(candidate_id) > 256
    ):
        raise ValueError("candidate_id 必须是非空短字符串")


__all__ = [
    "HealthRegistry",
    "HealthSnapshot",
    "ModelAttemptTelemetry",
    "ModelCandidate",
    "ModelRoutingTelemetry",
    "ResilientModelRouter",
    "SelectionPolicy",
    "TaskRequirements",
]
