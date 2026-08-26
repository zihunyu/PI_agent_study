"""OpenAI-compatible 请求序列化测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop.providers import (  # noqa: E402
    ProviderProfile,
    ProviderProtocolError,
    serialize_chat_request,
)


class ProviderSerializeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = ProviderProfile(
            name="test",
            protocol="openai_chat_completions",
            base_url="https://vendor.example/v1",
            endpoint="/chat/completions",
            auth_type="bearer",
            api_key="test-secret",
            model="chosen-model",
            stream=True,
            connect_timeout_seconds=10,
            request_timeout_seconds=60,
            allow_insecure_http=False,
        )

    def test_序列化_system_用户_工具调用和工具结果(self) -> None:
        context = {
            "systemPrompt": "你是计算助手",
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "计算 2+3"}],
                },
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "call-1",
                            "name": "add",
                            "arguments": {"a": 2, "b": 3},
                        }
                    ],
                },
                {
                    "role": "toolResult",
                    "toolCallId": "call-1",
                    "toolName": "add",
                    "content": [{"type": "text", "text": "5"}],
                },
            ],
            "tools": [
                {
                    "name": "add",
                    "description": "计算加法",
                    "parameters": {
                        "type": "object",
                        "properties": {"a": {"type": "number"}},
                    },
                }
            ],
        }

        payload = serialize_chat_request(self.profile, context)

        self.assertEqual(payload["model"], "chosen-model")
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertEqual(payload["messages"][1]["content"], "计算 2+3")
        self.assertEqual(
            payload["messages"][2]["tool_calls"][0]["function"]["arguments"],
            '{"a":2,"b":3}',
        )
        self.assertEqual(payload["messages"][3]["role"], "tool")
        self.assertEqual(payload["messages"][3]["tool_call_id"], "call-1")
        self.assertEqual(payload["tools"][0]["function"]["name"], "add")
        self.assertEqual(payload["tool_choice"], "auto")

    def test_没有工具时不发送_tools_和_tool_choice(self) -> None:
        payload = serialize_chat_request(
            self.profile,
            {
                "systemPrompt": "",
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "你好"}],
                    }
                ],
                "tools": [],
            },
        )
        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)

    def test_支持_required_和_named_tool_choice(self) -> None:
        context = {
            "systemPrompt": "",
            "messages": [],
            "tools": [
                {
                    "name": "add",
                    "description": "计算加法",
                    "parameters": {"type": "object"},
                }
            ],
        }
        required = serialize_chat_request(
            self.profile,
            context,
            {"tool_choice": "required"},
        )
        named = serialize_chat_request(
            self.profile,
            context,
            {
                "tool_choice": {
                    "type": "function",
                    "function": {"name": "add"},
                }
            },
        )

        self.assertEqual(required["tool_choice"], "required")
        self.assertEqual(
            named["tool_choice"],
            {"type": "function", "function": {"name": "add"}},
        )

    def test_强制不可见工具被拒绝(self) -> None:
        with self.assertRaisesRegex(ProviderProtocolError, "不可见工具"):
            serialize_chat_request(
                self.profile,
                {
                    "systemPrompt": "",
                    "messages": [],
                    "tools": [
                        {
                            "name": "add",
                            "description": "计算加法",
                            "parameters": {"type": "object"},
                        }
                    ],
                },
                {
                    "tool_choice": {
                        "type": "function",
                        "function": {"name": "delete_all"},
                    }
                },
            )

    def test_没有工具时不能设置_required(self) -> None:
        with self.assertRaisesRegex(ProviderProtocolError, "没有可用工具"):
            serialize_chat_request(
                self.profile,
                {"systemPrompt": "", "messages": [], "tools": []},
                {"tool_choice": "required"},
            )

    def test_未知消息角色_fail_closed(self) -> None:
        with self.assertRaises(ProviderProtocolError):
            serialize_chat_request(
                self.profile,
                {
                    "systemPrompt": "",
                    "messages": [{"role": "ui-only", "content": []}],
                    "tools": [],
                },
            )


if __name__ == "__main__":
    unittest.main()
