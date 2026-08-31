"""Multi Intent Task 的纯事件 Reducer 和恢复规则。"""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any, Literal, cast

from .graph import DependencyGraph
from .types import (
    MultiIntentPlan,
    PlanApprovalReceipt,
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
            if current.approval_receipt is not None:
                raise PlanValidationError("已批准 Step 不能再次等待审批")
            action_hash = _required_sha256(event.data, "actionHash")
            raw_approval_id = event.data.get("approvalId")
            # 兼容已经落盘的 schema_version=1 旧事件；新 Executor 始终写入
            # 真实或稳定的 Approval ID。无 ID 的旧状态依然绝不能执行。
            approval_id = (
                None
                if raw_approval_id is None
                else _required_text(event.data, "approvalId")
            )
            updated = replace(
                current,
                status="waiting_approval",
                approval_id=approval_id,
                approval_action_hash=action_hash,
            )
        elif event.type == "step_approval_granted":
            if current.status not in {"pending", "waiting_approval"}:
                raise PlanValidationError("当前 Step 不能批准")
            action_hash = _required_sha256(event.data, "actionHash")
            expected_action_hash = current.approval_action_hash or action_hash
            if action_hash != expected_action_hash:
                raise PlanValidationError(
                    "Approval Action Hash 与等待中的最终 Action 不匹配"
                )
            approval_id = _required_text(event.data, "approvalId")
            raw_receipt = event.data.get("receipt")
            if not isinstance(raw_receipt, Mapping):
                raise PlanValidationError(
                    "批准 Plan Step 必须携带已消费的 PlanApprovalReceipt"
                )
            receipt = PlanApprovalReceipt.from_dict(raw_receipt)
            if receipt.consumed_at is None:
                raise PlanValidationError("PlanApprovalReceipt 尚未消费")
            if (
                receipt.approval_id != approval_id
                or receipt.plan_id != state.plan_id
                or receipt.step_id != step.step_id
                or receipt.action_hash != expected_action_hash
                or receipt.state_version != state.version
            ):
                raise PlanValidationError("PlanApprovalReceipt 与当前 Plan Step 不匹配")
            if (
                current.approval_id is not None
                and current.approval_id != approval_id
                and not current.approval_id.startswith("plan-approval-request-")
            ):
                raise PlanValidationError("Approval ID 与等待中的请求不匹配")
            if frozenset(step.approval_roles).isdisjoint(receipt.approver_roles):
                raise PlanValidationError("PlanApprovalReceipt 审批人角色不匹配")
            updated = replace(
                current,
                status="pending",
                approval_id=approval_id,
                approval_action_hash=expected_action_hash,
                approval_receipt=receipt,
                error=None,
            )
        elif event.type == "step_approval_denied":
            if current.status not in {"pending", "waiting_approval"}:
                raise PlanValidationError("当前 Step 不能拒绝")
            action_hash = _required_sha256(event.data, "actionHash")
            expected_action_hash = current.approval_action_hash or action_hash
            if action_hash != expected_action_hash:
                raise PlanValidationError(
                    "Approval Action Hash 与等待中的最终 Action 不匹配"
                )
            raw_approval_id = event.data.get("approvalId")
            decision_approval_id = (
                None
                if raw_approval_id is None
                else _required_text(event.data, "approvalId")
            )
            if (
                current.approval_id is not None
                and decision_approval_id is not None
                and current.approval_id != decision_approval_id
            ):
                raise PlanValidationError("拒绝决定与等待中的 Approval ID 不匹配")
            updated = replace(
                current,
                status="failed",
                approval_id=decision_approval_id or current.approval_id,
                approval_action_hash=expected_action_hash,
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
            if step.requires_approval:
                started_receipt = current.approval_receipt
                if current.approval_id is None or started_receipt is None:
                    raise PlanValidationError("Plan Step 未通过 Approval Barrier")
                if (
                    event.data.get("approvalReceiptId")
                    != started_receipt.receipt_id
                ):
                    raise PlanValidationError("Plan Step 启动时 Approval Receipt 不匹配")
                started_at = _required_timestamp(event.data, "startedAt")
                if started_at < cast(float, started_receipt.consumed_at):
                    raise PlanValidationError("Plan Step 不能早于 Approval Receipt 消费时间")
                if started_at > started_receipt.expires_at:
                    raise PlanValidationError("Plan Step 启动时 Approval Receipt 已过期")
            updated = replace(
                current,
                status="running",
                attempts=current.attempts + 1,
                error=None,
                validation_status=None,
            )
        elif event.type == "step_validation_passed":
            if current.status != "running":
                raise PlanValidationError("只有 running Step 可以通过结果校验")
            digest = _required_text(event.data, "resultDigest")
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest.casefold()
            ):
                raise PlanValidationError("Plan Result Digest 必须是 SHA-256")
            updated = replace(current, validation_status="passed")
        elif event.type == "step_validation_failed":
            if current.status != "running":
                raise PlanValidationError("只有 running Step 可以结果校验失败")
            updated = replace(
                current,
                status=(
                    "manual_intervention"
                    if step.write or step.replay_policy == "never"
                    else "failed"
                ),
                validation_status="failed",
                error=_required_text(event.data, "error"),
            )
        elif event.type == "step_succeeded":
            if current.status != "running":
                raise PlanValidationError("只有 running Step 可以成功")
            if step.result_contract is not None and current.validation_status != "passed":
                raise PlanValidationError("Plan Step 尚未通过受信结果校验")
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
        elif event.type == "step_outcome_unknown":
            if current.status != "running":
                raise PlanValidationError(
                    "只有 running Step 可以进入 outcome_unknown"
                )
            if step.replay_policy != "never":
                raise PlanValidationError(
                    "只有 never replay Step 可以进入 outcome_unknown"
                )
            updated = replace(
                current,
                status="manual_intervention",
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
        elif event.type in {"step_arguments_invalid", "step_precondition_failed"}:
            if current.status not in {"pending", "waiting_approval"}:
                raise PlanValidationError("当前 Plan Step 不能在派发前失败")
            updated = replace(
                current,
                status="failed",
                error=_required_text(event.data, "error"),
            )
        elif event.type == "step_not_applicable":
            if current.status not in {"pending", "waiting_approval"}:
                raise PlanValidationError("当前 Plan Step 不能标记为 not_applicable")
            updated = replace(
                current,
                status="not_applicable",
                approval_id=(
                    current.approval_id
                    if current.approval_receipt is not None
                    else None
                ),
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
                validation_status=None,
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
            "not_applicable",
            "manual_intervention",
        }
        for step in self.plan.steps:
            raw = raw_steps[step.step_id]
            required_fields = {
                "status",
                "attempts",
                "approvalId",
                "result",
                "error",
            }
            if (
                not isinstance(raw, Mapping)
                or not required_fields.issubset(raw)
                or set(raw)
                - (
                    required_fields
                    | {
                        "approvalReceipt",
                        "approvalActionHash",
                        "validationStatus",
                    }
                )
            ):
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
            raw_receipt = raw.get("approvalReceipt")
            raw_action_hash = raw.get("approvalActionHash")
            if raw_action_hash is None:
                approval_action_hash = (
                    step.action_hash if approval_id is not None else None
                )
            elif isinstance(raw_action_hash, str):
                approval_action_hash = _sha256_text(
                    raw_action_hash,
                    "Plan Snapshot Approval Action Hash",
                )
            else:
                raise PlanValidationError(
                    "Plan Snapshot Approval Action Hash 无效"
                )
            if raw_receipt is None:
                receipt = None
            elif isinstance(raw_receipt, Mapping):
                receipt = PlanApprovalReceipt.from_dict(raw_receipt)
            else:
                raise PlanValidationError("Plan Snapshot Approval Receipt 无效")
            if receipt is not None:
                if not step.requires_approval:
                    raise PlanValidationError("无需审批的 Step 不能携带 Approval Receipt")
                if (
                    approval_id != receipt.approval_id
                    or receipt.plan_id != self.plan.plan_id
                    or receipt.step_id != step.step_id
                    or receipt.action_hash != approval_action_hash
                    or receipt.consumed_at is None
                ):
                    raise PlanValidationError("Plan Snapshot Approval Receipt 绑定无效")
                if frozenset(step.approval_roles).isdisjoint(receipt.approver_roles):
                    raise PlanValidationError("Plan Snapshot Approval Receipt 角色无效")
            result = copy.deepcopy(raw.get("result"))
            _strict_json(result, "Plan Snapshot Result")
            error = raw.get("error")
            if error is not None and not isinstance(error, str):
                raise PlanValidationError("Plan Snapshot Error 无效")
            validation_status = raw.get("validationStatus")
            if validation_status not in {None, "passed", "failed"}:
                raise PlanValidationError("Plan Snapshot Validation Status 无效")
            typed_validation_status = cast(
                Literal["passed", "failed"] | None,
                validation_status,
            )
            steps[step.step_id] = PlanStepState(
                status=cast(PlanStepStatus, status),
                attempts=attempts,
                approval_id=approval_id,
                approval_action_hash=approval_action_hash,
                approval_receipt=receipt,
                result=result,
                error=error,
                validation_status=typed_validation_status,
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
            "not_applicable",
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
                if (
                    not step.requires_approval
                    or current.approval_receipt is not None
                ):
                    raise PlanValidationError("waiting_approval Snapshot 审批状态无效")
                if current.attempts != 0 or current.result is not None or current.error is not None:
                    raise PlanValidationError("waiting_approval Snapshot 执行字段无效")
            if current.status in {"running", "succeeded", "manual_intervention"}:
                if current.attempts < 1:
                    raise PlanValidationError(
                        f"Plan Snapshot Step {step.step_id} 缺少执行 Attempt"
                    )
                if step.requires_approval and (
                    current.approval_id is None
                    or current.approval_receipt is None
                ):
                    raise PlanValidationError(
                        f"Plan Snapshot Step {step.step_id} 绕过了 Approval Barrier"
                    )
            if current.approval_id is None and current.approval_receipt is not None:
                raise PlanValidationError("Plan Snapshot Receipt 缺少 Approval ID")
            if (
                current.approval_id is None
                and current.approval_action_hash is not None
                and current.status != "waiting_approval"
            ):
                raise PlanValidationError(
                    "Plan Snapshot Approval Action Hash 缺少 Approval ID"
                )
            if current.approval_id is not None and current.approval_action_hash is None:
                raise PlanValidationError(
                    "Plan Snapshot Approval ID 缺少最终 Action Hash"
                )
            if (
                current.approval_id is not None
                and current.approval_receipt is None
                and current.status not in {"waiting_approval", "failed"}
            ):
                raise PlanValidationError(
                    "Plan Snapshot Approval ID 不能替代已消费的 Receipt"
                )
            if current.status in {"pending", "running"} and (
                current.result is not None or current.error is not None
            ):
                raise PlanValidationError("未结束 Plan Step 不能携带结果或错误")
            if current.status == "succeeded" and current.error is not None:
                raise PlanValidationError("成功 Plan Step 不能携带错误")
            if current.status in {
                "failed",
                "skipped",
                "not_applicable",
                "manual_intervention",
            }:
                if current.result is not None or not current.error:
                    raise PlanValidationError("失败/跳过 Plan Step 必须只有错误原因")
            if current.validation_status == "failed" and current.status not in {
                "failed",
                "manual_intervention",
            }:
                raise PlanValidationError("结果校验失败状态与 Step 状态不一致")
            if current.validation_status == "passed" and current.status not in {
                "running",
                "succeeded",
            }:
                raise PlanValidationError("结果校验通过状态与 Step 状态不一致")
            if (
                step.result_contract is not None
                and current.status == "succeeded"
                and current.validation_status != "passed"
            ):
                raise PlanValidationError("成功 Step 缺少受信结果校验")
            if (
                step.requires_approval
                and current.status == "failed"
                and current.attempts > 0
                and (
                    current.approval_id is None
                    or current.approval_receipt is None
                )
            ):
                raise PlanValidationError("已执行审批 Step 缺少 Approval Receipt")


def _required_text(value: Mapping[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise PlanValidationError(f"Plan Event {key} 必须是非空字符串")
    return result


def _sha256_text(value: str, name: str) -> str:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value.casefold()
    ):
        raise PlanValidationError(f"{name} 必须是 SHA-256")
    return value


def _required_sha256(value: Mapping[str, Any], key: str) -> str:
    return _sha256_text(_required_text(value, key), f"Plan Event {key}")


def _required_timestamp(value: Mapping[str, Any], key: str) -> float:
    result = value.get(key)
    if (
        isinstance(result, bool)
        or not isinstance(result, (int, float))
        or not math.isfinite(float(result))
    ):
        raise PlanValidationError(f"Plan Event {key} 必须是有限时间戳")
    return float(result)


def _strict_json(value: Any, name: str) -> None:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise PlanValidationError(f"{name} 必须是严格 JSON：{error}") from error
