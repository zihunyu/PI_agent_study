"""把 Agent 生命周期持久化为可恢复 Operation Event。"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from ..types import AgentTool
from .operation_state import replay_operation_with_specs
from .operation_store import (
    OperationEventStore,
    OperationStoreConflictError,
    operation_last_sequence,
)


class DurableOperationRecorder:
    """可作为 Agent Listener；完整消息和工具事实写入 Operation Store。"""

    def __init__(
        self,
        store: OperationEventStore,
        *,
        session_id: str,
        tools: list[AgentTool],
        configuration: dict[str, Any] | None = None,
    ) -> None:
        if not session_id:
            raise ValueError("session_id 不能为空")
        self.store = store
        self.session_id = session_id
        self.tools = {tool.name: tool for tool in tools}
        self.configuration = dict(configuration or {})
        self.operation_id: str | None = None
        self.last_operation_id: str | None = None
        self._active_request_id: str | None = None
        self._last_failure: str | None = None

    async def start_operation(self) -> str:
        if self.operation_id is not None:
            raise RuntimeError("已有活动 Durable Operation")
        self.operation_id = str(uuid4())
        self.last_operation_id = self.operation_id
        self._last_failure = None
        await self._append(
            "operation_started",
            {
                "configuration": self.configuration,
                "tools": [
                    {"name": tool.name, "replayPolicy": tool.replay_policy}
                    for tool in self.tools.values()
                ],
            },
        )
        return self.operation_id

    async def record_external(
        self,
        event_type: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        await self._append(event_type, data or {})

    async def finish_operation(self, outcome: str) -> None:
        if self.operation_id is None:
            raise RuntimeError("没有活动 Durable Operation")
        operation_id = self.operation_id
        specs = [("operation_finished", {"outcome": outcome})]
        for _ in range(20):
            events = await self.store.load(
                session_id=self.session_id,
                operation_id=operation_id,
            )
            replay_operation_with_specs(events, specs)
            try:
                await self.store.append_batch(
                    self.session_id,
                    operation_id,
                    specs,
                    expected_last_sequence=operation_last_sequence(events),
                )
            except OperationStoreConflictError:
                continue
            self.operation_id = None
            self._active_request_id = None
            return
        raise RuntimeError("Operation Finished 并发冲突")

    async def listener(self, event: dict[str, Any], cancellation: Any) -> None:
        event_type = event.get("type")
        if event_type == "agent_start":
            if self.operation_id is None:
                await self.start_operation()
            return
        if self.operation_id is None:
            return
        if event_type == "transcript_repaired":
            message = event.get("message")
            if isinstance(message, dict):
                await self._append(
                    "message_appended",
                    {"message": message, "syntheticRepair": True},
                )
            return
        if event_type == "model_policy_selected":
            policy = event.get("policy")
            if not isinstance(policy, dict):
                raise RuntimeError("Model Policy Selected 缺少策略快照")
            await self._append(
                "model_policy_selected",
                {"policy": policy},
            )
            return
        if event_type == "model_request_start":
            if self._active_request_id is not None:
                raise RuntimeError("上一个 Model Request 尚未结束")
            policy = event.get("requestPolicy")
            if not isinstance(policy, dict):
                raise RuntimeError("Model Request Start 缺少策略快照")
            self._active_request_id = str(uuid4())
            await self._append(
                "model_request_started",
                {
                    "requestId": self._active_request_id,
                    "requestPolicy": policy,
                },
            )
            return
        if event_type == "message_start":
            message = event.get("message", {})
            if (
                message.get("role") == "assistant"
                and self._active_request_id is None
            ):
                # 兼容不发 model_request_start 的自定义旧 Loop。
                self._active_request_id = str(uuid4())
                await self._append(
                    "model_request_started",
                    {"requestId": self._active_request_id},
                )
            return
        if event_type == "message_end":
            message = event.get("message", {})
            role = message.get("role")
            if role == "assistant":
                if self._active_request_id is None:
                    raise RuntimeError("Assistant Message 没有 Model Request Started")
                stop_reason = message.get("stopReason")
                if stop_reason in {"error", "aborted"}:
                    self._last_failure = (
                        "cancelled" if stop_reason == "aborted" else "model_error"
                    )
                await self._append(
                    "model_request_completed",
                    {
                        "requestId": self._active_request_id,
                        "message": message,
                    },
                )
                self._active_request_id = None
            elif role in {"user", "toolResult"}:
                await self._append("message_appended", {"message": message})
            return
        if event_type == "tool_execution_start":
            tool_name = str(event.get("toolName", ""))
            tool = self.tools.get(tool_name)
            await self._append(
                "tool_intent_recorded",
                {
                    "toolCallId": str(event.get("toolCallId", "")),
                    "toolName": tool_name,
                    "arguments": event.get("args", {}),
                    "replayPolicy": tool.replay_policy if tool else "never",
                },
            )
            return
        if event_type == "tool_execution_dispatch_start":
            await self._append(
                "tool_dispatch_started",
                {
                    "toolCallId": str(event.get("toolCallId", "")),
                    "attempt": event.get("attempt", 1),
                },
            )
            return
        if event_type == "tool_execution_end":
            result = event.get("result", {})
            details = result.get("details", {}) if isinstance(result, dict) else {}
            outcome_unknown = (
                isinstance(details, dict)
                and details.get("code") == "outcome_unknown"
            )
            await self._append(
                "tool_outcome_unknown" if outcome_unknown else "tool_completed",
                {
                    "toolCallId": str(event.get("toolCallId", "")),
                    "toolName": str(event.get("toolName", "")),
                    "result": {
                        "content": result.get("content", [])
                        if isinstance(result, dict)
                        else [],
                        "details": details,
                        "isError": bool(event.get("isError")),
                    },
                },
            )
            return
        if event_type == "agent_end":
            cancelled = bool(getattr(cancellation, "cancelled", False))
            if cancelled or self._last_failure == "cancelled":
                outcome = "cancelled"
            elif self._last_failure is not None:
                outcome = "failed"
            else:
                outcome = "completed"
            await self.finish_operation(outcome)

    async def _append(self, event_type: str, data: dict[str, Any]) -> None:
        if self.operation_id is None:
            raise RuntimeError("没有活动 Durable Operation")
        await self.store.append(
            event_type,
            self.session_id,
            self.operation_id,
            data,
        )
