"""统一装配 Agent、持久状态、恢复、Approval 和写操作的 Host。"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..agent import Agent
from ..approval import ApprovalService
from ..messages import assistant_message, user_message
from ..retry.compaction import CompactionRetryPolicy, compact_on_context_overflow
from ..retry.events import JsonlRetryEventStore
from ..routing.capabilities import CapabilityRegistry
from ..routing.routed_agent import RoutedAgent
from ..runtime import RuntimeStateTracker
from ..session import (
    DurableOperationRecorder,
    JsonlOperationEventStore,
    JsonlRuntimeEventStore,
    RecoveryCallbacks,
    RuntimeRecoveryManager,
)
from ..security import VerifiedIdentity
from ..session.operation_state import replay_operation
from ..types import AgentTool, Model, StreamFn
from ..writes import WriteOperationService
from .approval_gateway import ApprovalResumeCoordinator
from .model_runtime_adapter import RecoverableModelRuntime
from .startup_recovery import StartupRecoveryCoordinator, StartupRecoveryReport
from .tool_runtime_adapter import RecoverableToolRuntime


@dataclass(frozen=True, slots=True)
class DurableHostPromptResult:
    result: Any
    operation_id: str | None
    approval_id: str | None = None


class DurableAgentHost:
    """P1 统一编排入口；具体业务通过 Router、Tool 和 Adapter 注入。"""

    def __init__(self) -> None:
        # 使用 async create()；私有字段在创建完成前不对外可用。
        self.session_id: str
        self.model: Model
        self.agent: Agent
        self.routed_agent: RoutedAgent | None
        self.runtime_tracker: RuntimeStateTracker
        self.operation_recorder: DurableOperationRecorder
        self.operation_store: JsonlOperationEventStore
        self.approvals: ApprovalService
        self.approval_resume: ApprovalResumeCoordinator
        self.writes: WriteOperationService
        self.model_runtime: RecoverableModelRuntime
        self.tool_runtime: RecoverableToolRuntime
        self.startup_recovery: StartupRecoveryCoordinator
        self.startup_recovery_report: StartupRecoveryReport | None

    @classmethod
    async def create(
        cls,
        *,
        session_id: str,
        state_dir: str | Path,
        model: Model,
        stream_fn: StreamFn,
        system_prompt: str,
        tools: list[AgentTool],
        router: Any | None = None,
        capabilities: CapabilityRegistry | None = None,
        before_tool_call: Callable[..., Any] | None = None,
        after_tool_call: Callable[..., Any] | None = None,
        reconcile_tool: Callable[..., Any] | None = None,
        authorize_never_replay: Callable[..., Any] | None = None,
        max_turns: int | None = None,
        max_tool_calls: int | None = None,
        max_parallel_tools: int | None = None,
        default_tool_timeout_seconds: float | None = None,
        compaction_policy: CompactionRetryPolicy | None = None,
        auto_recover: bool = True,
    ) -> "DurableAgentHost":
        if not session_id:
            raise ValueError("session_id 不能为空")
        self = cls()
        self.session_id = session_id
        self.model = model
        root = Path(state_dir)
        retry_store = JsonlRetryEventStore(root / "retry-events.jsonl")
        runtime_store = JsonlRuntimeEventStore(root / "runtime-events.jsonl")
        self.operation_store = JsonlOperationEventStore(
            root / "operation-events.jsonl"
        )
        await RuntimeRecoveryManager(runtime_store).recover()
        self.runtime_tracker = await RuntimeStateTracker.create(runtime_store)
        self.operation_recorder = DurableOperationRecorder(
            self.operation_store,
            session_id=session_id,
            tools=tools,
            configuration={"provider": model.provider, "model": model.id},
        )

        effective_stream = compact_on_context_overflow(
            stream_fn,
            compaction_policy
            or CompactionRetryPolicy(max_retries=1, keep_recent_messages=20),
        )
        self.agent = Agent(
            model=model,
            stream_fn=effective_stream,
            system_prompt=system_prompt,
            tools=tools,
            before_tool_call=before_tool_call,
            after_tool_call=after_tool_call,
            max_turns=max_turns,
            max_tool_calls=max_tool_calls,
            max_parallel_tools=max_parallel_tools,
            default_tool_timeout_seconds=default_tool_timeout_seconds,
            retry_event_sink=retry_store.append,
        )

        if router is not None:
            if capabilities is None:
                raise ValueError("使用 Router 时必须提供 CapabilityRegistry")
            self.routed_agent = RoutedAgent(
                self.agent,
                router,
                capabilities,
                runtime_tracker=self.runtime_tracker,
                operation_recorder=self.operation_recorder,
                suspend_on_approval=True,
            )
        else:
            self.routed_agent = None
            self.agent.subscribe(self.runtime_tracker.listener)
            self.agent.subscribe(self.operation_recorder.listener)

        self.model_runtime = RecoverableModelRuntime(
            model=model,
            stream_fn=effective_stream,
            system_prompt=system_prompt,
            tools=tools,
            retry_event_sink=retry_store.append,
        )
        self.tool_runtime = RecoverableToolRuntime(
            model=model,
            tools=tools,
            before_tool_call=before_tool_call,
            after_tool_call=after_tool_call,
            authorize_never=authorize_never_replay,
            retry_event_sink=retry_store.append,
            default_tool_timeout_seconds=default_tool_timeout_seconds,
        )

        async def default_reconcile(action):
            if reconcile_tool is None:
                raise RuntimeError(
                    f"工具 {action.tool_name} 需要 Reconciliation Adapter"
                )
            value = reconcile_tool(action)
            if hasattr(value, "__await__"):
                return await value
            return value

        callbacks = RecoveryCallbacks(
            request_model=lambda messages: self.model_runtime.request(messages),
            execute_tool=lambda action: self.tool_runtime.execute(action),
            reconcile_tool=default_reconcile,
        )
        self.startup_recovery = StartupRecoveryCoordinator(
            self.operation_store,
            callbacks,
        )
        self.approvals = ApprovalService(self.operation_store)
        self.approval_resume = ApprovalResumeCoordinator(
            self.operation_store,
            self.approvals,
        )
        self.writes = WriteOperationService(
            self.operation_store,
            self.approvals,
        )
        self.startup_recovery_report = (
            await self.startup_recovery.recover_all()
            if auto_recover
            else None
        )
        return self

    async def prompt(
        self,
        text: str,
        *,
        requester: VerifiedIdentity | None = None,
        approval_role: str = "approver",
    ) -> DurableHostPromptResult:
        if self.routed_agent is None:
            await self.agent.prompt(text)
            return DurableHostPromptResult(
                result=None,
                operation_id=self.operation_recorder.last_operation_id,
            )

        result = await self.routed_agent.prompt(text)
        if result.decision.status != "in_scope_approval_required":
            return DurableHostPromptResult(
                result=result,
                operation_id=self.operation_recorder.last_operation_id,
            )
        if requester is None:
            if (
                self.runtime_tracker.state.run_id is not None
                and not self.runtime_tracker.state.terminal
            ):
                await self.runtime_tracker.record_external(
                    "run_finished",
                    {"outcome": "failed"},
                )
            if self.operation_recorder.operation_id is not None:
                await self.operation_recorder.finish_operation("failed")
            raise PermissionError("该请求需要 Approval，必须提供 VerifiedIdentity")
        if len(result.decision.selected_tools) != 1:
            if (
                self.runtime_tracker.state.run_id is not None
                and not self.runtime_tracker.state.terminal
            ):
                await self.runtime_tracker.record_external(
                    "run_finished",
                    {"outcome": "failed"},
                )
            if self.operation_recorder.operation_id is not None:
                await self.operation_recorder.finish_operation("failed")
            raise RuntimeError("Approval Resume 当前要求一个明确的写工具")
        operation_id = self.operation_recorder.operation_id
        if operation_id is None:
            raise RuntimeError("Approval Required 但 Durable Operation 未保持活动")

        tool_name = result.decision.selected_tools[0]
        tool_call_id = str(uuid4())
        arguments = dict(result.decision.extracted_fields)
        await self.operation_recorder.record_external(
            "message_appended",
            {"message": user_message(text)},
        )
        request_id = str(uuid4())
        planned_call = assistant_message(
            model=self.model,
            stop_reason="toolUse",
            content=[
                {
                    "type": "toolCall",
                    "id": tool_call_id,
                    "name": tool_name,
                    "arguments": arguments,
                }
            ],
        )
        await self.operation_recorder.record_external(
            "model_request_started",
            {"requestId": request_id, "source": "host_approval_plan"},
        )
        await self.operation_recorder.record_external(
            "model_request_completed",
            {
                "requestId": request_id,
                "message": planned_call,
                "source": "host_approval_plan",
            },
        )
        pending = await self.approval_resume.request(
            session_id=self.session_id,
            operation_id=operation_id,
            requester=requester,
            action={"tool": tool_name, "arguments": arguments},
            action_summary=f"执行 {result.decision.intent or tool_name}",
            required_role=approval_role,
            resume_payload={
                "operationId": operation_id,
                "toolCallId": tool_call_id,
                "toolName": tool_name,
                "arguments": arguments,
                "intent": result.decision.intent,
            },
        )
        return DurableHostPromptResult(
            result=result,
            operation_id=operation_id,
            approval_id=pending.approval.approval_id,
        )

    async def approve_and_resume(
        self,
        approval_id: str,
        *,
        approver: VerifiedIdentity,
        consumer: VerifiedIdentity,
        idempotency_key: str,
        write_handler: Callable[..., Any],
    ) -> Any:
        resumed_operation_id: str | None = None

        async def resume(payload: dict[str, Any]) -> Any:
            nonlocal resumed_operation_id
            operation_id = str(payload["operationId"])
            resumed_operation_id = operation_id
            if (
                self.runtime_tracker.state.run_id is not None
                and not self.runtime_tracker.state.terminal
                and self.runtime_tracker.state.phase == "waiting_approval"
            ):
                await self.runtime_tracker.record_external("approval_granted")
            tool_call_id = str(payload["toolCallId"])
            tool_name = str(payload["toolName"])
            arguments = dict(payload.get("arguments", {}))
            write = await self.writes.prepare(
                session_id=self.session_id,
                operation_id=operation_id,
                tool_name=tool_name,
                arguments=arguments,
                idempotency_key=idempotency_key,
                requester=consumer,
                requires_approval=False,
            )

            async def invoke(args, key, actor):
                value = write_handler(args, key, actor)
                if hasattr(value, "__await__"):
                    return await value
                return value

            await self.operation_store.append(
                "tool_intent_recorded",
                self.session_id,
                operation_id,
                {
                    "toolCallId": tool_call_id,
                    "toolName": tool_name,
                    "arguments": arguments,
                    "replayPolicy": "never",
                    "source": "approval_resume",
                },
            )
            await self.operation_store.append(
                "tool_dispatch_started",
                self.session_id,
                operation_id,
                {"toolCallId": tool_call_id, "source": "approval_resume"},
            )
            write = await self.writes.execute(
                write.write_id,
                actor=consumer,
                idempotency_key=idempotency_key,
                handler=invoke,
            )
            tool_result = {
                "role": "toolResult",
                "toolCallId": tool_call_id,
                "toolName": tool_name,
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(write.result or {}, ensure_ascii=False),
                    }
                ],
                "details": write.result or {},
                "isError": False,
            }
            await self.operation_store.append(
                "tool_completed",
                self.session_id,
                operation_id,
                {
                    "toolCallId": tool_call_id,
                    "result": {
                        "content": tool_result["content"],
                        "details": tool_result["details"],
                        "isError": False,
                    },
                },
            )
            await self.operation_store.append(
                "message_appended",
                self.session_id,
                operation_id,
                {"message": tool_result},
            )
            operation = replay_operation(
                await self.operation_store.load(
                    session_id=self.session_id,
                    operation_id=operation_id,
                )
            )
            request_id = str(uuid4())
            await self.operation_store.append(
                "model_request_started",
                self.session_id,
                operation_id,
                {"requestId": request_id, "source": "approval_resume"},
            )
            final = await self.model_runtime.request(list(operation.messages))
            await self.operation_store.append(
                "model_request_completed",
                self.session_id,
                operation_id,
                {"requestId": request_id, "message": final},
            )
            return final

        try:
            result = await self.approval_resume.approve_and_resume(
                approval_id,
                approver=approver,
                consumer=consumer,
                resume=resume,
            )
        except Exception:
            if resumed_operation_id is not None:
                if self.operation_recorder.operation_id == resumed_operation_id:
                    await self.operation_recorder.finish_operation("failed")
                else:
                    await self.operation_store.append(
                        "operation_finished",
                        self.session_id,
                        resumed_operation_id,
                        {"outcome": "failed"},
                    )
            if (
                self.runtime_tracker.state.run_id is not None
                and not self.runtime_tracker.state.terminal
            ):
                await self.runtime_tracker.record_external(
                    "run_finished",
                    {"outcome": "failed"},
                )
            raise
        if resumed_operation_id is not None:
            if self.operation_recorder.operation_id == resumed_operation_id:
                await self.operation_recorder.finish_operation("completed")
            else:
                await self.operation_store.append(
                    "operation_finished",
                    self.session_id,
                    resumed_operation_id,
                    {"outcome": "completed"},
                )
        if (
            self.runtime_tracker.state.run_id is not None
            and not self.runtime_tracker.state.terminal
        ):
            await self.runtime_tracker.record_external(
                "run_finished",
                {"outcome": "completed"},
            )
        return result

    async def recover_on_startup(self) -> StartupRecoveryReport:
        return await self.startup_recovery.recover_all()

    async def close(self) -> None:
        if self.agent.state.is_streaming:
            self.agent.abort("DurableAgentHost 正在关闭")
        await self.agent.wait_for_idle()
