"""Agent prompt image normalization and finite input budget tests."""

from __future__ import annotations

import unittest

from pi_agent_loop import (
    Agent,
    MessageInputLimits,
    Model,
    ScriptedProvider,
    assistant_message,
    user_message,
)


class AgentMultimodalInputTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.model = Model(id="vision-test", provider="fake", api="fake")

    async def test_prompt把image_url字符串规范化并传给provider(self) -> None:
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "看到了"}],
                )
            ]
        )
        agent = Agent(model=self.model, stream_fn=provider.stream)

        await agent.prompt(
            "描述图片",
            images=[
                {
                    "type": "image_url",
                    "image_url": "https://cdn.example/image.png",
                }
            ],
        )

        blocks = provider.contexts[0]["messages"][0]["content"]
        self.assertEqual(blocks[0], {"type": "text", "text": "描述图片"})
        self.assertEqual(
            blocks[1],
            {
                "type": "image_url",
                "image_url": {"url": "https://cdn.example/image.png"},
            },
        )

    async def test_prompt不支持的媒体类型在调用provider前拒绝(self) -> None:
        provider = ScriptedProvider([])
        agent = Agent(model=self.model, stream_fn=provider.stream)

        with self.assertRaisesRegex(ValueError, "不支持"):
            await agent.prompt(
                "播放",
                images=[{"type": "audio", "url": "https://cdn.example/a.mp3"}],
            )

        self.assertEqual(provider.call_count, 0)

    async def test_agent可配置文本图片和content_block预算(self) -> None:
        limits = MessageInputLimits(
            max_text_bytes=4,
            max_images=1,
            max_image_url_bytes=64,
            max_total_image_url_bytes=64,
            max_content_blocks=2,
        )
        provider = ScriptedProvider([])
        agent = Agent(
            model=self.model,
            stream_fn=provider.stream,
            message_input_limits=limits,
        )

        with self.assertRaisesRegex(ValueError, "文本字节"):
            await agent.prompt("中文")
        with self.assertRaisesRegex(ValueError, "图片数量|block"):
            await agent.prompt(
                "ok",
                images=[
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://cdn.example/a.png"},
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://cdn.example/b.png"},
                    },
                ],
            )
        self.assertEqual(provider.call_count, 0)

    def test_user_message限制data_url单块和累计字节(self) -> None:
        data_url = "data:image/png;base64,aGVsbG8="
        with self.assertRaisesRegex(ValueError, "image URL"):
            user_message(
                "x",
                [
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    }
                ],
                limits=MessageInputLimits(
                    max_text_bytes=8,
                    max_images=1,
                    max_image_url_bytes=8,
                    max_total_image_url_bytes=8,
                    max_content_blocks=2,
                ),
            )


if __name__ == "__main__":
    unittest.main()
