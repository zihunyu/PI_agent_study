"""Multi Intent Task 的纯事件 Reducer 和恢复规则。"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any, cast

from .graph import DependencyGraph
from .types import (
    MultiIntentPlan,
    PlanEvent,
    PlanExecutionState,
    PlanStepState,
    PlanStepStatus,
    PlanValidationError,
)


class TaskStateMachine:
    def __init__(self, plan: MultiIntentPlan, graph: DependencyGraph | None = None) -> None:
        self.plan = plan
        self.graph = graph or DependencyGraph(plan)

    def initial_state(self) -> PlanExecutionState:
        return PlanExecutionState(
            plan_id=self.plan.plan_id,
            steps={step.step_id: PlanStepState() for step in self.plan.steps},
        )

    def apply(
        self,
        state: PlanExecutionState,
        event: PlanEvent,
    ) -> PlanExecutionState:
        self._validate_state_identity(state)
        if event.sequence != state.version + 1:
            raise PlanValidationError(
                f"Plan Event Sequence 不连续：期望 {state.version + 1}，实际 {event.sequence}"
            )
        step = self.plan.step(event.step_id)
        current = state.steps[event.step_id]
        updated: PlanStepState
        if event.type == "step_waiting_approval":
            if current.status != "pending" or not step.requires_approval:
                raise PlanValidationError("当前 Step 不能进入 waiting_approval")
            if current.approval_id is not None:
                raise PlanValidationError("已批准 Step 不能再次等待审批")
            updated = replace(current, status="waiting_approval")
        elif event.type == "step_approval_granted":
            if current.status != "waiting_approval":
                raise PlanValidationError("只有 waiting_approval Step 可以批准")
            if event.data.get("actionHash") != step.action_hash:
                raise PlanValidationError("Approval Action Hash 与 Plan Step 不匹配")
            approval_id = _required_text(event.data, "approvalId")
            updated = replace(
                current,
                status="pending",
                approval_id=approval_id,
                error=None,
            )
        elif event.type == "step_approval_denied":
            if current.status != "waiting_approval":
                raise PlanValidationError("只有 waiting_approval Step 可以拒绝")
            if event.data.get("actionHash") != step.action_hash:
                raise PlanValidationError("Approval Action Hash 与 Plan Step 不匹配")
            updated = replace(
                current,
                status="failed",
                error=_required_text(event.data, "reason"),
            )
        elif event.type == "step_started":
            if current.status != "pending":
                raise PlanValidationError("只有 pending Step 可以开始")
            if any(
                state.steps[dependency].status != "succeeded"
                for dependency in step.depends_on
            ):
                raise PlanValidationError("Plan Step 依赖尚未完成")
            if step.requires_approval and current.approval_id is None:
                raise PlanValidationError("Plan Step 未通过 Approval Barrier")
            updated = replace(
                current,
                status="running",
                attempts=current.attempts + 1,
                error=None,
            )
        elif event.type == "step_succeeded":
            if current.status != "running":
                raise PlanValidationError("只有 running Step 可以成功")
            result = copy.deepcopy(event.data.get("result"))
            _strict_json(result, "Plan Step Result")
            updated = replace(current, status="succeeded", result=result, error=None)
        elif event.type == "step_failed":
            if current.status != "running":
                raise PlanValidationError("只有 running Step 可以失败")
            updated = replace(
                current,
                status="failed",
                error=_required_text(event.data, "error"),
            )
        elif event.type == "step_skipped":
            if current.status not in {"pending", "waiting_approval"}:
                raise PlanValidationError("当前 Plan Step 不能跳过")
            updated = replace(
                current,
                status="skipped",
                error=_required_text(event.data, "reason"),
            )
        elif event.type == "step_recovered":
            if current.status != "running":
                raise PlanValidationError("只有中断在 running 的 Step 可以恢复")
            target = event.data.get("targetStatus")
            expected = "pending" if step.replay_policy == "safe" else "manual_intervention"
            if target != expected:
                raise PlanValidationError("Step Recovery 与 replay_policy 不一致")
            updated = replace(
                current,
                status=cast(PlanStepStatus, target),
                error=(
                    None
                    if target == "pending"
                    else "never replay Step 中断，结果必须人工核对"
                ),
            )
        else:
            raise PlanValidationError(f"未知 Plan Event：{event.type}")
        return PlanExecutionState(
            plan_id=state.plan_id,
            steps={**state.steps, event.step_id: updated},
            version=event.sequence,
        )

    def replay(self, events: Iterable[PlanEvent]) -> PlanExecutionState:
        state = self.initial_state()
        for event in events:
            state = self.apply(state, event)
        return state

    def recover_interrupted(
        self,
        state: PlanExecutionState,
    ) -> tuple[PlanExecutionState, tuple[PlanEvent, ...]]:
        """safe Step 可重放；never Step 必须转人工介入。"""

        recovered = state
        events: list[PlanEvent] = []
        for step_id in self.graph.topological_order:
            if recovered.steps[step_id].status != "running":
                continue
            step = self.plan.step(step_id)
            event = PlanEvent(
                sequence=recovered.version + 1,
                type="step_recovered",
                step_id=step_id,
                data={
                    "targetStatus": (
                        "pending" if step.replay_policy == "safe" else "manual_intervention"
                    )
                },
            )
            recovered = self.apply(recovered, event)
            events.append(event)
        return recovered, tuple(events)

    def state_from_dict(self, value: Mapping[str, Any]) -> PlanExecutionState:
        if set(value) != {"schemaVersion", "planId", "version", "phase", "steps"}:
            raise PlanValidationError("Plan Snapshot 字段不完整或包含未知字段")
        if value.get("schemaVersion") != 1 or value.get("planId") != self.plan.plan_id:
            raise PlanValidationError("Plan Snapshot 身份或版本不匹配")
        raw_steps = value.get("steps")
        if not isinstance(raw_steps, Mapping) or set(raw_steps) != {
            step.step_id for step in self.plan.steps
        }:
            raise PlanValidationError("Plan Snapshot Step 集合不匹配")
        steps: dict[str, PlanStepState] = {}
        allowed_statuses = {
            "pending",
            "waiting_approval",
            "running",
            "succeeded",
            "failed",
            "skipped",
            "manual_intervention",
        }
        for step in self.plan.steps:
            raw = raw_steps[step.step_id]
            if not isinstance(raw, Mapping) or set(raw) != {
                "status",
                "attempts",
                "approvalId",
                "result",
                "error",
            }:
                raise PlanValidationError("Plan Snapshot Step 字段无效")
            status = raw.get("status")
            attempts = raw.get("attempts")
            if status not in allowed_statuses:
                raise PlanValidationError("Plan Snapshot Step 状态无效")
            if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
                raise PlanValidationError("Plan Snapshot attempts 无效")
            approval_id = raw.get("approvalId")
            if approval_id is not None and (
                not isinstance(approval_id, str) or not approval_id.strip()
            ):
                raise PlanValidationError("Plan Snapshot Approval ID 无效")
            if not step.requires_approval and approval_id is not None:
                raise PlanValidationError("无需审批的 Step 不能携带 Approval ID")
            if step.requires_approval and status in {"running", "succeeded"} and approval_id is None:
                raise PlanValidationError("已执行审批 Step 缺少 Approval ID")
            result = copy.deepcopy(raw.get("result"))
            _strict_json(result, "Plan Snapshot Result")
            error = raw.get("error")
            if error is not None and not isinstance(error, str):
                raise PlanValidationError("Plan Snapshot Error 无效")
            steps[step.step_id] = PlanStepState(
                status=cast(PlanStepStatus, status),
                attempts=attempts,
                approval_id=approval_id,
                result=result,
                error=error,
            )
        version = value.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise PlanValidationError("Plan Snapshot Version 无效")
        state = PlanExecutionState(self.plan.plan_id, steps, version)
        if value.get("phase") != state.phase:
            raise PlanValidationError("Plan Snapshot Phase 与 Step 状态不一致")
        self._validate_state_identity(state)
        self._validate_state_coherence(state)
        return state

    def _validate_state_identity(self, state: PlanExecutionState) -> None:
        if state.plan_id != self.plan.plan_id:
            raise PlanValidationError("Plan State 与 Plan ID 不匹配")
        if set(state.steps) != {step.step_id for step in self.plan.steps}:
            raise PlanValidationError("Plan State Step 集合不匹配")

    def _validate_state_coherence(self, state: PlanExecutionState) -> None:
        """Reject snapshots that could not have been produced by this reducer."""

        dependency_ready_statuses = {
            "waiting_approval",
            "running",
            "succeeded",
            "failed",
            "manual_intervention",
        }
        for step in self.plan.steps:
            current = state.steps[step.step_id]
            if current.status in dependency_ready_statuses and any(
                state.steps[dependency].status != "succeeded"
                for dependency in step.depends_on
            ):
                raise PlanValidationError(
                    f"Plan Snapshot Step {step.step_id} 绕过了未完成依赖"
                )
            if current.status == "waiting_approval":
                if not step.requires_approval or current.approval_id is not None:
                    raise PlanValidationError("waiting_approval Snapshot 审批状态无效")
                if current.attempts != 0 or current.result is not None or current.error is not None:
                    raise PlanValidationError("waiting_approval Snapshot 执行字段无效")
            if current.status in {"running", "succeeded", "manual_intervention"}:
                if current.attempts < 1:
                    raise PlanValidationError(
                        f"Plan Snapshot Step {step.step_id} 缺少执行 Attempt"
                    )
                if step.requires_approval and current.approval_id is None:
                    raise PlanValidationError(
                        f"Plan Snapshot Step {step.step_id} 绕过了 Approval Barrier"
                    )
            if current.status in {"pending", "running"} and (
                current.result is not None or current.error is not None
            ):
                raise PlanValidationError("未结束 Plan Step 不能携带结果或错误")
            if current.status == "succeeded" and current.error is not None:
                raise PlanValidationError("成功 Plan Step 不能携带错误")
            if current.status in {"failed", "skipped", "manual_intervention"}:
                if current.result is not None or not current.error:
                    raise PlanValidationError("失败/跳过 Plan Step 必须只有错误原因")
            if (
                step.requires_approval
                and current.status == "failed"
                and current.attempts > 0
                and current.approval_id is None
            ):
                raise PlanValidationError("已执行审批 Step 缺少 Approval ID")


def _required_text(value: Mapping[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise PlanValidationError(f"Plan Event {key} 必须是非空字符串")
    return result


def _strict_json(value: Any, name: str) -> None:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise PlanValidationError(f"{name} 必须是严格 JSON：{error}") from error
