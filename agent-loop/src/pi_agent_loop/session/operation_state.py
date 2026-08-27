"""从 Operation Event 重建完整消息 Context 和未完成工作。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any

from ..model_policy import ModelRequestPolicy, ModelRequestPolicyError
from ..transcript import (
    TranscriptIntegrityError,
    validate_closed_tool_call_transcript,
)
from .operation_events import OperationEvent


class OperationLogInvariantError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ModelRequestState:
    request_id: str
    phase: str
    policy: ModelRequestPolicy | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalSnapshot:
    approval_id: str
    state: str
    action_hash: str
    requester_id: str
    required_role: str
    action: dict[str, Any] = field(default_factory=dict)
    tool_call_id: str | None = None
    consumer_id: str | None = None
    resume_state: str = "registered"


@dataclass(frozen=True, slots=True)
class WriteSnapshot:
    write_id: str
    state: str
    tool_name: str
    arguments: dict[str, Any]
    action_hash: str
    idempotency_key_hash: str
    tool_call_id: str | None = None
    approval_id: str | None = None
    result: dict[str, Any] | None = None


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
    approvals: dict[str, ApprovalSnapshot] = field(default_factory=dict)
    writes: dict[str, WriteSnapshot] = field(default_factory=dict)
    configuration: dict[str, Any] = field(default_factory=dict)
    active_model_policy: ModelRequestPolicy | None = None
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
    if event.type == "model_policy_selected":
        policy = _model_policy(event.data.get("policy"))
        return replace(base, active_model_policy=policy)
    if event.type == "model_request_started":
        request_id = _text(event.data, "requestId")
        if request_id in state.model_requests:
            raise OperationLogInvariantError("Model Request ID 重复")
        raw_policy = event.data.get("requestPolicy")
        policy = (
            _model_policy(raw_policy)
            if raw_policy is not None
            else state.active_model_policy
        )
        request = ModelRequestState(request_id, "started", policy=policy)
        return replace(
            base,
            active_model_policy=policy or state.active_model_policy,
            model_requests={**state.model_requests, request_id: request},
        )
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
        approval = _approval_for_tool_call(state, tool_call_id)
        if approval is not None and approval.action:
            _validate_approval_action(approval, invocation.tool_name, invocation.arguments)
        return replace(base, tools={**state.tools, tool_call_id: invocation})
    if event.type == "tool_dispatch_started":
        tool_call_id = _text(event.data, "toolCallId")
        current = _tool(state, tool_call_id)
        approval = _approval_for_tool_call(state, tool_call_id)
        if approval is not None and approval.state not in {"consumed", "resume_started"}:
            raise OperationLogInvariantError(
                f"Tool {tool_call_id} 仍在等待 Approval，禁止 Dispatch"
            )
        if approval is not None and approval.action:
            _validate_approval_action(approval, current.tool_name, current.arguments)
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
    if event.type.startswith("approval_"):
        return _reduce_approval_event(base, event)
    if event.type.startswith("write_"):
        return _reduce_write_event(base, event)
    if event.type == "operation_finished":
        pending_requests = [
            request
            for request in state.model_requests.values()
            if request.phase == "started"
        ]
        if pending_requests:
            raise OperationLogInvariantError(
                "Operation Finished 时仍有未结束 Model Request"
            )
        try:
            validate_closed_tool_call_transcript(list(state.messages))
        except TranscriptIntegrityError as error:
            raise OperationLogInvariantError(
                f"Operation Finished 时 Tool Call 未闭合：{error}"
            ) from error
        outcome = str(event.data.get("outcome", "failed"))
        if outcome not in {"completed", "failed", "cancelled"}:
            raise OperationLogInvariantError("Operation outcome 无效")
        if outcome == "completed":
            blocking_approvals = [
                approval.approval_id
                for approval in state.approvals.values()
                if approval.state in {"waiting", "approved", "resume_started"}
                or (
                    approval.state == "consumed"
                    and approval.resume_state != "unregistered"
                )
            ]
            if blocking_approvals:
                raise OperationLogInvariantError(
                    "Operation Completed 时仍有未完成 Approval Resume："
                    + ", ".join(blocking_approvals)
                )
            blocking_writes = [
                write.write_id
                for write in state.writes.values()
                if write.state not in {"succeeded", "failed"}
            ]
            if blocking_writes:
                raise OperationLogInvariantError(
                    "Operation Completed 时仍有未完成 Write："
                    + ", ".join(blocking_writes)
                )
        return replace(base, phase=outcome, outcome=outcome)

    # Routing 事件只影响独立 Runtime Projection。
    if event.type.startswith("routing_"):
        return base
    raise OperationLogInvariantError(f"未知 Operation Event：{event.type}")


def _reduce_approval_event(
    state: OperationState,
    event: OperationEvent,
) -> OperationState:
    if event.type == "approval_pending":
        return replace(state, phase="waiting_approval")
    approval_id = _text(event.data, "approvalId")
    current = state.approvals.get(approval_id)
    if event.type == "approval_requested":
        if current is not None:
            raise OperationLogInvariantError("Approval Request 重复")
        approval = ApprovalSnapshot(
            approval_id=approval_id,
            state="waiting",
            action_hash=_text(event.data, "actionHash"),
            requester_id=_text(event.data, "requesterId"),
            required_role=_text(event.data, "requiredRole"),
            resume_state="unregistered",
        )
        return replace(
            state,
            phase="waiting_approval",
            approvals={**state.approvals, approval_id: approval},
        )
    if current is None:
        raise OperationLogInvariantError("Approval Event 缺少 Request")

    if event.type == "approval_resume_registered":
        if current.resume_state != "unregistered":
            raise OperationLogInvariantError("Approval Resume Registered 重复")
        action = dict(event.data.get("action", {}))
        if _action_digest(action) != current.action_hash:
            raise OperationLogInvariantError("Approval Resume Action Hash 不匹配")
        payload = dict(event.data.get("resumePayload", {}))
        approval = replace(
            current,
            action=action,
            tool_call_id=(
                str(payload.get("toolCallId"))
                if payload.get("toolCallId") is not None
                else None
            ),
            resume_state="registered",
        )
        return replace(
            state,
            phase="waiting_approval" if approval.state == "waiting" else state.phase,
            approvals={**state.approvals, approval_id: approval},
        )
    if event.type == "approval_granted":
        if current.state != "waiting":
            raise OperationLogInvariantError("只有 Waiting Approval 可以批准")
        approval = replace(current, state="approved")
        phase = "ready_to_resume"
    elif event.type == "approval_consumed":
        if current.state != "approved":
            raise OperationLogInvariantError("只有 Approved Approval 可以消费")
        approval = replace(
            current,
            state="consumed",
            consumer_id=str(event.data.get("consumerId", "")),
        )
        phase = "ready_to_resume"
    elif event.type == "approval_rejected":
        if current.state != "waiting":
            raise OperationLogInvariantError("只有 Waiting Approval 可以拒绝")
        approval = replace(current, state="rejected", resume_state="cancelled")
        phase = "running"
    elif event.type == "approval_expired":
        if current.state != "waiting":
            raise OperationLogInvariantError("只有 Waiting Approval 可以过期")
        approval = replace(current, state="expired", resume_state="cancelled")
        phase = "running"
    elif event.type == "approval_resume_started":
        if current.state != "consumed":
            raise OperationLogInvariantError("Approval 未消费，不能开始 Resume")
        if current.resume_state not in {"registered", "started"}:
            raise OperationLogInvariantError("Approval Resume 当前状态不能 Started")
        approval = replace(current, state="resume_started", resume_state="started")
        phase = "executing_write"
    elif event.type == "approval_resume_completed":
        if current.state != "resume_started":
            raise OperationLogInvariantError("Approval Resume 未 Started 不能 Completed")
        approval = replace(current, state="resume_completed", resume_state="completed")
        phase = "running"
    elif event.type == "approval_resume_failed":
        if current.state != "resume_started":
            raise OperationLogInvariantError("Approval Resume 未 Started 不能 Failed")
        approval = replace(current, state="resume_failed", resume_state="failed")
        phase = "running"
    elif event.type == "approval_resume_cancelled":
        if current.state not in {"waiting", "rejected", "expired"}:
            raise OperationLogInvariantError("当前 Approval 状态不能取消 Resume")
        approval = replace(current, state="resume_cancelled", resume_state="cancelled")
        phase = "running"
    else:
        raise OperationLogInvariantError(f"未知 Approval Event：{event.type}")
    return replace(
        state,
        phase=phase,
        approvals={**state.approvals, approval_id: approval},
    )


def _reduce_write_event(
    state: OperationState,
    event: OperationEvent,
) -> OperationState:
    write_id = _text(event.data, "writeId")
    current = state.writes.get(write_id)
    if event.type == "write_prepared":
        if current is not None:
            raise OperationLogInvariantError("Write Prepared 重复")
        write = WriteSnapshot(
            write_id=write_id,
            state="prepared",
            tool_name=_text(event.data, "toolName"),
            arguments=dict(event.data.get("arguments", {})),
            action_hash=_text(event.data, "actionHash"),
            idempotency_key_hash=_text(event.data, "idempotencyKeyHash"),
            tool_call_id=(
                str(event.data.get("toolCallId"))
                if event.data.get("toolCallId") is not None
                else None
            ),
        )
        return replace(state, writes={**state.writes, write_id: write})
    if current is None:
        raise OperationLogInvariantError("Write Event 缺少 Prepared")

    if event.type == "write_waiting_approval":
        if current.state != "prepared":
            raise OperationLogInvariantError("只有 Prepared Write 可以等待审批")
        write = replace(
            current,
            state="waiting_approval",
            approval_id=_text(event.data, "approvalId"),
        )
        phase = "waiting_approval"
    elif event.type == "write_approved":
        if current.state not in {"prepared", "waiting_approval"}:
            raise OperationLogInvariantError("当前 Write 状态不能 Approved")
        write = replace(current, state="approved")
        phase = "ready_to_resume"
    elif event.type == "write_submitting":
        if current.state != "approved":
            raise OperationLogInvariantError("只有 Approved Write 可以提交")
        write = replace(current, state="submitting")
        phase = "executing_write"
    elif event.type == "write_succeeded":
        if current.state not in {"submitting", "reconciling"}:
            raise OperationLogInvariantError("当前 Write 状态不能 Succeeded")
        write = replace(
            current,
            state="succeeded",
            result=dict(event.data.get("result", {})),
        )
        phase = "running"
    elif event.type == "write_failed":
        if current.state not in {"submitting", "reconciling"}:
            raise OperationLogInvariantError("当前 Write 状态不能 Failed")
        write = replace(
            current,
            state="failed",
            result=dict(event.data.get("result", {})),
        )
        phase = "running"
    elif event.type == "write_outcome_unknown":
        if current.state != "submitting":
            raise OperationLogInvariantError("只有 Submitting Write 可变成 OutcomeUnknown")
        write = replace(current, state="outcome_unknown")
        phase = "outcome_unknown"
    elif event.type == "write_reconciling":
        if current.state != "outcome_unknown":
            raise OperationLogInvariantError("只有 OutcomeUnknown Write 可以核对")
        write = replace(current, state="reconciling")
        phase = "outcome_unknown"
    else:
        raise OperationLogInvariantError(f"未知 Write Event：{event.type}")
    return replace(
        state,
        phase=phase,
        writes={**state.writes, write_id: write},
    )


def replay_operation(events: list[OperationEvent]) -> OperationState:
    state: OperationState | None = None
    for event in sorted(events, key=lambda item: item.sequence):
        state = reduce_operation_event(state, event)
    if state is None:
        raise OperationLogInvariantError("Operation Log 为空")
    return state


def replay_operation_with_specs(
    events: list[OperationEvent],
    specs: list[tuple[str, dict[str, Any]]],
) -> OperationState:
    """在 CAS 追加前纯函数验证一批候选 Event。"""

    state = replay_operation(events)
    sequence = max(event.sequence for event in events)
    for event_type, data in specs:
        sequence += 1
        state = reduce_operation_event(
            state,
            OperationEvent(
                type=event_type,
                session_id=state.session_id,
                operation_id=state.operation_id,
                sequence=sequence,
                data=dict(data),
            ),
        )
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


def _model_policy(value: Any) -> ModelRequestPolicy:
    try:
        return ModelRequestPolicy.from_dict(value)
    except (ModelRequestPolicyError, TypeError, ValueError) as error:
        raise OperationLogInvariantError(
            f"Model Request Policy 无效：{error}"
        ) from error


def _approval_for_tool_call(
    state: OperationState,
    tool_call_id: str,
) -> ApprovalSnapshot | None:
    return next(
        (
            approval
            for approval in state.approvals.values()
            if approval.tool_call_id == tool_call_id
        ),
        None,
    )


def _validate_approval_action(
    approval: ApprovalSnapshot,
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    action = {"tool": tool_name, "arguments": arguments}
    if _action_digest(action) != approval.action_hash:
        raise OperationLogInvariantError(
            "Approval Action Hash 与 Tool Call 不匹配"
        )


def _action_digest(action: dict[str, Any]) -> str:
    canonical = json.dumps(
        action,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _tool(state: OperationState, tool_call_id: str) -> ToolInvocationState:
    invocation = state.tools.get(tool_call_id)
    if invocation is None:
        raise OperationLogInvariantError("Tool Event 没有对应 Intent")
    return invocation
