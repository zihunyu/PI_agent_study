"""可信 Approval、幂等写操作和崩溃恢复骨架离线演示。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pi_agent_loop import (  # noqa: E402
    ApprovalService,
    DurableSessionRecovery,
    IdentityClaim,
    JsonlOperationEventStore,
    Model,
    ModelRequestPolicy,
    RecoveryCallbacks,
    StaticIdentityVerifier,
    WriteOperationService,
    assistant_message,
)


async def approval_and_write_demo(store: JsonlOperationEventStore) -> None:
    print("\n[可信身份 + Approval + 幂等写操作]")
    session_id = "durable-demo"
    operation_id = "write-operation"
    await store.append(
        "operation_started",
        session_id,
        operation_id,
        {"configuration": {}, "tools": []},
    )
    verifier = StaticIdentityVerifier({
        "operator": ("operator-secret", {"operator"}),
        "auditor": ("auditor-secret", {"approver"}),
    })
    operator = await verifier.verify(IdentityClaim("operator", "operator-secret"))
    auditor = await verifier.verify(IdentityClaim("auditor", "auditor-secret"))
    approvals = ApprovalService(store)
    writes = WriteOperationService(store, approvals)
    write = await writes.prepare(
        session_id=session_id,
        operation_id=operation_id,
        tool_name="cancel_order",
        arguments={"order_id": "1001"},
        idempotency_key="demo-idempotency-key",
        requester=operator,
        requires_approval=True,
    )
    print("准备后状态：", write.state)
    await approvals.grant(write.approval_id, auditor)

    calls = 0

    async def handler(arguments, idempotency_key, actor):
        nonlocal calls
        calls += 1
        return {
            "status": "cancelled",
            "orderId": arguments["order_id"],
            "actor": actor.principal_id,
            "idempotencyAccepted": bool(idempotency_key),
        }

    write = await writes.execute(
        write.write_id,
        actor=operator,
        idempotency_key="demo-idempotency-key",
        handler=handler,
    )
    duplicate = await writes.prepare(
        session_id=session_id,
        operation_id=operation_id,
        tool_name="cancel_order",
        arguments={"order_id": "1001"},
        idempotency_key="demo-idempotency-key",
        requester=operator,
        requires_approval=True,
    )
    duplicate = await writes.execute(
        duplicate.write_id,
        actor=operator,
        idempotency_key="demo-idempotency-key",
        handler=handler,
    )
    print("执行后状态：", write.state)
    print("重复请求 Write ID 相同：", duplicate.write_id == write.write_id)
    print("真实 Handler 调用次数：", calls)


async def crash_recovery_demo(store: JsonlOperationEventStore) -> None:
    print("\n[完整 Context + Safe Tool Replay + 继续模型]")
    session_id = "durable-demo"
    operation_id = "recovery-operation"
    model = Model(id="recovery-demo", provider="fake", api="fake")
    await store.append(
        "operation_started",
        session_id,
        operation_id,
        {"configuration": {"model": model.id}, "tools": []},
    )
    policy = ModelRequestPolicy(
        visible_tool_names=("divide",),
        tool_choice="required",
        allowed_tool_names=("divide",),
        expected_tool_arguments={"a": 10, "b": 2},
        continuation_policy=ModelRequestPolicy.no_tools(),
    )
    await store.append(
        "model_policy_selected",
        session_id,
        operation_id,
        {"policy": policy.to_dict()},
    )
    await store.append(
        "message_appended",
        session_id,
        operation_id,
        {
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "计算 10÷2"}],
            }
        },
    )
    assistant = assistant_message(
        model=model,
        stop_reason="toolUse",
        content=[{
            "type": "toolCall",
            "id": "divide-after-crash",
            "name": "divide",
            "arguments": {"a": 10, "b": 2},
        }],
    )
    await store.append(
        "model_request_started",
        session_id,
        operation_id,
        {
            "requestId": "request-before-crash",
            "requestPolicy": policy.to_dict(),
        },
    )
    await store.append(
        "model_request_completed",
        session_id,
        operation_id,
        {"requestId": "request-before-crash", "message": assistant},
    )
    await store.append(
        "tool_intent_recorded",
        session_id,
        operation_id,
        {
            "toolCallId": "divide-after-crash",
            "toolName": "divide",
            "arguments": {"a": 10, "b": 2},
            "replayPolicy": "safe",
        },
    )
    await store.append(
        "tool_dispatch_started",
        session_id,
        operation_id,
        {"toolCallId": "divide-after-crash"},
    )

    async def execute_tool(action):
        return {
            "role": "toolResult",
            "toolCallId": action.tool_call_id,
            "toolName": action.tool_name,
            "content": [{"type": "text", "text": "5.0"}],
            "details": {"recovered": True},
            "isError": False,
        }

    async def request_model(messages, recovered_policy):
        print("恢复后的模型 Context 角色：", [item["role"] for item in messages])
        print("恢复策略 Tool Choice：", recovered_policy.tool_choice)
        return assistant_message(
            model=model,
            content=[{"type": "text", "text": "恢复完成，结果是 5"}],
        )

    async def reconcile_tool(_action):
        raise RuntimeError("safe divide 不需要核对")

    result = await DurableSessionRecovery(store).resume(
        session_id=session_id,
        operation_id=operation_id,
        callbacks=RecoveryCallbacks(
            request_model=request_model,
            execute_tool=execute_tool,
            reconcile_tool=reconcile_tool,
        ),
    )
    print("恢复状态：", result.status)
    print("最终消息：", result.operation.messages[-1]["content"][0]["text"])


async def main() -> None:
    path = ROOT / "state" / "durable-session-demo.jsonl"
    if path.exists():
        path.unlink()
    store = JsonlOperationEventStore(path)
    await approval_and_write_demo(store)
    await crash_recovery_demo(store)
    print("\nOperation Journal：", path)


if __name__ == "__main__":
    asyncio.run(main())
