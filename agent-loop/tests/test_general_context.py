"""Dispatch evidence and proactive context regressions; no network services."""

import copy
import json

import pytest

from pi_agent_loop import (
    Agent,
    CancellationToken,
    ExecutionPolicy,
    Model,
    ModelCallRuntime,
    ScriptedProvider,
    assistant_message,
)
from pi_agent_loop.context import (
    ContextBudget,
    ContextBudgetExceeded,
    JournalModelRequestAudit,
)
from pi_agent_loop.session import (
    JournalPrincipal,
    SQLiteSessionEventJournal,
    StaticJournalKeyProvider,
)


MODEL = Model(id="offline", provider="test")


def answer():
    return assistant_message(model=MODEL, content=[{"type": "text", "text": "ok"}])


def audit_store(path, tenant="tenant"):
    journal = SQLiteSessionEventJournal(
        path,
        key_provider=StaticJournalKeyProvider(
            {"test": b"x" * 32}, active_key_id="test"
        ),
    )
    return JournalModelRequestAudit(journal, JournalPrincipal.system(tenant), "session")


@pytest.mark.asyncio
async def test_transformed_request_can_be_reconstructed_after_restart(tmp_path):
    path = tmp_path / "audit.db"
    audit = audit_store(path)
    provider = ScriptedProvider([answer()])
    marker = "retrieved-document-version-one"

    async def transform(messages, token, context):
        return messages + [
            {
                "role": "user",
                "content": marker,
                "source": {"id": "doc", "version": "one"},
            }
        ]

    original = {"systemPrompt": "compare", "messages": []}
    runtime = ModelCallRuntime(
        provider.stream,
        execution_policy=ExecutionPolicy(transform_context=transform),
        request_audit=audit,
    )
    try:
        await runtime.invoke(MODEL, original)
    finally:
        await runtime.aclose()
    restored = await audit_store(path).load()
    assert len(restored) == 1
    assert restored[0]["context"]["messages"] == provider.contexts[0]["messages"]
    assert marker in json.dumps(restored)
    assert original["messages"] == []
    assert marker.encode() not in path.read_bytes()
    assert await audit_store(path, "another-tenant").load() == ()


@pytest.mark.asyncio
async def test_failed_audit_prevents_provider_dispatch():
    async def unavailable(*args):
        raise OSError("audit unavailable")

    provider = ScriptedProvider([answer()])
    runtime = ModelCallRuntime(provider.stream, request_audit=unavailable)
    try:
        await runtime.invoke(MODEL, {"messages": []})
        assert provider.contexts == []
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_context_compacts_before_first_model_call_and_keeps_goal(tmp_path):
    messages = [
        {"role": "user", "content": "Compare all three products; cite your sources."}
    ]
    messages += [
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "old observation " * 500}],
        }
        for _ in range(20)
    ]
    messages += [{"role": "user", "content": "Produce the final report."}]
    before = copy.deepcopy(messages)
    budget = ContextBudget(
        5000, output_reserve=500, safety_margin=100, keep_recent_messages=2
    )
    audit = audit_store(tmp_path / "audit.db")
    provider = ScriptedProvider([answer()])
    runtime = ModelCallRuntime(
        provider.stream,
        execution_policy=ExecutionPolicy(context_budget=budget),
        request_audit=audit,
    )
    try:
        await runtime.invoke(MODEL, {"messages": messages})
    finally:
        await runtime.aclose()
    assert len(provider.contexts) == 1
    actual = provider.contexts[0]["messages"]
    assert len(json.dumps(actual)) < len(json.dumps(before))
    assert "Compare all three products" in json.dumps(actual)
    assert messages == before
    assert (await audit.load())[0]["context"]["messages"] == actual


@pytest.mark.asyncio
async def test_unfit_system_prompt_fails_before_dispatch():
    budget = ContextBudget(100, output_reserve=20, safety_margin=10)
    with pytest.raises(ContextBudgetExceeded):
        await budget.prepare(
            {"systemPrompt": "x" * 10000, "messages": []}, CancellationToken()
        )


@pytest.mark.asyncio
async def test_regular_agent_uses_same_preflight_budget():
    provider = ScriptedProvider([answer()])
    agent = Agent(
        model=MODEL,
        stream_fn=provider.stream,
        system_prompt="x" * 10000,
        execution_policy=ExecutionPolicy(
            context_budget=ContextBudget(100, output_reserve=20, safety_margin=10)
        ),
    )
    await agent.prompt("hello")
    assert provider.contexts == []
    assert agent.state.messages[-1]["stopReason"] == "error"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"context_window": True},
        {"context_window": 100},
        {"context_window": 9000, "safety_margin": -1},
        {"context_window": 9000, "version": ""},
    ],
)
def test_budget_rejects_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        ContextBudget(**kwargs)


def test_budget_change_changes_managed_policy_identity():
    a = ExecutionPolicy(context_budget=ContextBudget(9000))
    b = ExecutionPolicy(context_budget=ContextBudget(10000))
    assert a.configuration_version != b.configuration_version
