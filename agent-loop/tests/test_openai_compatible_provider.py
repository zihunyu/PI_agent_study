"""OpenAI-compatible HTTP Provider 与 Agent 集成测试。"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import (  # noqa: E402
    Agent,
    CancellationToken,
    Model,
    ModelRetryPolicy,
    create_add_tool,
)
from pi_agent_loop.providers import (  # noqa: E402
    OpenAICompatibleProvider,
    ProviderProfile,
)


def sse(*payloads: dict | str) -> bytes:
    lines: list[str] = []
    for payload in payloads:
        data = payload if isinstance(payload, str) else json.dumps(payload)
        lines.append(f"data: {data}\n\n")
    return "".join(lines).encode()


def chunk(*, delta: dict | None = None, finish_reason=None, usage=None) -> dict:
    payload: dict = {
        "id": "chatcmpl-test",
        "choices": [
            {
                "index": 0,
                "delta": delta or {},
                "finish_reason": finish_reason,
            }
        ],
    }
    if usage is not None:
        payload["usage"] = usage
    return payload


class OpenAICompatibleProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.secret = "unit-test-secret"
        self.profile = ProviderProfile(
            name="third_party",
            protocol="openai_chat_completions",
            base_url="https://vendor.example/v1",
            endpoint="/chat/completions",
            auth_type="bearer",
            api_key=self.secret,
            model="chosen-model",
            stream=True,
            connect_timeout_seconds=10,
            request_timeout_seconds=60,
            allow_insecure_http=False,
        )
        self.model = Model(
            id="chosen-model",
            provider="third_party",
            api="openai_chat_completions",
        )

    async def collect(self, stream):
        events = [event async for event in stream]
        return events, await stream.result()

    async def test_发送_bearer_和指定模型并翻译文本流(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["authorization"] = request.headers["authorization"]
            captured["json"] = json.loads(request.content)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse(
                    chunk(delta={"role": "assistant"}),
                    chunk(delta={"content": "你"}),
                    chunk(delta={"content": "好"}),
                    chunk(
                        finish_reason="stop",
                        usage={
                            "prompt_tokens": 3,
                            "completion_tokens": 2,
                            "total_tokens": 5,
                        },
                    ),
                    "[DONE]",
                ),
            )

        provider = OpenAICompatibleProvider(
            self.profile,
            transport=httpx.MockTransport(handler),
        )
        stream = provider.stream(
            self.model,
            {
                "systemPrompt": "测试",
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "你好"}],
                    }
                ],
                "tools": [],
            },
            {},
        )

        events, result = await self.collect(stream)

        self.assertEqual(
            captured["url"], "https://vendor.example/v1/chat/completions"
        )
        self.assertEqual(captured["authorization"], f"Bearer {self.secret}")
        self.assertEqual(captured["json"]["model"], "chosen-model")
        self.assertTrue(captured["json"]["stream"])
        self.assertEqual(result["content"][0]["text"], "你好")
        self.assertEqual(result["usage"]["totalTokens"], 5)
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(provider.call_count, 1)

    async def test_429_按照_provider_policy_重试但逻辑调用只计一次(self) -> None:
        attempts = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(
                    429,
                    headers={"retry-after-ms": "0"},
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse(
                    chunk(delta={"content": "重试成功"}),
                    chunk(finish_reason="stop"),
                    "[DONE]",
                ),
            )

        profile = replace(
            self.profile,
            retry_policy=ModelRetryPolicy(
                enabled=True,
                max_retries=2,
                initial_delay_seconds=0,
                max_delay_seconds=1,
                jitter_ratio=0,
            ),
        )
        provider = OpenAICompatibleProvider(
            profile,
            transport=httpx.MockTransport(handler),
        )

        events, result = await self.collect(
            provider.stream(
                self.model,
                {"systemPrompt": "", "messages": [], "tools": []},
                {},
            )
        )

        self.assertEqual(result["content"][0]["text"], "重试成功")
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(provider.attempt_count, 2)
        self.assertIn("model_retry_scheduled", [event["type"] for event in events])

    async def test_流式_tool_call_参数被安全组装(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream; charset=utf-8"},
                content=sse(
                    chunk(
                        delta={
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "add",
                                        "arguments": '{"a":',
                                    },
                                }
                            ]
                        }
                    ),
                    chunk(
                        delta={
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '2,"b":3}'},
                                }
                            ]
                        }
                    ),
                    chunk(finish_reason="tool_calls"),
                    "[DONE]",
                ),
            )

        provider = OpenAICompatibleProvider(
            self.profile,
            transport=httpx.MockTransport(handler),
        )
        _, result = await self.collect(
            provider.stream(
                self.model,
                {"systemPrompt": "", "messages": [], "tools": []},
                {},
            )
        )

        self.assertEqual(result["stopReason"], "toolUse")
        self.assertEqual(
            result["content"][0],
            {
                "type": "toolCall",
                "id": "call-1",
                "name": "add",
                "arguments": {"a": 2, "b": 3},
            },
        )

    async def test_401_不泄露_api_key(self) -> None:
        provider = OpenAICompatibleProvider(
            self.profile,
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(401, content=b"unauthorized")
            ),
        )

        events, result = await self.collect(
            provider.stream(
                self.model,
                {"systemPrompt": "", "messages": [], "tools": []},
                {},
            )
        )

        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(result["stopReason"], "error")
        self.assertIn("鉴权失败", result["errorMessage"])
        self.assertEqual(
            result["providerError"],
            {
                "code": "provider_authentication_error",
                "statusCode": 401,
                "retryAfterMs": None,
                "retryable": False,
            },
        )
        self.assertNotIn(self.secret, repr(events))
        self.assertNotIn(self.secret, repr(result))

    async def test_取消令牌会终止_http_流并返回_aborted(self) -> None:
        started = asyncio.Event()
        closed = asyncio.Event()
        never = asyncio.Event()

        class SlowBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                started.set()
                await never.wait()
                yield b""

            async def aclose(self) -> None:
                closed.set()

        provider = OpenAICompatibleProvider(
            self.profile,
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=SlowBody(),
                )
            ),
        )
        cancellation = CancellationToken()
        collect_task = asyncio.create_task(
            self.collect(
                provider.stream(
                    self.model,
                    {"systemPrompt": "", "messages": [], "tools": []},
                    {"cancellation_token": cancellation},
                )
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        cancellation.cancel("用户停止真实模型请求")
        events, result = await asyncio.wait_for(collect_task, timeout=1)

        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(result["stopReason"], "aborted")
        self.assertIn("用户停止", result["errorMessage"])
        self.assertTrue(closed.is_set())

    async def test_真实_provider_事件可完成_agent_tool_call_闭环(self) -> None:
        requests: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                response = sse(
                    chunk(
                        delta={
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-add",
                                    "type": "function",
                                    "function": {
                                        "name": "add",
                                        "arguments": '{"a":2,"b":3}',
                                    },
                                }
                            ]
                        }
                    ),
                    chunk(finish_reason="tool_calls"),
                    "[DONE]",
                )
            else:
                response = sse(
                    chunk(delta={"content": "结果是 5"}),
                    chunk(finish_reason="stop"),
                    "[DONE]",
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=response,
            )

        provider = OpenAICompatibleProvider(
            self.profile,
            transport=httpx.MockTransport(handler),
        )
        agent = Agent(
            model=self.model,
            stream_fn=provider.stream,
            system_prompt="需要计算时调用工具。",
            tools=[create_add_tool()],
            max_turns=5,
            max_tool_calls=5,
            max_parallel_tools=2,
        )

        await agent.prompt("计算 2+3")

        self.assertEqual(len(requests), 2)
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(requests[0]["tools"][0]["function"]["name"], "add")
        tool_message = next(
            message
            for message in requests[1]["messages"]
            if message["role"] == "tool"
        )
        self.assertEqual(tool_message["tool_call_id"], "call-add")
        self.assertEqual(tool_message["content"], "5")
        self.assertEqual(agent.state.messages[-1]["content"][0]["text"], "结果是 5")


if __name__ == "__main__":
    unittest.main()
