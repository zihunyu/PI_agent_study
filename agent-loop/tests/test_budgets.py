"""最大 Turn、Tool Call 和并行工具数测试。"""

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
    RetryableToolError,
    ScriptedProvider,
    ToolRetryPolicy,
    assistant_message,
)


class AgentBudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.model = Model(id="budget-model", provider="fake", api="fake")

    def tool_call(self, call_id: str, name: str = "work") -> dict:
        return {
            "type": "toolCall",
            "id": call_id,
            "name": name,
            "arguments": {},
        }

    def tool_response(self, calls: list[dict]) -> dict:
        return assistant_message(
            model=self.model,
            stop_reason="toolUse",
            content=calls,
        )

    def final_response(self, text: str = "完成") -> dict:
        return assistant_message(
            model=self.model,
            content=[{"type": "text", "text": text}],
        )

    def make_tool(
        self,
        execute,
        *,
        retry_policy: ToolRetryPolicy | None = None,
    ) -> AgentTool:
        return AgentTool(
            name="work",
            label="工作工具",
            description="预算测试工具",
            execute=execute,
            retry_policy=retry_policy,
        )

    def retry_policy(self) -> ToolRetryPolicy:
        return ToolRetryPolicy(
            max_retries=1,
            retryable_codes=frozenset({"upstream_unavailable"}),
            idempotent=True,
            initial_delay_seconds=0,
            max_delay_seconds=1,
            jitter_ratio=0,
        )

    async def test_预算内两轮两个工具正常完成(self) -> None:
        executed = 0

        async def execute(_id, _args, _token, _on_update):
            nonlocal executed
            executed += 1
            return AgentToolResult(
                content=[{"type": "text", "text": "ok"}], details={}
            )

        provider = ScriptedProvider(
            [
                self.tool_response(
                    [self.tool_call("one"), self.tool_call("two")]
                ),
                self.final_response(),
            ]
        )
        agent = Agent(
            model=self.model,
            stream_fn=provider.stream,
            tools=[self.make_tool(execute)],
            max_turns=2,
            max_tool_calls=2,
            max_parallel_tools=2,
        )

        await agent.prompt("预算内运行")

        self.assertEqual(provider.call_count, 2)
        self.assertEqual(executed, 2)
        self.assertEqual(agent.state.messages[-1]["role"], "assistant")
        self.assertIsNone(agent.state.error_message)

    async def test_max_turns_为1时禁止第二次模型请求(self) -> None:
        async def execute(_id, _args, _token, _on_update):
            return AgentToolResult(
                content=[{"type": "text", "text": "工具完成"}], details={}
            )

        provider = ScriptedProvider(
            [self.tool_response([self.tool_call("one")]), self.final_response()]
        )
        agent = Agent(
            model=self.model,
            stream_fn=provider.stream,
            tools=[self.make_tool(execute)],
            max_turns=1,
            max_tool_calls=10,
            max_parallel_tools=5,
        )
        budget_events: list[dict] = []
        agent.subscribe(
            lambda event, _token: budget_events.append(event)
            if event["type"] == "budget_exceeded"
            else None
        )

        await agent.prompt("只能一轮")

        self.assertEqual(provider.call_count, 1)
        self.assertEqual(len(budget_events), 1)
        self.assertEqual(budget_events[0]["budget"], "turns")
        self.assertEqual(budget_events[0]["limit"], 1)
        self.assertEqual(agent.state.messages[-1]["role"], "toolResult")

    async def test_tool_call_总预算不足时整批不执行(self) -> None:
        executed = 0

        async def execute(_id, _args, _token, _on_update):
            nonlocal executed
            executed += 1
            return AgentToolResult(content=[], details={})

        provider = ScriptedProvider(
            [
                self.tool_response(
                    [self.tool_call("one"), self.tool_call("two")]
                )
            ]
        )
        agent = Agent(
            model=self.model,
            stream_fn=provider.stream,
            tools=[self.make_tool(execute)],
            max_turns=20,
            max_tool_calls=1,
            max_parallel_tools=5,
        )
        budget_events: list[dict] = []
        agent.subscribe(
            lambda event, _token: budget_events.append(event)
            if event["type"] == "budget_exceeded"
            else None
        )

        await agent.prompt("请求两个工具")

        self.assertEqual(executed, 0)
        self.assertEqual(provider.call_count, 1)
        results = [
            message
            for message in agent.state.messages
            if message["role"] == "toolResult"
        ]
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result["isError"] for result in results))
        self.assertTrue(
            all(
                result["details"]["code"] == "tool_call_budget_exceeded"
                for result in results
            )
        )
        self.assertEqual(budget_events[0]["requested"], 2)
        self.assertEqual(budget_events[0]["remaining"], 1)

    async def test_tool_call_预算跨多个_turn_累计(self) -> None:
        executed = 0

        async def execute(_id, _args, _token, _on_update):
            nonlocal executed
            executed += 1
            return AgentToolResult(
                content=[{"type": "text", "text": "ok"}], details={}
            )

        provider = ScriptedProvider(
            [
                self.tool_response([self.tool_call("turn-1")]),
                self.tool_response([self.tool_call("turn-2")]),
            ]
        )
        agent = Agent(
            model=self.model,
            stream_fn=provider.stream,
            tools=[self.make_tool(execute)],
            max_turns=20,
            max_tool_calls=1,
            max_parallel_tools=5,
        )

        await agent.prompt("跨轮累计")

        self.assertEqual(provider.call_count, 2)
        self.assertEqual(executed, 1)
        results = [
            message
            for message in agent.state.messages
            if message["role"] == "toolResult"
        ]
        self.assertEqual(len(results), 2)
        self.assertFalse(results[0]["isError"])
        self.assertEqual(
            results[1]["details"]["code"], "tool_call_budget_exceeded"
        )

    async def test_max_parallel_tools_限制同时执行数量(self) -> None:
        active = 0
        observed_max = 0
        executed = 0

        async def execute(_id, _args, _token, _on_update):
            nonlocal active, observed_max, executed
            active += 1
            observed_max = max(observed_max, active)
            try:
                await asyncio.sleep(0.01)
                executed += 1
                return AgentToolResult(
                    content=[{"type": "text", "text": "ok"}], details={}
                )
            finally:
                active -= 1

        calls = [self.tool_call(f"call-{index}") for index in range(8)]
        provider = ScriptedProvider(
            [self.tool_response(calls), self.final_response()]
        )
        agent = Agent(
            model=self.model,
            stream_fn=provider.stream,
            tools=[self.make_tool(execute)],
            max_turns=20,
            max_tool_calls=10,
            max_parallel_tools=2,
        )

        await agent.prompt("八个工具，最多并行两个")

        self.assertEqual(executed, 8)
        self.assertEqual(observed_max, 2)
        self.assertEqual(provider.call_count, 2)

    async def test_未知工具也会消耗_tool_call_预算(self) -> None:
        provider = ScriptedProvider(
            [
                self.tool_response([self.tool_call("ghost-1", "ghost")]),
                self.tool_response([self.tool_call("ghost-2", "ghost")]),
            ]
        )
        agent = Agent(
            model=self.model,
            stream_fn=provider.stream,
            tools=[],
            max_turns=20,
            max_tool_calls=1,
            max_parallel_tools=5,
        )

        await agent.prompt("重复未知工具")

        self.assertEqual(provider.call_count, 2)
        results = [
            message
            for message in agent.state.messages
            if message["role"] == "toolResult"
        ]
        self.assertEqual(len(results), 2)
        self.assertIn("工具不存在", results[0]["content"][0]["text"])
        self.assertEqual(
            results[1]["details"]["code"], "tool_call_budget_exceeded"
        )

    async def test_retry_真实_attempt_超过预算时不再次进入_handler(self) -> None:
        executed = 0

        async def execute(_id, _args, _token, _on_update):
            nonlocal executed
            executed += 1
            raise RetryableToolError(
                "上游暂时不可用",
                code="upstream_unavailable",
            )

        provider = ScriptedProvider(
            [self.tool_response([self.tool_call("retry-one")]), self.final_response()]
        )
        agent = Agent(
            model=self.model,
            stream_fn=provider.stream,
            tools=[self.make_tool(execute, retry_policy=self.retry_policy())],
            max_turns=20,
            max_tool_calls=1,
            max_parallel_tools=1,
        )
        budget_events: list[dict] = []
        agent.subscribe(
            lambda event, _token: budget_events.append(event)
            if event["type"] == "budget_exceeded"
            else None
        )

        await agent.prompt("重试不能突破真实调用预算")

        self.assertEqual(executed, 1)
        self.assertEqual(provider.call_count, 1)
        result = agent.state.messages[-1]
        self.assertEqual(result["role"], "toolResult")
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["details"]["code"], "tool_call_budget_exceeded"
        )
        self.assertEqual(result["details"]["attempt"], 2)
        self.assertEqual(len(budget_events), 1)
        self.assertEqual(budget_events[0]["budget"], "tool_calls")
        self.assertEqual(budget_events[0]["used"], 1)

    async def test_retry_有剩余预算时允许第二次_handler并继续模型(self) -> None:
        executed = 0

        async def execute(_id, _args, _token, _on_update):
            nonlocal executed
            executed += 1
            if executed == 1:
                raise RetryableToolError(
                    "上游暂时不可用",
                    code="upstream_unavailable",
                )
            return AgentToolResult(
                content=[{"type": "text", "text": "retry-ok"}], details={}
            )

        provider = ScriptedProvider(
            [self.tool_response([self.tool_call("retry-two")]), self.final_response()]
        )
        agent = Agent(
            model=self.model,
            stream_fn=provider.stream,
            tools=[self.make_tool(execute, retry_policy=self.retry_policy())],
            max_turns=20,
            max_tool_calls=2,
            max_parallel_tools=1,
        )

        await agent.prompt("预算允许一次重试")

        self.assertEqual(executed, 2)
        self.assertEqual(provider.call_count, 2)
        results = [
            message
            for message in agent.state.messages
            if message["role"] == "toolResult"
        ]
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["isError"])

    async def test_并发_retry_竞争最后一个预算名额时只有一个进入_handler(
        self,
    ) -> None:
        first_attempts_ready = asyncio.Event()
        release_first_attempts = asyncio.Event()
        attempts_by_call: dict[str, int] = {}
        attempts_guard = asyncio.Lock()

        async def execute(call_id, _args, _token, _on_update):
            async with attempts_guard:
                attempt = attempts_by_call.get(call_id, 0) + 1
                attempts_by_call[call_id] = attempt
                if sum(attempts_by_call.values()) == 2:
                    first_attempts_ready.set()
            if attempt == 1:
                await release_first_attempts.wait()
                raise RetryableToolError(
                    "并发上游暂时不可用",
                    code="upstream_unavailable",
                )
            return AgentToolResult(
                content=[{"type": "text", "text": f"{call_id}-ok"}], details={}
            )

        provider = ScriptedProvider(
            [
                self.tool_response(
                    [self.tool_call("parallel-a"), self.tool_call("parallel-b")]
                ),
                self.final_response(),
            ]
        )
        agent = Agent(
            model=self.model,
            stream_fn=provider.stream,
            tools=[self.make_tool(execute, retry_policy=self.retry_policy())],
            max_turns=20,
            max_tool_calls=3,
            max_parallel_tools=2,
        )
        budget_events: list[dict] = []
        agent.subscribe(
            lambda event, _token: budget_events.append(event)
            if event["type"] == "budget_exceeded"
            else None
        )

        prompt_task = asyncio.create_task(agent.prompt("两个工具同时重试"))
        await asyncio.wait_for(first_attempts_ready.wait(), timeout=1)
        release_first_attempts.set()
        await asyncio.wait_for(prompt_task, timeout=1)

        self.assertEqual(sum(attempts_by_call.values()), 3)
        self.assertEqual(sorted(attempts_by_call.values()), [1, 2])
        self.assertEqual(provider.call_count, 1)
        results = [
            message
            for message in agent.state.messages
            if message["role"] == "toolResult"
        ]
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(bool(result["isError"]) for result in results), 1)
        rejected = next(result for result in results if result["isError"])
        self.assertEqual(
            rejected["details"]["code"], "tool_call_budget_exceeded"
        )
        self.assertEqual(len(budget_events), 1)
        self.assertEqual(budget_events[0]["used"], 3)

    async def test_新_prompt_重新获得独立预算(self) -> None:
        provider = ScriptedProvider(
            [self.final_response("第一次"), self.final_response("第二次")]
        )
        agent = Agent(
            model=self.model,
            stream_fn=provider.stream,
            max_turns=1,
            max_tool_calls=1,
            max_parallel_tools=1,
        )

        await agent.prompt("任务一")
        await agent.prompt("任务二")

        self.assertEqual(provider.call_count, 2)
        assistant_texts = [
            message["content"][0]["text"]
            for message in agent.state.messages
            if message["role"] == "assistant"
        ]
        self.assertEqual(assistant_texts, ["第一次", "第二次"])


if __name__ == "__main__":
    unittest.main()
