"""Tool Call/Tool Result 配对校验与安全补齐。"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from .messages import now_ms


class TranscriptIntegrityError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class UnresolvedToolCall:
    tool_call_id: str
    tool_name: str
    assistant_index: int


@dataclass(frozen=True, slots=True)
class TranscriptAnalysis:
    unresolved: tuple[UnresolvedToolCall, ...]


def analyze_tool_call_transcript(messages: list[dict[str, Any]]) -> TranscriptAnalysis:
    """检查重复、孤立和未闭合 Tool Call；未闭合本身不抛错。"""

    pending: dict[str, UnresolvedToolCall] = {}
    seen_calls: set[str] = set()
    seen_results: set[str] = set()
    unresolved: list[UnresolvedToolCall] = []

    for index, message in enumerate(messages):
        role = message.get("role")
        if role != "toolResult" and pending:
            unresolved.extend(pending.values())
            pending.clear()

        if role == "assistant":
            for block in message.get("content", []):
                if not isinstance(block, dict) or block.get("type") != "toolCall":
                    continue
                call_id = str(block.get("id", ""))
                name = str(block.get("name", ""))
                if not call_id or not name:
                    raise TranscriptIntegrityError(
                        "invalid_tool_call",
                        "Assistant Tool Call 缺少 id 或 name",
                    )
                if call_id in seen_calls:
                    raise TranscriptIntegrityError(
                        "duplicate_tool_call_id",
                        f"Tool Call ID 重复：{call_id}",
                    )
                seen_calls.add(call_id)
                pending[call_id] = UnresolvedToolCall(call_id, name, index)
        elif role == "toolResult":
            call_id = str(message.get("toolCallId", ""))
            if call_id in seen_results:
                raise TranscriptIntegrityError(
                    "duplicate_tool_result",
                    f"Tool Result 重复：{call_id}",
                )
            expected = pending.get(call_id)
            if expected is None:
                raise TranscriptIntegrityError(
                    "orphan_tool_result",
                    f"Tool Result 没有对应的待处理 Tool Call：{call_id}",
                )
            if str(message.get("toolName", "")) != expected.tool_name:
                raise TranscriptIntegrityError(
                    "tool_name_mismatch",
                    f"Tool Result {call_id} 的工具名不匹配",
                )
            seen_results.add(call_id)
            pending.pop(call_id)

    unresolved.extend(pending.values())
    return TranscriptAnalysis(tuple(unresolved))


def repair_unresolved_tool_calls(
    messages: list[dict[str, Any]],
    *,
    code: str = "tool_result_missing_repaired",
    text: str = "工具调用未执行，系统已补写错误结果以保持对话协议完整。",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """在下一条非 ToolResult 消息前，按模型顺序补齐未解决调用。"""

    # 先拒绝重复/孤立等无法安全自动修复的损坏。
    analyze_tool_call_transcript(messages)
    output: list[dict[str, Any]] = []
    inserted: list[dict[str, Any]] = []
    pending: dict[str, tuple[str, int]] = {}

    def close_pending() -> None:
        for call_id, (tool_name, _index) in list(pending.items()):
            result = synthetic_tool_result(
                call_id,
                tool_name,
                code=code,
                text=text,
            )
            output.append(result)
            inserted.append(result)
        pending.clear()

    for index, original in enumerate(messages):
        message = copy.deepcopy(original)
        role = message.get("role")
        if role != "toolResult" and pending:
            close_pending()
        output.append(message)
        if role == "assistant":
            for block in message.get("content", []):
                if isinstance(block, dict) and block.get("type") == "toolCall":
                    pending[str(block["id"])] = (str(block["name"]), index)
        elif role == "toolResult":
            pending.pop(str(message.get("toolCallId", "")), None)
    if pending:
        close_pending()
    return output, inserted


def validate_closed_tool_call_transcript(messages: list[dict[str, Any]]) -> None:
    analysis = analyze_tool_call_transcript(messages)
    if analysis.unresolved:
        ids = ", ".join(call.tool_call_id for call in analysis.unresolved)
        raise TranscriptIntegrityError(
            "unresolved_tool_calls",
            f"存在缺少 Tool Result 的 Tool Call：{ids}",
        )


def sanitize_terminal_assistant_tool_calls(message: dict[str, Any]) -> dict[str, Any]:
    """Error/Aborted Assistant 的 Tool Call 不可执行，提交前从最终消息移除。"""

    if message.get("role") != "assistant" or message.get("stopReason") not in {
        "error",
        "aborted",
    }:
        return message
    sanitized = copy.deepcopy(message)
    sanitized["content"] = [
        block
        for block in sanitized.get("content", [])
        if not isinstance(block, dict) or block.get("type") != "toolCall"
    ]
    return sanitized


def synthetic_tool_result(
    tool_call_id: str,
    tool_name: str,
    *,
    code: str,
    text: str,
) -> dict[str, Any]:
    return {
        "role": "toolResult",
        "toolCallId": tool_call_id,
        "toolName": tool_name,
        "content": [{"type": "text", "text": text}],
        "details": {"code": code, "synthetic": True},
        "isError": True,
        "timestamp": now_ms(),
    }
