"""检测崩溃时未结束的 Run，并追加挂起事件。"""

from __future__ import annotations

from ..runtime.events import RuntimeEvent
from ..runtime.states import RunState
from .replay import replay_runtime_events
from .store import RuntimeEventStore


class RuntimeRecoveryManager:
    def __init__(self, store: RuntimeEventStore) -> None:
        self.store = store

    async def recover(self) -> RunState:
        events = await self.store.load()
        state = replay_runtime_events(events)
        if state.run_id is None or state.phase in {
            "idle",
            "completed",
            "failed",
            "cancelled",
            "suspended",
        }:
            return state
        interrupted = RuntimeEvent(
            type="run_interrupted",
            run_id=state.run_id,
            sequence=state.sequence + 1,
            data={"reason": "process_restart"},
        )
        # 先验证归约，再持久化，避免把非法转换写入日志。
        recovered = replay_runtime_events([*events, interrupted])
        await self.store.append(interrupted)
        return recovered
