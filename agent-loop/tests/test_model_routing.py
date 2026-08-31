"""Deterministic model selection and safe provider failover tests."""

from __future__ import annotations

import asyncio
import unittest

from pi_agent_loop import (
    Agent,
    CancellationToken,
    HealthRegistry,
    Model,
    ModelCandidate,
    ResilientModelRouter,
    ScriptedProvider,
    SelectionPolicy,
    TaskRequirements,
    assistant_message,
)
from pi_agent_loop.event_stream import AssistantMessageEventStream


def _model(
    provider: str,
    model_id: str,
    *,
    context_window: int = 128_000,
) -> Model:
    return Model(
        id=model_id,
        provider=provider,
        api="scripted",
        context_window=context_window,
    )


def _success(model: Model, text: str) -> dict:
    return assistant_message(
        model=model,
        content=[{"type": "text", "text": text}],
    )


def _provider_failure(
    model: Model,
    *,
    retryable: bool,
    content: list[dict] | None = None,
    code: str = "provider_timeout_error",
) -> dict:
    final = assistant_message(
        model=model,
        content=content,
        stop_reason="error",
        error_message="provider unavailable",
    )
    final["providerError"] = {
        "code": code,
        "statusCode": 503,
        "retryAfterMs": None,
        "retryable": retryable,
    }
    return final


async def _collect(stream: AssistantMessageEventStream) -> tuple[list[dict], dict]:
    events = [event async for event in stream]
    return events, await stream.result()


class ModelSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_capability_context和健康约束在调用provider前筛选(self) -> None:
        text_model = _model("text-provider", "text", context_window=8_000)
        vision_model = _model("vision-provider", "vision", context_window=128_000)
        text_provider = ScriptedProvider([_success(text_model, "wrong")])
        vision_provider = ScriptedProvider([_success(vision_model, "vision-ok")])
        router = ResilientModelRouter(
            [
                ModelCandidate(
                    text_model,
                    text_provider.stream,
                    capabilities=frozenset({"json"}),
                    quality_score=0.99,
                ),
                ModelCandidate(
                    vision_model,
                    vision_provider.stream,
                    capabilities=frozenset({"json", "vision"}),
                    quality_score=0.8,
                ),
            ],
            requirements=TaskRequirements(
                required_capabilities=frozenset({"json"}),
                required_context_tokens=32_000,
            ),
        )
        context = {
            "systemPrompt": "",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "看图"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://cdn.example/a.png"},
                        },
                    ],
                }
            ],
            "tools": [],
        }

        _events, final = await _collect(router.stream(text_model, context, {}))

        self.assertEqual(final["provider"], "vision-provider")
        self.assertEqual(text_provider.call_count, 0)
        self.assertEqual(vision_provider.call_count, 1)

    def test_cost与quality权重产生确定性且可解释的排序(self) -> None:
        premium_model = _model("p", "premium")
        cheap_model = _model("c", "cheap")
        provider = ScriptedProvider([])
        premium = ModelCandidate(
            premium_model,
            provider.stream,
            quality_score=0.98,
            input_cost_per_million=30,
            output_cost_per_million=60,
            expected_latency_ms=300,
        )
        cheap = ModelCandidate(
            cheap_model,
            provider.stream,
            quality_score=0.75,
            input_cost_per_million=1,
            output_cost_per_million=2,
            expected_latency_ms=100,
        )
        requirements = TaskRequirements(
            expected_input_tokens=10_000,
            expected_output_tokens=2_000,
        )

        quality_first = SelectionPolicy(
            quality_weight=10,
            cost_weight=0,
            latency_weight=0,
        ).rank((cheap, premium), requirements)
        cost_first = SelectionPolicy(
            quality_weight=0.1,
            cost_weight=10,
            latency_weight=0,
        ).rank((premium, cheap), requirements)

        self.assertEqual(quality_first[0].key, premium.key)
        self.assertEqual(cost_first[0].key, cheap.key)
        self.assertEqual(
            SelectionPolicy(
                quality_weight=10,
                cost_weight=0,
                latency_weight=0,
            ).rank((cheap, premium), requirements),
            quality_first,
        )

    async def test_health_registry_open_cooldown_half_open只允许一个probe(self) -> None:
        now = [100.0]

        def clock() -> float:
            return now[0]

        registry = HealthRegistry(
            failure_threshold=1,
            cooldown_seconds=5,
            clock=clock,
        )
        self.assertTrue(await registry.try_acquire("provider:model"))
        await registry.record_failure("provider:model")
        self.assertEqual((await registry.snapshot("provider:model")).status, "open")
        self.assertFalse(await registry.try_acquire("provider:model"))

        now[0] = 106.0
        self.assertEqual(
            (await registry.snapshot("provider:model")).status,
            "half_open",
        )
        probes = await asyncio.gather(
            registry.try_acquire("provider:model"),
            registry.try_acquire("provider:model"),
        )
        self.assertEqual(probes.count(True), 1)
        await registry.record_success("provider:model")
        snapshot = await registry.snapshot("provider:model")
        self.assertEqual(snapshot.status, "closed")
        self.assertEqual(snapshot.consecutive_failures, 0)


class ResilientModelRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_total_deadline覆盖requirements_resolver(self) -> None:
        model = _model("primary", "unused")
        provider = ScriptedProvider([_success(model, "must not run")])
        resolver_entered = asyncio.Event()
        release_resolver = asyncio.Event()

        async def slow_resolver(_context, _options):
            resolver_entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release_resolver.wait()
            return TaskRequirements()

        router = ResilientModelRouter(
            [ModelCandidate(model, provider.stream)],
            requirements_resolver=slow_resolver,
            max_elapsed_seconds=0.01,
        )

        try:
            _events, final = await asyncio.wait_for(
                _collect(
                    router.stream(
                        model,
                        {"systemPrompt": "", "messages": [], "tools": []},
                        {},
                    )
                ),
                timeout=0.1,
            )
        finally:
            release_resolver.set()
            await asyncio.sleep(0)

        self.assertTrue(resolver_entered.is_set())
        self.assertEqual(
            final["providerError"]["code"],
            "model_route_deadline_exceeded",
        )
        self.assertEqual(provider.call_count, 0)

    async def test_telemetry_sink不能突破总deadline(self) -> None:
        model = _model("primary", "fast")
        provider = ScriptedProvider([_success(model, "ok")])
        sink_entered = asyncio.Event()
        release_sink = asyncio.Event()

        async def slow_sink(_event):
            sink_entered.set()
            await release_sink.wait()

        router = ResilientModelRouter(
            [ModelCandidate(model, provider.stream)],
            telemetry_sink=slow_sink,
            telemetry_timeout_seconds=1.0,
            max_elapsed_seconds=0.5,
        )

        try:
            _events, final = await asyncio.wait_for(
                _collect(
                    router.stream(
                        model,
                        {"systemPrompt": "", "messages": [], "tools": []},
                        {},
                    )
                ),
                timeout=0.75,
            )
        finally:
            release_sink.set()
            await asyncio.sleep(0)

        self.assertTrue(sink_entered.is_set())
        self.assertEqual(final["content"][0]["text"], "ok")
        self.assertEqual(provider.call_count, 1)

    async def test_pre_output_retryable_failure切换并可直接供Agent使用(self) -> None:
        primary_model = _model("primary", "quality")
        fallback_model = _model("fallback", "stable")
        primary = ScriptedProvider(
            [_provider_failure(primary_model, retryable=True)]
        )
        fallback = ScriptedProvider([_success(fallback_model, "fallback answer")])
        telemetry_events: list[dict] = []
        router = ResilientModelRouter(
            [
                ModelCandidate(
                    primary_model,
                    primary.stream,
                    quality_score=0.99,
                ),
                ModelCandidate(
                    fallback_model,
                    fallback.stream,
                    quality_score=0.8,
                ),
            ],
            telemetry_sink=telemetry_events.append,
        )
        agent = Agent(model=primary_model, stream_fn=router.stream)

        await agent.prompt("answer")

        final = agent.state.messages[-1]
        self.assertEqual(final["provider"], "fallback")
        self.assertEqual(final["content"][0]["text"], "fallback answer")
        self.assertEqual(primary.call_count, 1)
        self.assertEqual(fallback.call_count, 1)
        self.assertEqual(
            [message["role"] for message in agent.state.messages],
            ["user", "assistant"],
        )
        attempts = final["modelRouting"]["attempts"]
        self.assertEqual(
            [attempt["outcome"] for attempt in attempts],
            ["retryable_error", "success"],
        )
        self.assertEqual(final["modelRouting"]["selected"]["provider"], "fallback")
        self.assertEqual(router.last_telemetry.selected_model, "stable")
        self.assertEqual(
            [event["type"] for event in telemetry_events],
            [
                "model_route_attempt",
                "model_route_attempt",
                "model_route_finished",
            ],
        )

    async def test_post_output_failure绝不回退到第二provider(self) -> None:
        primary_model = _model("primary", "streaming")
        fallback_model = _model("fallback", "unused")
        primary = ScriptedProvider(
            [
                _provider_failure(
                    primary_model,
                    retryable=True,
                    content=[{"type": "text", "text": "partial"}],
                )
            ],
            chunk_size=2,
        )
        fallback = ScriptedProvider([_success(fallback_model, "must not run")])
        router = ResilientModelRouter(
            [
                ModelCandidate(primary_model, primary.stream, quality_score=1.0),
                ModelCandidate(fallback_model, fallback.stream, quality_score=0.5),
            ]
        )
        agent = Agent(model=primary_model, stream_fn=router.stream)

        await agent.prompt("stream")

        final = agent.state.messages[-1]
        self.assertEqual(final["stopReason"], "error")
        self.assertEqual(final["content"][0]["text"], "partial")
        self.assertEqual(primary.call_count, 1)
        self.assertEqual(fallback.call_count, 0)
        self.assertTrue(final["modelRouting"]["attempts"][0]["visibleOutput"])
        self.assertEqual(final["modelRouting"]["selected"]["provider"], "primary")

    async def test_router熔断跳过故障候选并在cooldown后half_open恢复(self) -> None:
        now = [10.0]

        def clock() -> float:
            return now[0]

        primary_model = _model("primary", "preferred")
        fallback_model = _model("fallback", "stable")
        primary = ScriptedProvider(
            [
                _provider_failure(primary_model, retryable=True),
                _provider_failure(primary_model, retryable=True),
                _success(primary_model, "primary recovered"),
            ]
        )
        fallback = ScriptedProvider(
            [
                _success(fallback_model, "fallback-1"),
                _success(fallback_model, "fallback-2"),
                _success(fallback_model, "fallback-3"),
            ]
        )
        health = HealthRegistry(
            failure_threshold=2,
            cooldown_seconds=5,
            clock=clock,
        )
        router = ResilientModelRouter(
            [
                ModelCandidate(primary_model, primary.stream, quality_score=1.0),
                ModelCandidate(fallback_model, fallback.stream, quality_score=0.5),
            ],
            health_registry=health,
            # Deadline uses a real monotonic clock; HealthRegistry uses the fake
            # clock above so cooldown transitions stay deterministic.
        )
        context = {"systemPrompt": "", "messages": [], "tools": []}

        first = (await _collect(router.stream(primary_model, context, {})))[1]
        second = (await _collect(router.stream(primary_model, context, {})))[1]
        third = (await _collect(router.stream(primary_model, context, {})))[1]

        self.assertEqual(first["provider"], "fallback")
        self.assertEqual(second["provider"], "fallback")
        self.assertEqual(third["provider"], "fallback")
        self.assertEqual(primary.call_count, 2)
        self.assertEqual((await health.snapshot("primary:preferred")).status, "open")

        now[0] = 16.0
        recovered = (await _collect(router.stream(primary_model, context, {})))[1]

        self.assertEqual(recovered["provider"], "primary")
        self.assertEqual(recovered["content"][0]["text"], "primary recovered")
        self.assertEqual((await health.snapshot("primary:preferred")).status, "closed")

    async def test_cancellation传播且不触发failover(self) -> None:
        primary_model = _model("primary", "hanging")
        fallback_model = _model("fallback", "unused")
        called = asyncio.Event()
        primary_calls = 0

        def hanging_stream(_model, _context, _options):
            nonlocal primary_calls
            primary_calls += 1
            called.set()
            return AssistantMessageEventStream()

        fallback = ScriptedProvider([_success(fallback_model, "must not run")])
        router = ResilientModelRouter(
            [
                ModelCandidate(primary_model, hanging_stream, quality_score=1.0),
                ModelCandidate(fallback_model, fallback.stream, quality_score=0.5),
            ],
            max_elapsed_seconds=2,
        )
        token = CancellationToken()
        stream = router.stream(
            primary_model,
            {"systemPrompt": "", "messages": [], "tools": []},
            {"cancellation_token": token},
        )
        await asyncio.wait_for(called.wait(), timeout=1)
        token.cancel("caller cancelled")

        _events, final = await asyncio.wait_for(_collect(stream), timeout=1)

        self.assertEqual(final["stopReason"], "aborted")
        self.assertEqual(primary_calls, 1)
        self.assertEqual(fallback.call_count, 0)
        self.assertEqual(final["providerError"]["code"], "model_route_cancelled")

    async def test_attempt硬上限并脱敏聚合所有失败(self) -> None:
        secret = "xoxb-1234567890-abcdefghij"
        primary_model = _model("primary", "bad")
        fallback_model = _model("fallback", "unused")
        failure = _provider_failure(
            primary_model,
            retryable=True,
            code=f"provider_{secret}",
        )
        failure["errorMessage"] = f"Authorization: Bearer {secret}"
        primary = ScriptedProvider([failure])
        fallback = ScriptedProvider([_success(fallback_model, "must not run")])
        router = ResilientModelRouter(
            [
                ModelCandidate(primary_model, primary.stream, quality_score=1.0),
                ModelCandidate(fallback_model, fallback.stream, quality_score=0.5),
            ],
            max_attempts=1,
        )

        _events, final = await _collect(
            router.stream(
                primary_model,
                {"systemPrompt": "", "messages": [], "tools": []},
                {},
            )
        )

        self.assertEqual(final["providerError"]["code"], "model_failover_exhausted")
        self.assertEqual(len(final["providerError"]["failures"]), 1)
        self.assertNotIn(secret, repr(final))
        self.assertEqual(primary.call_count, 1)
        self.assertEqual(fallback.call_count, 0)

    async def test_total_deadline硬上限停止当前attempt且不再切换(self) -> None:
        primary_model = _model("primary", "slow")
        fallback_model = _model("fallback", "unused")
        primary_calls = 0

        def hanging_stream(_model, _context, _options):
            nonlocal primary_calls
            primary_calls += 1
            return AssistantMessageEventStream()

        fallback = ScriptedProvider([_success(fallback_model, "must not run")])
        router = ResilientModelRouter(
            [
                ModelCandidate(primary_model, hanging_stream, quality_score=1.0),
                ModelCandidate(fallback_model, fallback.stream, quality_score=0.5),
            ],
            max_elapsed_seconds=0.03,
        )

        _events, final = await asyncio.wait_for(
            _collect(
                router.stream(
                    primary_model,
                    {"systemPrompt": "", "messages": [], "tools": []},
                    {},
                )
            ),
            timeout=1,
        )

        self.assertEqual(
            final["providerError"]["code"],
            "model_route_deadline_exceeded",
        )
        self.assertEqual(primary_calls, 1)
        self.assertEqual(fallback.call_count, 0)
        self.assertEqual(
            final["modelRouting"]["attempts"][0]["outcome"],
            "deadline_exceeded",
        )


if __name__ == "__main__":
    unittest.main()
