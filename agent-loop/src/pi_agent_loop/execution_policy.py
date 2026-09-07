"""Trusted execution policy shared by Agent and Durable Host runtimes."""

from __future__ import annotations

import copy
import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .cancellation import CancellationToken
from ._context_transform import ContextTransformState, run_context_transform
from .safety import ContentSafetyPipeline
from .types import AfterToolCallContext, AfterToolCallResult, AgentToolResult, UNSET


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    phase: str
    tenant_id: str | None = None
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    content_safety: ContentSafetyPipeline | None = None
    transform_context: Callable[..., Any] | None = None
    version: str = "1"
    transform_timeout_seconds: float = 30.0
    _transform_state: ContextTransformState = field(
        default_factory=ContextTransformState, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        import math

        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("ExecutionPolicy.version must be non-empty text")
        if self.content_safety is not None and not isinstance(
            self.content_safety, ContentSafetyPipeline
        ):
            raise TypeError("content_safety must be ContentSafetyPipeline")
        if self.transform_context is not None and not callable(self.transform_context):
            raise TypeError("transform_context must be callable")
        value = self.transform_timeout_seconds
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError("transform_timeout_seconds must be finite and positive")

    async def transform(
        self,
        messages: list[dict[str, Any]],
        cancellation: CancellationToken,
        context: ExecutionContext,
    ) -> list[dict[str, Any]]:
        cancellation.throw_if_cancelled()
        callback = self.transform_context
        if callback is None:
            return copy.deepcopy(messages)
        child = cancellation.create_child()
        try:
            arguments: tuple[Any, ...] = (copy.deepcopy(messages), child, context)
            try:
                inspect.signature(callback).bind(*arguments)
            except TypeError:
                arguments = arguments[:2]
            result = await run_context_transform(
                callback, arguments, child,
                timeout=self.transform_timeout_seconds,
                state=self._transform_state,
            )
        finally:
            child.detach()
        if not isinstance(result, list) or any(
            not isinstance(item, dict) for item in result
        ):
            raise TypeError("context transform must return a list of messages")
        return copy.deepcopy(result)

    async def inspect_input(
        self,
        messages: list[dict[str, Any]],
        cancellation: CancellationToken,
        context: ExecutionContext,
    ) -> list[dict[str, Any]]:
        if self.content_safety is None:
            return messages
        value = await self.content_safety.inspect(
            "model_input",
            messages,
            cancellation,
            tenant_id=context.tenant_id,
            metadata={"phase": context.phase, "sessionId": context.session_id or ""},
        )
        if not isinstance(value, list) or any(
            not isinstance(item, dict) for item in value
        ):
            raise TypeError("content safety returned invalid model input")
        return copy.deepcopy(value)

    async def inspect_output(
        self,
        message: dict[str, Any],
        cancellation: CancellationToken,
        context: ExecutionContext,
    ) -> dict[str, Any]:
        if self.content_safety is None:
            return message
        value = await self.content_safety.inspect(
            "model_output",
            message,
            cancellation,
            tenant_id=context.tenant_id,
            metadata={"phase": context.phase, "sessionId": context.session_id or ""},
        )
        if not isinstance(value, dict):
            raise TypeError("content safety returned invalid model output")
        return copy.deepcopy(value)


def _apply_after_tool_override(
    result: AgentToolResult,
    is_error: bool,
    override: Any,
) -> tuple[AgentToolResult, bool]:
    """Apply the public after-hook contract before content-safety inspection."""

    if not isinstance(override, AfterToolCallResult):
        return result, is_error
    updated = AgentToolResult(
        content=(
            result.content if override.content is UNSET else list(override.content)
        ),
        details=(result.details if override.details is UNSET else override.details),
        usage=result.usage if override.usage is UNSET else override.usage,
        added_tool_names=result.added_tool_names,
        terminate=(
            result.terminate if override.terminate is UNSET else override.terminate
        ),
    )
    if override.is_error is not UNSET:
        is_error = bool(override.is_error)
    return updated, is_error


def _validate_guarded_tool_output(value: Any) -> dict[str, Any]:
    """Reject malformed policy rewrites before they re-enter the transcript."""

    if not isinstance(value, Mapping):
        raise TypeError("内容安全策略返回了无效的工具输出")
    required = {"content", "details", "usage", "terminate", "isError"}
    if set(value) != required:
        raise ValueError("内容安全工具输出字段不完整或包含未知字段")
    content = value.get("content")
    usage = value.get("usage")
    terminate = value.get("terminate")
    is_error = value.get("isError")
    if not isinstance(content, list) or any(
        not isinstance(block, dict) for block in content
    ):
        raise TypeError("内容安全工具输出 content 必须是对象列表")
    if usage is not None and not isinstance(usage, dict):
        raise TypeError("内容安全工具输出 usage 必须是对象或 None")
    if terminate is not None and not isinstance(terminate, bool):
        raise TypeError("内容安全工具输出 terminate 必须是布尔值或 None")
    if not isinstance(is_error, bool):
        raise TypeError("内容安全工具输出 isError 必须是布尔值")
    return {
        "content": copy.deepcopy(content),
        "details": copy.deepcopy(value.get("details")),
        "usage": copy.deepcopy(usage),
        "terminate": terminate,
        "isError": is_error,
    }


def guard_tool_output(
    pipeline: ContentSafetyPipeline | None,
    after_tool_call: Callable[..., Any] | None,
    *,
    tenant_id: str | None = None,
    session_id: str | None = None,
) -> Callable[[AfterToolCallContext, CancellationToken], Any] | None:
    """Expose only inspected Tool output to hooks and the transcript."""

    if pipeline is None:
        return after_tool_call

    async def inspect_output(
        result: AgentToolResult,
        is_error: bool,
        cancellation: CancellationToken,
        *,
        tool_name: str | None,
        tool_call_id: str,
    ) -> dict[str, Any]:
        inspected = await pipeline.inspect(
            "tool_output",
            {
                "content": copy.deepcopy(result.content),
                "details": copy.deepcopy(result.details),
                "usage": copy.deepcopy(result.usage),
                "terminate": result.terminate,
                "isError": is_error,
            },
            cancellation,
            tenant_id=tenant_id,
            tool_name=tool_name,
            metadata=(
                {
                    "toolCallId": tool_call_id,
                    "sessionId": session_id or "",
                    "phase": "tool",
                }
            ),
        )
        return _validate_guarded_tool_output(inspected)

    async def guarded(
        context: AfterToolCallContext,
        cancellation: CancellationToken,
    ) -> AfterToolCallResult:
        original_result = copy.deepcopy(context.result)
        original_is_error = context.is_error
        tool_name = str(context.tool_call.get("name", "")) or None
        tool_call_id = str(context.tool_call.get("id", ""))
        safe = await inspect_output(
            original_result,
            original_is_error,
            cancellation,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
        )
        if after_tool_call is not None:
            # The caller hook is an observer/rewriter, not a trusted bypass:
            # it receives the inspected snapshot and any override is applied
            # to the original result, then inspected again before release.
            safe_context = AfterToolCallContext(
                assistant_message=context.assistant_message,
                tool_call=context.tool_call,
                args=context.args,
                result=AgentToolResult(
                    content=safe["content"],
                    details=safe["details"],
                    usage=safe["usage"],
                    added_tool_names=copy.deepcopy(original_result.added_tool_names),
                    terminate=safe["terminate"],
                ),
                is_error=safe["isError"],
                context=context.context,
            )
            override = await _maybe_await(after_tool_call(safe_context, cancellation))
            if isinstance(override, AfterToolCallResult):
                overridden_result, overridden_is_error = _apply_after_tool_override(
                    original_result,
                    original_is_error,
                    override,
                )
                safe = await inspect_output(
                    overridden_result,
                    overridden_is_error,
                    cancellation,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                )
        return AfterToolCallResult(
            content=safe["content"],
            details=safe["details"],
            usage=safe["usage"],
            terminate=safe["terminate"],
            is_error=safe["isError"],
        )

    return guarded
