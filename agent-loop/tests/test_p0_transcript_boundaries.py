"""Transcript Commit Boundary 与完整 Durable Context 回归测试。"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import (  # noqa: E402
    Agent,
    AgentContext,
    AgentLoopConfig,
    AgentTool,
    AgentToolResult,
    CancellationToken,
    CapabilityRegistry,
    DurableOperationRecorder,
    InMemoryOperationEventStore,
    Model,
    RequestDecision,
    RoutedAgent,
    ScriptedProvider,
    assistant_message,
    replay_operation,
    run_agent_loop,
    run_agent_loop_continue,
    user_message,
    validate_closed_tool_call_transcript,
)


class FixedRouter:
    def __init__(self, decision: RequestDecision) -> None:
        self.decision = decision

    def route(self, _text: str) -> RequestDecision:
        return self.decision


class P0TranscriptBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.model = Model(id="p0-transcript", provider="fake", api="fake")

    def tool(self, name: str, execute) -> AgentTool:
        return AgentTool(
            name=name,
            label=name,
            description=name,
            execute=execute,
            execution_mode="exclusive",
            replay_policy="never",
        )

    def tool_call_response(self, call_id: str, tool_name: str) -> dict:
        return assistant_message(
            model=self.model,
            stop_reason="toolUse",
            content=[
                {
                    "type": "toolCall",
                    "id": call_id,
                    "name": tool_name,
                    "arguments": {},
                }
            ],
        )

    async def test_external_cancel_at_tool_end_commits_real_result_first(self) -> None:
        side_effects: list[str] = []
        observer_entered = asyncio.Event()

        async def execute(_id, _args, _token, _update):
            side_effects.append("committed")
            return AgentToolResult(
                content=[{"type": "text", "text": "真实成功"}],
                details={"status": "succeeded"},
            )

        async def blocking_observer(event, _token):
            if event.get("type") == "tool_execution_end":
                observer_entered.set()
                await asyncio.sleep(10)

        tool = self.tool("ship_order", execute)
        provider = ScriptedProvider(
            [self.tool_call_response("ship-call", "ship_order")]
        )
        store = InMemoryOperationEventStore()
        recorder = DurableOperationRecorder(
            store,
            session_id="commit-cancel",
            tools=[tool],
        )
        agent = Agent(
            model=self.model,
            stream_fn=provider.stream,
            tools=[tool],
            tool_execution="sequential",
        )
        # 故意让可取消 Observer 排在 Durable Recorder 前面。
        agent.subscribe(blocking_observer)
        agent.subscribe(recorder.listener)

        prompt_task = asyncio.create_task(agent.prompt("发货"))
        await asyncio.wait_for(observer_entered.wait(), timeout=1)
        prompt_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await prompt_task

        operation = replay_operation(
            await store.load(operation_id=recorder.last_operation_id)
        )
        results = [
            message
            for message in operation.messages
            if message.get("role") == "toolResult"
        ]
        self.assertEqual(side_effects, ["committed"])
        self.assertEqual(operation.phase, "cancelled")
        self.assertEqual(operation.tools["ship-call"].phase, "completed")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["details"], {"status": "succeeded"})
        self.assertNotEqual(
            results[0]["details"].get("code"),
            "tool_not_executed_due_run_error",
        )
        validate_closed_tool_call_transcript(agent.state.messages)
        validate_closed_tool_call_transcript(list(operation.messages))

    async def test_low_level_continue_exposes_repair_and_mutates_context(self) -> None:
        unresolved = self.tool_call_response("old-call", "old-tool")
        context = AgentContext(
            system_prompt="",
            messages=[unresolved, user_message("继续")],
            tools=[],
        )
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "已继续"}],
                )
            ]
        )
        events: list[dict] = []
        config = AgentLoopConfig(
            model=self.model,
            convert_to_llm=lambda messages: messages,
        )

        new_messages = await run_agent_loop_continue(
            context,
            config,
            events.append,
            CancellationToken(),
            provider.stream,
        )

        self.assertEqual(
            [message["role"] for message in context.messages],
            ["assistant", "toolResult", "user"],
        )
        repairs = [
            event for event in events if event.get("type") == "transcript_repaired"
        ]
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0]["contextMessages"], context.messages)
        self.assertEqual(provider.contexts[0]["messages"], context.messages)
        validate_closed_tool_call_transcript(context.messages + new_messages)

    async def test_low_level_prompt_exposes_repair_before_new_prompt(self) -> None:
        unresolved = self.tool_call_response("old-prompt-call", "old-tool")
        context = AgentContext(
            system_prompt="",
            messages=[unresolved],
            tools=[],
        )
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "新回答"}],
                )
            ]
        )
        events: list[dict] = []
        prompt = user_message("新问题")

        new_messages = await run_agent_loop(
            [prompt],
            context,
            AgentLoopConfig(
                model=self.model,
                convert_to_llm=lambda messages: messages,
            ),
            events.append,
            CancellationToken(),
            provider.stream,
        )

        self.assertEqual(
            [message["role"] for message in context.messages],
            ["assistant", "toolResult"],
        )
        self.assertEqual(
            provider.contexts[0]["messages"],
            context.messages + [prompt],
        )
        self.assertEqual(
            len(
                [
                    event
                    for event in events
                    if event.get("type") == "transcript_repaired"
                ]
            ),
            1,
        )
        validate_closed_tool_call_transcript(context.messages + new_messages)

    async def test_agent_operation_matches_provider_context_without_duplicates(self) -> None:
        history = [
            user_message("旧问题"),
            assistant_message(
                model=self.model,
                content=[{"type": "text", "text": "旧回答"}],
            ),
        ]
        final = assistant_message(
            model=self.model,
            content=[{"type": "text", "text": "新回答"}],
        )
        provider = ScriptedProvider([final])
        store = InMemoryOperationEventStore()
        recorder = DurableOperationRecorder(store, session_id="full-context", tools=[])
        agent = Agent(
            model=self.model,
            stream_fn=provider.stream,
            messages=history,
        )
        agent.subscribe(recorder.listener)

        await agent.prompt("新问题")

        operation = replay_operation(
            await store.load(operation_id=recorder.last_operation_id)
        )
        provider_context = provider.contexts[0]["messages"]
        self.assertEqual(list(operation.messages[:-1]), provider_context)
        self.assertEqual(operation.messages[-1], final)
        self.assertEqual(len(operation.messages), len(provider_context) + 1)

    async def test_routed_operation_uses_repaired_full_context_once(self) -> None:
        unresolved = self.tool_call_response("old-call", "old-tool")
        final = assistant_message(
            model=self.model,
            content=[{"type": "text", "text": "路由回答"}],
        )
        provider = ScriptedProvider([final])
        store = InMemoryOperationEventStore()
        recorder = DurableOperationRecorder(
            store,
            session_id="routed-full-context",
            tools=[],
        )
        agent = Agent(
            model=self.model,
            stream_fn=provider.stream,
            messages=[unresolved],
        )
        routed = RoutedAgent(
            agent,
            FixedRouter(
                RequestDecision(
                    status="in_scope_no_tool",
                    reason="允许普通回答",
                    message="",
                )
            ),
            CapabilityRegistry(),
            operation_recorder=recorder,
        )

        await routed.prompt("新问题")

        operation = replay_operation(
            await store.load(operation_id=recorder.last_operation_id)
        )
        provider_context = provider.contexts[0]["messages"]
        self.assertEqual(
            [message["role"] for message in provider_context],
            ["assistant", "toolResult", "user"],
        )
        self.assertEqual(list(operation.messages[:-1]), provider_context)
        self.assertEqual(operation.messages[-1], final)
        self.assertEqual(len(operation.messages), len(provider_context) + 1)
        validate_closed_tool_call_transcript(list(operation.messages))


if __name__ == "__main__":
    unittest.main()
