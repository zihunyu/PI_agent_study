"""业务 Capability 与实际 AgentTool 的独立注册和匹配。"""

from __future__ import annotations

from dataclasses import dataclass

from ..types import AgentTool
from .types import RiskLevel, highest_risk_level, validate_risk_level


@dataclass(frozen=True, slots=True)
class ToolCapability:
    """一个工具向业务层声明的能力元数据。"""

    tool: AgentTool
    capabilities: frozenset[str]
    domain: str
    operation: str = "read"
    risk: RiskLevel = "low"
    requires_approval: bool = False
    priority: int = 0
    side_effect: bool | None = None

    def __post_init__(self) -> None:
        if not self.capabilities:
            raise ValueError(f"工具 {self.tool.name} 至少需要声明一个 capability")
        if any(not item.strip() for item in self.capabilities):
            raise ValueError("capability 不能是空字符串")
        if self.side_effect is not None and not isinstance(self.side_effect, bool):
            raise ValueError("side_effect 必须是 bool 或 None")
        normalized_risk = validate_risk_level(self.risk)
        if normalized_risk != self.risk:
            object.__setattr__(self, "risk", normalized_risk)
        if self.has_side_effect and self.tool.replay_policy != "never":
            raise ValueError(
                f"有副作用 Capability {self.tool.name} 必须绑定 "
                "replay_policy=never 的 Tool"
            )

    @property
    def has_side_effect(self) -> bool:
        """Return explicit policy, with operation retained as legacy fallback."""

        if self.side_effect is not None:
            return self.side_effect
        return self.operation.casefold() != "read"

    @property
    def approval_required(self) -> bool:
        """Combine Tool, capability, side-effect and risk policy fail-closed."""

        return (
            self.tool.requires_approval
            or self.requires_approval
            or self.has_side_effect
            or self.risk in {"high", "critical"}
        )


@dataclass(frozen=True, slots=True)
class CapabilityMatch:
    """一次能力匹配结果。"""

    tools: tuple[AgentTool, ...]
    missing_capabilities: tuple[str, ...]
    candidate_entries: tuple[ToolCapability, ...] = ()

    @property
    def available(self) -> bool:
        return not self.missing_capabilities

    @property
    def requires_approval(self) -> bool:
        """Conservatively union the policy of every matching implementation.

        Capability metadata is a Host-owned security boundary.  A lower-priority
        implementation must not silently remove an approval requirement from a
        higher-priority implementation (or vice versa), so routing fails closed
        whenever any candidate declares approval.
        """

        return any(entry.approval_required for entry in self.candidate_entries)

    @property
    def has_side_effect(self) -> bool:
        return any(entry.has_side_effect for entry in self.candidate_entries)

    @property
    def risk(self) -> RiskLevel:
        return highest_risk_level(
            [entry.risk for entry in self.candidate_entries]
        )


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
        risk: RiskLevel = "low",
        requires_approval: bool = False,
        priority: int = 0,
        side_effect: bool | None = None,
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
            side_effect=side_effect,
        )
        self._entries[tool.name] = entry
        return entry

    def all_tools(self) -> list[AgentTool]:
        return [entry.tool for entry in self._entries.values()]

    def all_entries(self) -> tuple[ToolCapability, ...]:
        """返回不可变注册快照，供 Host 做最终安全策略校验。"""

        return tuple(self._entries.values())

    def entries_by_names(self, names: tuple[str, ...]) -> tuple[ToolCapability, ...]:
        entries: list[ToolCapability] = []
        for name in names:
            entry = self._entries.get(name)
            if entry is None:
                raise KeyError(f"CapabilityRegistry 中不存在工具：{name}")
            entries.append(entry)
        return tuple(entries)

    def approval_required_for_tools(self, names: tuple[str, ...]) -> bool:
        """Tool 与 Capability 元数据取并集，任一要求审批即 fail-closed。"""

        return any(entry.approval_required for entry in self.entries_by_names(names))

    def tools_by_names(self, names: tuple[str, ...]) -> list[AgentTool]:
        return [entry.tool for entry in self.entries_by_names(names)]

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
        candidates: dict[str, ToolCapability] = {}
        missing: list[str] = []
        entries = sorted(
            self._entries.values(),
            key=lambda item: (-item.priority, item.tool.name),
        )
        for capability in required_capabilities:
            matching = tuple(
                candidate
                for candidate in entries
                if capability in candidate.capabilities
            )
            entry = matching[0] if matching else None
            if entry is None:
                missing.append(capability)
            else:
                selected.setdefault(entry.tool.name, entry.tool)
                for candidate in matching:
                    candidates.setdefault(candidate.tool.name, candidate)
        return CapabilityMatch(
            tools=tuple(selected.values()),
            missing_capabilities=tuple(missing),
            candidate_entries=tuple(candidates.values()),
        )
