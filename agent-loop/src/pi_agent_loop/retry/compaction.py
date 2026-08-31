"""Context Overflow 时压缩消息并在同一逻辑 Turn 重试一次。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from ..event_stream import AssistantMessageEventStream
from ..transcript import TranscriptIntegrityError, validate_closed_tool_call_transcript
from ..types import Model, StreamFn
from .model import (
    ProducerOwnedAssistantMessageEventStream,
    bind_stream_producer,
    settle_stream_producer,
)


class ContextCompactionValidationError(RuntimeError):
    """压缩结果无法证明与原 Context 对应，或破坏了 Tool Transcript。"""


TokenCounter = Callable[[dict[str, Any]], int]


@dataclass(frozen=True, slots=True)
class CompactionRetryPolicy:
    enabled: bool = True
    max_retries: int = 1
    keep_recent_messages: int = 20
    # 为 None 时保持旧版 SlidingWindow 行为；配置后启用结构化 token-aware 压缩。
    target_context_tokens: int | None = None
    reserved_tokens: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int) or self.max_retries < 1:
            raise ValueError("compaction max_retries 必须是大于 0 的整数")
        if isinstance(self.keep_recent_messages, bool) or not isinstance(self.keep_recent_messages, int) or self.keep_recent_messages < 1:
            raise ValueError("keep_recent_messages 必须是大于 0 的整数")
        if (
            self.target_context_tokens is not None
            and (
                isinstance(self.target_context_tokens, bool)
                or not isinstance(self.target_context_tokens, int)
                or self.target_context_tokens < 1
            )
        ):
            raise ValueError("target_context_tokens 必须是大于 0 的整数或 None")
        if (
            isinstance(self.reserved_tokens, bool)
            or not isinstance(self.reserved_tokens, int)
            or self.reserved_tokens < 0
        ):
            raise ValueError("reserved_tokens 必须是非负整数")
        if (
            self.target_context_tokens is not None
            and self.reserved_tokens >= self.target_context_tokens
        ):
            raise ValueError("reserved_tokens 必须小于 target_context_tokens")


class SlidingWindowCompactor:
    """教学用滑动窗口；生产系统应替换为 token-aware 摘要压缩。"""

    def __init__(self, keep_recent_messages: int) -> None:
        self.keep_recent_messages = keep_recent_messages

    async def __call__(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(messages) <= self.keep_recent_messages:
            return list(messages)
        return copy.deepcopy(messages[-self.keep_recent_messages :])


@dataclass(frozen=True, slots=True)
class ContextReplacement:
    """可落盘且可验证的 Context Replacement。"""

    source_digest: str
    replacement_digest: str
    source_message_count: int
    messages: tuple[dict[str, Any], ...]
    estimated_tokens: int
    token_budget: int | None
    budget_exceeded: bool
    summary: dict[str, Any] | None = None
    schema_version: int = 1

    @classmethod
    def create(
        cls,
        source_messages: list[dict[str, Any]],
        replacement_messages: list[dict[str, Any]],
        *,
        summary: dict[str, Any] | None = None,
        token_counter: TokenCounter | None = None,
        token_budget: int | None = None,
    ) -> "ContextReplacement":
        counter = token_counter or estimate_message_tokens
        copied = copy.deepcopy(replacement_messages)
        estimated = _estimate_messages(copied, counter)
        replacement = cls(
            source_digest=_json_digest(source_messages),
            replacement_digest=_json_digest(copied),
            source_message_count=len(source_messages),
            messages=tuple(copied),
            estimated_tokens=estimated,
            token_budget=token_budget,
            budget_exceeded=(token_budget is not None and estimated > token_budget),
            summary=copy.deepcopy(summary),
        )
        replacement.verify(source_messages, token_counter=counter)
        return replacement

    def verify(
        self,
        source_messages: list[dict[str, Any]],
        *,
        token_counter: TokenCounter | None = None,
    ) -> None:
        validate_context_replacement(
            source_messages,
            self,
            token_counter=token_counter,
        )

    def to_event_data(self) -> dict[str, Any]:
        """事件可直接持久化，不依赖进程内 Compactor 状态。"""

        return {
            "schemaVersion": self.schema_version,
            "sourceDigest": self.source_digest,
            "replacementDigest": self.replacement_digest,
            "sourceMessageCount": self.source_message_count,
            "estimatedTokens": self.estimated_tokens,
            "tokenBudget": self.token_budget,
            "budgetExceeded": self.budget_exceeded,
            "summary": copy.deepcopy(self.summary),
            "messages": copy.deepcopy(list(self.messages)),
        }


class TokenAwareStructuredCompactor:
    """按 token 预算压缩，并保留成对 Tool、Approval、约束和业务事实。"""

    def __init__(
        self,
        target_tokens: int,
        *,
        keep_recent_messages: int = 8,
        reserved_tokens: int = 0,
        token_counter: TokenCounter | None = None,
        max_summary_text_chars: int = 600,
    ) -> None:
        if isinstance(target_tokens, bool) or not isinstance(target_tokens, int) or target_tokens < 1:
            raise ValueError("target_tokens 必须是大于 0 的整数")
        if isinstance(keep_recent_messages, bool) or not isinstance(keep_recent_messages, int) or keep_recent_messages < 1:
            raise ValueError("keep_recent_messages 必须是大于 0 的整数")
        if isinstance(reserved_tokens, bool) or not isinstance(reserved_tokens, int) or reserved_tokens < 0:
            raise ValueError("reserved_tokens 必须是非负整数")
        if reserved_tokens >= target_tokens:
            raise ValueError("reserved_tokens 必须小于 target_tokens")
        if max_summary_text_chars < 40:
            raise ValueError("max_summary_text_chars 不能小于 40")
        self.target_tokens = target_tokens
        self.keep_recent_messages = keep_recent_messages
        self.reserved_tokens = reserved_tokens
        self.token_counter = token_counter or estimate_message_tokens
        self.max_summary_text_chars = max_summary_text_chars

    @property
    def available_tokens(self) -> int:
        return self.target_tokens - self.reserved_tokens

    async def __call__(
        self,
        messages: list[dict[str, Any]],
    ) -> ContextReplacement:
        source = copy.deepcopy(messages)
        try:
            validate_closed_tool_call_transcript(source)
        except TranscriptIntegrityError as error:
            raise ContextCompactionValidationError(
                f"压缩前 Transcript 未闭合：{error}"
            ) from error

        units = _message_units(source)
        mandatory_indexes = set(range(max(0, len(source) - self.keep_recent_messages), len(source)))
        mandatory_indexes.update(
            index for index, message in enumerate(source) if _is_critical_message(message)
        )
        mandatory_units = {
            unit_index
            for unit_index, unit in enumerate(units)
            if any(index in mandatory_indexes for index in unit)
        }
        selected_units = set(mandatory_units)
        replacement = self._build_replacement(source, units, selected_units)
        # 从最新单元向前逐组尝试；每次均以“原始消息 + 更新后的摘要”真实估算，
        # 不会因只计算 Raw Context 而低估摘要自身开销。
        for unit_index in reversed(range(len(units))):
            if unit_index in selected_units:
                continue
            candidate_units = {*selected_units, unit_index}
            candidate = self._build_replacement(source, units, candidate_units)
            if not candidate.budget_exceeded:
                selected_units = candidate_units
                replacement = candidate
        return replacement

    def _build_replacement(
        self,
        source: list[dict[str, Any]],
        units: list[tuple[int, ...]],
        selected_units: set[int],
    ) -> ContextReplacement:
        retained_indexes = {
            index
            for unit_index in selected_units
            for index in units[unit_index]
        }
        dropped_indexes = [
            index for index in range(len(source)) if index not in retained_indexes
        ]
        retained = [
            copy.deepcopy(source[index])
            for index in range(len(source))
            if index in retained_indexes
        ]
        settings = (
            (self.max_summary_text_chars, 32),
            (min(240, self.max_summary_text_chars), 16),
            (min(100, self.max_summary_text_chars), 8),
            (min(40, self.max_summary_text_chars), 4),
            (0, 0),
        )
        last: ContextReplacement | None = None
        for max_text_chars, max_message_records in settings:
            summary = (
                _structured_summary(
                    source,
                    dropped_indexes,
                    source_digest=_json_digest(source),
                    max_text_chars=max_text_chars,
                    max_message_records=max_message_records,
                )
                if dropped_indexes
                else None
            )
            compacted = copy.deepcopy(retained)
            if summary is not None:
                compacted.insert(0, _summary_message(summary))
            last = ContextReplacement.create(
                source,
                compacted,
                summary=summary,
                token_counter=self.token_counter,
                token_budget=self.available_tokens,
            )
            if not last.budget_exceeded:
                return last
        return cast(ContextReplacement, last)


class ContextOverflowCompactingStreamFn:
    def __init__(
        self,
        stream_fn: StreamFn,
        policy: CompactionRetryPolicy,
        *,
        compactor: Callable[
            [list[dict[str, Any]]],
            Awaitable[list[dict[str, Any]] | ContextReplacement],
        ] | None = None,
    ) -> None:
        self.stream_fn = stream_fn
        self.policy = policy
        if compactor is not None:
            self.compactor = compactor
        elif policy.target_context_tokens is not None:
            self.compactor = TokenAwareStructuredCompactor(
                policy.target_context_tokens,
                keep_recent_messages=policy.keep_recent_messages,
                reserved_tokens=policy.reserved_tokens,
            )
        else:
            self.compactor = SlidingWindowCompactor(policy.keep_recent_messages)

    def __call__(self, model: Model, context: dict[str, Any], options: dict[str, Any]) -> Any:
        if not self.policy.enabled:
            return self.stream_fn(model, context, options)
        output = ProducerOwnedAssistantMessageEventStream()
        task = asyncio.create_task(
            self._run(output, model, context, options),
            name=f"pi-context-compaction:{model.provider}:{model.id}",
        )
        bind_stream_producer(output, task)
        return output

    async def _run(
        self,
        output: AssistantMessageEventStream,
        model: Model,
        context: dict[str, Any],
        options: dict[str, Any],
    ) -> None:
        working = copy.deepcopy(context)
        retries = 0
        try:
            while True:
                events, final = await _consume(
                    self.stream_fn,
                    model,
                    working,
                    options,
                )
                if not _is_context_overflow(final) or retries >= self.policy.max_retries:
                    for event in events:
                        output.push(event)
                    return
                retries += 1
                old_messages = list(working.get("messages", []))
                compacted_value = await self.compactor(old_messages)
                replacement = (
                    compacted_value
                    if isinstance(compacted_value, ContextReplacement)
                    else ContextReplacement.create(
                        old_messages,
                        compacted_value,
                    )
                )
                # Custom compactors are an untrusted extension boundary.  Even a
                # pre-built ContextReplacement must be rebound to this exact source
                # before its messages are sent to the provider or persisted.
                counter = (
                    self.compactor.token_counter
                    if isinstance(self.compactor, TokenAwareStructuredCompactor)
                    else estimate_message_tokens
                )
                replacement.verify(old_messages, token_counter=counter)
                compacted = copy.deepcopy(list(replacement.messages))
                old_estimated_tokens = _estimate_messages(old_messages, counter)
                # A replacement that still exceeds its declared safe budget must
                # never be sent merely because it contains fewer message objects.
                # Likewise, retrying with an equal/larger token estimate cannot
                # resolve a context overflow and only burns another model call.
                if replacement.budget_exceeded or (
                    replacement.estimated_tokens >= old_estimated_tokens
                ):
                    for event in events:
                        output.push(event)
                    return
                await self._emit_event(output, options, {
                    "type": "context_compaction_started",
                    "kind": "model",
                    "attempt": retries,
                    "beforeMessages": len(old_messages),
                })
                working["messages"] = compacted
                await self._emit_event(output, options, {
                    "type": "context_compaction_finished",
                    "kind": "model",
                    "attempt": retries,
                    "beforeMessages": len(old_messages),
                    "afterMessages": len(compacted),
                    "replacement": replacement.to_event_data(),
                })
        except BaseException as error:
            output.fail(error)

    async def _emit_event(
        self,
        output: AssistantMessageEventStream,
        options: dict[str, Any],
        event: dict[str, Any],
    ) -> None:
        sink = options.get("retry_event_sink")
        if callable(sink):
            value = sink(dict(event))
            if inspect.isawaitable(value):
                await cast(Awaitable[Any], value)
        output.push(event)


async def _consume(
    stream_fn: StreamFn,
    model: Model,
    context: dict[str, Any],
    options: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    value = stream_fn(model, context, options)
    stream = await cast(Awaitable[Any], value) if inspect.isawaitable(value) else value
    completed = False
    try:
        events = [event async for event in stream]
        final = await stream.result()
        completed = True
        return events, final
    finally:
        await settle_stream_producer(stream, cancel=not completed)


def _is_context_overflow(message: dict[str, Any]) -> bool:
    details = message.get("providerError")
    if isinstance(details, dict) and details.get("code") in {
        "context_overflow",
        "context_length_exceeded",
    }:
        return True
    text = str(message.get("errorMessage", "")).casefold()
    return "context overflow" in text or "context length" in text


def compact_on_context_overflow(
    stream_fn: StreamFn,
    policy: CompactionRetryPolicy,
    *,
    compactor: Callable[
        [list[dict[str, Any]]],
        Awaitable[list[dict[str, Any]] | ContextReplacement],
    ] | None = None,
) -> StreamFn:
    return cast(StreamFn, ContextOverflowCompactingStreamFn(stream_fn, policy, compactor=compactor))


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """无第三方 tokenizer 时的确定性保守估算，可由 Provider tokenizer 替换。"""

    # 只估算 Provider 实际可见字段；持久化校验元数据不会进入模型请求。
    role = message.get("role")
    visible: dict[str, Any] = {"role": role}
    if role == "assistant":
        visible["content"] = [
            block
            for block in message.get("content", [])
            if isinstance(block, dict)
            and block.get("type") in {"text", "toolCall"}
        ]
    elif role == "toolResult":
        visible["toolCallId"] = message.get("toolCallId")
        visible["content"] = message.get("content", [])
    else:
        visible["content"] = message.get("content", [])
    encoded = _canonical_json(visible).encode("utf-8")
    return max(1, math.ceil(len(encoded) / 3))


def _estimate_messages(
    messages: list[dict[str, Any]],
    counter: TokenCounter,
) -> int:
    total = 0
    for message in messages:
        value = counter(message)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ContextCompactionValidationError(
                "Token Counter 必须为每条消息返回非负整数"
            )
        total += value
    return total


def validate_context_replacement(
    source_messages: list[dict[str, Any]],
    replacement: ContextReplacement,
    *,
    token_counter: TokenCounter | None = None,
) -> None:
    """验证摘要来源、Replacement 内容、关键事实和 Tool 配对。"""

    if replacement.schema_version != 1:
        raise ContextCompactionValidationError("不支持的 Context Replacement 版本")
    if replacement.source_message_count != len(source_messages):
        raise ContextCompactionValidationError("Context Replacement 消息数量不匹配")
    if replacement.source_digest != _json_digest(source_messages):
        raise ContextCompactionValidationError("Context Replacement 源摘要不匹配")
    messages = copy.deepcopy(list(replacement.messages))
    if replacement.replacement_digest != _json_digest(messages):
        raise ContextCompactionValidationError("Context Replacement 内容摘要不匹配")
    if (
        isinstance(replacement.estimated_tokens, bool)
        or not isinstance(replacement.estimated_tokens, int)
        or replacement.estimated_tokens < 0
    ):
        raise ContextCompactionValidationError("Context Replacement Token 估算无效")
    if replacement.token_budget is not None and (
        isinstance(replacement.token_budget, bool)
        or not isinstance(replacement.token_budget, int)
        or replacement.token_budget < 1
    ):
        raise ContextCompactionValidationError("Context Replacement Token 预算无效")
    expected_exceeded = (
        replacement.token_budget is not None
        and replacement.estimated_tokens > replacement.token_budget
    )
    if replacement.budget_exceeded != expected_exceeded:
        raise ContextCompactionValidationError("Context Replacement Token 预算标记不一致")
    try:
        validate_closed_tool_call_transcript(messages)
    except TranscriptIntegrityError as error:
        raise ContextCompactionValidationError(
            f"Context Replacement 破坏 Tool 配对：{error}"
        ) from error

    source_critical = {
        _json_digest(message)
        for message in source_messages
        if _is_critical_message(message)
    }
    replacement_digests = {_json_digest(message) for message in messages}
    if not source_critical.issubset(replacement_digests):
        raise ContextCompactionValidationError(
            "Context Replacement 丢失 Approval、约束或业务事实"
        )

    source_calls = _tool_calls(source_messages)
    replacement_calls = _tool_calls(messages)
    summarized_calls = {
        str(item.get("toolCallId", ""))
        for item in (replacement.summary or {}).get("toolInteractions", [])
        if isinstance(item, dict)
    }
    missing = set(source_calls) - set(replacement_calls) - summarized_calls
    if missing:
        raise ContextCompactionValidationError(
            f"Context Replacement 丢失 Tool 交互：{sorted(missing)}"
        )
    if replacement.summary is None:
        if not _is_message_subsequence(messages, source_messages):
            raise ContextCompactionValidationError(
                "Context Replacement 包含无法追溯到原 Context 的消息"
            )
    else:
        summary = copy.deepcopy(replacement.summary)
        if set(summary) != {
            "schemaVersion",
            "kind",
            "sourceDigest",
            "droppedMessageCount",
            "droppedIndexes",
            "toolInteractions",
            "approvalLedger",
            "constraintLedger",
            "factLedger",
            "messages",
            "omittedMessageRecords",
            "omittedMessagesDigest",
            "summaryDigest",
        }:
            raise ContextCompactionValidationError("结构化摘要字段不完整或包含未知字段")
        supplied = summary.pop("summaryDigest")
        if supplied != _json_digest(summary):
            raise ContextCompactionValidationError("结构化摘要校验失败")
        if summary.get("sourceDigest") != replacement.source_digest:
            raise ContextCompactionValidationError("结构化摘要来源不匹配")
        if summary.get("schemaVersion") != 1 or summary.get("kind") != "structured_context_summary":
            raise ContextCompactionValidationError("结构化摘要类型或版本无效")
        dropped_indexes = summary.get("droppedIndexes")
        if (
            not isinstance(dropped_indexes, list)
            or any(
                isinstance(index, bool)
                or not isinstance(index, int)
                or index < 0
                or index >= len(source_messages)
                for index in dropped_indexes
            )
            or dropped_indexes != sorted(set(dropped_indexes))
            or summary.get("droppedMessageCount") != len(dropped_indexes)
        ):
            raise ContextCompactionValidationError("结构化摘要的丢弃消息索引无效")
        dropped = set(dropped_indexes)
        retained = [
            copy.deepcopy(message)
            for index, message in enumerate(source_messages)
            if index not in dropped
        ]
        original_summary = copy.deepcopy(replacement.summary)
        if not messages or messages[0] != _summary_message(original_summary):
            raise ContextCompactionValidationError("结构化摘要消息与持久摘要不一致")
        if messages[1:] != retained:
            raise ContextCompactionValidationError("结构化 Context 保留消息与原文不一致")
        if summary.get("toolInteractions") != _summarized_tool_interactions(
            source_messages, dropped
        ):
            raise ContextCompactionValidationError("结构化摘要的 Tool 事实与原文不一致")
        expected_ledgers = _structured_ledgers(source_messages, dropped)
        if summary.get("approvalLedger") != expected_ledgers["approvalLedger"]:
            raise ContextCompactionValidationError("结构化摘要的 Approval 账本与原文不一致")
        if summary.get("constraintLedger") != expected_ledgers["constraintLedger"]:
            raise ContextCompactionValidationError("结构化摘要的约束账本与原文不一致")
        if summary.get("factLedger") != expected_ledgers["factLedger"]:
            raise ContextCompactionValidationError("结构化摘要的业务事实账本与原文不一致")
        _validate_summary_message_records(source_messages, dropped, summary)

    counter = token_counter or estimate_message_tokens
    expected_tokens = _estimate_messages(messages, counter)
    if replacement.estimated_tokens != expected_tokens:
        raise ContextCompactionValidationError(
            "Context Replacement Token 估算与消息重算结果不一致"
        )
    expected_exceeded = (
        replacement.token_budget is not None
        and expected_tokens > replacement.token_budget
    )
    if replacement.budget_exceeded != expected_exceeded:
        raise ContextCompactionValidationError("Context Replacement Token 预算标记不一致")


def _message_units(messages: list[dict[str, Any]]) -> list[tuple[int, ...]]:
    """将 Assistant Tool Call 与所有对应 Result 合并为不可拆分单元。"""

    parents = list(range(len(messages)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    call_owners: dict[str, int] = {}
    for index, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        for block in message.get("content", []):
            if isinstance(block, dict) and block.get("type") == "toolCall":
                call_id = str(block.get("id", ""))
                if call_id:
                    call_owners[call_id] = index
    for index, message in enumerate(messages):
        if message.get("role") != "toolResult":
            continue
        owner = call_owners.get(str(message.get("toolCallId", "")))
        if owner is not None:
            union(owner, index)

    grouped: dict[int, list[int]] = {}
    for index in range(len(messages)):
        grouped.setdefault(find(index), []).append(index)
    return [
        tuple(indexes)
        for indexes in sorted(grouped.values(), key=lambda value: value[0])
    ]


def _structured_summary(
    messages: list[dict[str, Any]],
    dropped_indexes: list[int],
    *,
    source_digest: str,
    max_text_chars: int,
    max_message_records: int,
) -> dict[str, Any]:
    dropped = set(dropped_indexes)
    records: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if index not in dropped:
            continue
        if message.get("role") == "toolResult":
            continue
        text = _message_text(message)
        if text:
            record: dict[str, Any] = {
                "index": index,
                "role": str(message.get("role", "")),
                "messageDigest": _json_digest(message),
            }
            if max_text_chars:
                record.update(
                    {
                        "text": text[:max_text_chars],
                        "truncated": len(text) > max_text_chars,
                    }
                )
            records.append(record)
    omitted_records = records[:-max_message_records] if max_message_records else records
    retained_records = records[-max_message_records:] if max_message_records else []
    summary: dict[str, Any] = {
        "schemaVersion": 1,
        "kind": "structured_context_summary",
        "sourceDigest": source_digest,
        "droppedMessageCount": len(dropped_indexes),
        "droppedIndexes": dropped_indexes,
        "toolInteractions": _summarized_tool_interactions(messages, dropped),
        **_structured_ledgers(messages, dropped),
        "messages": retained_records,
        "omittedMessageRecords": len(omitted_records),
        "omittedMessagesDigest": (
            _json_digest(
                [record["messageDigest"] for record in omitted_records]
            )
            if omitted_records
            else None
        ),
    }
    summary["summaryDigest"] = _json_digest(summary)
    return summary


def _summarized_tool_interactions(
    messages: list[dict[str, Any]],
    dropped: set[int],
) -> list[dict[str, Any]]:
    results = {
        str(message.get("toolCallId", "")): message
        for index, message in enumerate(messages)
        if index in dropped and message.get("role") == "toolResult"
    }
    interactions: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if index not in dropped:
            continue
        calls = [
            block
            for block in message.get("content", [])
            if isinstance(block, dict) and block.get("type") == "toolCall"
        ]
        for call in calls:
            call_id = str(call.get("id", ""))
            result = results.get(call_id, {})
            interactions.append(
                {
                    "toolCallId": call_id,
                    "toolName": str(call.get("name", result.get("toolName", ""))),
                    "arguments": _compact_json_value(call.get("arguments", {})),
                    "result": _compact_json_value(
                        {
                            "content": result.get("content", []),
                            "details": result.get("details"),
                            "isError": bool(result.get("isError", False)),
                        }
                    ),
                }
            )
    return interactions


_APPROVAL_FIELD_MARKERS = ("approval", "approve", "审批", "批准")
_CONSTRAINT_FIELD_MARKERS = (
    "constraint",
    "policy",
    "permission",
    "scope",
    "约束",
    "策略",
    "权限",
)
_FACT_FIELD_NAMES = frozenset(
    {
        "businessfact",
        "businessfacts",
        "contextfact",
        "contextfacts",
        "criticalfact",
        "criticalfacts",
        "domainfact",
        "domainfacts",
        "entitysnapshot",
        "entitystate",
        "fact",
        "facts",
        "entityversion",
        "entityversions",
        "statefact",
        "statefacts",
        "stateversion",
    }
)


def _structured_ledgers(
    messages: list[dict[str, Any]],
    dropped: set[int],
) -> dict[str, list[dict[str, Any]]]:
    """Derive exact, replay-verifiable safety ledgers from the source context.

    Explicit, domain-neutral metadata is preferred.  Short dropped user
    statements remain available verbatim, while long authoritative facts must
    use a ``*Facts``/``entityState`` field, a typed fact content block, or set
    ``preserveInCompaction=true``.  The framework deliberately does not infer
    authoritative facts from domain-specific vocabulary.
    """

    approvals: list[dict[str, Any]] = []
    constraints: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        normalized_fields = {
            _normalized_field_name(key): (str(key), copy.deepcopy(value))
            for key, value in message.items()
            if key not in {"role", "content"}
        }
        approval_fields = {
            original: value
            for normalized, (original, value) in normalized_fields.items()
            if any(marker in normalized for marker in _APPROVAL_FIELD_MARKERS)
        }
        constraint_fields = {
            original: value
            for normalized, (original, value) in normalized_fields.items()
            if any(marker in normalized for marker in _CONSTRAINT_FIELD_MARKERS)
        }
        fact_fields = {
            original: value
            for normalized, (original, value) in normalized_fields.items()
            if normalized in _FACT_FIELD_NAMES
        }
        text = _message_text(message)
        folded = text.casefold()
        retained = index not in dropped
        # Ledgers describe information removed from the verbatim transcript.
        # Retained messages are already authoritative and duplicating them here
        # wastes the very budget compaction is meant to recover.
        if retained:
            continue
        # The summary already binds the complete source with sourceDigest and is
        # revalidated against the original messages.  sourceIndex is therefore
        # enough provenance here and avoids repeating a 64-byte digest per fact.
        base = {"sourceIndex": index}
        if approval_fields or any(
            marker in folded for marker in _APPROVAL_FIELD_MARKERS
        ):
            entry = {**base, "fields": approval_fields}
            if text:
                entry["text"] = text
            approvals.append(entry)
        if constraint_fields or any(
            marker in folded for marker in _CONSTRAINT_FIELD_MARKERS
        ):
            entry = {**base, "fields": constraint_fields}
            if text:
                entry["text"] = text
            constraints.append(entry)

        content_facts = _content_fact_values(message)
        preserve_user_text = (
            message.get("role") == "user"
            and bool(text)
            and (
                len(text) <= 256
                or message.get("preserveInCompaction") is True
            )
        )
        if fact_fields or content_facts or preserve_user_text:
            entry = {
                **base,
                "fields": fact_fields,
                "contentFacts": content_facts,
            }
            if preserve_user_text:
                entry["text"] = text
            facts.append(entry)
    return {
        "approvalLedger": approvals,
        "constraintLedger": constraints,
        "factLedger": facts,
    }


def _normalized_field_name(value: Any) -> str:
    return "".join(character for character in str(value).casefold() if character.isalnum())


def _content_fact_values(message: dict[str, Any]) -> list[Any]:
    content = message.get("content", [])
    if not isinstance(content, list):
        return []
    return [
        copy.deepcopy(block)
        for block in content
        if isinstance(block, dict)
        and str(block.get("type", "")).casefold()
        in {
            "fact",
            "businessfact",
            "business_fact",
            "criticalfact",
            "critical_fact",
            "domainfact",
            "domain_fact",
            "statefact",
            "state_fact",
        }
    ]


def _validate_summary_message_records(
    source_messages: list[dict[str, Any]],
    dropped: set[int],
    summary: dict[str, Any],
) -> None:
    all_records = [
        {
            "index": index,
            "role": str(message.get("role", "")),
            "messageDigest": _json_digest(message),
            "text": _message_text(message),
        }
        for index, message in enumerate(source_messages)
        if index in dropped
        and message.get("role") != "toolResult"
        and _message_text(message)
    ]
    supplied = summary.get("messages")
    omitted_count = summary.get("omittedMessageRecords")
    if (
        not isinstance(supplied, list)
        or isinstance(omitted_count, bool)
        or not isinstance(omitted_count, int)
        or omitted_count < 0
        or omitted_count + len(supplied) != len(all_records)
    ):
        raise ContextCompactionValidationError("结构化摘要的消息记录数量无效")
    expected_suffix = all_records[omitted_count:]
    for record, expected in zip(supplied, expected_suffix, strict=True):
        if not isinstance(record, dict) or set(record) not in (
            {"index", "role", "messageDigest"},
            {"index", "role", "messageDigest", "text", "truncated"},
        ):
            raise ContextCompactionValidationError("结构化摘要的消息记录无效")
        if any(record.get(key) != expected[key] for key in ("index", "role", "messageDigest")):
            raise ContextCompactionValidationError("结构化摘要的消息记录与原文不一致")
        if "text" in record:
            text = record.get("text")
            expected_text = expected.get("text")
            if (
                not isinstance(text, str)
                or not isinstance(expected_text, str)
                or not expected_text.startswith(text)
            ):
                raise ContextCompactionValidationError("结构化摘要文本不是原文前缀")
            if record.get("truncated") is not (
                len(expected_text) > len(text)
            ):
                raise ContextCompactionValidationError("结构化摘要截断标记无效")
    omitted = all_records[:omitted_count]
    expected_digest = (
        _json_digest([record["messageDigest"] for record in omitted])
        if omitted
        else None
    )
    if summary.get("omittedMessagesDigest") != expected_digest:
        raise ContextCompactionValidationError("结构化摘要的省略消息摘要不匹配")


def _is_message_subsequence(
    candidate: list[dict[str, Any]],
    source: list[dict[str, Any]],
) -> bool:
    source_index = 0
    for message in candidate:
        while source_index < len(source) and source[source_index] != message:
            source_index += 1
        if source_index >= len(source):
            return False
        source_index += 1
    return True


def _summary_message(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": (
                    "[STRUCTURED_CONTEXT_REPLACEMENT_V1]\n"
                    + _canonical_json(summary)
                ),
            }
        ],
        "contextReplacement": {
            "schemaVersion": summary["schemaVersion"],
            "sourceDigest": summary["sourceDigest"],
            "summaryDigest": summary["summaryDigest"],
        },
    }


def _tool_calls(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    calls: dict[str, dict[str, Any]] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for block in message.get("content", []):
            if isinstance(block, dict) and block.get("type") == "toolCall":
                call_id = str(block.get("id", ""))
                if call_id:
                    calls[call_id] = block
    return calls


def _is_critical_message(message: dict[str, Any]) -> bool:
    if message.get("role") in {"system", "developer"}:
        return True
    searchable = _canonical_json(message).casefold()
    return any(
        marker in searchable
        for marker in (
            "approval",
            "constraint",
            "policyconstraint",
            "审批",
            "约束",
        )
    )


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content", [])
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict)
        and block.get("type") in {"text", "input_text", "output_text"}
        and block.get("text")
    )


def _compact_json_value(value: Any, *, max_chars: int = 2000) -> Any:
    text = _canonical_json(value)
    if len(text) <= max_chars:
        return copy.deepcopy(value)
    return {
        "digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "preview": text[:max_chars],
        "truncated": True,
    }


def _json_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ContextCompactionValidationError(
            f"Context 必须是严格 JSON：{error}"
        ) from error
