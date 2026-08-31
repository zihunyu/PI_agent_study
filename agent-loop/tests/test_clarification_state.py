"""Tenant/session-scoped clarification continuation integration tests."""

from __future__ import annotations

import unittest

from pi_agent_loop import (
    Agent,
    AgentTool,
    AgentToolResult,
    CapabilityRegistry,
    HybridModelRouter,
    InMemoryClarificationStateStore,
    Model,
    RoutedAgent,
    ScriptedProvider,
    SimpleBusinessConfig,
    SimpleIntent,
    SimpleProduct,
    assistant_message,
)


class ClarificationStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.model = Model(id="clarification-model", provider="fake", api="fake")
        self.config = SimpleBusinessConfig(
            product=SimpleProduct("订单助手", "查询租户订单", False),
            intents=(
                SimpleIntent(
                    id="order.get_status",
                    name="查询订单",
                    description="按账户和订单号查询订单",
                    examples=("查询订单",),
                    required_fields=("account_id", "order_id"),
                    capability="orders.read",
                    must_use_tool=True,
                    requires_approval=False,
                    ask_when_missing="请补充账户号和订单号。",
                ),
            ),
        )

    def _classification(self, arguments: dict) -> dict:
        return assistant_message(
            model=self.model,
            stop_reason="toolUse",
            content=[
                {
                    "type": "toolCall",
                    "id": "route-call",
                    "name": "select_business_intent",
                    "arguments": {
                        "decision": "order.get_status",
                        "arguments": arguments,
                        "confidence": 0.99,
                        "reason": "测试澄清续接",
                    },
                }
            ],
        )

    def _capabilities(self) -> CapabilityRegistry:
        async def execute(_call_id, arguments, _token, _update):
            return AgentToolResult(
                content=[
                    {
                        "type": "text",
                        "text": (
                            f"{arguments['account_id']}/"
                            f"{arguments['order_id']} 已发货"
                        ),
                    }
                ],
                details={},
            )

        tool = AgentTool(
            name="get_order_status",
            label="查询订单",
            description="查询真实订单状态",
            parameters={
                "type": "object",
                "properties": {
                    "account_id": {"type": "string"},
                    "order_id": {"type": "string"},
                },
                "required": ["account_id", "order_id"],
            },
            validate_args=lambda value: value,
            execute=execute,
        )
        registry = CapabilityRegistry()
        registry.register(
            tool,
            capabilities={"orders.read"},
            domain="orders",
        )
        return registry

    async def test_routed_agent两轮短答恢复意图和历史槽位(self) -> None:
        provider = ScriptedProvider(
            [
                self._classification({"account_id": "acct-a"}),
                self._classification({"order_id": "1001"}),
                assistant_message(
                    model=self.model,
                    stop_reason="toolUse",
                    content=[
                        {
                            "type": "toolCall",
                            "id": "business-call",
                            "name": "get_order_status",
                            "arguments": {
                                "account_id": "acct-a",
                                "order_id": "1001",
                            },
                        }
                    ],
                ),
                assistant_message(
                    model=self.model,
                    content=[{"type": "text", "text": "订单已发货"}],
                ),
            ]
        )
        store = InMemoryClarificationStateStore()
        capabilities = self._capabilities()
        router = HybridModelRouter(
            self.config,
            capabilities,
            model=self.model,
            stream_fn=provider.stream,
            clarification_store=store,
        )
        routed = RoutedAgent(
            Agent(
                model=self.model,
                stream_fn=provider.stream,
                tenant_id="tenant-a",
            ),
            router,
            capabilities,
            session_id="session-a",
            tenant_id="tenant-a",
        )

        first = await routed.prompt("查询 acct-a 的订单")
        pending = await store.load("tenant-a", "session-a")
        second = await routed.prompt("1001")

        self.assertFalse(first.model_called)
        self.assertEqual(first.decision.missing_fields, ("order_id",))
        assert pending is not None
        self.assertEqual(pending.pending_intent, "order.get_status")
        self.assertEqual(pending.extracted_fields, {"account_id": "acct-a"})
        self.assertEqual(second.decision.status, "in_scope_tool_ready")
        self.assertEqual(
            second.decision.extracted_fields,
            {"account_id": "acct-a", "order_id": "1001"},
        )
        self.assertEqual(second.response_text, "订单已发货")
        self.assertIn(
            '"pendingClarification"',
            provider.contexts[1]["systemPrompt"],
        )
        self.assertIsNone(await store.load("tenant-a", "session-a"))

    async def test_tenant和session共同隔离pending_state(self) -> None:
        provider = ScriptedProvider(
            [
                self._classification({"account_id": "same-tenant-session-a"}),
                self._classification({"account_id": "same-tenant-session-b"}),
                self._classification({"account_id": "other-tenant-session-a"}),
            ]
        )
        store = InMemoryClarificationStateStore()
        router = HybridModelRouter(
            self.config,
            self._capabilities(),
            model=self.model,
            stream_fn=provider.stream,
            clarification_store=store,
        )

        await router.route("查询订单", tenant_id="t1", session_id="s1")
        await router.route("查询订单", tenant_id="t1", session_id="s2")
        await router.route("查询订单", tenant_id="t2", session_id="s1")

        state_t1s1 = await store.load("t1", "s1")
        state_t1s2 = await store.load("t1", "s2")
        state_t2s1 = await store.load("t2", "s1")
        assert state_t1s1 is not None
        assert state_t1s2 is not None
        assert state_t2s1 is not None
        self.assertEqual(
            state_t1s1.extracted_fields["account_id"],
            "same-tenant-session-a",
        )
        self.assertEqual(
            state_t1s2.extracted_fields["account_id"],
            "same-tenant-session-b",
        )
        self.assertEqual(
            state_t2s1.extracted_fields["account_id"],
            "other-tenant-session-a",
        )

    async def test_pending_state过期后不再注入或合并(self) -> None:
        now = [100.0]

        def clock() -> float:
            return now[0]

        provider = ScriptedProvider(
            [
                self._classification({"account_id": "expired-account"}),
                self._classification({"order_id": "1001"}),
            ]
        )
        store = InMemoryClarificationStateStore(clock=clock)
        router = HybridModelRouter(
            self.config,
            self._capabilities(),
            model=self.model,
            stream_fn=provider.stream,
            clarification_store=store,
            clarification_ttl_seconds=5,
            clarification_clock=clock,
        )

        first = await router.route("查询订单", tenant_id="t1", session_id="s1")
        now[0] = 106.0
        second = await router.route("1001", tenant_id="t1", session_id="s1")

        self.assertEqual(first.missing_fields, ("order_id",))
        self.assertEqual(second.status, "in_scope_need_clarification")
        self.assertEqual(second.missing_fields, ("account_id",))
        self.assertNotIn(
            '"pendingClarification"',
            provider.contexts[1]["systemPrompt"],
        )

    async def test启用store却没有可信作用域时fail_closed(self) -> None:
        provider = ScriptedProvider([])
        router = HybridModelRouter(
            self.config,
            self._capabilities(),
            model=self.model,
            stream_fn=provider.stream,
            clarification_store=InMemoryClarificationStateStore(),
        )

        decision = await router.route("查询订单")

        self.assertEqual(decision.status, "in_scope_need_clarification")
        self.assertIn("tenant_id/session_id", decision.reason)
        self.assertEqual(provider.call_count, 0)


if __name__ == "__main__":
    unittest.main()
