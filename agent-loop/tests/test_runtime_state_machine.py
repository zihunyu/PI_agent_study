"""Runtime 状态机、持久重放、恢复和 Domain 状态机测试。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import (  # noqa: E402
    Agent,
    CapabilityRegistry,
    DomainEvent,
    DomainStateMachine,
    DomainTransition,
    DomainTransitionError,
    InMemoryRuntimeEventStore,
    JsonlRuntimeEventStore,
    Model,
    RequestDecision,
    RoutedAgent,
    RunState,
    RuntimeEvent,
    RuntimeInvariantError,
    RuntimeRecoveryManager,
    RuntimeStateTracker,
    ScriptedProvider,
    assistant_message,
    create_divide_tool,
    project_runtime_state,
    reduce_runtime_state,
    replay_runtime_events,
)


class RuntimeStateMachineTests(unittest.IsolatedAsyncioTestCase):
    def event(self, state: RunState, event_type, **data) -> RuntimeEvent:
        return RuntimeEvent(
            type=event_type,
            run_id=state.run_id or "run-1",
            sequence=state.sequence + 1,
            data=data,
        )

    def reduce(self, state: RunState, event_type, **data) -> RunState:
        return reduce_runtime_state(state, self.event(state, event_type, **data))

    async def test_完整_run_tool_retry_生命周期(self) -> None:
        state = self.reduce(RunState(), "run_started")
        state = self.reduce(state, "turn_started", turn=1)
        state = self.reduce(state, "model_request_started")
        state = self.reduce(state, "model_response_finished", stopReason="toolUse")
        state = self.reduce(
            state,
            "tool_started",
            toolCallId="divide-1",
            toolName="divide",
        )
        state = self.reduce(
            state,
            "tool_retry_scheduled",
            toolCallId="divide-1",
            attempt=1,
        )
        self.assertEqual(state.tools["divide-1"].phase, "retry_backoff")
        state = self.reduce(
            state,
            "tool_retry_attempt_started",
            toolCallId="divide-1",
        )
        state = self.reduce(
            state,
            "tool_retry_finished",
            toolCallId="divide-1",
            success=True,
        )
        state = self.reduce(
            state,
            "tool_finished",
            toolCallId="divide-1",
            success=True,
        )
        state = self.reduce(state, "turn_finished")
        state = self.reduce(state, "run_finished", outcome="completed")

        self.assertEqual(state.phase, "completed")
        self.assertTrue(state.terminal)
        self.assertEqual(state.tools["divide-1"].phase, "succeeded")

    async def test_并行工具全部结束前保持_executing_tools(self) -> None:
        state = self.reduce(RunState(), "run_started")
        state = self.reduce(
            state,
            "tool_started",
            toolCallId="a",
            toolName="add",
        )
        state = self.reduce(
            state,
            "tool_started",
            toolCallId="b",
            toolName="divide",
        )
        state = self.reduce(state, "tool_finished", toolCallId="a", success=True)
        self.assertEqual(state.phase, "executing_tools")
        self.assertEqual(state.active_tool_count, 1)

        state = self.reduce(state, "tool_finished", toolCallId="b", success=True)
        self.assertEqual(state.phase, "running")
        self.assertEqual(state.active_tool_count, 0)

    async def test_未知工具结束事件被_invariant_拒绝(self) -> None:
        state = self.reduce(RunState(), "run_started")
        with self.assertRaisesRegex(RuntimeInvariantError, "未知 Tool Call"):
            self.reduce(
                state,
                "tool_finished",
                toolCallId="missing",
                success=True,
            )

    async def test_终态后不能继续写事件(self) -> None:
        state = self.reduce(RunState(), "run_started")
        state = self.reduce(state, "run_finished", outcome="completed")
        with self.assertRaisesRegex(RuntimeInvariantError, "终态"):
            self.reduce(state, "turn_started", turn=1)

    async def test_jsonl_重放得到相同状态(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JsonlRuntimeEventStore(Path(directory) / "runtime.jsonl")
            events = [
                RuntimeEvent("run_started", "run-1", 0),
                RuntimeEvent("turn_started", "run-1", 1, data={"turn": 1}),
                RuntimeEvent("model_request_started", "run-1", 2),
                RuntimeEvent(
                    "model_response_finished",
                    "run-1",
                    3,
                    data={"stopReason": "stop"},
                ),
                RuntimeEvent("turn_finished", "run-1", 4),
                RuntimeEvent(
                    "run_finished",
                    "run-1",
                    5,
                    data={"outcome": "completed"},
                ),
            ]
            for event in events:
                await store.append(event)

            loaded = await store.load()
            state = replay_runtime_events(loaded)

            self.assertEqual(state.phase, "completed")
            self.assertEqual(state.sequence, 5)

    async def test_进程中断恢复为_suspended(self) -> None:
        store = InMemoryRuntimeEventStore()
        await store.append(RuntimeEvent("run_started", "run-1", 0))
        await store.append(
            RuntimeEvent("turn_started", "run-1", 1, data={"turn": 1})
        )

        recovered = await RuntimeRecoveryManager(store).recover()

        self.assertEqual(recovered.phase, "suspended")
        self.assertEqual(recovered.failure_code, "process_interrupted")
        self.assertEqual(len(await store.load()), 3)

    async def test_tracker_可直接订阅现有_agent_event(self) -> None:
        model = Model(id="state-model", provider="fake", api="fake")
        provider = ScriptedProvider([
            assistant_message(
                model=model,
                stop_reason="toolUse",
                content=[{
                    "type": "toolCall",
                    "id": "divide-state",
                    "name": "divide",
                    "arguments": {"a": 10, "b": 2},
                }],
            ),
            assistant_message(
                model=model,
                content=[{"type": "text", "text": "结果是 5"}],
            ),
        ])
        store = InMemoryRuntimeEventStore()
        tracker = await RuntimeStateTracker.create(store)
        agent = Agent(
            model=model,
            stream_fn=provider.stream,
            tools=[create_divide_tool()],
        )
        agent.subscribe(tracker.listener)

        await agent.prompt("计算 10÷2")

        self.assertEqual(tracker.state.phase, "completed")
        self.assertEqual(tracker.state.turn, 2)
        self.assertEqual(
            tracker.state.tools["divide-state"].phase,
            "succeeded",
        )
        view = project_runtime_state(tracker.state)
        self.assertEqual(view["phaseLabel"], "已完成")
        self.assertEqual(view["activeToolCount"], 0)

    async def test_routed_agent_路由阶段也进入同一个_run(self) -> None:
        class FixedRouter:
            def route(self, _text):
                return RequestDecision(
                    status="in_scope_capability_missing",
                    reason="缺少能力",
                    message="当前缺少业务能力",
                )

        store = InMemoryRuntimeEventStore()
        tracker = await RuntimeStateTracker.create(store)
        routed = RoutedAgent(
            Agent(
                model=Model(id="route-state", provider="fake", api="fake"),
                stream_fn=ScriptedProvider([]).stream,
            ),
            FixedRouter(),
            CapabilityRegistry(),
            runtime_tracker=tracker,
        )

        result = await routed.prompt("查询业务数据")

        self.assertFalse(result.model_called)
        self.assertEqual(tracker.state.phase, "completed")
        self.assertEqual(
            tracker.state.routing_status,
            "in_scope_capability_missing",
        )

    async def test_domain_state_只能由可信事件推进(self) -> None:
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
            ],
        )
        state = machine.initial("order-1001")
        state = machine.apply(
            state,
            DomainEvent(
                entity_id="order-1001",
                type="payment_succeeded",
                source="payment_api",
                expected_version=0,
            ),
        )
        self.assertEqual(state.state, "paid")

        with self.assertRaisesRegex(DomainTransitionError, "不能驱动"):
            machine.apply(
                state,
                DomainEvent(
                    entity_id="order-1001",
                    type="shipment_created",
                    source="user_message",
                    expected_version=1,
                ),
            )

    async def test_domain_审批和乐观版本检查(self) -> None:
        machine = DomainStateMachine(
            initial_state="paid",
            transitions=[
                DomainTransition(
                    event_type="cancel_approved",
                    from_states=frozenset({"paid"}),
                    to_state="cancelled",
                    allowed_sources=frozenset({"cancel_api"}),
                    requires_approval=True,
                )
            ],
        )
        state = machine.initial("order-1002")
        event = DomainEvent(
            entity_id="order-1002",
            type="cancel_approved",
            source="cancel_api",
            expected_version=0,
            approved=False,
        )
        with self.assertRaisesRegex(DomainTransitionError, "需要审批"):
            machine.apply(state, event)

        approved = DomainEvent(
            entity_id="order-1002",
            type="cancel_approved",
            source="cancel_api",
            expected_version=0,
            approved=True,
        )
        state = machine.apply(state, approved)
        self.assertEqual(state.state, "cancelled")
        with self.assertRaisesRegex(DomainTransitionError, "版本"):
            machine.apply(state, approved)


if __name__ == "__main__":
    unittest.main()
