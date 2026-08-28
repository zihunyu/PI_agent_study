"""RoutedAgent 强制工具策略和 Guard 集成测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi_agent_loop import (  # noqa: E402
    Agent,
    AgentTool,
    AgentToolResult,
    CapabilityRegistry,
    DurableOperationRecorder,
    InMemoryOperationEventStore,
    Model,
    RequestDecision,
    RoutedAgent,
    ScriptedProvider,
    ToolChoicePolicy,
    assistant_message,
    replay_operation,
)


class FixedRouter:
    """只用于隔离测试 RoutedAgent，不承担自然语言分类。"""

    def __init__(self, decision: RequestDecision) -> None:
        self.decision = decision

    def route(self, _user_text: str) -> RequestDecision:
        return self.decision


class RoutedAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.model = Model(id="routing-model", provider="fake", api="fake")

    def make_capabilities(self) -> CapabilityRegistry:
        async def execute(_id, arguments, _token, _update):
            return AgentToolResult(
                content=[
                    {
                        "type": "text",
                        "text": f"订单 {arguments['order_id']} 当前状态：已发货",
                    }
                ],
                details={"source": "mock-order-system"},
            )

        tool = AgentTool(
            name="get_order_status",
            label="查询订单状态",
            description="读取订单系统中的当前真实状态",
            parameters={
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
                "additionalProperties": False,
            },
            validate_args=lambda value: value,
            execute=execute,
        )
        capabilities = CapabilityRegistry()
        capabilities.register(
            tool,
            capabilities={"orders.read_current"},
            domain="orders",
        )
        return capabilities

    def tool_decision(self) -> RequestDecision:
        return RequestDecision(
            status="in_scope_tool_ready",
            reason="测试要求查询当前订单",
            message="已找到工具",
            domain="order",
            intent="order.get_status",
            extracted_fields={"order_id": "1001"},
            required_capabilities=("orders.read_current",),
            selected_tools=("get_order_status",),
            tool_policy=ToolChoicePolicy("required"),
        )

    async def test_required_请求被模型直接回答时_guard_拒绝(self) -> None:
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=self.model,
                    content=[
                        {
                            "type": "text",
                            "text": "订单已经发货（这是模型猜测）",
                        }
                    ],
                )
            ]
        )
        capabilities = self.make_capabilities()
        agent = Agent(model=self.model, stream_fn=provider.stream)
        routed = RoutedAgent(
            agent,
            FixedRouter(self.tool_decision()),
            capabilities,
        )

        result = await routed.prompt("查询订单 1001 当前状态")

        self.assertTrue(result.model_called)
        self.assertEqual(result.error_code, "required_tool_call_missing")
        self.assertIn("必须使用工具", result.response_text)
        self.assertEqual(provider.call_count, 1)

    async def test_required_工具参数与_router_提取值不一致时拒绝(self) -> None:
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=self.model,
                    stop_reason="toolUse",
                    content=[
                        {
                            "type": "toolCall",
                            "id": "wrong-order",
                            "name": "get_order_status",
                            "arguments": {"order_id": "9999"},
                        }
                    ],
                )
            ]
        )
        capabilities = self.make_capabilities()
        agent = Agent(model=self.model, stream_fn=provider.stream)
        routed = RoutedAgent(
            agent,
            FixedRouter(self.tool_decision()),
            capabilities,
        )

        result = await routed.prompt("查询订单 1001 当前状态")

        self.assertEqual(result.error_code, "required_tool_arguments_mismatch")
        self.assertFalse(
            any(message.get("role") == "toolResult" for message in agent.state.messages)
        )

    async def test_required_工具成功后下一轮切回_auto_生成回答(self) -> None:
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=self.model,
                    stop_reason="toolUse",
                    content=[
                        {
                            "type": "toolCall",
                            "id": "order-call",
                            "name": "get_order_status",
                            "arguments": {"order_id": "1001"},
                        }
                    ],
                ),
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "订单 1001 已发货"}],
                ),
            ]
        )
        capabilities = self.make_capabilities()
        agent = Agent(model=self.model, stream_fn=provider.stream)
        routed = RoutedAgent(
            agent,
            FixedRouter(self.tool_decision()),
            capabilities,
        )

        result = await routed.prompt("查询订单 1001 当前状态")

        self.assertIsNone(result.error_code)
        self.assertEqual(result.response_text, "订单 1001 已发货")
        self.assertEqual(provider.call_count, 2)

    async def test_required策略按每次模型请求持久化并切换continuation(self) -> None:
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=self.model,
                    stop_reason="toolUse",
                    content=[
                        {
                            "type": "toolCall",
                            "id": "order-policy-call",
                            "name": "get_order_status",
                            "arguments": {"order_id": "1001"},
                        }
                    ],
                ),
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "订单 1001 已发货"}],
                ),
            ]
        )
        capabilities = self.make_capabilities()
        store = InMemoryOperationEventStore()
        recorder = DurableOperationRecorder(
            store,
            session_id="policy-session",
            tools=capabilities.all_tools(),
        )
        routed = RoutedAgent(
            Agent(model=self.model, stream_fn=provider.stream),
            FixedRouter(self.tool_decision()),
            capabilities,
            operation_recorder=recorder,
        )

        await routed.prompt("查询订单 1001 当前状态")

        operation = replay_operation(
            await store.load(operation_id=recorder.last_operation_id)
        )
        requests = list(operation.model_requests.values())
        self.assertEqual(len(requests), 2)
        first = requests[0].policy
        second = requests[1].policy
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(first.visible_tool_names, ("get_order_status",))
        self.assertEqual(first.tool_choice, "required")
        self.assertEqual(first.allowed_tool_names, ("get_order_status",))
        self.assertEqual(
            first.expected_tool_arguments,
            {"order_id": "1001"},
        )
        self.assertEqual(second.visible_tool_names, ("get_order_status",))
        self.assertEqual(second.tool_choice, "auto")
        self.assertIsNone(second.expected_tool_arguments)

    async def test_capability_missing_不调用模型(self) -> None:
        provider = ScriptedProvider([])
        capabilities = CapabilityRegistry()
        decision = RequestDecision(
            status="in_scope_capability_missing",
            reason="缺少能力",
            message="当前缺少能力：orders.read_current",
            missing_capabilities=("orders.read_current",),
        )
        routed = RoutedAgent(
            Agent(model=self.model, stream_fn=provider.stream),
            FixedRouter(decision),
            capabilities,
        )

        result = await routed.prompt("查询订单 1001 当前状态")

        self.assertFalse(result.model_called)
        self.assertEqual(result.error_code, "in_scope_capability_missing")
        self.assertEqual(provider.call_count, 0)

    async def test_no_tool_intent_不向模型暴露业务工具(self) -> None:
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=self.model,
                    content=[
                        {
                            "type": "text",
                            "text": "订单状态表示订单当前所处阶段。",
                        }
                    ],
                )
            ]
        )
        capabilities = self.make_capabilities()
        decision = RequestDecision(
            status="in_scope_no_tool",
            reason="稳定概念",
            message="直接回答",
            intent="order.explain_status",
            tool_policy=ToolChoicePolicy("none"),
        )
        agent = Agent(model=self.model, stream_fn=provider.stream)
        routed = RoutedAgent(agent, FixedRouter(decision), capabilities)

        result = await routed.prompt("请解释什么是订单状态")

        self.assertIsNone(result.error_code)
        self.assertEqual(provider.contexts[0]["tools"], [])

    def test_capability_registry_拒绝工具重名(self) -> None:
        capabilities = self.make_capabilities()
        with self.assertRaisesRegex(ValueError, "已存在工具"):
            capabilities.register(
                capabilities.all_tools()[0],
                capabilities={"orders.read_current"},
                domain="orders",
            )


if __name__ == "__main__":
    unittest.main()
