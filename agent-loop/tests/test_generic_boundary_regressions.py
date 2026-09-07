"""Offline reproductions of the five follow-up extension review findings."""

from __future__ import annotations

import asyncio
import copy
import json
import threading
import tomllib
from dataclasses import replace
from unittest.mock import patch

import httpx
import pytest

from pi_agent_loop import (
    CancellationToken,
    ContentSafetyPipeline,
    ExecutionContext,
    ExecutionPolicy,
    ModelCallRuntime,
    ModelRetryPolicy,
    OperationCancelledError,
    SafetyDecision,
    ScriptedProvider,
    load_business_bundle,
    user_message,
)
from pi_agent_loop.model_attempts import (
    ModelAttemptAdmissionScope,
    activate_model_attempt_admission,
    model_attempt_usage,
    model_attempt_usage_known,
)
from pi_agent_loop.planning import PlanIntentCondition, PlanIntentResultReference
from pi_agent_loop.providers import OpenAICompatibleProvider
from pi_agent_loop.retry.compaction import CompactionRetryPolicy
from test_generic_extensions import EXAMPLE, MODEL, bundle, factories, message, profile


def admission(stage="agent"):
    return ModelAttemptAdmissionScope(
        run_id="review", stage=stage, reservation_id="reserved", max_model_calls=3
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["agent", "router", "planner", "recovery"])
async def test_compacted_input_is_reviewed_before_dispatch_without_transforming_twice(
    phase,
):
    inspected, transformed = [], []

    class Safety:
        async def inspect(self, inspection, token):
            if inspection.stage == "model_input":
                inspected.append(inspection)
                if "REQUIRED_CONTEXT" not in str(inspection.value):
                    return SafetyDecision.block(reason="Required context is absent")
            return SafetyDecision.allow()

    async def transform(messages, token, context):
        transformed.append(context)
        return [user_message("authorized memory")] + messages

    async def compact(messages):
        return messages[-1:]

    overflow = message()
    overflow.update(stopReason="error", errorMessage="context overflow")
    provider = ScriptedProvider([overflow, message("must not dispatch")])
    runtime = ModelCallRuntime(
        provider.stream,
        compaction_policy=CompactionRetryPolicy(),
        compactor=compact,
        execution_policy=ExecutionPolicy(ContentSafetyPipeline([Safety()]), transform),
        tenant_id="trusted-tenant",
        session_id="trusted-session",
    )
    context = {
        "messages": [
            user_message("REQUIRED_CONTEXT " + "history " * 200),
            user_message("continue"),
        ]
    }
    original = copy.deepcopy(context)
    scope = admission(phase)
    try:
        async with activate_model_attempt_admission(scope):
            final = await runtime.invoke(
                MODEL, context, {"model_request_source": phase}
            )
        assert final["stopReason"] == "error"
        assert provider.call_count == 1
        assert (await scope.snapshot()).model_calls == 1
        assert len(inspected) == 2
        assert all(item.tenant_id == "trusted-tenant" for item in inspected)
        assert all(
            item.metadata == {"phase": phase, "sessionId": "trusted-session"}
            for item in inspected
        )
        assert transformed == [
            ExecutionContext(phase, "trusted-tenant", "trusted-session")
        ]
        assert str(provider.contexts[0]).count("authorized memory") == 1
        assert context == original
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_runtime_retry_checks_input_again_and_does_not_reinject_memory():
    inputs, transforms = [], []

    class Safety:
        async def inspect(self, inspection, token):
            if inspection.stage == "model_input":
                inputs.append(inspection.value)
            return SafetyDecision.allow()

    async def transform(messages, token):
        transforms.append(1)
        return messages + [user_message("one memory")]

    failed = message()
    failed.update(
        stopReason="error",
        providerError={
            "code": "provider_unavailable",
            "statusCode": 503,
            "retryable": True,
        },
    )
    provider = ScriptedProvider([failed, message()])
    runtime = ModelCallRuntime(
        provider.stream,
        retry_policy=ModelRetryPolicy(
            enabled=True, max_retries=1, initial_delay_seconds=0, jitter_ratio=0
        ),
        execution_policy=ExecutionPolicy(ContentSafetyPipeline([Safety()]), transform),
    )
    try:
        final = await runtime.invoke(MODEL, {"messages": [user_message("hello")]})
        assert final["stopReason"] == "stop"
        assert provider.call_count == len(inputs) == 2
        assert transforms == [1]
        assert all(
            str(context).count("one memory") == 1 for context in provider.contexts
        )
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout", "token", "task"])
async def test_uncooperative_async_transform_is_bounded_and_quarantined(mode):
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def transform(messages, token, context):
        calls.append(token)
        entered.set()
        try:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            return messages
        finally:
            finished.set()

    policy = ExecutionPolicy(
        transform_context=transform,
        transform_timeout_seconds=0.02 if mode == "timeout" else 5,
    )
    token = CancellationToken()
    context = ExecutionContext("agent")
    task = asyncio.create_task(policy.transform([], token, context))
    try:
        await entered.wait()
        if mode == "token":
            token.cancel()
        elif mode == "task":
            task.cancel()
        done, _ = await asyncio.wait({task}, timeout=0.5)
        assert task in done, (
            "the request must finish even if the callback ignores cancellation"
        )
        error = {
            "timeout": TimeoutError,
            "token": OperationCancelledError,
            "task": asyncio.CancelledError,
        }[mode]
        with pytest.raises(error):
            await task
        assert calls[0].cancelled
        assert token.child_count == 0
        with pytest.raises(RuntimeError, match="still running"):
            await policy.transform([], CancellationToken(), context)
        assert len(calls) == 1
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 1)
        await asyncio.gather(task, return_exceptions=True)
    assert await policy.transform([], CancellationToken(), context) == []
    assert not [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith("pi-context-") and not task.done()
    ]


@pytest.mark.asyncio
async def test_blocking_legacy_transform_does_not_block_loop_or_runtime_close():
    entered, release = threading.Event(), threading.Event()
    workers, tokens = [], []

    def transform(messages, token):
        workers.append(threading.current_thread())
        tokens.append(token)
        entered.set()
        release.wait(2)
        return messages + [user_message("late private result")]

    policy = ExecutionPolicy(
        transform_context=transform, transform_timeout_seconds=0.02
    )
    provider = ScriptedProvider()
    runtime = ModelCallRuntime(provider.stream, execution_policy=policy)
    task = asyncio.create_task(runtime.invoke(MODEL, {"messages": []}))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        done, _ = await asyncio.wait({task}, timeout=0.5)
        assert task in done
        final = await task
        assert final["stopReason"] == "error"
        assert "late private result" not in str(final)
        assert provider.call_count == 0
        assert tokens[0].cancelled
        with pytest.raises(RuntimeError, match="still running"):
            await policy.transform([], CancellationToken(), ExecutionContext("agent"))
        await asyncio.wait_for(runtime.aclose(), 0.5)
    finally:
        release.set()
        for worker in workers:
            await asyncio.to_thread(worker.join, 1)
            assert not worker.is_alive()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.aclose()


@pytest.mark.asyncio
async def test_legacy_sync_wrapper_returning_awaitable_remains_supported():
    async def result(messages):
        return messages

    policy = ExecutionPolicy(transform_context=lambda messages, token: result(messages))
    token = CancellationToken()
    assert await policy.transform([], token, ExecutionContext("agent")) == []
    assert token.child_count == 0


@pytest.mark.asyncio
async def test_runtime_close_is_bounded_with_an_active_cancel_resistant_transform():
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def transform(messages, token, context):
        entered.set()
        try:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            return messages + [user_message("late private result")]
        finally:
            finished.set()

    provider = ScriptedProvider()
    runtime = ModelCallRuntime(
        provider.stream,
        execution_policy=ExecutionPolicy(transform_context=transform),
    )
    request = asyncio.create_task(runtime.invoke(MODEL, {"messages": []}))
    await entered.wait()
    closing = asyncio.create_task(runtime.aclose())
    try:
        done, _ = await asyncio.wait({closing}, timeout=0.5)
        assert closing in done
        await closing
        assert provider.call_count == 0
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 1)
        await asyncio.gather(closing, request, return_exceptions=True)
    assert not [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith(("pi-context-", "pi-model-")) and not task.done()
    ]


@pytest.mark.asyncio
async def test_late_sync_awaitable_is_closed_without_executing_it():
    entered, release = threading.Event(), threading.Event()
    workers, returned, executed = [], [], []

    async def late():
        executed.append(True)
        return []

    def transform(messages, token):
        workers.append(threading.current_thread())
        entered.set()
        release.wait(2)
        result = late()
        returned.append(result)
        return result

    policy = ExecutionPolicy(
        transform_context=transform, transform_timeout_seconds=0.02
    )
    task = asyncio.create_task(
        policy.transform([], CancellationToken(), ExecutionContext("agent"))
    )
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        with pytest.raises(TimeoutError):
            await task
    finally:
        release.set()
        for worker in workers:
            await asyncio.to_thread(worker.join, 1)
        await asyncio.gather(task, return_exceptions=True)
    assert len(returned) == 1
    assert returned[0].cr_frame is None
    assert executed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("total", [0, 149, 151])
async def test_http_inconsistent_usage_is_unknown_and_cannot_fund_another_attempt(
    total,
):
    captured = []

    def respond(request):
        captured.append(request)
        chunk = {
            "choices": [
                {"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": total,
            },
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAICompatibleProvider(profile(), client=client)
        runtime = ModelCallRuntime(provider.stream)
        scope = admission()
        try:
            async with activate_model_attempt_admission(scope):
                first = await runtime.invoke(MODEL, {"messages": []})
                second = await runtime.invoke(MODEL, {"messages": []})
            assert first["stopReason"] == second["stopReason"] == "error"
            assert first["usageObserved"] is False
            assert not model_attempt_usage_known(first)
            assert len(captured) == 1
            snapshot = await scope.snapshot()
            assert snapshot.model_calls == snapshot.unknown_attempts == 1
        finally:
            await runtime.aclose()
            await provider.aclose()


@pytest.mark.asyncio
async def test_custom_provider_cannot_undercount_contradictory_usage():
    bad = message()
    bad["usage"].update(input=100, output=50, totalTokens=0)
    assert not model_attempt_usage_known(bad)
    assert model_attempt_usage(bad)[0] == 150
    provider = ScriptedProvider([bad, message()])
    runtime = ModelCallRuntime(provider.stream)
    scope = admission()
    try:
        async with activate_model_attempt_admission(scope):
            await runtime.invoke(MODEL, {"messages": []})
            final = await runtime.invoke(MODEL, {"messages": []})
        assert final["stopReason"] == "error"
        assert provider.call_count == 1
        snapshot = await scope.snapshot()
        assert snapshot.tokens == 150
        assert snapshot.unknown_attempts == 1
    finally:
        await runtime.aclose()


def test_python_bundle_cannot_drop_intent_capabilities():
    value = bundle()
    policy = replace(value.plan_policies["catalogue.lookup"], capabilities=())
    with pytest.raises(ValueError, match="capability conflict"):
        replace(value, plan_policies={"catalogue.lookup": policy})


@pytest.mark.parametrize(
    "field, replacement",
    [("required_fields", ()), ("optional_fields", ("unexpected",))],
)
def test_python_bundle_validates_intent_parameter_contract(field, replacement):
    value = bundle()
    intent = replace(value.router_config.intents[0], **{field: replacement})
    with pytest.raises(ValueError, match="parameter contract conflict"):
        replace(value, router_config=replace(value.router_config, intents=(intent,)))


@pytest.mark.parametrize("entrypoint", ["toml", "python"])
@pytest.mark.parametrize("reference", ["left", "expected_from"])
def test_all_bundle_entrypoints_reject_unknown_precondition_sources(
    entrypoint, reference
):
    value = bundle()
    condition = PlanIntentCondition(
        left=PlanIntentResultReference(
            "missing" if reference == "left" else "catalogue.lookup", ("value",)
        ),
        operator="eq",
        expected_from=PlanIntentResultReference("missing", ("value",))
        if reference == "expected_from"
        else None,
    )
    with pytest.raises(ValueError, match="unknown precondition source"):
        if entrypoint == "python":
            policy = replace(
                value.plan_policies["catalogue.lookup"], preconditions=(condition,)
            )
            replace(value, plan_policies={"catalogue.lookup": policy})
        else:
            root = tomllib.loads(
                (EXAMPLE / "business.toml").read_text(encoding="utf-8")
            )
            root["plans"][0]["preconditions"] = [condition.to_dict()]
            with patch("pi_agent_loop.business.tomllib.load", return_value=root):
                load_business_bundle(
                    EXAMPLE / "business.toml", tool_factories=factories()
                )


@pytest.mark.parametrize("entrypoint", ["toml", "python"])
def test_precondition_dependencies_participate_in_cycle_detection(entrypoint):
    value = bundle()
    condition = PlanIntentCondition(
        PlanIntentResultReference("catalogue.lookup", ("value",)), "truthy"
    )
    with pytest.raises(ValueError, match="cyclic"):
        if entrypoint == "python":
            policy = replace(
                value.plan_policies["catalogue.lookup"], preconditions=(condition,)
            )
            replace(value, plan_policies={"catalogue.lookup": policy})
        else:
            root = tomllib.loads(
                (EXAMPLE / "business.toml").read_text(encoding="utf-8")
            )
            root["plans"][0]["preconditions"] = [condition.to_dict()]
            with patch("pi_agent_loop.business.tomllib.load", return_value=root):
                load_business_bundle(
                    EXAMPLE / "business.toml", tool_factories=factories()
                )
