"""真实模型基础示例：接收任意用户消息并保留编号事件说明。

命令行直接传入消息：

    python examples/basic_usage.py "请同时计算 2+3 和 4×5"

不传消息时，程序会在终端询问。运行前需准备本地 `agent.toml` 和
`providers.toml`；真实配置均已被 Git 忽略。
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from urllib.parse import urlsplit

# 允许在没有 pip install -e . 的情况下直接运行仓库示例。
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pi_agent_loop import (  # noqa: E402
    Agent,
    CompactionRetryPolicy,
    DurableOperationRecorder,
    JsonlOperationEventStore,
    JsonlRetryEventStore,
    JsonlRuntimeEventStore,
    ProviderConfigError,
    RuntimeRecoveryManager,
    RuntimeStateTracker,
    ToolRegistry,
    create_add_tool,
    create_divide_tool,
    create_multiply_tool,
    compact_on_context_overflow,
    create_provider,
    load_agent_limits,
    load_provider_settings,
    project_runtime_state,
)

# ============================================================================
# Tool Timeout 教学配置区
# ============================================================================
# 正常成功：三个值都保持 0。
# 触发加法超时：改成 3，因为 add 的独立 timeout 是 2 秒。
# 触发乘法超时：改成 6，因为 multiply 的独立 timeout 是 5 秒。
# 触发除法超时：改成 4，因为 divide 的独立 timeout 是 3 秒。
ADD_TOOL_DELAY_SECONDS = 0.0
MULTIPLY_TOOL_DELAY_SECONDS = 0.0
DIVIDE_TOOL_DELAY_SECONDS = 0.0


def parse_user_message() -> str:
    """读取命令行后面的任意消息；没有参数时再使用交互输入。"""

    parser = argparse.ArgumentParser(
        description="通过真实 OpenAI-compatible 模型运行 Agent Loop",
    )
    parser.add_argument(
        "message",
        nargs="*",
        help="要交给 Agent 的用户消息；含空格时建议使用引号",
    )
    arguments = parser.parse_args()
    if arguments.message:
        return " ".join(arguments.message).strip()
    return input("请输入用户消息：").strip()


def content_text(message: dict) -> str:
    """从消息 content 中取出全部文本。"""

    return "".join(
        str(block.get("text", ""))
        for block in message.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    )


def result_text(result: dict) -> str:
    """从工具结果中取第一段文本。"""

    for block in result.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            return str(block.get("text", ""))
    return "（没有文本结果）"


def model_call_explanations(messages: list[dict]) -> list[str]:
    """根据真实 assistant 历史解释每次模型调用，而不是写死调用两次。"""

    assistant_messages = [
        message for message in messages if message.get("role") == "assistant"
    ]
    explanations: list[str] = []
    for index, message in enumerate(assistant_messages, start=1):
        tool_names = [
            str(block.get("name", "未知工具"))
            for block in message.get("content", [])
            if isinstance(block, dict) and block.get("type") == "toolCall"
        ]
        if tool_names:
            explanations.append(
                f"  第 {index} 次：模型决定调用工具：{'、'.join(tool_names)}。"
            )
        elif message.get("stopReason") in {"error", "aborted"}:
            explanations.append(
                f"  第 {index} 次：模型请求失败："
                f"{message.get('errorMessage', '未知错误')}。"
            )
        elif index == 1:
            explanations.append(
                "  第 1 次：模型判断不需要工具，直接生成回答。"
            )
        else:
            explanations.append(
                f"  第 {index} 次：模型读取此前消息和工具结果，生成回答。"
            )
    return explanations


async def main() -> None:
    user_message = parse_user_message()
    if not user_message:
        print("用户消息为空，程序结束。")
        return

    try:
        limits = load_agent_limits(ROOT / "config" / "agent.toml")
        provider_settings = load_provider_settings(
            ROOT / "config" / "providers.toml"
        )
    except (FileNotFoundError, ProviderConfigError, ValueError) as error:
        print(f"配置错误：{error}")
        print("请按照 config/README.md 复制并填写本地配置。")
        return

    model, provider = create_provider(provider_settings)
    profile = provider_settings.active

    registry = ToolRegistry()
    registry.register(create_add_tool(delay_seconds=ADD_TOOL_DELAY_SECONDS))
    registry.register(
        create_multiply_tool(delay_seconds=MULTIPLY_TOOL_DELAY_SECONDS)
    )
    registry.register(
        create_divide_tool(delay_seconds=DIVIDE_TOOL_DELAY_SECONDS)
    )

    retry_store = JsonlRetryEventStore(ROOT / "state" / "retry-events.jsonl")
    runtime_store = JsonlRuntimeEventStore(ROOT / "state" / "runtime-events.jsonl")
    operation_store = JsonlOperationEventStore(ROOT / "state" / "operation-events.jsonl")
    operation_recorder = DurableOperationRecorder(
        operation_store,
        session_id="basic-usage",
        tools=registry.all(),
        configuration={"provider": model.provider, "model": model.id},
    )
    await RuntimeRecoveryManager(runtime_store).recover()
    runtime_tracker = await RuntimeStateTracker.create(runtime_store)
    compacting_stream = compact_on_context_overflow(
        provider.stream,
        CompactionRetryPolicy(max_retries=1, keep_recent_messages=20),
    )
    agent = Agent(
        model=model,
        stream_fn=compacting_stream,
        system_prompt=(
            "你是一个中文助手。先理解用户的真实请求。只有在请求适合当前工具时"
            "才调用工具；工具执行后必须读取 Tool Result 再回答。没有合适工具时，"
            "使用模型自身能力回答，不得编造已经执行了外部操作。"
        ),
        tools=registry.all(),
        tool_execution="parallel",
        max_tool_calls=limits.max_tool_calls,
        max_parallel_tools=limits.max_parallel_tools,
        max_turns=limits.max_turns,
        retry_event_sink=retry_store.append,
    )

    # 保留原来的 01、02、03……中文事件显示模式。message_update 是逐字流事件，
    # 数量可能非常多，所以继续只展示关键生命周期事件。
    event_number = 0
    turn_number = 0

    async def print_event(event, _cancellation) -> None:
        nonlocal event_number, turn_number
        event_type = event["type"]
        if event_type == "message_update":
            return

        event_number += 1
        explanation = ""

        if event_type == "agent_start":
            explanation = "Agent 开始处理本次用户任务。"
        elif event_type == "turn_start":
            turn_number += 1
            explanation = f"开始第 {turn_number} 轮：准备请求真实模型。"
        elif event_type == "message_start":
            message = event["message"]
            role = message.get("role")
            if role == "user":
                explanation = f"收到本次用户输入：{content_text(message)}"
            elif role == "assistant":
                explanation = "真实模型开始生成一条回复。"
            elif role == "toolResult":
                explanation = (
                    f"准备把 {message.get('toolName')} 的结果写回模型上下文。"
                )
        elif event_type == "message_end":
            message = event["message"]
            role = message.get("role")
            if role == "user":
                explanation = "用户消息已经加入本次模型上下文。"
            elif role == "assistant":
                tool_names = [
                    str(block.get("name", "未知工具"))
                    for block in message.get("content", [])
                    if isinstance(block, dict) and block.get("type") == "toolCall"
                ]
                if tool_names:
                    explanation = "模型要求调用工具：" + "、".join(tool_names) + "。"
                elif message.get("stopReason") in {"error", "aborted"}:
                    explanation = (
                        "模型请求失败："
                        f"{message.get('errorMessage', '未知错误')}"
                    )
                else:
                    explanation = f"模型回复完成：{content_text(message)}"
            elif role == "toolResult":
                explanation = (
                    f"{message.get('toolName')} 的结果已经写回模型上下文："
                    f"{result_text(message)}"
                )
        elif event_type == "tool_execution_start":
            explanation = (
                f"开始执行工具 {event['toolName']}，参数为 {event['args']}。"
            )
        elif event_type == "tool_execution_dispatch_start":
            explanation = (
                f"工具 {event['toolName']} 第 {event['attempt']} 次实际进入执行函数。"
            )
        elif event_type == "tool_execution_update":
            explanation = (
                f"工具 {event['toolName']} 报告中间进度："
                f"{result_text(event['partialResult'])}"
            )
        elif event_type == "tool_execution_end":
            status = "失败" if event["isError"] else "成功"
            explanation = (
                f"工具 {event['toolName']} 执行{status}，结果为 "
                f"{result_text(event['result'])}。"
            )
        elif event_type == "model_retry_scheduled":
            explanation = (
                f"模型请求出现瞬时错误 {event['errorCode']}，"
                f"将在 {event['delayMs']} 毫秒后进行第 {event['attempt']} 次重试。"
            )
        elif event_type == "model_retry_attempt_start":
            explanation = f"开始第 {event['attempt']} 次模型重试。"
        elif event_type == "model_retry_finished":
            explanation = (
                "模型重试成功。" if event["success"] else "模型重试最终失败。"
            )
        elif event_type == "tool_retry_scheduled":
            explanation = (
                f"工具 {event['toolName']} 出现瞬时错误 {event['errorCode']}，"
                f"将在 {event['delayMs']} 毫秒后重试。"
            )
        elif event_type == "tool_retry_attempt_start":
            explanation = (
                f"工具 {event['toolName']} 开始第 {event['attempt']} 次重试。"
            )
        elif event_type == "tool_retry_finished":
            explanation = (
                f"工具 {event['toolName']} 重试"
                f"{'成功' if event['success'] else '失败'}。"
            )
        elif event_type == "budget_exceeded":
            explanation = f"运行预算不足：{event['message']}。"
        elif event_type == "turn_end":
            explanation = f"第 {turn_number} 轮结束。"
        elif event_type == "agent_end":
            explanation = "没有更多工具和排队消息，Agent 结束本次运行。"
        else:
            explanation = f"发生内部事件 {event_type}。"

        print(f"[{event_number:02d}] {explanation}")

    print("=" * 68)
    print("真实 OpenAI-compatible Agent 示例")
    print(f"Provider：{model.provider}")
    print(f"模型：{model.id}")
    print(f"用户消息：{user_message}")
    print(
        "已注册工具：add（独立超时 2 秒）、"
        "multiply（独立超时 5 秒）、divide（独立超时 3 秒）。"
    )
    print(
        "运行预算："
        f"max_turns={limits.max_turns}，"
        f"max_tool_calls={limits.max_tool_calls}，"
        f"max_parallel_tools={limits.max_parallel_tools}。"
    )
    print(
        "模型重试："
        f"enabled={profile.retry_policy.enabled}，"
        f"max_retries={profile.retry_policy.max_retries}。"
    )
    parsed_url = urlsplit(profile.base_url)
    if parsed_url.scheme == "http" and parsed_url.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        print("安全警告：当前使用远程明文 HTTP，Bearer 和消息没有 TLS 保护。")
    print("=" * 68)

    # 状态事实先写入 Runtime Event Store，再交给终端故事线。
    agent.subscribe(runtime_tracker.listener)
    agent.subscribe(operation_recorder.listener)
    agent.subscribe(print_event)
    await agent.prompt(user_message)

    final = agent.state.messages[-1]
    final_text = content_text(final)
    if not final_text:
        final_text = str(final.get("errorMessage", "（模型没有返回文本）"))

    print("\n" + "=" * 68)
    print("最终回答：", final_text)
    print("模型调用次数：", provider.call_count)
    print(f"为什么调用 {provider.call_count} 次：")
    explanations = model_call_explanations(agent.state.messages)
    if explanations:
        for explanation in explanations:
            print(explanation)
    else:
        print("  本次没有形成 assistant 消息。")
    runtime_view = project_runtime_state(runtime_tracker.state)
    print("运行状态：", runtime_view["phaseLabel"])
    print("Runtime Run ID：", runtime_view["runId"])
    print("Durable Operation ID：", operation_recorder.last_operation_id)
    print("=" * 68)


if __name__ == "__main__":
    asyncio.run(main())
