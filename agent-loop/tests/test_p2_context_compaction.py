"""Token-aware 结构化 Context Replacement 测试。"""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from dataclasses import replace
from pathlib import Path

from pi_agent_loop import Agent, Model, ScriptedProvider, assistant_message, user_message
from pi_agent_loop.retry.compaction import (
    CompactionRetryPolicy,
    ContextCompactionValidationError,
    ContextReplacement,
    TokenAwareStructuredCompactor,
    compact_on_context_overflow,
    validate_context_replacement,
)


class P2ContextCompactionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.model = Model(id="compact-model", provider="fake", api="fake")

    def messages_with_tool_and_approval(self) -> list[dict]:
        return [
            user_message("很早以前的上下文" * 80),
            assistant_message(
                model=self.model,
                content=[
                    {
                        "type": "toolCall",
                        "id": "lookup-1",
                        "name": "get_order",
                        "arguments": {"order_id": "1001"},
                    }
                ],
            ),
            {
                "role": "toolResult",
                "toolCallId": "lookup-1",
                "toolName": "get_order",
                "content": [{"type": "text", "text": "订单已付款"}],
                "details": {"status": "paid"},
                "isError": False,
            },
            user_message("普通历史" * 100),
            assistant_message(
                model=self.model,
                content=[{"type": "text", "text": "普通回答" * 100}],
            ),
            {
                "role": "user",
                "content": [{"type": "text", "text": "审批约束：取消必须由经理批准"}],
                "approvalId": "approval-1",
                "businessFacts": {"order_id": "1001", "version": 7},
            },
            user_message("现在继续处理"),
        ]

    async def test_token_aware压缩保留关键事实并成对摘要tool(self) -> None:
        source = self.messages_with_tool_and_approval()
        replacement = await TokenAwareStructuredCompactor(
            360,
            keep_recent_messages=1,
        )(source)

        self.assertIsInstance(replacement, ContextReplacement)
        replacement.verify(source)
        self.assertFalse(replacement.budget_exceeded)
        self.assertLessEqual(replacement.estimated_tokens, 360)
        self.assertTrue(
            any(message.get("approvalId") == "approval-1" for message in replacement.messages)
        )
        summary = replacement.summary or {}
        interactions = summary.get("toolInteractions", [])
        raw_calls = [
            block
            for message in replacement.messages
            for block in message.get("content", [])
            if isinstance(block, dict) and block.get("type") == "toolCall"
        ]
        if raw_calls:
            self.assertTrue(
                any(
                    message.get("role") == "toolResult"
                    and message.get("toolCallId") == "lookup-1"
                    for message in replacement.messages
                )
            )
        else:
            self.assertEqual(interactions[0]["toolCallId"], "lookup-1")
            self.assertEqual(interactions[0]["result"]["details"]["status"], "paid")

    async def test_replacement摘要或消息被篡改时拒绝(self) -> None:
        source = self.messages_with_tool_and_approval()
        replacement = await TokenAwareStructuredCompactor(
            240, keep_recent_messages=1
        )(source)
        tampered_messages = copy.deepcopy(list(replacement.messages))
        tampered_messages[-1]["content"][0]["text"] = "篡改"
        tampered = replace(replacement, messages=tuple(tampered_messages))
        with self.assertRaises(ContextCompactionValidationError):
            validate_context_replacement(source, tampered)

    async def test_重算普通摘要后仍不能伪造tool事实(self) -> None:
        source = self.messages_with_tool_and_approval()
        replacement = await TokenAwareStructuredCompactor(
            240, keep_recent_messages=1
        )(source)
        summary = copy.deepcopy(replacement.summary)
        self.assertIsNotNone(summary)
        summary["toolInteractions"][0]["result"] = {
            "details": {"status": "refunded"}
        }
        summary_without_digest = {
            key: value for key, value in summary.items() if key != "summaryDigest"
        }
        summary["summaryDigest"] = self._digest(summary_without_digest)
        messages = copy.deepcopy(list(replacement.messages))
        messages[0] = {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "[STRUCTURED_CONTEXT_REPLACEMENT_V1]\n"
                    + self._canonical(summary),
                }
            ],
            "contextReplacement": {
                "schemaVersion": summary["schemaVersion"],
                "sourceDigest": summary["sourceDigest"],
                "summaryDigest": summary["summaryDigest"],
            },
        }
        forged = replace(
            replacement,
            messages=tuple(messages),
            replacement_digest=self._digest(messages),
            summary=summary,
        )
        with self.assertRaisesRegex(ContextCompactionValidationError, "Tool 事实"):
            forged.verify(source)

    async def test_无摘要replacement不能注入非原文消息(self) -> None:
        source = [user_message("原文一"), user_message("原文二")]
        injected_messages = [user_message("伪造指令")]
        injected = ContextReplacement(
            source_digest=self._digest(source),
            replacement_digest=self._digest(injected_messages),
            source_message_count=len(source),
            messages=tuple(injected_messages),
            estimated_tokens=5,
            token_budget=None,
            budget_exceeded=False,
        )
        with self.assertRaisesRegex(ContextCompactionValidationError, "无法追溯"):
            injected.verify(source)

    async def test_replacement的token声明必须按消息重新计算(self) -> None:
        source = [
            user_message("X" * 10_000),
            user_message("尾部消息"),
        ]
        replacement = ContextReplacement.create(
            source,
            [source[0]],
            token_budget=10,
        )
        forged = replace(
            replacement,
            estimated_tokens=1,
            budget_exceeded=False,
        )

        with self.assertRaisesRegex(
            ContextCompactionValidationError,
            "Token 估算与消息重算",
        ):
            forged.verify(source)

    async def test_显式业务事实进入可验证ledger而不是随长文本丢失(self) -> None:
        facts = {
            "order_id": "1001",
            "paid": True,
            "location": "上海仓",
        }
        source = [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "冗长解释" * 500}],
                "businessFacts": facts,
            },
            *[
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": f"普通回答-{index}-" + "x" * 100}],
                )
                for index in range(10)
            ],
            user_message("继续"),
        ]

        replacement = await TokenAwareStructuredCompactor(
            350,
            keep_recent_messages=1,
        )(source)
        replacement.verify(source)

        self.assertFalse(replacement.budget_exceeded)
        self.assertNotIn("冗长解释" * 500, self._canonical(list(replacement.messages)))
        self.assertEqual(
            replacement.summary["factLedger"],
            [
                {
                    "sourceIndex": 0,
                    "fields": {"businessFacts": facts},
                    "contentFacts": [],
                }
            ],
        )
        self.assertIn("approvalLedger", replacement.summary)
        self.assertIn("constraintLedger", replacement.summary)

    async def test_非订单领域事实通过通用domainFacts保留(self) -> None:
        facts = {
            "deployment_id": "deploy-7",
            "environment": "staging",
            "health": "degraded",
        }
        source = [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "运行记录" * 800}],
                "domainFacts": facts,
            },
            *[
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "普通消息" * 120}],
                )
                for _ in range(6)
            ],
            user_message("继续诊断"),
        ]

        replacement = await TokenAwareStructuredCompactor(
            320,
            keep_recent_messages=1,
        )(source)
        replacement.verify(source)

        self.assertEqual(
            replacement.summary["factLedger"],
            [
                {
                    "sourceIndex": 0,
                    "fields": {"domainFacts": facts},
                    "contentFacts": [],
                }
            ],
        )
        source_code = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "pi_agent_loop"
            / "retry"
            / "compaction.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("_BUSINESS_FACT_TEXT_MARKERS", source_code)

    async def test_压缩后仍超预算时不会发起第二次模型请求(self) -> None:
        overflow = assistant_message(
            model=self.model,
            stop_reason="error",
            error_message="context length exceeded",
        )
        overflow["providerError"] = {"code": "context_length_exceeded"}
        provider = ScriptedProvider(
            [
                overflow,
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "不应被调用"}],
                ),
            ]
        )
        wrapped = compact_on_context_overflow(
            provider.stream,
            CompactionRetryPolicy(
                max_retries=1,
                keep_recent_messages=1,
                target_context_tokens=10,
            ),
        )

        stream = wrapped(
            self.model,
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "订单1001已付款，地址不能丢失" * 100,
                            }
                        ],
                    }
                ],
                "tools": [],
            },
            {},
        )
        events = [event async for event in stream]
        result = await stream.result()

        self.assertEqual(provider.call_count, 1)
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(result["providerError"]["code"], "context_length_exceeded")

    async def test_overflow事件携带可持久replacement(self) -> None:
        overflow = assistant_message(
            model=self.model,
            stop_reason="error",
            error_message="context length exceeded",
        )
        overflow["providerError"] = {
            "code": "context_length_exceeded",
            "statusCode": 400,
            "retryable": False,
            "retryAfterMs": None,
        }
        provider = ScriptedProvider(
            [
                overflow,
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "压缩成功"}],
                ),
            ]
        )
        wrapped = compact_on_context_overflow(
            provider.stream,
            CompactionRetryPolicy(
                max_retries=1,
                keep_recent_messages=1,
                target_context_tokens=240,
            ),
        )
        agent = Agent(
            model=self.model,
            stream_fn=wrapped,
            messages=[user_message("旧消息" * 200) for _ in range(4)],
        )
        finished: list[dict] = []
        agent.subscribe(
            lambda event, _token: finished.append(event)
            if event.get("type") == "context_compaction_finished"
            else None
        )

        await agent.prompt("最新问题")

        self.assertEqual(provider.call_count, 2)
        self.assertEqual(len(finished), 1)
        persisted = finished[0]["replacement"]
        self.assertEqual(persisted["schemaVersion"], 1)
        self.assertEqual(persisted["messages"], provider.contexts[1]["messages"])
        self.assertIn("sourceDigest", persisted)
        self.assertIn("replacementDigest", persisted)

    @staticmethod
    def _canonical(value) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @classmethod
    def _digest(cls, value) -> str:
        return hashlib.sha256(cls._canonical(value).encode("utf-8")).hexdigest()


if __name__ == "__main__":
    unittest.main()
