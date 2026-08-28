"""P1 DurableAgentHost、恢复 Runtime 和 Approval Resume 离线演示。"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pi_agent_loop import (  # noqa: E402
    AgentTool,
    AgentToolResult,
    CapabilityRegistry,
    DurableAgentHost,
    IdentityClaim,
    Model,
    RequestDecision,
    ScriptedProvider,
    StaticIdentityVerifier,
    assistant_message,
    create_divide_tool,
)

MODEL = Model(id="p1-host-demo", provider="scripted", api="fake")


class FixedApprovalRouter:
    def route(self, _text):
        return RequestDecision(
            status="in_scope_approval_required",
            reason="写操作需要审批",
            message="等待审批",
            domain="orders",
            intent="order.cancel",
            extracted_fields={"order_id": "1001"},
            required_capabilities=("orders.cancel",),
            selected_tools=("cancel_order",),
            requires_approval=True,
        )


async def normal_host_demo() -> None:
    print("\n[DurableAgentHost 自动装配]")
    provider = ScriptedProvider([
        assistant_message(
            model=MODEL,
            content=[{"type": "text", "text": "Host 正常回答"}],
        )
    ])
    host = await DurableAgentHost.create(
        session_id="p1-normal",
        state_dir=ROOT / "state" / "p1-normal",
        model=MODEL,
        stream_fn=provider.stream,
        system_prompt="P1 Host 演示",
        tools=[create_divide_tool()],
    )
    await host.prompt("你好")
    print("回答：", host.agent.state.messages[-1]["content"][0]["text"])
    print("运行状态：", host.runtime_tracker.state.phase)
    print("Operation ID：", host.operation_recorder.last_operation_id)
    print("启动恢复报告：", host.startup_recovery_report)
    await host.close()


async def approval_resume_demo() -> None:
    print("\n[Approval → 幂等写操作 → 继续模型]")

    async def forbidden_direct_execute(_id, _args, _token, _update):
        raise RuntimeError("写工具不能绕过 WriteOperationService")

    write_tool = AgentTool(
        name="cancel_order",
        label="取消订单",
        description="取消订单写操作",
        parameters={"type": "object"},
        execute=forbidden_direct_execute,
        execution_mode="exclusive",
        replay_policy="never",
    )
    capabilities = CapabilityRegistry()
    capabilities.register(
        write_tool,
        capabilities={"orders.cancel"},
        domain="orders",
        operation="write",
        requires_approval=True,
    )
    provider = ScriptedProvider([
        assistant_message(
            model=MODEL,
            content=[{"type": "text", "text": "订单 1001 已取消"}],
        )
    ])
    host = await DurableAgentHost.create(
        session_id="p1-approval",
        state_dir=ROOT / "state" / "p1-approval",
        model=MODEL,
        stream_fn=provider.stream,
        system_prompt="审批恢复演示",
        tools=[write_tool],
        router=FixedApprovalRouter(),
        capabilities=capabilities,
    )
    verifier = StaticIdentityVerifier({
        "operator": ("operator-secret", {"operator"}),
        "approver": ("approver-secret", {"approver"}),
    })
    operator = await verifier.verify(IdentityClaim("operator", "operator-secret"))
    approver = await verifier.verify(IdentityClaim("approver", "approver-secret"))
    pending = await host.prompt(
        "取消订单 1001",
        requester=operator,
        idempotency_key="cancel-order-1001",
    )
    print("等待状态：", host.runtime_tracker.state.phase)
    print("Approval ID：", pending.approval_id)

    calls = 0

    async def write_handler(arguments, _idempotency_key, actor):
        nonlocal calls
        calls += 1
        return {
            "status": "cancelled",
            "orderId": arguments["order_id"],
            "actor": actor.principal_id,
        }

    final = await host.approve_and_resume(
        pending.approval_id,
        approver=approver,
        consumer=operator,
        idempotency_key="cancel-order-1001",
        write_handler=write_handler,
    )
    print("写 Handler 次数：", calls)
    print("最终回答：", final["content"][0]["text"])
    print("最终状态：", host.runtime_tracker.state.phase)
    print("Operation ID：", pending.operation_id)
    await host.close()


async def main() -> None:
    for directory in (ROOT / "state" / "p1-normal", ROOT / "state" / "p1-approval"):
        if directory.exists():
            shutil.rmtree(directory)
    await normal_host_demo()
    await approval_resume_demo()


if __name__ == "__main__":
    asyncio.run(main())
