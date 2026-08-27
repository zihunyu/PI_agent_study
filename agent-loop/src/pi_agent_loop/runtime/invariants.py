"""Runtime Event 状态转换不变量。"""

from __future__ import annotations

from .events import RuntimeEvent
from .states import RunState


class RuntimeInvariantError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_runtime_transition(state: RunState, event: RuntimeEvent) -> None:
    expected_sequence = state.sequence + 1
    if event.sequence != expected_sequence:
        raise RuntimeInvariantError(
            "non_consecutive_event",
            f"Runtime Event sequence={event.sequence}，预期 {expected_sequence}",
        )

    if event.type == "run_started":
        if state.phase not in {"idle", "completed", "failed", "cancelled", "suspended"}:
            raise RuntimeInvariantError(
                "run_already_active",
                f"状态 {state.phase} 下不能开始新 Run",
            )
        return

    if state.run_id is None:
        raise RuntimeInvariantError("no_active_run", f"事件 {event.type} 没有活动 Run")
    if event.run_id != state.run_id:
        raise RuntimeInvariantError(
            "run_id_mismatch",
            f"事件属于 {event.run_id}，当前 Run 是 {state.run_id}",
        )
    if state.terminal:
        raise RuntimeInvariantError(
            "event_after_terminal",
            f"Run 已处于终态 {state.phase}，不能接收 {event.type}",
        )

    if event.type in {
        "tool_dispatch_started",
        "tool_retry_scheduled",
        "tool_retry_attempt_started",
        "tool_retry_finished",
        "tool_finished",
    }:
        tool_call_id = event.data.get("toolCallId")
        if not isinstance(tool_call_id, str) or tool_call_id not in state.tools:
            raise RuntimeInvariantError(
                "unknown_tool_call",
                f"事件 {event.type} 引用了未知 Tool Call：{tool_call_id}",
            )
        if state.tools[tool_call_id].terminal:
            raise RuntimeInvariantError(
                "tool_event_after_terminal",
                f"Tool Call {tool_call_id} 已结束，不能接收 {event.type}",
            )

    if event.type == "approval_granted" and state.phase != "waiting_approval":
        raise RuntimeInvariantError(
            "approval_not_waiting",
            "只有 waiting_approval 状态才能批准",
        )
    if event.type == "approval_rejected" and state.phase != "waiting_approval":
        raise RuntimeInvariantError(
            "approval_not_waiting",
            "只有 waiting_approval 状态才能拒绝",
        )
