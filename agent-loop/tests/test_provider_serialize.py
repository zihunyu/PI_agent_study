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

    def test_标准image_url和data_url被序列化为多模态content(self) -> None:
        data_url = "data:image/png;base64,aGVsbG8="
        payload = serialize_chat_request(
            self.profile,
            {
                "systemPrompt": "",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "比较两张图"},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "https://cdn.example/a.png",
                                    "detail": "high",
                                },
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": data_url},
                            },
                        ],
                    }
                ],
                "tools": [],
            },
        )

        content = payload["messages"][0]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[0], {"type": "text", "text": "比较两张图"})
        self.assertEqual(content[1]["type"], "image_url")
        self.assertEqual(content[1]["image_url"]["detail"], "high")
        self.assertEqual(content[2]["image_url"]["url"], data_url)

    def test_不支持的用户媒体类型在provider边界明确拒绝(self) -> None:
        with self.assertRaisesRegex(ProviderProtocolError, "不支持"):
            serialize_chat_request(
                self.profile,
                {
                    "systemPrompt": "",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "input_audio", "input_audio": {}}
                            ],
                        }
                    ],
                    "tools": [],
                },
            )

    def test_system_tool_schema和完整请求均有可配置出站上限(self) -> None:
        with self.assertRaisesRegex(ProviderProtocolError, "systemPrompt"):
            serialize_chat_request(
                self.profile,
                {"systemPrompt": "中文", "messages": [], "tools": []},
                {"max_system_prompt_bytes": 5},
            )
        with self.assertRaisesRegex(ProviderProtocolError, "工具 schema"):
            serialize_chat_request(
                self.profile,
                {
                    "systemPrompt": "",
                    "messages": [],
                    "tools": [
                        {
                            "name": "large",
                            "description": "x" * 100,
                            "parameters": {"type": "object"},
                        }
                    ],
                },
                {"max_tool_schema_bytes": 32},
            )
        with self.assertRaisesRegex(ProviderProtocolError, "模型请求"):
            serialize_chat_request(
                self.profile,
                {
                    "systemPrompt": "",
                    "messages": [
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "hello"}],
                        }
                    ],
                    "tools": [],
                },
                {"max_request_bytes": 16},
            )

    def test_provider再次限制content_block和image_url字节(self) -> None:
        context = {
            "systemPrompt": "",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "看图"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://cdn.example/a.png"},
                        },
                    ],
                }
            ],
            "tools": [],
        }
        with self.assertRaisesRegex(ProviderProtocolError, "block"):
            serialize_chat_request(
                self.profile,
                context,
                {"max_content_blocks_per_message": 1},
            )
        with self.assertRaisesRegex(ProviderProtocolError, "URL"):
            serialize_chat_request(
                self.profile,
                context,
                {"max_image_url_bytes": 8},
            )


if __name__ == "__main__":
    unittest.main()
