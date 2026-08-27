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
import math

from ..types import AgentTool, AgentToolResult
from .validators import validate_two_numbers


async def _execute_add(
    tool_call_id,
    arguments,
    cancellation,
    on_update,
    *,
    delay_seconds: float = 0.0,
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

    # delay_seconds 只用于教学演示超时。默认 0 表示立即计算；例如设成 3，
    # 就会超过加法工具的 2 秒限制，从而触发独立 Tool Timeout。
    await asyncio.sleep(delay_seconds)
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


def create_add_tool(*, delay_seconds: float = 0.0) -> AgentTool:
    """创建加法工具。

    ``delay_seconds`` 是教学用模拟延时，不是模型参数。默认立即执行；传入
    大于 2 的值可以在示例中触发加法工具的 2 秒 Timeout。
    """

    if not math.isfinite(delay_seconds) or delay_seconds < 0:
        raise ValueError("加法工具 delay_seconds 必须是大于等于 0 的有限数字")

    async def execute(tool_call_id, arguments, cancellation, on_update):
        return await _execute_add(
            tool_call_id,
            arguments,
            cancellation,
            on_update,
            delay_seconds=delay_seconds,
        )

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
        execute=execute,
        execution_mode="parallel",
        # 加法工具最多执行 2 秒；超时只取消当前加法，不影响并行工具。
        timeout_seconds=2,
        replay_policy="safe",
    )
