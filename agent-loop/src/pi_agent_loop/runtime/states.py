"""通用 Agent Run 与 Tool Call 状态定义。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeAlias

RunPhase: TypeAlias = Literal[
    "idle",
    "running",
    "routing",
    "requesting_model",
    "executing_tools",
    "retrying",
    "compacting",
    "waiting_approval",
    "outcome_unknown",
    "completed",
    "failed",
    "cancelled",
    "suspended",
]
ToolCallPhase: TypeAlias = Literal[
    "queued",
    "executing",
    "retry_backoff",
    "succeeded",
    "failed",
    "timed_out",
    "cancelled",
    "outcome_unknown",
]

_TERMINAL_RUN_PHASES = frozenset({"completed", "failed", "cancelled", "suspended"})
_TERMINAL_TOOL_PHASES = frozenset(
    {"succeeded", "failed", "timed_out", "cancelled", "outcome_unknown"}
)


@dataclass(frozen=True, slots=True)
class ToolCallState:
    tool_call_id: str
    tool_name: str
    phase: ToolCallPhase
    retry_attempt: int = 0
    error_code: str | None = None

    @property
    def terminal(self) -> bool:
        return self.phase in _TERMINAL_TOOL_PHASES


@dataclass(frozen=True, slots=True)
class RunState:
    """由 RuntimeEvent 归约出的当前运行快照。"""

    phase: RunPhase = "idle"
    run_id: str | None = None
    sequence: int = -1
    turn: int = 0
    model_retry_attempt: int = 0
    tools: dict[str, ToolCallState] = field(default_factory=dict)
    failure_code: str | None = None
    routing_status: str | None = None
    last_event: str | None = None

    @property
    def terminal(self) -> bool:
        return self.phase in _TERMINAL_RUN_PHASES

    @property
    def active_tool_count(self) -> int:
        return sum(1 for tool in self.tools.values() if not tool.terminal)
