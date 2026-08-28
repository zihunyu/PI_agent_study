"""受可信 Intent Catalog 约束的 HybridRequestPlanner。"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast
from uuid import uuid4

from .graph import PlanValidator
from .types import IntentPlanPolicy, MultiIntentPlan, PlanStep, PlanValidationError


PlannerFn = Callable[[str, tuple[dict[str, Any], ...]], Any]
RulePlannerFn = Callable[[str], Any]


class HybridRequestPlanner:
    """规则优先、结构化 Planner 回退；模型不能修改可信策略。"""

    def __init__(
        self,
        policies: Mapping[str, IntentPlanPolicy],
        planner_fn: PlannerFn,
        *,
        rule_planner: RulePlannerFn | None = None,
    ) -> None:
        self.policies = dict(policies)
        self.planner_fn = planner_fn
        self.rule_planner = rule_planner
        self.validator = PlanValidator(self.policies)

    async def plan(self, request: str) -> MultiIntentPlan:
        if not isinstance(request, str) or not request.strip():
            raise PlanValidationError("规划请求不能为空")
        raw: Any = None
        if self.rule_planner is not None:
            raw = self.rule_planner(request)
            raw = await cast(Awaitable[Any], raw) if inspect.isawaitable(raw) else raw
        if raw is None:
            catalog = tuple(
                {
                    "intent": policy.intent,
                    "requiresApproval": policy.requires_approval,
                    "write": policy.write,
                    "replayPolicy": policy.replay_policy,
                    "capabilities": list(policy.capabilities),
                }
                for policy in self.policies.values()
            )
            raw = self.planner_fn(request, catalog)
            raw = await cast(Awaitable[Any], raw) if inspect.isawaitable(raw) else raw
        plan = raw if isinstance(raw, MultiIntentPlan) else self._parse(request, raw)
        if plan.request != request:
            raise PlanValidationError("Planner 不得替换原始用户请求")
        self.validator.validate(plan)
        return plan

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
