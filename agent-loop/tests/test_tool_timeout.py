"""每个工具独立 Tool Timeout 测试。"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import (  # noqa: E402
    Agent,
    AgentTool,
    AgentToolResult,
    Model,
    ScriptedProvider,
    assistant_message,
    create_divide_tool,
)


class ToolTimeoutTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.model = Model(id="timeout-model", provider="fake", api="fake")

    def provider_for_calls(self, calls: list[dict]) -> ScriptedProvider:
        """创建“先调用工具、再结束回答”的假 Provider。"""

        return ScriptedProvider(
            [
                assistant_message(
                    model=self.model,
                    stop_reason="toolUse",
                    content=calls,
                ),
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "工具阶段结束"}],
                ),
            ]
        )

    async def test_工具在自己的超时限制内完成(self) -> None:
        async def execute(_id, _args, _token, _on_update):
            await asyncio.sleep(0.005)
            return AgentToolResult(
                content=[{"type": "text", "text": "快速完成"}], details={}
            )

        tool = AgentTool(
            name="fast",
            label="快速工具",
            description="快速完成",
            execute=execute,
            timeout_seconds=0.1,
        )
        provider = self.provider_for_calls(
            [
                {
                    "type": "toolCall",
                    "id": "fast-id",
                    "name": "fast",
                    "arguments": {},
                }
            ]
        )
        agent = Agent(model=self.model, stream_fn=provider.stream, tools=[tool])

        await agent.prompt("运行快速工具")

        result = next(
            message
            for message in agent.state.messages
            if message["role"] == "toolResult"
        )
        self.assertFalse(result["isError"])
        self.assertEqual(result["content"][0]["text"], "快速完成")

    async def test_工具超过自己的限制会返回_timeout_错误(self) -> None:
        async def execute(_id, _args, _token, _on_update):
            await asyncio.sleep(10)
            return AgentToolResult(content=[], details={})

        tool = AgentTool(
            name="slow",
            label="慢工具",
            description="故意超过时间",
            execute=execute,
            timeout_seconds=0.02,
        )
        provider = self.provider_for_calls(
            [
                {
                    "type": "toolCall",
                    "id": "slow-id",
                    "name": "slow",
                    "arguments": {},
                }
            ]
        )
        agent = Agent(model=self.model, stream_fn=provider.stream, tools=[tool])

        await agent.prompt("运行慢工具")

        result = next(
            message
            for message in agent.state.messages
            if message["role"] == "toolResult"
        )
        self.assertTrue(result["isError"])
        self.assertEqual(result["details"]["code"], "tool_timeout")
        self.assertEqual(result["details"]["timeoutSeconds"], 0.02)
        self.assertIn("执行超时", result["content"][0]["text"])

    async def test_并行工具中一个超时不会影响另一个工具(self) -> None:
        async def slow_execute(_id, _args, _token, _on_update):
            await asyncio.sleep(10)
            return AgentToolResult(content=[], details={})

        async def fast_execute(_id, _args, _token, _on_update):
            await asyncio.sleep(0.005)
            return AgentToolResult(
                content=[{"type": "text", "text": "fast-ok"}], details={}
            )

        slow_tool = AgentTool(
            name="slow",
            label="慢工具",
            description="会超时",
            execute=slow_execute,
            timeout_seconds=0.02,
        )
        fast_tool = AgentTool(
            name="fast",
            label="快工具",
            description="会成功",
            execute=fast_execute,
            timeout_seconds=0.1,
        )
        provider = self.provider_for_calls(
            [
                {
                    "type": "toolCall",
                    "id": "slow-id",
                    "name": "slow",
                    "arguments": {},
                },
                {
                    "type": "toolCall",
                    "id": "fast-id",
                    "name": "fast",
                    "arguments": {},
                },
            ]
        )
        agent = Agent(
            model=self.model,
            stream_fn=provider.stream,
            tools=[slow_tool, fast_tool],
            tool_execution="parallel",
        )

        await agent.prompt("并行执行")

        results = [
            message
            for message in agent.state.messages
            if message["role"] == "toolResult"
        ]
        # ToolResult 仍按模型调用来源顺序排列：slow 在前、fast 在后。
        self.assertEqual([item["toolName"] for item in results], ["slow", "fast"])
        self.assertTrue(results[0]["isError"])
        self.assertEqual(results[0]["details"]["code"], "tool_timeout")
        self.assertFalse(results[1]["isError"])
        self.assertEqual(results[1]["content"][0]["text"], "fast-ok")

    async def test_除法工具使用自己的独立_timeout(self) -> None:
        tool = create_divide_tool(delay_seconds=0.05)
        # 保留真实工具 execute，只把测试中的限制缩短，避免等待 3 秒。
        tool.timeout_seconds = 0.01
        provider = self.provider_for_calls(
            [
                {
                    "type": "toolCall",
                    "id": "divide-timeout",
                    "name": "divide",
                    "arguments": {"a": 10, "b": 2},
                }
            ]
        )
        agent = Agent(model=self.model, stream_fn=provider.stream, tools=[tool])

        await agent.prompt("测试除法工具超时")

        result = next(
            message
            for message in agent.state.messages
            if message["role"] == "toolResult"
        )
        self.assertTrue(result["isError"])
        self.assertEqual(result["details"]["code"], "tool_timeout")
        self.assertEqual(result["details"]["timeoutSeconds"], 0.01)

    async def test_Agent_默认超时用于没有独立配置的工具(self) -> None:
        async def execute(_id, _args, _token, _on_update):
            await asyncio.sleep(10)
            return AgentToolResult(content=[], details={})

        tool = AgentTool(
            name="default-timeout",
            label="默认超时工具",
            description="使用 Agent 默认值",
            execute=execute,
        )
        provider = self.provider_for_calls(
            [
                {
                    "type": "toolCall",
                    "id": "default-id",
                    "name": "default-timeout",
                    "arguments": {},
                }
            ]
        )
        agent = Agent(
            model=self.model,
            stream_fn=provider.stream,
            tools=[tool],
            default_tool_timeout_seconds=0.015,
        )

        await agent.prompt("测试默认超时")

        result = next(
            message
            for message in agent.state.messages
            if message["role"] == "toolResult"
        )
        self.assertEqual(result["details"]["code"], "tool_timeout")
        self.assertEqual(result["details"]["timeoutSeconds"], 0.015)

    async def test_用户取消会取消全部工具且忽略迟到_update(self) -> None:
        late_updates = 0
        tool_started = asyncio.Event()

        async def execute(_id, _args, _token, on_update):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                # 模拟一个不够规范的工具：取消后还试图报告迟到进度。
                on_update(
                    AgentToolResult(
                        content=[{"type": "text", "text": "迟到更新"}],
                        details={},
                    )
                )
                raise
            return AgentToolResult(content=[], details={})

        tool = AgentTool(
            name="cancel-me",
            label="取消测试工具",
            description="等待用户取消",
            execute=execute,
            timeout_seconds=5,
        )
        provider = self.provider_for_calls(
            [
                {
                    "type": "toolCall",
                    "id": "cancel-id",
                    "name": "cancel-me",
                    "arguments": {},
                }
            ]
        )
        agent = Agent(model=self.model, stream_fn=provider.stream, tools=[tool])

        def listener(event, _token):
            nonlocal late_updates
            if event["type"] == "tool_execution_start":
                tool_started.set()
            if (
                event["type"] == "tool_execution_update"
                and event["partialResult"]["content"][0]["text"] == "迟到更新"
            ):
                late_updates += 1

        agent.subscribe(listener)
        prompt_task = asyncio.create_task(agent.prompt("等待取消"))
        await asyncio.wait_for(tool_started.wait(), timeout=1)
        agent.abort("测试用户取消")
        await asyncio.wait_for(prompt_task, timeout=1)

        result = next(
            message
            for message in agent.state.messages
            if message["role"] == "toolResult"
        )
        self.assertTrue(result["isError"])
        self.assertEqual(result["details"]["code"], "tool_cancelled")
        self.assertEqual(late_updates, 0)
        self.assertFalse(agent.state.is_streaming)


if __name__ == "__main__":
    unittest.main()
