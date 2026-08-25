"""最简单示例：不调用工具，只演示用户消息和模型回答。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import Agent, Model, ScriptedProvider, assistant_message  # noqa: E402


async def main() -> None:
    model = Model(id="demo-model", provider="scripted", api="demo")

    # ScriptedProvider 是假模型。下面这条消息就是它唯一的预设回答。
    provider = ScriptedProvider(
        [
            assistant_message(
                model=model,
                content=[
                    {
                        "type": "text",
                        "text": "你好！这是一条由假模型返回的固定回答。",
                    }
                ],
            )
        ]
    )

    agent = Agent(
        model=model,
        stream_fn=provider.stream,
        system_prompt="你是一个中文助手。",
    )

    question = "你好，请介绍你自己。"
    print("用户输入：", question)
    print("Agent 正在把用户消息交给假模型……")

    await agent.prompt(question)

    final_message = agent.state.messages[-1]
    final_text = final_message["content"][0]["text"]
    print("模型回答：", final_text)
    print("\n说明：这个例子没有工具调用，也没有连接真实网络。")


if __name__ == "__main__":
    asyncio.run(main())
