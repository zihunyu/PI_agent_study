"""Agent Loop 核心行为测试。

使用 unittest.IsolatedAsyncioTestCase，因此无需安装 pytest。
"""

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
)


class AgentLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.model = Model(id="test-model", provider="fake", api="fake")

    async def test_纯文本响应会进入状态并产生完整生命周期(self) -> None:
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "你好"}],
                )
            ],
            chunk_size=1,
        )
        agent = Agent(model=self.model, stream_fn=provider.stream)
        events: list[str] = []
        agent.subscribe(lambda event, _token: events.append(event["type"]))

        await agent.prompt("问候")

        self.assertEqual(agent.state.messages[-1]["content"][0]["text"], "你好")
        self.assertEqual(events[0], "agent_start")
        self.assertIn("message_update", events)
        self.assertEqual(events[-1], "agent_end")
        self.assertFalse(agent.state.is_streaming)

    async def test_工具结果会回灌模型并触发下一轮(self) -> None:
        first = assistant_message(
            model=self.model,
            stop_reason="toolUse",
            content=[
                {
                    "type": "toolCall",
                    "id": "call-1",
                    "name": "double",
                    "arguments": {"value": 6},
                }
            ],
        )

        def second(context, _options):
            result = next(
                message
                for message in context["messages"]
                if message.get("role") == "toolResult"
            )
            return assistant_message(
                model=self.model,
                content=[
                    {
                        "type": "text",
                        "text": f"结果={result['content'][0]['text']}",
                    }
                ],
            )

        provider = ScriptedProvider([first, second])

        def validate(arguments):
            if not isinstance(arguments, dict) or not isinstance(
                arguments.get("value"), int
            ):
                raise ValueError("value 必须是整数")
            return arguments

        async def execute(_id, arguments, _token, _on_update):
            return AgentToolResult(
                content=[{"type": "text", "text": str(arguments["value"] * 2)}],
                details={},
            )

        tool = AgentTool(
            name="double",
            label="翻倍",
            description="把整数翻倍",
            execute=execute,
            validate_args=validate,
        )
        agent = Agent(model=self.model, stream_fn=provider.stream, tools=[tool])

        await agent.prompt("把 6 翻倍")

        self.assertEqual(provider.call_count, 2)
        roles = [message["role"] for message in agent.state.messages]
        self.assertEqual(roles, ["user", "assistant", "toolResult", "assistant"])
        self.assertEqual(agent.state.messages[-1]["content"][0]["text"], "结果=12")

    async def test_并行工具完成事件按完成顺序但结果按来源顺序(self) -> None:
        first = assistant_message(
            model=self.model,
            stop_reason="toolUse",
            content=[
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
            ],
        )
        provider = ScriptedProvider(
            [
                first,
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "完成"}],
                ),
            ]
        )

        def make_tool(name: str, delay: float) -> AgentTool:
            async def execute(_id, _arguments, _token, _on_update):
                await asyncio.sleep(delay)
                return AgentToolResult(
                    content=[{"type": "text", "text": name}], details={}
                )

            return AgentTool(
                name=name,
                label=name,
                description=name,
                execute=execute,
            )

        agent = Agent(
            model=self.model,
            stream_fn=provider.stream,
            tools=[make_tool("slow", 0.03), make_tool("fast", 0.005)],
        )
        completion_order: list[str] = []

        def listener(event, _token):
            if event["type"] == "tool_execution_end":
                completion_order.append(event["toolName"])

        agent.subscribe(listener)
        await agent.prompt("并行执行")

        self.assertEqual(completion_order, ["fast", "slow"])
        tool_results = [
            message
            for message in agent.state.messages
            if message["role"] == "toolResult"
        ]
        self.assertEqual(
            [message["toolName"] for message in tool_results], ["slow", "fast"]
        )

    async def test_length_截断的工具参数不会执行(self) -> None:
        truncated = assistant_message(
            model=self.model,
            stop_reason="length",
            content=[
                {
                    "type": "toolCall",
                    "id": "danger-id",
                    "name": "danger",
                    "arguments": {"path": "/important"},
                }
            ],
        )
        provider = ScriptedProvider(
            [
                truncated,
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "已重新处理"}],
                ),
            ]
        )
        executed = False

        async def execute(_id, _arguments, _token, _on_update):
            nonlocal executed
            executed = True
            return AgentToolResult(content=[], details={})

        agent = Agent(
            model=self.model,
            stream_fn=provider.stream,
            tools=[
                AgentTool(
                    name="danger",
                    label="危险工具",
                    description="测试工具",
                    execute=execute,
                )
            ],
        )

        await agent.prompt("执行危险工具")

        self.assertFalse(executed)
        result = next(
            message
            for message in agent.state.messages
            if message["role"] == "toolResult"
        )
        self.assertTrue(result["isError"])
        self.assertIn("参数可能被截断", result["content"][0]["text"])

    async def test_follow_up_只在原响应结束后处理(self) -> None:
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "第一项完成"}],
                ),
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "后续项完成"}],
                ),
            ]
        )
        agent = Agent(model=self.model, stream_fn=provider.stream)
        agent.follow_up("再做后续项")

        await agent.prompt("先做第一项")

        self.assertEqual(provider.call_count, 2)
        user_texts = [
            message["content"][0]["text"]
            for message in agent.state.messages
            if message["role"] == "user"
        ]
        self.assertEqual(user_texts, ["先做第一项", "再做后续项"])

    async def test_agent_end_listener_结束前_agent_仍不空闲(self) -> None:
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "完成"}],
                )
            ]
        )
        agent = Agent(model=self.model, stream_fn=provider.stream)
        listener_finished = False
        agent_was_busy_inside_listener = False

        async def listener(event, _token):
            nonlocal listener_finished, agent_was_busy_inside_listener
            if event["type"] == "agent_end":
                agent_was_busy_inside_listener = agent.state.is_streaming
                await asyncio.sleep(0.02)
                listener_finished = True

        agent.subscribe(listener)
        await agent.prompt("测试结算")

        # 直接验证语义，不使用容易受 Windows timer 精度影响的耗时阈值。
        self.assertTrue(agent_was_busy_inside_listener)
        self.assertTrue(listener_finished)
        await agent.wait_for_idle()


if __name__ == "__main__":
    unittest.main()
