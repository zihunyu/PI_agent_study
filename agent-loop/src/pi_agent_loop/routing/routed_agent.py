"""把 Hybrid Router、Capability 和 Guard 组合到高层 Agent Host。"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable
from typing import Any, Protocol, cast

from ..agent import Agent
from ..types import AgentLoopTurnUpdate, TurnCompletedContext
from .capabilities import CapabilityRegistry
from .guard import RequiredToolCallGuard, guard_stream_fn
from .types import RequestDecision, RoutedPromptResult


class RouterLike(Protocol):
    """同步或异步业务 Router 的最小契约。"""

    def route(self, user_text: str) -> Any: ...


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
    ) -> None:
        self.agent = agent
        self.router = router
        self.capabilities = capabilities
        self.runtime_tracker = runtime_tracker
        self.operation_recorder = operation_recorder
        if runtime_tracker is not None:
            self.agent.subscribe(runtime_tracker.listener)
        if operation_recorder is not None:
            self.agent.subscribe(operation_recorder.listener)
        self.guard = RequiredToolCallGuard(capabilities)
        self.agent.stream_fn = guard_stream_fn(self.agent.stream_fn, self.guard)
        self.last_decision: RequestDecision | None = None
        self._active_decision: RequestDecision | None = None
        self._original_prepare_next_turn = self.agent.prepare_next_turn
        self.agent.prepare_next_turn = self._prepare_next_turn

    def subscribe(self, listener: Any) -> Any:
        """把 Agent 生命周期事件订阅能力透传给 UI。"""

        return self.agent.subscribe(listener)

    async def prompt(self, user_text: str) -> RoutedPromptResult:
        if self.runtime_tracker is not None:
            await self.runtime_tracker.start_run()
            await self.runtime_tracker.record_external("routing_started")
        if self.operation_recorder is not None:
            await self.operation_recorder.start_operation()
            await self.operation_recorder.record_external("routing_started")
        try:
            route_value = self.router.route(user_text)
            decision = (
                await cast(Awaitable[Any], route_value)
                if inspect.isawaitable(route_value)
                else route_value
            )
        except BaseException:
            if self.runtime_tracker is not None:
                await self.runtime_tracker.record_external(
                    "routing_finished",
                    {"status": "routing_error"},
                )
                await self.runtime_tracker.record_external(
                    "run_finished",
                    {"outcome": "failed"},
                )
            if self.operation_recorder is not None:
                await self.operation_recorder.record_external(
                    "routing_finished",
                    {"status": "routing_error"},
                )
                await self.operation_recorder.finish_operation("failed")
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
            if self.runtime_tracker is not None:
                await self.runtime_tracker.record_external(
                    "run_finished",
                    {"outcome": "completed"},
                )
            if self.operation_recorder is not None:
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
        self.agent.stream_options["expected_tool_arguments"] = dict(
            decision.extracted_fields
        )

        self._active_decision = decision
        try:
            await self.agent.prompt(user_text)
        finally:
            self._active_decision = None
        final = self.agent.state.messages[-1]
        response_text = _message_text(final)
        error_code: str | None = None
        policy_error = final.get("policyError")
        if isinstance(policy_error, dict):
            error_code = str(policy_error.get("code", "tool_policy_violation"))
        elif final.get("stopReason") in {"error", "aborted"}:
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
        next_options["expected_tool_arguments"] = {}

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
