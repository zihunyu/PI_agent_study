"""把 Agent 生命周期持久化为可恢复 Operation Event。"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from ..tool_contract import ToolSecurityContract
from ..types import AgentTool
from .operation_state import replay_operation, replay_operation_with_specs
from .operation_store import (
    ClaimLease,
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
        run_id_provider: Callable[[], str | None] | None = None,
        fenced_lease: ClaimLease | None = None,
        fenced_lease_seconds: float | None = None,
    ) -> None:
        if not session_id:
            raise ValueError("session_id 不能为空")
        self.store = store
        self.session_id = session_id
        self.tools = {tool.name: tool for tool in tools}
        if len(self.tools) != len(tools):
            raise ValueError("Durable Recorder 不接受重复 Tool 名称")
        self.tool_contracts = {
            name: ToolSecurityContract.capture(tool)
            for name, tool in self.tools.items()
        }
        self.configuration = dict(configuration or {})
        self.run_id_provider = run_id_provider
        if fenced_lease is not None:
            if (
                fenced_lease.claim_type != "conversation_session_writer"
                or fenced_lease.resource_id != session_id
            ):
                raise ValueError("Recorder Fenced Lease 与 Session 不匹配")
            if fenced_lease_seconds is None or fenced_lease_seconds <= 0:
                raise ValueError("Recorder Fenced Lease 必须提供正数续租时长")
        elif fenced_lease_seconds is not None:
            raise ValueError("fenced_lease_seconds 只能与 fenced_lease 一起提供")
        self.fenced_lease = fenced_lease
        self.fenced_lease_seconds = fenced_lease_seconds
        self.operation_id: str | None = None
        self.last_operation_id: str | None = None
        self._active_request_id: str | None = None
        self._last_failure: str | None = None

    def current_durable_metadata(self) -> dict[str, str]:
        """返回供可信 Agent/Host 装配边界使用的当前请求关联标识。"""

        values: dict[str, str | None] = {
            "sessionId": self.session_id,
            "operationId": self.operation_id,
            "runId": self.run_id_provider() if self.run_id_provider is not None else None,
        }
        return {
            key: value
            for key, value in values.items()
            if isinstance(value, str) and value
        }

    async def start_operation(
        self,
        initial_messages: list[dict[str, Any]] | None = None,
    ) -> str:
        if self.operation_id is not None:
            raise RuntimeError("已有活动 Durable Operation")
        self.operation_id = str(uuid4())
        self.last_operation_id = self.operation_id
        self._last_failure = None
        specs: list[tuple[str, dict[str, Any]]] = [
            (
                "operation_started",
                {
                    "configuration": self.configuration,
                    "tools": [
                        {
                            **contract.to_dict(),
                            "securityContractDigest": contract.digest,
                        }
                        for contract in self.tool_contracts.values()
                    ],
                },
            )
        ]
        specs.extend(
            (
                "message_appended",
                {"message": copy.deepcopy(message), "initialContext": True},
            )
            for message in list(initial_messages or [])
        )
        try:
            await self._append_batch(
                self.session_id,
                self.operation_id,
                specs,
                expected_last_sequence=-1,
            )
        except BaseException:
            self.operation_id = None
            self.last_operation_id = None
            raise
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
                await self._append_batch(
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
            context_messages = event.get("contextMessages")
            initial_messages = (
                copy.deepcopy(context_messages)
                if isinstance(context_messages, list)
                else []
            )
            if self.operation_id is None:
                await self.start_operation(initial_messages=initial_messages)
            else:
                await self._synchronize_context(initial_messages)
            return
        if self.operation_id is None:
            return
        if event_type == "transcript_repaired":
            message = event.get("message")
            if isinstance(message, dict):
                if message.get("role") == "toolResult":
                    await self._record_tool_result_message(message)
                else:
                    await self._append(
                        "message_appended",
                        {"message": message, "syntheticRepair": True},
                    )
            context_messages = event.get("contextMessages")
            if isinstance(context_messages, list):
                await self._synchronize_context(context_messages)
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
            supplied_request_id = event.get("requestId")
            self._active_request_id = (
                supplied_request_id
                if isinstance(supplied_request_id, str) and supplied_request_id
                else str(uuid4())
            )
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
                if stop_reason in {"error", "aborted", "length"}:
                    if stop_reason == "aborted":
                        self._last_failure = "cancelled"
                    elif stop_reason == "length":
                        self._last_failure = "model_output_truncated"
                    else:
                        self._last_failure = "model_error"
                if stop_reason == "length":
                    # length 是不完整响应，不能伪装成 Model Request Completed。
                    # 同一批次仍保存 Assistant Message，使随后生成的 Synthetic
                    # ToolResult 可与截断 Tool Call 形成闭合 Transcript。
                    operation_id = self.operation_id
                    if operation_id is None:
                        raise RuntimeError("没有活动 Durable Operation")
                    await self._append_batch(
                        self.session_id,
                        operation_id,
                        [
                            (
                                "model_request_failed",
                                {
                                    "requestId": self._active_request_id,
                                    "errorCode": "model_output_truncated",
                                },
                            ),
                            ("message_appended", {"message": message}),
                        ],
                    )
                else:
                    await self._append(
                        "model_request_completed",
                        {
                            "requestId": self._active_request_id,
                            "message": message,
                        },
                    )
                self._active_request_id = None
            elif role == "toolResult":
                await self._record_tool_result_message(message)
            elif role == "user":
                await self._append("message_appended", {"message": message})
            return
        if event_type == "tool_execution_start":
            tool_name = str(event.get("toolName", ""))
            contract = self.tool_contracts.get(tool_name)
            await self._append(
                "tool_intent_recorded",
                {
                    "toolCallId": str(event.get("toolCallId", "")),
                    "toolName": tool_name,
                    "arguments": event.get("args", {}),
                    "replayPolicy": (
                        contract.replay_policy if contract is not None else "never"
                    ),
                    "securityContractDigest": (
                        contract.digest if contract is not None else None
                    ),
                    "implementationVersion": (
                        contract.implementation_version
                        if contract is not None
                        else None
                    ),
                    "securityPolicyVersion": (
                        contract.security_policy_version
                        if contract is not None
                        else None
                    ),
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
            committed_message = event.get("toolResultMessage")
            if isinstance(committed_message, dict):
                await self._record_tool_result_message(committed_message)
                return
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

    async def _synchronize_context(
        self,
        context_messages: list[dict[str, Any]],
    ) -> None:
        """确保 Operation 消息是 Agent/Provider Context 的无重复前缀。"""

        if self.operation_id is None or not context_messages:
            return
        expected = copy.deepcopy(context_messages)
        for _ in range(20):
            events = await self.store.load(
                session_id=self.session_id,
                operation_id=self.operation_id,
            )
            operation = replay_operation(events)
            existing = list(operation.messages)
            if existing == expected:
                return
            if len(existing) > len(expected) and existing[: len(expected)] == expected:
                return
            if existing != expected[: len(existing)]:
                raise RuntimeError(
                    "Durable Operation Context 与 Agent Provider Context 不一致"
                )
            missing = expected[len(existing) :]
            if not missing:
                return
            specs = [
                (
                    "message_appended",
                    {"message": copy.deepcopy(message), "contextSync": True},
                )
                for message in missing
            ]
            replay_operation_with_specs(events, specs)
            try:
                await self._append_batch(
                    self.session_id,
                    self.operation_id,
                    specs,
                    expected_last_sequence=operation_last_sequence(events),
                )
            except OperationStoreConflictError:
                continue
            return
        raise RuntimeError("Durable Operation Context 同步并发冲突")

    async def _record_tool_result_message(
        self,
        message: dict[str, Any],
    ) -> None:
        if self.operation_id is None:
            raise RuntimeError("没有活动 Durable Operation")
        tool_call_id = str(message.get("toolCallId", ""))
        for _ in range(20):
            events = await self.store.load(
                session_id=self.session_id,
                operation_id=self.operation_id,
            )
            operation = replay_operation(events)
            if any(
                item.get("role") == "toolResult"
                and item.get("toolCallId") == tool_call_id
                for item in operation.messages
            ):
                return
            invocation = operation.tools.get(tool_call_id)
            specs: list[tuple[str, dict[str, Any]]] = []
            if invocation is not None and invocation.phase in {
                "intent_recorded",
                "dispatch_started",
            }:
                details = message.get("details", {})
                outcome_unknown = (
                    isinstance(details, dict)
                    and details.get("code") == "outcome_unknown"
                )
                specs.append(
                    (
                        "tool_outcome_unknown"
                        if outcome_unknown
                        else "tool_completed",
                        {
                            "toolCallId": tool_call_id,
                            "toolName": message.get("toolName"),
                            "result": {
                                "content": message.get("content", []),
                                "details": details,
                                "isError": bool(message.get("isError")),
                            },
                            "recoveredAtCommitBoundary": True,
                        },
                    )
                )
            specs.append(("message_appended", {"message": message}))
            replay_operation_with_specs(events, specs)
            try:
                await self._append_batch(
                    self.session_id,
                    self.operation_id,
                    specs,
                    expected_last_sequence=operation_last_sequence(events),
                )
            except OperationStoreConflictError:
                continue
            return
        raise RuntimeError("Tool Result Commit Boundary 并发冲突")

    async def _append(self, event_type: str, data: dict[str, Any]) -> None:
        if self.operation_id is None:
            raise RuntimeError("没有活动 Durable Operation")
        await self._append_batch(
            self.session_id,
            self.operation_id,
            [(event_type, data)],
        )

    async def _append_batch(
        self,
        session_id: str,
        operation_id: str,
        events: list[tuple[str, dict[str, Any]]],
        *,
        expected_last_sequence: int | None = None,
        deadline_ms: int | None = None,
    ) -> list[Any]:
        if self.fenced_lease is None:
            return await self.store.append_batch(
                session_id,
                operation_id,
                events,
                expected_last_sequence=expected_last_sequence,
                deadline_ms=deadline_ms,
            )
        append_fenced = getattr(
            self.store, "append_batch_if_fenced_claim", None
        )
        if not callable(append_fenced):
            raise OperationStoreConflictError(
                "Operation Store 不支持原子 Fenced Append"
            )
        return await append_fenced(
            session_id,
            operation_id,
            events,
            self.fenced_lease,
            renew_lease_seconds=self.fenced_lease_seconds,
            expected_last_sequence=expected_last_sequence,
            deadline_ms=deadline_ms,
        )
