"""把现有 Agent Event 适配为 Runtime Event 并维护可持久状态。"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from ..session.replay import replay_runtime_events
from ..session.operation_store import ClaimLease
from ..session.store import RuntimeEventStore
from .events import RuntimeEvent, RuntimeEventType
from .reducer import reduce_runtime_state
from .states import RunState


class RuntimeStateTracker:
    """可直接作为 `Agent.subscribe()` 的异步 Listener。"""

    def __init__(
        self,
        store: RuntimeEventStore,
        state: RunState,
        *,
        fenced_claim: ClaimLease | None = None,
        fenced_claim_lease_seconds: float = 300,
    ) -> None:
        if fenced_claim_lease_seconds <= 0:
            raise ValueError("Runtime Tracker Claim Lease 必须大于 0")
        if fenced_claim is not None and not getattr(
            store,
            "supports_fenced_runtime_append",
            False,
        ):
            raise RuntimeError(
                "Runtime Tracker Store 不支持原子 Fenced Append"
            )
        self.store = store
        self.state = state
        self.fenced_claim = fenced_claim
        self.fenced_claim_lease_seconds = fenced_claim_lease_seconds
        self._lock = asyncio.Lock()
        self._active_model_identity: dict[str, Any] = {}

    @classmethod
    async def create(
        cls,
        store: RuntimeEventStore,
        *,
        fenced_claim: ClaimLease | None = None,
        fenced_claim_lease_seconds: float = 300,
    ) -> "RuntimeStateTracker":
        state = replay_runtime_events(await store.load())
        return cls(
            store,
            state,
            fenced_claim=fenced_claim,
            fenced_claim_lease_seconds=fenced_claim_lease_seconds,
        )

    async def start_run(self) -> RunState:
        """在 Router 等 Agent Loop 外层工作开始前显式打开一个 Run。"""

        async with self._lock:
            await self._append("run_started", {})
            return self.state

    async def listener(self, event: dict[str, Any], cancellation: Any) -> None:
        async with self._lock:
            observed = dict(event)
            converted = self._convert_agent_event(
                observed,
                cancelled=bool(getattr(cancellation, "cancelled", False)),
            )
            for event_type, data in converted:
                await self._append(event_type, data)
            if observed.get("type") == "model_request_start":
                self._active_model_identity = _model_identity_data(observed)
            elif (
                observed.get("type") == "message_end"
                and isinstance(observed.get("message"), dict)
                and observed["message"].get("role") == "assistant"
            ):
                self._active_model_identity = {}

    async def record_external(
        self,
        event_type: RuntimeEventType,
        data: dict[str, Any] | None = None,
    ) -> RunState:
        """记录 Approval、Outcome 等 Agent Loop 外层业务事件。"""

        async with self._lock:
            await self._append(event_type, data or {})
            return self.state

    async def _append(
        self,
        event_type: RuntimeEventType,
        data: dict[str, Any],
    ) -> None:
        if event_type == "run_started":
            run_id = str(uuid4())
        else:
            if self.state.run_id is None:
                raise RuntimeError(f"没有活动 Run，不能记录 {event_type}")
            run_id = self.state.run_id
        runtime_event = RuntimeEvent(
            type=event_type,
            run_id=run_id,
            sequence=self.state.sequence + 1,
            data=data,
        )
        next_state = reduce_runtime_state(self.state, runtime_event)
        if self.fenced_claim is None:
            await self.store.append_cas(
                runtime_event,
                expected_last_sequence=self.state.sequence,
            )
        else:
            await self.store.append_cas_if_fenced_claim(
                runtime_event,
                self.fenced_claim,
                renew_lease_seconds=self.fenced_claim_lease_seconds,
                expected_last_sequence=self.state.sequence,
            )
        self.state = next_state

    def _convert_agent_event(
        self,
        event: dict[str, Any],
        *,
        cancelled: bool,
    ) -> list[tuple[RuntimeEventType, dict[str, Any]]]:
        event_type = event.get("type")
        if event_type == "agent_start":
            # RoutedAgent 可能已在进入 Hybrid Router 前打开同一个 Run。
            if self.state.run_id is not None and not self.state.terminal:
                return []
            return [("run_started", {})]
        if event_type == "turn_start":
            return [("turn_started", {"turn": self.state.turn + 1})]
        if event_type == "model_request_start":
            return [(
                "model_request_started",
                _model_identity_data(event),
            )]
        if event_type == "message_start":
            message = event.get("message", {})
            if message.get("role") == "assistant":
                # Compatibility fallback for custom/legacy loops that do not
                # emit the explicit model_request_start boundary.
                if "requestId" not in self._active_model_identity:
                    return [("model_request_started", {})]
            return []
        if event_type == "message_end":
            message = event.get("message", {})
            if message.get("role") != "assistant":
                return []
            details = message.get("providerError") or message.get("policyError") or {}
            return [(
                "model_response_finished",
                {
                    "stopReason": message.get("stopReason", "stop"),
                    "errorCode": details.get("code")
                    if isinstance(details, dict)
                    else None,
                    **_model_identity_data(
                        event,
                        fallback=self._active_model_identity,
                    ),
                },
            )]
        if event_type == "tool_execution_start":
            return [(
                "tool_started",
                {
                    "toolCallId": str(event.get("toolCallId", "")),
                    "toolName": str(event.get("toolName", "")),
                },
            )]
        if event_type == "tool_execution_dispatch_start":
            return [(
                "tool_dispatch_started",
                {
                    "toolCallId": str(event.get("toolCallId", "")),
                    "toolName": str(event.get("toolName", "")),
                    "attempt": event.get("attempt"),
                },
            )]
        if event_type == "tool_execution_end":
            result = event.get("result", {})
            details = result.get("details", {}) if isinstance(result, dict) else {}
            return [(
                "tool_finished",
                {
                    "toolCallId": str(event.get("toolCallId", "")),
                    "toolName": str(event.get("toolName", "")),
                    "success": not bool(event.get("isError")),
                    "errorCode": details.get("code")
                    if isinstance(details, dict)
                    else None,
                },
            )]
        if event_type == "turn_end":
            return [("turn_finished", {})]
        if event_type == "budget_exceeded":
            return [(
                "budget_exceeded",
                {"budget": event.get("budget"), "limit": event.get("limit")},
            )]
        if event_type == "model_retry_scheduled":
            return [("model_retry_scheduled", _retry_data(event))]
        if event_type == "model_retry_attempt_start":
            return [("model_retry_attempt_started", _retry_data(event))]
        if event_type == "model_retry_finished":
            return [("model_retry_finished", _retry_data(event))]
        if event_type == "tool_retry_scheduled":
            return [("tool_retry_scheduled", _retry_data(event))]
        if event_type == "tool_retry_attempt_start":
            return [("tool_retry_attempt_started", _retry_data(event))]
        if event_type == "tool_retry_finished":
            return [("tool_retry_finished", _retry_data(event))]
        if event_type == "context_compaction_started":
            return [("context_compaction_started", dict(event))]
        if event_type == "context_compaction_finished":
            return [("context_compaction_finished", dict(event))]
        if event_type == "agent_end":
            if cancelled or self.state.failure_code == "cancelled":
                outcome = "cancelled"
            elif self.state.failure_code is not None:
                outcome = "failed"
            else:
                outcome = "completed"
            return [("run_finished", {"outcome": outcome})]
        return []


def _model_identity_data(
    event: dict[str, Any],
    *,
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = dict(fallback or {})
    for name in ("requestId", "sessionId", "operationId"):
        value = event.get(name)
        if isinstance(value, str) and value:
            data[name] = value
    return data


def _retry_data(event: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "retryId",
        "toolCallId",
        "toolName",
        "attempt",
        "maxAttempts",
        "delayMs",
        "errorCode",
        "statusCode",
        "success",
        "finalError",
    }
    return {key: event[key] for key in allowed if key in event}
