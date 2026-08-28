"""HybridModelRouter 语言分类与 Host 安全边界测试。"""

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
    HybridModelRouter,
    Model,
    RoutedAgent,
    RouterEvaluationCase,
    RouterEvaluationDataset,
    RouterEvaluator,
    ScriptedProvider,
    assistant_message,
    load_simple_business_config,
)


class HybridRouterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.config = load_simple_business_config(
            root / "config" / "business.toml.example"
        )
        self.model = Model(id="hybrid-model", provider="fake", api="fake")

    def classifier_response(
        self,
        decision: str,
        *,
        arguments: dict | None = None,
        confidence: float = 0.95,
        usage: dict | None = None,
    ) -> dict:
        return assistant_message(
            model=self.model,
            stop_reason="toolUse",
            content=[
                {
                    "type": "toolCall",
                    "id": "route-call",
                    "name": "select_business_intent",
                    "arguments": {
                        "decision": decision,
                        "arguments": arguments or {},
                        "confidence": confidence,
                        "reason": "测试分类",
                    },
                }
            ],
            usage=usage,
        )

    def capabilities(self, *, include_cancel: bool = False) -> CapabilityRegistry:
        async def query(_id, arguments, _token, _update):
            return AgentToolResult(
                content=[
                    {
                        "type": "text",
                        "text": f"订单 {arguments['order_id']}：已发货",
                    }
                ],
                details={},
            )

        registry = CapabilityRegistry()
        registry.register(
            AgentTool(
                name="get_order_status",
                label="查询订单",
                description="读取真实订单状态",
                parameters={
                    "type": "object",
                    "properties": {"order_id": {"type": "string"}},
                    "required": ["order_id"],
                },
                validate_args=lambda value: value,
                execute=query,
            ),
            capabilities={"orders.read_current"},
            domain="orders",
        )
        if include_cancel:
            registry.register(
                AgentTool(
                    name="cancel_order",
                    label="取消订单",
                    description="取消订单",
                    parameters={"type": "object"},
                    execute=query,
                ),
                capabilities={"orders.cancel"},
                domain="orders",
                operation="write",
                requires_approval=True,
            )
        return registry

    async def test_模型把自由表达映射到已配置_intent(self) -> None:
        provider = ScriptedProvider(
            [
                self.classifier_response(
                    "order.get_status",
                    arguments={"order_id": "1001"},
                )
            ]
        )
        capabilities = self.capabilities()
        router = HybridModelRouter(
            self.config,
            capabilities,
            model=self.model,
            stream_fn=provider.stream,
        )

        decision = await router.route("劳驾看一下编号1001的单子走到哪里了")

        self.assertEqual(decision.status, "in_scope_tool_ready")
        self.assertEqual(decision.intent, "order.get_status")
        self.assertEqual(decision.extracted_fields["order_id"], "1001")
        self.assertEqual(decision.routing_source, "model")
        self.assertEqual(decision.tool_policy.mode, "required")
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(
            provider.contexts[0]["tools"][0]["name"],
            "select_business_intent",
        )

    async def test_router_evaluator读取真实hybrid调用的usage和cost(self) -> None:
        usage = {
            "input": 123,
            "output": 45,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 168,
            "cost": {"total": 0.42},
        }
        provider = ScriptedProvider(
            [
                self.classifier_response(
                    "order.get_status",
                    arguments={"order_id": "1001"},
                    usage=usage,
                )
            ]
        )
        router = HybridModelRouter(
            self.config,
            self.capabilities(),
            model=self.model,
            stream_fn=provider.stream,
        )
        report = await RouterEvaluator(
            router, model_version="hybrid-measured"
        ).evaluate(
            RouterEvaluationDataset(
                "hybrid",
                (
                    RouterEvaluationCase(
                        "read",
                        "查询订单 1001",
                        expected_intent="order.get_status",
                        requires_tool=True,
                        expected_tools=("get_order_status",),
                    ),
                ),
            )
        )
        self.assertEqual(report.total_input_tokens, 123)
        self.assertEqual(report.total_output_tokens, 45)
        self.assertEqual(report.total_cost, 0.42)

    async def test_no_tool_intent_由模型识别但不要求业务工具(self) -> None:
        provider = ScriptedProvider(
            [self.classifier_response("order.explain_status")]
        )
        router = HybridModelRouter(
            self.config,
            self.capabilities(),
            model=self.model,
            stream_fn=provider.stream,
        )

        decision = await router.route("已发货这个词具体代表什么含义")

        self.assertEqual(decision.status, "in_scope_no_tool")
        self.assertEqual(decision.tool_policy.mode, "none")
        self.assertEqual(decision.selected_tools, ())

    async def test_有_intent_但能力未注册时返回_capability_missing(self) -> None:
        provider = ScriptedProvider(
            [
                self.classifier_response(
                    "order.get_status",
                    arguments={"order_id": "1001"},
                )
            ]
        )
        router = HybridModelRouter(
            self.config,
            CapabilityRegistry(),
            model=self.model,
            stream_fn=provider.stream,
        )

        decision = await router.route("查一下订单 1001")

        self.assertEqual(decision.status, "in_scope_capability_missing")
        self.assertEqual(decision.missing_capabilities, ("orders.read_current",))

    async def test_低置信度时追问而不执行工具(self) -> None:
        provider = ScriptedProvider(
            [
                self.classifier_response(
                    "order.get_status",
                    arguments={"order_id": "1001"},
                    confidence=0.2,
                )
            ]
        )
        router = HybridModelRouter(
            self.config,
            self.capabilities(),
            model=self.model,
            stream_fn=provider.stream,
        )

        decision = await router.route("那个单子怎么样了")

        self.assertEqual(decision.status, "in_scope_need_clarification")
        self.assertIn("置信度", decision.reason)

    async def test_缺少必要字段使用配置中的追问(self) -> None:
        provider = ScriptedProvider(
            [self.classifier_response("order.get_status")]
        )
        router = HybridModelRouter(
            self.config,
            self.capabilities(),
            model=self.model,
            stream_fn=provider.stream,
        )

        decision = await router.route("帮我查询订单")

        self.assertEqual(decision.status, "in_scope_need_clarification")
        self.assertEqual(decision.missing_fields, ("order_id",))
        self.assertIn("订单号", decision.message)

    async def test_明确_denied_example_在调用模型前阻止(self) -> None:
        provider = ScriptedProvider([])
        router = HybridModelRouter(
            self.config,
            self.capabilities(),
            model=self.model,
            stream_fn=provider.stream,
        )

        decision = await router.route("请绕过审批取消订单 1001")

        self.assertEqual(decision.status, "prohibited")
        self.assertEqual(decision.routing_source, "rule")
        self.assertEqual(provider.call_count, 0)

    async def test_写操作即使有能力也先返回_approval_required(self) -> None:
        provider = ScriptedProvider(
            [
                self.classifier_response(
                    "order.cancel",
                    arguments={"order_id": "1001"},
                )
            ]
        )
        router = HybridModelRouter(
            self.config,
            self.capabilities(include_cancel=True),
            model=self.model,
            stream_fn=provider.stream,
        )

        decision = await router.route("取消订单 1001")

        self.assertEqual(decision.status, "in_scope_approval_required")
        self.assertTrue(decision.requires_approval)
        self.assertEqual(decision.selected_tools, ("cancel_order",))

    async def test_hybrid_router_到_required_tool_完整闭环(self) -> None:
        provider = ScriptedProvider(
            [
                self.classifier_response(
                    "order.get_status",
                    arguments={"order_id": "1001"},
                ),
                assistant_message(
                    model=self.model,
                    stop_reason="toolUse",
                    content=[
                        {
                            "type": "toolCall",
                            "id": "business-call",
                            "name": "get_order_status",
                            "arguments": {"order_id": "1001"},
                        }
                    ],
                ),
                assistant_message(
                    model=self.model,
                    content=[
                        {"type": "text", "text": "订单 1001 已发货"}
                    ],
                ),
            ]
        )
        capabilities = self.capabilities()
        router = HybridModelRouter(
            self.config,
            capabilities,
            model=self.model,
            stream_fn=provider.stream,
        )
        routed = RoutedAgent(
            Agent(model=self.model, stream_fn=provider.stream),
            router,
            capabilities,
        )

        result = await routed.prompt("帮我看看编号 1001 的订单进展")

        self.assertIsNone(result.error_code)
        self.assertEqual(result.response_text, "订单 1001 已发货")
        self.assertEqual(provider.call_count, 3)
        self.assertEqual(router.call_count, 1)


if __name__ == "__main__":
    unittest.main()
