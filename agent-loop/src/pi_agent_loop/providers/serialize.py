"""把内部 Agent 消息转换成 OpenAI Chat Completions 请求。"""

from __future__ import annotations

import json
from typing import Any

from .errors import ProviderProtocolError
from .settings import ProviderProfile


def _blocks_to_text(blocks: Any, *, role: str) -> str:
    if not isinstance(blocks, list):
        raise ProviderProtocolError(f"{role} 消息 content 必须是列表")
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            raise ProviderProtocolError(f"{role} 消息包含无效 content block")
        block_type = block.get("type")
        if block_type == "text":
            parts.append(str(block.get("text", "")))
        elif role == "assistant" and block_type == "thinking":
            # 隐藏推理内容不重新发送为普通文本。
            continue
        elif role == "assistant" and block_type == "toolCall":
            continue
        else:
            raise ProviderProtocolError(
                f"当前 OpenAI-compatible Provider 不支持 {role} "
                f"content 类型：{block_type!r}"
            )
    return "".join(parts)


def _serialize_assistant(message: dict[str, Any]) -> dict[str, Any]:
    content = message.get("content", [])
    text = _blocks_to_text(content, role="assistant")
    tool_calls: list[dict[str, Any]] = []
    for block in content:
        if block.get("type") != "toolCall":
            continue
        call_id = block.get("id")
        name = block.get("name")
        arguments = block.get("arguments", {})
        if not isinstance(call_id, str) or not call_id:
            raise ProviderProtocolError("assistant toolCall 缺少 id")
        if not isinstance(name, str) or not name:
            raise ProviderProtocolError("assistant toolCall 缺少 name")
        tool_calls.append(
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(
                        arguments,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            }
        )

    serialized: dict[str, Any] = {
        "role": "assistant",
        "content": text if text else (None if tool_calls else ""),
    }
    if tool_calls:
        serialized["tool_calls"] = tool_calls
    return serialized


def _serialize_message(message: dict[str, Any]) -> dict[str, Any]:
    role = message.get("role")
    if role == "user":
        return {
            "role": "user",
            "content": _blocks_to_text(message.get("content"), role="user"),
        }
    if role == "assistant":
        return _serialize_assistant(message)
    if role == "toolResult":
        call_id = message.get("toolCallId")
        if not isinstance(call_id, str) or not call_id:
            raise ProviderProtocolError("toolResult 消息缺少 toolCallId")
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "content": _blocks_to_text(
                message.get("content"), role="toolResult"
            ),
        }
    raise ProviderProtocolError(f"不支持发送给模型的消息角色：{role!r}")


def _serialize_tool(tool: Any) -> dict[str, Any]:
    if not isinstance(tool, dict):
        raise ProviderProtocolError("工具定义必须是对象")
    name = tool.get("name")
    description = tool.get("description")
    parameters = tool.get("parameters")
    if not isinstance(name, str) or not name:
        raise ProviderProtocolError("工具定义缺少 name")
    if not isinstance(description, str):
        raise ProviderProtocolError(f"工具 {name} 的 description 必须是字符串")
    if not isinstance(parameters, dict):
        raise ProviderProtocolError(f"工具 {name} 的 parameters 必须是对象")
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


def serialize_chat_request(
    profile: ProviderProfile,
    context: dict[str, Any],
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 OpenAI-compatible `/chat/completions` JSON 请求体。"""

    options = options or {}
    raw_messages = context.get("messages", [])
    if not isinstance(raw_messages, list):
        raise ProviderProtocolError("模型 context.messages 必须是列表")

    messages: list[dict[str, Any]] = []
    system_prompt = context.get("systemPrompt", "")
    if system_prompt:
        if not isinstance(system_prompt, str):
            raise ProviderProtocolError("systemPrompt 必须是字符串")
        messages.append({"role": "system", "content": system_prompt})
    messages.extend(_serialize_message(message) for message in raw_messages)

    payload: dict[str, Any] = {
        "model": profile.model,
        "messages": messages,
        "stream": True,
    }

    raw_tools = context.get("tools", [])
    if not isinstance(raw_tools, list):
        raise ProviderProtocolError("模型 context.tools 必须是列表")
    if raw_tools:
        payload["tools"] = [_serialize_tool(tool) for tool in raw_tools]
        available_names = {
            tool["function"]["name"] for tool in payload["tools"]
        }
        payload["tool_choice"] = _serialize_tool_choice(
            options.get("tool_choice", "auto"),
            available_names,
        )
    elif (
        options.get("tool_choice") is not None
        and options.get("tool_choice") not in ("none", "auto")
    ):
        raise ProviderProtocolError(
            "当前没有可用工具，不能使用 required 或 named tool_choice"
        )

    # reasoning_effort 并非所有兼容服务都支持，只有调用方显式要求时才发送。
    reasoning = options.get("reasoning")
    if reasoning:
        payload["reasoning_effort"] = reasoning
    return payload


def _serialize_tool_choice(
    value: Any,
    available_names: set[str],
) -> str | dict[str, Any]:
    """校验并返回 OpenAI Chat Completions 的工具选择策略。"""

    if isinstance(value, str) and value in {"none", "auto", "required"}:
        return value
    if not isinstance(value, dict):
        raise ProviderProtocolError(
            "tool_choice 必须是 none、auto、required 或 named function 对象"
        )
    if value.get("type") != "function":
        raise ProviderProtocolError("named tool_choice.type 必须是 function")
    function = value.get("function")
    if not isinstance(function, dict):
        raise ProviderProtocolError("named tool_choice.function 必须是对象")
    if set(function) != {"name"}:
        raise ProviderProtocolError(
            "named tool_choice.function 只能包含 name"
        )
    name = function.get("name")
    if not isinstance(name, str) or not name:
        raise ProviderProtocolError("named tool_choice 缺少工具名称")
    if name not in available_names:
        raise ProviderProtocolError(f"named tool_choice 指定了不可见工具：{name}")
    return {"type": "function", "function": {"name": name}}
