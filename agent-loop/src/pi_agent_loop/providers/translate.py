"""把 OpenAI-compatible 流式 JSON 转换成内部 assistant 事件。"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any

from ..event_stream import AssistantMessageEventStream
from ..messages import assistant_message, empty_usage
from ..types import Model
from .errors import ProviderProtocolError


DEFAULT_MAX_TOOL_CALLS = 128
DEFAULT_MAX_TOOL_ARGUMENT_BYTES = 1024 * 1024
DEFAULT_MAX_TOTAL_TOOL_ARGUMENT_BYTES = 4 * 1024 * 1024


def _positive_limit(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} 必须是正整数")
    return value


@dataclass(slots=True)
class _ToolAccumulator:
    api_index: int
    content_index: int
    call_id: str = ""
    name: str = ""
    argument_fragments: list[str] = field(default_factory=list)
    argument_bytes: int = 0


class OpenAIStreamTranslator:
    """有状态地累积 content/tool_call delta 并推送 Pi 风格事件。"""

    def __init__(
        self,
        stream: AssistantMessageEventStream,
        model: Model,
        *,
        max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
        max_tool_argument_bytes: int = DEFAULT_MAX_TOOL_ARGUMENT_BYTES,
        max_total_tool_argument_bytes: int = DEFAULT_MAX_TOTAL_TOOL_ARGUMENT_BYTES,
    ) -> None:
        self.stream = stream
        self.model = model
        self.max_tool_calls = _positive_limit("max_tool_calls", max_tool_calls)
        self.max_tool_argument_bytes = _positive_limit(
            "max_tool_argument_bytes",
            max_tool_argument_bytes,
        )
        self.max_total_tool_argument_bytes = _positive_limit(
            "max_total_tool_argument_bytes",
            max_total_tool_argument_bytes,
        )
        self.partial = assistant_message(
            model=model,
            content=[],
            stop_reason="pending",
        )
        self._text_index: int | None = None
        self._thinking_index: int | None = None
        self._tools: dict[int, _ToolAccumulator] = {}
        self._finish_reason: str | None = None
        self._finished = False
        self._started = False
        self._saw_payload = False
        self._total_tool_argument_bytes = 0
        self.partial["usageObserved"] = False

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def saw_payload(self) -> bool:
        return self._saw_payload

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.stream.push({"type": "start", "partial": copy.deepcopy(self.partial)})

    def feed(self, payload: Any) -> None:
        if self._finished:
            return
        self._saw_payload = True
        if not isinstance(payload, dict):
            raise ProviderProtocolError("SSE data JSON 必须是对象")
        if payload.get("error") is not None:
            raise ProviderProtocolError("第三方 API 在 SSE 中返回错误对象")

        usage = payload.get("usage")
        if usage is not None:
            self.partial["usage"] = _translate_usage(usage)
            self.partial["usageObserved"] = "prompt_tokens" in usage and "completion_tokens" in usage

        choices = payload.get("choices", [])
        if not isinstance(choices, list):
            raise ProviderProtocolError("响应 choices 必须是列表")
        if not choices:
            return

        choice = next(
            (
                item
                for item in choices
                if isinstance(item, dict) and item.get("index", 0) == 0
            ),
            None,
        )
        if choice is None:
            return
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            raise ProviderProtocolError("响应 choice.delta 必须是对象")

        reasoning = delta.get("reasoning_content")
        if reasoning is None:
            reasoning = delta.get("reasoning")
        if reasoning is not None:
            if not isinstance(reasoning, str):
                raise ProviderProtocolError("reasoning_content 必须是字符串")
            self._append_thinking(reasoning)

        content = delta.get("content")
        if content is not None:
            if not isinstance(content, str):
                raise ProviderProtocolError("delta.content 必须是字符串或 null")
            self._append_text(content)

        tool_calls = delta.get("tool_calls")
        if tool_calls is not None:
            if not isinstance(tool_calls, list):
                raise ProviderProtocolError("delta.tool_calls 必须是列表")
            for fragment in tool_calls:
                self._append_tool_call(fragment)

        finish_reason = choice.get("finish_reason")
        if finish_reason is not None:
            if not isinstance(finish_reason, str):
                raise ProviderProtocolError("finish_reason 必须是字符串或 null")
            self._finish_reason = finish_reason

    def _append_text(self, delta: str) -> None:
        if self._text_index is None:
            self._text_index = len(self.partial["content"])
            self.partial["content"].append({"type": "text", "text": ""})
            self.stream.push(
                {
                    "type": "text_start",
                    "contentIndex": self._text_index,
                    "partial": copy.deepcopy(self.partial),
                }
            )
        self.partial["content"][self._text_index]["text"] += delta
        self.stream.push(
            {
                "type": "text_delta",
                "contentIndex": self._text_index,
                "delta": delta,
                "partial": copy.deepcopy(self.partial),
            }
        )

    def _append_thinking(self, delta: str) -> None:
        if self._thinking_index is None:
            self._thinking_index = len(self.partial["content"])
            self.partial["content"].append({"type": "thinking", "thinking": ""})
            self.stream.push(
                {
                    "type": "thinking_start",
                    "contentIndex": self._thinking_index,
                    "partial": copy.deepcopy(self.partial),
                }
            )
        self.partial["content"][self._thinking_index]["thinking"] += delta
        self.stream.push(
            {
                "type": "thinking_delta",
                "contentIndex": self._thinking_index,
                "delta": delta,
                "partial": copy.deepcopy(self.partial),
            }
        )

    def _append_tool_call(self, fragment: Any) -> None:
        if not isinstance(fragment, dict):
            raise ProviderProtocolError("tool_calls fragment 必须是对象")
        api_index = fragment.get("index")
        if isinstance(api_index, bool) or not isinstance(api_index, int):
            raise ProviderProtocolError("tool_calls fragment 缺少整数 index")

        accumulator = self._tools.get(api_index)
        if accumulator is None:
            if len(self._tools) >= self.max_tool_calls:
                raise ProviderProtocolError("流式 tool call 数量超过上限")
            content_index = len(self.partial["content"])
            accumulator = _ToolAccumulator(api_index, content_index)
            self._tools[api_index] = accumulator
            self.partial["content"].append(
                {
                    "type": "toolCall",
                    "id": "",
                    "name": "",
                    "arguments": {},
                }
            )
            self.stream.push(
                {
                    "type": "toolcall_start",
                    "contentIndex": content_index,
                    "partial": copy.deepcopy(self.partial),
                }
            )

        call_id = fragment.get("id")
        if call_id is not None:
            if not isinstance(call_id, str):
                raise ProviderProtocolError("tool call id fragment 必须是字符串")
            accumulator.call_id += call_id

        function = fragment.get("function") or {}
        if not isinstance(function, dict):
            raise ProviderProtocolError("tool call function 必须是对象")
        name = function.get("name")
        if name is not None:
            if not isinstance(name, str):
                raise ProviderProtocolError("tool call name fragment 必须是字符串")
            accumulator.name += name
        arguments = function.get("arguments")
        if arguments is not None:
            if not isinstance(arguments, str):
                raise ProviderProtocolError("tool call arguments fragment 必须是字符串")
            argument_bytes = len(arguments.encode("utf-8"))
            if (
                accumulator.argument_bytes + argument_bytes
                > self.max_tool_argument_bytes
            ):
                raise ProviderProtocolError("单个 tool call arguments 超过字节上限")
            if (
                self._total_tool_argument_bytes + argument_bytes
                > self.max_total_tool_argument_bytes
            ):
                raise ProviderProtocolError("全部 tool call arguments 超过累计字节上限")
            accumulator.argument_fragments.append(arguments)
            accumulator.argument_bytes += argument_bytes
            self._total_tool_argument_bytes += argument_bytes

        block = self.partial["content"][accumulator.content_index]
        block["id"] = accumulator.call_id
        block["name"] = accumulator.name
        if arguments is not None:
            self.stream.push(
                {
                    "type": "toolcall_delta",
                    "contentIndex": accumulator.content_index,
                    "delta": arguments,
                    "partial": copy.deepcopy(self.partial),
                }
            )

    def finish(self) -> None:
        if self._finished:
            return
        self._finished = True

        if self._thinking_index is not None:
            value = self.partial["content"][self._thinking_index]["thinking"]
            self.stream.push(
                {
                    "type": "thinking_end",
                    "contentIndex": self._thinking_index,
                    "content": value,
                    "partial": copy.deepcopy(self.partial),
                }
            )
        if self._text_index is not None:
            value = self.partial["content"][self._text_index]["text"]
            self.stream.push(
                {
                    "type": "text_end",
                    "contentIndex": self._text_index,
                    "content": value,
                    "partial": copy.deepcopy(self.partial),
                }
            )

        for accumulator in sorted(
            self._tools.values(), key=lambda item: item.content_index
        ):
            if not accumulator.call_id or not accumulator.name:
                raise ProviderProtocolError("流式 tool call 缺少 id 或 function.name")
            raw_arguments = "".join(accumulator.argument_fragments) or "{}"
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError as error:
                raise ProviderProtocolError(
                    f"工具 {accumulator.name} 的 arguments 不是合法 JSON"
                ) from error
            if not isinstance(arguments, dict):
                raise ProviderProtocolError(
                    f"工具 {accumulator.name} 的 arguments 必须是 JSON 对象"
                )
            final_call = {
                "type": "toolCall",
                "id": accumulator.call_id,
                "name": accumulator.name,
                "arguments": arguments,
            }
            self.partial["content"][accumulator.content_index] = final_call
            self.stream.push(
                {
                    "type": "toolcall_end",
                    "contentIndex": accumulator.content_index,
                    "toolCall": copy.deepcopy(final_call),
                    "partial": copy.deepcopy(self.partial),
                }
            )

        stop_reason = _translate_stop_reason(
            self._finish_reason,
            has_tools=bool(self._tools),
        )
        self.partial["stopReason"] = stop_reason
        final = copy.deepcopy(self.partial)
        if stop_reason == "error":
            final["errorMessage"] = "模型响应被内容安全策略阻止"
            self.stream.push({"type": "error", "reason": "error", "error": final})
        else:
            self.stream.push({"type": "done", "reason": stop_reason, "message": final})


def _translate_stop_reason(reason: str | None, *, has_tools: bool) -> str:
    if reason in {None, "stop"}:
        return "toolUse" if has_tools else "stop"
    if reason in {"tool_calls", "function_call"}:
        return "toolUse"
    if reason == "length":
        return "length"
    if reason == "content_filter":
        return "error"
    raise ProviderProtocolError(f"不支持的 finish_reason：{reason}")


def _translate_usage(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProviderProtocolError("响应 usage 必须是对象")

    def token(name: str) -> int:
        current = value.get(name, 0)
        if isinstance(current, bool) or not isinstance(current, int) or current < 0:
            raise ProviderProtocolError(f"usage.{name} 必须是非负整数")
        return current

    usage = empty_usage()
    usage["input"] = token("prompt_tokens")
    usage["output"] = token("completion_tokens")
    usage["totalTokens"] = token("total_tokens") if "total_tokens" in value else usage["input"] + usage["output"]
    return usage
