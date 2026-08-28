"""Multi Intent Plan 的策略校验和依赖图。"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping

from .types import (
    IntentPlanPolicy,
    MultiIntentPlan,
    PlanExecutionState,
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
        terminal_failures = {"failed", "skipped", "manual_intervention"}
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
            ):
                raise PlanValidationError(
                    f"Plan Step {step.step_id} 试图覆盖可信 Intent Policy"
                )
        return DependencyGraph(plan)
