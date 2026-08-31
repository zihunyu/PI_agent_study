"""通用 Router 安全合同：审批并集与 JSON 参数。"""

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
    RouteAuthorizationDecision,
    RoutedAgent,
    ScriptedProvider,
    SimpleBusinessConfig,
    SimpleDeniedRule,
    SimpleIntent,
    SimpleProduct,
    TaskDecision,
    assistant_message,
)


async def _execute(_call_id, _arguments, _cancellation, _update):
    return AgentToolResult(content=[{"type": "text", "text": "ok"}], details={})


def _tool(name: str) -> AgentTool:
    return AgentTool(
        name=name,
        label=name,
        description="测试工具",
        parameters={"type": "object", "additionalProperties": False},
        execute=_execute,
    )


class RouterSecurityContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.model = Model(id="router-security", provider="fake", api="fake")
        self.config = SimpleBusinessConfig(
            product=SimpleProduct("通用资源助手", "测试通用路由合同", False),
            intents=(
                SimpleIntent(
                    id="resource.change",
                    name="变更资源",
                    description="提交结构化资源变更",
                    examples=("变更资源",),
                    required_fields=("payload",),
                    capability="resources.change",
                    must_use_tool=True,
                    requires_approval=False,
                    ask_when_missing="请提供 payload",
                ),
            ),
        )

    def _provider(
        self,
        arguments: dict,
        *,
        decision: str = "resource.change",
    ) -> ScriptedProvider:
        return ScriptedProvider(
            [
                assistant_message(
                    model=self.model,
                    stop_reason="toolUse",
                    content=[
                        {
                            "type": "toolCall",
                            "id": "route-1",
                            "name": "select_business_intent",
                            "arguments": {
                                "decision": decision,
                                "arguments": arguments,
                                "confidence": 0.99,
                                "reason": "测试",
                            },
                        }
                    ],
                )
            ]
        )

    async def test_tool元数据要求审批时intent不能关闭审批(self) -> None:
        capabilities = CapabilityRegistry()
        capabilities.register(
            _tool("preferred_without_approval"),
            capabilities={"resources.change"},
            domain="resources",
            operation="write",
            requires_approval=False,
            priority=10,
        )
        capabilities.register(
            _tool("fallback_requires_approval"),
            capabilities={"resources.change"},
            domain="resources",
            operation="write",
            requires_approval=True,
            priority=0,
        )
        router = HybridModelRouter(
            self.config,
            capabilities,
            model=self.model,
            stream_fn=self._provider({"payload": {"enabled": True}}).stream,
        )

        decision = await router.route("启用资源")

        self.assertEqual(decision.selected_tools, ("preferred_without_approval",))
        self.assertEqual(decision.status, "in_scope_approval_required")
        self.assertTrue(decision.requires_approval)

    async def test_router保留严格json参数而不强制转成字符串(self) -> None:
        capabilities = CapabilityRegistry()
        capabilities.register(
            _tool("change_resource"),
            capabilities={"resources.change"},
            domain="resources",
        )
        payload = {
            "enabled": True,
            "replicas": 3,
            "labels": ["blue", "safe"],
            "limits": {"cpu": 1.5, "memory": None},
        }
        provider = self._provider({"payload": payload})
        router = HybridModelRouter(
            self.config,
            capabilities,
            model=self.model,
            stream_fn=provider.stream,
        )

        decision = await router.route("按给出的结构更新资源")

        self.assertEqual(decision.status, "in_scope_tool_ready")
        self.assertEqual(decision.extracted_fields["payload"], payload)
        argument_schema = provider.contexts[0]["tools"][0]["parameters"][
            "properties"
        ]["arguments"]["additionalProperties"]
        self.assertIn("anyOf", argument_schema)

    async def test_模型路由后由可信权限策略拒绝且收到参数深拷贝(self) -> None:
        capabilities = CapabilityRegistry()
        capabilities.register(
            _tool("change_resource"),
            capabilities={"resources.change"},
            domain="resources",
        )
        provider = self._provider({"payload": {"enabled": True}})
        observed = []

        async def authorize(context):
            observed.append(context)
            # 授权器拿到的是隔离快照，错误修改不能篡改 Router 决策。
            context.arguments["payload"]["enabled"] = False
            return RouteAuthorizationDecision.deny(
                "role_missing",
                "当前角色无权变更资源。",
            )

        router = HybridModelRouter(
            self.config,
            capabilities,
            model=self.model,
            stream_fn=provider.stream,
            authorization_policy=authorize,
        )

        decision = await router.route("启用资源")

        self.assertEqual(decision.status, "permission_denied")
        self.assertEqual(decision.selected_tools, ())
        self.assertEqual(decision.tool_policy.mode, "none")
        self.assertEqual(
            decision.extracted_fields,
            {"payload": {"enabled": True}},
        )
        self.assertEqual(observed[0].intent, "resource.change")
        self.assertEqual(
            observed[0].required_capabilities,
            ("resources.change",),
        )
        self.assertEqual(observed[0].selected_tools, ("change_resource",))

    async def test_规则禁止优先且不调用权限策略或分类模型(self) -> None:
        config = SimpleBusinessConfig(
            product=SimpleProduct("安全助手", "规则优先", False),
            intents=self.config.intents,
            denied=(
                SimpleDeniedRule(
                    "导出密钥",
                    "禁止导出全部密钥",
                    ("导出全部密钥",),
                    "该操作已被安全规则阻止。",
                ),
            ),
        )
        provider = ScriptedProvider([])
        authorization_calls = 0

        def authorize(_context):
            nonlocal authorization_calls
            authorization_calls += 1
            return RouteAuthorizationDecision.allow()

        router = HybridModelRouter(
            config,
            CapabilityRegistry(),
            model=self.model,
            stream_fn=provider.stream,
            authorization_policy=authorize,
        )

        decision = await router.route("请导出全部密钥")

        self.assertEqual(decision.status, "prohibited")
        self.assertEqual(provider.call_count, 0)
        self.assertEqual(authorization_calls, 0)

    async def test_权限策略异常时fail_closed而不是继续执行(self) -> None:
        capabilities = CapabilityRegistry()
        capabilities.register(
            _tool("change_resource"),
            capabilities={"resources.change"},
            domain="resources",
        )
        provider = self._provider({"payload": {"enabled": True}})

        def unavailable(_context):
            raise TimeoutError("identity service timeout")

        router = HybridModelRouter(
            self.config,
            capabilities,
            model=self.model,
            stream_fn=provider.stream,
            authorization_policy=unavailable,
        )

        decision = await router.route("启用资源")

        self.assertEqual(decision.status, "permission_denied")
        self.assertEqual(decision.reason, "authorization_policy_unavailable")
        self.assertEqual(decision.selected_tools, ())

    async def test_复合任务任一组件越权则整批拒绝且不允许部分规划(self) -> None:
        config = SimpleBusinessConfig(
            product=SimpleProduct("资源助手", "复合资源操作", False),
            intents=(
                SimpleIntent(
                    "resource.read",
                    "读取资源",
                    "读取资源",
                    ("读取",),
                    (),
                    "resources.read",
                    True,
                    False,
                    "无需参数",
                ),
                self.config.intents[0],
            ),
        )
        capabilities = CapabilityRegistry()
        capabilities.register(
            _tool("read_resource"),
            capabilities={"resources.read"},
            domain="resources",
        )
        capabilities.register(
            _tool("change_resource"),
            capabilities={"resources.change"},
            domain="resources",
        )
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=self.model,
                    stop_reason="toolUse",
                    content=[
                        {
                            "type": "toolCall",
                            "id": "route-batch",
                            "name": "select_business_intent",
                            "arguments": {
                                "decisions": [
                                    {
                                        "taskId": "read",
                                        "dependsOn": [],
                                        "decision": "resource.read",
                                        "arguments": {},
                                        "confidence": 0.99,
                                        "reason": "读取",
                                    },
                                    {
                                        "taskId": "change",
                                        "dependsOn": ["read"],
                                        "decision": "resource.change",
                                        "arguments": {
                                            "payload": {"enabled": True}
                                        },
                                        "confidence": 0.99,
                                        "reason": "变更",
                                    },
                                ]
                            },
                        }
                    ],
                )
            ]
        )

        def authorize(context):
            if context.intent == "resource.change":
                return RouteAuthorizationDecision.deny("writer_role_missing")
            return RouteAuthorizationDecision.allow()

        router = HybridModelRouter(
            config,
            capabilities,
            model=self.model,
            stream_fn=provider.stream,
            max_intents=4,
            authorization_policy=authorize,
        )

        decision = await router.route("读取资源后再变更资源")

        self.assertEqual(decision.status, "permission_denied")
        self.assertEqual(decision.selected_tools, ())
        self.assertIsNotNone(decision.task_decision)
        assert decision.task_decision is not None
        self.assertEqual(
            decision.task_decision.permission_denied_tasks,
            ("change",),
        )
        self.assertFalse(decision.task_decision.ready_for_planner)

    async def test_router权限拒绝后routed_agent不调用回答模型和工具(self) -> None:
        tool_calls = 0

        async def execute(_call_id, _arguments, _cancellation, _update):
            nonlocal tool_calls
            tool_calls += 1
            return AgentToolResult(
                content=[{"type": "text", "text": "不应执行"}],
                details={},
            )

        tool = AgentTool(
            name="change_resource",
            label="变更资源",
            description="变更资源",
            parameters={"type": "object"},
            execute=execute,
        )
        capabilities = CapabilityRegistry()
        capabilities.register(
            tool,
            capabilities={"resources.change"},
            domain="resources",
        )
        classifier = self._provider({"payload": {"enabled": True}})
        router = HybridModelRouter(
            self.config,
            capabilities,
            model=self.model,
            stream_fn=classifier.stream,
            authorization_policy=lambda _context: (
                RouteAuthorizationDecision.deny("role_missing")
            ),
        )
        answer_provider = ScriptedProvider([])
        routed = RoutedAgent(
            Agent(model=self.model, stream_fn=answer_provider.stream),
            router,
            capabilities,
        )

        result = await routed.prompt("启用资源")

        self.assertEqual(result.error_code, "permission_denied")
        self.assertFalse(result.model_called)
        self.assertEqual(classifier.call_count, 1)
        self.assertEqual(answer_provider.call_count, 0)
        self.assertEqual(tool_calls, 0)

    async def test_通用router可选择开启多intent只读组合(self) -> None:
        config = SimpleBusinessConfig(
            product=SimpleProduct("运维助手", "组合多个实时只读目标", False),
            intents=(
                SimpleIntent(
                    "service.health",
                    "服务健康",
                    "读取服务健康",
                    ("服务怎么样",),
                    (),
                    "services.read_health",
                    True,
                    False,
                    "无需参数",
                ),
                SimpleIntent(
                    "deployment.list",
                    "部署列表",
                    "读取部署列表",
                    ("有哪些部署",),
                    (),
                    "deployments.read",
                    True,
                    False,
                    "无需参数",
                ),
            ),
        )
        capabilities = CapabilityRegistry()
        capabilities.register(
            _tool("get_service_health"),
            capabilities={"services.read_health"},
            domain="services",
        )
        capabilities.register(
            _tool("list_deployments"),
            capabilities={"deployments.read"},
            domain="deployments",
        )
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=self.model,
                    stop_reason="toolUse",
                    content=[
                        {
                            "type": "toolCall",
                            "id": "route-many",
                            "name": "select_business_intent",
                            "arguments": {
                                "decisions": [
                                    {
                                        "decision": "service.health",
                                        "arguments": {},
                                        "confidence": 0.98,
                                        "reason": "需要健康状态",
                                    },
                                    {
                                        "decision": "deployment.list",
                                        "arguments": {},
                                        "confidence": 0.96,
                                        "reason": "需要部署列表",
                                    },
                                ]
                            },
                        }
                    ],
                )
            ]
        )
        router = HybridModelRouter(
            config,
            capabilities,
            model=self.model,
            stream_fn=provider.stream,
            max_intents=4,
        )

        decision = await router.route("服务健康如何，同时列出部署")

        self.assertEqual(decision.status, "in_scope_tool_ready")
        self.assertEqual(
            decision.required_capabilities,
            ("services.read_health", "deployments.read"),
        )
        self.assertEqual(
            decision.selected_tools,
            ("get_service_health", "list_deployments"),
        )
        self.assertEqual(len(decision.component_decisions), 2)
        self.assertIn(
            "decisions",
            provider.contexts[0]["tools"][0]["parameters"]["properties"],
        )

    async def test_复合写请求输出可交给planner的结构化任务(self) -> None:
        config = SimpleBusinessConfig(
            product=SimpleProduct("采购助手", "查询并创建采购", False),
            intents=(
                SimpleIntent(
                    id="account.balance",
                    name="查询余额",
                    description="查询当前余额",
                    examples=("查余额",),
                    required_fields=(),
                    capability="accounts.read_balance",
                    must_use_tool=True,
                    requires_approval=False,
                    ask_when_missing="无需参数",
                    side_effect=False,
                ),
                SimpleIntent(
                    id="purchase.create",
                    name="创建采购",
                    description="校验库存并创建采购",
                    examples=("购买商品",),
                    required_fields=("product_id",),
                    capability=None,
                    must_use_tool=True,
                    requires_approval=False,
                    ask_when_missing="请提供商品",
                    optional_fields=("note",),
                    capabilities=("inventory.reserve", "purchases.create"),
                    side_effect=True,
                    risk="high",
                ),
            ),
        )
        capabilities = CapabilityRegistry()
        capabilities.register(
            _tool("read_balance"),
            capabilities={"accounts.read_balance"},
            domain="accounts",
            operation="query",
            side_effect=False,
        )
        capabilities.register(
            _tool("reserve_inventory"),
            capabilities={"inventory.reserve"},
            domain="inventory",
            operation="command",
            side_effect=True,
        )
        capabilities.register(
            _tool("create_purchase"),
            capabilities={"purchases.create"},
            domain="purchases",
            operation="command",
            side_effect=True,
            risk="high",
        )
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=self.model,
                    stop_reason="toolUse",
                    content=[
                        {
                            "type": "toolCall",
                            "id": "route-task",
                            "name": "select_business_intent",
                            "arguments": {
                                "decisions": [
                                    {
                                        "taskId": "check_balance",
                                        "dependsOn": [],
                                        "decision": "account.balance",
                                        "arguments": {},
                                        "confidence": 0.99,
                                        "reason": "先查询余额",
                                    },
                                    {
                                        "taskId": "create_purchase",
                                        "dependsOn": ["check_balance"],
                                        "decision": "purchase.create",
                                        "arguments": {
                                            "product_id": "shoe-1",
                                            "note": "送到前台",
                                        },
                                        "confidence": 0.98,
                                        "reason": "余额足够后创建采购",
                                    },
                                ]
                            },
                        }
                    ],
                )
            ]
        )
        router = HybridModelRouter(
            config,
            capabilities,
            model=self.model,
            stream_fn=provider.stream,
            max_intents=4,
        )

        decision = await router.route("查余额，够的话购买鞋子并备注送到前台")

        self.assertEqual(decision.status, "in_scope_plan_required")
        self.assertIsInstance(decision.task_decision, TaskDecision)
        task = decision.task_decision
        assert task is not None
        self.assertTrue(task.ready_for_planner)
        self.assertEqual(
            task.dependencies[0].depends_on,
            ("check_balance",),
        )
        self.assertEqual(task.approval_required_tasks, ("create_purchase",))
        self.assertEqual(task.side_effect_tasks, ("create_purchase",))
        self.assertEqual(task.highest_risk, "high")
        self.assertEqual(
            task.components[1].extracted_fields,
            {"product_id": "shoe-1", "note": "送到前台"},
        )
        self.assertEqual(
            task.components[1].required_capabilities,
            ("inventory.reserve", "purchases.create"),
        )
        item_schema = provider.contexts[0]["tools"][0]["parameters"][
            "properties"
        ]["decisions"]["items"]
        self.assertIn("taskId", item_schema["properties"])
        self.assertIn("dependsOn", item_schema["properties"])

    async def test_显式side_effect覆盖非标准operation且risk可升级审批(self) -> None:
        config = SimpleBusinessConfig(
            product=SimpleProduct("资源助手", "读取资源", False),
            intents=(
                SimpleIntent(
                    id="resource.query",
                    name="查询资源",
                    description="查询资源",
                    examples=("查询",),
                    required_fields=(),
                    capability="resources.query",
                    must_use_tool=True,
                    requires_approval=False,
                    ask_when_missing="无需参数",
                    side_effect=False,
                    risk="medium",
                ),
            ),
        )
        capabilities = CapabilityRegistry()
        capabilities.register(
            _tool("query_resource"),
            capabilities={"resources.query"},
            domain="resources",
            operation="query",
            side_effect=False,
            risk="medium",
        )
        provider = self._provider({}, decision="resource.query")
        router = HybridModelRouter(
            config,
            capabilities,
            model=self.model,
            stream_fn=provider.stream,
        )

        decision = await router.route("查询资源")

        self.assertEqual(decision.status, "in_scope_tool_ready")
        self.assertFalse(decision.side_effect)
        self.assertEqual(decision.risk, "medium")

        high_risk = CapabilityRegistry()
        high_risk.register(
            _tool("read_sensitive_resource"),
            capabilities={"resources.query"},
            domain="resources",
            operation="read",
            side_effect=False,
            risk="high",
        )
        high_provider = self._provider({}, decision="resource.query")
        high_router = HybridModelRouter(
            config,
            high_risk,
            model=self.model,
            stream_fn=high_provider.stream,
        )

        high_decision = await high_router.route("读取敏感资源")

        self.assertEqual(high_decision.status, "in_scope_approval_required")
        self.assertEqual(high_decision.risk, "high")

    async def test_复合任务拒绝循环依赖而不交给planner(self) -> None:
        config = SimpleBusinessConfig(
            product=SimpleProduct("运维助手", "组合查询", False),
            intents=(
                SimpleIntent(
                    "service.health",
                    "服务健康",
                    "读取健康",
                    ("健康",),
                    (),
                    "services.health",
                    True,
                    False,
                    "无需参数",
                ),
                SimpleIntent(
                    "deployment.list",
                    "部署列表",
                    "读取部署",
                    ("部署",),
                    (),
                    "deployments.read",
                    True,
                    False,
                    "无需参数",
                ),
            ),
        )
        provider = ScriptedProvider(
            [
                assistant_message(
                    model=self.model,
                    stop_reason="toolUse",
                    content=[
                        {
                            "type": "toolCall",
                            "id": "route-cycle",
                            "name": "select_business_intent",
                            "arguments": {
                                "decisions": [
                                    {
                                        "taskId": "a",
                                        "dependsOn": ["b"],
                                        "decision": "service.health",
                                        "arguments": {},
                                        "confidence": 0.99,
                                        "reason": "a",
                                    },
                                    {
                                        "taskId": "b",
                                        "dependsOn": ["a"],
                                        "decision": "deployment.list",
                                        "arguments": {},
                                        "confidence": 0.99,
                                        "reason": "b",
                                    },
                                ]
                            },
                        }
                    ],
                )
            ]
        )
        router = HybridModelRouter(
            config,
            CapabilityRegistry(),
            model=self.model,
            stream_fn=provider.stream,
            max_intents=2,
        )

        decision = await router.route("查健康后列部署")

        self.assertEqual(decision.status, "in_scope_need_clarification")
        self.assertIn("循环", decision.reason)


if __name__ == "__main__":
    unittest.main()
