"""Agent 配置文件读取与严格校验。

Python 3.11 标准库提供 tomllib，可以读取 TOML 而无需第三方依赖。配置对象
使用冻结 dataclass，运行开始后不会被其他代码意外原地修改。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class AgentLimits:
    """一次 Agent 运行的轮次、工具总数和工具并行数限制。"""

    max_tool_calls: int
    max_parallel_tools: int
    max_turns: int

    def __post_init__(self) -> None:
        _validate_positive_integer("max_tool_calls", self.max_tool_calls)
        _validate_positive_integer("max_parallel_tools", self.max_parallel_tools)
        _validate_positive_integer("max_turns", self.max_turns)


def _validate_positive_integer(name: str, value: Any) -> int:
    """拒绝 bool、0、负数和小数，返回经过类型收窄的正整数。"""

    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"配置 {name} 必须是大于 0 的整数，当前值：{value!r}")
    return value


def load_agent_limits(path: str | Path) -> AgentLimits:
    """从 TOML 文件的 ``[limits]`` 分区加载运行限制。"""

    config_path = Path(path)
    try:
        with config_path.open("rb") as file:
            data = tomllib.load(file)
    except FileNotFoundError as error:
        raise FileNotFoundError(f"Agent 配置文件不存在：{config_path}") from error
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"Agent 配置文件 TOML 格式错误：{error}") from error

    limits = data.get("limits")
    if not isinstance(limits, dict):
        raise ValueError("Agent 配置文件必须包含 [limits] 分区")

    required = {"max_tool_calls", "max_parallel_tools", "max_turns"}
    missing = sorted(required - limits.keys())
    if missing:
        raise ValueError(f"Agent 配置缺少字段：{', '.join(missing)}")

    # 限制分区中出现拼写错误时明确失败，避免用户以为配置已经生效。
    unknown = sorted(limits.keys() - required)
    if unknown:
        raise ValueError(f"Agent 配置包含未知字段：{', '.join(unknown)}")

    return AgentLimits(
        max_tool_calls=_validate_positive_integer(
            "max_tool_calls", limits["max_tool_calls"]
        ),
        max_parallel_tools=_validate_positive_integer(
            "max_parallel_tools", limits["max_parallel_tools"]
        ),
        max_turns=_validate_positive_integer("max_turns", limits["max_turns"]),
    )
