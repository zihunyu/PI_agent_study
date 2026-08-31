"""Plan 参数合同在规划、持久化、恢复和派发边界的回归测试。"""

from __future__ import annotations

import unittest

from pi_agent_loop import CancellationToken, Model
from pi_agent_loop.harness import agent_configuration_hash
from pi_agent_loop.planning import (
    HybridRequestPlanner,
    IntentPlanPolicy,
    MultiIntentPlan,
    PlanExecutor,
    PlanParameterContract,
    PlanStep,
    PlanValidationError,
    PlanValidator,
)
from pi_agent_loop.routing import RequestDecision, TaskDecision


class PlanParameterContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.contract = PlanParameterContract(
            required={"resource_id": "string"},
            optional={"quantity": ("integer", "null")},
        )
        self.policy = IntentPlanPolicy(
            "resource.read",
            capabilities=("resources.read",),
            parameter_contract=self.contract,
        )

    def test合同可持久化可哈希且拒绝不可声明类型(self) -> None:
        restored = PlanParameterContract.from_dict(self.contract.to_dict())
        self.assertEqual(restored, self.contract)
        self.assertEqual(restored.contract_hash, self.contract.contract_hash)
        self.assertEqual(hash(restored), hash(self.contract))

        policy = IntentPlanPolicy.from_dict(self.policy.to_dict())
        self.assertEqual(policy, self.policy)

        with self.assertRaisesRegex(PlanValidationError, "类型声明"):
            PlanParameterContract(required={"resource_id": lambda: None})  # type: ignore[dict-item]

    async def test自由planner对缺参未知参数和类型错误全部fail_closed(self) -> None:
        invalid_arguments = (
            ({}, "缺少必填参数"),
            ({"resource_id": "r-1", "extra": True}, "合同之外"),
            ({"resource_id": 123}, "类型错误"),
            ({"resource_id": "r-1", "quantity": True}, "类型错误"),
        )
        for arguments, message in invalid_arguments:
            with self.subTest(arguments=arguments):
                planner = HybridRequestPlanner(
                    {self.policy.intent: self.policy},
                    lambda _request, _catalog, arguments=arguments: {
                        "steps": [
                            {
                                "stepId": "read",
                                "intent": "resource.read",
                                "arguments": arguments,
                            }
                        ]
                    },
                )
                with self.assertRaisesRegex(PlanValidationError, message):
                    await planner.plan("读取资源")

    async def test_task_decision映射同样执行可信参数合同(self) -> None:
        secondary = IntentPlanPolicy("response.explain")
        planner = HybridRequestPlanner(
            {
                self.policy.intent: self.policy,
                secondary.intent: secondary,
            },
            lambda *_args: None,
        )
        task_decision = TaskDecision(
            components=(
                RequestDecision(
                    status="in_scope_tool_ready",
                    reason="读取",
                    message="读取",
                    intent=self.policy.intent,
                    extracted_fields={"resource_id": 7},
                    task_id="read",
                ),
                RequestDecision(
                    status="in_scope_no_tool",
                    reason="解释",
                    message="解释",
                    intent=secondary.intent,
                    task_id="explain",
                ),
            )
        )

        with self.assertRaisesRegex(PlanValidationError, "类型错误"):
            await planner.plan("读取并解释", task_decision=task_decision)

    def test危险空参数默认拒绝但可由可信合同显式声明(self) -> None:
        legacy_policy = IntentPlanPolicy(
            "resource.write",
            requires_approval=True,
            write=True,
            replay_policy="never",
            approval_roles=("manager",),
        )
        legacy_step = PlanStep(
            "write",
            legacy_policy.intent,
            requires_approval=True,
            write=True,
            replay_policy="never",
            approval_roles=("manager",),
        )
        with self.assertRaisesRegex(PlanValidationError, "空参数"):
            PlanValidator({legacy_policy.intent: legacy_policy}).validate(
                MultiIntentPlan("更新", (legacy_step,))
            )

        empty_contract = PlanParameterContract(allow_empty=True)
        explicit_policy = IntentPlanPolicy(
            "resource.write",
            requires_approval=True,
            write=True,
            replay_policy="never",
            approval_roles=("manager",),
            parameter_contract=empty_contract,
        )
        explicit_step = PlanStep(
            "write",
            explicit_policy.intent,
            requires_approval=True,
            write=True,
            replay_policy="never",
            approval_roles=("manager",),
            parameter_contract=empty_contract,
        )
        graph = PlanValidator({explicit_policy.intent: explicit_policy}).validate(
            MultiIntentPlan("更新", (explicit_step,))
        )
        self.assertEqual(graph.topological_order, ("write",))

        # 未配置合同的旧只读策略继续兼容。
        read = PlanStep("read", "legacy.read", arguments={"custom": [1]})
        PlanValidator({"legacy.read": IntentPlanPolicy("legacy.read")}).validate(
            MultiIntentPlan("读取", (read,))
        )

    def test持久plan和action_hash绑定参数合同(self) -> None:
        step = PlanStep(
            "read",
            self.policy.intent,
            arguments={"resource_id": "r-1"},
            capabilities=self.policy.capabilities,
            parameter_contract=self.contract,
        )
        changed = PlanStep(
            "read",
            self.policy.intent,
            arguments={"resource_id": "r-1"},
            capabilities=self.policy.capabilities,
            parameter_contract=PlanParameterContract(
                required={"resource_id": ("string", "null")}
            ),
        )
        self.assertNotEqual(step.action_hash, changed.action_hash)

        raw = MultiIntentPlan("读取", (step,), plan_id="contract-plan").to_dict()
        restored = MultiIntentPlan.from_dict(raw)
        self.assertEqual(restored.step("read").parameter_contract, self.contract)
        raw["steps"][0]["parameterContract"]["required"]["resource_id"] = "integer"
        with self.assertRaisesRegex(PlanValidationError, "Action Hash"):
            MultiIntentPlan.from_dict(raw)

    async def test_executor在派发和恢复前再次验证参数(self) -> None:
        step = PlanStep(
            "read",
            self.policy.intent,
            arguments={"resource_id": "r-1"},
            capabilities=self.policy.capabilities,
            parameter_contract=self.contract,
        )
        plan = MultiIntentPlan("读取", (step,))
        calls = 0

        async def execute(_step, _token: CancellationToken):
            nonlocal calls
            calls += 1
            return "unexpected"

        executor = PlanExecutor(plan, {self.policy.intent: self.policy}, execute)
        mutable_arguments = step.arguments
        self.assertIsInstance(mutable_arguments, dict)
        mutable_arguments["resource_id"] = 9  # type: ignore[index]

        with self.assertRaisesRegex(PlanValidationError, "类型错误"):
            await executor.execute()
        self.assertEqual(calls, 0)

    def test参数合同变化会改变session配置hash(self) -> None:
        model = Model(id="model", provider="test")
        baseline = agent_configuration_hash(
            model=model,
            system_prompt="test",
            tools=[],
            plan_policies={self.policy.intent: self.policy},
        )
        changed_policy = IntentPlanPolicy(
            self.policy.intent,
            capabilities=self.policy.capabilities,
            parameter_contract=PlanParameterContract(
                required={"resource_id": "integer"}
            ),
        )
        changed = agent_configuration_hash(
            model=model,
            system_prompt="test",
            tools=[],
            plan_policies={changed_policy.intent: changed_policy},
        )
        self.assertNotEqual(baseline, changed)


if __name__ == "__main__":
    unittest.main()
