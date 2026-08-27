"""Recovery 使用的正式 Model Runtime Adapter。"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable
from typing import Any, cast

from ..cancellation import CancellationToken
from ..model_policy import (
    ModelRequestPolicy,
    ModelRequestPolicyError,
    validate_model_response_policy,
)
from ..types import AgentTool, Model, StreamFn


class RecoverableModelRuntime:
    """通过现有 StreamFn 请求模型，保留 Retry/Circuit/Compaction 管线。"""

    def __init__(
        self,
        *,
        model: Model,
        stream_fn: StreamFn,
        system_prompt: str,
        tools: list[AgentTool],
        retry_event_sink: Any | None = None,
    ) -> None:
        self.model = model
        self.stream_fn = stream_fn
        self.system_prompt = system_prompt
        self.tools = list(tools)
        self._tools_by_name = {tool.name: tool for tool in self.tools}
        self.retry_event_sink = retry_event_sink

    async def request(
        self,
        messages: list[dict[str, Any]],
        *,
        policy: ModelRequestPolicy,
        cancellation: CancellationToken | None = None,
    ) -> dict[str, Any]:
        if not isinstance(policy, ModelRequestPolicy):
            raise ModelRequestPolicyError(
                "恢复模型请求必须提供持久化 ModelRequestPolicy"
            )
        missing = [
            name
            for name in policy.visible_tool_names
            if name not in self._tools_by_name
        ]
        if missing:
            raise ModelRequestPolicyError(
                "当前 Runtime 缺少策略要求的工具：" + "、".join(missing)
            )
        selected_tools = [
            self._tools_by_name[name]
            for name in policy.visible_tool_names
        ]
        token = cancellation or CancellationToken()
        context = {
            "systemPrompt": self.system_prompt,
            "messages": list(messages),
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                }
                for tool in selected_tools
            ],
        }
        options = {
            "cancellation_token": token,
            "tool_choice": policy.tool_choice,
            "required_capabilities": list(policy.required_capabilities),
            "allowed_tool_names": list(policy.allowed_tool_names),
            "expected_tool_arguments": dict(
                policy.expected_tool_arguments
            ),
            "retry_event_sink": self.retry_event_sink,
        }
        value = self.stream_fn(self.model, context, options)
        stream = (
            await cast(Awaitable[Any], value)
            if inspect.isawaitable(value)
            else value
        )
        if not hasattr(stream, "__aiter__") or not hasattr(stream, "result"):
            raise TypeError("RecoverableModelRuntime 收到无效 StreamFn 结果")
        async for _event in stream:
            pass
        message = await stream.result()
        if message.get("stopReason") in {"error", "aborted"}:
            raise RuntimeError(
                str(message.get("errorMessage", "恢复模型请求失败"))
            )
        validate_model_response_policy(message, policy)
        return message
