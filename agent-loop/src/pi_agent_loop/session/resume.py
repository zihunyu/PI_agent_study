"""根据持久 Operation State 规划安全恢复动作。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from ..model_policy import ModelRequestPolicy
from .operation_state import (
    OperationState,
    ToolInvocationState,
    replay_operation,
    replay_operation_with_specs,
)
from .operation_store import (
    OperationEventStore,
    OperationStoreConflictError,
    operation_last_sequence,
)


@dataclass(frozen=True, slots=True)
class RecoveryAction:
    kind: str
    request_id: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    stored_result: dict[str, Any] | None = None
    approval_id: str | None = None
    write_id: str | None = None
    request_policy: ModelRequestPolicy | None = None


@dataclass(frozen=True, slots=True)
class OperationRecoveryPlan:
    operation: OperationState
    actions: tuple[RecoveryAction, ...]


class OperationRecoveryPlanner:
    """只根据持久事实制定恢复动作，不执行模型或工具。"""

    def plan(self, operation: OperationState) -> OperationRecoveryPlan:
        if operation.phase in {"completed", "failed", "cancelled"}:
            return OperationRecoveryPlan(operation, ())

        guarded_actions = self._approval_write_actions(operation)
        if guarded_actions:
            return OperationRecoveryPlan(operation, guarded_actions)

        pending_requests = [
            request
            for request in operation.model_requests.values()
            if request.phase == "started"
        ]
        if pending_requests:
            pending = pending_requests[-1]
            if pending.policy is None:
                return OperationRecoveryPlan(
                    operation,
                    (
                        RecoveryAction(
                            kind="manual_intervention",
                            request_id=pending.request_id,
                            reason=(
                                "未完成 Model Request 缺少持久化请求策略，"
                                "禁止使用全部工具和 auto 策略恢复"
                            ),
                        ),
                    ),
                )
            return OperationRecoveryPlan(
                operation,
                (
                    RecoveryAction(
                        kind="retry_model_request",
                        request_id=pending.request_id,
                        request_policy=pending.policy,
                        reason="模型请求已开始但没有完整 Assistant Message",
                    ),
                ),
            )

        last_assistant = next(
            (
                message
                for message in reversed(operation.messages)
                if message.get("role") == "assistant"
            ),
            None,
        )
        tool_results = {
            str(message.get("toolCallId"))
            for message in operation.messages
            if message.get("role") == "toolResult"
        }
        actions: list[RecoveryAction] = []
        if last_assistant is not None:
            calls = [
                block
                for block in last_assistant.get("content", [])
                if isinstance(block, dict) and block.get("type") == "toolCall"
            ]
            for call in calls:
                call_id = str(call.get("id", ""))
                if not call_id or call_id in tool_results:
                    continue
                invocation = operation.tools.get(call_id)
                if invocation is None:
                    actions.append(
                        RecoveryAction(
                            kind="execute_tool",
                            tool_call_id=call_id,
                            tool_name=str(call.get("name", "")),
                            arguments=dict(call.get("arguments", {})),
                            reason="Assistant Tool Call 已持久化，但尚未记录 Dispatch Intent",
                        )
                    )
                    continue
                actions.append(self._tool_action(invocation))

        if actions:
            return OperationRecoveryPlan(operation, tuple(actions))
        if not operation.messages:
            return OperationRecoveryPlan(
                operation,
                (
                    RecoveryAction(
                        kind="manual_intervention",
                        reason="Operation 没有可恢复消息 Context",
                    ),
                ),
            )
        last_role = operation.messages[-1].get("role")
        if last_role in {"user", "toolResult"}:
            policy = operation.active_model_policy
            if (
                last_role == "toolResult"
                and policy is not None
                and policy.continuation_policy is not None
            ):
                policy = policy.continuation_policy
            elif (
                last_role == "toolResult"
                and policy is not None
                and policy.tool_choice != "auto"
                and policy.tool_choice != "none"
            ):
                policy = None
            if policy is None:
                action = RecoveryAction(
                    kind="manual_intervention",
                    reason=(
                        "待继续 Context 缺少持久化请求策略，"
                        "禁止扩大工具可见范围"
                    ),
                )
            else:
                action = RecoveryAction(
                    kind="continue_model",
                    request_policy=policy,
                    reason="Context 与模型请求策略均完整，可以继续请求模型",
                )
        else:
            action = RecoveryAction(
                kind="finish_operation",
                reason="最后 Assistant 已完成且没有未解决 Tool Call",
            )
        return OperationRecoveryPlan(operation, (action,))

    def _approval_write_actions(
        self,
        operation: OperationState,
    ) -> tuple[RecoveryAction, ...]:
        actions: list[RecoveryAction] = []
        for write in operation.writes.values():
            common = {
                "write_id": write.write_id,
                "tool_call_id": write.tool_call_id,
                "tool_name": write.tool_name,
                "arguments": write.arguments,
                "approval_id": write.approval_id,
            }
            if write.state in {"submitting", "outcome_unknown", "reconciling"}:
                actions.append(
                    RecoveryAction(
                        kind="reconcile_write",
                        reason="写操作已经提交或结果不确定，必须核对状态",
                        **common,
                    )
                )
            elif write.state == "waiting_approval":
                actions.append(
                    RecoveryAction(
                        kind="wait_for_approval",
                        reason="写操作仍在等待审批",
                        **common,
                    )
                )
            elif write.state in {"prepared", "approved"}:
                actions.append(
                    RecoveryAction(
                        kind="resume_approved_write",
                        reason="写操作已准备完成，可以由 Write Runtime 恢复",
                        **common,
                    )
                )
        if actions:
            return tuple(actions)

        for approval in operation.approvals.values():
            if (
                approval.resume_state == "unregistered"
                and approval.state == "consumed"
            ):
                # 普通 WriteOperation Approval 已消费，无独立 Resume Workflow。
                continue
            action = approval.action
            common = {
                "approval_id": approval.approval_id,
                "tool_call_id": approval.tool_call_id,
                "tool_name": str(action.get("tool", "")) or None,
                "arguments": dict(action.get("arguments", {})),
            }
            if approval.state == "waiting":
                actions.append(
                    RecoveryAction(
                        kind="wait_for_approval",
                        reason="Approval 仍在等待人工批准",
                        **common,
                    )
                )
            elif approval.state == "approved":
                actions.append(
                    RecoveryAction(
                        kind="consume_approval",
                        reason="Approval 已批准，等待可信 Consumer 消费",
                        **common,
                    )
                )
            elif approval.state in {"consumed", "resume_started"}:
                actions.append(
                    RecoveryAction(
                        kind="resume_approved_write",
                        reason="Approval 已消费，可以恢复已批准写操作",
                        **common,
                    )
                )
            elif approval.state in {"rejected", "expired"}:
                actions.append(
                    RecoveryAction(
                        kind="finalize_rejected_approval",
                        reason=f"Approval 状态为 {approval.state}",
                        **common,
                    )
                )
            elif approval.state == "resume_failed":
                actions.append(
                    RecoveryAction(
                        kind="manual_intervention",
                        reason="Approval Resume 已失败，需要人工核对",
                        **common,
                    )
                )
        return tuple(actions)

    def _tool_action(self, invocation: ToolInvocationState) -> RecoveryAction:
        common = {
            "tool_call_id": invocation.tool_call_id,
            "tool_name": invocation.tool_name,
            "arguments": invocation.arguments,
        }
        if invocation.phase == "completed":
            return RecoveryAction(
                kind="materialize_tool_result",
                stored_result=invocation.result,
                reason="工具结果已持久化，但 ToolResult Message 尚未写入 Context",
                **common,
            )
        if invocation.phase == "outcome_unknown":
            return RecoveryAction(
                kind="reconcile_tool",
                reason="写操作结果不确定，必须核对状态，不能直接重放",
                **common,
            )
        if invocation.phase == "intent_recorded":
            return RecoveryAction(
                kind="execute_tool",
                reason="工具尚未进入 Dispatch，可以安全执行",
                **common,
            )
        if invocation.phase == "dispatch_started":
            if invocation.replay_policy == "safe":
                return RecoveryAction(
                    kind="replay_safe_tool",
                    reason="工具声明 replay_policy=safe",
                    **common,
                )
            return RecoveryAction(
                kind="reconcile_tool",
                reason="工具已进入 Dispatch 且不可安全重放",
                **common,
            )
        return RecoveryAction(kind="manual_intervention", reason="未知 Tool 状态", **common)


@dataclass(frozen=True, slots=True)
class RecoveryCallbacks:
    request_model: Callable[
        [list[dict[str, Any]], ModelRequestPolicy],
        Awaitable[dict[str, Any]],
    ]
    execute_tool: Callable[[RecoveryAction], Awaitable[dict[str, Any]]]
    reconcile_tool: Callable[[RecoveryAction], Awaitable[dict[str, Any]]]
    consume_approval: Callable[[RecoveryAction], Awaitable[Any]] | None = None
    resume_write: Callable[[RecoveryAction], Awaitable[Any]] | None = None
    reconcile_write: Callable[[RecoveryAction], Awaitable[Any]] | None = None


@dataclass(frozen=True, slots=True)
class RecoveryExecutionResult:
    operation: OperationState
    status: str
    remaining_actions: tuple[RecoveryAction, ...] = ()


class DurableSessionRecovery:
    """加载、规划并通过 Host 回调安全恢复一个 Operation。"""

    def __init__(self, store: OperationEventStore) -> None:
        self.store = store
        self.planner = OperationRecoveryPlanner()

    async def plan(
        self,
        *,
        session_id: str,
        operation_id: str,
    ) -> OperationRecoveryPlan:
        events = await self.store.load(
            session_id=session_id,
            operation_id=operation_id,
        )
        return self.planner.plan(replay_operation(events))

    async def resume(
        self,
        *,
        session_id: str,
        operation_id: str,
        callbacks: RecoveryCallbacks,
        max_cycles: int = 20,
    ) -> RecoveryExecutionResult:
        """使用跨进程 Lease Claim 串行恢复同一 Operation。"""

        owner_token = str(uuid4())
        resource_id = f"{session_id}:{operation_id}"
        acquired = await self.store.try_acquire_claim(
            "operation_recovery",
            resource_id,
            owner_token,
        )
        if not acquired:
            plan = await self.plan(
                session_id=session_id,
                operation_id=operation_id,
            )
            return RecoveryExecutionResult(
                plan.operation,
                "recovery_claimed",
                plan.actions,
            )
        try:
            return await self._resume_claimed(
                session_id=session_id,
                operation_id=operation_id,
                callbacks=callbacks,
                max_cycles=max_cycles,
            )
        finally:
            await self.store.release_claim(
                "operation_recovery",
                resource_id,
                owner_token,
            )

    async def _resume_claimed(
        self,
        *,
        session_id: str,
        operation_id: str,
        callbacks: RecoveryCallbacks,
        max_cycles: int,
    ) -> RecoveryExecutionResult:
        for _ in range(max_cycles):
            plan = await self.plan(
                session_id=session_id,
                operation_id=operation_id,
            )
            if not plan.actions:
                return RecoveryExecutionResult(plan.operation, plan.operation.phase)
            if any(action.kind == "manual_intervention" for action in plan.actions):
                return RecoveryExecutionResult(
                    plan.operation,
                    "manual_intervention",
                    plan.actions,
                )
            if any(action.kind == "wait_for_approval" for action in plan.actions):
                return RecoveryExecutionResult(
                    plan.operation,
                    "waiting_approval",
                    plan.actions,
                )
            if any(
                action.kind == "consume_approval" for action in plan.actions
            ) and callbacks.consume_approval is None:
                return RecoveryExecutionResult(
                    plan.operation,
                    "approval_consumer_required",
                    plan.actions,
                )
            if any(
                action.kind == "resume_approved_write" for action in plan.actions
            ) and callbacks.resume_write is None:
                return RecoveryExecutionResult(
                    plan.operation,
                    "approved_write_runtime_required",
                    plan.actions,
                )
            if any(
                action.kind == "reconcile_write" for action in plan.actions
            ) and callbacks.reconcile_write is None:
                return RecoveryExecutionResult(
                    plan.operation,
                    "write_reconciliation_required",
                    plan.actions,
                )

            for action in plan.actions:
                if action.kind in {"retry_model_request", "continue_model"}:
                    await self._request_model(plan.operation, action, callbacks)
                elif action.kind in {"execute_tool", "replay_safe_tool"}:
                    await self._execute_tool(plan.operation, action, callbacks)
                elif action.kind == "reconcile_tool":
                    await self._reconcile_tool(plan.operation, action, callbacks)
                elif action.kind == "materialize_tool_result":
                    await self._materialize_result(plan.operation, action)
                elif action.kind == "consume_approval":
                    await callbacks.consume_approval(action)  # type: ignore[misc]
                elif action.kind == "resume_approved_write":
                    await callbacks.resume_write(action)  # type: ignore[misc]
                elif action.kind == "reconcile_write":
                    await callbacks.reconcile_write(action)  # type: ignore[misc]
                elif action.kind == "finalize_rejected_approval":
                    await self._finalize_rejected_approval(plan.operation, action)
                elif action.kind == "finish_operation":
                    await self._finish_operation(
                        plan.operation,
                        "completed",
                    )
                else:
                    return RecoveryExecutionResult(
                        plan.operation,
                        "manual_intervention",
                        (action,),
                    )
        plan = await self.plan(session_id=session_id, operation_id=operation_id)
        return RecoveryExecutionResult(
            plan.operation,
            "max_recovery_cycles",
            plan.actions,
        )

    async def _finish_operation(
        self,
        operation: OperationState,
        outcome: str,
    ) -> None:
        specs = [("operation_finished", {"outcome": outcome})]
        for _ in range(20):
            events = await self.store.load(
                session_id=operation.session_id,
                operation_id=operation.operation_id,
            )
            replay_operation_with_specs(events, specs)
            try:
                await self.store.append_batch(
                    operation.session_id,
                    operation.operation_id,
                    specs,
                    expected_last_sequence=operation_last_sequence(events),
                )
            except OperationStoreConflictError:
                continue
            return
        raise RuntimeError("Operation Finished 并发冲突")

    async def _request_model(
        self,
        operation: OperationState,
        action: RecoveryAction,
        callbacks: RecoveryCallbacks,
    ) -> None:
        if action.kind == "retry_model_request" and action.request_id is not None:
            await self.store.append(
                "model_request_failed",
                operation.session_id,
                operation.operation_id,
                {
                    "requestId": action.request_id,
                    "errorCode": "process_interrupted",
                    "recovery": True,
                },
            )
        if action.request_policy is None:
            raise RuntimeError("恢复模型请求缺少持久化策略")
        request_id = str(uuid4())
        await self.store.append(
            "model_request_started",
            operation.session_id,
            operation.operation_id,
            {
                "requestId": request_id,
                "requestPolicy": action.request_policy.to_dict(),
                "recovery": True,
            },
        )
        message = await callbacks.request_model(
            list(operation.messages),
            action.request_policy,
        )
        await self.store.append(
            "model_request_completed",
            operation.session_id,
            operation.operation_id,
            {"requestId": request_id, "message": message, "recovery": True},
        )

    async def _execute_tool(
        self,
        operation: OperationState,
        action: RecoveryAction,
        callbacks: RecoveryCallbacks,
    ) -> None:
        if action.tool_call_id not in operation.tools:
            await self.store.append(
                "tool_intent_recorded",
                operation.session_id,
                operation.operation_id,
                {
                    "toolCallId": action.tool_call_id,
                    "toolName": action.tool_name,
                    "arguments": action.arguments,
                    "replayPolicy": "never",
                    "recovery": True,
                },
            )
        await self.store.append(
            "tool_dispatch_started",
            operation.session_id,
            operation.operation_id,
            {"toolCallId": action.tool_call_id, "recovery": True},
        )
        result_message = await callbacks.execute_tool(action)
        await self._store_tool_result(
            operation,
            action,
            result_message,
            event_type="tool_completed",
        )

    async def _reconcile_tool(
        self,
        operation: OperationState,
        action: RecoveryAction,
        callbacks: RecoveryCallbacks,
    ) -> None:
        result_message = await callbacks.reconcile_tool(action)
        await self._store_tool_result(
            operation,
            action,
            result_message,
            event_type="tool_reconciled",
        )

    async def _finalize_rejected_approval(
        self,
        operation: OperationState,
        action: RecoveryAction,
    ) -> None:
        if action.approval_id is None or action.tool_call_id is None or action.tool_name is None:
            raise RuntimeError("Rejected Approval 缺少 Tool Call 关联")
        if action.tool_call_id not in operation.tools:
            await self.store.append(
                "tool_intent_recorded",
                operation.session_id,
                operation.operation_id,
                {
                    "toolCallId": action.tool_call_id,
                    "toolName": action.tool_name,
                    "arguments": action.arguments,
                    "replayPolicy": "never",
                    "recovery": True,
                },
            )
        result_message = {
            "role": "toolResult",
            "toolCallId": action.tool_call_id,
            "toolName": action.tool_name,
            "content": [
                {
                    "type": "text",
                    "text": "工具调用未执行：Approval 已拒绝或过期。",
                }
            ],
            "details": {
                "code": "approval_not_granted",
                "approvalId": action.approval_id,
                "synthetic": True,
            },
            "isError": True,
        }
        await self._store_tool_result(
            operation,
            action,
            result_message,
            event_type="tool_completed",
        )
        await self.store.append(
            "approval_resume_cancelled",
            operation.session_id,
            operation.operation_id,
            {
                "approvalId": action.approval_id,
                "reason": "rejected_or_expired",
                "recovery": True,
            },
        )

    async def _materialize_result(
        self,
        operation: OperationState,
        action: RecoveryAction,
    ) -> None:
        result = action.stored_result or {}
        message = {
            "role": "toolResult",
            "toolCallId": action.tool_call_id,
            "toolName": action.tool_name,
            "content": result.get("content", []),
            "details": result.get("details", {}),
            "isError": bool(result.get("isError")),
        }
        await self.store.append(
            "message_appended",
            operation.session_id,
            operation.operation_id,
            {"message": message, "recovery": True},
        )
        await self._persist_continuation_policy(operation)

    async def _store_tool_result(
        self,
        operation: OperationState,
        action: RecoveryAction,
        result_message: dict[str, Any],
        *,
        event_type: str,
    ) -> None:
        result = {
            "content": result_message.get("content", []),
            "details": result_message.get("details", {}),
            "isError": bool(result_message.get("isError")),
        }
        await self.store.append(
            event_type,
            operation.session_id,
            operation.operation_id,
            {"toolCallId": action.tool_call_id, "result": result, "recovery": True},
        )
        await self.store.append(
            "message_appended",
            operation.session_id,
            operation.operation_id,
            {"message": result_message, "recovery": True},
        )
        await self._persist_continuation_policy(operation)

    async def _persist_continuation_policy(
        self,
        operation: OperationState,
    ) -> None:
        policy = operation.active_model_policy
        if policy is None or policy.continuation_policy is None:
            return
        await self.store.append(
            "model_policy_selected",
            operation.session_id,
            operation.operation_id,
            {"policy": policy.continuation_policy.to_dict(), "recovery": True},
        )
