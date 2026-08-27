"""通过本地配置访问真实 OpenAI-compatible 第三方模型。

运行前请先复制并填写：

    config/agent.toml.example     -> config/agent.toml
    config/providers.toml.example -> config/providers.toml

真实配置已被 Git 忽略。程序不会打印 API Key。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pi_agent_loop import (  # noqa: E402
    Agent,
    CompactionRetryPolicy,
    JsonlRetryEventStore,
    ProviderConfigError,
    compact_on_context_overflow,
    create_calculator_tools,
    create_provider,
    load_agent_limits,
    load_provider_settings,
)


async def main() -> None:
    try:
        provider_settings = load_provider_settings(
            ROOT / "config" / "providers.toml"
        )
        limits = load_agent_limits(ROOT / "config" / "agent.toml")
    except (FileNotFoundError, ProviderConfigError, ValueError) as error:
        print(f"配置错误：{error}")
        print("请按照 config/README.md 复制并填写本地配置。")
        return

    model, provider = create_provider(provider_settings)

    retry_store = JsonlRetryEventStore(ROOT / "state" / "retry-events.jsonl")
    compacting_stream = compact_on_context_overflow(
        provider.stream,
        CompactionRetryPolicy(max_retries=1, keep_recent_messages=20),
    )
    agent = Agent(
        model=model,
        stream_fn=compacting_stream,
        system_prompt=(
            "你是一个中文助手。需要计算加法或乘法时必须调用已有工具，"
            "读取工具结果后再回答。"
        ),
        tools=create_calculator_tools(),
        tool_execution="parallel",
        max_tool_calls=limits.max_tool_calls,
        max_parallel_tools=limits.max_parallel_tools,
        max_turns=limits.max_turns,
        retry_event_sink=retry_store.append,
    )

    def print_stream(event, _cancellation) -> None:
        if event.get("type") != "message_update":
            return
        assistant_event = event.get("assistantMessageEvent", {})
        if assistant_event.get("type") == "text_delta":
            print(assistant_event.get("delta", ""), end="", flush=True)

    agent.subscribe(print_stream)

    print(f"Provider：{model.provider}")
    print(f"模型：{model.id}")
    prompt = input("用户：").strip()
    if not prompt:
        print("没有输入，程序结束。")
        return

    print("助手：", end="", flush=True)
    await agent.prompt(prompt)
    print()

    if agent.state.error_message:
        print(f"错误：{agent.state.error_message}")


if __name__ == "__main__":
    asyncio.run(main())
