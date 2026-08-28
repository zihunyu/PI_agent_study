"""多 Intent 计划的公共值对象。"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, TypeAlias
from uuid import uuid4


PlanReplayPolicy: TypeAlias = Literal["safe", "never"]
PlanStepStatus: TypeAlias = Literal[
    "pending",
    "waiting_approval",
    "running",
    "succeeded",
    "failed",
    "skipped",
    "manual_intervention",
]
PlanPhase: TypeAlias = Literal[
    "pending",
    "running",
    "waiting_approval",
    "completed",
    "failed",
    "manual_intervention",
]


class PlanValidationError(ValueError):
    pass


class PlanExecutionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class IntentPlanPolicy:
    """由可信业务配置提供，不能由规划模型覆盖。"""

    intent: str
    requires_approval: bool = False
    write: bool = False
    replay_policy: PlanReplayPolicy = "safe"
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _non_empty(self.intent, "intent")
        if self.replay_policy not in {"safe", "never"}:
            raise PlanValidationError("replay_policy 必须是 safe 或 never")
        if self.write and self.replay_policy != "never":
            raise PlanValidationError("写 Intent 的 replay_policy 必须是 never")
        if self.write and not self.requires_approval:
            raise PlanValidationError("写 Intent 必须经过 Approval Barrier")
        if any(not item.strip() for item in self.capabilities):
            raise PlanValidationError("capabilities 不能包含空值")


@dataclass(frozen=True, slots=True)
class PlanStep:
    step_id: str
    intent: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    requires_approval: bool = False
    write: bool = False
    replay_policy: PlanReplayPolicy = "safe"
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _non_empty(self.step_id, "step_id")
        _non_empty(self.intent, "intent")
        if self.step_id in self.depends_on:
            raise PlanValidationError("Plan Step 不能依赖自己")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise PlanValidationError("Plan Step 依赖不能重复")
        if self.replay_policy not in {"safe", "never"}:
            raise PlanValidationError("Plan Step replay_policy 无效")
        if self.write and self.replay_policy != "never":
            raise PlanValidationError("写 Plan Step 不能声明 safe replay")
        if self.write and not self.requires_approval:
            raise PlanValidationError("写 Plan Step 必须需要审批")
        _strict_json(dict(self.arguments), "arguments")
        object.__setattr__(self, "arguments", copy.deepcopy(dict(self.arguments)))

    @property
    def action_hash(self) -> str:
        return _digest(
            {
                "stepId": self.step_id,
                "intent": self.intent,
                "arguments": dict(self.arguments),
                "dependencies": list(self.depends_on),
                "requiresApproval": self.requires_approval,
                "write": self.write,
                "replayPolicy": self.replay_policy,
                "capabilities": list(self.capabilities),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "stepId": self.step_id,
            "intent": self.intent,
            "arguments": copy.deepcopy(dict(self.arguments)),
            "dependsOn": list(self.depends_on),
            "requiresApproval": self.requires_approval,
            "write": self.write,
            "replayPolicy": self.replay_policy,
            "capabilities": list(self.capabilities),
            "actionHash": self.action_hash,
        }


@dataclass(frozen=True, slots=True)
class MultiIntentPlan:
    request: str
    steps: tuple[PlanStep, ...]
    plan_id: str = field(default_factory=lambda: str(uuid4()))
    schema_version: int = 1

    def __post_init__(self) -> None:
        _non_empty(self.plan_id, "plan_id")
        _non_empty(self.request, "request")
        if self.schema_version != 1:
            raise PlanValidationError("不支持的 Plan Schema Version")
        if not self.steps:
            raise PlanValidationError("Multi Intent Plan 至少需要一个 Step")
        ids = [step.step_id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise PlanValidationError("Plan Step ID 必须唯一")

    def step(self, step_id: str) -> PlanStep:
        result = next((item for item in self.steps if item.step_id == step_id), None)
        if result is None:
            raise KeyError(f"Plan Step 不存在：{step_id}")
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "planId": self.plan_id,
            "request": self.request,
            "steps": [step.to_dict() for step in self.steps],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MultiIntentPlan":
        """Strictly restore a persisted plan and verify every action hash.

        Persisted plans are executable security decisions, not loose model output.
        Unknown fields and action-hash mismatches therefore fail closed.
        """

        if set(value) != {"schemaVersion", "planId", "request", "steps"}:
            raise PlanValidationError("Multi Intent Plan 字段不完整或包含未知字段")
        raw_steps = value.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise PlanValidationError("Multi Intent Plan steps 必须是非空数组")
        steps: list[PlanStep] = []
        expected_step_fields = {
            "stepId",
            "intent",
            "arguments",
            "dependsOn",
            "requiresApproval",
            "write",
            "replayPolicy",
            "capabilities",
            "actionHash",
        }
        for index, raw in enumerate(raw_steps, start=1):
            if not isinstance(raw, Mapping) or set(raw) != expected_step_fields:
                raise PlanValidationError(
                    f"Persisted Plan Step #{index} 字段不完整或包含未知字段"
                )
            arguments = raw.get("arguments")
            depends_on = raw.get("dependsOn")
            capabilities = raw.get("capabilities")
            requires_approval = raw.get("requiresApproval")
            write = raw.get("write")
            if not isinstance(arguments, Mapping):
                raise PlanValidationError("Persisted Plan Step arguments 必须是对象")
            if not isinstance(depends_on, list) or any(
                not isinstance(item, str) or not item.strip() for item in depends_on
            ):
                raise PlanValidationError("Persisted Plan Step dependsOn 必须是字符串数组")
            if not isinstance(capabilities, list) or any(
                not isinstance(item, str) or not item.strip() for item in capabilities
            ):
                raise PlanValidationError("Persisted Plan Step capabilities 必须是字符串数组")
            if not isinstance(requires_approval, bool) or not isinstance(write, bool):
                raise PlanValidationError("Persisted Plan Step 策略字段必须是布尔值")
            step = PlanStep(
                step_id=_text(raw.get("stepId"), "stepId"),
                intent=_text(raw.get("intent"), "intent"),
                arguments=dict(arguments),
                depends_on=tuple(depends_on),
                requires_approval=requires_approval,
                write=write,
                replay_policy=_text(raw.get("replayPolicy"), "replayPolicy"),  # type: ignore[arg-type]
                capabilities=tuple(capabilities),
            )
            if raw.get("actionHash") != step.action_hash:
                raise PlanValidationError(
                    f"Persisted Plan Step {step.step_id} Action Hash 不匹配"
                )
            steps.append(step)
        return cls(
            request=_text(value.get("request"), "request"),
            steps=tuple(steps),
            plan_id=_text(value.get("planId"), "planId"),
            schema_version=_integer(value.get("schemaVersion"), "schemaVersion"),
        )


@dataclass(frozen=True, slots=True)
class PlanApprovalDecision:
    approved: bool
    action_hash: str
    approval_id: str | None = None
    reason: str | None = None

    @classmethod
    def grant(cls, step: PlanStep, approval_id: str) -> "PlanApprovalDecision":
        return cls(True, step.action_hash, approval_id=approval_id)

    @classmethod
    def deny(cls, step: PlanStep, reason: str) -> "PlanApprovalDecision":
        return cls(False, step.action_hash, reason=reason)

    def __post_init__(self) -> None:
        _non_empty(self.action_hash, "action_hash")
        if self.approved:
            if self.approval_id is None or not self.approval_id.strip():
                raise PlanValidationError("批准决定必须携带 Approval ID")
        elif self.reason is None or not self.reason.strip():
            raise PlanValidationError("拒绝决定必须携带原因")


@dataclass(frozen=True, slots=True)
class PlanEvent:
    sequence: int
    type: str
    step_id: str
    data: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            raise PlanValidationError("Plan Event sequence 必须是正整数")
        _non_empty(self.type, "event type")
        _non_empty(self.step_id, "step_id")
        if self.schema_version != 1:
            raise PlanValidationError("不支持的 Plan Event 版本")
        _strict_json(dict(self.data), "event data")
        object.__setattr__(self, "data", copy.deepcopy(dict(self.data)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "sequence": self.sequence,
            "type": self.type,
            "stepId": self.step_id,
            "data": copy.deepcopy(dict(self.data)),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PlanEvent":
        if set(value) != {"schemaVersion", "sequence", "type", "stepId", "data"}:
            raise PlanValidationError("Plan Event 字段不完整或包含未知字段")
        return cls(
            schema_version=_integer(value["schemaVersion"], "schemaVersion"),
            sequence=_integer(value["sequence"], "sequence"),
            type=_text(value["type"], "type"),
            step_id=_text(value["stepId"], "stepId"),
            data=_mapping(value["data"], "data"),
        )


@dataclass(frozen=True, slots=True)
class PlanStepState:
    status: PlanStepStatus = "pending"
    attempts: int = 0
    approval_id: str | None = None
    result: Any = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = {
            "status": self.status,
            "attempts": self.attempts,
            "approvalId": self.approval_id,
            "result": copy.deepcopy(self.result),
            "error": self.error,
        }
        _strict_json(value, "step state")
        return value


@dataclass(frozen=True, slots=True)
class PlanExecutionState:
    plan_id: str
    steps: Mapping[str, PlanStepState]
    version: int = 0

    @property
    def phase(self) -> PlanPhase:
        statuses = {step.status for step in self.steps.values()}
        if "manual_intervention" in statuses:
            return "manual_intervention"
        if statuses == {"succeeded"}:
            return "completed"
        if "running" in statuses:
            return "running"
        if "waiting_approval" in statuses:
            return "waiting_approval"
        if statuses & {"failed", "skipped"} and not statuses & {"pending"}:
            return "failed"
        return "pending"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "planId": self.plan_id,
            "version": self.version,
            "phase": self.phase,
            "steps": {
                step_id: step.to_dict() for step_id, step in self.steps.items()
            },
        }


@dataclass(frozen=True, slots=True)
class SynthesizedPlanResult:
    plan_id: str
    status: PlanPhase
    ordered_results: tuple[tuple[str, Any], ...]
    failures: tuple[tuple[str, str], ...]
    manual_intervention: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PlanExecutionResult:
    state: PlanExecutionState
    synthesized: SynthesizedPlanResult
    events: tuple[PlanEvent, ...]


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _strict_json(value: Any, name: str) -> None:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise PlanValidationError(f"{name} 必须是严格 JSON：{error}") from error


def _non_empty(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise PlanValidationError(f"{name} 不能为空")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanValidationError(f"{name} 必须是非空字符串")
    return value


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlanValidationError(f"{name} 必须是整数")
    return value


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PlanValidationError(f"{name} 必须是对象")
    return value
