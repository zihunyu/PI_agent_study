"""Durable Recovery 到统一 :class:`ToolDispatchRuntime` 的薄适配器。"""

from __future__ import annotations

import copy
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, cast

from ..cancellation import CancellationToken
from ..session.resume import RecoveryAction
from ..tool_runtime import ToolDispatchRuntime
from ..types import AgentTool, Model


class RecoverableToolRuntime:
    """校验 Recovery Policy 后直接 Dispatch，不再创建嵌套 Agent。"""

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
        runtime: ToolDispatchRuntime | None = None,
    ) -> None:
        # model 保留在兼容签名中；直接工具执行不需要伪造模型回合。
        self.model = model
        self.tools = {tool.name: tool for tool in tools}
        self.authorize_never = authorize_never
        self.runtime = runtime or ToolDispatchRuntime(
            tools,
            before_tool_call=before_tool_call,
            after_tool_call=after_tool_call,
            retry_event_sink=retry_event_sink,
            default_tool_timeout_seconds=default_tool_timeout_seconds,
        )

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

        token = cancellation or CancellationToken()
        if token.cancelled:
            raise RuntimeError(token.reason)
        call_id = action.tool_call_id or "recovery-tool-call"
        outcome = await self.runtime.dispatch(
            {
                "type": "toolCall",
                "id": call_id,
                "name": tool.name,
                "arguments": copy.deepcopy(action.arguments),
            },
            cancellation=token,
            emit_messages=True,
        )
        if outcome.result_message is None:
            raise RuntimeError("恢复工具没有产生 ToolResult")
        return copy.deepcopy(outcome.result_message)


__all__ = ["RecoverableToolRuntime"]
