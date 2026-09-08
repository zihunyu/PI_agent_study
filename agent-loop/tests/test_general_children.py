"""Real Hosts behind a durable mailbox; failures occur between commit points."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys
import time

import pytest

from pi_agent_loop import DurableAgentHost, ScriptedProvider, SessionAlreadyOpenError
from pi_agent_loop.children import ChildSessionRequest, DurableChildSessionManager
from pi_agent_loop.general import create_general_agent_bundle
from pi_agent_loop.planning import ClosedLoopBudget
from pi_agent_loop.usage_meter import RuntimeUsageMeter
from pi_agent_loop.validation import ArtifactRequirement, TaskContract
from test_general_tasks import MODEL, planned, store


ALLOCATION = ClosedLoopBudget(
    max_model_calls=4,
    max_plan_steps=4,
    max_step_attempts=4,
    max_tool_calls=4,
    max_duration_seconds=60,
)
TOTAL = replace(
    ALLOCATION,
    max_model_calls=8,
    max_plan_steps=8,
    max_step_attempts=8,
    max_tool_calls=8,
    max_duration_seconds=120,
)


def setup(tmp_path, responses=None, *, lease_seconds=30):
    artifacts = store(tmp_path, session="parent")
    provider = ScriptedProvider(
        responses
        or [
            planned(
                [
                    {
                        "stepId": "save",
                        "intent": "tool.create_artifact",
                        "arguments": {
                            "name": "report.md",
                            "content": "Verified child artifact",
                        },
                        "dependsOn": [],
                    }
                ]
            )
        ]
    )

    async def factory(request):
        child_artifacts = store(tmp_path, session=request.session_id)
        bundle = create_general_agent_bundle(
            [],
            artifact_store=child_artifacts,
            task_contract=TaskContract(artifacts=(ArtifactRequirement("report.md"),)),
        )
        return await DurableAgentHost.create(
            session_id=request.session_id,
            state_dir=tmp_path / "children",
            tenant_id=request.tenant_id,
            model=MODEL,
            stream_fn=provider.stream,
            system_prompt="Complete this child task",
            general_bundle=bundle,
            plan_correction_budget=request.budget,
            plan_usage_meter=RuntimeUsageMeter(),
            exclusive_session=True,
            session_writer_lease_seconds=lease_seconds,
        )

    def manager():
        return DurableChildSessionManager(
            artifacts.records.journal,
            artifacts.records.principal,
            "parent",
            host_factories={"research": factory},
            budget=TOTAL,
            version="1",
            lease_seconds=lease_seconds,
        )

    return manager, provider, factory


@pytest.mark.asyncio
async def test_child_writer_contention_stays_pending_without_hiding_bad_backends(
    tmp_path,
):
    create, provider, factory = setup(tmp_path)
    manager = create()
    session = await manager.enqueue(
        "research",
        "request-1",
        "Create the report",
        factory="research",
        allocation=ALLOCATION,
    )
    request = ChildSessionRequest(
        "parent",
        session,
        "tenant",
        "research",
        "request-1",
        "Create the report",
        ALLOCATION,
        time.time() + 120,
    )
    existing = await factory(request)
    try:
        assert await manager.run_next("research") is None
        assert not provider.contexts and await manager.results() == ()
    finally:
        await existing.close()

    def unsupported(request):
        raise SessionAlreadyOpenError("backend lacks cross-process fencing")

    manager.factories["research"] = unsupported
    with pytest.raises(SessionAlreadyOpenError, match="fencing"):
        await manager.run_next("research")
    manager.factories["research"] = factory
    try:
        assert (await manager.run_next("research"))["status"] == "completed"
        assert len(provider.contexts) == 1
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_abrupt_parent_process_exit_recovers_child_without_redispatch(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "general_child_process.py"

    async def run(mode):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-X",
            "utf8",
            str(fixture),
            mode,
            str(tmp_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 45)
            return (
                process.returncode,
                stdout.decode("utf-8", "replace"),
                stderr.decode("utf-8", "replace"),
            )
        finally:
            if process.returncode is None:
                process.kill()
                await process.communicate()

    code, stdout, stderr = await run("crash")
    assert code == 91, (stdout, stderr)
    assert json.loads((tmp_path / "child-completed.json").read_text()) == {
        "modelCalls": 1,
        "status": "completed",
    }
    code, stdout, stderr = await run("recover")
    assert code == 0, (stdout, stderr)
    assert json.loads(stdout) == {
        "status": "completed",
        "recoveryModelCalls": 0,
        "results": 1,
        "artifacts": 1,
    }


@pytest.mark.asyncio
async def test_crash_after_child_completion_recovers_without_redispatch(
    tmp_path, monkeypatch
):
    create, provider, _ = setup(tmp_path)
    first = create()
    session = await first.enqueue(
        "research",
        "request-1",
        "Create the report",
        factory="research",
        allocation=ALLOCATION,
    )

    async def crash(*args):
        raise OSError("simulated parent crash before mailbox publication")

    monkeypatch.setattr(first, "_finish", crash)
    with pytest.raises(OSError, match="parent crash"):
        await first.run_next("research")
    assert len(provider.contexts) == 1
    await first.aclose()
    second = create()
    assert (
        await second.enqueue(
            "research",
            "request-1",
            "Create the report",
            factory="research",
            allocation=ALLOCATION,
        )
        == session
    )
    result = await second.run_next("research")
    assert result["status"] == "completed"
    assert len(provider.contexts) == 1
    assert await second.run_next("research") is None
    assert len(await second.results()) == 1
    await second.acknowledge("request-1", result["deliveryId"])
    assert await second.results() == ()
    assert len(await create().results(include_acknowledged=True)) == 1
    await second.aclose()


@pytest.mark.asyncio
async def test_budget_reservations_are_atomic_and_survive_restart(tmp_path):
    create, provider, _ = setup(tmp_path)
    left, right = create(), create()
    outcomes = await asyncio.gather(
        *(
            manager.enqueue(
                "child" + str(i),
                "message" + str(i),
                "Create report",
                factory="research",
                allocation=ALLOCATION,
            )
            for i, manager in enumerate((left, right, left))
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(value, str) for value in outcomes) == 2
    assert any(
        isinstance(value, ValueError) and "budget" in str(value) for value in outcomes
    )
    assert len((await create().status())["messages"]) == 2
    assert provider.contexts == []


@pytest.mark.asyncio
async def test_durable_cancel_and_identity_conflicts_do_not_dispatch(tmp_path):
    create, provider, _ = setup(tmp_path)
    manager = create()
    await manager.enqueue(
        "child", "message", "Create report", factory="research", allocation=ALLOCATION
    )
    with pytest.raises(ValueError, match="different"):
        await manager.enqueue(
            "child",
            "message",
            "Delete report",
            factory="research",
            allocation=ALLOCATION,
        )
    await manager.cancel("child")
    result = await create().run_next("child")
    assert result["status"] == "cancelled"
    assert provider.contexts == []
    with pytest.raises(ValueError, match="delivery"):
        await manager.acknowledge("message", "forged-delivery")


@pytest.mark.asyncio
async def test_submit_task_same_id_is_bound_to_original_text(tmp_path):
    create, provider, factory = setup(tmp_path)
    from pi_agent_loop.children import ChildSessionRequest
    import time

    request = ChildSessionRequest(
        "parent",
        "child",
        "tenant",
        "child",
        "request",
        "Create report",
        ALLOCATION,
        time.time() + 60,
    )
    host = await factory(request)
    try:
        results = await asyncio.gather(
            host.submit_task("request", "Create report"),
            host.submit_task("request", "Create report"),
        )
        assert results[0].plan_id == results[1].plan_id
        assert len(provider.contexts) == 1
        with pytest.raises(ValueError, match="different"):
            await host.submit_task("request", "Different instructions")
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_lost_child_claim_prevents_result_publication(tmp_path):
    create, _, _ = setup(tmp_path)
    manager = create()
    await manager.enqueue(
        "child", "message", "Create report", factory="research", allocation=ALLOCATION
    )
    journal, principal = manager.records.journal, manager.records.principal
    claim = await journal.acquire_fenced_claim(
        principal, "durable_child", "scope", "old", lease_seconds=1
    )
    await journal.release_fenced_claim(principal, claim)
    replacement = await journal.acquire_fenced_claim(
        principal, "durable_child", "scope", "new", lease_seconds=1
    )
    with pytest.raises(Exception, match="[Ff]enc|[Cc]laim|租约"):
        await manager._finish(
            "message",
            {
                "status": "completed",
                "responseText": "forged",
                "planId": None,
                "approvalIds": [],
            },
            claim,
        )
    assert await manager.results() == ()
    await journal.release_fenced_claim(principal, replacement)


@pytest.mark.asyncio
async def test_active_child_cancel_is_observed_by_another_controller(tmp_path):
    create, provider, _ = setup(tmp_path)
    entered = asyncio.Event()
    closed = asyncio.Event()
    original = provider.stream

    async def slow(model, context, options):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            closed.set()
        return original(model, context, options)

    provider.stream = slow
    manager = create()
    await manager.enqueue(
        "child", "message", "Create report", factory="research", allocation=ALLOCATION
    )
    task = asyncio.create_task(manager.run_next("child"))
    await asyncio.wait_for(entered.wait(), 15)
    await create().cancel("child")
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 15)
    assert closed.is_set()
    assert (await create().run_next("child"))["status"] == "cancelled"
    assert not manager._active
    assert not [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith("durable-child-") and not task.done()
    ]


@pytest.mark.asyncio
async def test_single_model_call_budget_still_allows_deterministic_acceptance(tmp_path):
    _, provider, factory = setup(tmp_path)
    from pi_agent_loop.children import ChildSessionRequest
    import time

    request = ChildSessionRequest(
        "parent",
        "one-call",
        "tenant",
        "one-call",
        "one",
        "Create report",
        replace(ALLOCATION, max_model_calls=1),
        time.time() + 60,
    )
    host = await factory(request)
    try:
        result = await host.submit_task("one", "Create report")
        assert result.autonomous_result.status == "completed"
        record = await host.autonomous_run_store.load(
            result.autonomous_result.closed_loop_run_id
        )
        assert record.resource_usage.model_calls == 1
        assert len(provider.contexts) == 1
    finally:
        await host.close()
