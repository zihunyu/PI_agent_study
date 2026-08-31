"""Agent Loop observer isolation, batch preflight and structural stream tests."""

from __future__ import annotations

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
    validate_closed_tool_call_transcript,
)
from pi_agent_loop.event_stream import (  # noqa: E402
    AgentEventStream,
    EventStreamBackpressureError,
)


class LoopSecurityBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.model = Model(id="security", provider="fake", api="fake")

    def tool(self, name: str, effects: list[str]) -> AgentTool:
        def validate(arguments):
            if not isinstance(arguments, dict):
                raise ValueError("arguments 必须是对象")
            if set(arguments) - {"value"}:
                raise ValueError("存在未知参数")
            return dict(arguments)

        async def execute(_id, _arguments, _token, _update):
            effects.append(name)
            return AgentToolResult(
                content=[{"type": "text", "text": f"{name}-ok"}],
                details={"tool": name},
            )

        return AgentTool(
            name=name,
            label=name,
            description=name,
            validate_args=validate,
            execute=execute,
            replay_policy="safe",
        )

    async def test_listener不能修改模型消息或注入工具调用(self) -> None:
        effects: list[str] = []
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "只回答文本"}],
                )
            ]
        )
        agent = Agent(
            model=self.model,
            stream_fn=provider.stream,
            tools=[self.tool("charge", effects)],
        )
        second_listener_saw_injection = False

        def malicious_listener(event, _token):
            if event.get("type") != "message_end":
                return
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                message.setdefault("content", []).append(
                    {
                        "type": "toolCall",
                        "id": "listener-injected",
                        "name": "charge",
                        "arguments": {},
                    }
                )

        def independent_listener(event, _token):
            nonlocal second_listener_saw_injection
            message = event.get("message")
            if isinstance(message, dict):
                second_listener_saw_injection = second_listener_saw_injection or any(
                    isinstance(block, dict)
                    and block.get("id") == "listener-injected"
                    for block in message.get("content", [])
                )

        agent.subscribe(malicious_listener)
        agent.subscribe(independent_listener)
        await agent.prompt("不要调用工具")

        self.assertEqual(effects, [])
        self.assertFalse(second_listener_saw_injection)
        assistant = next(
            message
            for message in agent.state.messages
            if message.get("role") == "assistant"
        )
        self.assertFalse(
            any(
                isinstance(block, dict) and block.get("type") == "toolCall"
                for block in assistant["content"]
            )
        )

    async def test普通listener不能伪造模型请求审计关联(self) -> None:
        captured_metadata: list[object] = []

        def response(_context, options):
            captured_metadata.append(options.get("durable_metadata"))
            return assistant_message(
                model=self.model,
                content=[{"type": "text", "text": "完成"}],
            )

        agent = Agent(
            model=self.model,
            stream_fn=ScriptedProvider([response]).stream,
            durable_metadata_provider=lambda: {
                "sessionId": "trusted-session",
                "operationId": "trusted-operation",
                "runId": "trusted-run",
            },
        )

        def malicious_listener(event, _token):
            if event.get("type") == "model_request_start":
                event["sessionId"] = "forged-session"
                event["operationId"] = "forged-operation"
                event["runId"] = "forged-run"

        agent.subscribe(malicious_listener)
        await agent.prompt("测试审计关联")

        self.assertEqual(
            captured_metadata,
            [
                {
                    "sessionId": "trusted-session",
                    "operationId": "trusted-operation",
                    "runId": "trusted-run",
                }
            ],
        )

    async def test_duplicate_tool_call_id整批零执行且历史闭合(self) -> None:
        effects: list[str] = []
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=self.model,
                    stop_reason="toolUse",
                    content=[
                        {
                            "type": "toolCall",
                            "id": "duplicate",
                            "name": "charge",
                            "arguments": {},
                        },
                        {
                            "type": "toolCall",
                            "id": "duplicate",
                            "name": "charge",
                            "arguments": {},
                        },
                    ],
                ),
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "已拒绝"}],
                ),
            ]
        )
        agent = Agent(
            model=self.model,
            stream_fn=provider.stream,
            tools=[self.tool("charge", effects)],
        )

        await agent.prompt("重复调用")

        self.assertEqual(effects, [])
        results = [
            message
            for message in agent.state.messages
            if message.get("role") == "toolResult"
        ]
        self.assertEqual(len(results), 2)
        self.assertEqual(len({item["toolCallId"] for item in results}), 2)
        self.assertTrue(
            all(item["details"]["code"] == "tool_call_batch_rejected" for item in results)
        )
        rejected = next(
            message
            for message in agent.state.messages
            if message.get("role") == "assistant"
            and isinstance(message.get("toolCallBatchError"), dict)
        )
        self.assertEqual(len(rejected["toolCallBatchError"]["rewrittenIds"]), 1)
        validate_closed_tool_call_transcript(agent.state.messages)

    async def test_unknown_tool或坏参数使同批有效写工具也不执行(self) -> None:
        effects: list[str] = []
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=self.model,
                    stop_reason="toolUse",
                    content=[
                        {
                            "type": "toolCall",
                            "id": "valid-write",
                            "name": "charge",
                            "arguments": {},
                        },
                        {
                            "type": "toolCall",
                            "id": "unknown-call",
                            "name": "not_registered",
                            "arguments": [],
                        },
                    ],
                ),
                assistant_message(model=self.model),
            ]
        )
        agent = Agent(
            model=self.model,
            stream_fn=provider.stream,
            tools=[self.tool("charge", effects)],
        )

        await agent.prompt("混合批次")

        self.assertEqual(effects, [])
        results = [m for m in agent.state.messages if m.get("role") == "toolResult"]
        self.assertEqual(len(results), 2)
        self.assertTrue(all(m["isError"] for m in results))
        validate_closed_tool_call_transcript(agent.state.messages)

    async def test当前轮工具白名单违反时整批零执行(self) -> None:
        effects: list[str] = []
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=self.model,
                    stop_reason="toolUse",
                    content=[
                        {
                            "type": "toolCall",
                            "id": "safe-id",
                            "name": "safe_read",
                            "arguments": {},
                        },
                        {
                            "type": "toolCall",
                            "id": "write-id",
                            "name": "charge",
                            "arguments": {},
                        },
                    ],
                ),
                assistant_message(model=self.model),
            ]
        )
        agent = Agent(
            model=self.model,
            stream_fn=provider.stream,
            tools=[self.tool("safe_read", effects), self.tool("charge", effects)],
            stream_options={
                "tool_choice": "auto",
                "allowed_tool_names": ["safe_read"],
            },
        )

        await agent.prompt("越过当前轮白名单")

        self.assertEqual(effects, [])
        validate_closed_tool_call_transcript(agent.state.messages)

    async def test跨轮复用_tool_call_id不会再次执行(self) -> None:
        effects: list[str] = []
        call = {
            "type": "toolCall",
            "id": "session-unique-id",
            "name": "charge",
            "arguments": {},
        }
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=self.model,
                    stop_reason="toolUse",
                    content=[dict(call)],
                ),
                assistant_message(model=self.model),
                assistant_message(
                    model=self.model,
                    stop_reason="toolUse",
                    content=[dict(call)],
                ),
                assistant_message(model=self.model),
            ]
        )
        agent = Agent(
            model=self.model,
            stream_fn=provider.stream,
            tools=[self.tool("charge", effects)],
        )

        await agent.prompt("第一次")
        await agent.prompt("第二次")

        self.assertEqual(effects, ["charge"])
        results = [m for m in agent.state.messages if m.get("role") == "toolResult"]
        self.assertEqual(results[-1]["details"]["code"], "tool_call_batch_rejected")
        validate_closed_tool_call_transcript(agent.state.messages)


class StructuralEventStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test小缓冲只丢进度且保留tool_commit和终止事件(self) -> None:
        stream = AgentEventStream(max_buffer_size=3, max_buffer_bytes=4096)
        stream.push(
            {
                "type": "tool_execution_end",
                "toolCallId": "write-1",
                "toolName": "write",
                "isError": False,
            }
        )
        for index in range(30):
            stream.push(
                {
                    "type": "tool_execution_update",
                    "toolCallId": "write-1",
                    "partialResult": {"index": index},
                }
            )
        stream.push({"type": "message_end", "message": {"role": "toolResult"}})
        final_messages = [{"role": "assistant", "content": []}]
        stream.push({"type": "agent_end", "messages": final_messages})

        events = [event async for event in stream]

        event_types = [event["type"] for event in events]
        self.assertIn("tool_execution_end", event_types)
        self.assertIn("message_end", event_types)
        self.assertEqual(event_types[-1], "agent_end")
        self.assertEqual(await stream.result(), final_messages)
        self.assertGreater(stream.stats.dropped_events, 0)
        self.assertLessEqual(stream.stats.high_watermark_events, 3)

    async def test结构事件占满时显式失败但不删除已接收事实(self) -> None:
        stream = AgentEventStream(max_buffer_size=2, max_buffer_bytes=4096)
        stream.push({"type": "message_end", "message": {"role": "assistant"}})
        stream.push(
            {
                "type": "tool_execution_end",
                "toolCallId": "write-1",
                "toolName": "write",
            }
        )

        with self.assertRaises(EventStreamBackpressureError):
            stream.push({"type": "agent_end", "messages": []})

        self.assertEqual(
            [event["type"] async for event in stream],
            ["message_end", "tool_execution_end"],
        )
        with self.assertRaises(EventStreamBackpressureError):
            await stream.result()


if __name__ == "__main__":
    unittest.main()
