"""Executable bounded multi-agent orchestration tests."""

from __future__ import annotations

import asyncio
import threading
import unittest
from collections.abc import Awaitable, Callable

from pi_agent_loop import (
    Agent,
    AgentPromptWorkerRunner,
    BoundedRunStateStore,
    ExactMatchArbitrator,
    Model,
    MultiAgentOrchestrator,
    MultiAgentPlan,
    MultiAgentTask,
    OrchestrationBudgetExceeded,
    OrchestrationLimits,
    OrchestrationValidationError,
    ScriptedProvider,
    WorkerExecutionError,
    WorkerOutput,
    WorkerRegistration,
    WorkerRegistry,
    WorkerRequest,
    assistant_message,
)
from pi_agent_loop.cancellation import CancellationToken


class _Runner:
    def __init__(
        self,
        callback: Callable[[WorkerRequest], WorkerOutput | Awaitable[WorkerOutput]],
    ) -> None:
        self.callback = callback
        self.calls: list[str] = []

    async def run(self, request: WorkerRequest) -> WorkerOutput:
        self.calls.append(request.task_id)
        value = self.callback(request)
        return await value if isinstance(value, Awaitable) else value


def _registry(
    runner: _Runner | AgentPromptWorkerRunner,
    *,
    worker_id: str = "worker-a",
    max_concurrency: int = 4,
    capabilities: frozenset[str] = frozenset({"analysis"}),
    priority: int = 0,
) -> WorkerRegistry:
    return WorkerRegistry(
        [
            WorkerRegistration(
                worker_id,
                runner,
                roles=frozenset({"analyst"}),
                capabilities=capabilities,
                max_concurrency=max_concurrency,
                priority=priority,
            )
        ]
    )


class MultiAgentDagTests(unittest.IsolatedAsyncioTestCase):
    async def test_dag_roots并行且dependent等待全部完成(self) -> None:
        active = 0
        max_active = 0
        finished: set[str] = set()

        async def execute(request: WorkerRequest) -> WorkerOutput:
            nonlocal active, max_active
            if request.task_id == "join":
                self.assertEqual(finished, {"left", "right"})
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1
            finished.add(request.task_id)
            return WorkerOutput(text=request.task_id)

        runner = _Runner(execute)
        orchestrator = MultiAgentOrchestrator(
            _registry(runner, max_concurrency=8),
            limits=OrchestrationLimits(max_concurrency=2),
        )
        result = await orchestrator.run(
            MultiAgentPlan(
                (
                    MultiAgentTask("left", "left"),
                    MultiAgentTask("right", "right"),
                    MultiAgentTask(
                        "join",
                        "join",
                        dependencies=frozenset({"left", "right"}),
                    ),
                )
            ),
            tenant_id="tenant-a",
            run_id="dag-run",
        )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(max_active, 2)
        self.assertEqual(result.task_results["join"].output, "join")

    async def test_trusted_capability_registry决定worker而非task自报runner(self) -> None:
        wrong = _Runner(lambda _request: WorkerOutput(text="wrong"))
        right = _Runner(lambda _request: WorkerOutput(text="right"))
        registry = WorkerRegistry(
            [
                WorkerRegistration(
                    "high-priority-wrong-capability",
                    wrong,
                    roles=frozenset({"analyst"}),
                    capabilities=frozenset({"vision"}),
                    priority=10,
                ),
                WorkerRegistration(
                    "eligible",
                    right,
                    roles=frozenset({"analyst"}),
                    capabilities=frozenset({"analysis"}),
                ),
            ]
        )
        result = await MultiAgentOrchestrator(registry).run(
            MultiAgentPlan(
                (
                    MultiAgentTask(
                        "task",
                        "work",
                        required_roles=frozenset({"analyst"}),
                        required_capabilities=frozenset({"analysis"}),
                    ),
                )
            ),
            tenant_id="tenant-a",
            run_id="capability-run",
        )

        self.assertEqual(result.task_results["task"].selected_worker_id, "eligible")
        self.assertEqual(wrong.calls, [])
        self.assertEqual(right.calls, ["task"])

    async def test_failure与cancel向依赖传播且不调用dependent_worker(self) -> None:
        async def execute(request: WorkerRequest) -> WorkerOutput:
            if request.task_id == "root":
                raise WorkerExecutionError("upstream_failed")
            return WorkerOutput(text="must-not-run")

        runner = _Runner(execute)
        result = await MultiAgentOrchestrator(_registry(runner)).run(
            MultiAgentPlan(
                (
                    MultiAgentTask("root", "fail"),
                    MultiAgentTask(
                        "dependent",
                        "after",
                        dependencies=frozenset({"root"}),
                    ),
                )
            ),
            tenant_id="tenant-a",
            run_id="failure-run",
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.task_results["root"].error_code, "worker_error")
        self.assertEqual(result.task_results["dependent"].status, "skipped")
        self.assertEqual(result.task_results["dependent"].error_code, "dependency_failed")
        self.assertEqual(runner.calls, ["root"])

        cancelled_runner = _Runner(
            lambda request: WorkerOutput(
                "cancelled" if request.task_id == "root" else "succeeded",
                text="must-not-run" if request.task_id != "root" else "",
                error_code="worker_cancelled" if request.task_id == "root" else None,
            )
        )
        cancelled = await MultiAgentOrchestrator(_registry(cancelled_runner)).run(
            MultiAgentPlan(
                (
                    MultiAgentTask("root", "cancel"),
                    MultiAgentTask(
                        "dependent",
                        "after",
                        dependencies=frozenset({"root"}),
                    ),
                )
            ),
            tenant_id="tenant-a",
            run_id="dependency-cancel-run",
        )
        self.assertEqual(cancelled.task_results["dependent"].status, "cancelled")
        self.assertEqual(
            cancelled.task_results["dependent"].error_code,
            "dependency_cancelled",
        )
        self.assertEqual(cancelled_runner.calls, ["root"])


class MultiAgentIsolationAndBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_run_id按tenant隔离message与result(self) -> None:
        async def execute(request: WorkerRequest) -> WorkerOutput:
            await request.send_message(request.task_id, f"message:{request.tenant_id}")
            return WorkerOutput(text=f"result:{request.tenant_id}")

        runner = _Runner(execute)
        limits = OrchestrationLimits(max_concurrency=2)
        store = BoundedRunStateStore(limits=limits)
        orchestrator = MultiAgentOrchestrator(
            _registry(runner, max_concurrency=2),
            limits=limits,
            state_store=store,
        )
        plan = MultiAgentPlan((MultiAgentTask("task", "work"),))

        first, second = await asyncio.gather(
            orchestrator.run(plan, tenant_id="tenant-a", run_id="same-run"),
            orchestrator.run(plan, tenant_id="tenant-b", run_id="same-run"),
        )
        snapshot_a = await store.snapshot("tenant-a", "same-run")
        snapshot_b = await store.snapshot("tenant-b", "same-run")

        self.assertEqual(first.task_results["task"].output, "result:tenant-a")
        self.assertEqual(second.task_results["task"].output, "result:tenant-b")
        self.assertEqual(snapshot_a.messages[0].content, "message:tenant-a")
        self.assertEqual(snapshot_b.messages[0].content, "message:tenant-b")

    async def test_resource_key冲突串行且不同task仍受全局预算(self) -> None:
        active = 0
        max_active = 0

        async def execute(_request: WorkerRequest) -> WorkerOutput:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1
            return WorkerOutput(text="ok")

        runner = _Runner(execute)
        result = await MultiAgentOrchestrator(
            _registry(runner, max_concurrency=8),
            limits=OrchestrationLimits(max_concurrency=8),
        ).run(
            MultiAgentPlan(
                (
                    MultiAgentTask(
                        "one",
                        "one",
                        resource_keys=frozenset({"document:1"}),
                    ),
                    MultiAgentTask(
                        "two",
                        "two",
                        resource_keys=frozenset({"document:1"}),
                    ),
                )
            ),
            tenant_id="tenant-a",
            run_id="lock-run",
        )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(max_active, 1)

    async def test_single_worker_concurrency独立限制(self) -> None:
        active = 0
        max_active = 0

        async def execute(_request: WorkerRequest) -> WorkerOutput:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1
            return WorkerOutput(text="ok")

        runner = _Runner(execute)
        result = await MultiAgentOrchestrator(
            _registry(runner, max_concurrency=1),
            limits=OrchestrationLimits(max_concurrency=8),
        ).run(
            MultiAgentPlan(
                (
                    MultiAgentTask("one", "one"),
                    MultiAgentTask("two", "two"),
                )
            ),
            tenant_id="tenant-a",
            run_id="worker-limit-run",
        )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(max_active, 1)

    async def test_task和result硬预算fail_closed(self) -> None:
        runner = _Runner(lambda _request: WorkerOutput(text="too-large"))
        tight = OrchestrationLimits(
            max_tasks=1,
            max_worker_invocations=1,
            max_result_bytes=4,
            max_total_result_bytes=4,
        )
        orchestrator = MultiAgentOrchestrator(_registry(runner), limits=tight)
        with self.assertRaises(OrchestrationBudgetExceeded):
            await orchestrator.run(
                MultiAgentPlan(
                    (
                        MultiAgentTask("one", "one"),
                        MultiAgentTask("two", "two"),
                    )
                ),
                tenant_id="tenant-a",
                run_id="too-many",
            )
        self.assertEqual(runner.calls, [])

        result = await orchestrator.run(
            MultiAgentPlan((MultiAgentTask("one", "one"),)),
            tenant_id="tenant-a",
            run_id="large-result",
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.task_results["one"].error_code, "result_too_large")

        async def publish_too_much(request: WorkerRequest) -> WorkerOutput:
            await request.send_message(request.task_id, "12345")
            return WorkerOutput(text="ok")

        message_limits = OrchestrationLimits(
            max_message_bytes=4,
            max_total_message_bytes=4,
        )
        message_result = await MultiAgentOrchestrator(
            _registry(_Runner(publish_too_much)),
            limits=message_limits,
        ).run(
            MultiAgentPlan((MultiAgentTask("one", "one"),)),
            tenant_id="tenant-a",
            run_id="large-message",
        )
        self.assertEqual(message_result.status, "failed")
        self.assertEqual(
            message_result.task_results["one"].error_code,
            "message_too_large",
        )


class MultiAgentCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_total_deadline_does_not_wait_for_blocking_sync_telemetry(self) -> None:
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def blocking_sink(_event: dict) -> None:
            started.set()
            release.wait(timeout=2)
            finished.set()

        orchestrator = MultiAgentOrchestrator(
            _registry(_Runner(lambda _request: WorkerOutput(text="ok"))),
            limits=OrchestrationLimits(total_deadline_seconds=0.03),
            telemetry_sink=blocking_sink,
            telemetry_timeout_seconds=0.2,
        )
        loop = asyncio.get_running_loop()
        before = loop.time()
        try:
            result = await asyncio.wait_for(
                orchestrator.run(
                    MultiAgentPlan((MultiAgentTask("task", "work"),)),
                    tenant_id="tenant-a",
                    run_id="blocking-telemetry-run",
                ),
                timeout=0.3,
            )
            elapsed = loop.time() - before
            self.assertTrue(started.is_set())
            self.assertFalse(finished.is_set())
            self.assertLess(elapsed, 0.2)
            self.assertEqual(result.status, "deadline_exceeded")
        finally:
            release.set()
        self.assertTrue(await asyncio.to_thread(finished.wait, 1))
        await asyncio.sleep(0)

    async def test_total_deadline取消真实runner且不遗留运行(self) -> None:
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def execute(_request: WorkerRequest) -> WorkerOutput:
            started.set()
            try:
                await asyncio.sleep(30)
            finally:
                cleaned.set()
            return WorkerOutput(text="late")

        result = await MultiAgentOrchestrator(
            _registry(_Runner(execute)),
            limits=OrchestrationLimits(total_deadline_seconds=0.03),
        ).run(
            MultiAgentPlan((MultiAgentTask("slow", "wait"),)),
            tenant_id="tenant-a",
            run_id="deadline-run",
        )

        self.assertTrue(started.is_set())
        self.assertTrue(cleaned.is_set())
        self.assertEqual(result.status, "deadline_exceeded")
        self.assertEqual(result.task_results["slow"].status, "cancelled")

    async def test_total_deadline_does_not_wait_for_blocking_sync_runner(self) -> None:
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        class BlockingSyncRunner:
            def run(self, _request: WorkerRequest) -> WorkerOutput:
                started.set()
                release.wait(timeout=2)
                finished.set()
                return WorkerOutput(text="late")

        registry = WorkerRegistry(
            [
                WorkerRegistration(
                    "sync-worker",
                    BlockingSyncRunner(),
                    roles=frozenset({"analyst"}),
                    capabilities=frozenset({"analysis"}),
                )
            ]
        )
        orchestrator = MultiAgentOrchestrator(
            registry,
            limits=OrchestrationLimits(total_deadline_seconds=0.03),
        )
        loop = asyncio.get_running_loop()
        before = loop.time()
        try:
            result = await asyncio.wait_for(
                orchestrator.run(
                    MultiAgentPlan((MultiAgentTask("sync", "wait"),)),
                    tenant_id="tenant-a",
                    run_id="sync-deadline-run",
                ),
                timeout=0.3,
            )
            elapsed = loop.time() - before
            self.assertTrue(started.is_set())
            self.assertFalse(finished.is_set())
            self.assertLess(elapsed, 0.2)
            self.assertEqual(result.status, "deadline_exceeded")
            self.assertEqual(result.task_results["sync"].status, "cancelled")
        finally:
            release.set()
        self.assertTrue(await asyncio.to_thread(finished.wait, 1))

    async def test_external_cancellation_token传播到worker(self) -> None:
        started = asyncio.Event()

        async def execute(request: WorkerRequest) -> WorkerOutput:
            started.set()
            await request.cancellation.wait()
            request.cancellation.throw_if_cancelled()
            return WorkerOutput(text="late")

        token = CancellationToken()
        future = asyncio.create_task(
            MultiAgentOrchestrator(_registry(_Runner(execute))).run(
                MultiAgentPlan((MultiAgentTask("slow", "wait"),)),
                tenant_id="tenant-a",
                run_id="cancel-run",
                cancellation=token,
            )
        )
        await started.wait()
        token.cancel("caller cancelled")
        result = await future

        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.task_results["slow"].status, "cancelled")
        self.assertEqual(token.child_count, 0)

    async def test_asyncio_task_cancellation清理worker并向调用方传播(self) -> None:
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def execute(_request: WorkerRequest) -> WorkerOutput:
            started.set()
            try:
                await asyncio.sleep(30)
            finally:
                cleaned.set()
            return WorkerOutput(text="late")

        store = BoundedRunStateStore()
        future = asyncio.create_task(
            MultiAgentOrchestrator(
                _registry(_Runner(execute)),
                state_store=store,
            ).run(
                MultiAgentPlan((MultiAgentTask("slow", "wait"),)),
                tenant_id="tenant-a",
                run_id="task-cancel-run",
            )
        )
        await started.wait()
        future.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await future

        self.assertTrue(cleaned.is_set())
        snapshot = await store.snapshot("tenant-a", "task-cancel-run")
        self.assertEqual(snapshot.status, "cancelled")


class MultiAgentArbitrationAndAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_replicas不允许隐式多数仲裁(self) -> None:
        with self.assertRaises(OrchestrationValidationError):
            MultiAgentTask(
                "review",
                "review",
                replicas=2,
                allow_replicated_execution=True,
            )

    async def test_exact_match_arbitrator一致成功分歧fail_closed(self) -> None:
        first = _Runner(lambda _request: WorkerOutput(text="same"))
        second_text = ["same"]
        second = _Runner(lambda _request: WorkerOutput(text=second_text[0]))
        registry = WorkerRegistry(
            [
                WorkerRegistration(
                    "worker-a",
                    first,
                    roles=frozenset({"reviewer"}),
                ),
                WorkerRegistration(
                    "worker-b",
                    second,
                    roles=frozenset({"reviewer"}),
                ),
            ]
        )
        orchestrator = MultiAgentOrchestrator(
            registry,
            arbitrators={"exact": ExactMatchArbitrator()},
        )
        task = MultiAgentTask(
            "review",
            "review",
            required_roles=frozenset({"reviewer"}),
            replicas=2,
            arbitrator_id="exact",
            allow_replicated_execution=True,
        )
        matched = await orchestrator.run(
            MultiAgentPlan((task,)),
            tenant_id="tenant-a",
            run_id="match-run",
        )
        second_text[0] = "different"
        disagreed = await orchestrator.run(
            MultiAgentPlan((task,)),
            tenant_id="tenant-a",
            run_id="disagree-run",
        )

        self.assertEqual(matched.status, "succeeded")
        self.assertEqual(matched.task_results["review"].output, "same")
        self.assertEqual(disagreed.status, "failed")
        self.assertEqual(
            disagreed.task_results["review"].error_code,
            "replica_disagreement",
        )

    async def test_real_agent_prompt_adapter提取终态且telemetry不含prompt(self) -> None:
        model = Model(id="worker-model", provider="scripted", api="scripted")
        secret_prompt = "analyze SECRET-DO-NOT-LOG"
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=model,
                    content=[{"type": "text", "text": "agent-result"}],
                )
            ]
        )
        agent = Agent(model=model, stream_fn=provider.stream, tenant_id="tenant-a")
        events: list[dict] = []
        runner = AgentPromptWorkerRunner(agent)
        result = await MultiAgentOrchestrator(
            _registry(runner, max_concurrency=1),
            telemetry_sink=events.append,
        ).run(
            MultiAgentPlan((MultiAgentTask("agent-task", secret_prompt),)),
            tenant_id="tenant-a",
            run_id="agent-run",
        )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.task_results["agent-task"].output, "agent-result")
        self.assertEqual(provider.call_count, 1)
        self.assertNotIn(secret_prompt, repr(events))
        self.assertNotIn("agent-result", repr(events))
        self.assertNotIn("tenant-a", repr(events))
        self.assertNotIn("agent-task", repr(events))

    async def test_real_agent_prompt_adapter_rejects_wrong_tenant_binding(self) -> None:
        model = Model(id="worker-model", provider="scripted", api="scripted")
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=model,
                    content=[{"type": "text", "text": "must-not-run"}],
                )
            ]
        )
        agent = Agent(model=model, stream_fn=provider.stream, tenant_id="tenant-a")
        result = await MultiAgentOrchestrator(
            _registry(AgentPromptWorkerRunner(agent), max_concurrency=1)
        ).run(
            MultiAgentPlan((MultiAgentTask("agent-task", "work"),)),
            tenant_id="tenant-b",
            run_id="tenant-mismatch-run",
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(
            result.task_results["agent-task"].error_code,
            "worker_tenant_mismatch",
        )
        self.assertEqual(provider.call_count, 0)

    async def test_real_agent_factory_binds_tenant_from_request(self) -> None:
        model = Model(id="worker-model", provider="scripted", api="scripted")
        factory_tenants: list[str] = []

        def factory(request: WorkerRequest) -> Agent:
            factory_tenants.append(request.tenant_id)
            provider = ScriptedProvider(
                [
                    assistant_message(
                        model=model,
                        content=[{"type": "text", "text": request.tenant_id}],
                    )
                ]
            )
            return Agent(
                model=model,
                stream_fn=provider.stream,
                tenant_id=request.tenant_id,
            )

        orchestrator = MultiAgentOrchestrator(
            _registry(AgentPromptWorkerRunner(agent_factory=factory), max_concurrency=1)
        )
        first = await orchestrator.run(
            MultiAgentPlan((MultiAgentTask("agent-task", "work"),)),
            tenant_id="tenant-a",
            run_id="tenant-factory-a",
        )
        second = await orchestrator.run(
            MultiAgentPlan((MultiAgentTask("agent-task", "work"),)),
            tenant_id="tenant-b",
            run_id="tenant-factory-b",
        )

        self.assertEqual(first.task_results["agent-task"].output, "tenant-a")
        self.assertEqual(second.task_results["agent-task"].output, "tenant-b")
        self.assertEqual(factory_tenants, ["tenant-a", "tenant-b"])


if __name__ == "__main__":
    unittest.main()
