"""Multi Intent Plan 的策略校验和依赖图。"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import replace

from .types import (
    IntentPlanPolicy,
    MultiIntentPlan,
    PlanArgumentBinding,
    PlanCondition,
    PlanIntentArgumentBinding,
    PlanIntentCondition,
    PlanIntentResultReference,
    PlanResultReference,
    PlanExecutionState,
    PlanStep,
    PlanValidationError,
)


class DependencyGraph:
    def __init__(self, plan: MultiIntentPlan) -> None:
        self.plan = plan
        self.dependencies = {
            step.step_id: frozenset(step.depends_on) for step in plan.steps
        }
        dependents: dict[str, set[str]] = {step.step_id: set() for step in plan.steps}
        for step_id, dependencies in self.dependencies.items():
            for dependency in dependencies:
                if dependency not in dependents:
                    raise PlanValidationError(
                        f"Plan Step {step_id} 依赖不存在的 Step：{dependency}"
                    )
                dependents[dependency].add(step_id)
        self.dependents = {
            step_id: frozenset(values) for step_id, values in dependents.items()
        }
        self._topological_order = self._sort()

    @property
    def topological_order(self) -> tuple[str, ...]:
        return self._topological_order

    def ready_steps(self, state: PlanExecutionState) -> tuple[str, ...]:
        ready = []
        for step_id in self.topological_order:
            step_state = state.steps[step_id]
            if step_state.status != "pending":
                continue
            if all(
                state.steps[dependency].status == "succeeded"
                for dependency in self.dependencies[step_id]
            ):
                ready.append(step_id)
        return tuple(ready)

    def blocked_steps(self, state: PlanExecutionState) -> tuple[str, ...]:
        blocked = []
        terminal_failures = {
            "failed",
            "skipped",
            "not_applicable",
            "manual_intervention",
        }
        for step_id in self.topological_order:
            if state.steps[step_id].status != "pending":
                continue
            if any(
                state.steps[dependency].status in terminal_failures
                for dependency in self.dependencies[step_id]
            ):
                blocked.append(step_id)
        return tuple(blocked)

    def descendants(self, step_id: str) -> tuple[str, ...]:
        if step_id not in self.dependents:
            raise KeyError(f"Plan Step 不存在：{step_id}")
        found: set[str] = set()
        queue = deque(self.dependents[step_id])
        while queue:
            current = queue.popleft()
            if current in found:
                continue
            found.add(current)
            queue.extend(self.dependents[current])
        return tuple(item for item in self.topological_order if item in found)

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return descendant in self.descendants(ancestor)

    def _sort(self) -> tuple[str, ...]:
        indegree = {
            step_id: len(dependencies)
            for step_id, dependencies in self.dependencies.items()
        }
        source_order = {step.step_id: index for index, step in enumerate(self.plan.steps)}
        ready = deque(
            sorted(
                (step_id for step_id, count in indegree.items() if count == 0),
                key=source_order.__getitem__,
            )
        )
        ordered: list[str] = []
        while ready:
            current = ready.popleft()
            ordered.append(current)
            for dependent in sorted(
                self.dependents[current], key=source_order.__getitem__
            ):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(dependent)
        if len(ordered) != len(indegree):
            cyclic = sorted(
                step_id for step_id, count in indegree.items() if count > 0
            )
            raise PlanValidationError(f"Plan Dependency Graph 存在环：{cyclic}")
        return tuple(ordered)


class PlanValidator:
    """将模型 Plan 与可信 Intent Policy Catalog 做精确比对。"""

    def __init__(self, policies: Mapping[str, IntentPlanPolicy]) -> None:
        self.policies = dict(policies)
        if not self.policies:
            raise PlanValidationError("Intent Plan Policy Catalog 不能为空")
        for intent, policy in self.policies.items():
            if intent != policy.intent:
                raise PlanValidationError("Intent Policy Catalog Key 与 Policy 不一致")

    def validate(self, plan: MultiIntentPlan) -> DependencyGraph:
        for step in plan.steps:
            self.validate_step(step)
        graph = DependencyGraph(plan)
        self._validate_dataflow(plan, graph)
        self._validate_required_predecessors(plan, graph)
        self._validate_side_effect_order(plan, graph)
        return graph

    def bind_trusted_dependencies(self, plan: MultiIntentPlan) -> MultiIntentPlan:
        """Apply policy-owned dependencies/dataflow to untrusted planner output.

        Router/model dependency hints are only extra restrictions.  They can
        never remove a business prerequisite or opt dangerous operations into
        parallel execution.
        """

        source_order = {step.step_id: index for index, step in enumerate(plan.steps)}
        policies: dict[str, IntentPlanPolicy] = {}
        for step in plan.steps:
            policy = self.policies.get(step.intent)
            if policy is None:
                raise PlanValidationError(
                    f"规划模型选择了未配置 Intent：{step.intent}"
                )
            policies[step.step_id] = policy

        dependencies = {
            step.step_id: list(dict.fromkeys(step.depends_on)) for step in plan.steps
        }
        previous_unsafe: list[PlanStep] = []
        for step in plan.steps:
            policy = policies[step.step_id]
            predecessor_intents = set(policy.required_predecessor_intents)
            predecessor_intents.update(
                binding.source.intent for binding in policy.argument_bindings
            )
            for condition in policy.preconditions:
                predecessor_intents.add(condition.left.intent)
                if condition.expected_from is not None:
                    predecessor_intents.add(condition.expected_from.intent)
            for intent in predecessor_intents:
                candidates = [
                    candidate
                    for candidate in plan.steps
                    if candidate.intent == intent
                    and source_order[candidate.step_id] < source_order[step.step_id]
                ]
                if not candidates:
                    raise PlanValidationError(
                        f"Plan Step {step.step_id} 缺少可信前置 Intent：{intent}"
                    )
                for candidate in candidates:
                    if candidate.step_id not in dependencies[step.step_id]:
                        dependencies[step.step_id].append(candidate.step_id)

            unsafe = policy.write or policy.replay_policy == "never"
            if unsafe:
                for previous in previous_unsafe:
                    previous_policy = policies[previous.step_id]
                    if (
                        policy.allow_parallel_side_effects
                        and previous_policy.allow_parallel_side_effects
                    ):
                        continue
                    if previous.step_id not in dependencies[step.step_id]:
                        dependencies[step.step_id].append(previous.step_id)
                previous_unsafe.append(step)

        bound_steps: list[PlanStep] = []
        for step in plan.steps:
            policy = policies[step.step_id]
            direct = tuple(dependencies[step.step_id])
            bindings = tuple(
                _bind_argument_template(step, template, plan, direct)
                for template in policy.argument_bindings
            )
            conditions = tuple(
                _bind_condition_template(step, template, plan, direct)
                for template in policy.preconditions
            )
            bound_steps.append(
                replace(
                    step,
                    depends_on=direct,
                    requires_approval=policy.requires_approval,
                    write=policy.write,
                    replay_policy=policy.replay_policy,
                    capabilities=policy.capabilities,
                    approval_roles=policy.approval_roles,
                    parameter_contract=policy.parameter_contract,
                    required_predecessor_intents=policy.required_predecessor_intents,
                    argument_bindings=bindings,
                    preconditions=conditions,
                    allow_parallel_side_effects=policy.allow_parallel_side_effects,
                    result_contract=policy.result_contract,
                )
            )
        bound = MultiIntentPlan(
            request=plan.request,
            steps=tuple(bound_steps),
            plan_id=plan.plan_id,
            schema_version=plan.schema_version,
        )
        self.validate(bound)
        return bound

    def validate_step(self, step: PlanStep) -> None:
        """Validate one Step immediately before it crosses an execution boundary."""

        policy = self.policies.get(step.intent)
        if policy is None:
            raise PlanValidationError(
                f"规划模型选择了未配置 Intent：{step.intent}"
            )
        if (
            step.requires_approval != policy.requires_approval
            or step.write != policy.write
            or step.replay_policy != policy.replay_policy
            or tuple(step.capabilities) != tuple(policy.capabilities)
            or tuple(step.approval_roles) != tuple(policy.approval_roles)
            or step.parameter_contract != policy.parameter_contract
            or tuple(step.required_predecessor_intents)
            != tuple(policy.required_predecessor_intents)
            or step.allow_parallel_side_effects
            != policy.allow_parallel_side_effects
            or step.result_contract != policy.result_contract
        ):
            raise PlanValidationError(
                f"Plan Step {step.step_id} 试图覆盖可信 Intent Policy"
            )
        unsafe = policy.write or policy.replay_policy == "never"
        contract = policy.parameter_contract
        if contract is None:
            # Read-only legacy policies remain source compatible.  A legacy
            # dangerous policy may continue to carry existing non-empty
            # parameters, but an empty write/never shell is never executable.
            if unsafe and not step.arguments and not step.argument_bindings:
                raise PlanValidationError(
                    f"Plan Step {step.step_id} 是 write/never 操作；"
                    "空参数需要显式 PlanParameterContract"
                )
            return
        contract.validate_unresolved(
            step.arguments,
            tuple(binding.target for binding in step.argument_bindings),
            owner=f"Plan Step {step.step_id}",
            require_non_empty=unsafe,
        )

    def _validate_dataflow(
        self,
        plan: MultiIntentPlan,
        graph: DependencyGraph,
    ) -> None:
        for step in plan.steps:
            policy = self.policies[step.intent]
            expected_bindings = tuple(
                _bind_argument_template(step, template, plan, step.depends_on)
                for template in policy.argument_bindings
            )
            expected_conditions = tuple(
                _bind_condition_template(step, template, plan, step.depends_on)
                for template in policy.preconditions
            )
            if step.argument_bindings != expected_bindings:
                raise PlanValidationError(
                    f"Plan Step {step.step_id} argument binding 未由可信 Policy 声明"
                )
            if step.preconditions != expected_conditions:
                raise PlanValidationError(
                    f"Plan Step {step.step_id} precondition 未由可信 Policy 声明"
                )
            references = [binding.source for binding in step.argument_bindings]
            for condition in step.preconditions:
                references.append(condition.left)
                if condition.expected_from is not None:
                    references.append(condition.expected_from)
            for reference in references:
                if reference.step_id not in graph.dependencies[step.step_id]:
                    raise PlanValidationError(
                        f"Plan Step {step.step_id} 只能读取直接 depends_on 的结果："
                        f"{reference.step_id}"
                    )

    def _validate_required_predecessors(
        self,
        plan: MultiIntentPlan,
        graph: DependencyGraph,
    ) -> None:
        for step in plan.steps:
            for intent in self.policies[step.intent].required_predecessor_intents:
                candidates = [
                    candidate.step_id
                    for candidate in plan.steps
                    if candidate.intent == intent
                    and graph.is_ancestor(candidate.step_id, step.step_id)
                ]
                if not candidates:
                    raise PlanValidationError(
                        f"Plan Step {step.step_id} 缺少可信前置 Intent：{intent}"
                    )

    def _validate_side_effect_order(
        self,
        plan: MultiIntentPlan,
        graph: DependencyGraph,
    ) -> None:
        unsafe = [
            step
            for step in plan.steps
            if self.policies[step.intent].write
            or self.policies[step.intent].replay_policy == "never"
        ]
        for index, left in enumerate(unsafe):
            left_policy = self.policies[left.intent]
            for right in unsafe[index + 1 :]:
                right_policy = self.policies[right.intent]
                if (
                    left_policy.allow_parallel_side_effects
                    and right_policy.allow_parallel_side_effects
                ):
                    continue
                ordered = graph.is_ancestor(left.step_id, right.step_id) or graph.is_ancestor(
                    right.step_id, left.step_id
                )
                if not ordered:
                    raise PlanValidationError(
                        "危险副作用 Step 缺少可信顺序依赖："
                        f"{left.step_id}, {right.step_id}"
                    )


def _source_step_for_intent(
    owner: PlanStep,
    source_intent: str,
    plan: MultiIntentPlan,
    dependencies: tuple[str, ...],
) -> str:
    candidates = [
        step.step_id
        for step in plan.steps
        if step.step_id in dependencies and step.intent == source_intent
    ]
    if len(candidates) != 1:
        raise PlanValidationError(
            f"Plan Step {owner.step_id} 的前置 Intent {source_intent} "
            "必须精确对应一个直接依赖"
        )
    return candidates[0]


def _bind_reference(
    owner: PlanStep,
    reference: PlanIntentResultReference,
    plan: MultiIntentPlan,
    dependencies: tuple[str, ...],
) -> PlanResultReference:
    return PlanResultReference(
        _source_step_for_intent(owner, reference.intent, plan, dependencies),
        reference.path,
    )


def _bind_argument_template(
    owner: PlanStep,
    template: PlanIntentArgumentBinding,
    plan: MultiIntentPlan,
    dependencies: tuple[str, ...],
) -> PlanArgumentBinding:
    return PlanArgumentBinding(
        template.target,
        _bind_reference(owner, template.source, plan, dependencies),
    )


def _bind_condition_template(
    owner: PlanStep,
    template: PlanIntentCondition,
    plan: MultiIntentPlan,
    dependencies: tuple[str, ...],
) -> PlanCondition:
    return PlanCondition(
        left=_bind_reference(owner, template.left, plan, dependencies),
        operator=template.operator,
        expected=template.expected,
        expected_from=(
            None
            if template.expected_from is None
            else _bind_reference(owner, template.expected_from, plan, dependencies)
        ),
    )
