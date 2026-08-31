"""统一 ToolDispatchRuntime 与生产调度能力。"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from pi_agent_loop.cancellation import CancellationToken
from pi_agent_loop.harness.tool_runtime_adapter import RecoverableToolRuntime
from pi_agent_loop.retry.errors import RetryableToolError
from pi_agent_loop.retry.types import ToolRetryPolicy
from pi_agent_loop.runtime.telemetry import InMemoryTelemetryExporter, Telemetry
from pi_agent_loop.session.resume import RecoveryAction
from pi_agent_loop.tool_runtime import (
    ResourceLeaseLostError,
    ResourceLockBackendCapabilities,
    SQLiteResourceLockBackend,
    ToolDispatchRuntime,
)
from pi_agent_loop.tool_contract import ToolSecurityContract
from pi_agent_loop.types import AgentContext, AgentTool, AgentToolResult, Model


def tool_call(call_id: str, name: str, arguments: dict | None = None) -> dict:
    return {
        "type": "toolCall",
        "id": call_id,
        "name": name,
        "arguments": arguments or {},
    }


def metric_rows(telemetry: Telemetry, kind: str, name: str) -> list[dict]:
    return [
        row
        for row in telemetry.metrics.snapshot()[kind]
        if row["name"] == name
    ]


class ToolDispatchRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_telemetry记录工具结果耗时span和日志(self) -> None:
        spans = InMemoryTelemetryExporter()
        logs = InMemoryTelemetryExporter()
        telemetry = Telemetry(span_sink=spans, log_sink=logs)

        async def execute(_id, _args, _token, _update):
            return AgentToolResult(content=[{"type": "text", "text": "ok"}])

        tool = AgentTool(
            name="observed",
            label="observed",
            description="observed",
            execute=execute,
        )
        runtime = ToolDispatchRuntime([tool], telemetry=telemetry)

        success = await runtime.dispatch(tool_call("1", "observed", {"secret": "not-logged"}))
        missing = await runtime.dispatch(tool_call("2", "attacker-controlled-name"))

        self.assertFalse(success.is_error)
        self.assertTrue(missing.is_error)
        calls = metric_rows(telemetry, "counters", "tool_calls_total")
        self.assertEqual(
            {(row["labels"]["tool"], row["labels"]["outcome"]) for row in calls},
            {("observed", "success"), ("__unknown__", "error")},
        )
        latency = metric_rows(telemetry, "histograms", "tool_latency_ms")
        self.assertEqual(sum(row["count"] for row in latency), 2)
        self.assertEqual([record["status"] for record in spans.records], ["ok", "error"])
        self.assertTrue(
            any(record["event"] == "tool_call_finished" for record in logs.records)
        )
        self.assertNotIn("not-logged", repr(spans.records) + repr(logs.records))

    async def test_telemetry记录retry_attempt与调度深度(self) -> None:
        spans = InMemoryTelemetryExporter()
        telemetry = Telemetry(span_sink=spans)
        attempts = 0

        async def execute(_id, _args, _token, _update):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RetryableToolError("temporary", code="upstream_unavailable")
            return AgentToolResult(content=[])

        tool = AgentTool(
            name="retrying",
            label="retrying",
            description="retrying",
            execute=execute,
            retry_policy=ToolRetryPolicy(
                max_retries=1,
                retryable_codes=frozenset({"upstream_unavailable"}),
                idempotent=True,
                initial_delay_seconds=0,
                max_delay_seconds=0.01,
                jitter_ratio=0,
                max_elapsed_seconds=1,
            ),
        )
        runtime = ToolDispatchRuntime([tool], telemetry=telemetry)

        outcome = await runtime.dispatch(tool_call("1", "retrying"))

        self.assertFalse(outcome.is_error)
        self.assertEqual(attempts, 2)
        retries = metric_rows(telemetry, "counters", "tool_retries_total")
        self.assertEqual(retries[0]["value"], 1)
        attempts_rows = metric_rows(telemetry, "counters", "tool_attempts_total")
        self.assertEqual(sum(row["value"] for row in attempts_rows), 2)
        self.assertEqual(runtime.scheduler.stats.queued, 2)
        self.assertEqual(runtime.scheduler.stats.completed, 1)
        self.assertEqual(runtime.scheduler.stats.active, 0)
        event_names = [event["name"] for event in spans.records[0]["events"]]
        self.assertIn("tool_retry_scheduled", event_names)
        self.assertIn("tool_retry_finished", event_names)
        scheduler_depth = metric_rows(
            telemetry,
            "gauges",
            "tool_scheduler_queue_depth",
        )
        self.assertTrue(
            any(
                row["labels"] == {"scope": "scheduler"} and row["value"] == 0
                for row in scheduler_depth
            )
        )

    async def test_telemetry记录资源锁等待超时和告警(self) -> None:
        alerts = InMemoryTelemetryExporter()
        telemetry = Telemetry(alert_hooks=[alerts])
        started = asyncio.Event()
        release = asyncio.Event()

        async def hold(_id, _args, _token, _update):
            started.set()
            await release.wait()
            return AgentToolResult(content=[])

        def make(name: str, timeout: float | None = None) -> AgentTool:
            return AgentTool(
                name=name,
                label=name,
                description=name,
                execute=hold,
                execution_mode="resource_locked",
                resolve_resource_keys=lambda _args: "order:telemetry-test",
                lock_timeout_seconds=timeout,
            )

        first = make("first")
        second = make("second", 0.02)
        runtime = ToolDispatchRuntime([first, second], telemetry=telemetry)
        first_task = asyncio.create_task(runtime.dispatch(tool_call("1", "first")))
        await asyncio.wait_for(started.wait(), 1)

        outcome = await runtime.dispatch(tool_call("2", "second"))
        release.set()
        await first_task

        self.assertEqual(outcome.result.details["code"], "resource_lock_timeout")
        waits = metric_rows(telemetry, "histograms", "resource_lock_wait_ms")
        self.assertEqual(waits[0]["labels"]["resource"], "order:telemetry-test")
        self.assertEqual(waits[0]["count"], 2)
        timeouts = metric_rows(
            telemetry,
            "counters",
            "tool_scheduler_timeouts_total",
        )
        self.assertEqual(timeouts[0]["labels"]["scope"], "resource_lock")
        self.assertEqual(timeouts[0]["value"], 1)
        self.assertEqual(runtime.scheduler.stats.lock_timeouts, 1)
        self.assertEqual(runtime.scheduler.stats.waiting, 0)
        self.assertEqual(runtime.scheduler.stats.active, 0)
        self.assertEqual(
            {record["name"] for record in alerts.records},
            {"tool_scheduler_timeout", "tool_resource_lock_timeout"},
        )

    async def test_telemetry记录租户节流和update背压(self) -> None:
        telemetry = Telemetry()
        first_started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def execute(_id, _args, _token, on_update):
            nonlocal calls
            calls += 1
            if calls == 1:
                first_started.set()
                await release.wait()
            for index in range(4):
                on_update(AgentToolResult(content=[], details={"index": index}))
            return AgentToolResult(content=[])

        async def slow_updates(event):
            if event["type"] == "tool_execution_update":
                await asyncio.sleep(0.01)

        tool = AgentTool(
            name="tenant_tool",
            label="tenant_tool",
            description="tenant_tool",
            execute=execute,
            resolve_tenant_id=lambda args: args["tenant"],
        )
        runtime = ToolDispatchRuntime(
            [tool],
            telemetry=telemetry,
            tenant_limits={"acme": 1},
            max_parallel_tools=2,
            max_update_tasks=1,
        )
        task = asyncio.create_task(
            runtime.dispatch_many(
                [
                    tool_call("1", "tenant_tool", {"tenant": "acme"}),
                    tool_call("2", "tenant_tool", {"tenant": "acme"}),
                ],
                context=AgentContext("", [], [tool]),
                assistant_message={"role": "assistant", "content": []},
                cancellation=CancellationToken(),
                emit=slow_updates,
                tenant_id="acme",
            )
        )
        await asyncio.wait_for(first_started.wait(), 1)
        await asyncio.sleep(0)
        release.set()
        await task

        throttles = metric_rows(
            telemetry,
            "counters",
            "tool_scheduler_tenant_throttled_total",
        )
        self.assertEqual(throttles[0]["labels"]["tenant"], "acme")
        self.assertEqual(throttles[0]["value"], 1)
        backpressure = metric_rows(
            telemetry,
            "counters",
            "tool_update_backpressure_total",
        )
        self.assertGreaterEqual(sum(row["value"] for row in backpressure), 1)
        active = metric_rows(telemetry, "gauges", "tool_scheduler_active")
        self.assertTrue(all(row["value"] == 0 for row in active))
        self.assertEqual(runtime.scheduler.stats.tenant_throttles, 1)
        self.assertEqual(runtime.scheduler.stats.active, 0)

    async def test_recovery直接复用runtime且不创建嵌套agent(self) -> None:
        calls: list[str] = []

        async def execute(call_id, _args, _token, _update):
            calls.append(call_id)
            return AgentToolResult(content=[{"type": "text", "text": "ok"}])

        tool = AgentTool(
            name="read",
            label="读取",
            description="读取",
            execute=execute,
            replay_policy="safe",
        )
        shared = ToolDispatchRuntime([tool])
        recovery = RecoverableToolRuntime(
            model=Model(id="m", provider="test"),
            tools=[tool],
            runtime=shared,
        )
        result = await recovery.execute(
            RecoveryAction(
                kind="replay_safe_tool",
                tool_call_id="call-1",
                tool_name="read",
                arguments={},
                expected_replay_policy="safe",
                expected_tool_contract_digest=(
                    ToolSecurityContract.capture(tool).digest
                ),
            )
        )
        self.assertEqual(calls, ["call-1"])
        self.assertFalse(result["isError"])
        self.assertEqual(shared.scheduler.stats.completed, 1)

    async def test_recovery把fencing_token传入可信tool_context(self) -> None:
        observed: list[tuple[int | None, str | None]] = []

        async def execute_with_context(
            _call_id,
            _args,
            context,
            _token,
            _update,
        ):
            observed.append(
                (context.fencing_token, context.fencing_scope)
            )
            return AgentToolResult(content=[])

        tool = AgentTool(
            name="fenced_read",
            label="fenced_read",
            description="fenced_read",
            execute=None,
            execute_with_context=execute_with_context,
            replay_policy="safe",
        )
        recovery = RecoverableToolRuntime(
            model=Model(id="m", provider="test"),
            tools=[tool],
        )

        result = await recovery.execute(
            RecoveryAction(
                kind="replay_safe_tool",
                tool_call_id="call-fenced",
                tool_name=tool.name,
                arguments={},
                expected_replay_policy="safe",
                expected_tool_contract_digest=(
                    ToolSecurityContract.capture(tool).digest
                ),
                fencing_token=17,
                fencing_scope="operation-recovery-scope",
            )
        )

        self.assertFalse(result["isError"])
        self.assertEqual(observed, [(17, "operation-recovery-scope")])

    async def test_priority在同一屏障内先执行高优先级(self) -> None:
        order: list[str] = []

        def make(name: str, priority: int) -> AgentTool:
            async def execute(_id, _args, _token, _update):
                order.append(name)
                await asyncio.sleep(0)
                return AgentToolResult(content=[])

            return AgentTool(
                name=name,
                label=name,
                description=name,
                execute=execute,
                priority=priority,
            )

        low = make("low", 0)
        high = make("high", 10)
        runtime = ToolDispatchRuntime([low, high], max_parallel_tools=1)
        await runtime.dispatch_many(
            [tool_call("1", "low"), tool_call("2", "high")],
            context=AgentContext("", [], [low, high]),
            assistant_message={"role": "assistant", "content": []},
            cancellation=CancellationToken(),
            emit=lambda _event: None,
        )
        self.assertEqual(order, ["high", "low"])

    async def test_read锁共享_write锁独占且registry自动清理(self) -> None:
        active_readers = 0
        max_readers = 0
        writer_saw_readers: list[int] = []

        async def read(_id, _args, _token, _update):
            nonlocal active_readers, max_readers
            active_readers += 1
            max_readers = max(max_readers, active_readers)
            await asyncio.sleep(0.03)
            active_readers -= 1
            return AgentToolResult(content=[])

        async def write(_id, _args, _token, _update):
            writer_saw_readers.append(active_readers)
            return AgentToolResult(content=[])

        reader = AgentTool(
            name="reader",
            label="reader",
            description="reader",
            execute=read,
            execution_mode="resource_locked",
            resolve_resource_keys=lambda _args: "order:1",
            resource_access="read",
        )
        writer = AgentTool(
            name="writer",
            label="writer",
            description="writer",
            execute=write,
            execution_mode="resource_locked",
            resolve_resource_keys=lambda _args: "order:1",
        )
        runtime = ToolDispatchRuntime([reader, writer], max_parallel_tools=3)
        await runtime.dispatch_many(
            [
                tool_call("r1", "reader"),
                tool_call("r2", "reader"),
                tool_call("w", "writer"),
            ],
            context=AgentContext("", [], [reader, writer]),
            assistant_message={"role": "assistant", "content": []},
            cancellation=CancellationToken(),
            emit=lambda _event: None,
        )
        self.assertEqual(max_readers, 2)
        self.assertEqual(writer_saw_readers, [0])
        self.assertEqual(runtime.scheduler.resource_locks.size, 0)

    async def test资源锁等待超时返回结构化错误(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def hold(_id, _args, _token, _update):
            started.set()
            await release.wait()
            return AgentToolResult(content=[])

        first = AgentTool(
            name="first",
            label="first",
            description="first",
            execute=hold,
            execution_mode="resource_locked",
            resolve_resource_keys=lambda _args: "shared",
        )
        second = AgentTool(
            name="second",
            label="second",
            description="second",
            execute=hold,
            execution_mode="resource_locked",
            resolve_resource_keys=lambda _args: "shared",
            lock_timeout_seconds=0.03,
        )
        runtime = ToolDispatchRuntime([first, second])
        first_task = asyncio.create_task(runtime.dispatch(tool_call("1", "first")))
        await asyncio.wait_for(started.wait(), 1)
        outcome = await runtime.dispatch(tool_call("2", "second"))
        self.assertTrue(outcome.is_error)
        self.assertEqual(outcome.result.details["code"], "resource_lock_timeout")
        release.set()
        await first_task

    async def test同租户并发限制(self) -> None:
        active = 0
        maximum = 0

        async def execute(_id, _args, _token, _update):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.02)
            active -= 1
            return AgentToolResult(content=[])

        tool = AgentTool(
            name="tenant_tool",
            label="tenant",
            description="tenant",
            execute=execute,
            resolve_tenant_id=lambda args: args["tenant"],
        )
        runtime = ToolDispatchRuntime(
            [tool],
            max_parallel_tools=4,
            tenant_limits={"acme": 1},
        )
        await runtime.dispatch_many(
            [
                tool_call("1", "tenant_tool", {"tenant": "acme"}),
                tool_call("2", "tenant_tool", {"tenant": "acme"}),
            ],
            context=AgentContext("", [], [tool]),
            assistant_message={"role": "assistant", "content": []},
            cancellation=CancellationToken(),
            emit=lambda _event: None,
            tenant_id="acme",
        )
        self.assertEqual(maximum, 1)

    async def test_exclusive跨独立dispatch形成全局屏障(self) -> None:
        active = 0
        maximum = 0

        async def execute(_id, _args, _token, _update):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.03)
            active -= 1
            return AgentToolResult(content=[])

        tool = AgentTool(
            name="exclusive_tool",
            label="exclusive",
            description="exclusive",
            execute=execute,
            execution_mode="exclusive",
        )
        runtime = ToolDispatchRuntime([tool], max_parallel_tools=4)

        first, second = await asyncio.gather(
            runtime.dispatch(tool_call("1", "exclusive_tool")),
            runtime.dispatch(tool_call("2", "exclusive_tool")),
        )

        self.assertFalse(first.is_error)
        self.assertFalse(second.is_error)
        self.assertEqual(maximum, 1)
        self.assertEqual(runtime.scheduler.stats.active, 0)

    async def test_可信tenant缺失与伪造参数均安全失败(self) -> None:
        calls = 0

        async def execute(_id, _args, _token, _update):
            nonlocal calls
            calls += 1
            return AgentToolResult(content=[])

        tool = AgentTool(
            name="tenant_tool",
            label="tenant",
            description="tenant",
            execute=execute,
            resolve_tenant_id=lambda args: args.get("tenant"),
        )
        runtime = ToolDispatchRuntime(
            [tool],
            tenant_limits={"acme": 1},
        )

        missing = await runtime.dispatch(
            tool_call("missing", "tenant_tool", {"tenant": "acme"})
        )
        forged = await runtime.dispatch(
            tool_call("forged", "tenant_tool", {"tenant": "attacker"}),
            tenant_id="acme",
        )
        unknown = await runtime.dispatch(
            tool_call("unknown", "tenant_tool", {"tenant": "other"}),
            tenant_id="other",
        )
        success = await runtime.dispatch(
            tool_call("success", "tenant_tool", {"tenant": "acme"}),
            tenant_id="acme",
        )

        self.assertEqual(missing.result.details["code"], "tenant_context_required")
        self.assertEqual(forged.result.details["code"], "tenant_context_mismatch")
        self.assertEqual(unknown.result.details["code"], "tenant_context_unconfigured")
        self.assertFalse(success.is_error)
        self.assertEqual(calls, 1)

    async def test_可信tenant不要求出现在模型参数中(self) -> None:
        calls = 0

        async def execute(_id, args, _token, _update):
            nonlocal calls
            calls += 1
            self.assertEqual(args, {"orderId": 9})
            return AgentToolResult(content=[])

        tool = AgentTool(
            name="business_tool",
            label="business",
            description="business",
            execute=execute,
        )
        runtime = ToolDispatchRuntime(
            [tool],
            tenant_limits={"acme": 1},
            default_tenant_id="acme",
        )

        outcome = await runtime.dispatch(
            tool_call("1", "business_tool", {"orderId": 9})
        )

        self.assertFalse(outcome.is_error)
        self.assertEqual(calls, 1)

    async def test_lease丢失立即取消safe工具且不提交(self) -> None:
        class LeaseLostBackend:
            lease_seconds = 0.03

            async def acquire(self, *_args, **_kwargs):
                return True

            async def renew(self, *_args, **_kwargs):
                return False

            async def release(self, *_args, **_kwargs):
                return None

        started = asyncio.Event()
        committed = False

        async def execute(_id, _args, _token, _update):
            nonlocal committed
            started.set()
            await asyncio.sleep(10)
            committed = True
            return AgentToolResult(content=[])

        tool = AgentTool(
            name="safe_read",
            label="safe",
            description="safe",
            execute=execute,
            execution_mode="resource_locked",
            resolve_resource_keys=lambda _args: "order:lease-safe",
            replay_policy="safe",
        )
        runtime = ToolDispatchRuntime(
            [tool],
            distributed_lock_backend=LeaseLostBackend(),
        )

        outcome = await asyncio.wait_for(
            runtime.dispatch(tool_call("1", "safe_read")),
            1,
        )

        self.assertTrue(started.is_set())
        self.assertFalse(committed)
        self.assertTrue(outcome.is_error)
        self.assertEqual(outcome.result.details["code"], "resource_lease_lost")
        self.assertEqual(outcome.result.details["reason"], "resource_lease_lost")

    async def test_never工具开始后lease丢失必须返回outcome_unknown(self) -> None:
        class LeaseLostBackend:
            lease_seconds = 0.03

            async def acquire(self, *_args, **_kwargs):
                return True

            async def renew(self, *_args, **_kwargs):
                return False

            async def release(self, *_args, **_kwargs):
                return None

        side_effects: list[str] = []

        async def execute(_id, _args, _token, _update):
            side_effects.append("committed")
            await asyncio.sleep(10)
            return AgentToolResult(content=[])

        tool = AgentTool(
            name="write_once",
            label="write",
            description="write",
            execute=execute,
            execution_mode="resource_locked",
            resolve_resource_keys=lambda _args: "order:lease-write",
            replay_policy="never",
        )
        runtime = ToolDispatchRuntime(
            [tool],
            distributed_lock_backend=LeaseLostBackend(),
        )

        outcome = await asyncio.wait_for(
            runtime.dispatch(tool_call("1", "write_once")),
            1,
        )

        self.assertEqual(side_effects, ["committed"])
        self.assertTrue(outcome.is_error)
        self.assertEqual(outcome.result.details["code"], "outcome_unknown")
        self.assertEqual(outcome.result.details["reason"], "resource_lease_lost")
        self.assertFalse(outcome.result.details["retryable"])
        await asyncio.sleep(0)
        live = [
            task.get_name()
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and not task.done()
            and (
                task.get_name().startswith("tool-leased-execute:")
                or task.get_name().startswith("tool-resource-heartbeat:")
            )
        ]
        self.assertEqual(live, [])

    async def test_lease在release阶段才暴露也不能退化成execution_error(self) -> None:
        async def execute(_id, _args, _token, _update):
            return AgentToolResult(content=[], details={"committed": True})

        tool = AgentTool(
            name="late_loss",
            label="late loss",
            description="late loss",
            execute=execute,
            replay_policy="never",
        )
        runtime = ToolDispatchRuntime([tool])

        class LateLostLease:
            heartbeat = None

            async def release(self):
                raise ResourceLeaseLostError(
                    "late lease loss",
                    outcome_unknown=False,
                )

        async def acquire(_tool, _args, *, tenant_id=None):
            return LateLostLease()

        runtime.scheduler.acquire = acquire  # type: ignore[method-assign]
        events: list[dict] = []

        outcome = await runtime.dispatch(
            tool_call("1", "late_loss"),
            emit=events.append,
        )

        self.assertTrue(outcome.is_error)
        self.assertEqual(outcome.result.details["code"], "outcome_unknown")
        self.assertNotEqual(outcome.result.details["code"], "tool_execution_error")
        lease_event = next(
            event for event in events if event["type"] == "tool_resource_lease_lost"
        )
        self.assertTrue(lease_event["outcomeUnknown"])
        self.assertEqual(lease_event["detectedDuring"], "release")

    async def test_sqlite跨runtime资源锁互斥(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "locks.sqlite3"
            first_started = asyncio.Event()
            release = asyncio.Event()

            async def first_execute(_id, _args, _token, _update):
                first_started.set()
                await release.wait()
                return AgentToolResult(content=[])

            async def second_execute(_id, _args, _token, _update):
                return AgentToolResult(content=[])

            def make(name, execute, timeout=None):
                return AgentTool(
                    name=name,
                    label=name,
                    description=name,
                    execute=execute,
                    execution_mode="resource_locked",
                    resolve_resource_keys=lambda _args: "order:9",
                    lock_timeout_seconds=timeout,
                )

            first_tool = make("first", first_execute)
            second_tool = make("second", second_execute, 0.05)
            first_runtime = ToolDispatchRuntime(
                [first_tool],
                distributed_lock_backend=SQLiteResourceLockBackend(path),
            )
            second_runtime = ToolDispatchRuntime(
                [second_tool],
                distributed_lock_backend=SQLiteResourceLockBackend(path),
            )
            task = asyncio.create_task(first_runtime.dispatch(tool_call("1", "first")))
            await asyncio.wait_for(first_started.wait(), 1)
            outcome = await second_runtime.dispatch(tool_call("2", "second"))
            self.assertTrue(outcome.is_error)
            self.assertEqual(outcome.result.details["code"], "resource_lock_timeout")
            release.set()
            await task

    async def test_sqlite资源锁向下游传递稳定scope和单调generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fenced-locks.sqlite3"
            observed: list[tuple[int | None, str | None]] = []

            async def execute_with_context(
                _id,
                _args,
                context,
                _token,
                _update,
            ):
                observed.append(
                    (
                        context.resource_fencing_token,
                        context.resource_fencing_scope,
                    )
                )
                return AgentToolResult(content=[])

            tool = AgentTool(
                name="fenced-write",
                label="fenced-write",
                description="fenced-write",
                execute=None,
                execute_with_context=execute_with_context,
                execution_mode="resource_locked",
                resolve_resource_keys=lambda _args: "order:9",
                replay_policy="never",
                supports_resource_fencing=True,
            )
            first = ToolDispatchRuntime(
                [tool],
                distributed_lock_backend=SQLiteResourceLockBackend(path),
            )
            second = ToolDispatchRuntime(
                [tool],
                distributed_lock_backend=SQLiteResourceLockBackend(path),
            )

            self.assertFalse(
                (await first.dispatch(tool_call("first", tool.name))).is_error
            )
            self.assertFalse(
                (await second.dispatch(tool_call("second", tool.name))).is_error
            )
            self.assertEqual(len(observed), 2)
            first_token, first_scope = observed[0]
            second_token, second_scope = observed[1]
            self.assertIsInstance(first_token, int)
            self.assertIsInstance(second_token, int)
            assert first_token is not None and second_token is not None
            self.assertGreater(second_token, first_token)
            self.assertEqual(first_scope, second_scope)

    async def test_锁后端虚报fencing能力时在callback前fail_closed(self) -> None:
        class LyingBackend:
            capabilities = ResourceLockBackendCapabilities(
                backend_name="lying-lock",
                supports_cross_process=True,
                supports_multi_host=True,
                atomic_multi_resource_acquire=True,
                supports_lease_renewal=True,
                supports_fencing_tokens=True,
            )

            async def acquire(self, *_args, **_kwargs):
                return True

            async def renew(self, *_args, **_kwargs):
                return True

            async def release(self, *_args, **_kwargs):
                return None

        calls = 0

        async def execute_with_context(
            _id,
            _args,
            _context,
            _token,
            _update,
        ):
            nonlocal calls
            calls += 1
            return AgentToolResult(content=[])

        tool = AgentTool(
            name="fenced-write",
            label="fenced-write",
            description="fenced-write",
            execute=None,
            execute_with_context=execute_with_context,
            execution_mode="resource_locked",
            resolve_resource_keys=lambda _args: "order:9",
            supports_resource_fencing=True,
        )
        outcome = await ToolDispatchRuntime(
            [tool],
            distributed_lock_backend=LyingBackend(),
        ).dispatch(tool_call("lying", tool.name))

        self.assertTrue(outcome.is_error)
        self.assertEqual(calls, 0)

    async def test_sqlite锁获取线程被取消后不会遗留孤儿lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cancelled-lock.sqlite3"
            backend = SQLiteResourceLockBackend(path)
            started = threading.Event()
            release = threading.Event()
            original = backend._try_acquire_sync

            def blocking_acquire(resource_keys, owner_token, access):
                started.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("test lock worker was not released")
                return original(resource_keys, owner_token, access)

            backend._try_acquire_sync = blocking_acquire

            async def execute(_id, _args, _token, _update):
                return AgentToolResult(content=[])

            tool = AgentTool(
                name="cancel-lock",
                label="cancel-lock",
                description="cancel-lock",
                execute=execute,
                execution_mode="resource_locked",
                resolve_resource_keys=lambda _args: "order:cancelled",
            )
            runtime = ToolDispatchRuntime(
                [tool],
                distributed_lock_backend=backend,
            )
            task = asyncio.create_task(
                runtime.dispatch(tool_call("cancelled", "cancel-lock"))
            )
            deadline = asyncio.get_running_loop().time() + 2
            while not started.is_set():
                if asyncio.get_running_loop().time() >= deadline:
                    self.fail("distributed lock worker did not start")
                await asyncio.sleep(0.001)

            task.cancel("cancel while sqlite lock commits")
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)

            with closing(sqlite3.connect(path)) as connection:
                remaining = connection.execute(
                    "SELECT COUNT(*) FROM tool_resource_locks"
                ).fetchone()[0]
            self.assertEqual(remaining, 0)


if __name__ == "__main__":
    unittest.main()
