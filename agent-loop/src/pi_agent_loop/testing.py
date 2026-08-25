"""测试用确定性 Provider。

真实大模型输出不稳定，Agent Loop 单元测试必须使用可控 Provider。本模块按
队列返回预设 assistant message，并把最终内容展开成与真实 Provider 相似的
start/delta/end 事件。
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Callable
from typing import Any

from .event_stream import AssistantMessageEventStream
from .messages import assistant_message
from .types import Model

ResponseFactory = Callable[[dict[str, Any], dict[str, Any]], dict]


class ScriptedProvider:
    """按顺序消费预设响应的假 Provider。"""

    def __init__(
        self,
        responses: list[dict | ResponseFactory] | None = None,
        *,
        chunk_size: int = 8,
        delay_seconds: float = 0.0,
    ) -> None:
        self._responses = list(responses or [])
        self.chunk_size = max(1, chunk_size)
        self.delay_seconds = max(0.0, delay_seconds)
        self.call_count = 0
        self.contexts: list[dict[str, Any]] = []

    def append(self, *responses: dict | ResponseFactory) -> None:
        self._responses.extend(responses)

    def stream(
        self,
        model: Model,
        context: dict[str, Any],
        options: dict[str, Any],
    ) -> AssistantMessageEventStream:
        """返回事件流；真正生产事件的协程在后台运行。"""

        stream = AssistantMessageEventStream()
        self.call_count += 1
        self.contexts.append(copy.deepcopy(context))

        async def produce() -> None:
            if not self._responses:
                final = assistant_message(
                    model=model,
                    stop_reason="error",
                    error_message="ScriptedProvider 没有更多预设响应",
                )
                stream.push({"type": "error", "reason": "error", "error": final})
                return

            item = self._responses.pop(0)
            final = item(context, options) if callable(item) else copy.deepcopy(item)
            final["api"] = model.api
            final["provider"] = model.provider
            final["model"] = model.id
            await self._emit_message(stream, model, final, options)

        asyncio.create_task(produce())
        return stream

    async def _emit_message(
        self,
        stream: AssistantMessageEventStream,
        model: Model,
        final: dict,
        options: dict[str, Any],
    ) -> None:
        token = options.get("cancellation_token")
        partial = assistant_message(model=model, content=[], stop_reason="pending")
        stream.push({"type": "start", "partial": copy.deepcopy(partial)})

        for index, block in enumerate(final.get("content", [])):
            if token is not None and token.cancelled:
                aborted = copy.deepcopy(partial)
                aborted["stopReason"] = "aborted"
                aborted["errorMessage"] = token.reason
                stream.push(
                    {"type": "error", "reason": "aborted", "error": aborted}
                )
                return

            block_type = block.get("type")
            if block_type == "text":
                partial["content"].append({"type": "text", "text": ""})
                stream.push(
                    {
                        "type": "text_start",
                        "contentIndex": index,
                        "partial": copy.deepcopy(partial),
                    }
                )
                for chunk in self._chunks(str(block.get("text", ""))):
                    await self._delay()
                    partial["content"][index]["text"] += chunk
                    stream.push(
                        {
                            "type": "text_delta",
                            "contentIndex": index,
                            "delta": chunk,
                            "partial": copy.deepcopy(partial),
                        }
                    )
                stream.push(
                    {
                        "type": "text_end",
                        "contentIndex": index,
                        "content": block.get("text", ""),
                        "partial": copy.deepcopy(partial),
                    }
                )
            elif block_type == "thinking":
                partial["content"].append({"type": "thinking", "thinking": ""})
                stream.push(
                    {
                        "type": "thinking_start",
                        "contentIndex": index,
                        "partial": copy.deepcopy(partial),
                    }
                )
                for chunk in self._chunks(str(block.get("thinking", ""))):
                    await self._delay()
                    partial["content"][index]["thinking"] += chunk
                    stream.push(
                        {
                            "type": "thinking_delta",
                            "contentIndex": index,
                            "delta": chunk,
                            "partial": copy.deepcopy(partial),
                        }
                    )
                stream.push(
                    {
                        "type": "thinking_end",
                        "contentIndex": index,
                        "content": block.get("thinking", ""),
                        "partial": copy.deepcopy(partial),
                    }
                )
            elif block_type == "toolCall":
                call = {
                    "type": "toolCall",
                    "id": block["id"],
                    "name": block["name"],
                    "arguments": {},
                }
                partial["content"].append(call)
                stream.push(
                    {
                        "type": "toolcall_start",
                        "contentIndex": index,
                        "partial": copy.deepcopy(partial),
                    }
                )
                encoded = json.dumps(block.get("arguments", {}), ensure_ascii=False)
                for chunk in self._chunks(encoded):
                    await self._delay()
                    stream.push(
                        {
                            "type": "toolcall_delta",
                            "contentIndex": index,
                            "delta": chunk,
                            "partial": copy.deepcopy(partial),
                        }
                    )
                partial["content"][index] = copy.deepcopy(block)
                stream.push(
                    {
                        "type": "toolcall_end",
                        "contentIndex": index,
                        "toolCall": copy.deepcopy(block),
                        "partial": copy.deepcopy(partial),
                    }
                )

        stop_reason = final.get("stopReason", "stop")
        if stop_reason in {"error", "aborted"}:
            stream.push(
                {"type": "error", "reason": stop_reason, "error": final}
            )
        else:
            stream.push({"type": "done", "reason": stop_reason, "message": final})

    def _chunks(self, value: str) -> list[str]:
        if not value:
            return [""]
        return [
            value[index : index + self.chunk_size]
            for index in range(0, len(value), self.chunk_size)
        ]

    async def _delay(self) -> None:
        # sleep(0) 也主动让出事件循环，便于测试真实的异步交错。
        await asyncio.sleep(self.delay_seconds)
