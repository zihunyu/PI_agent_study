"""按依赖顺序汇总多 Intent 执行结果。"""

from __future__ import annotations

import copy

from .graph import DependencyGraph
from .types import MultiIntentPlan, PlanExecutionState, SynthesizedPlanResult


class ResultSynthesizer:
    """确定性结果汇总；不会把失败或人工介入伪装成成功。"""

    def synthesize(
        self,
        plan: MultiIntentPlan,
        state: PlanExecutionState,
        graph: DependencyGraph | None = None,
    ) -> SynthesizedPlanResult:
        dependency_graph = graph or DependencyGraph(plan)
        ordered_results: list[tuple[str, object]] = []
        failures: list[tuple[str, str]] = []
        manual: list[str] = []
        not_applicable: list[tuple[str, str]] = []
        for step_id in dependency_graph.topological_order:
            step_state = state.steps[step_id]
            if step_state.status == "succeeded":
                ordered_results.append((step_id, copy.deepcopy(step_state.result)))
            elif step_state.status in {"failed", "skipped"}:
                failures.append((step_id, step_state.error or step_state.status))
            elif step_state.status == "manual_intervention":
                manual.append(step_id)
            elif step_state.status == "not_applicable":
                not_applicable.append(
                    (step_id, step_state.error or "condition_not_met")
                )
        return SynthesizedPlanResult(
            plan_id=plan.plan_id,
            status=state.phase,
            ordered_results=tuple(ordered_results),
            failures=tuple(failures),
            manual_intervention=tuple(manual),
            not_applicable=tuple(not_applicable),
        )
