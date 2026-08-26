"""乘法工具。"""

from __future__ import annotations

import asyncio

from ..types import AgentTool, AgentToolResult
from .validators import validate_two_numbers


async def _execute_multiply(
    tool_call_id,
    arguments,
    cancellation,
    on_update,
) -> AgentToolResult:
    """执行乘法并返回模型可读取的文本结果。"""

    cancellation.throw_if_cancelled()
    on_update(
        AgentToolResult(
            content=[{"type": "text", "text": "乘法工具正在计算……"}],
            details={"phase": "calculating", "operation": "multiply"},
        )
    )
    await asyncio.sleep(0)
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


def create_multiply_tool() -> AgentTool:
    """创建一份可注册到 Agent 的乘法工具定义。"""

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
        execute=_execute_multiply,
        execution_mode="parallel",
    )
