"""纯函数 Runtime Event Reducer。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .events import RuntimeEvent
from .invariants import RuntimeInvariantError, validate_runtime_transition
from .states import RunPhase, RunState, ToolCallPhase, ToolCallState


def reduce_runtime_state(state: RunState, event: RuntimeEvent) -> RunState:
    """应用一个已排序事件并返回新快照，不原地修改旧状态。"""

    validate_runtime_transition(state, event)
    base = replace(state, sequence=event.sequence, last_event=event.type)

    if event.type == "run_started":
        return RunState(
            phase="running",
            run_id=event.run_id,
            sequence=event.sequence,
            last_event=event.type,
        )
    if event.type == "run_interrupted":
        return replace(base, phase="suspended", failure_code="process_interrupted")
    if event.type == "routing_started":
        return replace(base, phase="routing", routing_status=None)
    if event.type == "routing_finished":
        status = _required_text(event.data, "status")
        return replace(base, phase="running", routing_status=status)
    if event.type == "turn_started":
        return replace(
            base,
            phase="requesting_model",
            turn=_positive_int(event.data.get("turn"), "turn"),
            model_retry_attempt=0,
        )
    if event.type == "model_request_started":
        return replace(base, phase="requesting_model")
    if event.type == "model_response_finished":
        stop_reason = event.data.get("stopReason")
        failure_code = base.failure_code
        if stop_reason == "error":
            failure_code = str(event.data.get("errorCode", "model_error"))
        elif stop_reason == "aborted":
            failure_code = "cancelled"
        elif stop_reason == "length":
            failure_code = "model_output_truncated"
        return replace(base, phase="running", failure_code=failure_code)
    if event.type == "model_retry_scheduled":
        return replace(
            base,
            phase="retrying",
            model_retry_attempt=_positive_int(event.data.get("attempt"), "attempt"),
        )
    if event.type == "model_retry_attempt_started":
        return replace(base, phase="requesting_model")
    if event.type == "model_retry_finished":
        if event.data.get("success") is False:
            return replace(
                base,
                phase="running",
                failure_code=str(event.data.get("finalError", "model_retry_failed")),
            )
        return replace(base, phase="requesting_model")
    if event.type == "context_compaction_started":
        return replace(base, phase="compacting")
    if event.type == "context_compaction_finished":
        return replace(base, phase="requesting_model")
    if event.type == "tool_started":
        tool_call_id = _required_text(event.data, "toolCallId")
        if tool_call_id in state.tools:
            raise RuntimeInvariantError(
                "duplicate_tool_start",
                f"Tool Call {tool_call_id} 重复开始",
            )
        tool = ToolCallState(
            tool_call_id=tool_call_id,
            tool_name=_required_text(event.data, "toolName"),
            phase="queued",
        )
        return replace(base, phase="executing_tools", tools={**state.tools, tool_call_id: tool})
    if event.type == "tool_dispatch_started":
        attempt = _positive_int(event.data.get("attempt"), "attempt")
        return _update_tool(
            base,
            event,
            phase="executing",
            retry_attempt=attempt - 1,
        )
    if event.type == "tool_retry_scheduled":
        return _update_tool(
            base,
            event,
            phase="retry_backoff",
            retry_attempt=_positive_int(event.data.get("attempt"), "attempt"),
        )
    if event.type == "tool_retry_attempt_started":
        return _update_tool(base, event, phase="executing")
    if event.type == "tool_retry_finished":
        return _update_tool(base, event, phase="executing")
    if event.type == "tool_finished":
        tool_call_id = _required_text(event.data, "toolCallId")
        current = state.tools[tool_call_id]
        success = event.data.get("success") is True
        error_code = None if success else str(event.data.get("errorCode", "tool_error"))
        if success:
            tool_phase: ToolCallPhase = "succeeded"
        elif error_code == "tool_timeout":
            tool_phase = "timed_out"
        elif error_code == "tool_cancelled":
            tool_phase = "cancelled"
        elif error_code == "outcome_unknown":
            tool_phase = "outcome_unknown"
        else:
            tool_phase = "failed"
        tools = {
            **state.tools,
            tool_call_id: replace(
                current,
                phase=tool_phase,
                error_code=error_code,
            ),
        }
        active = any(not tool.terminal for tool in tools.values())
        return replace(base, phase="executing_tools" if active else "running", tools=tools)
    if event.type == "turn_finished":
        return replace(base, phase="running")
    if event.type == "approval_required":
        return replace(base, phase="waiting_approval")
    if event.type == "approval_granted":
        return replace(base, phase="running")
    if event.type == "approval_rejected":
        return replace(base, phase="running", failure_code="approval_rejected")
    if event.type == "outcome_unknown":
        return replace(base, phase="outcome_unknown", failure_code="outcome_unknown")
    if event.type == "reconciliation_started":
        return replace(base, phase="outcome_unknown")
    if event.type == "reconciliation_finished":
        success = event.data.get("success") is True
        return replace(
            base,
            phase="running",
            failure_code=None if success else "reconciliation_failed",
        )
    if event.type == "budget_exceeded":
        return replace(base, phase="running", failure_code="budget_exceeded")
    if event.type == "run_finished":
        outcome = event.data.get("outcome")
        if outcome == "completed" and base.failure_code is None:
            run_phase: RunPhase = "completed"
        elif outcome == "cancelled" or base.failure_code == "cancelled":
            run_phase = "cancelled"
        else:
            run_phase = "failed"
        if run_phase == "completed" and any(
            not tool.terminal for tool in state.tools.values()
        ):
            raise RuntimeInvariantError(
                "unfinished_tools_at_completion",
                "Run 完成时仍存在未结束 Tool Call",
            )
        return replace(base, phase=run_phase)

    raise RuntimeInvariantError("unknown_runtime_event", f"未知 Runtime Event：{event.type}")


def _update_tool(
    state: RunState,
    event: RuntimeEvent,
    *,
    phase: ToolCallPhase,
    retry_attempt: int | None = None,
) -> RunState:
    tool_call_id = _required_text(event.data, "toolCallId")
    current = state.tools[tool_call_id]
    updated = replace(
        current,
        phase=phase,
        retry_attempt=current.retry_attempt if retry_attempt is None else retry_attempt,
    )
    return replace(
        state,
        phase="executing_tools",
        tools={**state.tools, tool_call_id: updated},
    )


def _required_text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeInvariantError("invalid_event_data", f"事件缺少 {key}")
    return value


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RuntimeInvariantError("invalid_event_data", f"{name} 必须是正整数")
    return value
