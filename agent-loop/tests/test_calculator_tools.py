"""加法、乘法工具和注册表测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import (  # noqa: E402
    Agent,
    CancellationToken,
    Model,
    ScriptedProvider,
    ToolRegistry,
    assistant_message,
    create_add_tool,
    create_calculator_registry,
    create_divide_tool,
    create_multiply_tool,
)


class CalculatorToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_加法工具返回正确结果(self) -> None:
        tool = create_add_tool()
        updates = []
        self.assertEqual(tool.timeout_seconds, 2)

        result = await tool.execute(
            "add-id",
            tool.validate_args({"a": 2, "b": 3}),
            CancellationToken(),
            updates.append,
        )

        self.assertEqual(result.content[0]["text"], "5")
        self.assertEqual(result.details["operation"], "add")
        self.assertEqual(result.details["value"], 5)
        self.assertEqual(len(updates), 1)

    async def test_乘法工具返回正确结果(self) -> None:
        tool = create_multiply_tool()
        updates = []
        self.assertEqual(tool.timeout_seconds, 5)

        result = await tool.execute(
            "multiply-id",
            tool.validate_args({"a": 4, "b": 5}),
            CancellationToken(),
            updates.append,
        )

        self.assertEqual(result.content[0]["text"], "20")
        self.assertEqual(result.details["operation"], "multiply")
        self.assertEqual(result.details["value"], 20)
        self.assertEqual(len(updates), 1)

    async def test_除法工具返回正确结果(self) -> None:
        tool = create_divide_tool()
        updates = []
        self.assertEqual(tool.timeout_seconds, 3)

        result = await tool.execute(
            "divide-id",
            tool.validate_args({"a": 10, "b": 4}),
            CancellationToken(),
            updates.append,
        )

        self.assertEqual(result.content[0]["text"], "2.5")
        self.assertEqual(result.details["operation"], "divide")
        self.assertEqual(result.details["value"], 2.5)
        self.assertEqual(len(updates), 1)

    async def test_除法工具拒绝除数为零(self) -> None:
        tool = create_divide_tool()

        with self.assertRaisesRegex(ValueError, "除数 b 不能为 0"):
            tool.validate_args({"a": 10, "b": 0})

        with self.assertRaisesRegex(ValueError, "除数 b 不能为 0"):
            tool.validate_args({"a": 10, "b": -0.0})

    async def test_参数校验拒绝缺少字段和布尔值(self) -> None:
        tool = create_add_tool()

        with self.assertRaisesRegex(ValueError, "同时包含 a 和 b"):
            tool.validate_args({"a": 2})

        with self.assertRaisesRegex(ValueError, "必须是有限数字"):
            tool.validate_args({"a": True, "b": 3})

    async def test_注册表拒绝重名工具(self) -> None:
        registry = ToolRegistry()
        registry.register(create_add_tool())

        with self.assertRaisesRegex(ValueError, "工具已经注册：add"):
            registry.register(create_add_tool())

    async def test_注册工具后_agent_loop_会执行真实工具函数(self) -> None:
        model = Model(id="test", provider="fake", api="fake")
        first = assistant_message(
            model=model,
            stop_reason="toolUse",
            content=[
                {
                    "type": "toolCall",
                    "id": "add-call",
                    "name": "add",
                    "arguments": {"a": 2, "b": 3},
                },
                {
                    "type": "toolCall",
                    "id": "multiply-call",
                    "name": "multiply",
                    "arguments": {"a": 4, "b": 5},
                },
                {
                    "type": "toolCall",
                    "id": "divide-call",
                    "name": "divide",
                    "arguments": {"a": 10, "b": 4},
                },
            ],
        )

        def final_response(context, _options):
            values = [
                message["content"][0]["text"]
                for message in context["messages"]
                if message.get("role") == "toolResult"
            ]
            return assistant_message(
                model=model,
                content=[
                    {
                        "type": "text",
                        "text": (
                            f"加法={values[0]}，乘法={values[1]}，"
                            f"除法={values[2]}"
                        ),
                    }
                ],
            )

        provider = ScriptedProvider([first, final_response])
        registry = create_calculator_registry()
        agent = Agent(
            model=model,
            stream_fn=provider.stream,
            tools=registry.all(),
        )

        await agent.prompt("同时计算 2+3、4×5 和 10÷4")

        self.assertEqual(registry.names(), ["add", "multiply", "divide"])
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(
            agent.state.messages[-1]["content"][0]["text"],
            "加法=5，乘法=20，除法=2.5",
        )


if __name__ == "__main__":
    unittest.main()
