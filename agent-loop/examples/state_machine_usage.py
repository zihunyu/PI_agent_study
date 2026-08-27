"""Runtime 状态机和项目业务状态机离线演示。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pi_agent_loop import (  # noqa: E402
    Agent,
    DomainEvent,
    DomainStateMachine,
    DomainTransition,
    DomainTransitionError,
    JsonlRuntimeEventStore,
    Model,
    RuntimeRecoveryManager,
    RuntimeStateTracker,
    ScriptedProvider,
    assistant_message,
    create_divide_tool,
    project_runtime_state,
)


async def runtime_demo() -> None:
    print("\n[通用 Agent Runtime 状态机]")
    path = ROOT / "state" / "state-machine-demo.jsonl"
    if path.exists():
        path.unlink()
    store = JsonlRuntimeEventStore(path)
    await RuntimeRecoveryManager(store).recover()
    tracker = await RuntimeStateTracker.create(store)
    model = Model(id="state-demo", provider="scripted", api="fake")
    provider = ScriptedProvider([
        assistant_message(
            model=model,
            stop_reason="toolUse",
            content=[{
                "type": "toolCall",
                "id": "divide-demo",
                "name": "divide",
                "arguments": {"a": 10, "b": 2},
            }],
        ),
        assistant_message(
            model=model,
            content=[{"type": "text", "text": "结果是 5"}],
        ),
    ])
    agent = Agent(
        model=model,
        stream_fn=provider.stream,
        tools=[create_divide_tool()],
    )
    agent.subscribe(tracker.listener)
    await agent.prompt("计算 10÷2")

    view = project_runtime_state(tracker.state)
    print("Run ID：", view["runId"])
    print("最终阶段：", view["phaseLabel"])
    print("模型 Turn：", view["turn"])
    print("工具状态：", view["tools"])
    print("持久事件：", path)


async def domain_demo() -> None:
    print("\n[项目业务状态机：订单示例]")
    machine = DomainStateMachine(
        initial_state="pending_payment",
        transitions=[
            DomainTransition(
                event_type="payment_succeeded",
                from_states=frozenset({"pending_payment"}),
                to_state="paid",
                allowed_sources=frozenset({"payment_api"}),
            ),
            DomainTransition(
                event_type="shipment_created",
                from_states=frozenset({"paid"}),
                to_state="shipped",
                allowed_sources=frozenset({"order_api"}),
            ),
            DomainTransition(
                event_type="cancel_approved",
                from_states=frozenset({"paid"}),
                to_state="cancelled",
                allowed_sources=frozenset({"cancel_api"}),
                requires_approval=True,
            ),
        ],
    )
    state = machine.initial("order-1001")
    print("初始状态：", state.state)
    state = machine.apply(
        state,
        DomainEvent(
            entity_id="order-1001",
            type="payment_succeeded",
            source="payment_api",
            expected_version=0,
        ),
    )
    print("支付成功后：", state.state)
    state = machine.apply(
        state,
        DomainEvent(
            entity_id="order-1001",
            type="shipment_created",
            source="order_api",
            expected_version=1,
        ),
    )
    print("发货事件后：", state.state)

    try:
        machine.apply(
            state,
            DomainEvent(
                entity_id="order-1001",
                type="shipment_created",
                source="user_message",
                expected_version=2,
            ),
        )
    except DomainTransitionError as error:
        print("非法转换被拒绝：", error.code, str(error))


async def main() -> None:
    await runtime_demo()
    await domain_demo()


if __name__ == "__main__":
    asyncio.run(main())
