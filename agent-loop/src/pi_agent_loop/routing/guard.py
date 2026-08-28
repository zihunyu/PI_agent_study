"""模型响应工具策略校验，以及可组合的 StreamFn Guard。"""

from __future__ import annotations

import asyncio
import copy
import inspect
from collections.abc import Awaitable
from typing import Any, cast

from ..event_stream import AssistantMessageEventStream
from ..model_policy import tool_calls_match_expected_arguments
from ..retry.model import (
    ProducerOwnedAssistantMessageEventStream,
    bind_stream_producer,
    settle_stream_producer,
)
from ..types import Model, StreamFn
from .capabilities import CapabilityRegistry
from .types import ToolChoicePolicy, ToolGuardViolation


class RequiredToolCallGuard:
    """验证模型是否真的调用了本次 Intent 要求的业务能力。"""

    def __init__(self, capabilities: CapabilityRegistry) -> None:
        self.capabilities = capabilities

    def validate(
        self,
        message: dict[str, Any],
        *,
        policy: ToolChoicePolicy,
        required_capabilities: tuple[str, ...] = (),
        allowed_tool_names: tuple[str, ...] = (),
        expected_arguments: dict[str, Any] | None = None,
    ) -> ToolGuardViolation | None:
        tool_blocks = [
            block
            for block in message.get("content", [])
            if isinstance(block, dict) and block.get("type") == "toolCall"
        ]
        called_tools = tuple(str(block.get("name", "")) for block in tool_blocks)
        called_set = set(called_tools)
        allowed_set = set(allowed_tool_names)

        if policy.mode == "none":
            if called_tools:
                return ToolGuardViolation(
                    code="unexpected_tool_call",
                    message="当前请求禁止使用工具，但模型返回了 Tool Call。",
                    required_capabilities=(),
                    called_tools=called_tools,
                )
            if expected_arguments is None:
                return None

        if allowed_set:
            unexpected = tuple(
                name for name in called_tools if name not in allowed_set
            )
            if unexpected:
                return ToolGuardViolation(
                    code="tool_not_allowed_for_intent",
                    message=(
                        "模型调用了当前 Intent 不允许的工具："
                        + "、".join(unexpected)
                    ),
                    required_capabilities=required_capabilities,
                    called_tools=called_tools,
                )

        if policy.mode == "named":
            expected = cast(str, policy.tool_name)
            if expected not in called_set:
                return ToolGuardViolation(
                    code="required_named_tool_missing",
                    message=f"模型没有调用强制指定的工具：{expected}",
                    required_capabilities=required_capabilities,
                    called_tools=called_tools,
                )

        if policy.mode == "required" and not called_tools:
            return ToolGuardViolation(
                code="required_tool_call_missing",
                message="该业务请求必须使用工具，但模型直接返回了文本。",
                required_capabilities=required_capabilities,
                called_tools=(),
                missing_capabilities=required_capabilities,
            )

        if not tool_calls_match_expected_arguments(
            tool_blocks,
            expected_arguments,
        ):
            return ToolGuardViolation(
                code="required_tool_arguments_mismatch",
                message=(
                    "模型必须在单个 Tool Call 中使用与 Router 确认值"
                    "完全一致的参数，禁止额外参数或跨调用拼凑。"
                ),
                required_capabilities=required_capabilities,
                called_tools=called_tools,
            )

        provided = self.capabilities.capabilities_for_tools(called_set)
        missing = tuple(
            capability
            for capability in required_capabilities
            if capability not in provided
        )
        if missing:
            return ToolGuardViolation(
                code="required_capability_not_called",
                message="模型没有调用满足以下业务能力的工具：" + "、".join(missing),
                required_capabilities=required_capabilities,
                called_tools=called_tools,
                missing_capabilities=missing,
            )
        return None


def guard_stream_fn(
    stream_fn: StreamFn,
    guard: RequiredToolCallGuard,
) -> StreamFn:
    """包装 Provider；强制策略未满足时把直接回答替换为结构化错误。

    `required/none/named` 模式会先缓冲模型事件，Guard 通过后才交给 Agent
    Loop，避免模型违反策略的直接文本先被 UI 当作正式回答显示。
    """

    def guarded(
        model: Model,
        context: dict[str, Any],
        options: dict[str, Any],
    ) -> AssistantMessageEventStream:
        output = ProducerOwnedAssistantMessageEventStream()
        task = asyncio.create_task(
            _pump(output, model, context, options),
            name=f"pi-model-guard:{model.provider}:{model.id}",
        )
        bind_stream_producer(output, task)
        return output

    async def _pump(
        output: AssistantMessageEventStream,
        model: Model,
        context: dict[str, Any],
        options: dict[str, Any],
    ) -> None:
        source: Any | None = None
        completed = False
        try:
            source_value = stream_fn(model, context, options)
            source = (
                await cast(Awaitable[Any], source_value)
                if inspect.isawaitable(source_value)
                else source_value
            )
            if not hasattr(source, "__aiter__") or not hasattr(source, "result"):
                raise TypeError("被 Guard 包装的 stream_fn 返回值不符合事件流契约")

            policy = _policy_from_options(options.get("tool_choice", "auto"))
            expected_arguments = _expected_arguments_from_options(options)
            required_capabilities = tuple(
                options.get("required_capabilities", ())
            )
            allowed_tool_names = tuple(
                options.get("allowed_tool_names", ())
            )
            strict = (
                policy.mode != "auto"
                or expected_arguments is not None
                or bool(required_capabilities)
                or bool(allowed_tool_names)
            )
            buffered: list[dict[str, Any]] = []
            terminal_seen = False

            async for event in source:
                if not strict:
                    output.push(event)
                    if event.get("type") in {"done", "error"}:
                        terminal_seen = True
                    continue

                buffered.append(event)
                event_type = event.get("type")
                if event_type == "error":
                    terminal_seen = True
                    for item in buffered:
                        output.push(item)
                    buffered.clear()
                elif event_type == "done":
                    terminal_seen = True
                    violation = guard.validate(
                        event["message"],
                        policy=policy,
                        required_capabilities=required_capabilities,
                        allowed_tool_names=allowed_tool_names,
                        expected_arguments=expected_arguments,
                    )
                    if violation is None:
                        for item in buffered:
                            output.push(item)
                    else:
                        output.push(
                            _violation_event(model, event["message"], violation)
                        )
                    buffered.clear()

            if not terminal_seen:
                raise RuntimeError("被 Guard 包装的 Provider 未产生终止事件")
            completed = True
        except BaseException as error:
            output.fail(error)
        finally:
            if source is not None:
                await settle_stream_producer(source, cancel=not completed)

    return guarded


def _policy_from_options(value: Any) -> ToolChoicePolicy:
    if value in (None, "auto"):
        return ToolChoicePolicy("auto")
    if value in ("none", "required"):
        return ToolChoicePolicy(value)
    if isinstance(value, dict):
        function = value.get("function")
        if value.get("type") == "function" and isinstance(function, dict):
            name = function.get("name")
            if isinstance(name, str) and name:
                return ToolChoicePolicy("named", name)
    raise ValueError("Guard 收到无效 tool_choice")


def _expected_arguments_from_options(
    options: dict[str, Any],
) -> dict[str, Any] | None:
    value = options.get("expected_tool_arguments")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Guard 收到的 expected_tool_arguments 必须是对象")
    return copy.deepcopy(value)


def _violation_event(
    model: Model,
    original_message: dict[str, Any],
    violation: ToolGuardViolation,
) -> dict[str, Any]:
    error_message = copy.deepcopy(original_message)
    error_message["role"] = "assistant"
    error_message["api"] = model.api
    error_message["provider"] = model.provider
    error_message["model"] = model.id
    error_message["content"] = []
    error_message["stopReason"] = "error"
    error_message["errorMessage"] = violation.message
    error_message["policyError"] = {
        "code": violation.code,
        "requiredCapabilities": list(violation.required_capabilities),
        "calledTools": list(violation.called_tools),
        "missingCapabilities": list(violation.missing_capabilities),
    }
    return {"type": "error", "reason": "error", "error": error_message}
