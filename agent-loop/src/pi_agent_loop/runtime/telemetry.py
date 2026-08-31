"""Small dependency-free production telemetry primitives.

The module deliberately exposes sinks instead of selecting a vendor. Applications
can bridge the emitted metrics, spans, structured logs and alerts to OpenTelemetry,
Prometheus, an APM product or their existing logging stack.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import math
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast
from uuid import uuid4

from ..messages import is_sensitive_key, redact_sensitive_text

TelemetrySink = Callable[[dict[str, Any]], Any]
MetricKind = Literal["counter", "gauge", "histogram"]

DEFAULT_MAX_METRIC_SERIES_PER_KIND = 4_096
DEFAULT_MAX_METRIC_LABELS = 16
DEFAULT_MAX_METRIC_LABEL_LENGTH = 256
DEFAULT_MAX_SPAN_EVENTS = 256
DEFAULT_EXPORT_TIMEOUT_SECONDS = 1.0

_SENSITIVE_PARTS = (
    "authorization",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "password",
    "secret",
    "cookie",
)


async def _invoke_sink(sink: TelemetrySink, payload: dict[str, Any]) -> Any:
    """Invoke arbitrary sinks without letting synchronous code block the loop."""

    if inspect.iscoroutinefunction(sink):
        return await cast(Callable[[dict[str, Any]], Awaitable[Any]], sink)(payload)
    value = await asyncio.to_thread(sink, payload)
    if inspect.isawaitable(value):
        return await cast(Awaitable[Any], value)
    return value


def redact_telemetry_fields(value: Any) -> Any:
    """Recursively redact common secret-bearing fields before export."""

    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = key.casefold().replace("-", "_")
            if is_sensitive_key(key) or any(
                part in normalized for part in _SENSITIVE_PARTS
            ):
                output[key] = "[REDACTED]"
            else:
                output[key] = redact_telemetry_fields(item)
        return output
    if isinstance(value, (list, tuple)):
        return [redact_telemetry_fields(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value, replacement="[REDACTED]")
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    # Avoid invoking arbitrary repr implementations that may expose credentials.
    return f"<{type(value).__name__}>"


def _labels_key(
    labels: Mapping[str, Any] | None,
    *,
    max_labels: int,
    max_label_length: int,
) -> tuple[tuple[str, str], ...]:
    if not labels:
        return ()
    pairs = sorted(
        (
            str(key)[:max_label_length],
            str(value)[:max_label_length],
        )
        for key, value in labels.items()
    )
    if len(pairs) > max_labels:
        # Keep memory bounded without making observability input capable of
        # failing the workload.  The marker also makes the lossy projection
        # visible to exporters.  A bounded digest prevents different omitted
        # label sets from silently collapsing into the same admitted series.
        omitted_digest = hashlib.sha256(repr(pairs).encode("utf-8")).hexdigest()[
            : min(16, max_label_length)
        ]
        pairs = pairs[: max_labels - 1] + [
            ("labels_truncated"[:max_label_length], omitted_digest)
        ]
    return tuple(pairs)


@dataclass(frozen=True, slots=True)
class HistogramSnapshot:
    count: int
    total: float
    minimum: float | None
    maximum: float | None


@dataclass(slots=True)
class _Histogram:
    count: int = 0
    total: float = 0.0
    minimum: float | None = None
    maximum: float | None = None

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)


class MetricRegistry:
    """Thread-safe counters, gauges and lightweight histogram summaries."""

    def __init__(
        self,
        *,
        max_series_per_kind: int = DEFAULT_MAX_METRIC_SERIES_PER_KIND,
        max_labels_per_series: int = DEFAULT_MAX_METRIC_LABELS,
        max_label_length: int = DEFAULT_MAX_METRIC_LABEL_LENGTH,
    ) -> None:
        self.max_series_per_kind = _positive_int(
            max_series_per_kind,
            "max_series_per_kind",
        )
        self.max_labels_per_series = _positive_int(
            max_labels_per_series,
            "max_labels_per_series",
        )
        self.max_label_length = _positive_int(
            max_label_length,
            "max_label_length",
        )
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._histograms: dict[
            tuple[str, tuple[tuple[str, str], ...]], _Histogram
        ] = {}
        self._dropped_series: dict[MetricKind, int] = {
            "counter": 0,
            "gauge": 0,
            "histogram": 0,
        }

    def _key(
        self,
        name: str,
        labels: Mapping[str, Any] | None,
    ) -> tuple[str, tuple[tuple[str, str], ...]]:
        return (
            _metric_name(name),
            _labels_key(
                labels,
                max_labels=self.max_labels_per_series,
                max_label_length=self.max_label_length,
            ),
        )

    def _admit_series(
        self,
        values: Mapping[tuple[str, tuple[tuple[str, str], ...]], Any],
        key: tuple[str, tuple[tuple[str, str], ...]],
        kind: MetricKind,
    ) -> bool:
        if key in values:
            return True
        if len(values) < self.max_series_per_kind:
            return True
        self._dropped_series[kind] += 1
        return False

    def increment(
        self,
        name: str,
        value: float = 1.0,
        *,
        labels: Mapping[str, Any] | None = None,
    ) -> None:
        numeric = _finite(value, "counter value")
        if numeric < 0:
            raise ValueError("counter value cannot be negative")
        key = self._key(name, labels)
        with self._lock:
            if not self._admit_series(self._counters, key, "counter"):
                return
            self._counters[key] = self._counters.get(key, 0.0) + numeric

    def set_gauge(
        self,
        name: str,
        value: float,
        *,
        labels: Mapping[str, Any] | None = None,
    ) -> None:
        key = self._key(name, labels)
        with self._lock:
            if not self._admit_series(self._gauges, key, "gauge"):
                return
            self._gauges[key] = _finite(value, "gauge value")

    def observe(
        self,
        name: str,
        value: float,
        *,
        labels: Mapping[str, Any] | None = None,
    ) -> None:
        key = self._key(name, labels)
        numeric = _finite(value, "histogram value")
        with self._lock:
            if not self._admit_series(self._histograms, key, "histogram"):
                return
            histogram = self._histograms.setdefault(key, _Histogram())
            histogram.observe(numeric)

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            histograms = {
                key: HistogramSnapshot(
                    count=value.count,
                    total=value.total,
                    minimum=value.minimum,
                    maximum=value.maximum,
                )
                for key, value in self._histograms.items()
            }
            dropped_series = dict(self._dropped_series)
        counter_rows = _metric_rows(counters, "value")
        counter_rows.extend(
            {
                "name": "telemetry_metric_series_dropped_total",
                "labels": {"kind": kind},
                "value": float(count),
            }
            for kind, count in sorted(dropped_series.items())
            if count
        )
        counter_rows.sort(key=lambda row: (row["name"], sorted(row["labels"].items())))
        return {
            "counters": counter_rows,
            "gauges": _metric_rows(gauges, "value"),
            "histograms": [
                {
                    "name": name,
                    "labels": dict(labels),
                    "count": value.count,
                    "total": value.total,
                    "minimum": value.minimum,
                    "maximum": value.maximum,
                }
                for (name, labels), value in sorted(histograms.items())
            ],
        }


def _metric_rows(
    values: Mapping[tuple[str, tuple[tuple[str, str], ...]], float],
    value_name: str,
) -> list[dict[str, Any]]:
    return [
        {"name": name, "labels": dict(labels), value_name: value}
        for (name, labels), value in sorted(values.items())
    ]


def _metric_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("metric name cannot be empty")
    return name.strip()


def _finite(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be numeric")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(f"{label} must be finite")
    return numeric


def _positive_int(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _positive_float(value: float, label: str) -> float:
    numeric = _finite(value, label)
    if numeric <= 0:
        raise ValueError(f"{label} must be positive")
    return numeric


@dataclass(slots=True)
class TelemetrySpan:
    """One trace span. Call ``finish`` exactly once or use ``async with``."""

    telemetry: "Telemetry"
    name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    attributes: dict[str, Any]
    started_at: float = field(default_factory=time.monotonic)
    started_timestamp_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    events: list[dict[str, Any]] = field(default_factory=list)
    max_events: int = DEFAULT_MAX_SPAN_EVENTS
    _dropped_events: int = 0
    _finished: bool = False

    def add_event(self, name: str, **attributes: Any) -> None:
        if self._finished:
            return
        if len(self.events) >= self.max_events:
            self._dropped_events += 1
            return
        self.events.append(
            {
                "name": name,
                "timestamp": int(time.time() * 1000),
                "attributes": redact_telemetry_fields(attributes),
            }
        )

    async def finish(
        self,
        *,
        status: Literal["ok", "error", "cancelled"] = "ok",
        error: BaseException | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        if self._finished:
            return
        self._finished = True
        duration_ms = max(0.0, (time.monotonic() - self.started_at) * 1000)
        final_attributes = dict(self.attributes)
        if attributes:
            final_attributes.update(attributes)
        payload = {
            "type": "span",
            "name": self.name,
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "parentSpanId": self.parent_span_id,
            "startedAt": self.started_timestamp_ms,
            "durationMs": duration_ms,
            "status": status,
            "attributes": redact_telemetry_fields(final_attributes),
            "events": list(self.events),
            "droppedEvents": self._dropped_events,
        }
        if error is not None:
            payload["error"] = {
                "type": type(error).__name__,
            }
        self.telemetry.metrics.observe(
            "trace_span_duration_ms",
            duration_ms,
            labels={"span": self.name, "status": status},
        )
        await self.telemetry._export(self.telemetry.span_sink, payload)

    async def __aenter__(self) -> "TelemetrySpan":
        return self

    async def __aexit__(self, exc_type, exc, _traceback) -> bool:
        await self.finish(status="error" if exc is not None else "ok", error=exc)
        return False


class Telemetry:
    """Vendor-neutral metrics, tracing, structured logging and alert hooks."""

    def __init__(
        self,
        *,
        metrics: MetricRegistry | None = None,
        span_sink: TelemetrySink | None = None,
        log_sink: TelemetrySink | None = None,
        alert_hooks: tuple[TelemetrySink, ...] | list[TelemetrySink] = (),
        export_timeout_seconds: float = DEFAULT_EXPORT_TIMEOUT_SECONDS,
        max_span_events: int = DEFAULT_MAX_SPAN_EVENTS,
    ) -> None:
        self.metrics = metrics or MetricRegistry()
        self.span_sink = span_sink
        self.log_sink = log_sink
        self.alert_hooks = tuple(alert_hooks)
        self.export_timeout_seconds = _positive_float(
            export_timeout_seconds,
            "export_timeout_seconds",
        )
        self.max_span_events = _positive_int(max_span_events, "max_span_events")

    def start_span(
        self,
        name: str,
        *,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> TelemetrySpan:
        return TelemetrySpan(
            telemetry=self,
            name=name,
            trace_id=trace_id or uuid4().hex,
            span_id=uuid4().hex[:16],
            parent_span_id=parent_span_id,
            attributes=redact_telemetry_fields(dict(attributes or {})),
            max_events=self.max_span_events,
        )

    async def log(
        self,
        level: str,
        event: str,
        *,
        trace_id: str | None = None,
        span_id: str | None = None,
        **fields: Any,
    ) -> None:
        payload = {
            "type": "log",
            "timestamp": int(time.time() * 1000),
            "level": level,
            "event": event,
            "traceId": trace_id,
            "spanId": span_id,
            "fields": redact_telemetry_fields(fields),
        }
        await self._export(self.log_sink, payload)

    async def alert(
        self,
        name: str,
        *,
        severity: Literal["info", "warning", "critical"] = "warning",
        **fields: Any,
    ) -> None:
        self.metrics.increment(
            "alerts_total",
            labels={"name": name, "severity": severity},
        )
        payload = {
            "type": "alert",
            "timestamp": int(time.time() * 1000),
            "name": name,
            "severity": severity,
            "fields": redact_telemetry_fields(fields),
        }
        for hook in self.alert_hooks:
            await self._export(hook, payload)

    async def record_model_finished(
        self,
        *,
        provider: str,
        model: str,
        source: str,
        outcome: str,
        duration_ms: float,
        usage: Mapping[str, Any] | None,
    ) -> None:
        labels = {
            "provider": provider,
            "model": model,
            "source": source,
            "outcome": outcome,
        }
        self.metrics.increment("model_requests_total", labels=labels)
        self.metrics.observe("model_latency_ms", duration_ms, labels=labels)
        if usage:
            for source_key, metric in (
                ("input", "model_input_tokens_total"),
                ("output", "model_output_tokens_total"),
                ("cacheRead", "model_cache_read_tokens_total"),
                ("cacheWrite", "model_cache_write_tokens_total"),
                ("totalTokens", "model_tokens_total"),
            ):
                value = usage.get(source_key, 0)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                    self.metrics.increment(metric, float(value), labels=labels)
            cost = usage.get("cost")
            if isinstance(cost, Mapping):
                total_cost = cost.get("total", 0)
                if (
                    isinstance(total_cost, (int, float))
                    and not isinstance(total_cost, bool)
                    and total_cost >= 0
                ):
                    self.metrics.increment("model_cost_total", float(total_cost), labels=labels)

    def record_queue_depth(
        self,
        queue: str,
        *,
        events: int,
        retained_bytes: int,
    ) -> None:
        labels = {"queue": queue}
        self.metrics.set_gauge("queue_depth", events, labels=labels)
        self.metrics.set_gauge("queue_retained_bytes", retained_bytes, labels=labels)

    def record_backpressure(self, queue: str, payload: Mapping[str, Any]) -> None:
        action = str(payload.get("action", "unknown"))
        self.metrics.increment(
            "queue_backpressure_total",
            labels={"queue": queue, "action": action},
        )
        self.record_queue_depth(
            queue,
            events=int(payload.get("queuedEvents", 0)),
            retained_bytes=int(payload.get("queuedBytes", 0)),
        )

    def record_tool_finished(self, tool: str, *, outcome: str, duration_ms: float) -> None:
        labels = {"tool": tool, "outcome": outcome}
        self.metrics.increment("tool_calls_total", labels=labels)
        self.metrics.observe("tool_latency_ms", duration_ms, labels=labels)

    def record_approval_wait(self, *, outcome: str, duration_ms: float) -> None:
        self.metrics.observe(
            "approval_wait_ms",
            duration_ms,
            labels={"outcome": outcome},
        )

    def record_recovery(self, *, outcome: str, duration_ms: float) -> None:
        labels = {"outcome": outcome}
        self.metrics.increment("recovery_total", labels=labels)
        self.metrics.observe("recovery_duration_ms", duration_ms, labels=labels)

    def record_resource_lock_wait(self, resource: str, duration_ms: float) -> None:
        self.metrics.observe(
            "resource_lock_wait_ms",
            duration_ms,
            labels={"resource": resource},
        )

    async def _export(self, sink: TelemetrySink | None, payload: dict[str, Any]) -> None:
        if sink is None:
            return
        try:
            async with asyncio.timeout(self.export_timeout_seconds):
                await _invoke_sink(sink, redact_telemetry_fields(payload))
        except TimeoutError:
            self.metrics.increment(
                "telemetry_export_timeouts_total",
                labels={"channel": str(payload.get("type", "unknown"))},
            )
            self.metrics.increment(
                "telemetry_export_failures_total",
                labels={
                    "channel": str(payload.get("type", "unknown")),
                    "error_type": "TimeoutError",
                },
            )
            return
        except Exception as error:
            # Telemetry is deliberately failure-isolated from the workload. A
            # durable state sink belongs at the runtime boundary, not here. Keep
            # a local, dependency-free signal so exporter failures do not become
            # an invisible observability black hole.
            self.metrics.increment(
                "telemetry_export_failures_total",
                labels={
                    "channel": str(payload.get("type", "unknown")),
                    "error_type": type(error).__name__,
                },
            )
            return


class InMemoryTelemetryExporter:
    """Deterministic sink for tests and local diagnostics."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def __call__(self, payload: dict[str, Any]) -> None:
        self.records.append(redact_telemetry_fields(payload))
