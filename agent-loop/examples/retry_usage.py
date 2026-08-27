"""离线重试功能演示；不会访问真实模型或收费 API。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pi_agent_loop import (  # noqa: E402
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
    create_divide_tool,
    retry_model_stream,
    user_message,
)

MODEL = Model(id="retry-demo", provider="scripted", api="fake")
STATE_PATH = ROOT / "state" / "retry-demo.jsonl"


def transient_model_error() -> dict:
    message = assistant_message(
        model=MODEL,
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


async def model_retry_demo() -> None:
    print("\n[模型 Retry：429 → 成功]")
    provider = ScriptedProvider([
        transient_model_error(),
        assistant_message(
            model=MODEL,
            content=[{"type": "text", "text": "模型重试成功"}],
        ),
    ])
    policy = ModelRetryPolicy(
        enabled=True,
        max_retries=2,
        initial_delay_seconds=0,
        max_delay_seconds=1,
        jitter_ratio=0,
    )
    store = JsonlRetryEventStore(STATE_PATH)
    agent = Agent(
        model=MODEL,
        stream_fn=retry_model_stream(provider.stream, policy),
        retry_event_sink=store.append,
    )
    agent.subscribe(_print_retry_event)
    await agent.prompt("测试模型重试")
    print("逻辑调用：", 1)
    print("实际 Attempt：", provider.call_count)
    print("最终回答：", agent.state.messages[-1]["content"][0]["text"])


async def tool_retry_demo() -> None:
    print("\n[当前 divide 工具：第一次瞬时失败，只重试 divide]")
    divide = create_divide_tool()
    attempts = 0

    async def flaky_execute(call_id, arguments, cancellation, on_update):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RetryableToolError(
                "模拟上游暂时不可用",
                code="upstream_unavailable",
            )
        return await divide.execute(
            call_id,
            arguments,
            cancellation,
            on_update,
        )

    flaky_divide = AgentTool(
        name=divide.name,
        label=divide.label,
        description=divide.description,
        parameters=divide.parameters,
        validate_args=divide.validate_args,
        execute=flaky_execute,
        timeout_seconds=divide.timeout_seconds,
        retry_policy=ToolRetryPolicy(
            max_retries=2,
            retryable_codes=frozenset({"upstream_unavailable"}),
            idempotent=True,
            initial_delay_seconds=0,
            max_delay_seconds=1,
            jitter_ratio=0,
        ),
    )
    provider = ScriptedProvider([
        assistant_message(
            model=MODEL,
            stop_reason="toolUse",
            content=[{
                "type": "toolCall",
                "id": "divide-retry",
                "name": "divide",
                "arguments": {"a": 10, "b": 4},
            }],
        ),
        assistant_message(
            model=MODEL,
            content=[{"type": "text", "text": "工具重试完成"}],
        ),
    ])
    agent = Agent(
        model=MODEL,
        stream_fn=provider.stream,
        tools=[flaky_divide],
        retry_event_sink=JsonlRetryEventStore(STATE_PATH).append,
    )
    agent.subscribe(_print_retry_event)
    await agent.prompt("计算 10÷4")
    result = next(item for item in agent.state.messages if item["role"] == "toolResult")
    print("divide Attempts：", attempts)
    print("divide 最终结果：", result["content"][0]["text"])


async def circuit_demo() -> None:
    print("\n[Circuit Breaker：连续失败后 Open]")
    breaker = CircuitBreaker(CircuitBreakerPolicy(
        enabled=True,
        failure_threshold=2,
        recovery_timeout_seconds=30,
    ))
    await breaker.before_call()
    await breaker.record_failure()
    await breaker.before_call()
    await breaker.record_failure()
    try:
        await breaker.before_call()
    except CircuitOpenError as error:
        print("状态：", breaker.state, "；快速失败：", error)


async def task_demo() -> None:
    print("\n[Task Retry：Worker 瞬时失败]")
    attempts = 0
    executor = TaskRetryExecutor(
        TaskRetryPolicy(
            max_retries=2,
            retryable_codes=frozenset({"worker_unavailable"}),
            idempotent=True,
            initial_delay_seconds=0,
            max_delay_seconds=1,
            jitter_ratio=0,
        ),
        event_store=JsonlRetryEventStore(STATE_PATH),
        event_sink=_print_retry_event,
    )

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RetryableTaskError("Worker 不可用", code="worker_unavailable")
        return "task-ok"

    result = await executor.execute("demo-task", operation, CancellationToken())
    print("Task Attempts：", attempts, "；结果：", result)


async def outcome_demo() -> None:
    print("\n[outcome_unknown：不重放写操作，只核对状态]")
    unknown = OutcomeUnknownToolError(
        "写操作结果不确定",
        operation_id="operation-demo",
        idempotency_key="hidden-key",
        reconciliation_name="check_demo",
    )
    registry = OutcomeReconciliationRegistry()

    async def reconcile(error, _token):
        return AgentToolResult(
            content=[{"type": "text", "text": "核对确认：操作已成功"}],
            details={"operationId": error.operation_id, "status": "succeeded"},
        )

    registry.register("check_demo", reconcile)
    result = await registry.reconcile(unknown, CancellationToken())
    print(result.content[0]["text"])


async def compaction_demo() -> None:
    print("\n[Context Overflow：压缩后重试]")
    overflow = assistant_message(
        model=MODEL,
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
            model=MODEL,
            content=[{"type": "text", "text": "压缩后成功"}],
        ),
    ])
    agent = Agent(
        model=MODEL,
        stream_fn=compact_on_context_overflow(
            provider.stream,
            CompactionRetryPolicy(max_retries=1, keep_recent_messages=2),
        ),
        messages=[user_message(f"旧消息 {index}") for index in range(5)],
        retry_event_sink=JsonlRetryEventStore(STATE_PATH).append,
    )
    agent.subscribe(_print_retry_event)
    await agent.prompt("最新问题")
    print("压缩前消息数：", len(provider.contexts[0]["messages"]))
    print("压缩后消息数：", len(provider.contexts[1]["messages"]))
    print("最终回答：", agent.state.messages[-1]["content"][0]["text"])


async def recovery_demo() -> None:
    print("\n[进程恢复：扫描未完成 Retry Chain]")
    store = JsonlRetryEventStore(STATE_PATH)
    if not store.incomplete_chains():
        await store.append({
            "type": "task_retry_scheduled",
            "kind": "task",
            "retryId": "simulated-crash-chain",
            "taskId": "crashed-task",
            "attempt": 1,
        })
    pending = store.incomplete_chains()
    print("未完成 Chain 数：", len(pending))

    async def handler(chain) -> bool:
        print("恢复 Chain：", chain.retry_id, "类型：", chain.kind)
        return True

    await RetryRecoveryManager(store).recover(handler)


def _print_retry_event(event, _token=None) -> None:
    event_type = event.get("type", "")
    if "retry" in event_type or "compaction" in event_type:
        print("事件：", event_type, {k: v for k, v in event.items() if k != "type"})


async def main() -> None:
    parser = argparse.ArgumentParser(description="离线 Retry 功能演示")
    parser.add_argument(
        "scenario",
        choices=["all", "model", "tool", "circuit", "task", "outcome", "compaction", "recovery"],
        default="all",
        nargs="?",
    )
    scenario = parser.parse_args().scenario
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if STATE_PATH.exists():
        STATE_PATH.unlink()

    actions = {
        "model": model_retry_demo,
        "tool": tool_retry_demo,
        "circuit": circuit_demo,
        "task": task_demo,
        "outcome": outcome_demo,
        "compaction": compaction_demo,
        "recovery": recovery_demo,
    }
    if scenario == "all":
        for action in actions.values():
            await action()
    else:
        await actions[scenario]()
    print("\nRetry Journal：", STATE_PATH)


if __name__ == "__main__":
    asyncio.run(main())
