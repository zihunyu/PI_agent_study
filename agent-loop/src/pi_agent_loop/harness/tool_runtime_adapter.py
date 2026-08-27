"""Recovery Tool Action 通过现有 Agent Tool 管线执行。"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, cast

from ..agent import Agent
from ..cancellation import CancellationToken
from ..messages import assistant_message
from ..session.resume import RecoveryAction
from ..testing import ScriptedProvider
from ..types import (
    AgentTool,
    BeforeToolCallContext,
    BeforeToolCallResult,
    Model,
)


class RecoverableToolRuntime:
    """复用参数校验、Hook、Timeout、Retry、资源锁和取消管线。"""

    def __init__(
        self,
        *,
        model: Model,
        tools: list[AgentTool],
        before_tool_call: Callable[..., Any] | None = None,
        after_tool_call: Callable[..., Any] | None = None,
        authorize_never: Callable[[RecoveryAction], Any] | None = None,
        retry_event_sink: Any | None = None,
        default_tool_timeout_seconds: float | None = None,
    ) -> None:
        self.model = model
        self.tools = {tool.name: tool for tool in tools}
        self.before_tool_call = before_tool_call
        self.after_tool_call = after_tool_call
        self.authorize_never = authorize_never
        self.retry_event_sink = retry_event_sink
        self.default_tool_timeout_seconds = default_tool_timeout_seconds

    async def execute(
        self,
        action: RecoveryAction,
        *,
        cancellation: CancellationToken | None = None,
    ) -> dict[str, Any]:
        tool = self.tools.get(action.tool_name or "")
        if tool is None:
            raise KeyError(f"恢复工具不存在：{action.tool_name}")
        if action.kind == "replay_safe_tool" and tool.replay_policy != "safe":
            raise PermissionError(f"工具 {tool.name} 不允许 Safe Replay")
        if tool.replay_policy == "never":
            if self.authorize_never is None:
                raise PermissionError(f"工具 {tool.name} 需要 Approval/Write Coordinator")
            allowed = self.authorize_never(action)
            if inspect.isawaitable(allowed):
                allowed = await cast(Awaitable[Any], allowed)
            if not allowed:
                raise PermissionError(f"工具 {tool.name} 的恢复执行未获授权")

        call_id = action.tool_call_id or "recovery-tool-call"
        first = assistant_message(
            model=self.model,
            stop_reason="toolUse",
            content=[
                {
                    "type": "toolCall",
                    "id": call_id,
                    "name": tool.name,
                    "arguments": dict(action.arguments),
                }
            ],
        )
        provider = ScriptedProvider(
            [
                first,
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "恢复工具阶段完成"}],
                ),
            ]
        )

        async def before(context: BeforeToolCallContext, token: CancellationToken):
            if self.before_tool_call is None:
                return None
            value = self.before_tool_call(context, token)
            return await cast(Awaitable[Any], value) if inspect.isawaitable(value) else value

        agent = Agent(
            model=self.model,
            stream_fn=provider.stream,
            system_prompt="执行持久 Operation 已确认的恢复 Tool Call。",
            tools=[tool],
            tool_execution="sequential",
            default_tool_timeout_seconds=self.default_tool_timeout_seconds,
            before_tool_call=before if self.before_tool_call is not None else None,
            after_tool_call=self.after_tool_call,
            retry_event_sink=self.retry_event_sink,
        )
        token = cancellation
        if token is not None and token.cancelled:
            raise RuntimeError(token.reason)
        # Agent 有自己的运行 Token；桥接 Task 保证外部取消能立即向下传播。
        prompt_task = asyncio.create_task(agent.prompt("恢复持久 Tool Call"))
        cancel_task = (
            asyncio.create_task(token.wait()) if token is not None else None
        )
        try:
            if cancel_task is None:
                await prompt_task
            else:
                done, _ = await asyncio.wait(
                    {prompt_task, cancel_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_task in done and not prompt_task.done():
                    agent.abort(token.reason)
                await prompt_task
        finally:
            if cancel_task is not None and not cancel_task.done():
                cancel_task.cancel()
            if cancel_task is not None:
                await asyncio.gather(cancel_task, return_exceptions=True)

        result = next(
            (
                message
                for message in agent.state.messages
                if message.get("role") == "toolResult"
                and message.get("toolCallId") == call_id
            ),
            None,
        )
        if result is None:
            raise RuntimeError("恢复工具没有产生 ToolResult")
        return result
