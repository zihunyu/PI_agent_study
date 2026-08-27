"""模型 Turn 与单个幂等 Tool Call 重试测试。"""

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
    ModelRetryPolicy,
    RetryableToolError,
    ScriptedProvider,
    ToolRetryPolicy,
    assistant_message,
    retry_model_stream,
)


class RetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.model = Model(id="retry-model", provider="fake", api="fake")

    def provider_error(self, *, status: int, retryable: bool) -> dict:
        message = assistant_message(
            model=self.model,
            stop_reason="error",
            error_message=f"HTTP {status}",
        )
        message["providerError"] = {
            "code": "provider_rate_limit_error"
            if status == 429
            else "provider_authentication_error",
            "statusCode": status,
            "retryAfterMs": 0,
            "retryable": retryable,
        }
        return message

    def model_policy(self, *, delay: float = 0) -> ModelRetryPolicy:
        return ModelRetryPolicy(
            enabled=True,
            max_retries=2,
            initial_delay_seconds=delay,
            max_delay_seconds=max(1, delay),
            jitter_ratio=0,
        )

    async def test_模型429后只重试同一逻辑_turn(self) -> None:
        provider = ScriptedProvider(
            [
                self.provider_error(status=429, retryable=True),
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "重试成功"}],
                ),
            ]
        )
        agent = Agent(
            model=self.model,
            stream_fn=retry_model_stream(
                provider.stream,
                self.model_policy(),
                random=lambda: 0.5,
            ),
        )
        events: list[str] = []
        agent.subscribe(lambda event, _token: events.append(event["type"]))

        await agent.prompt("测试模型重试")

        self.assertEqual(provider.call_count, 2)
        self.assertEqual(agent.state.messages[-1]["content"][0]["text"], "重试成功")
        self.assertIn("model_retry_scheduled", events)
        self.assertIn("model_retry_attempt_start", events)
        self.assertIn("model_retry_finished", events)

    async def test_模型401不会重试(self) -> None:
        provider = ScriptedProvider(
            [self.provider_error(status=401, retryable=False)]
        )
        agent = Agent(
            model=self.model,
            stream_fn=retry_model_stream(provider.stream, self.model_policy()),
        )

        await agent.prompt("测试鉴权失败")

        self.assertEqual(provider.call_count, 1)
        self.assertEqual(agent.state.messages[-1]["stopReason"], "error")

    async def test_模型backoff_可被用户取消(self) -> None:
        transient_error = self.provider_error(status=429, retryable=True)
        transient_error["providerError"]["retryAfterMs"] = None
        provider = ScriptedProvider([transient_error])
        agent = Agent(
            model=self.model,
            stream_fn=retry_model_stream(
                provider.stream,
                self.model_policy(delay=1),
            ),
        )
        scheduled = asyncio.Event()

        def listener(event, _token):
            if event["type"] == "model_retry_scheduled":
                scheduled.set()

        agent.subscribe(listener)
        prompt_task = asyncio.create_task(agent.prompt("取消重试"))
        await asyncio.wait_for(scheduled.wait(), timeout=1)
        agent.abort("用户取消 Retry Backoff")
        await asyncio.wait_for(prompt_task, timeout=1)

        self.assertEqual(provider.call_count, 1)
        self.assertEqual(agent.state.messages[-1]["stopReason"], "aborted")

    def tool_policy(self, *, max_retries: int = 2) -> ToolRetryPolicy:
        return ToolRetryPolicy(
            max_retries=max_retries,
            retryable_codes=frozenset({"upstream_unavailable"}),
            idempotent=True,
            initial_delay_seconds=0,
            max_delay_seconds=1,
            jitter_ratio=0,
        )

    def tool_provider(self, *tool_names: str) -> ScriptedProvider:
        calls = [
            {
                "type": "toolCall",
                "id": f"{name}-id",
                "name": name,
                "arguments": {},
            }
            for name in tool_names
        ]
        return ScriptedProvider(
            [
                assistant_message(
                    model=self.model,
                    stop_reason="toolUse",
                    content=calls,
                ),
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "工具阶段完成"}],
                ),
            ]
        )

    async def test_并行工具只重试失败的那个(self) -> None:
        transient_attempts = 0
        sibling_attempts = 0

        async def transient(_id, _args, _token, _update):
            nonlocal transient_attempts
            transient_attempts += 1
            if transient_attempts == 1:
                raise RetryableToolError(
                    "上游暂时不可用",
                    code="upstream_unavailable",
                )
            return AgentToolResult(
                content=[{"type": "text", "text": "transient-ok"}],
                details={},
            )

        async def sibling(_id, _args, _token, _update):
            nonlocal sibling_attempts
            sibling_attempts += 1
            return AgentToolResult(
                content=[{"type": "text", "text": "sibling-ok"}],
                details={},
            )

        tools = [
            AgentTool(
                name="transient",
                label="瞬时失败工具",
                description="第一次失败，第二次成功",
                execute=transient,
                retry_policy=self.tool_policy(),
            ),
            AgentTool(
                name="sibling",
                label="兄弟工具",
                description="只应执行一次",
                execute=sibling,
            ),
        ]
        provider = self.tool_provider("transient", "sibling")
        agent = Agent(model=self.model, stream_fn=provider.stream, tools=tools)
        events: list[str] = []
        agent.subscribe(lambda event, _token: events.append(event["type"]))

        await agent.prompt("并行工具重试")

        self.assertEqual(transient_attempts, 2)
        self.assertEqual(sibling_attempts, 1)
        results = [
            message
            for message in agent.state.messages
            if message["role"] == "toolResult"
        ]
        self.assertEqual(
            [result["content"][0]["text"] for result in results],
            ["transient-ok", "sibling-ok"],
        )
        self.assertIn("tool_retry_scheduled", events)
        self.assertIn("tool_retry_finished", events)

    async def test_非_retryable_code_不会重试(self) -> None:
        attempts = 0

        async def execute(_id, _args, _token, _update):
            nonlocal attempts
            attempts += 1
            raise RetryableToolError("参数错误", code="invalid_arguments")

        tool = AgentTool(
            name="permanent",
            label="永久失败",
            description="不应重试",
            execute=execute,
            retry_policy=self.tool_policy(),
        )
        agent = Agent(
            model=self.model,
            stream_fn=self.tool_provider("permanent").stream,
            tools=[tool],
        )

        await agent.prompt("永久错误")

        self.assertEqual(attempts, 1)
        result = next(
            message
            for message in agent.state.messages
            if message["role"] == "toolResult"
        )
        self.assertEqual(result["details"]["code"], "invalid_arguments")
        self.assertEqual(result["details"]["attempts"], 1)

    async def test_工具重试耗尽只返回一个最终错误(self) -> None:
        attempts = 0

        async def execute(_id, _args, _token, _update):
            nonlocal attempts
            attempts += 1
            raise RetryableToolError(
                "上游一直不可用",
                code="upstream_unavailable",
            )

        tool = AgentTool(
            name="exhausted",
            label="耗尽重试",
            description="每次都失败",
            execute=execute,
            retry_policy=self.tool_policy(max_retries=2),
        )
        agent = Agent(
            model=self.model,
            stream_fn=self.tool_provider("exhausted").stream,
            tools=[tool],
        )

        await agent.prompt("耗尽重试")

        self.assertEqual(attempts, 3)
        results = [
            message
            for message in agent.state.messages
            if message["role"] == "toolResult"
        ]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["details"]["attempts"], 3)
        self.assertIn("retryId", results[0]["details"])

    def test_非幂等工具不能配置自动重试(self) -> None:
        with self.assertRaisesRegex(ValueError, "幂等"):
            ToolRetryPolicy(
                max_retries=1,
                retryable_codes=frozenset({"timeout"}),
                idempotent=False,
            )


if __name__ == "__main__":
    unittest.main()
