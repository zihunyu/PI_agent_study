"""P2 unified model boundary, pooled HTTP, backpressure and telemetry tests."""

from __future__ import annotations

import asyncio
import json
import unittest

import httpx
import pi_agent_loop

from pi_agent_loop.cancellation import CancellationToken
from pi_agent_loop.event_stream import (
    AgentEventStream,
    AssistantMessageEventStream,
    EventStream,
    EventStreamBackpressureError,
)
from pi_agent_loop.harness.model_runtime_adapter import (
    ModelCallRuntime,
    TokenPricing,
)
from pi_agent_loop.messages import assistant_message
from pi_agent_loop.providers.openai_compatible import OpenAICompatibleProvider
from pi_agent_loop.providers.settings import ProviderProfile
from pi_agent_loop.retry.compaction import CompactionRetryPolicy
from pi_agent_loop.retry.types import ModelRetryPolicy
from pi_agent_loop.retry.circuit_breaker import CircuitBreakerPolicy
from pi_agent_loop.routing import (
    CapabilityRegistry,
    RequiredToolCallGuard,
    guard_stream_fn,
)
from pi_agent_loop.runtime.telemetry import (
    InMemoryTelemetryExporter,
    MetricRegistry,
    Telemetry,
    redact_telemetry_fields,
)
from pi_agent_loop.testing import ScriptedProvider
from pi_agent_loop.types import Model


MODEL = Model(id="runtime-model", provider="test", api="fake")


def _profile() -> ProviderProfile:
    return ProviderProfile(
        name="vendor",
        protocol="openai_chat_completions",
        base_url="https://vendor.example/v1",
        endpoint="/chat/completions",
        auth_type="bearer",
        api_key="test-only-secret",
        model="runtime-model",
        stream=True,
        connect_timeout_seconds=1,
        request_timeout_seconds=2,
        allow_insecure_http=False,
        retry_policy=ModelRetryPolicy(),
    )


def _sse_response(text: str) -> httpx.Response:
    chunks = [
        {
            "id": "response-1",
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
        },
        {
            "id": "response-1",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 3,
                "total_tokens": 5,
            },
        },
    ]
    body = "".join(
        f"data: {json.dumps(chunk)}\n\n" for chunk in chunks
    ) + "data: [DONE]\n\n"
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=body.encode(),
    )


async def _collect(stream: AssistantMessageEventStream):
    events = [event async for event in stream]
    return events, await stream.result()


class EventStreamBackpressureTests(unittest.IsolatedAsyncioTestCase):
    async def test_end_none也会结算result(self) -> None:
        stream = EventStream(lambda _event: False, lambda _event: None)
        stream.end()

        self.assertEqual([event async for event in stream], [])
        self.assertIsNone(await stream.result())

    async def test_bounded_stream_coalesces_updates_and_preserves_terminal(self) -> None:
        notices: list[dict] = []
        stream = AssistantMessageEventStream(
            max_buffer_size=4,
            max_buffer_bytes=2048,
            on_backpressure=notices.append,
        )
        partial = assistant_message(model=MODEL)
        stream.push({"type": "start", "partial": partial})
        for index in range(30):
            stream.push(
                {
                    "type": "text_delta",
                    "contentIndex": 0,
                    "delta": str(index % 10),
                    "partial": partial,
                }
            )
        final = assistant_message(
            model=MODEL,
            content=[{"type": "text", "text": "complete"}],
        )
        stream.push({"type": "done", "reason": "stop", "message": final})

        events, result = await _collect(stream)

        self.assertIs(result, final)
        self.assertEqual(events[-1]["type"], "done")
        self.assertLessEqual(stream.stats.high_watermark_events, 4)
        self.assertGreater(stream.stats.coalesced_events, 0)
        self.assertTrue(notices)

    async def test_terminal_evicts_stale_events_instead_of_hanging(self) -> None:
        stream = AgentEventStream(max_buffer_size=3, max_buffer_bytes=512)
        for index in range(20):
            stream.push({"type": "message_update", "message": {"index": index}})
        final_messages = [{"role": "assistant", "content": []}]
        stream.push({"type": "agent_end", "messages": final_messages})

        events = [event async for event in stream]

        self.assertEqual(events[-1]["type"], "agent_end")
        self.assertEqual(await stream.result(), final_messages)
        self.assertLessEqual(stream.stats.high_watermark_events, 3)

    async def test_error_policy_fails_explicitly(self) -> None:
        stream = AssistantMessageEventStream(
            max_buffer_size=2,
            max_buffer_bytes=4096,
            slow_consumer_policy="error",
        )
        stream.push({"type": "start", "partial": {}})
        stream.push({"type": "text_start", "partial": {}})
        with self.assertRaises(EventStreamBackpressureError):
            stream.push({"type": "thinking_start", "partial": {}})
        with self.assertRaises(EventStreamBackpressureError):
            await stream.result()

    async def test_non_terminal_oversized_event不会突破字节上限(self) -> None:
        stream = AgentEventStream(
            max_buffer_size=4,
            max_buffer_bytes=64,
        )

        stream.push(
            {
                "type": "message_update",
                "message": {"text": "x" * 10_000},
            }
        )

        self.assertEqual(stream.stats.queued_events, 0)
        self.assertEqual(stream.stats.queued_bytes, 0)
        self.assertEqual(stream.stats.dropped_events, 1)

    async def test_protected_coalesced_update也不会突破字节上限(self) -> None:
        stream = AssistantMessageEventStream(
            max_buffer_size=4,
            max_buffer_bytes=512,
        )
        partial = assistant_message(model=MODEL)

        for _index in range(100):
            stream.push(
                {
                    "type": "text_delta",
                    "contentIndex": 0,
                    "delta": "x" * 80,
                    "partial": partial,
                }
            )
            self.assertLessEqual(stream.stats.queued_bytes, 512)

        final = assistant_message(model=MODEL)
        stream.push({"type": "done", "message": final})
        events, result = await _collect(stream)

        self.assertIs(result, final)
        self.assertEqual(events[-1]["type"], "done")
        self.assertLessEqual(stream.stats.high_watermark_bytes, 512)

    async def test_oversized_terminal显式失败且不会突破上限(self) -> None:
        stream = AgentEventStream(max_buffer_size=3, max_buffer_bytes=64)

        with self.assertRaises(EventStreamBackpressureError):
            stream.push(
                {
                    "type": "agent_end",
                    "messages": [{"role": "assistant", "text": "x" * 10_000}],
                }
            )

        self.assertLessEqual(stream.stats.queued_bytes, 64)
        self.assertEqual([event async for event in stream], [])
        with self.assertRaises(EventStreamBackpressureError):
            await stream.result()

    async def test_async背压hook有界合并并在close时清理(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def slow_hook(_payload):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()

        stream = AgentEventStream(
            max_buffer_size=3,
            max_buffer_bytes=512,
            on_backpressure=slow_hook,
        )
        for index in range(200):
            stream.push({"type": "message_update", "message": {"index": index}})
        await asyncio.wait_for(started.wait(), 1)

        live = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and not task.done()
            and task.get_name() == "event-stream-backpressure"
        ]
        self.assertEqual(len(live), 1)
        stream.end([])
        await asyncio.sleep(0)
        self.assertTrue(live[0].done())
        self.assertLess(calls, 200)

    async def test_malformed_event分类异常会fail_close(self) -> None:
        stream = AssistantMessageEventStream()

        with self.assertRaises(AttributeError):
            stream.push(None)  # type: ignore[arg-type]

        self.assertEqual([event async for event in stream], [])
        with self.assertRaises(AttributeError):
            await stream.result()

    async def test_custom_is_complete异常会fail_close(self) -> None:
        def explode(_event):
            raise ValueError("bad classifier")

        stream = EventStream(explode, lambda event: event)

        with self.assertRaisesRegex(ValueError, "bad classifier"):
            stream.push({"value": 1})
        self.assertEqual([event async for event in stream], [])
        with self.assertRaisesRegex(ValueError, "bad classifier"):
            await stream.result()

    async def test_custom_coalesce_key异常会fail_close(self) -> None:
        def explode(_event):
            raise ValueError("bad key")

        stream = EventStream(
            lambda _event: False,
            lambda event: event,
            coalesce_key=explode,
        )

        with self.assertRaisesRegex(ValueError, "bad key"):
            stream.push({"value": 1})
        self.assertEqual([event async for event in stream], [])
        with self.assertRaisesRegex(ValueError, "bad key"):
            await stream.result()

    async def test_custom_merge异常会fail_close(self) -> None:
        def explode(_previous, _current):
            raise ValueError("bad merge")

        stream = EventStream(
            lambda _event: False,
            lambda event: event,
            max_buffer_size=2,
            coalesce_key=lambda _event: "same",
            merge_updates=explode,
        )
        stream.push({"value": 1})
        stream.push({"value": 2})

        with self.assertRaisesRegex(ValueError, "bad merge"):
            stream.push({"value": 3})
        _events = [event async for event in stream]
        with self.assertRaisesRegex(ValueError, "bad merge"):
            await stream.result()

    async def test_size_estimation异常会fail_close(self) -> None:
        class BadRepresentation:
            def __repr__(self) -> str:
                raise ValueError("bad size")

        stream = EventStream(lambda _event: False, lambda event: event)

        with self.assertRaisesRegex(ValueError, "bad size"):
            stream.push(BadRepresentation())
        self.assertEqual([event async for event in stream], [])
        with self.assertRaisesRegex(ValueError, "bad size"):
            await stream.result()

    async def test_terminal_extractor异常仍会结束消费者和result(self) -> None:
        def explode(_event):
            raise ValueError("bad terminal")

        stream = EventStream(
            lambda event: bool(event.get("done")),
            explode,
            max_buffer_size=2,
            max_buffer_bytes=128,
        )
        with self.assertRaisesRegex(ValueError, "bad terminal"):
            stream.push({"done": True})

        self.assertEqual([event async for event in stream], [])
        with self.assertRaisesRegex(ValueError, "bad terminal"):
            await stream.result()


class ProviderPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_close会取消并等待活动http请求(self) -> None:
        provider = OpenAICompatibleProvider(
            _profile(),
            transport=httpx.MockTransport(lambda _request: _sse_response("unused")),
        )
        started = asyncio.Event()
        drained = asyncio.Event()

        async def blocking_request(_stream, _model, _context, _options, _api_key):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()

        provider._request = blocking_request
        stream = provider.stream(MODEL, {"messages": [], "tools": []}, {})
        await asyncio.wait_for(started.wait(), timeout=1)

        await asyncio.wait_for(provider.aclose(), timeout=1)

        _events, result = await asyncio.wait_for(_collect(stream), timeout=1)
        self.assertTrue(drained.is_set())
        self.assertEqual(result["stopReason"], "aborted")
        self.assertTrue(provider.client.is_closed)
        self.assertTrue(provider.closed)

    async def test_internal_client_is_reused_and_owned(self) -> None:
        calls = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return _sse_response(str(calls))

        provider = OpenAICompatibleProvider(
            _profile(),
            transport=httpx.MockTransport(handler),
        )
        client = provider.client
        await _collect(provider.stream(MODEL, {"messages": [], "tools": []}, {}))
        await _collect(provider.stream(MODEL, {"messages": [], "tools": []}, {}))

        self.assertIs(provider.client, client)
        self.assertEqual(calls, 2)
        self.assertTrue(provider.owns_client)
        await provider.aclose()
        self.assertTrue(client.is_closed)
        self.assertTrue(provider.closed)

    async def test_injected_client_is_caller_owned_by_default(self) -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: _sse_response("ok"))
        )
        provider = OpenAICompatibleProvider(_profile(), client=client)

        await _collect(provider.stream(MODEL, {"messages": [], "tools": []}, {}))
        await provider.aclose()

        self.assertFalse(client.is_closed)
        self.assertFalse(provider.owns_client)
        await client.aclose()

    async def test_explicitly_owned_injected_client_is_closed(self) -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: _sse_response("ok"))
        )
        provider = OpenAICompatibleProvider(
            _profile(),
            client=client,
            owns_client=True,
        )

        await provider.aclose()

        self.assertTrue(client.is_closed)

    async def test_closed_provider_rejects_new_stream(self) -> None:
        provider = OpenAICompatibleProvider(
            _profile(),
            transport=httpx.MockTransport(lambda _request: _sse_response("ok")),
        )
        await provider.aclose()

        with self.assertRaisesRegex(RuntimeError, "closed"):
            provider.stream(MODEL, {"messages": [], "tools": []}, {})


class TelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_metrics_spans_logs_alerts_and_redaction(self) -> None:
        spans = InMemoryTelemetryExporter()
        logs = InMemoryTelemetryExporter()
        alerts = InMemoryTelemetryExporter()
        telemetry = Telemetry(
            span_sink=spans,
            log_sink=logs,
            alert_hooks=[alerts],
        )
        span = telemetry.start_span(
            "unit.test",
            attributes={"api_key": "secret", "safe": "value"},
        )
        span.add_event("step", password="hidden")
        await telemetry.log("info", "test", authorization="Bearer hidden")
        await telemetry.alert("test_alert", secret="hidden")
        await span.finish()

        self.assertEqual(spans.records[0]["attributes"]["api_key"], "[REDACTED]")
        self.assertEqual(
            spans.records[0]["events"][0]["attributes"]["password"],
            "[REDACTED]",
        )
        self.assertEqual(
            logs.records[0]["fields"]["authorization"],
            "[REDACTED]",
        )
        self.assertEqual(alerts.records[0]["fields"]["secret"], "[REDACTED]")

    def test_metric_registry_tracks_all_metric_kinds(self) -> None:
        metrics = MetricRegistry()
        metrics.increment("calls", labels={"kind": "model"})
        metrics.set_gauge("queue", 3)
        metrics.observe("latency", 12.5)

        snapshot = metrics.snapshot()

        self.assertEqual(snapshot["counters"][0]["value"], 1)
        self.assertEqual(snapshot["gauges"][0]["value"], 3)
        self.assertEqual(snapshot["histograms"][0]["count"], 1)

    def test_redaction_is_recursive(self) -> None:
        value = redact_telemetry_fields(
            {"nested": {"access_token": "secret"}, "safe": [1, 2]}
        )
        self.assertEqual(value["nested"]["access_token"], "[REDACTED]")
        self.assertEqual(value["safe"], [1, 2])


class ModelCallRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_runtime_close会排空忽略协作取消的活动调用(self) -> None:
        started = asyncio.Event()
        drained = asyncio.Event()

        class StalledStream:
            def __aiter__(self):
                return self._iterate()

            async def _iterate(self):
                started.set()
                try:
                    await asyncio.Event().wait()
                    yield {"type": "never"}
                finally:
                    drained.set()

            async def result(self):
                await asyncio.Event().wait()

        runtime = ModelCallRuntime(
            lambda _model, _context, _options: StalledStream()
        )
        stream = runtime.stream(MODEL, {"messages": [], "tools": []}, {})
        await asyncio.wait_for(started.wait(), timeout=1)

        await asyncio.wait_for(runtime.aclose(), timeout=1)

        _events, result = await asyncio.wait_for(_collect(stream), timeout=1)
        self.assertTrue(drained.is_set())
        self.assertEqual(result["stopReason"], "aborted")
        self.assertEqual(runtime.active_call_count, 0)

    async def test_guard消费者取消会递归排空统一model_runtime(self) -> None:
        upstream_started = asyncio.Event()
        upstream_drained = asyncio.Event()
        never = asyncio.Event()

        async def blocking_stream(_model, _context, _options):
            upstream_started.set()
            try:
                await never.wait()
            finally:
                upstream_drained.set()

        runtime = ModelCallRuntime(blocking_stream)
        guarded = guard_stream_fn(
            runtime.stream,
            RequiredToolCallGuard(CapabilityRegistry()),
        )
        stream = guarded(
            MODEL,
            {"messages": [], "tools": []},
            {"tool_choice": "auto"},
        )
        consumer = asyncio.create_task(_collect(stream))
        await asyncio.wait_for(upstream_started.wait(), timeout=1)

        consumer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(consumer, timeout=1)
        await asyncio.wait_for(upstream_drained.wait(), timeout=1)

        self.assertEqual(runtime.active_call_count, 0)
        await runtime.aclose()
        self.assertTrue(runtime.closed)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            runtime.stream(MODEL, {"messages": [], "tools": []}, {})

    async def test_model_runtime消费者取消会排空后台producer(self) -> None:
        started = asyncio.Event()
        drained = asyncio.Event()

        class StalledStream:
            def __aiter__(self):
                return self._iterate()

            async def _iterate(self):
                started.set()
                try:
                    await asyncio.Event().wait()
                    yield {"type": "never"}
                finally:
                    drained.set()

            async def result(self):
                await asyncio.Event().wait()

        runtime = ModelCallRuntime(
            lambda _model, _context, _options: StalledStream()
        )
        stream = runtime.stream(MODEL, {"messages": [], "tools": []}, {})
        consumer = asyncio.create_task(_collect(stream))
        await asyncio.wait_for(started.wait(), timeout=1)

        consumer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(consumer, timeout=1)

        await asyncio.wait_for(drained.wait(), timeout=1)
        self.assertEqual(runtime.active_call_count, 0)
        await runtime.aclose()

    async def test_model_retry和circuit_open进入统一telemetry(self) -> None:
        failure = assistant_message(
            model=MODEL,
            stop_reason="error",
            error_message="temporary upstream failure",
        )
        failure["providerError"] = {
            "code": "provider_http_error",
            "statusCode": 503,
            "retryAfterMs": 0,
            "retryable": True,
        }
        provider = ScriptedProvider([failure])
        alerts = InMemoryTelemetryExporter()
        telemetry = Telemetry(alert_hooks=[alerts])
        runtime = ModelCallRuntime(
            provider.stream,
            retry_policy=ModelRetryPolicy(
                enabled=True,
                max_retries=1,
                initial_delay_seconds=0,
                max_delay_seconds=1,
                jitter_ratio=0,
                circuit_breaker=CircuitBreakerPolicy(
                    enabled=True,
                    failure_threshold=1,
                    recovery_timeout_seconds=60,
                ),
            ),
            telemetry=telemetry,
        )

        _events, result = await _collect(
            runtime.stream(
                MODEL,
                {"systemPrompt": "", "messages": [], "tools": []},
                {"model_request_source": "router"},
            )
        )

        self.assertEqual(result["providerError"]["code"], "provider_circuit_open")
        counters = {
            row["name"]: row["value"]
            for row in telemetry.metrics.snapshot()["counters"]
        }
        self.assertEqual(counters["model_retries_total"], 1)
        self.assertEqual(counters["model_retry_sequences_total"], 1)
        self.assertEqual(counters["model_circuit_open_total"], 1)
        self.assertTrue(
            any(record["name"] == "model_circuit_open" for record in alerts.records)
        )

    async def test_unified_boundary_records_durable_usage_cost_and_telemetry(self) -> None:
        final = assistant_message(
            model=MODEL,
            content=[{"type": "text", "text": "ok"}],
            usage={
                "input": 1_000_000,
                "output": 500_000,
                "cacheRead": 0,
                "cacheWrite": 0,
                "totalTokens": 1_500_000,
                "cost": {},
            },
        )
        provider = ScriptedProvider([final])
        durable: list[dict] = []
        spans = InMemoryTelemetryExporter()
        logs = InMemoryTelemetryExporter()
        telemetry = Telemetry(span_sink=spans, log_sink=logs)
        runtime = ModelCallRuntime(
            provider.stream,
            durable_event_sink=durable.append,
            telemetry=telemetry,
            pricing=TokenPricing(input_per_million=2, output_per_million=4),
        )

        events, result = await _collect(
            runtime.stream(
                MODEL,
                {"systemPrompt": "", "messages": [], "tools": []},
                {
                    "tool_choice": "none",
                    "model_request_id": "request-1",
                    "model_request_source": "router",
                },
            )
        )

        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(result["usage"]["cost"]["total"], 4.0)
        self.assertEqual(
            [event["type"] for event in durable],
            ["model_request_started", "model_request_completed"],
        )
        self.assertEqual(durable[-1]["message"]["usage"]["cost"]["total"], 4.0)
        self.assertEqual(spans.records[0]["status"], "ok")
        self.assertEqual(len(logs.records), 2)
        metrics = telemetry.metrics.snapshot()
        self.assertTrue(
            any(row["name"] == "model_requests_total" for row in metrics["counters"])
        )
        queue_gauges = {
            row["name"]: row["value"]
            for row in metrics["gauges"]
            if row["labels"] == {"queue": "model_call"}
        }
        self.assertEqual(queue_gauges["queue_depth"], 0)
        self.assertEqual(queue_gauges["queue_retained_bytes"], 0)

    async def test_pre_cancelled_request_is_durably_finished(self) -> None:
        provider = ScriptedProvider(
            [assistant_message(model=MODEL, content=[{"type": "text", "text": "bad"}])]
        )
        durable: list[dict] = []
        token = CancellationToken()
        token.cancel("stop")
        runtime = ModelCallRuntime(provider.stream, durable_event_sink=durable.append)

        _events, result = await _collect(
            runtime.stream(
                MODEL,
                {"messages": [], "tools": []},
                {"cancellation_token": token},
            )
        )

        self.assertEqual(result["stopReason"], "aborted")
        self.assertEqual(provider.call_count, 0)
        self.assertEqual(
            [event["type"] for event in durable],
            ["model_request_started", "model_request_failed"],
        )
        self.assertEqual(durable[0]["requestId"], durable[1]["requestId"])
        self.assertEqual(durable[1]["outcome"], "cancelled")

    async def test_cancellation_interrupts_stream_that_ignores_token_and_drains_pump(
        self,
    ) -> None:
        started = asyncio.Event()
        drained = asyncio.Event()
        never = asyncio.Event()
        final = assistant_message(
            model=MODEL,
            content=[{"type": "text", "text": "must not complete"}],
        )

        class StalledStream:
            def __aiter__(self):
                return self._iterate()

            async def _iterate(self):
                started.set()
                try:
                    yield {
                        "type": "text_delta",
                        "contentIndex": 0,
                        "delta": "partial",
                    }
                    await never.wait()
                finally:
                    drained.set()

            async def result(self):
                await never.wait()
                return final

        durable: list[dict] = []
        token = CancellationToken()
        runtime = ModelCallRuntime(
            lambda _model, _context, _options: StalledStream(),
            durable_event_sink=durable.append,
        )
        stream = runtime.stream(
            MODEL,
            {"messages": [], "tools": []},
            {"cancellation_token": token, "model_request_id": "during-stream"},
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        token.cancel("stop during stream")

        events, result = await asyncio.wait_for(_collect(stream), timeout=1)

        self.assertTrue(drained.is_set())
        self.assertEqual(events[0]["type"], "text_delta")
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(result["stopReason"], "aborted")
        self.assertEqual(result["errorMessage"], "stop during stream")
        self.assertEqual(
            [event["type"] for event in durable],
            ["model_request_started", "model_request_failed"],
        )
        self.assertEqual([event["requestId"] for event in durable], [
            "during-stream",
            "during-stream",
        ])
        self.assertEqual(durable[-1]["outcome"], "cancelled")

    async def test_cancellation_interrupts_awaitable_stream_factory_that_never_returns(
        self,
    ) -> None:
        started = asyncio.Event()
        drained = asyncio.Event()
        never = asyncio.Event()

        async def malicious_stream(_model, _context, _options):
            started.set()
            try:
                await never.wait()
            finally:
                drained.set()

        durable: list[dict] = []
        token = CancellationToken()
        runtime = ModelCallRuntime(
            malicious_stream,
            durable_event_sink=durable.append,
        )
        stream = runtime.stream(
            MODEL,
            {"messages": [], "tools": []},
            {"cancellation_token": token, "model_request_id": "never-returns"},
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        token.cancel("stop malicious stream")

        events, result = await asyncio.wait_for(_collect(stream), timeout=1)

        self.assertTrue(drained.is_set())
        self.assertEqual([event["type"] for event in events], ["error"])
        self.assertEqual(result["stopReason"], "aborted")
        self.assertEqual(
            [event["type"] for event in durable],
            ["model_request_started", "model_request_failed"],
        )
        self.assertEqual(durable[-1]["outcome"], "cancelled")

    async def test_terminal_result_wins_same_turn_cancellation_without_duplicate_finish(
        self,
    ) -> None:
        token = CancellationToken()
        final = assistant_message(
            model=MODEL,
            content=[{"type": "text", "text": "authoritative"}],
        )

        class TerminalRaceStream:
            def __init__(self) -> None:
                self.done = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.done:
                    raise StopAsyncIteration
                self.done = True
                token.cancel("same-turn cancellation")
                return {"type": "done", "reason": "stop", "message": final}

            async def result(self):
                return final

        durable: list[dict] = []
        runtime = ModelCallRuntime(
            lambda _model, _context, _options: TerminalRaceStream(),
            durable_event_sink=durable.append,
        )

        events, result = await asyncio.wait_for(
            _collect(
                runtime.stream(
                    MODEL,
                    {"messages": [], "tools": []},
                    {
                        "cancellation_token": token,
                        "model_request_id": "terminal-race",
                    },
                )
            ),
            timeout=1,
        )

        self.assertEqual([event["type"] for event in events], ["done"])
        self.assertEqual(result["stopReason"], "stop")
        self.assertEqual(
            [event["type"] for event in durable],
            ["model_request_started", "model_request_completed"],
        )
        self.assertEqual([event["requestId"] for event in durable], [
            "terminal-race",
            "terminal-race",
        ])

    async def test_close取消finished_telemetry不会重复或改写durable终态(self) -> None:
        provider = ScriptedProvider([
            assistant_message(
                model=MODEL,
                content=[{"type": "text", "text": "authoritative"}],
            )
        ])
        durable: list[dict] = []
        finished_log_started = asyncio.Event()
        never = asyncio.Event()
        finished_log_calls = 0

        async def blocking_log(payload):
            nonlocal finished_log_calls
            if payload.get("event") != "model_request_finished":
                return
            finished_log_calls += 1
            if finished_log_calls == 1:
                finished_log_started.set()
                await never.wait()

        runtime = ModelCallRuntime(
            provider.stream,
            durable_event_sink=durable.append,
            telemetry=Telemetry(log_sink=blocking_log),
        )
        stream = runtime.stream(
            MODEL,
            {"messages": [], "tools": []},
            {"model_request_id": "terminal-telemetry-race"},
        )
        consumer = asyncio.create_task(_collect(stream))
        await asyncio.wait_for(finished_log_started.wait(), timeout=1)

        await asyncio.wait_for(runtime.aclose(), timeout=1)
        events, result = await asyncio.wait_for(consumer, timeout=1)

        self.assertEqual(
            [event["type"] for event in durable],
            ["model_request_started", "model_request_completed"],
        )
        self.assertEqual([event["type"] for event in events][-1], "done")
        self.assertEqual(result["stopReason"], "stop")
        self.assertEqual(finished_log_calls, 1)

    async def test_runtime_close递归取消并排空compaction生产任务(self) -> None:
        overflow = assistant_message(
            model=MODEL,
            stop_reason="error",
            error_message="context overflow",
        )
        overflow["providerError"] = {"code": "context_overflow"}
        provider = ScriptedProvider([overflow])
        compactor_started = asyncio.Event()
        compactor_drained = asyncio.Event()
        never = asyncio.Event()

        async def blocking_compactor(messages):
            compactor_started.set()
            try:
                await never.wait()
            finally:
                compactor_drained.set()
            return messages[-1:]

        runtime = ModelCallRuntime(
            provider.stream,
            compaction_policy=CompactionRetryPolicy(
                max_retries=1,
                keep_recent_messages=1,
            ),
            compactor=blocking_compactor,
        )
        stream = runtime.stream(
            MODEL,
            {
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "x"}]}
                ],
                "tools": [],
            },
            {},
        )
        consumer = asyncio.create_task(_collect(stream))
        await asyncio.wait_for(compactor_started.wait(), timeout=1)

        await asyncio.wait_for(runtime.aclose(), timeout=1)
        await asyncio.wait_for(compactor_drained.wait(), timeout=1)
        await asyncio.wait_for(consumer, timeout=1)

        leaked = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and not task.done()
            and task.get_name() == "pi-context-compaction:test:runtime-model"
        ]
        self.assertEqual(leaked, [])
        self.assertEqual(runtime.active_call_count, 0)

    async def test_durable_metadata不能覆盖模型事件保留字段(self) -> None:
        provider = ScriptedProvider([
            assistant_message(model=MODEL, content=[{"type": "text", "text": "bad"}])
        ])
        durable: list[dict] = []
        runtime = ModelCallRuntime(
            provider.stream,
            durable_event_sink=durable.append,
        )

        with self.assertRaisesRegex(ValueError, "reserved fields"):
            await _collect(
                runtime.stream(
                    MODEL,
                    {"messages": [], "tools": []},
                    {
                        "durable_metadata": {
                            "type": "operation_finished",
                            "requestId": "forged",
                        }
                    },
                )
            )

        self.assertEqual(provider.call_count, 0)
        self.assertEqual(durable, [])


class PublicExportsTests(unittest.TestCase):
    def test_new_runtime_types_are_public(self) -> None:
        for name in (
            "EventStreamBackpressureError",
            "EventStreamStats",
            "MetricRegistry",
            "ModelCallRuntime",
            "Telemetry",
            "TelemetrySpan",
            "TokenPricing",
        ):
            self.assertTrue(hasattr(pi_agent_loop, name), name)


if __name__ == "__main__":
    unittest.main()
