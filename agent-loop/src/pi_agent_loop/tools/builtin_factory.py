"""工作区内置工具的 profile-aware 工厂。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

from ..types import AgentTool
from .services import ToolSecurityProfile, ToolServices
from .workspace_files import (
    create_find_tool,
    create_grep_tool,
    create_list_dir_tool,
    create_read_tool,
)
from .workspace_mutations import create_edit_tool, create_write_tool
from .workspace_shell import create_shell_tool


BuiltinToolFactory = Callable[[ToolServices], AgentTool]


class BuiltinToolRegistry:
    """只负责装配；执行和安全检查仍由各 Tool/Service 完成。"""

    def __init__(self) -> None:
        self._factories: dict[str, BuiltinToolFactory] = {}

    def register(self, name: str, factory: BuiltinToolFactory) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Builtin Tool 名称不能为空")
        if name in self._factories:
            raise ValueError(f"Builtin Tool Factory 已注册：{name}")
        self._factories[name] = factory

    def create(
        self,
        services: ToolServices,
        names: Iterable[str] | None = None,
    ) -> list[AgentTool]:
        selected = tuple(names) if names is not None else services.allowed_tool_names
        if len(set(selected)) != len(selected):
            raise ValueError("Builtin Tool names 不能重复")
        disallowed = set(selected) - set(services.allowed_tool_names)
        if disallowed:
            raise ValueError(
                f"Profile {services.security_profile} 不允许工具："
                + ", ".join(sorted(disallowed))
            )
        missing = set(selected) - set(self._factories)
        if missing:
            raise ValueError("未知 Builtin Tool：" + ", ".join(sorted(missing)))
        return [self._factories[name](services) for name in selected]


def default_builtin_tool_registry() -> BuiltinToolRegistry:
    registry = BuiltinToolRegistry()
    for name, factory in (
        ("read", create_read_tool),
        ("list_dir", create_list_dir_tool),
        ("find", create_find_tool),
        ("grep", create_grep_tool),
        ("write", create_write_tool),
        ("edit", create_edit_tool),
        ("shell", create_shell_tool),
    ):
        registry.register(name, factory)
    return registry


def create_builtin_tools(
    services: ToolServices,
    names: Iterable[str] | None = None,
) -> list[AgentTool]:
    """按 profile 创建工具；默认 profile 只启用四个 workspace 只读工具。"""

    return default_builtin_tool_registry().create(services, names)


def create_workspace_tools(
    workspace: str | Path,
    *,
    security_profile: ToolSecurityProfile = "read-only",
    allow_trusted_shell: bool = False,
) -> tuple[ToolServices, list[AgentTool]]:
    """便捷工厂。

    ``full-access`` Shell 不是沙箱，必须同时传入
    ``allow_trusted_shell=True`` 才会创建。``workspace-write`` 返回的本地文件
    写工具仍要求 Agent/Host 提供可信身份与 Approval authorization；本便捷工厂
    不会伪造或自动批准写操作。
    """

    services = ToolServices.create(
        workspace,
        security_profile=security_profile,
        allow_trusted_shell=allow_trusted_shell,
    )
    return services, create_builtin_tools(services)
