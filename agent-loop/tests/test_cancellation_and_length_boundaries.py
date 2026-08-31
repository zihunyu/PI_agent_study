"""Host/Router/Agent 取消传播与 length 截断失败语义回归测试。"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import (  # noqa: E402
    Agent,
    AgentTool,
    AgentToolResult,
    CancellationToken,
    CapabilityRegistry,
    DurableAgentHost,
    HybridModelRouter,
    Model,
    OperationCancelledError,
    RequestDecision,
    RoutedAgent,
    ScriptedProvider,
    ToolChoicePolicy,
    assistant_message,
    load_simple_business_config,
    replay_operation,
)


class _CountingRouter:
    def __init__(self) -> None:
        self.call_count = 0
        self.tokens: list[CancellationToken | None] = []

    def route(
        self,
        _text: str,
        *,
        cancellation: CancellationToken | None = None,
    ) -> RequestDecision:
        self.call_count += 1
        self.tokens.append(cancellation)
        return RequestDecision(
            status="in_scope_no_tool",
            reason="测试普通回答",
            message="普通回答",
            tool_policy=ToolChoicePolicy("none"),
        )


class CancellationAndLengthBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.model = Model(id="boundary-model", provider="fake", api="fake")

    async def test_agent预取消不调用provider也不创建消息(self) -> None:
        provider = ScriptedProvider([])
        agent = Agent(model=self.model, stream_fn=provider.stream)
        token = CancellationToken()
        token.cancel("用户预取消")

        with self.assertRaises(OperationCancelledError):
            await agent.prompt("不得执行", cancellation=token)

        self.assertEqual(provider.call_count, 0)
        self.assertEqual(agent.state.messages, [])
        self.assertFalse(agent.state.is_streaming)

    async def test_routed_agent预取消不调用router或provider(self) -> None:
        provider = ScriptedProvider([])
        router = _CountingRouter()
        routed = RoutedAgent(
            Agent(model=self.model, stream_fn=provider.stream),
            router,
            CapabilityRegistry(),
        )
        token = CancellationToken()
        token.cancel("用户预取消")

        with self.assertRaises(OperationCancelledError):
            await routed.prompt("不得路由", cancellation=token)

        self.assertEqual(router.call_count, 0)
        self.assertEqual(provider.call_count, 0)

    async def test取消可中断不接收token的旧异步router(self) -> None:
        class BlockingRouter:
            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.cancelled = asyncio.Event()

            async def route(self, _text: str) -> RequestDecision:
                self.started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.cancelled.set()

        provider = ScriptedProvider([])
        router = BlockingRouter()
        routed = RoutedAgent(
            Agent(model=self.model, stream_fn=provider.stream),
            router,
            CapabilityRegistry(),
        )
        token = CancellationToken()
        task = asyncio.create_task(
            routed.prompt("等待路由", cancellation=token)
        )
        await asyncio.wait_for(router.started.wait(), timeout=1)

        token.cancel("用户停止路由")

        with self.assertRaises(OperationCancelledError):
            await asyncio.wait_for(task, timeout=1)
        self.assertTrue(router.cancelled.is_set())
        self.assertEqual(provider.call_count, 0)

    async def test_host把同一token贯穿无router和有router普通agent(self) -> None:
        seen: list[CancellationToken | None] = []

        def answer(_context, options):
            seen.append(options.get("cancellation_token"))
            return assistant_message(
                model=self.model,
                content=[{"type": "text", "text": "完成"}],
            )

        with tempfile.TemporaryDirectory() as directory:
            direct_token = CancellationToken()
            direct = await DurableAgentHost.create(
                session_id="direct-cancellation",
                state_dir=directory,
                model=self.model,
                stream_fn=ScriptedProvider([answer]).stream,
                system_prompt="test",
                tools=[],
                auto_recover=False,
            )
            try:
                await direct.prompt("直接回答", cancellation=direct_token)
            finally:
                await direct.close()

        with tempfile.TemporaryDirectory() as directory:
            routed_token = CancellationToken()
            router = _CountingRouter()
            routed = await DurableAgentHost.create(
                session_id="routed-cancellation",
                state_dir=directory,
                model=self.model,
                stream_fn=ScriptedProvider([answer]).stream,
                system_prompt="test",
                tools=[],
                router=router,
                capabilities=CapabilityRegistry(),
                auto_recover=False,
            )
            try:
                await routed.prompt("路由回答", cancellation=routed_token)
            finally:
                await routed.close()

        self.assertEqual(seen, [direct_token, routed_token])
        self.assertEqual(router.tokens, [routed_token])

    async def test_host预取消不调用router或provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider([])
            router = _CountingRouter()
            host = await DurableAgentHost.create(
                session_id="host-pre-cancelled",
                state_dir=directory,
                model=self.model,
                stream_fn=provider.stream,
                system_prompt="test",
                tools=[],
                router=router,
                capabilities=CapabilityRegistry(),
                auto_recover=False,
            )
            token = CancellationToken()
            token.cancel("用户预取消")
            try:
                with self.assertRaises(OperationCancelledError):
                    await host.prompt("不得执行", cancellation=token)
            finally:
                await host.close()

        self.assertEqual(router.call_count, 0)
        self.assertEqual(provider.call_count, 0)

    async def test_live_agent拒绝纯文本length且不处理follow_up(self) -> None:
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=self.model,
                    stop_reason="length",
                    content=[{"type": "text", "text": "不完整敏感片段"}],
                ),
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "不应调用"}],
                ),
            ]
        )
        agent = Agent(model=self.model, stream_fn=provider.stream)
        agent.follow_up("不应继续")

        await agent.prompt("生成长回答")

        self.assertEqual(provider.call_count, 1)
        self.assertEqual(agent.state.messages[-1]["stopReason"], "length")
        self.assertIn("结果不完整", agent.state.error_message or "")

    async def test_routed_agent不返回length截断文本(self) -> None:
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=self.model,
                    stop_reason="length",
                    content=[{"type": "text", "text": "不完整敏感片段"}],
                )
            ]
        )
        routed = RoutedAgent(
            Agent(model=self.model, stream_fn=provider.stream),
            _CountingRouter(),
            CapabilityRegistry(),
        )

        result = await routed.prompt("生成长回答")

        self.assertEqual(result.error_code, "model_output_truncated")
        self.assertNotIn("不完整敏感片段", result.response_text)
        self.assertIn("长度上限", result.response_text)

    async def test_hybrid_router使用调用方token并拒绝length分类(self) -> None:
        root = Path(__file__).resolve().parents[1]
        config = load_simple_business_config(
            root / "config" / "business.toml.example"
        )
        token = CancellationToken()
        seen: list[CancellationToken | None] = []

        def truncated_route(_context, options):
            seen.append(options.get("cancellation_token"))
            return assistant_message(
                model=self.model,
                stop_reason="length",
                content=[
                    {
                        "type": "toolCall",
                        "id": "truncated-route",
                        "name": "select_business_intent",
                        "arguments": {
                            "decision": "order.explain_status",
                            "arguments": {},
                            "confidence": 0.99,
                            "reason": "截断分类不得采用",
                        },
                    }
                ],
            )

        provider = ScriptedProvider([truncated_route])
        router = HybridModelRouter(
            config,
            CapabilityRegistry(),
            model=self.model,
            stream_fn=provider.stream,
        )

        decision = await router.route("解释订单状态", cancellation=token)

        self.assertEqual(seen, [token])
        self.assertEqual(decision.status, "in_scope_need_clarification")
        self.assertNotEqual(decision.intent, "order.explain_status")

    async def test_durable_host把length记为失败operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider(
                [
                    assistant_message(
                        model=self.model,
                        stop_reason="length",
                        content=[{"type": "text", "text": "不完整"}],
                    )
                ]
            )
            host = await DurableAgentHost.create(
                session_id="durable-length",
                state_dir=directory,
                model=self.model,
                stream_fn=provider.stream,
                system_prompt="test",
                tools=[],
                auto_recover=False,
            )
            try:
                result = await host.prompt("生成长回答")
                operation = replay_operation(
                    await host.operation_store.load(
                        operation_id=result.operation_id
                    )
                )
            finally:
                await host.close()

        self.assertEqual(operation.phase, "failed")
        self.assertEqual(host.runtime_tracker.state.phase, "failed")

    async def test_durable_length工具调用补写结果但绝不执行(self) -> None:
        executed = False

        async def execute(_call_id, _arguments, _token, _update):
            nonlocal executed
            executed = True
            return AgentToolResult(content=[])

        tool = AgentTool(
            name="danger",
            label="danger",
            description="must not execute truncated arguments",
            execute=execute,
        )
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider(
                [
                    assistant_message(
                        model=self.model,
                        stop_reason="length",
                        content=[
                            {
                                "type": "toolCall",
                                "id": "truncated-danger",
                                "name": "danger",
                                "arguments": {"value": "possibly-truncated"},
                            }
                        ],
                    )
                ]
            )
            host = await DurableAgentHost.create(
                session_id="durable-length-tool",
                state_dir=directory,
                model=self.model,
                stream_fn=provider.stream,
                system_prompt="test",
                tools=[tool],
                auto_recover=False,
            )
            try:
                result = await host.prompt("执行危险工具")
                operation = replay_operation(
                    await host.operation_store.load(
                        operation_id=result.operation_id
                    )
                )
            finally:
                await host.close()

        self.assertFalse(executed)
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(operation.phase, "failed")
        self.assertEqual(
            [message["role"] for message in operation.messages],
            ["user", "assistant", "toolResult"],
        )
        self.assertEqual(
            operation.messages[-1]["details"]["code"],
            "model_output_truncated",
        )


if __name__ == "__main__":
    unittest.main()
