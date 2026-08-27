"""从 Operation Event 重建完整消息 Context 和未完成工作。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .operation_events import OperationEvent


class OperationLogInvariantError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ModelRequestState:
    request_id: str
    phase: str
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ToolInvocationState:
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    replay_policy: str
    phase: str = "intent_recorded"
    attempts: int = 0
    result: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class OperationState:
    session_id: str
    operation_id: str
    phase: str = "running"
    last_sequence: int = -1
    messages: tuple[dict[str, Any], ...] = ()
    model_requests: dict[str, ModelRequestState] = field(default_factory=dict)
    tools: dict[str, ToolInvocationState] = field(default_factory=dict)
    configuration: dict[str, Any] = field(default_factory=dict)
    outcome: str | None = None


def reduce_operation_event(
    state: OperationState | None,
    event: OperationEvent,
) -> OperationState:
    if state is None:
        if event.type != "operation_started":
            raise OperationLogInvariantError("第一条事件必须是 operation_started")
        return OperationState(
            session_id=event.session_id,
            operation_id=event.operation_id,
            last_sequence=event.sequence,
            configuration={
                **dict(event.data.get("configuration", {})),
                "tools": list(event.data.get("tools", [])),
            },
        )
    if event.session_id != state.session_id or event.operation_id != state.operation_id:
        raise OperationLogInvariantError("Operation Event 身份不一致")
    if event.sequence <= state.last_sequence:
        raise OperationLogInvariantError("Operation Event sequence 必须递增")
    if state.phase in {"completed", "failed", "cancelled"}:
        raise OperationLogInvariantError("Operation 终态后不能追加事件")
    base = replace(state, last_sequence=event.sequence)

    if event.type == "message_appended":
        message = _message(event.data)
        return replace(base, messages=(*state.messages, message))
    if event.type == "model_request_started":
        request_id = _text(event.data, "requestId")
        if request_id in state.model_requests:
            raise OperationLogInvariantError("Model Request ID 重复")
        request = ModelRequestState(request_id, "started")
        return replace(base, model_requests={**state.model_requests, request_id: request})
    if event.type in {"model_request_completed", "model_request_failed"}:
        request_id = _text(event.data, "requestId")
        current = state.model_requests.get(request_id)
        if current is None or current.phase != "started":
            raise OperationLogInvariantError("Model Request 完成事件没有对应 Started")
        if event.type == "model_request_completed":
            message = _message(event.data)
            request = replace(current, phase="completed")
            return replace(
                base,
                model_requests={**state.model_requests, request_id: request},
                messages=(*state.messages, message),
            )
        request = replace(
            current,
            phase="failed",
            error_code=str(event.data.get("errorCode", "model_error")),
        )
        return replace(base, model_requests={**state.model_requests, request_id: request})
    if event.type == "tool_intent_recorded":
        tool_call_id = _text(event.data, "toolCallId")
        if tool_call_id in state.tools:
            raise OperationLogInvariantError("Tool Intent 重复")
        invocation = ToolInvocationState(
            tool_call_id=tool_call_id,
            tool_name=_text(event.data, "toolName"),
            arguments=dict(event.data.get("arguments", {})),
            replay_policy=str(event.data.get("replayPolicy", "never")),
        )
        return replace(base, tools={**state.tools, tool_call_id: invocation})
    if event.type == "tool_dispatch_started":
        tool_call_id = _text(event.data, "toolCallId")
        current = _tool(state, tool_call_id)
        if current.phase not in {"intent_recorded", "dispatch_started"}:
            raise OperationLogInvariantError("Tool 已结束，不能再次 Dispatch")
        invocation = replace(
            current,
            phase="dispatch_started",
            attempts=current.attempts + 1,
        )
        return replace(base, tools={**state.tools, tool_call_id: invocation})
    if event.type in {"tool_completed", "tool_outcome_unknown", "tool_reconciled"}:
        tool_call_id = _text(event.data, "toolCallId")
        current = _tool(state, tool_call_id)
        allowed_phases = (
            {"outcome_unknown", "dispatch_started"}
            if event.type == "tool_reconciled"
            else {"intent_recorded", "dispatch_started"}
        )
        if current.phase not in allowed_phases:
            raise OperationLogInvariantError("Tool Result 重复或状态不允许")
        phase = "outcome_unknown" if event.type == "tool_outcome_unknown" else "completed"
        invocation = replace(
            current,
            phase=phase,
            result=dict(event.data.get("result", {})),
        )
        return replace(base, tools={**state.tools, tool_call_id: invocation})
    if event.type == "operation_finished":
        outcome = str(event.data.get("outcome", "failed"))
        if outcome not in {"completed", "failed", "cancelled"}:
            raise OperationLogInvariantError("Operation outcome 无效")
        return replace(base, phase=outcome, outcome=outcome)

    # Routing、Approval、Write 事件由各自状态机消费，但仍存在同一日志。
    if (
        event.type.startswith("routing_")
        or event.type.startswith("approval_")
        or event.type.startswith("write_")
    ):
        return base
    raise OperationLogInvariantError(f"未知 Operation Event：{event.type}")


def replay_operation(events: list[OperationEvent]) -> OperationState:
    state: OperationState | None = None
    for event in sorted(events, key=lambda item: item.sequence):
        state = reduce_operation_event(state, event)
    if state is None:
        raise OperationLogInvariantError("Operation Log 为空")
    return state


def _text(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise OperationLogInvariantError(f"事件缺少 {key}")
    return value


def _message(data: dict[str, Any]) -> dict[str, Any]:
    message = data.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("role"), str):
        raise OperationLogInvariantError("事件缺少合法 message")
    return dict(message)


def _tool(state: OperationState, tool_call_id: str) -> ToolInvocationState:
    invocation = state.tools.get(tool_call_id)
    if invocation is None:
        raise OperationLogInvariantError("Tool Event 没有对应 Intent")
    return invocation
