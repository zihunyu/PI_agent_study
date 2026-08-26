"""业务 Capability 与实际 AgentTool 的独立注册和匹配。"""

from __future__ import annotations

from dataclasses import dataclass

from ..types import AgentTool


@dataclass(frozen=True, slots=True)
class ToolCapability:
    """一个工具向业务层声明的能力元数据。"""

    tool: AgentTool
    capabilities: frozenset[str]
    domain: str
    operation: str = "read"
    risk: str = "low"
    requires_approval: bool = False
    priority: int = 0

    def __post_init__(self) -> None:
        if not self.capabilities:
            raise ValueError(f"工具 {self.tool.name} 至少需要声明一个 capability")
        if any(not item.strip() for item in self.capabilities):
            raise ValueError("capability 不能是空字符串")


@dataclass(frozen=True, slots=True)
class CapabilityMatch:
    """一次能力匹配结果。"""

    tools: tuple[AgentTool, ...]
    missing_capabilities: tuple[str, ...]

    @property
    def available(self) -> bool:
        return not self.missing_capabilities


class CapabilityRegistry:
    """按业务能力寻找工具，而不是让 Router 依赖具体工具名。"""

    def __init__(self) -> None:
        self._entries: dict[str, ToolCapability] = {}

    def register(
        self,
        tool: AgentTool,
        *,
        capabilities: set[str] | frozenset[str],
        domain: str,
        operation: str = "read",
        risk: str = "low",
        requires_approval: bool = False,
        priority: int = 0,
    ) -> ToolCapability:
        if tool.name in self._entries:
            raise ValueError(f"CapabilityRegistry 已存在工具：{tool.name}")
        entry = ToolCapability(
            tool=tool,
            capabilities=frozenset(capabilities),
            domain=domain,
            operation=operation,
            risk=risk,
            requires_approval=requires_approval,
            priority=priority,
        )
        self._entries[tool.name] = entry
        return entry

    def all_tools(self) -> list[AgentTool]:
        return [entry.tool for entry in self._entries.values()]

    def tools_by_names(self, names: tuple[str, ...]) -> list[AgentTool]:
        tools: list[AgentTool] = []
        for name in names:
            entry = self._entries.get(name)
            if entry is None:
                raise KeyError(f"CapabilityRegistry 中不存在工具：{name}")
            tools.append(entry.tool)
        return tools

    def available_capabilities(self) -> frozenset[str]:
        values: set[str] = set()
        for entry in self._entries.values():
            values.update(entry.capabilities)
        return frozenset(values)

    def capabilities_for_tools(self, tool_names: set[str]) -> frozenset[str]:
        values: set[str] = set()
        for name in tool_names:
            entry = self._entries.get(name)
            if entry is not None:
                values.update(entry.capabilities)
        return frozenset(values)

    def match(self, required_capabilities: tuple[str, ...]) -> CapabilityMatch:
        """为每个所需能力选择优先级最高的工具，并去除重复工具。"""

        selected: dict[str, AgentTool] = {}
        missing: list[str] = []
        entries = sorted(
            self._entries.values(),
            key=lambda item: (-item.priority, item.tool.name),
        )
        for capability in required_capabilities:
            entry = next(
                (
                    candidate
                    for candidate in entries
                    if capability in candidate.capabilities
                ),
                None,
            )
            if entry is None:
                missing.append(capability)
            else:
                selected.setdefault(entry.tool.name, entry.tool)
        return CapabilityMatch(
            tools=tuple(selected.values()),
            missing_capabilities=tuple(missing),
        )
