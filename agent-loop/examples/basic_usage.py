"""最小可运行示例：模型调用两个工具，再根据结果生成最终回答。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 允许在没有 pip install -e . 的情况下直接运行仓库示例。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import (  # noqa: E402
    Agent,
    Model,
    ScriptedProvider,
    ToolRegistry,
    assistant_message,
    create_add_tool,
    create_multiply_tool,
)


async def main() -> None:
    model = Model(
        id="demo-model",
        provider="scripted",
        api="demo",
        name="演示模型",
    )

    # 第一次模型响应同时要求调用 add 和 multiply。
    first_response = assistant_message(
        model=model,
        stop_reason="toolUse",
        content=[
            {
                "type": "toolCall",
                "id": "call-add",
                "name": "add",
                "arguments": {"a": 2, "b": 3},
            },
            {
                "type": "toolCall",
                "id": "call-multiply",
                "name": "multiply",
                "arguments": {"a": 4, "b": 5},
            },
        ],
    )

    # 第二次响应根据 Context 中的两条 toolResult 生成最终文本。
    def final_response(context, _options):
        results = [
            block["content"][0]["text"]
            for block in context["messages"]
            if block.get("role") == "toolResult"
        ]
        return assistant_message(
            model=model,
            content=[
                {
                    "type": "text",
                    "text": f"加法结果是 {results[0]}，乘法结果是 {results[1]}。",
                }
            ],
            stop_reason="stop",
        )

    provider = ScriptedProvider(
        [first_response, final_response],
        chunk_size=4,
    )

    # 第一步：创建工具注册表。
    registry = ToolRegistry()

    # 第二步：创建我们自己编写的加法、乘法工具，并注册到注册表。
    registry.register(create_add_tool())
    registry.register(create_multiply_tool())

    # 第三步：把注册表中的工具列表交给 Agent。
    # 从这一刻开始，Provider 能在模型请求中看到工具定义；模型返回同名
    # toolCall 时，Agent Loop 就能找到并执行对应的 Python 函数。
    agent = Agent(
        model=model,
        stream_fn=provider.stream,
        system_prompt="你是一个只使用给定工具完成计算的助手。",
        tools=registry.all(),
        tool_execution="parallel",
    )

    # 下面把内部英文事件翻译成中文故事线。message_update 是逐字流事件，
    # 数量可能很多，为了让第一次阅读更清楚，本示例不逐条打印它。
    event_number = 0
    turn_number = 0

    def content_text(message: dict) -> str:
        """从消息 content 中取出所有文本，便于显示。"""

        return "".join(
            block.get("text", "")
            for block in message.get("content", [])
            if block.get("type") == "text"
        )

    def result_text(result: dict) -> str:
        """从工具结果中取第一段文本。"""

        for block in result.get("content", []):
            if block.get("type") == "text":
                return str(block.get("text", ""))
        return "（没有文本结果）"

    async def print_event(event, _cancellation):
        """把每个关键事件翻译为一条通俗中文说明。"""

        nonlocal event_number, turn_number
        event_type = event["type"]
        if event_type == "message_update":
            return

        event_number += 1
        explanation = ""

        if event_type == "agent_start":
            explanation = "Agent 开始处理用户任务。"
        elif event_type == "turn_start":
            turn_number += 1
            explanation = f"开始第 {turn_number} 轮：准备请求一次模型。"
        elif event_type == "message_start":
            message = event["message"]
            role = message.get("role")
            if role == "user":
                explanation = f"开始接收用户消息：{content_text(message)}"
            elif role == "assistant":
                explanation = "模型开始生成一条回复。"
            elif role == "toolResult":
                explanation = (
                    f"准备把 {message.get('toolName')} 的结果写回对话。"
                )
        elif event_type == "message_end":
            message = event["message"]
            role = message.get("role")
            if role == "user":
                explanation = "用户消息已经加入本次模型上下文。"
            elif role == "assistant":
                tool_names = [
                    block.get("name", "未知工具")
                    for block in message.get("content", [])
                    if block.get("type") == "toolCall"
                ]
                if tool_names:
                    explanation = (
                        "模型第一轮没有直接给答案，而是要求调用工具："
                        + "、".join(tool_names)
                        + "。"
                    )
                else:
                    explanation = f"模型回复完成：{content_text(message)}"
            elif role == "toolResult":
                explanation = (
                    f"{message.get('toolName')} 的结果已经写回对话："
                    f"{result_text(message)}"
                )
        elif event_type == "tool_execution_start":
            explanation = (
                f"开始执行工具 {event['toolName']}，参数为 {event['args']}。"
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
        elif event_type == "turn_end":
            explanation = f"第 {turn_number} 轮结束。"
        elif event_type == "agent_end":
            explanation = "没有更多工具和排队消息，Agent 结束本次运行。"
        else:
            explanation = f"发生内部事件 {event_type}。"

        print(f"[{event_number:02d}] {explanation}")

    print("=" * 68)
    print("这个示例不会连接真实大模型，而是使用预先写好的假模型响应。")
    print("任务：请同时计算 2+3 和 4×5。")
    print("重点：模型先请求两个工具，工具完成后，模型再生成最终回答。")
    print("=" * 68)

    agent.subscribe(print_event)
    await agent.prompt("请同时计算 2+3 和 4×5")

    final = agent.state.messages[-1]
    print("\n" + "=" * 68)
    print("最终回答：", final["content"][0]["text"])
    print("模型调用次数：", provider.call_count)
    print("为什么调用 2 次：")
    print("  第 1 次：模型决定调用 add 和 multiply 两个工具。")
    print("  第 2 次：模型读取两个工具结果，整理成最终中文回答。")
    print("=" * 68)


if __name__ == "__main__":
    asyncio.run(main())
