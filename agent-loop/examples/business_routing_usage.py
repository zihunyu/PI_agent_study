"""简化 business.toml + HybridModelRouter + 强制 Tool Call 示例。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mock_order_tools import create_mock_order_status_tool  # noqa: E402
from pi_agent_loop import (  # noqa: E402
    Agent,
    BusinessConfigError,
    CapabilityRegistry,
    HybridModelRouter,
    ProviderConfigError,
    RoutedAgent,
    create_provider,
    load_agent_limits,
    load_provider_settings,
    load_simple_business_config,
)


def user_input() -> str:
    parser = argparse.ArgumentParser(
        description="订单业务 Router 与 Required Tool Call 示例"
    )
    parser.add_argument("message", nargs="*")
    values = parser.parse_args().message
    return " ".join(values).strip() if values else input("请输入业务请求：").strip()


async def main() -> None:
    message = user_input()
    if not message:
        print("业务请求为空。")
        return
    try:
        limits = load_agent_limits(ROOT / "config" / "agent.toml")
        provider_settings = load_provider_settings(
            ROOT / "config" / "providers.toml"
        )
        business_config = load_simple_business_config(
            ROOT / "config" / "business.toml"
        )
    except (
        FileNotFoundError,
        ProviderConfigError,
        BusinessConfigError,
        ValueError,
    ) as error:
        print(f"配置错误：{error}")
        print("请按照 config/README.md 创建 agent/providers/business 三份配置。")
        return

    capabilities = CapabilityRegistry()
    capabilities.register(
        create_mock_order_status_tool(),
        capabilities={"orders.read_current"},
        domain="orders",
        operation="read",
        risk="low",
    )
    model, provider = create_provider(provider_settings)
    router = HybridModelRouter(
        business_config,
        capabilities,
        model=model,
        stream_fn=provider.stream,
    )
    agent = Agent(
        model=model,
        stream_fn=provider.stream,
        system_prompt=(
            "你是订单业务助手。必须遵守 Tool Choice 和业务范围策略；"
            "不能根据语言模型知识猜测订单实时状态。"
        ),
        max_turns=limits.max_turns,
        max_tool_calls=limits.max_tool_calls,
        max_parallel_tools=limits.max_parallel_tools,
    )
    routed = RoutedAgent(agent, router, capabilities)

    def event_listener(event, _token) -> None:
        if event.get("type") == "tool_execution_start":
            print(
                f"工具开始：{event['toolName']}，参数：{event['args']}"
            )
        elif event.get("type") == "tool_execution_end":
            print(
                f"工具结束：{event['toolName']}，错误：{event['isError']}"
            )

    parsed_url = urlsplit(provider_settings.active.base_url)
    if parsed_url.scheme == "http" and parsed_url.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        print("安全警告：当前使用远程明文 HTTP，Bearer 和消息没有 TLS 保护。")

    routed.subscribe(event_listener)
    result = await routed.prompt(message)
    decision = result.decision

    print("=" * 68)
    print("路由状态：", decision.status)
    print("Domain：", decision.domain)
    print("Intent：", decision.intent)
    print("Required Capabilities：", list(decision.required_capabilities))
    print("Selected Tools：", list(decision.selected_tools))
    print("Tool Policy：", decision.tool_policy.mode)
    print("路由来源：", decision.routing_source)
    print("路由置信度：", decision.confidence)
    print("是否需要审批：", decision.requires_approval)
    print("是否请求回答模型：", result.model_called)
    print("错误代码：", result.error_code)
    print("最终结果：", result.response_text)
    print("模型调用次数：", provider.call_count)
    print("=" * 68)


if __name__ == "__main__":
    asyncio.run(main())
