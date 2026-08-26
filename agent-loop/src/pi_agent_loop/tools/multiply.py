"""乘法工具。"""

from __future__ import annotations

import asyncio
import math

from ..types import AgentTool, AgentToolResult
from .validators import validate_two_numbers


async def _execute_multiply(
    tool_call_id,
    arguments,
    cancellation,
    on_update,
    *,
    delay_seconds: float = 0.0,
) -> AgentToolResult:
    """执行乘法并返回模型可读取的文本结果。"""

    cancellation.throw_if_cancelled()
    on_update(
        AgentToolResult(
            content=[{"type": "text", "text": "乘法工具正在计算……"}],
            details={"phase": "calculating", "operation": "multiply"},
        )
    )
    # 默认 0 表示立即计算；例如设成 6，会超过乘法工具 5 秒限制。
    await asyncio.sleep(delay_seconds)
    cancellation.throw_if_cancelled()

    a = arguments["a"]
    b = arguments["b"]
    value = a * b
    return AgentToolResult(
        content=[{"type": "text", "text": str(value)}],
        details={
            "operation": "multiply",
            "a": a,
            "b": b,
            "value": value,
            "toolCallId": tool_call_id,
        },
    )


def create_multiply_tool(*, delay_seconds: float = 0.0) -> AgentTool:
    """创建乘法工具。

    ``delay_seconds`` 是教学用模拟延时。传入大于 5 的值可以触发乘法工具
    的 5 秒 Timeout。
    """

    if not math.isfinite(delay_seconds) or delay_seconds < 0:
        raise ValueError("乘法工具 delay_seconds 必须是大于等于 0 的有限数字")

    async def execute(tool_call_id, arguments, cancellation, on_update):
        return await _execute_multiply(
            tool_call_id,
            arguments,
            cancellation,
            on_update,
            delay_seconds=delay_seconds,
        )

    return AgentTool(
        name="multiply",
        label="乘法",
        description="计算两个数字 a 与 b 的积",
        parameters={
            "type": "object",
            "properties": {
                "a": {"type": "number", "description": "第一个乘数"},
                "b": {"type": "number", "description": "第二个乘数"},
            },
            "required": ["a", "b"],
            "additionalProperties": False,
        },
        validate_args=validate_two_numbers,
        execute=execute,
        execution_mode="parallel",
        # 乘法工具最多执行 5 秒；超时只取消当前乘法，不影响并行工具。
        timeout_seconds=5,
    )
