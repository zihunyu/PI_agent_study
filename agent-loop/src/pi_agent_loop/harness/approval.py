"""Durable Host 的审批规划与恢复工作流。"""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from ..approval.state_machine import build_approval_request_event
from ..durable_action import DurableActionEnvelope
from ..messages import assistant_message, user_message
from ..model_policy import ModelRequestPolicy, ModelRequestPolicyError
from ..security import VerifiedIdentity
from ..session.operation_state import replay_operation, replay_operation_with_specs
from ..session.operation_store import (
    OperationStoreConflictError,
    operation_last_sequence,
)
from ..writes import hash_idempotency_key


class DurableApprovalWorkflow:
    """把 Approval Plan/Resume 从 Host Facade 中移出的应用服务。"""

    def __init__(self, host: Any) -> None:
        self.host = host

    async def prepare(
        self,
        *,
        text: str,
        routed_result: Any,
        requester: VerifiedIdentity | None,
        approval_role: str,
        idempotency_key: str | None,
    ) -> str:
        host = self.host
        if requester is None:
            await self._fail_active_operation()
            raise PermissionError("该请求需要 Approval，必须提供 VerifiedIdentity")
        if len(routed_result.decision.selected_tools) != 1:
            await self._fail_active_operation()
            raise RuntimeError("Approval Resume 当前要求一个明确的写工具")
        operation_id = host.operation_recorder.operation_id
        if operation_id is None:
            raise RuntimeError("Approval Required 但 Durable Operation 未保持活动")
        if not host.operation_store.supports_atomic_transactions:
            await self._fail_active_operation()
            raise RuntimeError("Approval 写请求必须使用支持原子事务的 Operation Store")
        if idempotency_key is None:
            await self._fail_active_operation()
            raise ValueError("Approval 写请求必须在初始 Prompt 提供 Idempotency Key")

        tool_name = routed_result.decision.selected_tools[0]
        arguments = dict(routed_result.decision.extracted_fields)
        envelope = DurableActionEnvelope(
            operation_id=operation_id,
            tool_call_id=str(uuid4()),
            tool_name=tool_name,
            arguments=arguments,
            write_id=str(uuid4()),
        )
        request_id = str(uuid4())
        planned_call = assistant_message(
            model=host.model,
            stop_reason="toolUse",
            content=[
                {
                    "type": "toolCall",
                    "id": envelope.tool_call_id,
                    "name": tool_name,
                    "arguments": arguments,
                }
            ],
        )
        policy = ModelRequestPolicy(
            visible_tool_names=(tool_name,),
            tool_choice={"type": "function", "function": {"name": tool_name}},
            required_capabilities=tuple(
                routed_result.decision.required_capabilities
            ),
            allowed_tool_names=(tool_name,),
            expected_tool_arguments=arguments,
            continuation_policy=ModelRequestPolicy.no_tools(),
        )
        approval_id, approval_data = build_approval_request_event(
            requester=requester,
            action=envelope.to_dict(),
            action_summary=(
                f"执行 {routed_result.decision.intent or tool_name}"
            ),
            required_role=approval_role,
        )
        specs = [
            ("message_appended", {"message": user_message(text)}),
            (
                "model_request_started",
                {
                    "requestId": request_id,
                    "source": "host_approval_plan",
                    "requestPolicy": policy.to_dict(),
                },
            ),
            (
                "model_request_completed",
                {
                    "requestId": request_id,
                    "message": planned_call,
                    "source": "host_approval_plan",
                },
            ),
            (
                "model_policy_selected",
                {"policy": ModelRequestPolicy.no_tools().to_dict()},
            ),
            (
                "write_prepared",
                {
                    "writeId": envelope.write_id,
                    "toolName": tool_name,
                    "arguments": arguments,
                    "actionHash": envelope.action_hash,
                    "idempotencyKeyHash": hash_idempotency_key(idempotency_key),
                    "requesterId": requester.principal_id,
                    "requesterVerificationId": requester.verification_id,
                    "toolCallId": envelope.tool_call_id,
                },
            ),
            ("approval_requested", approval_data),
            (
                "approval_resume_registered",
                {
                    "approvalId": approval_id,
                    "action": envelope.to_dict(),
                    "resumePayload": {"envelope": envelope.to_dict()},
                },
            ),
            (
                "write_waiting_approval",
                {"writeId": envelope.write_id, "approvalId": approval_id},
            ),
            (
                "tool_intent_recorded",
                {
                    "toolCallId": envelope.tool_call_id,
                    "toolName": tool_name,
                    "arguments": arguments,
                    "replayPolicy": "never",
                    "source": "host_approval_plan",
                },
            ),
        ]
        for _ in range(20):
            events = await host.operation_store.load(
                session_id=host.session_id,
                operation_id=operation_id,
            )
            replay_operation_with_specs(events, specs)
            try:
                await host.operation_store.append_batch(
                    host.session_id,
                    operation_id,
                    specs,
                    expected_last_sequence=operation_last_sequence(events),
                )
            except OperationStoreConflictError:
                continue
            pending = await host.approval_resume.get_pending(approval_id)
            return pending.approval.approval_id
        raise RuntimeError("Approval/Write/Tool Intent 原子持久化冲突")

    async def approve_and_resume(
        self,
        approval_id: str,
        *,
        approver: VerifiedIdentity,
        consumer: VerifiedIdentity,
        idempotency_key: str,
        write_handler: Callable[..., Any],
    ) -> Any:
        host = self.host
        pending = await host.approval_resume.get_pending(approval_id)
        approval_events = await host.operation_store.load(
            session_id=host.session_id,
            operation_id=pending.approval.operation_id,
        )
        requested_at = next(
            (
                event.timestamp
                for event in approval_events
                if event.type == "approval_requested"
                and event.data.get("approvalId") == approval_id
            ),
            None,
        )
        if requested_at is not None:
            host.telemetry.record_approval_wait(
                outcome="resume_attempt",
                duration_ms=max(0.0, time.time() * 1000 - requested_at),
            )
        pending_envelope = DurableActionEnvelope.from_dict(
            pending.resume_payload.get("envelope")
        )
        resumed_operation_id: str | None = pending_envelope.operation_id

        async def resume(payload: dict[str, Any]) -> Any:
            nonlocal resumed_operation_id
            envelope = DurableActionEnvelope.from_dict(payload.get("envelope"))
            operation_id = envelope.operation_id
            resumed_operation_id = operation_id
            if (
                host.runtime_tracker.state.run_id is not None
                and not host.runtime_tracker.state.terminal
                and host.runtime_tracker.state.phase == "waiting_approval"
            ):
                await host.runtime_tracker.record_external("approval_granted")
            write = await host.writes.get(envelope.write_id)
            envelope.assert_execution(
                operation_id=write.operation_id,
                tool_call_id=write.tool_call_id or "",
                tool_name=write.tool_name,
                arguments=write.arguments,
                write_id=write.write_id,
            )
            if write.action_hash != envelope.action_hash:
                raise RuntimeError("Approval Envelope 与 Write Action Hash 不一致")
            if write.approval_id != approval_id:
                raise RuntimeError("Approval Envelope 与 Write Approval ID 不一致")

            async def invoke(args, key, actor):
                value = write_handler(args, key, actor)
                return await value if hasattr(value, "__await__") else value

            write = await host.writes.execute(
                write.write_id,
                actor=consumer,
                idempotency_key=idempotency_key,
                handler=invoke,
                approval_resume_id=approval_id,
            )
            tool_result = {
                "role": "toolResult",
                "toolCallId": envelope.tool_call_id,
                "toolName": envelope.tool_name,
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(write.result or {}, ensure_ascii=False),
                    }
                ],
                "details": write.result or {},
                "isError": False,
            }
            await self.materialize_write_tool_result(operation_id, tool_result)
            return await self.request_final_model(
                operation_id=operation_id,
                approval_id=approval_id,
                policy=ModelRequestPolicy.no_tools(),
            )

        try:
            result = await host.approval_resume.approve_and_resume(
                approval_id,
                approver=approver,
                consumer=consumer,
                resume=resume,
                atomic_write_start=True,
            )
        except Exception:
            if resumed_operation_id is not None:
                if host.operation_recorder.operation_id == resumed_operation_id:
                    await host.operation_recorder.finish_operation("failed")
                else:
                    await self.finish_persisted_operation(
                        resumed_operation_id, "failed"
                    )
            if (
                host.runtime_tracker.state.run_id is not None
                and not host.runtime_tracker.state.terminal
            ):
                await host.runtime_tracker.record_external(
                    "run_finished", {"outcome": "failed"}
                )
            raise
        if resumed_operation_id is not None:
            if host.operation_recorder.operation_id == resumed_operation_id:
                await host.operation_recorder.finish_operation("completed")
            else:
                await self.finish_persisted_operation(
                    resumed_operation_id, "completed"
                )
        if (
            host.runtime_tracker.state.run_id is not None
            and not host.runtime_tracker.state.terminal
        ):
            await host.runtime_tracker.record_external(
                "run_finished", {"outcome": "completed"}
            )
        return result

    async def request_final_model(
        self,
        *,
        operation_id: str,
        approval_id: str,
        policy: ModelRequestPolicy,
    ) -> dict[str, Any]:
        """Resume 重入时复用同一条最终 Model Request。"""

        host = self.host
        request_id: str | None = None
        for _ in range(20):
            events = await host.operation_store.load(
                session_id=host.session_id,
                operation_id=operation_id,
            )
            current = replay_operation(events)
            matching = _approval_final_request_ids(events, approval_id)
            if any(current.model_requests[item].policy != policy for item in matching):
                raise RuntimeError("Approval 最终 Model Request 的持久策略不匹配")
            pending = [
                item
                for item in matching
                if current.model_requests[item].phase == "started"
            ]
            completed = [
                item
                for item in matching
                if current.model_requests[item].phase == "completed"
            ]
            if len(pending) > 1 or len(completed) > 1 or (pending and completed):
                raise RuntimeError("Approval 最终 Model Request 持久状态冲突")
            if completed:
                return _persisted_model_response(events, completed[-1])
            if pending:
                request_id = pending[0]
                operation = current
                break
            request_id = str(uuid4())
            specs = [
                (
                    "model_request_started",
                    {
                        "requestId": request_id,
                        "source": "approval_resume",
                        "approvalId": approval_id,
                        "requestPolicy": policy.to_dict(),
                    },
                )
            ]
            operation = replay_operation_with_specs(events, specs)
            try:
                await host.operation_store.append_batch(
                    host.session_id,
                    operation_id,
                    specs,
                    expected_last_sequence=operation_last_sequence(events),
                )
            except OperationStoreConflictError:
                continue
            break
        else:
            raise RuntimeError("Approval 最终 Model Request 开始并发冲突")

        if request_id is None:
            raise RuntimeError("Approval 最终 Model Request ID 缺失")
        try:
            final = await host.model_runtime.request(
                list(operation.messages),
                policy=policy,
                request_id=request_id,
                durable_metadata={
                    "sessionId": host.session_id,
                    "operationId": operation_id,
                    **(
                        {"runId": host.runtime_tracker.state.run_id}
                        if isinstance(host.runtime_tracker.state.run_id, str)
                        and host.runtime_tracker.state.run_id
                        else {}
                    ),
                },
                source="approval_resume",
            )
            tool_calls = [
                block
                for block in final.get("content", [])
                if isinstance(block, dict) and block.get("type") == "toolCall"
            ]
            if tool_calls:
                raise RuntimeError("Approval 完成后的最终模型响应禁止包含 Tool Call")
            if final.get("stopReason", "stop") != "stop":
                raise RuntimeError("Approval 完成后的最终模型响应没有正常结束")
        except Exception as error:
            persisted = await self.finish_model_request(
                operation_id=operation_id,
                approval_id=approval_id,
                request_id=request_id,
                event_type="model_request_failed",
                data={
                    "requestId": request_id,
                    "source": "approval_resume",
                    "approvalId": approval_id,
                    "errorCode": "approval_final_response_invalid",
                    "error": str(error),
                },
            )
            if persisted is not None:
                return persisted
            if isinstance(error, ModelRequestPolicyError) and "工具" in str(error):
                raise RuntimeError(
                    "Approval 完成后的最终模型响应禁止使用工具"
                ) from error
            raise
        persisted = await self.finish_model_request(
            operation_id=operation_id,
            approval_id=approval_id,
            request_id=request_id,
            event_type="model_request_completed",
            data={
                "requestId": request_id,
                "source": "approval_resume",
                "approvalId": approval_id,
                "message": final,
            },
        )
        return persisted if persisted is not None else final

    async def finish_model_request(
        self,
        *,
        operation_id: str,
        approval_id: str,
        request_id: str,
        event_type: str,
        data: dict[str, Any],
    ) -> dict[str, Any] | None:
        host = self.host
        for _ in range(20):
            events = await host.operation_store.load(
                session_id=host.session_id,
                operation_id=operation_id,
            )
            operation = replay_operation(events)
            request = operation.model_requests.get(request_id)
            if request is None or request_id not in _approval_final_request_ids(
                events, approval_id
            ):
                raise RuntimeError("Approval 最终 Model Request 绑定丢失")
            if request.phase == "completed":
                return _persisted_model_response(events, request_id)
            if request.phase == "failed":
                if event_type == "model_request_completed":
                    raise RuntimeError("Approval 最终 Model Request 已持久为 Failed")
                return None
            specs = [(event_type, data)]
            replay_operation_with_specs(events, specs)
            try:
                await host.operation_store.append_batch(
                    host.session_id,
                    operation_id,
                    specs,
                    expected_last_sequence=operation_last_sequence(events),
                )
            except OperationStoreConflictError:
                continue
            return copy.deepcopy(data.get("message"))
        raise RuntimeError("Approval 最终 Model Request 结束并发冲突")

    async def materialize_write_tool_result(
        self,
        operation_id: str,
        tool_result: dict[str, Any],
    ) -> Any:
        host = self.host
        tool_call_id = str(tool_result["toolCallId"])
        for _ in range(20):
            events = await host.operation_store.load(
                session_id=host.session_id,
                operation_id=operation_id,
            )
            operation = replay_operation(events)
            invocation = operation.tools.get(tool_call_id)
            if invocation is None:
                raise RuntimeError("Write Tool Result 缺少持久 Tool Intent")
            specs: list[tuple[str, dict[str, Any]]] = []
            if invocation.phase == "dispatch_started":
                specs.append(
                    (
                        "tool_completed",
                        {
                            "toolCallId": tool_call_id,
                            "result": {
                                "content": tool_result["content"],
                                "details": tool_result["details"],
                                "isError": False,
                            },
                        },
                    )
                )
            elif invocation.phase != "completed":
                raise RuntimeError(
                    f"Write Tool 当前状态 {invocation.phase} 不能物化结果"
                )
            if not any(
                message.get("role") == "toolResult"
                and message.get("toolCallId") == tool_call_id
                for message in operation.messages
            ):
                specs.append(("message_appended", {"message": tool_result}))
            if not specs:
                return operation
            replay_operation_with_specs(events, specs)
            try:
                await host.operation_store.append_batch(
                    host.session_id,
                    operation_id,
                    specs,
                    expected_last_sequence=operation_last_sequence(events),
                )
            except OperationStoreConflictError:
                continue
            return replay_operation(
                await host.operation_store.load(
                    session_id=host.session_id,
                    operation_id=operation_id,
                )
            )
        raise RuntimeError("Write Tool Result 物化并发冲突")

    async def finish_persisted_operation(
        self,
        operation_id: str,
        outcome: str,
    ) -> None:
        host = self.host
        specs = [("operation_finished", {"outcome": outcome})]
        for _ in range(20):
            events = await host.operation_store.load(
                session_id=host.session_id,
                operation_id=operation_id,
            )
            state = replay_operation(events)
            if state.phase in {"completed", "failed", "cancelled"}:
                return
            replay_operation_with_specs(events, specs)
            try:
                await host.operation_store.append_batch(
                    host.session_id,
                    operation_id,
                    specs,
                    expected_last_sequence=operation_last_sequence(events),
                )
            except OperationStoreConflictError:
                continue
            return
        raise RuntimeError("Operation Finished 并发冲突")

    async def _fail_active_operation(self) -> None:
        host = self.host
        if (
            host.runtime_tracker.state.run_id is not None
            and not host.runtime_tracker.state.terminal
        ):
            await host.runtime_tracker.record_external(
                "run_finished", {"outcome": "failed"}
            )
        if host.operation_recorder.operation_id is not None:
            await host.operation_recorder.finish_operation("failed")


def _approval_final_request_ids(events: list[Any], approval_id: str) -> list[str]:
    exact: list[str] = []
    legacy: list[str] = []
    active_approvals: set[str] = set()
    terminal_types = {
        "approval_resume_completed",
        "approval_resume_failed",
        "approval_resume_cancelled",
    }
    for event in sorted(events, key=lambda item: item.sequence):
        if event.type == "approval_resume_started":
            value = event.data.get("approvalId")
            if isinstance(value, str) and value:
                active_approvals.add(value)
        elif event.type in terminal_types:
            value = event.data.get("approvalId")
            if isinstance(value, str):
                active_approvals.discard(value)
        if event.type != "model_request_started" or event.data.get("source") != "approval_resume":
            continue
        request_id = event.data.get("requestId")
        if not isinstance(request_id, str) or not request_id:
            continue
        bound = event.data.get("approvalId")
        if bound == approval_id:
            exact.append(request_id)
        elif bound is None and active_approvals == {approval_id}:
            legacy.append(request_id)
    return exact or legacy


def _persisted_model_response(events: list[Any], request_id: str) -> dict[str, Any]:
    matches = [
        event
        for event in events
        if event.type == "model_request_completed"
        and event.data.get("requestId") == request_id
    ]
    if len(matches) != 1 or not isinstance(matches[0].data.get("message"), dict):
        raise RuntimeError("持久 Model Request Completed 缺少唯一响应")
    return copy.deepcopy(matches[0].data["message"])


__all__ = ["DurableApprovalWorkflow"]
