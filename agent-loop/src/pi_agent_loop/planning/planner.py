"""受可信 Intent Catalog 约束的 HybridRequestPlanner。"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast
from uuid import uuid4

from ..routing.types import TaskDecision
from .graph import PlanValidator
from .types import IntentPlanPolicy, MultiIntentPlan, PlanStep, PlanValidationError


PlannerFn = Callable[[str, tuple[dict[str, Any], ...]], Any]
RulePlannerFn = Callable[[str], Any]
ReplannerFn = Callable[
    [
        str,
        dict[str, Any],
        dict[str, Any],
        tuple[str, ...],
        tuple[dict[str, Any], ...],
    ],
    Any,
]


class HybridRequestPlanner:
    """规则优先、结构化 Planner 回退；模型不能修改可信策略。"""

    def __init__(
        self,
        policies: Mapping[str, IntentPlanPolicy],
        planner_fn: PlannerFn,
        *,
        rule_planner: RulePlannerFn | None = None,
        replanner_fn: ReplannerFn | None = None,
    ) -> None:
        self.policies = dict(policies)
        self.planner_fn = planner_fn
        self.rule_planner = rule_planner
        self.replanner_fn = replanner_fn
        self.validator = PlanValidator(self.policies)

    async def plan(
        self,
        request: str,
        *,
        task_decision: TaskDecision | None = None,
    ) -> MultiIntentPlan:
        if not isinstance(request, str) or not request.strip():
            raise PlanValidationError("规划请求不能为空")
        if task_decision is not None:
            return self.plan_from_task_decision(request, task_decision)
        raw: Any = None
        if self.rule_planner is not None:
            raw = self.rule_planner(request)
            raw = await cast(Awaitable[Any], raw) if inspect.isawaitable(raw) else raw
        if raw is None:
            catalog = self._catalog()
            raw = self.planner_fn(request, catalog)
            raw = await cast(Awaitable[Any], raw) if inspect.isawaitable(raw) else raw
        plan = raw if isinstance(raw, MultiIntentPlan) else self._parse(request, raw)
        if plan.request != request:
            raise PlanValidationError("Planner 不得替换原始用户请求")
        return self.validator.bind_trusted_dependencies(plan)

    def plan_from_task_decision(
        self,
        request: str,
        task_decision: TaskDecision,
    ) -> MultiIntentPlan:
        """Convert a Router decision to an executable, policy-bound plan.

        Router arguments and dependency hints remain untrusted.  The executable
        approval, write, replay and capability fields are always copied from the
        application-owned ``IntentPlanPolicy`` catalogue, then the complete DAG
        is checked by ``PlanValidator``.
        """

        if not isinstance(request, str) or not request.strip():
            raise PlanValidationError("规划请求不能为空")
        if not isinstance(task_decision, TaskDecision):
            raise TypeError("task_decision 必须是 TaskDecision")
        if not task_decision.ready_for_planner:
            raise PlanValidationError("Task Decision 尚有缺失参数或能力，不能执行")
        steps: list[PlanStep] = []
        for component in task_decision.components:
            if component.intent is None or component.task_id is None:
                raise PlanValidationError("Task Decision 缺少可信 Intent 或 task_id")
            if component.status not in {
                "in_scope_no_tool",
                "in_scope_tool_ready",
                "in_scope_approval_required",
            }:
                raise PlanValidationError(
                    f"Task {component.task_id} 尚不可执行：{component.status}"
                )
            policy = self.policies.get(component.intent)
            if policy is None:
                raise PlanValidationError(
                    f"Task Decision 选择了未配置 Intent：{component.intent}"
                )
            steps.append(
                PlanStep(
                    step_id=component.task_id,
                    intent=component.intent,
                    arguments=dict(component.extracted_fields),
                    depends_on=component.depends_on,
                    requires_approval=policy.requires_approval,
                    write=policy.write,
                    replay_policy=policy.replay_policy,
                    capabilities=policy.capabilities,
                    approval_roles=policy.approval_roles,
                    parameter_contract=policy.parameter_contract,
                    required_predecessor_intents=policy.required_predecessor_intents,
                    allow_parallel_side_effects=policy.allow_parallel_side_effects,
                    result_contract=policy.result_contract,
                )
            )
        plan = MultiIntentPlan(request=request, steps=tuple(steps))
        return self.validator.bind_trusted_dependencies(plan)

    async def replan(
        self,
        request: str,
        previous_plan: MultiIntentPlan,
        previous_result: Mapping[str, Any],
        issues: tuple[str, ...],
    ) -> MultiIntentPlan | None:
        """Build one bounded correction plan through a separately trusted hook.

        Replanning is deliberately opt-in.  Reusing the original planner without
        the observed result would commonly produce the same failed plan and can
        accidentally repeat side effects.  The returned plan is parsed through
        the same trusted policy catalogue and graph validator as the first plan.
        """

        if self.replanner_fn is None:
            return None
        if not isinstance(request, str) or not request.strip():
            raise PlanValidationError("重规划请求不能为空")
        if previous_plan.request != request:
            raise PlanValidationError("待纠正 Plan 与原始请求不匹配")
        if not issues or any(
            not isinstance(issue, str) or not issue.strip() for issue in issues
        ):
            raise PlanValidationError("重规划必须提供非空校验问题")
        raw = self.replanner_fn(
            request,
            previous_plan.to_dict(),
            dict(previous_result),
            tuple(issues),
            self._catalog(),
        )
        raw = await cast(Awaitable[Any], raw) if inspect.isawaitable(raw) else raw
        if raw is None:
            return None
        plan = raw if isinstance(raw, MultiIntentPlan) else self._parse(request, raw)
        if plan.request != request:
            raise PlanValidationError("Replanner 不得替换原始用户请求")
        if plan.plan_id == previous_plan.plan_id:
            raise PlanValidationError("Correction Plan 必须使用新的 planId")
        return self.validator.bind_trusted_dependencies(plan)

    def _catalog(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "intent": policy.intent,
                "requiresApproval": policy.requires_approval,
                "write": policy.write,
                "replayPolicy": policy.replay_policy,
                "capabilities": list(policy.capabilities),
                "approvalRoles": list(policy.approval_roles),
                "parameterContract": (
                    None
                    if policy.parameter_contract is None
                    else policy.parameter_contract.to_dict()
                ),
                "requiredPredecessorIntents": list(
                    policy.required_predecessor_intents
                ),
                "argumentBindings": [
                    item.to_dict() for item in policy.argument_bindings
                ],
                "preconditions": [
                    item.to_dict() for item in policy.preconditions
                ],
                "allowParallelSideEffects": policy.allow_parallel_side_effects,
                "resultContract": (
                    None
                    if policy.result_contract is None
                    else policy.result_contract.to_dict()
                ),
            }
            for policy in self.policies.values()
        )

    def _parse(self, request: str, raw: Any) -> MultiIntentPlan:
        if not isinstance(raw, Mapping):
            raise PlanValidationError("Planner 必须返回对象或 MultiIntentPlan")
        if set(raw) - {"planId", "steps"}:
            raise PlanValidationError("Planner 返回了未知字段")
        raw_steps = raw.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise PlanValidationError("Planner steps 必须是非空数组")
        steps: list[PlanStep] = []
        for index, value in enumerate(raw_steps, start=1):
            if not isinstance(value, Mapping):
                raise PlanValidationError(f"Planner Step #{index} 必须是对象")
            allowed = {
                "stepId",
                "intent",
                "arguments",
                "dependsOn",
                "requiresApproval",
                "write",
                "replayPolicy",
                "capabilities",
                "approvalRoles",
                "parameterContract",
                "requiredPredecessorIntents",
                "argumentBindings",
                "preconditions",
                "allowParallelSideEffects",
                "resultContract",
            }
            unknown = set(value) - allowed
            if unknown:
                raise PlanValidationError(
                    f"Planner Step #{index} 包含未知字段：{sorted(unknown)}"
                )
            step_id = _required_text(value, "stepId")
            intent = _required_text(value, "intent")
            policy = self.policies.get(intent)
            if policy is None:
                raise PlanValidationError(f"Planner 选择了未配置 Intent：{intent}")
            _assert_optional_policy(value, "requiresApproval", policy.requires_approval)
            _assert_optional_policy(value, "write", policy.write)
            _assert_optional_policy(value, "replayPolicy", policy.replay_policy)
            _assert_optional_policy(value, "capabilities", list(policy.capabilities))
            _assert_optional_policy(value, "approvalRoles", list(policy.approval_roles))
            _assert_optional_policy(
                value,
                "parameterContract",
                (
                    None
                    if policy.parameter_contract is None
                    else policy.parameter_contract.to_dict()
                ),
            )
            _assert_optional_policy(
                value,
                "requiredPredecessorIntents",
                list(policy.required_predecessor_intents),
            )
            _assert_optional_policy(
                value,
                "argumentBindings",
                [item.to_dict() for item in policy.argument_bindings],
            )
            _assert_optional_policy(
                value,
                "preconditions",
                [item.to_dict() for item in policy.preconditions],
            )
            _assert_optional_policy(
                value,
                "allowParallelSideEffects",
                policy.allow_parallel_side_effects,
            )
            _assert_optional_policy(
                value,
                "resultContract",
                (
                    None
                    if policy.result_contract is None
                    else policy.result_contract.to_dict()
                ),
            )
            arguments = value.get("arguments", {})
            if not isinstance(arguments, Mapping):
                raise PlanValidationError("Planner Step arguments 必须是对象")
            depends_on = value.get("dependsOn", [])
            if not isinstance(depends_on, list) or any(
                not isinstance(item, str) or not item.strip() for item in depends_on
            ):
                raise PlanValidationError("Planner Step dependsOn 必须是字符串数组")
            steps.append(
                PlanStep(
                    step_id=step_id,
                    intent=intent,
                    arguments=dict(arguments),
                    depends_on=tuple(depends_on),
                    requires_approval=policy.requires_approval,
                    write=policy.write,
                    replay_policy=policy.replay_policy,
                    capabilities=policy.capabilities,
                    approval_roles=policy.approval_roles,
                    parameter_contract=policy.parameter_contract,
                    required_predecessor_intents=policy.required_predecessor_intents,
                    allow_parallel_side_effects=policy.allow_parallel_side_effects,
                    result_contract=policy.result_contract,
                )
            )
        plan_id = raw.get("planId", str(uuid4()))
        if not isinstance(plan_id, str) or not plan_id.strip():
            raise PlanValidationError("planId 必须是非空字符串")
        return MultiIntentPlan(request=request, steps=tuple(steps), plan_id=plan_id)


def _required_text(value: Mapping[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise PlanValidationError(f"Planner {key} 必须是非空字符串")
    return result


def _assert_optional_policy(
    value: Mapping[str, Any], key: str, trusted_value: Any
) -> None:
    if key in value and value[key] != trusted_value:
        raise PlanValidationError(f"Planner 试图覆盖可信策略字段：{key}")
