"""根据持久 Operation State 规划安全恢复动作。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from .operation_state import OperationState, ToolInvocationState, replay_operation
from .operation_store import OperationEventStore


@dataclass(frozen=True, slots=True)
class RecoveryAction:
    kind: str
    request_id: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    stored_result: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class OperationRecoveryPlan:
    operation: OperationState
    actions: tuple[RecoveryAction, ...]


class OperationRecoveryPlanner:
    """只根据持久事实制定恢复动作，不执行模型或工具。"""

    def plan(self, operation: OperationState) -> OperationRecoveryPlan:
        if operation.phase in {"completed", "failed", "cancelled"}:
            return OperationRecoveryPlan(operation, ())

        pending_requests = [
            request
            for request in operation.model_requests.values()
            if request.phase == "started"
        ]
        if pending_requests:
            return OperationRecoveryPlan(
                operation,
                (
                    RecoveryAction(
                        kind="retry_model_request",
                        request_id=pending_requests[-1].request_id,
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
            action = RecoveryAction(
                kind="continue_model",
                reason="Context 已完整，最后消息允许继续请求模型",
            )
        else:
            action = RecoveryAction(
                kind="finish_operation",
                reason="最后 Assistant 已完成且没有未解决 Tool Call",
            )
        return OperationRecoveryPlan(operation, (action,))

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
    request_model: Callable[[list[dict[str, Any]]], Awaitable[dict[str, Any]]]
    execute_tool: Callable[[RecoveryAction], Awaitable[dict[str, Any]]]
    reconcile_tool: Callable[[RecoveryAction], Awaitable[dict[str, Any]]]


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
        """循环规划和执行恢复动作，直到完成或需要人工介入。"""

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

            for action in plan.actions:
                if action.kind in {"retry_model_request", "continue_model"}:
                    await self._request_model(plan.operation, action, callbacks)
                elif action.kind in {"execute_tool", "replay_safe_tool"}:
                    await self._execute_tool(plan.operation, action, callbacks)
                elif action.kind == "reconcile_tool":
                    await self._reconcile_tool(plan.operation, action, callbacks)
                elif action.kind == "materialize_tool_result":
                    await self._materialize_result(plan.operation, action)
                elif action.kind == "finish_operation":
                    await self.store.append(
                        "operation_finished",
                        session_id,
                        operation_id,
                        {"outcome": "completed"},
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
        request_id = str(uuid4())
        await self.store.append(
            "model_request_started",
            operation.session_id,
            operation.operation_id,
            {"requestId": request_id, "recovery": True},
        )
        message = await callbacks.request_model(list(operation.messages))
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
