"""Agent Runtime 状态、事件、Reducer、Invariant 和 Projection。"""

from .events import RuntimeEvent, RuntimeEventType
from .invariants import RuntimeInvariantError, validate_runtime_transition
from .projection import project_runtime_state
from .reducer import reduce_runtime_state
from .states import RunPhase, RunState, ToolCallPhase, ToolCallState
from .tracker import RuntimeStateTracker

__all__ = [
    "RunPhase",
    "RunState",
    "RuntimeEvent",
    "RuntimeEventType",
    "RuntimeInvariantError",
    "RuntimeStateTracker",
    "ToolCallPhase",
    "ToolCallState",
    "project_runtime_state",
    "reduce_runtime_state",
    "validate_runtime_transition",
]
