"""除法工具。

该工具演示一个完整只读计算能力：Schema、运行时校验、进度更新、取消、
独立 Timeout、结构化结果以及除零保护。
"""

from __future__ import annotations

import asyncio
import math

from ..types import AgentTool, AgentToolResult
from .validators import validate_division_args


async def _execute_divide(
    tool_call_id,
    arguments,
    cancellation,
    on_update,
    *,
    delay_seconds: float = 0.0,
) -> AgentToolResult:
    """计算被除数 a 除以除数 b。"""

    cancellation.throw_if_cancelled()
    on_update(
        AgentToolResult(
            content=[{"type": "text", "text": "除法工具正在计算……"}],
            details={"phase": "calculating", "operation": "divide"},
        )
    )
    await asyncio.sleep(delay_seconds)
    cancellation.throw_if_cancelled()

    a = arguments["a"]
    b = arguments["b"]
    value = a / b
    return AgentToolResult(
        content=[{"type": "text", "text": str(value)}],
        details={
            "operation": "divide",
            "a": a,
            "b": b,
            "value": value,
            "toolCallId": tool_call_id,
        },
    )


def create_divide_tool(*, delay_seconds: float = 0.0) -> AgentTool:
    """创建除法工具；传入大于 3 秒的模拟延时可触发独立 Timeout。"""

    if not math.isfinite(delay_seconds) or delay_seconds < 0:
        raise ValueError("除法工具 delay_seconds 必须是大于等于 0 的有限数字")

    async def execute(tool_call_id, arguments, cancellation, on_update):
        return await _execute_divide(
            tool_call_id,
            arguments,
            cancellation,
            on_update,
            delay_seconds=delay_seconds,
        )

    return AgentTool(
        name="divide",
        label="除法",
        description="计算被除数 a 除以非零除数 b 的商",
        parameters={
            "type": "object",
            "properties": {
                "a": {"type": "number", "description": "被除数"},
                "b": {
                    "type": "number",
                    "description": "非零除数",
                    "not": {"const": 0},
                },
            },
            "required": ["a", "b"],
            "additionalProperties": False,
        },
        validate_args=validate_division_args,
        execute=execute,
        execution_mode="parallel",
        timeout_seconds=3,
    )
