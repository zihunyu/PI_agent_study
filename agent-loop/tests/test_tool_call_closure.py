"""Tool Call/Tool Result 协议闭合与取消修复测试。"""

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
    DurableOperationRecorder,
    InMemoryOperationEventStore,
    Model,
    ScriptedProvider,
    TranscriptIntegrityError,
    analyze_tool_call_transcript,
    assistant_message,
    replay_operation,
    validate_closed_tool_call_transcript,
)
from pi_agent_loop.providers import (  # noqa: E402
    ProviderProfile,
    ProviderProtocolError,
    serialize_chat_request,
)


class ToolCallClosureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.model = Model(id="closure", provider="fake", api="fake")

    def provider(self, names: list[str], *, stop_reason="toolUse") -> ScriptedProvider:
        return ScriptedProvider([
            assistant_message(
                model=self.model,
                stop_reason=stop_reason,
                content=[
                    {
                        "type": "toolCall",
                        "id": name,
                        "name": name,
                        "arguments": {},
                    }
                    for name in names
                ],
            ),
            assistant_message(
                model=self.model,
                content=[{"type": "text", "text": "后续回答"}],
            ),
        ])

    @staticmethod
    def tool(name: str, execute) -> AgentTool:
        return AgentTool(
            name=name,
            label=name,
            description=name,
            execute=execute,
            execution_mode="parallel",
            replay_policy="safe",
        )

    async def test_sequential_执行首个工具时取消仍补齐全部_result(self) -> None:
        started = asyncio.Event()

        async def execute(_id, _args, _token, _update):
            started.set()
            await asyncio.sleep(10)
            return AgentToolResult(content=[], details={})

        tools = [self.tool(name, execute) for name in ("a", "b", "c")]
        agent = Agent(
            model=self.model,
            stream_fn=self.provider(["a", "b", "c"]).stream,
            tools=tools,
            tool_execution="sequential",
        )
        task = asyncio.create_task(agent.prompt("取消串行批次"))
        await asyncio.wait_for(started.wait(), timeout=1)
        agent.abort("用户取消")
        await task

        results = [m for m in agent.state.messages if m["role"] == "toolResult"]
        self.assertEqual([m["toolCallId"] for m in results], ["a", "b", "c"])
        self.assertEqual(
            [m["details"]["code"] for m in results[1:]],
            ["tool_aborted_before_dispatch", "tool_aborted_before_dispatch"],
        )
        validate_closed_tool_call_transcript(agent.state.messages)

    async def test_第一个工具_preflight_前取消会补齐全部_result(self) -> None:
        async def execute(_id, _args, _token, _update):
            raise AssertionError("工具不应 Dispatch")

        tools = [self.tool(name, execute) for name in ("a", "b", "c")]
        agent = Agent(
            model=self.model,
            stream_fn=self.provider(["a", "b", "c"]).stream,
            tools=tools,
            tool_execution="sequential",
        )
        cancelled = False

        def listener(event, token):
            nonlocal cancelled
            if event["type"] == "tool_execution_start" and not cancelled:
                cancelled = True
                token.cancel("Preflight 前取消")

        agent.subscribe(listener)
        await agent.prompt("取消全部")

        results = [m for m in agent.state.messages if m["role"] == "toolResult"]
        self.assertEqual(len(results), 3)
        self.assertTrue(all(m["isError"] for m in results))
        validate_closed_tool_call_transcript(agent.state.messages)

    async def test_parallel_preflight_取消仍为全部调用生成结果(self) -> None:
        async def execute(_id, _args, _token, _update):
            raise AssertionError("工具不应 Dispatch")

        agent = Agent(
            model=self.model,
            stream_fn=self.provider(["a", "b", "c"]).stream,
            tools=[self.tool(name, execute) for name in ("a", "b", "c")],
        )
        cancelled = False

        def listener(event, token):
            nonlocal cancelled
            if event["type"] == "tool_execution_start" and not cancelled:
                cancelled = True
                token.cancel("并行 Preflight 取消")

        agent.subscribe(listener)
        await agent.prompt("取消 Parallel")

        results = [m for m in agent.state.messages if m["role"] == "toolResult"]
        self.assertEqual([m["toolCallId"] for m in results], ["a", "b", "c"])
        validate_closed_tool_call_transcript(agent.state.messages)

    async def test_exclusive_barrier_取消后续调用仍闭合(self) -> None:
        started = asyncio.Event()

        async def execute(_id, _args, _token, _update):
            started.set()
            await asyncio.sleep(10)
            return AgentToolResult(content=[], details={})

        parallel = self.tool("a", execute)
        exclusive = self.tool("b", execute)
        exclusive.execution_mode = "exclusive"
        trailing = self.tool("c", execute)
        agent = Agent(
            model=self.model,
            stream_fn=self.provider(["a", "b", "c"]).stream,
            tools=[parallel, exclusive, trailing],
        )
        task = asyncio.create_task(agent.prompt("Barrier 取消"))
        await asyncio.wait_for(started.wait(), timeout=1)
        agent.abort("停止 Barrier")
        await task

        results = [m for m in agent.state.messages if m["role"] == "toolResult"]
        self.assertEqual([m["toolCallId"] for m in results], ["a", "b", "c"])
        validate_closed_tool_call_transcript(agent.state.messages)

    async def test_listener_异常后_agent_state_自动补齐未闭合调用(self) -> None:
        slow_started = asyncio.Event()

        async def fast(_id, _args, _token, _update):
            await slow_started.wait()
            return AgentToolResult(content=[], details={})

        async def slow(_id, _args, _token, _update):
            slow_started.set()
            await asyncio.sleep(0.01)
            return AgentToolResult(content=[], details={})

        agent = Agent(
            model=self.model,
            stream_fn=self.provider(["fast", "slow"]).stream,
            tools=[self.tool("fast", fast), self.tool("slow", slow)],
        )
        raised = False

        def listener(event, _token):
            nonlocal raised
            if event["type"] == "tool_execution_end" and not raised:
                raised = True
                raise RuntimeError("listener-failed")

        agent.subscribe(listener)
        await agent.prompt("Listener 失败")

        results = [m for m in agent.state.messages if m["role"] == "toolResult"]
        self.assertEqual([m["toolCallId"] for m in results], ["fast", "slow"])
        validate_closed_tool_call_transcript(agent.state.messages)

    async def test_error_assistant_中的_tool_call_会被移除(self) -> None:
        agent = Agent(
            model=self.model,
            stream_fn=self.provider(["danger"], stop_reason="error").stream,
            tools=[],
        )
        await agent.prompt("模型错误")

        assistant = next(m for m in agent.state.messages if m["role"] == "assistant")
        self.assertEqual(assistant["stopReason"], "error")
        self.assertFalse(
            any(block.get("type") == "toolCall" for block in assistant["content"])
        )
        validate_closed_tool_call_transcript(agent.state.messages)

    async def test_duplicate_和_orphan_tool_result_会被拒绝(self) -> None:
        orphan = {
            "role": "toolResult",
            "toolCallId": "missing",
            "toolName": "x",
            "content": [],
        }
        with self.assertRaisesRegex(TranscriptIntegrityError, "没有对应"):
            analyze_tool_call_transcript([orphan])

        assistant = assistant_message(
            model=self.model,
            stop_reason="toolUse",
            content=[{
                "type": "toolCall",
                "id": "one",
                "name": "x",
                "arguments": {},
            }],
        )
        result = {
            "role": "toolResult",
            "toolCallId": "one",
            "toolName": "x",
            "content": [],
        }
        with self.assertRaises(TranscriptIntegrityError):
            analyze_tool_call_transcript([assistant, result, result])

    async def test_openai_serializer_拒绝未闭合历史(self) -> None:
        profile = ProviderProfile(
            name="test",
            protocol="openai_chat_completions",
            base_url="https://example.com/v1",
            endpoint="/chat/completions",
            auth_type="bearer",
            api_key="hidden",
            model="model",
            stream=True,
            connect_timeout_seconds=1,
            request_timeout_seconds=10,
            allow_insecure_http=False,
        )
        assistant = assistant_message(
            model=self.model,
            stop_reason="toolUse",
            content=[{
                "type": "toolCall",
                "id": "missing-result",
                "name": "x",
                "arguments": {},
            }],
        )
        with self.assertRaisesRegex(ProviderProtocolError, "缺少 Tool Result"):
            serialize_chat_request(
                profile,
                {"systemPrompt": "", "messages": [assistant], "tools": []},
            )

    async def test_durable_cancelled_operation_没有_unresolved_tool_call(self) -> None:
        started = asyncio.Event()

        async def execute(_id, _args, _token, _update):
            started.set()
            await asyncio.sleep(10)
            return AgentToolResult(content=[], details={})

        tools = [self.tool(name, execute) for name in ("a", "b", "c")]
        store = InMemoryOperationEventStore()
        recorder = DurableOperationRecorder(
            store,
            session_id="closure-session",
            tools=tools,
        )
        agent = Agent(
            model=self.model,
            stream_fn=self.provider(["a", "b", "c"]).stream,
            tools=tools,
            tool_execution="sequential",
        )
        agent.subscribe(recorder.listener)
        task = asyncio.create_task(agent.prompt("持久取消"))
        await asyncio.wait_for(started.wait(), timeout=1)
        agent.abort("取消")
        await task

        state = replay_operation(
            await store.load(operation_id=recorder.last_operation_id)
        )
        self.assertEqual(state.phase, "cancelled")
        validate_closed_tool_call_transcript(list(state.messages))

    async def test_下一次_prompt_前自动修复旧的未闭合历史(self) -> None:
        old_assistant = assistant_message(
            model=self.model,
            stop_reason="toolUse",
            content=[{
                "type": "toolCall",
                "id": "old-call",
                "name": "old-tool",
                "arguments": {},
            }],
        )
        provider = ScriptedProvider([
            assistant_message(
                model=self.model,
                content=[{"type": "text", "text": "新回答"}],
            )
        ])
        agent = Agent(
            model=self.model,
            stream_fn=provider.stream,
            messages=[old_assistant],
        )

        await agent.prompt("新问题")

        roles = [message["role"] for message in agent.state.messages]
        self.assertEqual(roles[:3], ["assistant", "toolResult", "user"])
        self.assertEqual(
            agent.state.messages[1]["details"]["code"],
            "tool_result_missing_repaired",
        )
        validate_closed_tool_call_transcript(agent.state.messages)


if __name__ == "__main__":
    unittest.main()
