"""工具注册表。

Agent 最终只需要 ``list[AgentTool]``。注册表不是执行器，它只负责集中保存、
检查重名并导出工具列表，避免应用在不同位置手工拼接很多工具。
"""

from __future__ import annotations

from collections.abc import Iterable

from ..types import AgentTool


class ToolRegistry:
    """按工具名称保存 AgentTool 的简单注册表。"""

    def __init__(self) -> None:
        self._tools: dict[str, AgentTool] = {}

    def register(self, tool: AgentTool) -> None:
        """注册一个工具；重名时明确报错，不静默覆盖。"""

        if not tool.name.strip():
            raise ValueError("工具名称不能为空")
        if tool.name in self._tools:
            raise ValueError(f"工具已经注册：{tool.name}")
        self._tools[tool.name] = tool

    def register_many(self, tools: Iterable[AgentTool]) -> None:
        """按传入顺序注册多个工具。"""

        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> AgentTool | None:
        """按名称取得工具；不存在时返回 None。"""

        return self._tools.get(name)

    def all(self) -> list[AgentTool]:
        """返回注册顺序稳定的工具列表，供 Agent 构造函数使用。"""

        return list(self._tools.values())

    def names(self) -> list[str]:
        """返回全部已注册工具名称。"""

        return list(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)
