"""持久 Retry、Circuit Breaker、Task、Outcome 和 Compaction 测试。"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import (
    Agent,
    AgentTool,
    AgentToolResult,
    CancellationToken,
    CircuitBreaker,
    CircuitBreakerPolicy,
    CircuitOpenError,
    CompactionRetryPolicy,
    JsonlRetryEventStore,
    Model,
    ModelRetryPolicy,
    OutcomeReconciliationRegistry,
    OutcomeUnknownToolError,
    RetryRecoveryManager,
    RetryableTaskError,
    RetryableToolError,
    ScriptedProvider,
    TaskRetryExecutor,
    TaskRetryPolicy,
    ToolRetryPolicy,
    assistant_message,
    compact_on_context_overflow,
    retry_model_stream,
    user_message,
)


class AdvancedRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.model = Model(id="advanced-retry", provider="fake", api="fake")

    def transient_model_error(self) -> dict:
        message = assistant_message(
            model=self.model,
            stop_reason="error",
            error_message="HTTP 429",
        )
        message["providerError"] = {
            "code": "provider_rate_limit_error",
            "statusCode": 429,
            "retryAfterMs": 0,
            "retryable": True,
        }
        return message

    async def test_retry_event_jsonl_持久化脱敏和进程恢复(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "retry.jsonl"
            store = JsonlRetryEventStore(path)
            await store.append(
                {
                    "type": "tool_retry_scheduled",
                    "kind": "tool",
                    "retryId": "chain-1",
                    "toolCallId": "call-1",
                    "attempt": 1,
                    "api_key": "must-not-leak",
                }
            )

            # 模拟进程重启：重新创建 Store 后仍能发现未完成 Chain。
            restarted = JsonlRetryEventStore(path)
            chains = restarted.incomplete_chains()
            self.assertEqual(len(chains), 1)
            self.assertEqual(chains[0].retry_id, "chain-1")
            self.assertNotIn("must-not-leak", path.read_text(encoding="utf-8"))

            seen: list[str] = []

            async def recover(chain) -> bool:
                seen.append(chain.retry_id)
                return True

            recovered = await RetryRecoveryManager(restarted).recover(recover)
            self.assertEqual(seen, ["chain-1"])
            self.assertEqual(len(recovered), 1)
            self.assertEqual(restarted.incomplete_chains(), [])

    async def test_agent_retry事件可以直接持久化(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JsonlRetryEventStore(Path(directory) / "agent-retry.jsonl")
            provider = ScriptedProvider(
                [
                    self.transient_model_error(),
                    assistant_message(
                        model=self.model,
                        content=[{"type": "text", "text": "ok"}],
                    ),
                ]
            )
            policy = ModelRetryPolicy(
                enabled=True,
                max_retries=1,
                initial_delay_seconds=0,
                max_delay_seconds=1,
                jitter_ratio=0,
            )
            agent = Agent(
                model=self.model,
                stream_fn=retry_model_stream(provider.stream, policy),
                retry_event_sink=store.append,
            )

            await agent.prompt("持久化 Retry")

            event_types = [item["type"] for item in store.load()]
            self.assertEqual(
                event_types,
                [
                    "model_retry_scheduled",
                    "model_retry_attempt_start",
                    "model_retry_finished",
                ],
            )

    async def test_circuit_breaker_open_和_half_open_恢复(self) -> None:
        breaker = CircuitBreaker(
            CircuitBreakerPolicy(
                enabled=True,
                failure_threshold=2,
                recovery_timeout_seconds=0.01,
            )
        )
        await breaker.before_call()
        await breaker.record_failure()
        await breaker.before_call()
        await breaker.record_failure()
        self.assertEqual(breaker.state, "open")
        with self.assertRaises(CircuitOpenError):
            await breaker.before_call()

        await asyncio.sleep(0.015)
        await breaker.before_call()
        self.assertEqual(breaker.state, "half_open")
        await breaker.record_success()
        self.assertEqual(breaker.state, "closed")

    async def test_circuit_breaker_阻止后续_provider_attempt(self) -> None:
        transient_error = self.transient_model_error()
        provider = ScriptedProvider([transient_error])
        policy = ModelRetryPolicy(
            enabled=True,
            max_retries=2,
            initial_delay_seconds=0,
            max_delay_seconds=1,
            jitter_ratio=0,
            circuit_breaker=CircuitBreakerPolicy(
                enabled=True,
                failure_threshold=1,
                recovery_timeout_seconds=10,
            ),
        )
        agent = Agent(
            model=self.model,
            stream_fn=retry_model_stream(provider.stream, policy),
        )

        await agent.prompt("触发 Circuit")

        self.assertEqual(provider.call_count, 1)
        final = agent.state.messages[-1]
        self.assertEqual(final["providerError"]["code"], "provider_circuit_open")

    async def test_模型重试受全局最大耗时限制(self) -> None:
        transient_error = self.transient_model_error()
        transient_error["providerError"]["retryAfterMs"] = None
        provider = ScriptedProvider([transient_error])
        policy = ModelRetryPolicy(
            enabled=True,
            max_retries=2,
            initial_delay_seconds=1,
            max_delay_seconds=1,
            jitter_ratio=0,
            max_elapsed_seconds=0.01,
        )
        agent = Agent(
            model=self.model,
            stream_fn=retry_model_stream(provider.stream, policy),
        )

        await agent.prompt("不应等待一秒")

        self.assertEqual(provider.call_count, 1)
        self.assertEqual(agent.state.messages[-1]["stopReason"], "error")

    async def test_tool_retry_受最大耗时限制(self) -> None:
        attempts = 0

        async def execute(_id, _args, _token, _update):
            nonlocal attempts
            attempts += 1
            raise RetryableToolError(
                "瞬时失败",
                code="upstream_unavailable",
            )

        tool = AgentTool(
            name="elapsed-tool",
            label="耗时限制",
            description="不应进入第二次 Attempt",
            execute=execute,
            retry_policy=ToolRetryPolicy(
                max_retries=2,
                retryable_codes=frozenset({"upstream_unavailable"}),
                idempotent=True,
                initial_delay_seconds=1,
                max_delay_seconds=1,
                jitter_ratio=0,
                max_elapsed_seconds=0.01,
            ),
        )
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=self.model,
                    stop_reason="toolUse",
                    content=[{
                        "type": "toolCall",
                        "id": "elapsed-id",
                        "name": "elapsed-tool",
                        "arguments": {},
                    }],
                ),
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "done"}],
                ),
            ]
        )
        agent = Agent(model=self.model, stream_fn=provider.stream, tools=[tool])

        await agent.prompt("测试 Tool 最大 Retry 耗时")

        self.assertEqual(attempts, 1)

    async def test_outcome_unknown_不重试并可调用状态核对器(self) -> None:
        attempts = 0
        error = OutcomeUnknownToolError(
            "取消请求可能已提交，请先核对状态",
            operation_id="operation-1",
            idempotency_key="secret-idempotency-key",
            reconciliation_name="check_cancel_status",
        )

        async def execute(_id, _args, _token, _update):
            nonlocal attempts
            attempts += 1
            raise error

        tool = AgentTool(
            name="uncertain-write",
            label="结果不确定写操作",
            description="不得自动重试",
            execute=execute,
        )
        provider = ScriptedProvider([
            assistant_message(
                model=self.model,
                stop_reason="toolUse",
                content=[{
                    "type": "toolCall",
                    "id": "write-id",
                    "name": "uncertain-write",
                    "arguments": {},
                }],
            ),
            assistant_message(
                model=self.model,
                content=[{"type": "text", "text": "等待核对"}],
            ),
        ])
        agent = Agent(model=self.model, stream_fn=provider.stream, tools=[tool])

        await agent.prompt("执行结果不确定的写操作")

        self.assertEqual(attempts, 1)
        result = next(
            item for item in agent.state.messages if item["role"] == "toolResult"
        )
        self.assertEqual(result["details"]["code"], "outcome_unknown")
        self.assertNotIn("secret-idempotency-key", repr(result))

        registry = OutcomeReconciliationRegistry()

        async def reconcile(unknown, _token):
            return AgentToolResult(
                content=[{"type": "text", "text": "服务端确认操作已成功"}],
                details={"operationId": unknown.operation_id, "status": "succeeded"},
            )

        registry.register("check_cancel_status", reconcile)
        reconciled = await registry.reconcile(error, CancellationToken())
        self.assertEqual(reconciled.details["status"], "succeeded")

    async def test_task_retry_executor_支持持久事件和最终成功(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JsonlRetryEventStore(Path(directory) / "task.jsonl")
            executor = TaskRetryExecutor(
                TaskRetryPolicy(
                    max_retries=2,
                    retryable_codes=frozenset({"worker_unavailable"}),
                    idempotent=True,
                    initial_delay_seconds=0,
                    max_delay_seconds=1,
                    jitter_ratio=0,
                ),
                event_store=store,
            )
            attempts = 0

            async def operation() -> str:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise RetryableTaskError(
                        "Worker 暂时不可用",
                        code="worker_unavailable",
                    )
                return "task-ok"

            result = await executor.execute(
                "task-1",
                operation,
                CancellationToken(),
            )

            self.assertEqual(result, "task-ok")
            self.assertEqual(attempts, 2)
            self.assertEqual(
                [item["type"] for item in store.load()],
                [
                    "task_retry_scheduled",
                    "task_retry_attempt_start",
                    "task_retry_finished",
                ],
            )

    async def test_context_overflow_压缩后重试同一逻辑turn(self) -> None:
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
        provider = ScriptedProvider([
            overflow,
            assistant_message(
                model=self.model,
                content=[{"type": "text", "text": "压缩后成功"}],
            ),
        ])
        wrapped = compact_on_context_overflow(
            provider.stream,
            CompactionRetryPolicy(max_retries=1, keep_recent_messages=2),
        )
        agent = Agent(
            model=self.model,
            stream_fn=wrapped,
            messages=[user_message(f"旧消息 {index}") for index in range(5)],
        )
        events: list[str] = []
        agent.subscribe(lambda event, _token: events.append(event["type"]))

        await agent.prompt("最新问题")

        self.assertEqual(provider.call_count, 2)
        self.assertGreater(len(provider.contexts[0]["messages"]), 2)
        self.assertEqual(len(provider.contexts[1]["messages"]), 2)
        self.assertEqual(agent.state.messages[-1]["content"][0]["text"], "压缩后成功")
        self.assertIn("context_compaction_started", events)
        self.assertIn("context_compaction_finished", events)


if __name__ == "__main__":
    unittest.main()
