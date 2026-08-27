"""当前教学阶段的内置工具。

当前包含加法、乘法和除法，用于清楚演示：

模型生成 toolCall -> Registry 找到工具 -> Agent Loop 执行 -> 结果回灌模型。
"""

from .add import create_add_tool
from .divide import create_divide_tool
from .multiply import create_multiply_tool
from .registry import ToolRegistry
from .validators import validate_division_args, validate_two_numbers


def create_calculator_tools(
    *,
    add_delay_seconds: float = 0.0,
    multiply_delay_seconds: float = 0.0,
    divide_delay_seconds: float = 0.0,
):
    """创建加法、乘法、除法工具，可选教学模拟延时。"""

    return [
        create_add_tool(delay_seconds=add_delay_seconds),
        create_multiply_tool(delay_seconds=multiply_delay_seconds),
        create_divide_tool(delay_seconds=divide_delay_seconds),
    ]


def create_calculator_registry(
    *,
    add_delay_seconds: float = 0.0,
    multiply_delay_seconds: float = 0.0,
    divide_delay_seconds: float = 0.0,
) -> ToolRegistry:
    """创建并注册加法、乘法、除法工具的注册表。"""

    registry = ToolRegistry()
    registry.register_many(
        create_calculator_tools(
            add_delay_seconds=add_delay_seconds,
            multiply_delay_seconds=multiply_delay_seconds,
            divide_delay_seconds=divide_delay_seconds,
        )
    )
    return registry


__all__ = [
    "ToolRegistry",
    "create_add_tool",
    "create_calculator_registry",
    "create_calculator_tools",
    "create_divide_tool",
    "create_multiply_tool",
    "validate_division_args",
    "validate_two_numbers",
]
