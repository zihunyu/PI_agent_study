"""把 Hybrid Router、Capability 和 Guard 组合到高层 Agent Host。"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, Protocol, cast

from ..agent import Agent
from ..cancellation import CancellationToken
from ..model_policy import ModelRequestPolicy
from ..types import AgentLoopTurnUpdate, TurnCompletedContext
from .capabilities import CapabilityRegistry
from .guard import RequiredToolCallGuard, guard_stream_fn
from .types import RequestDecision, RoutedPromptResult


class RouterLike(Protocol):
    """同步或异步业务 Router 的最小契约。"""

    def route(
        self,
        user_text: str,
        *,
        cancellation: CancellationToken | None = None,
        session_id: str | None = None,
        tenant_id: str | None = None,
    ) -> Any: ...


class RoutedAgent:
    """产品层 Agent：先路由，再决定是否调用模型以及暴露哪些工具。"""

    def __init__(
        self,
        agent: Agent,
        router: RouterLike,
        capabilities: CapabilityRegistry,
        *,
        runtime_tracker: Any | None = None,
        operation_recorder: Any | None = None,
        suspend_on_approval: bool = False,
        session_id: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        self.agent = agent
        self.router = router
        self.capabilities = capabilities
        self.runtime_tracker = runtime_tracker
        self.operation_recorder = operation_recorder
        self.suspend_on_approval = suspend_on_approval
        recorded_session = getattr(operation_recorder, "session_id", None)
        if session_id is not None and (
            not isinstance(session_id, str) or not session_id.strip()
        ):
            raise ValueError("session_id 必须是非空字符串或 None")
        if tenant_id is not None and (
            not isinstance(tenant_id, str) or not tenant_id.strip()
        ):
            raise ValueError("tenant_id 必须是非空字符串或 None")
        if (
            session_id is not None
            and isinstance(recorded_session, str)
            and recorded_session
            and session_id.strip() != recorded_session
        ):
            raise ValueError("session_id 与 Operation Recorder 不一致")
        if (
            tenant_id is not None
            and isinstance(agent.tenant_id, str)
            and agent.tenant_id
            and tenant_id.strip() != agent.tenant_id
        ):
            raise ValueError("tenant_id 与 Agent 租户不一致")
        self.session_id = (
            session_id.strip()
            if isinstance(session_id, str)
            else recorded_session
        )
        self.tenant_id = (
            tenant_id.strip()
            if isinstance(tenant_id, str)
            else agent.tenant_id
        )
        if (
            self.agent.durable_metadata_provider is None
            and (operation_recorder is not None or runtime_tracker is not None)
        ):
            def durable_metadata_provider() -> dict[str, str]:
                values = {
                    "sessionId": getattr(operation_recorder, "session_id", None),
                    "operationId": getattr(operation_recorder, "operation_id", None),
                    "runId": getattr(
                        getattr(runtime_tracker, "state", None),
                        "run_id",
                        None,
                    ),
                }
                return {
                    key: value
                    for key, value in values.items()
                    if isinstance(value, str) and value
                }

            self.agent.durable_metadata_provider = durable_metadata_provider
        if operation_recorder is not None:
            self.agent.subscribe(operation_recorder.listener)
        if runtime_tracker is not None:
            self.agent.subscribe(runtime_tracker.listener)
        self.guard = RequiredToolCallGuard(capabilities)
        self.agent.stream_fn = guard_stream_fn(self.agent.stream_fn, self.guard)
        self.last_decision: RequestDecision | None = None
        self._active_decision: RequestDecision | None = None
        self._original_prepare_next_turn = self.agent.prepare_next_turn
        self.agent.prepare_next_turn = self._prepare_next_turn

    def subscribe(self, listener: Any) -> Any:
        """把 Agent 生命周期事件订阅能力透传给 UI。"""

        return self.agent.subscribe(listener)

    async def prompt(
        self,
        user_text: str,
        *,
        route_dispatcher: Callable[[Callable[[], Awaitable[Any]]], Any] | None = None,
        allow_agent_model: bool = True,
        cancellation: CancellationToken | None = None,
    ) -> RoutedPromptResult:
        if type(allow_agent_model) is not bool:
            raise TypeError("allow_agent_model 必须是布尔值")
        if route_dispatcher is not None and not callable(route_dispatcher):
            raise TypeError("route_dispatcher 必须可调用或为 None")
        if cancellation is not None:
            # 必须早于 Runtime/Operation 打开，更不能调用 Router。
            cancellation.throw_if_cancelled()
        if self.runtime_tracker is not None:
            await self.runtime_tracker.start_run()
            await self.runtime_tracker.record_external("routing_started")
        if self.operation_recorder is not None:
            # Router 在 Agent 生命周期之前创建 Operation；先完成同一份 Transcript
            # 修复，再把 Provider 将看到的历史作为 Operation 初始 Context。
            self.agent._repair_transcript_before_run()
            await self.operation_recorder.start_operation(
                initial_messages=list(self.agent.state.messages)
            )
            await self.operation_recorder.record_external("routing_started")
        try:
            async def invoke_route() -> Any:
                route = self.router.route
                route_kwargs: dict[str, Any] = {}
                if _accepts_keyword(route, "cancellation"):
                    route_kwargs["cancellation"] = cancellation
                if _accepts_keyword(route, "session_id"):
                    route_kwargs["session_id"] = self.session_id
                if _accepts_keyword(route, "tenant_id"):
                    route_kwargs["tenant_id"] = self.tenant_id
                # 兼容旧的同步/异步自定义 Router；外层等待仍会响应取消。
                route_value = route(user_text, **route_kwargs)
                return (
                    await _await_with_cancellation(
                        cast(Awaitable[Any], route_value),
                        cancellation,
                    )
                    if inspect.isawaitable(route_value)
                    else route_value
                )

            route_value = (
                route_dispatcher(invoke_route)
                if route_dispatcher is not None
                else invoke_route()
            )
            decision = (
                await _await_with_cancellation(
                    cast(Awaitable[Any], route_value),
                    cancellation,
                )
                if inspect.isawaitable(route_value)
                else route_value
            )
            if cancellation is not None:
                # Router 与取消同时完成时，禁止继续调用回答 Provider/工具。
                cancellation.throw_if_cancelled()
            if not isinstance(decision, RequestDecision):
                raise TypeError("Router 必须返回 RequestDecision")
            decision = self._enforce_approval_boundary(decision)
        except BaseException:
            cancelled = bool(cancellation is not None and cancellation.cancelled)
            routing_status = "routing_cancelled" if cancelled else "routing_error"
            outcome = "cancelled" if cancelled else "failed"
            if self.runtime_tracker is not None:
                await self.runtime_tracker.record_external(
                    "routing_finished",
                    {"status": routing_status},
                )
                await self.runtime_tracker.record_external(
                    "run_finished",
                    {"outcome": outcome},
                )
            if self.operation_recorder is not None:
                await self.operation_recorder.record_external(
                    "routing_finished",
                    {"status": routing_status},
                )
                await self.operation_recorder.finish_operation(outcome)
            raise
        self.last_decision = decision
        if self.runtime_tracker is not None:
            await self.runtime_tracker.record_external(
                "routing_finished",
                {"status": decision.status},
            )
        if self.operation_recorder is not None:
            await self.operation_recorder.record_external(
                "routing_finished",
                {"status": decision.status},
            )

        if decision.status not in {
            "in_scope_no_tool",
            "in_scope_tool_ready",
        }:
            waiting_approval = (
                decision.status == "in_scope_approval_required"
                and self.suspend_on_approval
            )
            if self.runtime_tracker is not None:
                if waiting_approval:
                    await self.runtime_tracker.record_external(
                        "approval_required",
                        {"intent": decision.intent},
                    )
                else:
                    await self.runtime_tracker.record_external(
                        "run_finished",
                        {"outcome": "completed"},
                    )
            if self.operation_recorder is not None:
                if waiting_approval:
                    await self.operation_recorder.record_external(
                        "approval_pending",
                        {"intent": decision.intent},
                    )
                else:
                    await self.operation_recorder.finish_operation("completed")
            return RoutedPromptResult(
                decision=decision,
                model_called=False,
                response_text=decision.message,
                error_code=decision.status,
            )

        if decision.status == "in_scope_no_tool":
            selected_tools = []
        else:
            selected_tools = self.capabilities.tools_by_names(
                decision.selected_tools
            )

        if not allow_agent_model:
            message = (
                "当前请求需要进入普通 Agent 模型循环，但该循环尚未接入同一 "
                "Durable model/token/cost Admission，已按硬预算策略阻止。"
            )
            blocked = replace(
                decision,
                status="in_scope_need_clarification",
                reason=message,
                message=message,
                selected_tools=(),
            )
            if self.runtime_tracker is not None:
                await self.runtime_tracker.record_external(
                    "run_finished",
                    {"outcome": "failed", "reason": "hard_budget_agent_blocked"},
                )
            if self.operation_recorder is not None:
                await self.operation_recorder.finish_operation("failed")
            return RoutedPromptResult(
                decision=blocked,
                model_called=False,
                response_text=message,
                error_code="hard_budget_agent_model_unmetered",
            )

        # 每次请求只暴露 Router 选中的工具，避免无关工具污染模型选择。
        self.agent.state.tools = selected_tools
        self.agent.stream_options["tool_choice"] = (
            decision.tool_policy.to_openai()
        )
        self.agent.stream_options["required_capabilities"] = list(
            decision.required_capabilities
        )
        self.agent.stream_options["allowed_tool_names"] = list(
            decision.selected_tools
        )
        if (
            decision.status == "in_scope_tool_ready"
            and len(decision.selected_tools) == 1
        ):
            self.agent.stream_options["expected_tool_arguments"] = dict(
                decision.extracted_fields
            )
        else:
            # 多 Intent 由 required_capabilities 强制每项能力都出现；现有
            # expected_tool_arguments 合同只描述一个 Tool Call，不能错误套用。
            self.agent.stream_options.pop("expected_tool_arguments", None)
        self.agent.stream_options["recovery_continuation_policy"] = (
            ModelRequestPolicy(
                visible_tool_names=tuple(decision.selected_tools),
                tool_choice="auto" if decision.selected_tools else "none",
                allowed_tool_names=tuple(decision.selected_tools),
            ).to_dict()
        )

        self._active_decision = decision
        try:
            await self.agent.prompt(
                user_text,
                cancellation=cancellation,
            )
        finally:
            self._active_decision = None
        final = self.agent.state.messages[-1]
        terminal_assistant = next(
            (
                message
                for message in reversed(self.agent.state.messages)
                if message.get("role") == "assistant"
            ),
            final,
        )
        stop_reason = terminal_assistant.get("stopReason")
        response_text = _message_text(final)
        error_code: str | None = None
        policy_error = terminal_assistant.get("policyError")
        if isinstance(policy_error, dict):
            error_code = str(policy_error.get("code", "tool_policy_violation"))
        elif stop_reason == "length":
            error_code = "model_output_truncated"
            # 截断文本绝不能作为最终答复暴露给上层调用方。
            response_text = "模型响应达到长度上限，结果不完整，请重试。"
        elif stop_reason in {"error", "aborted"}:
            error_code = "model_error"
        if not response_text:
            response_text = str(
                final.get("errorMessage", "模型没有返回可显示的文本")
            )
        return RoutedPromptResult(
            decision=decision,
            model_called=True,
            response_text=response_text,
            error_code=error_code,
        )

    def _enforce_approval_boundary(
        self,
        decision: RequestDecision,
    ) -> RequestDecision:
        """在模型/Tool 执行前对所有可信审批声明取并集。

        这是最终 Host 边界，不能只相信某个具体 Router 已经正确合并策略；
        自定义 Router 同样必须经过 Tool、Capability 和 Intent 决策的复核。
        """

        if decision.status not in {
            "in_scope_no_tool",
            "in_scope_tool_ready",
            "in_scope_approval_required",
        }:
            return decision

        selected_tools = tuple(decision.selected_tools)
        if len(selected_tools) != len(set(selected_tools)):
            raise ValueError("Router 返回了重复的 selected_tools")

        requires_approval = (
            decision.requires_approval
            or decision.status == "in_scope_approval_required"
            or self._configured_intent_requires_approval(decision)
            or any(
                component.requires_approval
                or component.status == "in_scope_approval_required"
                or self._configured_intent_requires_approval(component)
                for component in decision.component_decisions
            )
        )
        if selected_tools:
            requires_approval = (
                requires_approval
                or self.capabilities.approval_required_for_tools(selected_tools)
            )
        if decision.required_capabilities:
            match = self.capabilities.match(decision.required_capabilities)
            requires_approval = requires_approval or match.requires_approval

        if not requires_approval:
            return decision
        if not selected_tools:
            raise ValueError("需要 Approval 的 Router 决策必须选择明确工具")
        return replace(
            decision,
            status="in_scope_approval_required",
            requires_approval=True,
        )

    def _configured_intent_requires_approval(
        self,
        decision: RequestDecision,
    ) -> bool:
        """从 Host 注入 Router 的只读业务配置重新读取 Intent 策略。"""

        if decision.intent is None:
            return False
        config = getattr(self.router, "config", None)
        intent_by_id = getattr(config, "intent_by_id", None)
        if not callable(intent_by_id):
            return False
        intent = intent_by_id(decision.intent)
        return bool(
            intent is not None
            and (
                intent.requires_approval
                or intent.has_side_effect
                or intent.risk in {"high", "critical"}
            )
        )

    async def _prepare_next_turn(
        self,
        context: TurnCompletedContext,
        cancellation: Any,
    ) -> AgentLoopTurnUpdate | None:
        """强制工具完成后切回 auto，允许下一轮生成最终文本。"""

        original_update: AgentLoopTurnUpdate | None = None
        if self._original_prepare_next_turn is not None:
            value = self._original_prepare_next_turn(context, cancellation)
            original_update = (
                await cast(Awaitable[Any], value)
                if inspect.isawaitable(value)
                else value
            )

        decision = self._active_decision
        has_tool_call = any(
            isinstance(block, dict) and block.get("type") == "toolCall"
            for block in context.message.get("content", [])
        )
        if (
            decision is None
            or decision.tool_policy.mode not in {"required", "named"}
            or not has_tool_call
        ):
            return original_update

        next_options = dict(
            original_update.stream_options
            if original_update is not None
            and original_update.stream_options is not None
            else self.agent.stream_options
        )
        next_options["tool_choice"] = "auto"
        next_options["required_capabilities"] = []
        next_options.pop("expected_tool_arguments", None)
        next_options.pop("recovery_continuation_policy", None)

        return AgentLoopTurnUpdate(
            context=original_update.context if original_update else None,
            model=original_update.model if original_update else None,
            thinking_level=(
                original_update.thinking_level if original_update else None
            ),
            stream_options=next_options,
        )


def _message_text(message: dict[str, Any]) -> str:
    return "".join(
        str(block.get("text", ""))
        for block in message.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _accepts_keyword(callback: Callable[..., Any], keyword: str) -> bool:
    """兼容旧 Router，同时向新 Router 传递显式请求作用域。"""

    try:
        parameters = inspect.signature(callback).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == keyword
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


async def _await_with_cancellation(
    value: Awaitable[Any],
    cancellation: CancellationToken | None,
) -> Any:
    """中断不接收 token 的异步 Router/Dispatcher，并回收两个等待任务。"""

    if cancellation is None:
        return await value
    cancellation.throw_if_cancelled()
    operation = asyncio.ensure_future(value)
    cancellation_wait = asyncio.create_task(cancellation.wait())
    try:
        done, _pending = await asyncio.wait(
            {operation, cancellation_wait},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if operation in done:
            return await operation
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
        cancellation.throw_if_cancelled()
        raise RuntimeError("Router 取消等待未产生取消原因")
    finally:
        if not operation.done():
            operation.cancel()
        if not cancellation_wait.done():
            cancellation_wait.cancel()
        await asyncio.gather(
            operation,
            cancellation_wait,
            return_exceptions=True,
        )
