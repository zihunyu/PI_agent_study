"""Admission counts physical calls made by a Router with no framework base class."""

from __future__ import annotations

import asyncio
import copy

import pytest

from pi_agent_loop import (
    CapabilityRegistry,
    ClosedLoopBudget,
    DurableAgentHost,
    ExecutionPolicy,
    HybridRequestPlanner,
    IntentPlanPolicy,
    ModelRetryPolicy,
    PlanBudgetExceeded,
    PlanResourceUsage,
    PlanUsageReservation,
    RequestDecision,
    RuntimeBoundRouter,
    ScriptedProvider,
    ContentSafetyPipeline,
    SafetyDecision,
)
from pi_agent_loop.model_attempts import current_model_attempt_admission_scope
from test_generic_extensions import MODEL, message


class Router:
    def __init__(self, calls=1, started=None):
        self.calls = calls
        self.started = started
        self.stream_fn = None

    @property
    def call_count(self):
        raise AssertionError("Router diagnostic counters are not billing evidence")

    @property
    def evaluation_metrics(self):
        raise AssertionError("Router metrics are not billing evidence")

    def bind_runtime(
        self, *, stream_fn, retry_event_sink, durable_metadata_provider=None
    ):
        bound = copy.copy(self)
        bound.stream_fn = stream_fn
        return bound

    async def route(self, text, *, cancellation=None, **scope):
        for _ in range(self.calls):
            stream = self.stream_fn(
                MODEL,
                {
                    "messages": [
                        {"role": "user", "content": [{"type": "text", "text": text}]}
                    ],
                    "tools": [],
                },
                {"model_request_source": "router", "cancellation_token": cancellation},
            )
            if self.started is not None:
                self.started.set()
            _events = [event async for event in stream]
            await stream.result()
        return RequestDecision(
            status="out_of_scope", reason="offline", message="No action"
        )


class Meter:
    def __init__(self):
        self.snapshots = {}
        self.cancelled = []
        self.settled = []

    def reserve(self, *, run_id, reservation_id, stage, remaining):
        return PlanUsageReservation(
            reservation_id, stage, PlanResourceUsage(model_calls=3, tokens=30, cost=0.3)
        )

    async def dispatch(self, reservation, callback):
        try:
            return await callback()
        finally:
            self.snapshots[
                reservation.reservation_id
            ] = await current_model_attempt_admission_scope().snapshot()

    def settle(self, reservation):
        self.settled.append(reservation.reservation_id)
        observed = self.snapshots[reservation.reservation_id]
        return PlanResourceUsage(
            model_calls=observed.model_calls, tokens=observed.tokens, cost=observed.cost
        )

    def cancel(self, reservation):
        self.cancelled.append(reservation.reservation_id)


async def make_host(path, router, provider, meter, **kwargs):
    policies = {"read": IntentPlanPolicy("read")}
    return await DurableAgentHost.create(
        session_id="router",
        state_dir=path,
        model=MODEL,
        stream_fn=provider.stream,
        system_prompt="",
        tools=[],
        router=router,
        capabilities=CapabilityRegistry(),
        planner=HybridRequestPlanner(policies, lambda *_: None),
        plan_policies=policies,
        plan_step_executor=lambda *_: None,
        plan_usage_meter=meter,
        plan_correction_budget=ClosedLoopBudget(
            max_model_calls=9, max_tokens=90, max_cost=1
        ),
        **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("calls", [0, 1, 3])
async def test_custom_router_counts_zero_or_multiple_physical_attempts(tmp_path, calls):
    meter, router = Meter(), Router(calls)
    provider = ScriptedProvider([message() for _ in range(calls)])
    inspected, phases = [], []

    class Safety:
        async def inspect(self, inspection, token):
            inspected.append(inspection.stage)
            return SafetyDecision.allow()

    async def transform(messages, token, context):
        phases.append(context.phase)
        return messages

    host = await make_host(
        tmp_path,
        router,
        provider,
        meter,
        execution_policy=ExecutionPolicy(ContentSafetyPipeline([Safety()]), transform),
    )
    try:
        assert isinstance(host.routed_agent.router, RuntimeBoundRouter)
        assert host.routed_agent.router is not router
        await host.prompt("offline")
        snapshot = next(iter(meter.snapshots.values()))
        assert snapshot.model_calls == calls == provider.call_count
        assert bool(meter.cancelled) is (calls == 0)
        assert phases == ["router"] * calls
        assert inspected == ["model_input", "model_output"] * calls
        record = await host.autonomous_run_store.load(snapshot.run_id)
        assert record.resource_usage.model_calls == calls
        assert not record.active_reservations
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_custom_router_failed_retry_is_a_second_physical_attempt(tmp_path):
    first = message("retry")
    first.update(
        stopReason="error", providerError={"statusCode": 503, "retryable": True}
    )
    provider = ScriptedProvider([first, message()])
    meter = Meter()
    host = await make_host(
        tmp_path,
        Router(),
        provider,
        meter,
        model_retry_policy=ModelRetryPolicy(
            enabled=True, max_retries=1, initial_delay_seconds=0, jitter_ratio=0
        ),
    )
    try:
        await host.prompt("offline")
        snapshot = next(iter(meter.snapshots.values()))
        assert snapshot.model_calls == 2 == provider.call_count
        assert (
            await host.autonomous_run_store.load(snapshot.run_id)
        ).resource_usage.model_calls == 2
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_unknown_usage_retains_reservation_and_stops(tmp_path):
    unknown = message()
    unknown["usageObserved"] = False
    provider, meter = ScriptedProvider([unknown, message()]), Meter()
    host = await make_host(tmp_path, Router(2), provider, meter)
    try:
        with pytest.raises(PlanBudgetExceeded):
            await host.prompt("offline")
        snapshot = next(iter(meter.snapshots.values()))
        assert snapshot.unknown_attempts == 1
        assert provider.call_count == 1
        record = await host.autonomous_run_store.load(snapshot.run_id)
        assert record.active_reservations
        assert not meter.cancelled and not meter.settled
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_cancellation_of_dispatched_router_retains_unknown_usage(tmp_path):
    from pi_agent_loop.retry.model import (
        ProducerOwnedAssistantMessageEventStream,
        bind_stream_producer,
    )

    entered, finished = asyncio.Event(), asyncio.Event()

    class Provider:
        def stream(self, model, context, options):
            output = ProducerOwnedAssistantMessageEventStream()

            async def run():
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    finished.set()

            bind_stream_producer(
                output, asyncio.create_task(run(), name="test-owned-provider")
            )
            return output

    meter = Meter()
    host = await make_host(tmp_path, Router(), Provider(), meter)
    try:
        work = asyncio.create_task(host.prompt("offline"))
        await entered.wait()
        work.cancel()
        with pytest.raises((asyncio.CancelledError, PlanBudgetExceeded)):
            await work
        assert finished.is_set()
        snapshot = next(iter(meter.snapshots.values()))
        assert snapshot.unknown_attempts == 1
        assert (
            await host.autonomous_run_store.load(snapshot.run_id)
        ).active_reservations
        assert not meter.cancelled
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_router_binding_cannot_return_shared_instance(tmp_path):
    class Shared(Router):
        def bind_runtime(self, **kwargs):
            return self

    with pytest.raises(ValueError, match="independent"):
        await make_host(tmp_path, Shared(), ScriptedProvider(), Meter())
