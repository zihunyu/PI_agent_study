"""The same journal/transaction contracts through a non-inheriting adapter."""

from __future__ import annotations

import inspect
from dataclasses import replace

import pytest

from pi_agent_loop import (
    AutonomousConversationProjector,
    ClosedLoopBudget,
    DurableAgentHost,
    DurableHostResources,
    JournalOperationStore,
    JournalPlanStore,
    JournalPrincipal,
    JournalRunStore,
    MultiIntentPlan,
    PlanStep,
    SessionEventJournal,
    SessionJournalAutonomousRunStore,
    SessionJournalOperationEventStore,
    SessionJournalPlanStore,
    SessionJournalRetryEventStore,
    SessionJournalRuntimeEventStore,
    SQLiteSessionEventJournal,
    StaticJournalKeyProvider,
    validate_session_event_journal,
)
from pi_agent_loop.session import (
    JournalConflictError,
    JournalFencedClaimLostError,
    SessionEventSpec,
)
from test_generic_extensions import MODEL, bundle, message
from pi_agent_loop import ScriptedProvider


def public_adapter(protocol, implementation):
    """Composition deliberately exposes only the declared public protocol."""
    methods = {}
    for base in reversed(protocol.__mro__):
        for name, member in vars(base).items():
            if name.startswith("_"):
                continue
            if isinstance(member, property):
                methods[name] = property(
                    lambda self, name=name: getattr(self.implementation, name)
                )
            elif callable(member):
                if inspect.iscoroutinefunction(member):

                    async def forward(self, *args, _name=name, **kwargs):
                        return await getattr(self.implementation, _name)(
                            *args, **kwargs
                        )
                else:

                    def forward(self, *args, _name=name, **kwargs):
                        return getattr(self.implementation, _name)(*args, **kwargs)

                methods[name] = forward
    for base in protocol.__mro__:
        for name in getattr(base, "__annotations__", {}):
            methods.setdefault(
                name,
                property(lambda self, name=name: getattr(self.implementation, name)),
            )
    adapter = type("External" + protocol.__name__, (), methods)()
    adapter.implementation = implementation
    return adapter


@pytest.fixture(params=[False, True], ids=["sqlite", "public-composition"])
def journal(request, tmp_path):
    implementation = SQLiteSessionEventJournal(
        tmp_path / "state.sqlite3",
        key_provider=StaticJournalKeyProvider(
            {"test": b"x" * 32}, active_key_id="test"
        ),
    )
    return (
        public_adapter(SessionEventJournal, implementation)
        if request.param
        else implementation
    )


@pytest.mark.asyncio
async def test_journal_multistream_cas_rollback_and_old_fencing(journal):
    principal = JournalPrincipal.system("tenant")
    validate_session_event_journal(journal)
    first = SessionEventSpec("retry", "a", "session", {}, operation_id="plan")
    second = SessionEventSpec(
        "operation", "b", "session", {}, operation_id="conversation"
    )
    await journal.append_events(
        principal,
        [first, second],
        expected_stream_sequences={
            ("retry", "session", "plan"): -1,
            ("operation", "session", "conversation"): -1,
        },
    )
    before = await journal.load_events(principal, session_id="session")
    with pytest.raises(JournalConflictError):
        await journal.append_events(
            principal,
            [replace(first, event_type="next"), replace(second, event_type="next")],
            expected_stream_sequences={
                ("retry", "session", "plan"): 0,
                ("operation", "session", "conversation"): -1,
            },
        )
    assert await journal.load_events(principal, session_id="session") == before
    old = await journal.acquire_fenced_claim(
        principal, "test", "resource", "owner-a", lease_seconds=30
    )
    await journal.release_fenced_claim(principal, old)
    fresh = await journal.acquire_fenced_claim(
        principal, "test", "resource", "owner-b", lease_seconds=30
    )
    assert fresh.generation > old.generation
    with pytest.raises(JournalFencedClaimLostError):
        await journal.append_events_if_fenced_claim(
            principal, [replace(first, event_type="stale")], old, renew_lease_seconds=30
        )
    assert await journal.load_events(principal, session_id="session") == before
    assert await journal.verify_fenced_claim(principal, fresh)


@pytest.mark.asyncio
async def test_journal_snapshot_projection_and_async_retry_discovery(journal):
    principal = JournalPrincipal.system("tenant")
    await journal.append_events(
        principal, [SessionEventSpec("retry", "example", "session", {"value": 1})]
    )
    await journal.save_snapshot(
        principal,
        session_id="session",
        projection_name="total",
        last_sequence=0,
        state={"total": 1},
        state_version=1,
    )
    snapshot = await journal.load_snapshot(
        principal, session_id="session", projection_name="total"
    )
    assert snapshot.state == {"total": 1}
    replay = await journal.replay_projection(
        principal,
        session_id="session",
        projection_name="total",
        initial_state={"total": 0},
        reducer=lambda state, event: {"total": state["total"] + event.payload["value"]},
    )
    assert replay.state == {"total": 1}
    retries = SessionJournalRetryEventStore(journal, principal, session_id="session")
    assert await retries.incomplete_chains_async() == []
    if not isinstance(journal, SQLiteSessionEventJournal):
        assert not hasattr(journal, "_load_events_sync")
        with pytest.raises(TypeError, match="incomplete_chains_async"):
            retries.incomplete_chains()
    else:
        assert retries.incomplete_chains() == []


@pytest.mark.asyncio
async def test_three_stream_bootstrap_and_completion_idempotency(journal):
    principal = JournalPrincipal.system("tenant")
    plans = public_adapter(
        JournalPlanStore,
        SessionJournalPlanStore(journal, principal, session_id="session"),
    )
    runs = public_adapter(
        JournalRunStore,
        SessionJournalAutonomousRunStore(journal, principal, session_id="session"),
    )
    operations = public_adapter(
        JournalOperationStore, SessionJournalOperationEventStore(journal, principal)
    )
    assert isinstance(plans, JournalPlanStore) and isinstance(runs, JournalRunStore)
    projector = AutonomousConversationProjector(
        operations, session_id="session", model=MODEL
    )
    plan = MultiIntentPlan("read", (PlanStep("read", "read"),), plan_id="plan")
    link = await projector.bootstrap(
        "read",
        run_id="run",
        plan=plan,
        budget=ClosedLoopBudget(),
        initial_messages=[],
        run_store=runs,
        plan_store=plans,
    )
    assert (await plans.load("plan")).dispatchable
    assert (await runs.load("run")).initial_plan_bound
    assert await projector.find("run") == link
    await projector.finalize(link, response_text="complete", status="completed")
    before = await journal.load_events(principal, session_id="session")
    await projector.finalize(link, response_text="complete", status="completed")
    assert await journal.load_events(principal, session_id="session") == before


def test_inadequate_journal_capabilities_fail_closed(journal):
    with pytest.raises(ValueError, match="topology"):
        validate_session_event_journal(journal, require_multi_host=True)
    invalid = public_adapter(SessionEventJournal, journal)
    type(invalid).capabilities = replace(
        journal.capabilities, atomic_fenced_append=False
    )
    with pytest.raises(ValueError, match="fencing"):
        SessionJournalPlanStore(
            invalid, JournalPrincipal.system("tenant"), session_id="s"
        )
    with pytest.raises(TypeError, match="SessionEventJournal"):
        validate_session_event_journal(object())


@pytest.mark.asyncio
async def test_non_inherited_journal_and_plan_adapter_host_and_scope_rejection(
    tmp_path,
):
    journal = public_adapter(
        SessionEventJournal,
        SQLiteSessionEventJournal(
            tmp_path / "state.sqlite3",
            key_provider=StaticJournalKeyProvider(
                {"test": b"x" * 32}, active_key_id="test"
            ),
        ),
    )
    principal = JournalPrincipal.system("local")
    plans = public_adapter(
        JournalPlanStore,
        SessionJournalPlanStore(journal, principal, session_id="session"),
    )

    def resources(request):
        return DurableHostResources(
            request.state_dir,
            SessionJournalOperationEventStore(journal, principal),
            SessionJournalRuntimeEventStore(
                journal, principal, session_id=request.session_id
            ),
            SessionJournalRetryEventStore(
                journal, principal, session_id=request.session_id
            ),
            journal,
            principal,
        )

    provider = ScriptedProvider(
        [
            message(
                '{"steps":[{"stepId":"lookup","intent":"catalogue.lookup","arguments":{"key":"BLUE"}}]}'
            )
        ]
    )
    host = await DurableAgentHost.create(
        session_id="session",
        state_dir=tmp_path,
        model=MODEL,
        stream_fn=provider.stream,
        system_prompt="",
        business_bundle=bundle(),
        plan_store=plans,
        resource_factory=resources,
    )
    try:
        plan = await host.plan("Read BLUE")
        assert (await host.execute_plan(plan.plan_id)).state.phase == "completed"
        assert host.plan_store is plans
    finally:
        await host.close()
    bad_plans = public_adapter(
        JournalPlanStore,
        SessionJournalPlanStore(journal, principal, session_id="wrong-session"),
    )
    with pytest.raises(ValueError, match="session"):
        await DurableAgentHost.create(
            session_id="session",
            state_dir=tmp_path,
            model=MODEL,
            stream_fn=provider.stream,
            system_prompt="",
            business_bundle=bundle(),
            plan_store=bad_plans,
            resource_factory=resources,
        )


@pytest.mark.asyncio
async def test_retry_recovery_prefers_async_public_discovery(journal):
    from pi_agent_loop.retry.events import RetryRecoveryManager

    retries = SessionJournalRetryEventStore(
        journal, JournalPrincipal.system("tenant"), session_id="session"
    )
    await retries.append(
        {
            "type": "model_retry_scheduled",
            "retryId": "retry",
            "kind": "model",
            "attempt": 1,
        }
    )
    recovered = []

    async def handle(chain):
        recovered.append(chain.retry_id)
        return True

    await RetryRecoveryManager(retries).recover(handle)
    await RetryRecoveryManager(retries).recover(handle)
    assert recovered == ["retry"]


@pytest.mark.asyncio
async def test_three_stream_transaction_rolls_back_after_partial_insert(journal):
    implementation = (
        journal
        if isinstance(journal, SQLiteSessionEventJournal)
        else journal.implementation
    )
    insert = implementation._insert_specs_sync

    def fail_after_two(connection, actor, specs):
        insert(connection, actor, specs[:2])
        raise RuntimeError("injected transaction failure")

    implementation._insert_specs_sync = fail_after_two
    principal = JournalPrincipal.system("tenant")
    plans = public_adapter(
        JournalPlanStore,
        SessionJournalPlanStore(journal, principal, session_id="session"),
    )
    runs = public_adapter(
        JournalRunStore,
        SessionJournalAutonomousRunStore(journal, principal, session_id="session"),
    )
    operations = public_adapter(
        JournalOperationStore, SessionJournalOperationEventStore(journal, principal)
    )
    projector = AutonomousConversationProjector(
        operations, session_id="session", model=MODEL
    )
    plan = MultiIntentPlan("read", (PlanStep("read", "read"),), plan_id="plan")
    try:
        with pytest.raises(RuntimeError, match="injected"):
            await projector.bootstrap(
                "read",
                run_id="run",
                plan=plan,
                budget=ClosedLoopBudget(),
                initial_messages=[],
                run_store=runs,
                plan_store=plans,
            )
    finally:
        implementation._insert_specs_sync = insert
    assert await journal.load_events(principal, session_id="session") == []
    assert await plans.list_runnable_plans() == ()
    assert await projector.find("run") is None
