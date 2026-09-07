"""Fault and concurrency regressions from the framework source review."""

from __future__ import annotations

import asyncio
import threading

import pytest

from pi_agent_loop import (
    Agent,
    AgentTool,
    AgentToolResult,
    BoundedRunStateStore,
    CancellationToken,
    ContentSafetyPipeline,
    ExactMatchArbitrator,
    Model,
    MultiAgentError,
    MultiAgentOrchestrator,
    MultiAgentPlan,
    MultiAgentTask,
    OrchestrationLimits,
    SafetyDecision,
    ScriptedProvider,
    ToolServices,
    WorkerOutput,
    WorkerRegistration,
    WorkerRegistry,
    WorkspaceToolError,
    assistant_message,
    create_builtin_tools,
)
from pi_agent_loop.cancellation import OperationCancelledError
from pi_agent_loop.tools.atomic_writer import AtomicFileWriter
from pi_agent_loop.tools.mutation_queue import FileMutationQueue
from pi_agent_loop.tools.path_policy import WorkspacePathPolicy


class SuccessfulRunner:
    def __init__(self):
        self.calls = []

    async def run(self, request):
        self.calls.append(request.task_id)
        return WorkerOutput(text="synthetic success")


def workers(runner, count=1):
    return WorkerRegistry(
        [
            WorkerRegistration(f"worker-{index}", runner, roles={"review"})
            for index in range(count)
        ]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("save_before_error", [False, True])
async def test_failed_task_result_storage_never_releases_success(save_before_error):
    class FailingStore(BoundedRunStateStore):
        async def record_task_result(self, tenant_id, run_id, result):
            if result.task_id == "first":
                if save_before_error:
                    await super().record_task_result(tenant_id, run_id, result)
                raise OSError("private storage diagnostic")
            await super().record_task_result(tenant_id, run_id, result)

    runner = SuccessfulRunner()
    store = FailingStore()
    orchestrator = MultiAgentOrchestrator(workers(runner), state_store=store)
    with pytest.raises(MultiAgentError, match="task result persistence") as error:
        await orchestrator.run(
            MultiAgentPlan(
                (
                    MultiAgentTask("first", "read"),
                    MultiAgentTask("next", "use result", dependencies={"first"}),
                )
            ),
            tenant_id="review",
            run_id="storage-fault",
        )
    assert runner.calls == ["first"]
    assert error.value.task_result.status == "succeeded"
    assert error.value.task_result.output == "synthetic success"
    assert "private storage diagnostic" not in str(error.value)
    snapshot = await store.snapshot("review", "storage-fault")
    assert snapshot.status == "failed"
    assert snapshot.task_results["next"].status == "skipped"


@pytest.mark.asyncio
async def test_parallel_tool_outputs_are_all_inspected_and_reach_the_model():
    inspected = []

    class Policy:
        async def inspect(self, inspection, cancellation):
            if inspection.stage == "tool_output":
                await asyncio.sleep(0.01)
                inspected.append(inspection.tool_name)
            return SafetyDecision.allow()

    async def execute(call_id, arguments, cancellation, on_update):
        return AgentToolResult(content=[{"type": "text", "text": call_id}])

    model = Model(id="review", provider="fake", api="fake")
    provider = ScriptedProvider(
        [
            assistant_message(
                model=model,
                stop_reason="toolUse",
                content=[
                    {"type": "toolCall", "id": name, "name": name, "arguments": {}}
                    for name in ("read_a", "read_b")
                ],
            ),
            assistant_message(model=model, content=[{"type": "text", "text": "done"}]),
        ]
    )
    agent = Agent(
        model=model,
        stream_fn=provider.stream,
        tools=[
            AgentTool(
                name=name,
                label=name,
                description="synthetic read",
                execute=execute,
                replay_policy="safe",
            )
            for name in ("read_a", "read_b")
        ],
        content_safety=ContentSafetyPipeline([Policy()]),
    )
    await agent.prompt("run both")
    results = [m for m in agent.state.messages if m.get("role") == "toolResult"]
    assert {m["content"][0]["text"] for m in results} == {"read_a", "read_b"}
    assert all(not m["isError"] for m in results)
    assert set(inspected) == {"read_a", "read_b"}
    assert provider.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_task", [False, True])
async def test_cancelling_queued_safety_check_preserves_active_check(cancel_task):
    existing_tasks = asyncio.all_tasks()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    class Policy:
        async def inspect(self, inspection, cancellation):
            calls.append(inspection.value)
            entered.set()
            await release.wait()
            return SafetyDecision.allow()

    pipeline = ContentSafetyPipeline([Policy()])
    first = asyncio.create_task(
        pipeline.inspect("tool_output", "first", CancellationToken())
    )
    await entered.wait()
    token = CancellationToken()
    queued = asyncio.create_task(pipeline.inspect("tool_output", "queued", token))
    try:
        await asyncio.sleep(0.01)
        if cancel_task:
            queued.cancel()
        else:
            token.cancel()
        with pytest.raises(
            asyncio.CancelledError if cancel_task else OperationCancelledError
        ):
            await asyncio.wait_for(queued, 0.5)
        assert calls == ["first"]
    finally:
        release.set()
        await asyncio.gather(first, queued, return_exceptions=True)
    assert await first == "first"
    assert (
        await pipeline.inspect("tool_output", "later", CancellationToken()) == "later"
    )
    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - existing_tasks)


async def invoke(tool, arguments):
    return await tool.execute(
        "review", tool.validate_args(arguments), CancellationToken(), None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("literal", [False, True])
async def test_grep_searches_complete_long_lines_and_returns_the_matching_fragment(
    tmp_path, literal
):
    (tmp_path / "long.txt").write_text("x" * 5000 + "REVIEW_NEEDLE", encoding="utf-8")
    tool = create_builtin_tools(ToolServices.create(tmp_path), ["grep"])[0]
    result = await invoke(tool, {"pattern": "REVIEW_NEEDLE", "literal": literal})
    assert len(result.details["matches"]) == 1
    assert "REVIEW_NEEDLE" in result.details["matches"][0]["text"]
    assert result.details["matches"][0]["column"] == 5001
    assert result.details["matches"][0]["textTruncated"] is True
    assert result.details["truncated"] is False
    no_match = await invoke(tool, {"pattern": "x$", "literal": False})
    assert no_match.details["matches"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["write", "edit"])
async def test_independent_file_services_share_version_check_and_publish_lock(
    tmp_path, operation
):
    path = tmp_path / "state.txt"
    path.write_text("original", encoding="utf-8")
    entered, release = threading.Event(), threading.Event()

    class BlockingWriter(AtomicFileWriter):
        def write(self, target, data, *, create_only):
            entered.set()
            assert release.wait(timeout=2)
            super().write(target, data, create_only=create_only)

    first_services = ToolServices.create(
        tmp_path, security_profile="workspace-write", atomic_writer=BlockingWriter()
    )
    second_services = ToolServices.create(tmp_path, security_profile="workspace-write")
    bundles = [
        {t.name: t for t in create_builtin_tools(services)}
        for services in (first_services, second_services)
    ]
    versions = [
        (await invoke(bundle["read"], {"path": "state.txt"})).details["observation"]
        for bundle in bundles
    ]

    async def change(index, value):
        args = {"path": "state.txt", "expectedVersion": versions[index]}
        if operation == "write":
            args["content"] = value
        else:
            args["edits"] = [{"oldText": "original", "newText": value}]
        return await invoke(bundles[index][operation], args)

    first = asyncio.create_task(change(0, "first"))
    second = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        second = asyncio.create_task(change(1, "second"))
        await asyncio.sleep(0.05)
    finally:
        release.set()
        outcomes = await asyncio.gather(
            first, *([second] if second is not None else []), return_exceptions=True
        )
    errors = [item for item in outcomes if isinstance(item, WorkspaceToolError)]
    assert [error.code for error in errors] == ["stale_observation"]
    assert path.read_text(encoding="utf-8") == "first"


@pytest.mark.asyncio
async def test_shared_file_lock_cleans_cancelled_waiter_and_allows_other_paths(
    tmp_path,
):
    queues = [FileMutationQueue(WorkspacePathPolicy(tmp_path)) for _ in range(3)]
    target = tmp_path / "one.txt"

    async def acquire(queue, path):
        async with queue.acquire(path):
            return path

    async with queues[0].acquire(target):
        waiting = asyncio.create_task(acquire(queues[1], target))
        try:
            await asyncio.sleep(0.01)
            assert not waiting.done()
            assert await asyncio.wait_for(acquire(queues[2], tmp_path / "two.txt"), 0.5)
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
    assert await asyncio.wait_for(acquire(queues[2], target), 0.5) == target


@pytest.mark.asyncio
async def test_file_lock_coordinates_distinct_event_loops(tmp_path):
    outer = FileMutationQueue(WorkspacePathPolicy(tmp_path))
    target = tmp_path / "shared.txt"
    attempted, entered = threading.Event(), threading.Event()

    async def in_other_loop():
        queue = FileMutationQueue(WorkspacePathPolicy(tmp_path))
        attempted.set()
        async with queue.acquire(target):
            entered.set()

    worker = None
    try:
        async with outer.acquire(target):
            worker = asyncio.create_task(
                asyncio.to_thread(lambda: asyncio.run(in_other_loop()))
            )
            assert await asyncio.to_thread(attempted.wait, 1)
            await asyncio.sleep(0.03)
            assert not entered.is_set()
    finally:
        if worker is not None:
            await asyncio.wait_for(worker, 1)
    assert entered.is_set()


@pytest.mark.asyncio
async def test_sync_arbitration_cannot_block_run_deadline():
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    class BlockingArbitrator(ExactMatchArbitrator):
        def arbitrate(self, task, results):
            entered.set()
            try:
                release.wait(timeout=2)
                return super().arbitrate(task, results)
            finally:
                finished.set()

    orchestrator = MultiAgentOrchestrator(
        workers(SuccessfulRunner(), 2),
        arbitrators={"blocking": BlockingArbitrator()},
        limits=OrchestrationLimits(total_deadline_seconds=0.05),
    )
    try:
        result = await asyncio.wait_for(
            orchestrator.run(
                MultiAgentPlan(
                    (
                        MultiAgentTask(
                            "task",
                            "read",
                            replicas=2,
                            arbitrator_id="blocking",
                            allow_replicated_execution=True,
                        ),
                    )
                ),
                tenant_id="review",
                run_id="arbitrator-deadline",
            ),
            timeout=0.5,
        )
        assert entered.is_set()
        assert not finished.is_set()
        assert result.status == "deadline_exceeded"
        assert result.task_results["task"].status == "cancelled"
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
