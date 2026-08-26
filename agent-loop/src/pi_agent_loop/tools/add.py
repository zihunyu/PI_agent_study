"""加法工具。

本文件演示一个工具的完整组成：

1. 给模型看的名称和说明；
2. 给模型看的 JSON Schema；
3. Python 运行时参数校验；
4. 真正执行函数；
5. 统一 AgentToolResult。
"""

from __future__ import annotations

import asyncio

from ..types import AgentTool, AgentToolResult
from .validators import validate_two_numbers


async def _execute_add(
    tool_call_id,
    arguments,
    cancellation,
    on_update,
) -> AgentToolResult:
    """执行加法。

    ``on_update`` 不是最终结果，只是让 UI 知道当前工具正在做什么。
    最终结果必须通过 return 返回。
    """

    cancellation.throw_if_cancelled()
    on_update(
        AgentToolResult(
            content=[{"type": "text", "text": "加法工具正在计算……"}],
            details={"phase": "calculating", "operation": "add"},
        )
    )

    # 主动让出一次事件循环，使流式 update 有机会先被 listener 处理。
    await asyncio.sleep(0)
    cancellation.throw_if_cancelled()

    a = arguments["a"]
    b = arguments["b"]
    value = a + b
    return AgentToolResult(
        content=[{"type": "text", "text": str(value)}],
        details={
            "operation": "add",
            "a": a,
            "b": b,
            "value": value,
            "toolCallId": tool_call_id,
        },
    )


def create_add_tool() -> AgentTool:
    """创建一份可注册到 Agent 的加法工具定义。"""

    return AgentTool(
        name="add",
        label="加法",
        description="计算两个数字 a 与 b 的和",
        parameters={
            "type": "object",
            "properties": {
                "a": {"type": "number", "description": "第一个加数"},
                "b": {"type": "number", "description": "第二个加数"},
            },
            "required": ["a", "b"],
            "additionalProperties": False,
        },
        validate_args=validate_two_numbers,
        execute=_execute_add,
        execution_mode="parallel",
    )
