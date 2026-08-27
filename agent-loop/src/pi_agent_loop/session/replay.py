"""从 Runtime Event 重放当前状态。"""

from __future__ import annotations

from ..runtime.events import RuntimeEvent
from ..runtime.reducer import reduce_runtime_state
from ..runtime.states import RunState


def replay_runtime_events(events: list[RuntimeEvent]) -> RunState:
    state = RunState()
    for event in sorted(events, key=lambda item: item.sequence):
        state = reduce_runtime_state(state, event)
    return state
