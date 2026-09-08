"""P2 生产能力必须真正进入 Durable Host 主链路。"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from pi_agent_loop import (
    ApprovalError,
    CapabilityRegistry,
    DurableAgentHost,
    DurableHostClosedError,
    DurableHostLifecycle,
    DurableHostResources,
    HybridModelRouter,
    IdentityClaim,
    Model,
    ModelRequestPolicy,
    ModelRetryPolicy,
    OpenAICompatibleProvider,
    ProviderProfile,
    ScriptedProvider,
    SessionJournalOperationEventStore,
    SessionJournalRetryEventStore,
    SessionJournalRuntimeEventStore,
    SimpleBusinessConfig,
    SimpleProduct,
    StaticIdentityVerifier,
    VerifiedIdentity,
    assistant_message,
)


MODEL = Model(id="integration-model", provider="test", api="scripted")


class ManagedScriptedProvider(ScriptedProvider):
    def __init__(self, responses):
        super().__init__(responses)
        self.close_count = 0

    async def aclose(self) -> None:
        self.close_count += 1


class P2HostIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent结算失败仍会关闭托管资源但host保留可重试状态(self) -> None:
        class State:
            is_streaming = False

        class BrokenAgent:
            state = State()

            async def wait_for_idle(self):
                raise RuntimeError("agent settlement failed")

        class Resources:
            def __init__(self):
                self.closed = False

            async def close(self):
                self.closed = True

        resources = Resources()
        lifecycle = DurableHostLifecycle(BrokenAgent(), resources)
        with self.assertRaises(BaseExceptionGroup):
            await lifecycle.close()
        self.assertTrue(resources.closed)
        self.assertFalse(lifecycle.closed)
        self.assertFalse(lifecycle.accepting)

    async def test_资源关闭取消异常不被TypeError覆盖且可重试(self) -> None:
        class State:
            is_streaming = False

        class AgentStub:
            state = State()

            async def wait_for_idle(self):
                return None

        class FlakyResources:
            def __init__(self):
                self.attempts = 0

            async def close(self):
                self.attempts += 1
                if self.attempts == 1:
                    raise asyncio.CancelledError("transient close cancellation")

        resources = FlakyResources()
        lifecycle = DurableHostLifecycle(AgentStub(), resources)
        with self.assertRaises(BaseExceptionGroup) as raised:
            await lifecycle.close()
        self.assertIsInstance(raised.exception.exceptions[0], asyncio.CancelledError)
        self.assertFalse(lifecycle.closed)

        await lifecycle.close()

        self.assertTrue(lifecycle.closed)
        self.assertEqual(resources.attempts, 2)

    async def test_资源集合只重试上次关闭失败的对象(self) -> None:
        class Resource:
            def __init__(self, fail_once=False):
                self.fail_once = fail_once
                self.attempts = 0

            async def aclose(self):
                self.attempts += 1
                if self.fail_once and self.attempts == 1:
                    raise asyncio.CancelledError("retry me")

        flaky = Resource(fail_once=True)
        healthy = Resource()
        resources = DurableHostResources(
            root=Path("."),
            operation_store=None,
            runtime_store=None,
            retry_store=None,
            owned_resources=[flaky, healthy],
        )
        with self.assertRaises(BaseExceptionGroup):
            await resources.close()
        self.assertEqual(healthy.attempts, 1)
        self.assertEqual(flaky.attempts, 1)

        await resources.close()

        self.assertEqual(healthy.attempts, 1)
        self.assertEqual(flaky.attempts, 2)

    async def test_close会取消并等待路由阶段的活动prompt(self) -> None:
        class BlockingRouter:
            def __init__(self):
                self.started = asyncio.Event()

            async def route(self, _text):
                self.started.set()
                await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as directory:
            router = BlockingRouter()
            provider = ManagedScriptedProvider([])
            host = await DurableAgentHost.create(
                session_id="close-routing",
                state_dir=directory,
                model=MODEL,
                stream_fn=provider.stream,
                system_prompt="integration",
                tools=[],
                router=router,
                capabilities=CapabilityRegistry(),
                auto_recover=False,
                owned_resources=(provider,),
            )
            prompt_task = asyncio.create_task(host.prompt("block in router"))
            await asyncio.wait_for(router.started.wait(), timeout=2)

            await asyncio.wait_for(host.close(), timeout=5)

            with self.assertRaises(asyncio.CancelledError):
                await prompt_task
            self.assertEqual(provider.close_count, 1)
            self.assertEqual(host.lifecycle.active_operation_count, 0)
            with self.assertRaises(DurableHostClosedError):
                await host.prompt("must be rejected")

    async def test_close调用方连续取消两次仍先完成资源清理(self) -> None:
        class State:
            is_streaming = False

        class AgentStub:
            state = State()

            async def wait_for_idle(self):
                return None

        class SlowResources:
            def __init__(self):
                self.started = asyncio.Event()
                self.release = asyncio.Event()
                self.completed = False
                self.cancelled = False

            async def close(self):
                self.started.set()
                try:
                    await self.release.wait()
                    self.completed = True
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise

        resources = SlowResources()
        lifecycle = DurableHostLifecycle(AgentStub(), resources)
        close_task = asyncio.create_task(lifecycle.close())
        await asyncio.wait_for(resources.started.wait(), timeout=2)

        close_task.cancel("first cancellation")
        await asyncio.sleep(0)
        close_task.cancel("second cancellation")
        await asyncio.sleep(0)

        self.assertFalse(close_task.done())
        self.assertFalse(resources.cancelled)
        resources.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(close_task, timeout=2)
        self.assertTrue(resources.completed)
        self.assertFalse(resources.cancelled)
        self.assertTrue(lifecycle.closed)

    async def test_并发close共享同一个资源清理任务(self) -> None:
        class State:
            is_streaming = False

        class AgentStub:
            state = State()

            async def wait_for_idle(self):
                return None

        class SlowResources:
            def __init__(self):
                self.started = asyncio.Event()
                self.release = asyncio.Event()
                self.attempts = 0

            async def close(self):
                self.attempts += 1
                self.started.set()
                await self.release.wait()

        resources = SlowResources()
        lifecycle = DurableHostLifecycle(AgentStub(), resources)
        first = asyncio.create_task(lifecycle.close())
        await asyncio.wait_for(resources.started.wait(), timeout=2)
        second = asyncio.create_task(lifecycle.close())
        await asyncio.sleep(0)

        self.assertEqual(resources.attempts, 1)
        resources.release.set()
        await asyncio.gather(first, second)
        self.assertTrue(lifecycle.closed)
        self.assertEqual(resources.attempts, 1)

    async def test_活动操作取消清理中调用close不会与外部close死锁(self) -> None:
        class State:
            is_streaming = False

        class AgentStub:
            state = State()

            async def wait_for_idle(self):
                return None

        class Resources:
            def __init__(self):
                self.closed = False

            async def close(self):
                self.closed = True

        resources = Resources()
        lifecycle = DurableHostLifecycle(AgentStub(), resources)
        started = asyncio.Event()
        inner_close_rejected = asyncio.Event()

        async def operation():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                try:
                    await lifecycle.close()
                except RuntimeError:
                    inner_close_rejected.set()

        active = asyncio.create_task(lifecycle.run(operation))
        await asyncio.wait_for(started.wait(), timeout=2)

        await asyncio.wait_for(lifecycle.close(), timeout=2)

        with self.assertRaises(asyncio.CancelledError):
            await active
        self.assertTrue(inner_close_rejected.is_set())
        self.assertTrue(resources.closed)
        self.assertTrue(lifecycle.closed)

    async def test_default_host共用runtime并只写统一加密journal(self) -> None:
        secret = "customer-secret-8848"
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider([
                assistant_message(
                    model=MODEL,
                    content=[{"type": "text", "text": "ok"}],
                )
            ])
            host = await DurableAgentHost.create(
                session_id="unified-mainline",
                state_dir=directory,
                model=MODEL,
                stream_fn=provider.stream,
                system_prompt="integration",
                tools=[],
            )

            await host.prompt(secret)
            await host.close()

            self.assertIs(host.agent.stream_fn.__self__, host.model_runtime)
            self.assertIs(host.tool_runtime.runtime, host.tool_dispatch_runtime)
            self.assertIsInstance(
                host.operation_store,
                SessionJournalOperationEventStore,
            )
            self.assertIsInstance(
                host.resources.runtime_store,
                SessionJournalRuntimeEventStore,
            )
            self.assertIsInstance(
                host.resources.retry_store,
                SessionJournalRetryEventStore,
            )
            self.assertIs(
                host.operation_store.journal,
                host.resources.runtime_store.journal,
            )
            self.assertIs(
                host.operation_store.journal,
                host.resources.retry_store.journal,
            )
            root = Path(directory)
            self.assertFalse((root / "runtime-events.jsonl").exists())
            self.assertFalse((root / "operation-events.jsonl").exists())
            self.assertFalse((root / "retry-events.jsonl").exists())
            database = root / "agent-state.sqlite3"
            self.assertNotIn(secret.encode(), database.read_bytes())
            with closing(sqlite3.connect(database)) as connection:
                kinds = {
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT journal_kind FROM session_events"
                    )
                }
            self.assertEqual(kinds, {"runtime", "operation", "retry", "audit"})
            with closing(sqlite3.connect(database)) as connection:
                model_boundary_types = {
                    row[0]
                    for row in connection.execute(
                        "SELECT event_type FROM session_events "
                        "WHERE journal_kind = 'retry'"
                    )
                }
            self.assertEqual(
                model_boundary_types,
                {"model_request_started", "model_request_completed"},
            )

    async def test_hybrid_router使用独立副本绑定同一个model_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider([])
            router = HybridModelRouter(
                SimpleBusinessConfig(
                    product=SimpleProduct("demo", "demo", True),
                    intents=(),
                ),
                CapabilityRegistry(),
                model=MODEL,
                stream_fn=provider.stream,
            )
            host = await DurableAgentHost.create(
                session_id="shared-router-runtime",
                state_dir=directory,
                model=MODEL,
                stream_fn=provider.stream,
                system_prompt="integration",
                tools=[],
                router=router,
                capabilities=CapabilityRegistry(),
                auto_recover=False,
            )
            try:
                host_router = host.routed_agent.router
                self.assertIsNot(host_router, router)
                self.assertIs(host_router.stream_fn.__self__, host.model_runtime)
                self.assertIs(router.stream_fn.__self__, provider)
                self.assertIs(
                    host.model_runtime.upstream_stream_fn.__self__,
                    provider,
                )
            finally:
                await host.close()

    async def test_同一router模板不会在两个host之间串线(self) -> None:
        def classifier_response(decision: str) -> dict:
            return assistant_message(
                model=MODEL,
                stop_reason="toolUse",
                content=[
                    {
                        "type": "toolCall",
                        "id": f"route-{decision}",
                        "name": "select_business_intent",
                        "arguments": {
                            "decision": decision,
                            "arguments": {},
                            "confidence": 0.99,
                            "reason": "host isolation test",
                        },
                    }
                ],
            )

        template_provider = ScriptedProvider([])
        first_provider = ScriptedProvider(
            [classifier_response("__general_qa__")]
        )
        second_provider = ScriptedProvider(
            [classifier_response("__out_of_scope__")]
        )
        config = SimpleBusinessConfig(
            product=SimpleProduct("demo", "demo", True),
            intents=(),
        )
        capabilities = CapabilityRegistry()
        router_template = HybridModelRouter(
            config,
            capabilities,
            model=MODEL,
            stream_fn=template_provider.stream,
        )

        with tempfile.TemporaryDirectory() as directory:
            host_one = await DurableAgentHost.create(
                session_id="router-host-one",
                state_dir=Path(directory) / "one",
                model=MODEL,
                stream_fn=first_provider.stream,
                system_prompt="integration",
                tools=[],
                router=router_template,
                capabilities=capabilities,
                auto_recover=False,
            )
            host_two = await DurableAgentHost.create(
                session_id="router-host-two",
                state_dir=Path(directory) / "two",
                model=MODEL,
                stream_fn=second_provider.stream,
                system_prompt="integration",
                tools=[],
                router=router_template,
                capabilities=capabilities,
                auto_recover=False,
            )
            try:
                first_router = host_one.routed_agent.router
                second_router = host_two.routed_agent.router
                self.assertIsNot(first_router, router_template)
                self.assertIsNot(second_router, router_template)
                self.assertIsNot(first_router, second_router)

                second_decision = await second_router.route("host two request")
                self.assertEqual(second_decision.status, "out_of_scope")
                await host_two.close()

                first_decision = await first_router.route("host one request")
                self.assertEqual(first_decision.status, "in_scope_no_tool")
                self.assertEqual(first_decision.intent, "general.qa")
                self.assertEqual(first_provider.call_count, 1)
                self.assertEqual(second_provider.call_count, 1)
                self.assertEqual(template_provider.call_count, 0)
                self.assertEqual(first_router.call_count, 1)
                self.assertEqual(second_router.call_count, 1)
                self.assertEqual(router_template.call_count, 0)
                self.assertFalse(host_one.model_runtime.closed)
            finally:
                await host_two.close()
                await host_one.close()

    async def test_host可在统一model_runtime配置retry策略(self) -> None:
        failure = assistant_message(
            model=MODEL,
            stop_reason="error",
            error_message="temporary",
        )
        failure["providerError"] = {
            "code": "provider_http_error",
            "statusCode": 503,
            "retryAfterMs": 0,
            "retryable": True,
        }
        provider = ScriptedProvider(
            [
                failure,
                assistant_message(
                    model=MODEL,
                    content=[{"type": "text", "text": "recovered"}],
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            host = await DurableAgentHost.create(
                session_id="host-model-retry",
                state_dir=directory,
                model=MODEL,
                stream_fn=provider.stream,
                system_prompt="integration",
                tools=[],
                model_retry_policy=ModelRetryPolicy(
                    enabled=True,
                    max_retries=1,
                    initial_delay_seconds=0,
                    max_delay_seconds=1,
                    jitter_ratio=0,
                ),
            )
            await host.prompt("retry once")
            self.assertEqual(provider.call_count, 2)
            retry_metrics = [
                row
                for row in host.telemetry.metrics.snapshot()["counters"]
                if row["name"] == "model_retries_total"
            ]
            self.assertEqual(sum(row["value"] for row in retry_metrics), 1)
            await host.close()

    async def test_agent模型边界事件与operation和run使用同一身份(self) -> None:
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=MODEL,
                    content=[{"type": "text", "text": "correlated"}],
                )
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            host = await DurableAgentHost.create(
                session_id="model-correlation",
                state_dir=directory,
                model=MODEL,
                stream_fn=provider.stream,
                system_prompt="integration",
                tools=[],
                auto_recover=False,
            )
            try:
                result = await host.prompt("correlate")
                operation_events = await host.operation_store.load(
                    session_id=host.session_id,
                    operation_id=result.operation_id,
                )
                operation_started = next(
                    event
                    for event in operation_events
                    if event.type == "model_request_started"
                )
                boundary_events = [
                    event
                    for event in await host.resources.retry_store.load()
                    if event.get("type")
                    in {"model_request_started", "model_request_completed"}
                ]
                self.assertEqual(len(boundary_events), 2)
                self.assertEqual(
                    {event["requestId"] for event in boundary_events},
                    {operation_started.data["requestId"]},
                )
                self.assertEqual(
                    {event["operationId"] for event in boundary_events},
                    {result.operation_id},
                )
                self.assertEqual(
                    {event["runId"] for event in boundary_events},
                    {host.runtime_tracker.state.run_id},
                )
                runtime_model_events = [
                    event
                    for event in await host.resources.runtime_store.load()
                    if event.type
                    in {"model_request_started", "model_response_finished"}
                ]
                self.assertEqual(len(runtime_model_events), 2)
                self.assertEqual(
                    {event.data["requestId"] for event in runtime_model_events},
                    {operation_started.data["requestId"]},
                )
                self.assertEqual(
                    {event.data["operationId"] for event in runtime_model_events},
                    {result.operation_id},
                )
            finally:
                await host.close()

    async def test_router模型边界事件绑定当前operation和run(self) -> None:
        classifier = assistant_message(
            model=MODEL,
            stop_reason="toolUse",
            content=[
                {
                    "type": "toolCall",
                    "id": "route-correlated",
                    "name": "select_business_intent",
                    "arguments": {
                        "decision": "__general_qa__",
                        "arguments": {},
                        "confidence": 0.99,
                        "reason": "correlation",
                    },
                }
            ],
        )
        provider = ScriptedProvider(
            [
                classifier,
                assistant_message(
                    model=MODEL,
                    content=[{"type": "text", "text": "answer"}],
                ),
            ]
        )
        router = HybridModelRouter(
            SimpleBusinessConfig(
                product=SimpleProduct("demo", "demo", True),
                intents=(),
            ),
            CapabilityRegistry(),
            model=MODEL,
            stream_fn=provider.stream,
        )
        with tempfile.TemporaryDirectory() as directory:
            host = await DurableAgentHost.create(
                session_id="router-correlation",
                state_dir=directory,
                model=MODEL,
                stream_fn=provider.stream,
                system_prompt="integration",
                tools=[],
                router=router,
                capabilities=CapabilityRegistry(),
                auto_recover=False,
            )
            try:
                result = await host.prompt("question")
                router_events = [
                    event
                    for event in await host.resources.retry_store.load()
                    if event.get("source") == "router"
                ]
                self.assertEqual(
                    [event["type"] for event in router_events],
                    ["model_request_started", "model_request_completed"],
                )
                self.assertEqual(
                    {event["operationId"] for event in router_events},
                    {result.operation_id},
                )
                self.assertEqual(
                    {event["runId"] for event in router_events},
                    {host.runtime_tracker.state.run_id},
                )
            finally:
                await host.close()

    async def test_recovery模型边界复用operation的request身份(self) -> None:
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=MODEL,
                    content=[{"type": "text", "text": "recovered"}],
                )
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            host = await DurableAgentHost.create(
                session_id="recovery-correlation",
                state_dir=directory,
                model=MODEL,
                stream_fn=provider.stream,
                system_prompt="integration",
                tools=[],
                auto_recover=False,
            )
            operation_id = "recover-operation"
            policy = ModelRequestPolicy.no_tools()
            await host.operation_store.append_batch(
                host.session_id,
                operation_id,
                [
                    ("operation_started", {"configuration": {}, "tools": []}),
                    ("model_policy_selected", {"policy": policy.to_dict()}),
                    (
                        "message_appended",
                        {
                            "message": {
                                "role": "user",
                                "content": [{"type": "text", "text": "continue"}],
                            }
                        },
                    ),
                ],
                expected_last_sequence=-1,
            )
            try:
                report = await host.startup_recovery.recover_all()
                self.assertEqual(report.completed, (operation_id,))
                operation_events = await host.operation_store.load(
                    session_id=host.session_id,
                    operation_id=operation_id,
                )
                operation_request = next(
                    event
                    for event in operation_events
                    if event.type == "model_request_started"
                )
                boundary_events = [
                    event
                    for event in await host.resources.retry_store.load()
                    if event.get("source") == "recovery"
                ]
                self.assertEqual(
                    [event["type"] for event in boundary_events],
                    ["model_request_started", "model_request_completed"],
                )
                self.assertEqual(
                    {event["requestId"] for event in boundary_events},
                    {operation_request.data["requestId"]},
                )
                self.assertEqual(
                    {event["operationId"] for event in boundary_events},
                    {operation_id},
                )
                self.assertTrue(
                    all(event.get("runId") for event in boundary_events)
                )
            finally:
                await host.close()

    async def test_host关闭会排空router模型请求再关闭http池(self) -> None:
        profile = ProviderProfile(
            name="router-vendor",
            protocol="openai_chat_completions",
            base_url="https://vendor.example/v1",
            endpoint="/chat/completions",
            auth_type="bearer",
            api_key="test-only-key",
            model="integration-model",
            stream=True,
            connect_timeout_seconds=1,
            request_timeout_seconds=2,
            allow_insecure_http=False,
            retry_policy=ModelRetryPolicy(),
        )
        provider = OpenAICompatibleProvider(profile)
        started = asyncio.Event()
        drained = asyncio.Event()

        async def blocking_request(_stream, _model, _context, _options, _api_key):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()

        provider._request = blocking_request
        router = HybridModelRouter(
            SimpleBusinessConfig(
                product=SimpleProduct("demo", "demo", True),
                intents=(),
            ),
            CapabilityRegistry(),
            model=MODEL,
            stream_fn=provider.stream,
        )
        with tempfile.TemporaryDirectory() as directory:
            host = await DurableAgentHost.create(
                session_id="close-router-model",
                state_dir=directory,
                model=MODEL,
                stream_fn=provider.stream,
                system_prompt="integration",
                tools=[],
                router=router,
                capabilities=CapabilityRegistry(),
                auto_recover=False,
            )
            prompt_task = asyncio.create_task(host.prompt("需要模型分类"))
            await asyncio.wait_for(started.wait(), timeout=2)

            await asyncio.wait_for(host.close(), timeout=2)

            with self.assertRaises(asyncio.CancelledError):
                await prompt_task
            self.assertTrue(drained.is_set())
            self.assertTrue(host.model_runtime.closed)
            self.assertTrue(provider.closed)

    async def test_host关闭显式托管的provider且幂等(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = ManagedScriptedProvider([])
            host = await DurableAgentHost.create(
                session_id="managed-provider",
                state_dir=directory,
                model=MODEL,
                stream_fn=provider.stream,
                system_prompt="integration",
                tools=[],
                auto_recover=False,
                owned_resources=(provider,),
            )
            await host.close()
            await host.close()
            self.assertEqual(provider.close_count, 1)

    async def test_host自动托管stream背后的共享http连接池(self) -> None:
        profile = ProviderProfile(
            name="pooled-vendor",
            protocol="openai_chat_completions",
            base_url="https://vendor.example/v1",
            endpoint="/chat/completions",
            auth_type="bearer",
            api_key="test-only-key",
            model="integration-model",
            stream=True,
            connect_timeout_seconds=1,
            request_timeout_seconds=2,
            allow_insecure_http=False,
            retry_policy=ModelRetryPolicy(),
        )
        provider = OpenAICompatibleProvider(profile)
        with tempfile.TemporaryDirectory() as directory:
            host = await DurableAgentHost.create(
                session_id="auto-managed-http-pool",
                state_dir=directory,
                model=MODEL,
                stream_fn=provider.stream,
                system_prompt="integration",
                tools=[],
                auto_recover=False,
            )
            self.assertIs(host.resources.owned_resources[0], provider)
            await host.close()
        self.assertTrue(provider.closed)

    async def test_host创建中途失败会回收已托管provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = ManagedScriptedProvider([])
            with self.assertRaisesRegex(ValueError, "CapabilityRegistry"):
                await DurableAgentHost.create(
                    session_id="factory-cleanup",
                    state_dir=directory,
                    model=MODEL,
                    stream_fn=provider.stream,
                    system_prompt="integration",
                    tools=[],
                    router=object(),
                    capabilities=None,
                    auto_recover=False,
                    owned_resources=(provider,),
                )
            self.assertEqual(provider.close_count, 1)

    async def test_host构造函数失败也会回收调用方托管资源(self) -> None:
        class BrokenHost(DurableAgentHost):
            def __init__(self) -> None:
                raise RuntimeError("host constructor failed")

        with tempfile.TemporaryDirectory() as directory:
            provider = ManagedScriptedProvider([])
            with self.assertRaisesRegex(RuntimeError, "constructor failed"):
                await BrokenHost.create(
                    session_id="constructor-cleanup",
                    state_dir=directory,
                    model=MODEL,
                    stream_fn=provider.stream,
                    system_prompt="integration",
                    tools=[],
                    auto_recover=False,
                    owned_resources=(provider,),
                )
            self.assertEqual(provider.close_count, 1)

    async def test同一租户多个session可以各自从runtime序号零开始(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for session_id in ("session-a", "session-b"):
                provider = ScriptedProvider([
                    assistant_message(
                        model=MODEL,
                        content=[{"type": "text", "text": session_id}],
                    )
                ])
                host = await DurableAgentHost.create(
                    session_id=session_id,
                    state_dir=directory,
                    tenant_id="same-tenant",
                    model=MODEL,
                    stream_fn=provider.stream,
                    system_prompt="integration",
                    tools=[],
                )
                await host.prompt(session_id)
                await host.close()

            key = (Path(directory) / ".agent-journal.key").read_bytes()
            self.assertEqual(len(key), 32)
            with closing(
                sqlite3.connect(Path(directory) / "agent-state.sqlite3")
            ) as connection:
                sessions = {
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT session_id FROM session_events "
                        "WHERE journal_kind != 'audit'"
                    )
                }
            self.assertEqual(sessions, {"session-a", "session-b"})

    async def test_host不能消费同租户另一个session的approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            hosts = []
            for session_id in ("approval-owner", "approval-outsider"):
                host = await DurableAgentHost.create(
                    session_id=session_id,
                    state_dir=directory,
                    tenant_id="same-tenant",
                    model=MODEL,
                    stream_fn=ScriptedProvider([]).stream,
                    system_prompt="integration",
                    tools=[],
                    auto_recover=False,
                )
                hosts.append(host)
            owner, outsider = hosts
            try:
                await owner.operation_store.append(
                    "operation_started",
                    owner.session_id,
                    "approval-operation",
                    {"configuration": {}, "tools": []},
                )
                verifier = StaticIdentityVerifier(
                    {
                        "requester": ("requester-test-credential", {"operator"}),
                        "approver": ("approver-test-credential", {"approver"}),
                    }
                )
                requester = await verifier.verify(
                    IdentityClaim("requester", "requester-test-credential")
                )
                approval = await owner.approvals.request(
                    session_id=owner.session_id,
                    operation_id="approval-operation",
                    requester=requester,
                    action={"operation": "write"},
                    action_summary="cross-session",
                    required_role="approver",
                )
                approver = await verifier.verify(
                    IdentityClaim("approver", "approver-test-credential")
                )
                called = False

                async def write_handler(*_args, fenced_claim):
                    nonlocal called
                    self.assertIsNotNone(fenced_claim)
                    called = True

                with self.assertRaises(ApprovalError) as raised:
                    await outsider.approve_and_resume(
                        approval.approval_id,
                        approver=approver,
                        consumer=requester,
                        idempotency_key="cross-session",
                        write_handler=write_handler,
                    )
                self.assertEqual(raised.exception.code, "approval_not_found")
                self.assertFalse(called)
            finally:
                await outsider.close()
                await owner.close()

    async def test_host透传生产identity_validator并校验tool_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            identity = VerifiedIdentity(
                principal_id="oidc-operator",
                roles=frozenset({"operator"}),
                issuer="enterprise-oidc",
                verification_id="oidc-runtime-proof",
            )

            def validator(candidate: VerifiedIdentity) -> bool:
                return candidate is identity

            host = await DurableAgentHost.create(
                session_id="production-identity",
                state_dir=directory,
                tenant_id="tenant-a",
                model=MODEL,
                stream_fn=ScriptedProvider([]).stream,
                system_prompt="integration",
                tools=[],
                tool_identity=identity,
                approval_identity_validator=validator,
                auto_recover=False,
            )
            try:
                await host.operation_store.append(
                    "operation_started",
                    host.session_id,
                    "identity-operation",
                    {"configuration": {}, "tools": []},
                )
                approval = await host.approvals.request(
                    session_id=host.session_id,
                    operation_id="identity-operation",
                    requester=identity,
                    action={"operation": "read"},
                    action_summary="production identity",
                    required_role="operator",
                )
                self.assertEqual(approval.requester_id, identity.principal_id)
            finally:
                await host.close()

            rejected = VerifiedIdentity(
                principal_id="forged",
                roles=frozenset({"operator"}),
                issuer="untrusted",
                verification_id="forged-proof",
            )
            with self.assertRaises(ApprovalError) as raised:
                await DurableAgentHost.create(
                    session_id="rejected-tool-identity",
                    state_dir=directory,
                    tenant_id="tenant-a",
                    model=MODEL,
                    stream_fn=ScriptedProvider([]).stream,
                    system_prompt="integration",
                    tools=[],
                    tool_identity=rejected,
                    approval_identity_validator=validator,
                    auto_recover=False,
                )
            self.assertEqual(raised.exception.code, "identity_provenance_invalid")


if __name__ == "__main__":
    unittest.main()
