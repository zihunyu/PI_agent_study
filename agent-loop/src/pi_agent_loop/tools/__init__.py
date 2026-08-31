"""通用 Tool 注册表以及可选的教学工具。

Calculator 只为兼容已有教学示例按需加载；删除这些示例模块不会再导致
``import pi_agent_loop`` 失败。真实业务 Tool 应放在独立子包中显式导入。
"""

from importlib import import_module
from typing import Any

from .registry import ToolRegistry


_LAZY_EXPORTS = {
    "AccumulatedOutput": (".output", "AccumulatedOutput"),
    "AtomicFileWriter": (".atomic_writer", "AtomicFileWriter"),
    "BuiltinToolRegistry": (".builtin_factory", "BuiltinToolRegistry"),
    "FileMutationQueue": (".mutation_queue", "FileMutationQueue"),
    "FileObservation": (".observations", "FileObservation"),
    "FileObservationStore": (".observations", "FileObservationStore"),
    "OutputAccumulator": (".output", "OutputAccumulator"),
    "OutputPolicy": (".output", "OutputPolicy"),
    "ProcessRunResult": (".process_runner", "ProcessRunResult"),
    "ProcessRunner": (".process_runner", "ProcessRunner"),
    "ToolSecurityProfile": (".services", "ToolSecurityProfile"),
    "ToolServices": (".services", "ToolServices"),
    "WorkspacePathPolicy": (".path_policy", "WorkspacePathPolicy"),
    "WorkspaceToolError": (".errors", "WorkspaceToolError"),
    "WorkspaceToolPreconditionError": (
        ".errors",
        "WorkspaceToolPreconditionError",
    ),
    "create_builtin_tools": (".builtin_factory", "create_builtin_tools"),
    "default_builtin_tool_registry": (
        ".builtin_factory",
        "default_builtin_tool_registry",
    ),
    "create_edit_tool": (".workspace_mutations", "create_edit_tool"),
    "create_find_tool": (".workspace_files", "create_find_tool"),
    "create_grep_tool": (".workspace_files", "create_grep_tool"),
    "create_list_dir_tool": (".workspace_files", "create_list_dir_tool"),
    "create_read_tool": (".workspace_files", "create_read_tool"),
    "create_shell_tool": (".workspace_shell", "create_shell_tool"),
    "create_workspace_tools": (".builtin_factory", "create_workspace_tools"),
    "create_write_tool": (".workspace_mutations", "create_write_tool"),
    "create_add_tool": (".add", "create_add_tool"),
    "create_divide_tool": (".divide", "create_divide_tool"),
    "create_multiply_tool": (".multiply", "create_multiply_tool"),
    "validate_division_args": (".validators", "validate_division_args"),
    "validate_two_numbers": (".validators", "validate_two_numbers"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def create_calculator_tools(
    *,
    add_delay_seconds: float = 0.0,
    multiply_delay_seconds: float = 0.0,
    divide_delay_seconds: float = 0.0,
):
    """创建加法、乘法、除法工具，可选教学模拟延时。"""

    add = __getattr__("create_add_tool")
    multiply = __getattr__("create_multiply_tool")
    divide = __getattr__("create_divide_tool")
    return [
        add(delay_seconds=add_delay_seconds),
        multiply(delay_seconds=multiply_delay_seconds),
        divide(delay_seconds=divide_delay_seconds),
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
    "AccumulatedOutput",
    "AtomicFileWriter",
    "BuiltinToolRegistry",
    "FileMutationQueue",
    "FileObservation",
    "FileObservationStore",
    "OutputAccumulator",
    "OutputPolicy",
    "ProcessRunResult",
    "ProcessRunner",
    "ToolSecurityProfile",
    "ToolServices",
    "WorkspacePathPolicy",
    "WorkspaceToolError",
    "WorkspaceToolPreconditionError",
    "create_add_tool",
    "create_builtin_tools",
    "create_calculator_registry",
    "create_calculator_tools",
    "default_builtin_tool_registry",
    "create_divide_tool",
    "create_edit_tool",
    "create_find_tool",
    "create_grep_tool",
    "create_list_dir_tool",
    "create_multiply_tool",
    "create_read_tool",
    "create_shell_tool",
    "create_workspace_tools",
    "create_write_tool",
    "validate_division_args",
    "validate_two_numbers",
]
