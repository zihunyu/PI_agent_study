"""Offline contracts for the public generic extension boundaries."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from pi_agent_loop import (
    Agent,
    AfterToolCallResult,
    BusinessBundle,
    CancellationToken,
    ContentSafetyPipeline,
    DurableAgentHost,
    ExecutionContext,
    ExecutionPolicy,
    GenerationOptions,
    Model,
    ModelCallRuntime,
    ModelRequestPolicy,
    SafetyDecision,
    ScriptedProvider,
    SessionConfigurationMismatchError,
    assistant_message,
    load_business_bundle,
)
from pi_agent_loop.providers import (
    OpenAICompatibleProvider,
    ProviderConfigError,
    ProviderProfile,
    ProviderProtocolError,
    serialize_chat_request,
)

MODEL = Model(id="offline", provider="test", api="scripted")
EXAMPLE = Path(__file__).parents[1] / "examples" / "business_package"


def message(text="ok"):
    return assistant_message(model=MODEL, content=[{"type": "text", "text": text}])


def factories():
    spec = importlib.util.spec_from_file_location(
        "external_business_tools", EXAMPLE / "tools.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Client:
        async def lookup(self, key):
            return f"entry:{key}"

    return module.tool_factories(Client())


def bundle(path=None):
    return load_business_bundle(
        path or EXAMPLE / "business.toml", tool_factories=factories()
    )


def profile(**generation):
    return ProviderProfile(
        name="test",
        protocol="openai_chat_completions",
        base_url="https://offline.example/v1",
        endpoint="/chat/completions",
        auth_type="bearer",
        api_key="offline",
        model="offline",
        stream=True,
        connect_timeout_seconds=1,
        request_timeout_seconds=2,
        allow_insecure_http=False,
        generation=GenerationOptions.from_mapping(generation),
    )


def test_external_bundle_maps_all_public_catalogues():
    value = bundle()
    assert isinstance(value, BusinessBundle)
    assert value.tools == tuple(value.capabilities.all_tools())
    assert value.plan_tool_bindings == {"catalogue.lookup": "fictional_lookup"}
    assert value.plan_policies["catalogue.lookup"].parameter_contract.to_dict() == {
        "required": {"key": "string"},
        "optional": {},
        "allowEmpty": False,
    }
    with pytest.raises(TypeError):
        value.plan_tool_bindings["unexpected"] = "fictional_lookup"


@pytest.mark.parametrize(
    "change",
    [
        lambda text: text + '\n[[tools]]\nname="fictional_lookup"\n',
        lambda text: text.replace('tool = "fictional_lookup"', 'tool = "missing"'),
        lambda text: text.replace(
            'capability = "catalogue.read"', 'capability = "missing"'
        ),
        lambda text: text.replace(
            'required_fields = ["key"]', 'required_fields = ["wrong"]'
        ),
        lambda text: (
            text + '\n[[plans]]\nintent="catalogue.lookup"\ntool="fictional_lookup"\n'
        ),
        lambda text: text.replace("side_effect = false", 'side_effect = "false"'),
        lambda text: text.replace("[product]", "unexpected = true\n[product]"),
        lambda text: text + '\nreplayPolicy="wrong"\n',
        lambda text: text + '\ncapabilities=["missing"]\n',
        lambda text: text + '\nrequiredPredecessorIntents=["missing"]\n',
    ],
)
def test_bundle_rejects_configuration_conflicts_before_start(tmp_path, change):
    path = tmp_path / "business.toml"
    path.write_text(change((EXAMPLE / "business.toml").read_text()), encoding="utf-8")
    with pytest.raises((ValueError, TypeError)):
        bundle(path)


def test_bundle_factory_identity_and_missing_implementation():
    with pytest.raises(ValueError, match="missing tool factory"):
        load_business_bundle(EXAMPLE / "business.toml", tool_factories={})
    with pytest.raises(ValueError, match="must return AgentTool"):
        load_business_bundle(
            EXAMPLE / "business.toml",
            tool_factories={"fictional_lookup": lambda: object()},
        )


@pytest.mark.asyncio
async def test_bundle_host_plan_uses_shared_runtime_and_policy(tmp_path):
    audit = []
    transforms = []

    class Safety:
        async def inspect(self, inspection, token):
            audit.append(inspection)
            return SafetyDecision.allow()

    async def transform(messages, token, context):
        transforms.append(context)
        return messages

    provider = ScriptedProvider(
        [
            message(
                json.dumps(
                    {
                        "steps": [
                            {
                                "stepId": "lookup",
                                "intent": "catalogue.lookup",
                                "arguments": {"key": "BLUE"},
                            }
                        ]
                    }
                )
            )
        ]
    )
    host = await DurableAgentHost.create(
        session_id="bundle",
        state_dir=tmp_path,
        model=MODEL,
        stream_fn=provider.stream,
        system_prompt="offline",
        business_bundle=bundle(),
        execution_policy=ExecutionPolicy(
            ContentSafetyPipeline([Safety()]), transform, version="review-1"
        ),
    )
    try:
        plan = await host.plan("Read BLUE")
        result = await host.execute_plan(plan.plan_id)
        assert result.state.phase == "completed"
        assert provider.call_count == 1
        assert transforms == [ExecutionContext("planner", "local", "bundle")]
        assert [item.stage for item in audit] == [
            "model_input",
            "model_output",
            "tool_output",
        ]
    finally:
        await host.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "legacy",
    [
        "tools",
        "router",
        "capabilities",
        "plan_policies",
        "plan_tool_bindings",
        "planner",
        "plan_step_executor",
    ],
)
async def test_bundle_rejects_mixed_assembly(tmp_path, legacy):
    with pytest.raises(ValueError, match="cannot be mixed"):
        await DurableAgentHost.create(
            session_id="mixed",
            state_dir=tmp_path,
            model=MODEL,
            stream_fn=ScriptedProvider().stream,
            system_prompt="",
            business_bundle=bundle(),
            **{legacy: []},
        )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "options",
    [
        {"max_tokens": True},
        {"max_tokens": 0},
        {"max_tokens": 1.2},
        {"max_completion_tokens": -1},
        {"max_tokens": 1, "max_completion_tokens": 2},
        {"temperature": float("inf")},
        {"temperature": float("nan")},
        {"temperature": -0.1},
        {"temperature": 2.1},
        {"temperature": True},
        {"top_p": -0.1},
        {"top_p": 1.1},
        {"top_p": "0.5"},
        {"stream_options": {"include_usage": 1}},
        {"stream_options": {"other": True}},
        {"extra_body": {"model": "untrusted"}},
    ],
)
def test_generation_rejects_invalid_settings(options):
    with pytest.raises(ProviderConfigError):
        GenerationOptions.from_mapping(options)


def test_generation_unspecified_fields_omitted_and_overrides_validated():
    context = {"systemPrompt": "", "messages": [], "tools": []}
    request = serialize_chat_request(profile(), context, {})
    assert not (
        {
            "max_tokens",
            "max_completion_tokens",
            "temperature",
            "top_p",
            "stream_options",
        }
        & request.keys()
    )
    request = serialize_chat_request(
        profile(max_tokens=200, temperature=0.8),
        context,
        {"max_tokens": 100, "temperature": 0, "top_p": 1},
    )
    assert (request["max_tokens"], request["temperature"], request["top_p"]) == (
        100,
        0,
        1,
    )
    with pytest.raises(ProviderProtocolError):
        serialize_chat_request(
            profile(max_tokens=100), context, {"max_completion_tokens": 50}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage, known",
    [
        (None, False),
        ({}, False),
        ({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, True),
    ],
)
async def test_http_request_generation_and_usage_only_chunks(usage, known):
    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        chunks = [
            {
                "choices": [
                    {"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}
                ]
            }
        ]
        if usage is not None:
            chunks.append({"choices": [], "usage": usage})
        body = (
            "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
            + "data: [DONE]\n\n"
        )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=body
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAICompatibleProvider(
            profile(max_completion_tokens=200, top_p=0.9), client=client
        )
        try:
            stream = provider.stream(
                MODEL,
                {"messages": [], "tools": []},
                {
                    "temperature": 0.5,
                    "max_completion_tokens": 80,
                    "stream_options": {"include_usage": True},
                },
            )
            _events = [event async for event in stream]
            final = await stream.result()
            assert final["usageObserved"] is known
            assert captured[0]["stream_options"] == {"include_usage": True}
            assert captured[0]["max_completion_tokens"] == 80
            assert captured[0]["temperature"] == 0.5
            assert captured[0]["top_p"] == 0.9
        finally:
            await provider.aclose()


@pytest.mark.asyncio
async def test_host_policy_blocks_raw_output_and_recovery_and_rechecks_hooks(tmp_path):
    audit = []
    hooks = []

    class Safety:
        async def inspect(self, inspection, token):
            audit.append(inspection)
            if inspection.stage == "model_output" and "RAW_SECRET" in str(
                inspection.value
            ):
                return SafetyDecision.block(reason="blocked")
            if inspection.stage == "tool_output":
                clean = copy.deepcopy(inspection.value)
                clean["content"] = [{"type": "text", "text": "safe"}]
                return SafetyDecision.replace(clean)
            return SafetyDecision.allow()

    async def hook(context, token):
        hooks.append(context.result.content)
        return AfterToolCallResult(content=[{"type": "text", "text": "HOOK_SECRET"}])

    provider = ScriptedProvider([message("RAW_SECRET"), message("RAW_SECRET")])
    host = await DurableAgentHost.create(
        session_id="policy",
        state_dir=tmp_path,
        model=MODEL,
        stream_fn=provider.stream,
        system_prompt="",
        tools=[],
        execution_policy=ExecutionPolicy(ContentSafetyPipeline([Safety()])),
        after_tool_call=hook,
    )
    observed = []
    host.agent.subscribe(lambda event, token: observed.append(event))
    try:
        await host.prompt("hello")
        assert "RAW_SECRET" not in str(observed) + str(host.agent.state.messages)
        with pytest.raises(RuntimeError) as failure:
            await host.model_runtime.request([], policy=ModelRequestPolicy())
        assert "RAW_SECRET" not in str(failure.value)
        assert {item.metadata.get("phase") for item in audit} >= {"agent", "recovery"}
        # The same guarded after-hook is installed in the shared Plan/Tool Runtime.
        from pi_agent_loop import AfterToolCallContext, AgentContext, AgentToolResult

        tool_context = AfterToolCallContext(
            assistant_message=message(),
            tool_call={"id": "1", "name": "test"},
            args={},
            result=AgentToolResult(content=[{"type": "text", "text": "RAW_SECRET"}]),
            is_error=False,
            context=AgentContext(system_prompt="", messages=[], tools=[]),
        )
        result = await host.tool_dispatch_runtime.after_tool_call(
            tool_context, CancellationToken()
        )
        assert hooks == [[{"type": "text", "text": "safe"}]]
        assert result.content == [{"type": "text", "text": "safe"}]
        assert sum(item.stage == "tool_output" for item in audit) == 2
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_execution_transform_legacy_and_new_signature_once():
    calls = []

    async def legacy(messages, token):
        calls.append("legacy")
        return messages

    provider = ScriptedProvider([message(), message()])
    agent = Agent(model=MODEL, stream_fn=provider.stream, transform_context=legacy)
    await agent.prompt("hello")
    assert calls == ["legacy"]
    with pytest.raises(ValueError, match="cannot be mixed"):
        Agent(
            model=MODEL,
            stream_fn=provider.stream,
            transform_context=legacy,
            execution_policy=ExecutionPolicy(),
        )
    phases = []

    async def modern(messages, token, context):
        phases.append(context)
        return messages + [
            {"role": "user", "content": [{"type": "text", "text": "authorized-memory"}]}
        ]

    agent = Agent(
        model=MODEL,
        stream_fn=provider.stream,
        execution_policy=ExecutionPolicy(transform_context=modern),
        tenant_id="tenant",
        session_id="session",
    )
    await agent.prompt("hello")
    assert phases == [ExecutionContext("agent", "tenant", "session")]
    assert str(provider.contexts[-1]).count("authorized-memory") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [True, False])
async def test_transform_timeout_cancellation_drains_tasks(cancel):
    entered = asyncio.Event()
    finished = asyncio.Event()

    async def transform(messages, token, context):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            finished.set()

    policy = ExecutionPolicy(
        transform_context=transform, transform_timeout_seconds=0.05
    )
    provider = ScriptedProvider()
    runtime = ModelCallRuntime(provider.stream, execution_policy=policy)
    token = CancellationToken()
    try:
        stream = runtime.stream(MODEL, {"messages": []}, {"cancellation_token": token})
        await entered.wait()
        if cancel:
            token.cancel("cancel test")
        _events = [event async for event in stream]
        final = await stream.result()
        assert final["stopReason"] in {"error", "aborted"}
        assert provider.call_count == 0
        assert finished.is_set()
        assert not [
            task
            for task in asyncio.all_tasks()
            if task.get_name().startswith("pi-context-") and not task.done()
        ]
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_managed_session_policy_version_and_trusted_callback_reinjection(
    tmp_path,
):
    calls = []

    async def transform(messages, token, context):
        calls.append(context.session_id)
        return messages

    options = dict(
        session_id="managed",
        state_dir=tmp_path,
        workspace_path=tmp_path,
        managed_session=True,
        model=MODEL,
        system_prompt="",
        tools=[],
    )
    first = await DurableAgentHost.create(
        **options,
        stream_fn=ScriptedProvider([message()]).stream,
        execution_policy=ExecutionPolicy(transform_context=transform, version="one"),
    )
    await first.prompt("hello")
    await first.close()
    with pytest.raises(SessionConfigurationMismatchError):
        await DurableAgentHost.create(
            **options,
            stream_fn=ScriptedProvider().stream,
            execution_policy=ExecutionPolicy(version="two"),
        )
    second = await DurableAgentHost.create(
        **options,
        stream_fn=ScriptedProvider([message()]).stream,
        execution_policy=ExecutionPolicy(transform_context=transform, version="one"),
    )
    try:
        await second.prompt("again")
        assert calls == ["managed", "managed"]
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_generation_http_retries_keep_one_valid_configuration_and_reject_before_dispatch():
    from pi_agent_loop import ModelRetryPolicy

    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        if len(captured) == 1:
            return httpx.Response(503)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content='data: {"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\ndata: [DONE]\n\n',
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAICompatibleProvider(
            replace(
                profile(max_tokens=80, temperature=0),
                retry_policy=ModelRetryPolicy(
                    enabled=True, max_retries=1, initial_delay_seconds=0, jitter_ratio=0
                ),
            ),
            client=client,
        )
        try:
            with pytest.raises(ProviderProtocolError):
                provider.stream(MODEL, {"messages": []}, {"temperature": False})
            assert not captured and provider.attempt_count == 0
            options = {"max_tokens": 40, "stream_options": {"include_usage": True}}
            stream = provider.stream(MODEL, {"messages": []}, options)
            options["max_tokens"] = 999
            options["stream_options"]["include_usage"] = False
            _events = [event async for event in stream]
            assert len(captured) == 2
            assert captured[0] == captured[1]
            assert captured[0]["max_tokens"] == 40
            assert captured[0]["stream_options"] == {"include_usage": True}
        finally:
            await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit, expected", [(50, 50), (200, 80)])
async def test_generation_budget_can_only_tighten_http_token_limit(limit, expected):
    from pi_agent_loop.model_attempts import (
        ModelAttemptAdmissionScope,
        activate_model_attempt_admission,
    )

    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content='data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":0,"completion_tokens":0}}\n\ndata: [DONE]\n\n',
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAICompatibleProvider(
            profile(max_completion_tokens=80), client=client
        )
        try:
            scope = ModelAttemptAdmissionScope(
                run_id="run",
                stage="router",
                reservation_id="ticket",
                max_model_calls=1,
                max_tokens=limit,
            )
            async with activate_model_attempt_admission(scope):
                stream = provider.stream(MODEL, {"messages": []}, {})
                _events = [event async for event in stream]
                assert (await stream.result())["usageObserved"]
            assert captured[0]["max_completion_tokens"] == expected
            assert (await scope.snapshot()).unknown_attempts == 0
        finally:
            await provider.aclose()


def test_provider_toml_generation_defaults(tmp_path):
    from pi_agent_loop import load_provider_settings

    config = (EXAMPLE.parents[1] / "config" / "providers.toml.example").read_text(
        encoding="utf-8"
    )
    config = config.replace("REPLACE_WITH_YOUR_API_KEY", "offline-token").replace(
        "REPLACE_WITH_PROVIDER_MODEL_ID", "offline"
    )
    config += "\n[profiles.third_party.generation]\nmax_tokens=20\ntemperature=0.3\n[profiles.third_party.generation.stream_options]\ninclude_usage=true\n"
    path = tmp_path / "provider.toml"
    path.write_text(config, encoding="utf-8")
    generation = load_provider_settings(path).active.generation
    assert generation == GenerationOptions(
        max_tokens=20, temperature=0.3, include_usage=True
    )


def test_business_bundle_configuration_cannot_lower_tool_approval(tmp_path):
    original = factories()["fictional_lookup"]()
    guarded = replace(original, requires_approval=True)
    path = tmp_path / "approval.toml"
    path.write_text(
        (EXAMPLE / "business.toml").read_text(encoding="utf-8")
        + '\napprovalRoles=["reviewer"]\nrequiresApproval=false\n',
        encoding="utf-8",
    )
    value = load_business_bundle(
        path, tool_factories={"fictional_lookup": lambda: guarded}
    )
    assert value.plan_policies["catalogue.lookup"].requires_approval
    assert value.tools[0].requires_approval


@pytest.mark.asyncio
async def test_concurrent_runtime_requests_transform_once_without_history_mutation():
    seen = []

    async def transform(messages, token, context):
        seen.append(context)
        await asyncio.sleep(0)
        return messages + [
            {
                "role": "user",
                "content": [{"type": "text", "text": "authorized-context"}],
            }
        ]

    provider = ScriptedProvider([message(), message()])
    runtime = ModelCallRuntime(
        provider.stream,
        execution_policy=ExecutionPolicy(transform_context=transform),
        tenant_id="tenant",
        session_id="session",
    )
    context = {"messages": []}
    try:
        await asyncio.gather(
            runtime.invoke(MODEL, context, {"model_request_source": "router"}),
            runtime.invoke(MODEL, context, {"model_request_source": "planner"}),
        )
        assert sorted(item.phase for item in seen) == ["planner", "router"]
        assert all(
            str(item).count("authorized-context") == 1 for item in provider.contexts
        )
        assert context == {"messages": []}
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
async def test_unreviewed_tool_progress_never_bypasses_output_safety(tmp_path, durable):
    from pi_agent_loop import AgentTool, AgentToolResult

    class Safety:
        async def inspect(self, inspection, token):
            if inspection.stage == "tool_output":
                return SafetyDecision.block(reason="review rejected")
            return SafetyDecision.allow()

    async def execute(call_id, arguments, token, update):
        update(
            AgentToolResult(content=[{"type": "text", "text": "RAW_PROGRESS_SECRET"}])
        )
        await asyncio.sleep(0)
        return AgentToolResult(content=[{"type": "text", "text": "RAW_FINAL_SECRET"}])

    tool = AgentTool(
        name="read",
        label="read",
        description="read",
        execute=execute,
        replay_policy="safe",
    )
    first = message()
    first.update(
        stopReason="toolUse",
        content=[{"type": "toolCall", "id": "read-1", "name": "read", "arguments": {}}],
    )
    provider = ScriptedProvider([first, message()])
    policy = ExecutionPolicy(content_safety=ContentSafetyPipeline([Safety()]))
    if durable:
        host = await DurableAgentHost.create(
            session_id="progress",
            state_dir=tmp_path,
            model=MODEL,
            stream_fn=provider.stream,
            system_prompt="",
            tools=[tool],
            execution_policy=policy,
        )
        agent = host.agent
    else:
        host = None
        agent = Agent(
            model=MODEL,
            stream_fn=provider.stream,
            tools=[tool],
            execution_policy=policy,
        )
    events = []
    agent.subscribe(lambda event, token: events.append(event))
    try:
        if host is not None:
            await host.prompt("read")
        else:
            await agent.prompt("read")
        assert "RAW_PROGRESS_SECRET" not in str(events)
        assert "RAW_FINAL_SECRET" not in str(events) + str(agent.state.messages)
    finally:
        if host is not None:
            await host.close()
