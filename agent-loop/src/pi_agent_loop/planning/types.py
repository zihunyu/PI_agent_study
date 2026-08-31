"""多 Intent 计划的公共值对象。"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Literal,
    Mapping,
    Sequence,
    TypeAlias,
)
from uuid import uuid4

if TYPE_CHECKING:
    from ..session.operation_store import ClaimLease
    from ..security import VerifiedIdentity


PlanReplayPolicy: TypeAlias = Literal["safe", "never"]
PlanParameterType: TypeAlias = Literal[
    "string",
    "integer",
    "number",
    "boolean",
    "object",
    "array",
    "null",
]
PlanStepStatus: TypeAlias = Literal[
    "pending",
    "waiting_approval",
    "running",
    "succeeded",
    "failed",
    "skipped",
    "not_applicable",
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


PlanComparisonOperator: TypeAlias = Literal[
    "eq",
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "in",
    "not_in",
    "truthy",
    "falsy",
]


@dataclass(frozen=True, slots=True)
class PlanResultReference:
    """A restricted JSON path into one declared predecessor result.

    Paths can only traverse JSON object keys and non-negative array indexes.  In
    particular they cannot access Python attributes, call functions or evaluate
    expressions.
    """

    step_id: str
    path: tuple[str | int, ...] = ()

    def __post_init__(self) -> None:
        _non_empty(self.step_id, "result reference step_id")
        _validate_json_path(self.path, "result reference path")

    def to_dict(self) -> dict[str, Any]:
        return {"stepId": self.step_id, "path": list(self.path)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PlanResultReference":
        if not isinstance(value, Mapping) or set(value) != {"stepId", "path"}:
            raise PlanValidationError("Plan Result Reference 字段无效")
        path = value.get("path")
        if not isinstance(path, list):
            raise PlanValidationError("Plan Result Reference path 必须是数组")
        return cls(
            step_id=_text(value.get("stepId"), "stepId"),
            path=tuple(path),
        )


@dataclass(frozen=True, slots=True)
class PlanIntentResultReference:
    """Trusted policy template referencing a predecessor by intent."""

    intent: str
    path: tuple[str | int, ...] = ()

    def __post_init__(self) -> None:
        _non_empty(self.intent, "result reference intent")
        _validate_json_path(self.path, "result reference path")

    def to_dict(self) -> dict[str, Any]:
        return {"intent": self.intent, "path": list(self.path)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PlanIntentResultReference":
        if not isinstance(value, Mapping) or set(value) != {"intent", "path"}:
            raise PlanValidationError("Intent Result Reference 字段无效")
        path = value.get("path")
        if not isinstance(path, list):
            raise PlanValidationError("Intent Result Reference path 必须是数组")
        return cls(intent=_text(value.get("intent"), "intent"), path=tuple(path))


@dataclass(frozen=True, slots=True)
class PlanArgumentBinding:
    """Bind one top-level argument to a predecessor's JSON result."""

    target: str
    source: PlanResultReference

    def __post_init__(self) -> None:
        _non_empty(self.target, "argument binding target")
        if not isinstance(self.source, PlanResultReference):
            raise PlanValidationError("argument binding source 类型无效")

    def to_dict(self) -> dict[str, Any]:
        return {"target": self.target, "source": self.source.to_dict()}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PlanArgumentBinding":
        if not isinstance(value, Mapping) or set(value) != {"target", "source"}:
            raise PlanValidationError("Plan Argument Binding 字段无效")
        source = value.get("source")
        if not isinstance(source, Mapping):
            raise PlanValidationError("Plan Argument Binding source 必须是对象")
        return cls(
            target=_text(value.get("target"), "target"),
            source=PlanResultReference.from_dict(source),
        )


@dataclass(frozen=True, slots=True)
class PlanIntentArgumentBinding:
    """Trusted binding template; the planner resolves intent to a concrete step."""

    target: str
    source: PlanIntentResultReference

    def __post_init__(self) -> None:
        _non_empty(self.target, "intent argument binding target")
        if not isinstance(self.source, PlanIntentResultReference):
            raise PlanValidationError("intent argument binding source 类型无效")

    def to_dict(self) -> dict[str, Any]:
        return {"target": self.target, "source": self.source.to_dict()}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PlanIntentArgumentBinding":
        if not isinstance(value, Mapping) or set(value) != {"target", "source"}:
            raise PlanValidationError("Intent Argument Binding 字段无效")
        source = value.get("source")
        if not isinstance(source, Mapping):
            raise PlanValidationError("Intent Argument Binding source 必须是对象")
        return cls(
            target=_text(value.get("target"), "target"),
            source=PlanIntentResultReference.from_dict(source),
        )


@dataclass(frozen=True, slots=True)
class PlanCondition:
    """Restricted comparison evaluated before a step is dispatched."""

    left: PlanResultReference
    operator: PlanComparisonOperator
    expected: Any = None
    expected_from: PlanResultReference | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.left, PlanResultReference):
            raise PlanValidationError("Plan Condition left 类型无效")
        _validate_comparison(
            self.operator,
            self.expected,
            self.expected_from,
            owner="Plan Condition",
        )
        object.__setattr__(self, "expected", copy.deepcopy(self.expected))

    def to_dict(self) -> dict[str, Any]:
        return {
            "left": self.left.to_dict(),
            "operator": self.operator,
            "expected": copy.deepcopy(self.expected),
            "expectedFrom": (
                None if self.expected_from is None else self.expected_from.to_dict()
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PlanCondition":
        if not isinstance(value, Mapping) or set(value) != {
            "left",
            "operator",
            "expected",
            "expectedFrom",
        }:
            raise PlanValidationError("Plan Condition 字段无效")
        left = value.get("left")
        expected_from = value.get("expectedFrom")
        if not isinstance(left, Mapping):
            raise PlanValidationError("Plan Condition left 必须是对象")
        if expected_from is not None and not isinstance(expected_from, Mapping):
            raise PlanValidationError("Plan Condition expectedFrom 必须是对象或 null")
        return cls(
            left=PlanResultReference.from_dict(left),
            operator=_comparison_operator(value.get("operator")),
            expected=copy.deepcopy(value.get("expected")),
            expected_from=(
                None
                if expected_from is None
                else PlanResultReference.from_dict(expected_from)
            ),
        )


@dataclass(frozen=True, slots=True)
class PlanIntentCondition:
    """Trusted policy condition referencing predecessor intents, not model IDs."""

    left: PlanIntentResultReference
    operator: PlanComparisonOperator
    expected: Any = None
    expected_from: PlanIntentResultReference | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.left, PlanIntentResultReference):
            raise PlanValidationError("Intent Condition left 类型无效")
        _validate_comparison(
            self.operator,
            self.expected,
            self.expected_from,
            owner="Intent Condition",
        )
        object.__setattr__(self, "expected", copy.deepcopy(self.expected))

    def to_dict(self) -> dict[str, Any]:
        return {
            "left": self.left.to_dict(),
            "operator": self.operator,
            "expected": copy.deepcopy(self.expected),
            "expectedFrom": (
                None if self.expected_from is None else self.expected_from.to_dict()
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PlanIntentCondition":
        if not isinstance(value, Mapping) or set(value) != {
            "left",
            "operator",
            "expected",
            "expectedFrom",
        }:
            raise PlanValidationError("Intent Condition 字段无效")
        left = value.get("left")
        expected_from = value.get("expectedFrom")
        if not isinstance(left, Mapping):
            raise PlanValidationError("Intent Condition left 必须是对象")
        if expected_from is not None and not isinstance(expected_from, Mapping):
            raise PlanValidationError("Intent Condition expectedFrom 必须是对象或 null")
        return cls(
            left=PlanIntentResultReference.from_dict(left),
            operator=_comparison_operator(value.get("operator")),
            expected=copy.deepcopy(value.get("expected")),
            expected_from=(
                None
                if expected_from is None
                else PlanIntentResultReference.from_dict(expected_from)
            ),
        )


@dataclass(frozen=True, slots=True)
class PlanResultRule:
    """Restricted postcondition applied to the current step's JSON result."""

    path: tuple[str | int, ...]
    operator: PlanComparisonOperator
    expected: Any = None

    def __post_init__(self) -> None:
        _validate_json_path(self.path, "result rule path")
        _validate_comparison(
            self.operator,
            self.expected,
            None,
            owner="Plan Result Rule",
        )
        object.__setattr__(self, "expected", copy.deepcopy(self.expected))

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": list(self.path),
            "operator": self.operator,
            "expected": copy.deepcopy(self.expected),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PlanResultRule":
        if not isinstance(value, Mapping) or set(value) != {
            "path",
            "operator",
            "expected",
        }:
            raise PlanValidationError("Plan Result Rule 字段无效")
        path = value.get("path")
        if not isinstance(path, list):
            raise PlanValidationError("Plan Result Rule path 必须是数组")
        return cls(
            path=tuple(path),
            operator=_comparison_operator(value.get("operator")),
            expected=copy.deepcopy(value.get("expected")),
        )


@dataclass(frozen=True, slots=True)
class PlanResultContract:
    """Trusted, serializable per-step postcondition contract."""

    rules: tuple[PlanResultRule, ...]

    def __post_init__(self) -> None:
        if not self.rules or any(not isinstance(rule, PlanResultRule) for rule in self.rules):
            raise PlanValidationError("Plan Result Contract 至少需要一条合法规则")

    def to_dict(self) -> dict[str, Any]:
        return {"rules": [rule.to_dict() for rule in self.rules]}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PlanResultContract":
        if not isinstance(value, Mapping) or set(value) != {"rules"}:
            raise PlanValidationError("Plan Result Contract 字段无效")
        rules = value.get("rules")
        if not isinstance(rules, list):
            raise PlanValidationError("Plan Result Contract rules 必须是数组")
        return cls(tuple(PlanResultRule.from_dict(item) for item in rules))


@dataclass(frozen=True, slots=True, init=False)
class PlanParameterContract:
    """可持久化、可哈希的 Plan Step 顶层参数合同。

    合同故意只实现 JSON 类型这一小块稳定语义，而不是接受任意 JSON
    Schema 或 Python 校验 callable。这样配置可以安全地进入 Session 配置
    Hash、Plan Action Hash 和持久化记录，并在另一个 Worker 恢复时得到完全
    相同的校验结果。

    ``allow_empty`` 只用于显式声明“这个危险操作确实没有业务参数”。对于
    ``write``/``never`` Step，默认会拒绝空参数，避免模型生成一个空壳写计划。
    """

    required: tuple[tuple[str, tuple[PlanParameterType, ...]], ...]
    optional: tuple[tuple[str, tuple[PlanParameterType, ...]], ...]
    allow_empty: bool

    def __init__(
        self,
        *,
        required: Mapping[str, str | Sequence[str]] | None = None,
        optional: Mapping[str, str | Sequence[str]] | None = None,
        allow_empty: bool = False,
    ) -> None:
        if not isinstance(allow_empty, bool):
            raise PlanValidationError("Plan 参数合同 allow_empty 必须是布尔值")
        normalized_required = _normalize_parameter_fields(
            required, "Plan 参数合同 required"
        )
        normalized_optional = _normalize_parameter_fields(
            optional, "Plan 参数合同 optional"
        )
        overlap = {name for name, _ in normalized_required} & {
            name for name, _ in normalized_optional
        }
        if overlap:
            raise PlanValidationError(
                "Plan 参数合同 required/optional 字段重复："
                + ", ".join(sorted(overlap))
            )
        object.__setattr__(self, "required", normalized_required)
        object.__setattr__(self, "optional", normalized_optional)
        object.__setattr__(self, "allow_empty", allow_empty)

    @property
    def contract_hash(self) -> str:
        """返回不依赖 Python 进程随机种子的稳定内容摘要。"""

        return _digest(self.to_dict())

    def validate(
        self,
        arguments: Mapping[str, Any],
        *,
        owner: str = "Plan Step",
        require_non_empty: bool = False,
    ) -> None:
        if not isinstance(arguments, Mapping):
            raise PlanValidationError(f"{owner} arguments 必须是对象")
        names = set(arguments)
        if any(not isinstance(name, str) or not name.strip() for name in names):
            raise PlanValidationError(f"{owner} arguments 字段名必须是非空字符串")
        required = dict(self.required)
        optional = dict(self.optional)
        missing = set(required) - names
        if missing:
            raise PlanValidationError(
                f"{owner} 缺少必填参数：{sorted(missing)}"
            )
        unknown = names - set(required) - set(optional)
        if unknown:
            raise PlanValidationError(
                f"{owner} 包含合同之外的参数：{sorted(unknown)}"
            )
        if require_non_empty and not arguments and not self.allow_empty:
            raise PlanValidationError(
                f"{owner} 是 write/never 操作，空参数必须由可信合同显式允许"
            )
        declarations = {**required, **optional}
        for name, value in arguments.items():
            expected = declarations[name]
            if not any(_matches_parameter_type(value, item) for item in expected):
                raise PlanValidationError(
                    f"{owner} 参数 {name} 类型错误；期望 {'|'.join(expected)}"
                )

    def validate_unresolved(
        self,
        arguments: Mapping[str, Any],
        bound_fields: Sequence[str],
        *,
        owner: str = "Plan Step",
        require_non_empty: bool = False,
    ) -> None:
        """Validate a plan before dependency-backed arguments have values.

        Binding targets are checked as field declarations now and their concrete
        JSON types are checked again by :meth:`validate` immediately before
        dispatch.  A target cannot also appear as a literal argument.
        """

        if not isinstance(arguments, Mapping):
            raise PlanValidationError(f"{owner} arguments 必须是对象")
        fields = tuple(bound_fields)
        if any(not isinstance(name, str) or not name.strip() for name in fields):
            raise PlanValidationError(f"{owner} binding target 必须是非空字符串")
        if len(fields) != len(set(fields)):
            raise PlanValidationError(f"{owner} binding target 不能重复")
        overlap = set(arguments) & set(fields)
        if overlap:
            raise PlanValidationError(
                f"{owner} 参数不能同时是常量和绑定：{sorted(overlap)}"
            )
        marker = object()
        declarations = {**dict(self.required), **dict(self.optional)}
        combined = {**dict(arguments), **{name: marker for name in fields}}
        missing = set(dict(self.required)) - set(combined)
        if missing:
            raise PlanValidationError(f"{owner} 缺少必填参数：{sorted(missing)}")
        unknown = set(combined) - set(declarations)
        if unknown:
            raise PlanValidationError(
                f"{owner} 包含合同之外的参数：{sorted(unknown)}"
            )
        if require_non_empty and not combined and not self.allow_empty:
            raise PlanValidationError(
                f"{owner} 是 write/never 操作，空参数必须由可信合同显式允许"
            )
        for name, value in arguments.items():
            expected = declarations[name]
            if not any(_matches_parameter_type(value, item) for item in expected):
                raise PlanValidationError(
                    f"{owner} 参数 {name} 类型错误；期望 {'|'.join(expected)}"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "required": _parameter_fields_to_dict(self.required),
            "optional": _parameter_fields_to_dict(self.optional),
            "allowEmpty": self.allow_empty,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PlanParameterContract":
        if not isinstance(value, Mapping) or set(value) != {
            "required",
            "optional",
            "allowEmpty",
        }:
            raise PlanValidationError(
                "Plan 参数合同字段不完整或包含未知字段"
            )
        required = value.get("required")
        optional = value.get("optional")
        allow_empty = value.get("allowEmpty")
        if not isinstance(required, Mapping) or not isinstance(optional, Mapping):
            raise PlanValidationError("Plan 参数合同 required/optional 必须是对象")
        if not isinstance(allow_empty, bool):
            raise PlanValidationError("Plan 参数合同 allowEmpty 必须是布尔值")
        return cls(
            required=dict(required),
            optional=dict(optional),
            allow_empty=allow_empty,
        )


@dataclass(frozen=True, slots=True)
class IntentPlanPolicy:
    """由可信业务配置提供，不能由规划模型覆盖。"""

    intent: str
    requires_approval: bool = False
    write: bool = False
    replay_policy: PlanReplayPolicy = "safe"
    capabilities: tuple[str, ...] = ()
    approval_roles: tuple[str, ...] = ()
    parameter_contract: PlanParameterContract | None = None
    required_predecessor_intents: tuple[str, ...] = ()
    argument_bindings: tuple[PlanIntentArgumentBinding, ...] = ()
    preconditions: tuple[PlanIntentCondition, ...] = ()
    allow_parallel_side_effects: bool = False
    result_contract: PlanResultContract | None = None

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
        _validate_approval_roles(
            self.approval_roles,
            requires_approval=self.requires_approval,
            owner="Intent Policy",
        )
        if self.parameter_contract is not None and not isinstance(
            self.parameter_contract, PlanParameterContract
        ):
            raise PlanValidationError(
                "Intent Policy parameter_contract 必须是 PlanParameterContract 或 None"
            )
        _validate_unique_texts(
            self.required_predecessor_intents,
            "Intent Policy required_predecessor_intents",
        )
        if any(
            not isinstance(binding, PlanIntentArgumentBinding)
            for binding in self.argument_bindings
        ):
            raise PlanValidationError("Intent Policy argument_bindings 类型无效")
        targets = [binding.target for binding in self.argument_bindings]
        if len(targets) != len(set(targets)):
            raise PlanValidationError("Intent Policy argument binding target 不能重复")
        if any(
            not isinstance(condition, PlanIntentCondition)
            for condition in self.preconditions
        ):
            raise PlanValidationError("Intent Policy preconditions 类型无效")
        if not isinstance(self.allow_parallel_side_effects, bool):
            raise PlanValidationError("allow_parallel_side_effects 必须是布尔值")
        if self.allow_parallel_side_effects and not (
            self.write or self.replay_policy == "never"
        ):
            raise PlanValidationError("只有副作用 Intent 可以允许副作用并行")
        if self.result_contract is not None and not isinstance(
            self.result_contract, PlanResultContract
        ):
            raise PlanValidationError("Intent Policy result_contract 类型无效")

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "requiresApproval": self.requires_approval,
            "write": self.write,
            "replayPolicy": self.replay_policy,
            "capabilities": list(self.capabilities),
            "approvalRoles": list(self.approval_roles),
            "parameterContract": (
                None
                if self.parameter_contract is None
                else self.parameter_contract.to_dict()
            ),
            "requiredPredecessorIntents": list(self.required_predecessor_intents),
            "argumentBindings": [item.to_dict() for item in self.argument_bindings],
            "preconditions": [item.to_dict() for item in self.preconditions],
            "allowParallelSideEffects": self.allow_parallel_side_effects,
            "resultContract": (
                None if self.result_contract is None else self.result_contract.to_dict()
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "IntentPlanPolicy":
        required_fields = {
            "intent",
            "requiresApproval",
            "write",
            "replayPolicy",
            "capabilities",
        }
        optional_fields = {
            "approvalRoles",
            "parameterContract",
            "requiredPredecessorIntents",
            "argumentBindings",
            "preconditions",
            "allowParallelSideEffects",
            "resultContract",
        }
        if (
            not isinstance(value, Mapping)
            or not required_fields.issubset(value)
            or set(value) - required_fields - optional_fields
        ):
            raise PlanValidationError(
                "Intent Plan Policy 字段不完整或包含未知字段"
            )
        capabilities = value.get("capabilities")
        approval_roles = value.get("approvalRoles", [])
        requires_approval = value.get("requiresApproval")
        write = value.get("write")
        raw_contract = value.get("parameterContract")
        predecessor_intents = value.get("requiredPredecessorIntents", [])
        argument_bindings = value.get("argumentBindings", [])
        preconditions = value.get("preconditions", [])
        allow_parallel = value.get("allowParallelSideEffects", False)
        result_contract = value.get("resultContract")
        if not isinstance(capabilities, list) or any(
            not isinstance(item, str) or not item.strip() for item in capabilities
        ):
            raise PlanValidationError("Intent Plan Policy capabilities 必须是字符串数组")
        if not isinstance(approval_roles, list) or any(
            not isinstance(item, str) or not item.strip() for item in approval_roles
        ):
            raise PlanValidationError("Intent Plan Policy approvalRoles 必须是字符串数组")
        if not isinstance(requires_approval, bool) or not isinstance(write, bool):
            raise PlanValidationError("Intent Plan Policy 策略字段必须是布尔值")
        if raw_contract is not None and not isinstance(raw_contract, Mapping):
            raise PlanValidationError("Intent Plan Policy parameterContract 必须是对象或 null")
        if not isinstance(predecessor_intents, list) or any(
            not isinstance(item, str) or not item.strip()
            for item in predecessor_intents
        ):
            raise PlanValidationError(
                "Intent Plan Policy requiredPredecessorIntents 必须是字符串数组"
            )
        if not isinstance(argument_bindings, list) or any(
            not isinstance(item, Mapping) for item in argument_bindings
        ):
            raise PlanValidationError("Intent Plan Policy argumentBindings 必须是对象数组")
        if not isinstance(preconditions, list) or any(
            not isinstance(item, Mapping) for item in preconditions
        ):
            raise PlanValidationError("Intent Plan Policy preconditions 必须是对象数组")
        if not isinstance(allow_parallel, bool):
            raise PlanValidationError("Intent Plan Policy allowParallelSideEffects 必须是布尔值")
        if result_contract is not None and not isinstance(result_contract, Mapping):
            raise PlanValidationError("Intent Plan Policy resultContract 必须是对象或 null")
        return cls(
            intent=_text(value.get("intent"), "intent"),
            requires_approval=requires_approval,
            write=write,
            replay_policy=_text(value.get("replayPolicy"), "replayPolicy"),  # type: ignore[arg-type]
            capabilities=tuple(capabilities),
            approval_roles=tuple(approval_roles),
            parameter_contract=(
                None
                if raw_contract is None
                else PlanParameterContract.from_dict(raw_contract)
            ),
            required_predecessor_intents=tuple(predecessor_intents),
            argument_bindings=tuple(
                PlanIntentArgumentBinding.from_dict(item)
                for item in argument_bindings
            ),
            preconditions=tuple(
                PlanIntentCondition.from_dict(item) for item in preconditions
            ),
            allow_parallel_side_effects=allow_parallel,
            result_contract=(
                None
                if result_contract is None
                else PlanResultContract.from_dict(result_contract)
            ),
        )


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
    approval_roles: tuple[str, ...] = ()
    parameter_contract: PlanParameterContract | None = None
    required_predecessor_intents: tuple[str, ...] = ()
    argument_bindings: tuple[PlanArgumentBinding, ...] = ()
    preconditions: tuple[PlanCondition, ...] = ()
    allow_parallel_side_effects: bool = False
    result_contract: PlanResultContract | None = None
    # Runtime-only override created after dependency bindings have been resolved.
    # It is absent from Plan serialization, so a planner/model cannot supply it.
    _execution_action_hash: str | None = field(
        default=None,
        repr=False,
        compare=False,
    )

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
        _validate_approval_roles(
            self.approval_roles,
            requires_approval=self.requires_approval,
            owner="Plan Step",
        )
        if self.parameter_contract is not None and not isinstance(
            self.parameter_contract, PlanParameterContract
        ):
            raise PlanValidationError(
                "Plan Step parameter_contract 必须是 PlanParameterContract 或 None"
            )
        _validate_unique_texts(
            self.required_predecessor_intents,
            "Plan Step required_predecessor_intents",
        )
        if any(
            not isinstance(binding, PlanArgumentBinding)
            for binding in self.argument_bindings
        ):
            raise PlanValidationError("Plan Step argument_bindings 类型无效")
        targets = [binding.target for binding in self.argument_bindings]
        if len(targets) != len(set(targets)):
            raise PlanValidationError("Plan Step argument binding target 不能重复")
        if set(targets) & set(self.arguments):
            raise PlanValidationError("Plan Step 参数不能同时是常量和依赖绑定")
        if any(not isinstance(item, PlanCondition) for item in self.preconditions):
            raise PlanValidationError("Plan Step preconditions 类型无效")
        if not isinstance(self.allow_parallel_side_effects, bool):
            raise PlanValidationError("Plan Step allow_parallel_side_effects 必须是布尔值")
        if self.allow_parallel_side_effects and not (
            self.write or self.replay_policy == "never"
        ):
            raise PlanValidationError("只有副作用 Step 可以允许副作用并行")
        if self.result_contract is not None and not isinstance(
            self.result_contract, PlanResultContract
        ):
            raise PlanValidationError("Plan Step result_contract 类型无效")
        if self._execution_action_hash is not None:
            _sha256(
                self._execution_action_hash,
                "Plan Step execution action hash",
            )
        _strict_json(dict(self.arguments), "arguments")
        object.__setattr__(self, "arguments", copy.deepcopy(dict(self.arguments)))

    @property
    def action_hash(self) -> str:
        if self._execution_action_hash is not None:
            return self._execution_action_hash
        value = {
            "stepId": self.step_id,
            "intent": self.intent,
            "arguments": dict(self.arguments),
            "dependencies": list(self.depends_on),
            "requiresApproval": self.requires_approval,
            "write": self.write,
            "replayPolicy": self.replay_policy,
            "capabilities": list(self.capabilities),
        }
        # Keep hashes of legacy read-only plans stable. Approval plans are
        # intentionally re-bound to the newly trusted role policy.
        if self.approval_roles:
            value["approvalRoles"] = list(self.approval_roles)
        if self.parameter_contract is not None:
            value["parameterContract"] = self.parameter_contract.to_dict()
        if self.required_predecessor_intents:
            value["requiredPredecessorIntents"] = list(
                self.required_predecessor_intents
            )
        if self.argument_bindings:
            value["argumentBindings"] = [
                binding.to_dict() for binding in self.argument_bindings
            ]
        if self.preconditions:
            value["preconditions"] = [
                condition.to_dict() for condition in self.preconditions
            ]
        if self.allow_parallel_side_effects:
            value["allowParallelSideEffects"] = True
        if self.result_contract is not None:
            value["resultContract"] = self.result_contract.to_dict()
        return _digest(value)

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
            "approvalRoles": list(self.approval_roles),
            "parameterContract": (
                None
                if self.parameter_contract is None
                else self.parameter_contract.to_dict()
            ),
            "requiredPredecessorIntents": list(self.required_predecessor_intents),
            "argumentBindings": [item.to_dict() for item in self.argument_bindings],
            "preconditions": [item.to_dict() for item in self.preconditions],
            "allowParallelSideEffects": self.allow_parallel_side_effects,
            "resultContract": (
                None if self.result_contract is None else self.result_contract.to_dict()
            ),
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
        required_step_fields = {
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
        optional_step_fields = {
            "approvalRoles",
            "parameterContract",
            "requiredPredecessorIntents",
            "argumentBindings",
            "preconditions",
            "allowParallelSideEffects",
            "resultContract",
        }
        for index, raw in enumerate(raw_steps, start=1):
            if (
                not isinstance(raw, Mapping)
                or not required_step_fields.issubset(raw)
                or set(raw) - (required_step_fields | optional_step_fields)
            ):
                raise PlanValidationError(
                    f"Persisted Plan Step #{index} 字段不完整或包含未知字段"
                )
            arguments = raw.get("arguments")
            depends_on = raw.get("dependsOn")
            capabilities = raw.get("capabilities")
            approval_roles = raw.get("approvalRoles", [])
            raw_contract = raw.get("parameterContract")
            predecessor_intents = raw.get("requiredPredecessorIntents", [])
            argument_bindings = raw.get("argumentBindings", [])
            preconditions = raw.get("preconditions", [])
            allow_parallel = raw.get("allowParallelSideEffects", False)
            result_contract = raw.get("resultContract")
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
            if not isinstance(approval_roles, list) or any(
                not isinstance(item, str) or not item.strip() for item in approval_roles
            ):
                raise PlanValidationError("Persisted Plan Step approvalRoles 必须是字符串数组")
            if not isinstance(requires_approval, bool) or not isinstance(write, bool):
                raise PlanValidationError("Persisted Plan Step 策略字段必须是布尔值")
            if raw_contract is not None and not isinstance(raw_contract, Mapping):
                raise PlanValidationError(
                    "Persisted Plan Step parameterContract 必须是对象或 null"
                )
            if not isinstance(predecessor_intents, list) or any(
                not isinstance(item, str) or not item.strip()
                for item in predecessor_intents
            ):
                raise PlanValidationError(
                    "Persisted Plan Step requiredPredecessorIntents 必须是字符串数组"
                )
            if not isinstance(argument_bindings, list) or any(
                not isinstance(item, Mapping) for item in argument_bindings
            ):
                raise PlanValidationError(
                    "Persisted Plan Step argumentBindings 必须是对象数组"
                )
            if not isinstance(preconditions, list) or any(
                not isinstance(item, Mapping) for item in preconditions
            ):
                raise PlanValidationError(
                    "Persisted Plan Step preconditions 必须是对象数组"
                )
            if not isinstance(allow_parallel, bool):
                raise PlanValidationError(
                    "Persisted Plan Step allowParallelSideEffects 必须是布尔值"
                )
            if result_contract is not None and not isinstance(result_contract, Mapping):
                raise PlanValidationError(
                    "Persisted Plan Step resultContract 必须是对象或 null"
                )
            step = PlanStep(
                step_id=_text(raw.get("stepId"), "stepId"),
                intent=_text(raw.get("intent"), "intent"),
                arguments=dict(arguments),
                depends_on=tuple(depends_on),
                requires_approval=requires_approval,
                write=write,
                replay_policy=_text(raw.get("replayPolicy"), "replayPolicy"),  # type: ignore[arg-type]
                capabilities=tuple(capabilities),
                approval_roles=tuple(approval_roles),
                parameter_contract=(
                    None
                    if raw_contract is None
                    else PlanParameterContract.from_dict(raw_contract)
                ),
                required_predecessor_intents=tuple(predecessor_intents),
                argument_bindings=tuple(
                    PlanArgumentBinding.from_dict(item)
                    for item in argument_bindings
                ),
                preconditions=tuple(
                    PlanCondition.from_dict(item) for item in preconditions
                ),
                allow_parallel_side_effects=allow_parallel,
                result_contract=(
                    None
                    if result_contract is None
                    else PlanResultContract.from_dict(result_contract)
                ),
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
class PlanApprovalReceipt:
    """Approval-service proof bound to one exact Plan step.

    A resolver may return an unconsumed receipt, but only a trusted receipt
    consumer may add ``consumed_at``.  The framework validates the returned
    immutable binding before a write step can be started.
    """

    approval_id: str
    receipt_id: str
    plan_id: str
    step_id: str
    action_hash: str
    state_version: int
    approver_id: str
    approver_roles: frozenset[str]
    approver_issuer: str
    identity_verification_id: str
    verification_id: str
    issued_at: float
    expires_at: float
    consumed_at: float | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("approval_id", self.approval_id),
            ("receipt_id", self.receipt_id),
            ("plan_id", self.plan_id),
            ("step_id", self.step_id),
            ("action_hash", self.action_hash),
            ("approver_id", self.approver_id),
            ("approver_issuer", self.approver_issuer),
            ("identity_verification_id", self.identity_verification_id),
            ("verification_id", self.verification_id),
        ):
            _non_empty(value, f"PlanApprovalReceipt.{name}")
        _sha256(self.action_hash, "PlanApprovalReceipt.action_hash")
        if (
            isinstance(self.state_version, bool)
            or not isinstance(self.state_version, int)
            or self.state_version < 0
        ):
            raise PlanValidationError(
                "PlanApprovalReceipt.state_version 必须是非负整数"
            )
        roles = frozenset(self.approver_roles)
        if not roles or any(not isinstance(role, str) or not role.strip() for role in roles):
            raise PlanValidationError(
                "PlanApprovalReceipt.approver_roles 必须包含非空可信角色"
            )
        object.__setattr__(self, "approver_roles", roles)
        issued_at = _finite_timestamp(self.issued_at, "issued_at")
        expires_at = _finite_timestamp(self.expires_at, "expires_at")
        if issued_at >= expires_at:
            raise PlanValidationError(
                "PlanApprovalReceipt 时间必须满足 issued_at < expires_at"
            )
        if self.consumed_at is not None:
            consumed_at = _finite_timestamp(self.consumed_at, "consumed_at")
            if consumed_at < issued_at or consumed_at > expires_at:
                raise PlanValidationError(
                    "PlanApprovalReceipt 消费时间必须位于有效期内"
                )

    @classmethod
    def issue(
        cls,
        *,
        approval_id: str,
        receipt_id: str,
        plan_id: str,
        step: "PlanStep",
        state_version: int,
        approver: "VerifiedIdentity",
        verification_id: str,
        issued_at: float,
        expires_at: float,
    ) -> "PlanApprovalReceipt":
        return cls(
            approval_id=approval_id,
            receipt_id=receipt_id,
            plan_id=plan_id,
            step_id=step.step_id,
            action_hash=step.action_hash,
            state_version=state_version,
            approver_id=approver.principal_id,
            approver_roles=approver.roles,
            approver_issuer=approver.issuer,
            identity_verification_id=approver.verification_id,
            verification_id=verification_id,
            issued_at=issued_at,
            expires_at=expires_at,
        )

    def consumed(self, consumed_at: float) -> "PlanApprovalReceipt":
        """Return the immutable consumed snapshot (trusted consumers only)."""

        return PlanApprovalReceipt(
            approval_id=self.approval_id,
            receipt_id=self.receipt_id,
            plan_id=self.plan_id,
            step_id=self.step_id,
            action_hash=self.action_hash,
            state_version=self.state_version,
            approver_id=self.approver_id,
            approver_roles=self.approver_roles,
            approver_issuer=self.approver_issuer,
            identity_verification_id=self.identity_verification_id,
            verification_id=self.verification_id,
            issued_at=self.issued_at,
            expires_at=self.expires_at,
            consumed_at=consumed_at,
        )

    @property
    def authorization_binding(self) -> tuple[Any, ...]:
        return (
            self.approval_id,
            self.receipt_id,
            self.plan_id,
            self.step_id,
            self.action_hash,
            self.state_version,
            self.approver_id,
            tuple(sorted(self.approver_roles)),
            self.approver_issuer,
            self.identity_verification_id,
            self.verification_id,
            self.issued_at,
            self.expires_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "approvalId": self.approval_id,
            "receiptId": self.receipt_id,
            "planId": self.plan_id,
            "stepId": self.step_id,
            "actionHash": self.action_hash,
            "stateVersion": self.state_version,
            "approverId": self.approver_id,
            "approverRoles": sorted(self.approver_roles),
            "approverIssuer": self.approver_issuer,
            "identityVerificationId": self.identity_verification_id,
            "verificationId": self.verification_id,
            "issuedAt": self.issued_at,
            "expiresAt": self.expires_at,
            "consumedAt": self.consumed_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PlanApprovalReceipt":
        expected = {
            "approvalId",
            "receiptId",
            "planId",
            "stepId",
            "actionHash",
            "stateVersion",
            "approverId",
            "approverRoles",
            "approverIssuer",
            "identityVerificationId",
            "verificationId",
            "issuedAt",
            "expiresAt",
            "consumedAt",
        }
        if set(value) != expected:
            raise PlanValidationError(
                "PlanApprovalReceipt 字段不完整或包含未知字段"
            )
        roles = value.get("approverRoles")
        if not isinstance(roles, list) or any(
            not isinstance(role, str) or not role.strip() for role in roles
        ):
            raise PlanValidationError(
                "PlanApprovalReceipt.approverRoles 必须是字符串数组"
            )
        consumed_at = value.get("consumedAt")
        return cls(
            approval_id=_text(value.get("approvalId"), "approvalId"),
            receipt_id=_text(value.get("receiptId"), "receiptId"),
            plan_id=_text(value.get("planId"), "planId"),
            step_id=_text(value.get("stepId"), "stepId"),
            action_hash=_text(value.get("actionHash"), "actionHash"),
            state_version=_integer(value.get("stateVersion"), "stateVersion"),
            approver_id=_text(value.get("approverId"), "approverId"),
            approver_roles=frozenset(roles),
            approver_issuer=_text(value.get("approverIssuer"), "approverIssuer"),
            identity_verification_id=_text(
                value.get("identityVerificationId"), "identityVerificationId"
            ),
            verification_id=_text(value.get("verificationId"), "verificationId"),
            issued_at=_finite_timestamp(value.get("issuedAt"), "issuedAt"),
            expires_at=_finite_timestamp(value.get("expiresAt"), "expiresAt"),
            consumed_at=(
                None
                if consumed_at is None
                else _finite_timestamp(consumed_at, "consumedAt")
            ),
        )


@dataclass(frozen=True, slots=True)
class PlanApprovalDecision:
    """Approval 的严格三态结果。

    ``approved=None`` 表示审批请求已经创建、但尚无最终决定。保留
    ``approved`` 字段是为了兼容原有 resolver；执行器和 Barrier 必须通过
    ``status`` 区分 pending/approved/denied，不能再把 falsy 当作拒绝。
    """

    approved: bool | None
    action_hash: str
    approval_id: str | None = None
    reason: str | None = None
    receipt: PlanApprovalReceipt | None = None

    @property
    def status(self) -> Literal["pending", "approved", "denied"]:
        if self.approved is None:
            return "pending"
        return "approved" if self.approved else "denied"

    @classmethod
    def grant(
        cls,
        step: PlanStep,
        approval: str | PlanApprovalReceipt,
    ) -> "PlanApprovalDecision":
        """Create a grant decision.

        Passing a string remains source-compatible, but represents an unsafe
        legacy boolean decision and is rejected by ``ApprovalBarrier``.  New
        code must pass a verifiable receipt.
        """

        if isinstance(approval, PlanApprovalReceipt):
            return cls(
                True,
                step.action_hash,
                approval_id=approval.approval_id,
                receipt=approval,
            )
        return cls(True, step.action_hash, approval_id=approval)

    @classmethod
    def pending(
        cls,
        step: PlanStep,
        approval_id: str,
    ) -> "PlanApprovalDecision":
        """表示真实审批请求已创建，后续恢复必须再次查询同一 ID。"""

        return cls(None, step.action_hash, approval_id=approval_id)

    @classmethod
    def deny(
        cls,
        step: PlanStep,
        reason: str,
        *,
        approval_id: str | None = None,
    ) -> "PlanApprovalDecision":
        return cls(
            False,
            step.action_hash,
            approval_id=approval_id,
            reason=reason,
        )

    def __post_init__(self) -> None:
        _non_empty(self.action_hash, "action_hash")
        if self.approved is not None and not isinstance(self.approved, bool):
            raise PlanValidationError("Approval 决定 approved 必须是 bool 或 None")
        if self.approved is None:
            if self.approval_id is None or not self.approval_id.strip():
                raise PlanValidationError("pending 决定必须携带 Approval ID")
            if self.reason is not None or self.receipt is not None:
                raise PlanValidationError("pending 决定不能携带原因或 Approval Receipt")
        elif self.approved:
            if self.approval_id is None or not self.approval_id.strip():
                raise PlanValidationError("批准决定必须携带 Approval ID")
            if self.receipt is not None:
                if self.receipt.approval_id != self.approval_id:
                    raise PlanValidationError("批准决定与 Receipt Approval ID 不匹配")
                if self.receipt.action_hash != self.action_hash:
                    raise PlanValidationError("批准决定与 Receipt Action Hash 不匹配")
            if self.reason is not None:
                raise PlanValidationError("批准决定不能携带拒绝原因")
        else:
            if self.reason is None or not self.reason.strip():
                raise PlanValidationError("拒绝决定必须携带原因")
            if self.approval_id is not None and not self.approval_id.strip():
                raise PlanValidationError("拒绝决定 Approval ID 不能为空")
            if self.receipt is not None:
                raise PlanValidationError("拒绝决定不能携带 Approval Receipt")


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
    approval_receipt: PlanApprovalReceipt | None = None
    validation_status: Literal["passed", "failed"] | None = None
    # Kept last so existing positional construction remains source-compatible.
    approval_action_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = {
            "status": self.status,
            "attempts": self.attempts,
            "approvalId": self.approval_id,
            "approvalActionHash": self.approval_action_hash,
            "approvalReceipt": (
                None
                if self.approval_receipt is None
                else self.approval_receipt.to_dict()
            ),
            "result": copy.deepcopy(self.result),
            "error": self.error,
            "validationStatus": self.validation_status,
        }
        _strict_json(value, "step state")
        return value


@dataclass(frozen=True, slots=True)
class PlanStepExecutionContext:
    """Trusted dispatch input assembled from durable predecessor results."""

    plan_id: str
    step: PlanStep
    resolved_arguments: Mapping[str, Any]
    dependency_results: Mapping[str, Any]
    approval_receipt: PlanApprovalReceipt | None = None
    identity: "VerifiedIdentity | None" = None
    fencing_token: int | None = None
    fencing_scope: str | None = None
    authorization_action_hash: str | None = None
    fenced_claim: "ClaimLease | None" = None
    fenced_claim_lease_seconds: float | None = None
    tool_attempt_reserver: Callable[[int], Any | Awaitable[Any]] | None = None

    def __post_init__(self) -> None:
        _non_empty(self.plan_id, "execution context plan_id")
        if not isinstance(self.step, PlanStep):
            raise PlanValidationError("execution context step 类型无效")
        _strict_json(dict(self.resolved_arguments), "resolved arguments")
        _strict_json(dict(self.dependency_results), "dependency results")
        if self.fencing_token is not None and (
            isinstance(self.fencing_token, bool)
            or not isinstance(self.fencing_token, int)
            or self.fencing_token < 1
        ):
            raise PlanValidationError("execution context fencing_token 无效")
        if self.fencing_scope is not None and (
            not isinstance(self.fencing_scope, str) or not self.fencing_scope.strip()
        ):
            raise PlanValidationError("execution context fencing_scope 无效")
        if self.fenced_claim is not None:
            # Runtime import keeps serialized Plan values independent from the
            # concrete Session Store while still validating the trusted object.
            from ..session.operation_store import ClaimLease

            if not isinstance(self.fenced_claim, ClaimLease):
                raise PlanValidationError("execution context fenced_claim 无效")
            if self.fencing_token != self.fenced_claim.fencing_token:
                raise PlanValidationError(
                    "execution context fencing token 与 ClaimLease 不一致"
                )
            if (
                isinstance(self.fenced_claim_lease_seconds, bool)
                or not isinstance(self.fenced_claim_lease_seconds, (int, float))
                or self.fenced_claim_lease_seconds <= 0
            ):
                raise PlanValidationError(
                    "execution context fenced_claim_lease_seconds 无效"
                )
        elif self.fenced_claim_lease_seconds is not None:
            raise PlanValidationError(
                "execution context lease_seconds 只能与 fenced_claim 一起使用"
            )
        if self.tool_attempt_reserver is not None and not callable(
            self.tool_attempt_reserver
        ):
            raise PlanValidationError(
                "execution context tool_attempt_reserver 必须可调用或为 None"
            )
        if self.authorization_action_hash is not None:
            _sha256(
                self.authorization_action_hash,
                "execution context authorization_action_hash",
            )
        object.__setattr__(
            self,
            "resolved_arguments",
            copy.deepcopy(dict(self.resolved_arguments)),
        )
        object.__setattr__(
            self,
            "dependency_results",
            copy.deepcopy(dict(self.dependency_results)),
        )


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
        if statuses and statuses <= {"succeeded", "not_applicable"}:
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
    not_applicable: tuple[tuple[str, str], ...] = ()


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


_PLAN_PARAMETER_TYPES: tuple[PlanParameterType, ...] = (
    "string",
    "integer",
    "number",
    "boolean",
    "object",
    "array",
    "null",
)


def _normalize_parameter_fields(
    fields: Mapping[str, str | Sequence[str]] | None,
    owner: str,
) -> tuple[tuple[str, tuple[PlanParameterType, ...]], ...]:
    if fields is None:
        return ()
    if not isinstance(fields, Mapping):
        raise PlanValidationError(f"{owner} 必须是对象")
    normalized: list[tuple[str, tuple[PlanParameterType, ...]]] = []
    for name, raw_types in fields.items():
        if not isinstance(name, str) or not name.strip():
            raise PlanValidationError(f"{owner} 字段名必须是非空字符串")
        candidates: tuple[str, ...]
        if isinstance(raw_types, str):
            candidates = (raw_types,)
        elif isinstance(raw_types, Sequence):
            candidates = tuple(raw_types)
        else:
            raise PlanValidationError(
                f"{owner}.{name} 类型声明必须是字符串或字符串数组"
            )
        if not candidates or any(
            not isinstance(item, str) or item not in _PLAN_PARAMETER_TYPES
            for item in candidates
        ):
            raise PlanValidationError(
                f"{owner}.{name} 包含不支持的 JSON 类型"
            )
        if len(candidates) != len(set(candidates)):
            raise PlanValidationError(f"{owner}.{name} 类型声明不能重复")
        ordered = tuple(
            item for item in _PLAN_PARAMETER_TYPES if item in candidates
        )
        normalized.append((name, ordered))
    return tuple(sorted(normalized, key=lambda item: item[0]))


def _parameter_fields_to_dict(
    fields: tuple[tuple[str, tuple[PlanParameterType, ...]], ...],
) -> dict[str, str | list[str]]:
    return {
        name: types[0] if len(types) == 1 else list(types)
        for name, types in fields
    }


def _matches_parameter_type(value: Any, expected: PlanParameterType) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        )
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, list)
    return value is None


_PLAN_COMPARISON_OPERATORS: tuple[PlanComparisonOperator, ...] = (
    "eq",
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "in",
    "not_in",
    "truthy",
    "falsy",
)


def _comparison_operator(value: Any) -> PlanComparisonOperator:
    if not isinstance(value, str) or value not in _PLAN_COMPARISON_OPERATORS:
        raise PlanValidationError("Plan comparison operator 无效")
    return value  # type: ignore[return-value]


def _validate_comparison(
    operator: Any,
    expected: Any,
    expected_from: Any,
    *,
    owner: str,
) -> None:
    _comparison_operator(operator)
    if expected_from is not None and not isinstance(
        expected_from,
        (PlanResultReference, PlanIntentResultReference),
    ):
        raise PlanValidationError(f"{owner} expected_from 类型无效")
    if expected_from is not None and expected is not None:
        raise PlanValidationError(
            f"{owner} expected 和 expected_from 不能同时提供"
        )
    if operator in {"truthy", "falsy"} and (
        expected is not None or expected_from is not None
    ):
        raise PlanValidationError(f"{owner} truthy/falsy 不能携带右操作数")
    if operator in {"in", "not_in"} and expected_from is None and not isinstance(
        expected, list
    ):
        raise PlanValidationError(f"{owner} in/not_in 的 expected 必须是数组")
    _strict_json(expected, f"{owner} expected")


def _validate_json_path(path: Any, owner: str) -> None:
    if not isinstance(path, tuple):
        raise PlanValidationError(f"{owner} 必须是 tuple")
    for part in path:
        if isinstance(part, bool) or not isinstance(part, (str, int)):
            raise PlanValidationError(f"{owner} 只能包含字符串或非负整数")
        if isinstance(part, str) and not part:
            raise PlanValidationError(f"{owner} 不能包含空字段名")
        if isinstance(part, int) and part < 0:
            raise PlanValidationError(f"{owner} 数组下标不能为负数")


def _validate_unique_texts(values: tuple[str, ...], owner: str) -> None:
    if not isinstance(values, tuple):
        raise PlanValidationError(f"{owner} 必须是 tuple")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise PlanValidationError(f"{owner} 不能包含空值")
    if len(values) != len(set(values)):
        raise PlanValidationError(f"{owner} 不能重复")


def _validate_approval_roles(
    roles: tuple[str, ...],
    *,
    requires_approval: bool,
    owner: str,
) -> None:
    if not isinstance(roles, tuple):
        raise PlanValidationError(f"{owner} approval_roles 必须是 tuple")
    if any(not isinstance(role, str) or not role.strip() for role in roles):
        raise PlanValidationError(f"{owner} approval_roles 不能包含空值")
    if len(roles) != len(set(roles)):
        raise PlanValidationError(f"{owner} approval_roles 不能重复")
    if requires_approval and not roles:
        raise PlanValidationError(f"{owner} 需要审批时必须配置 approval_roles")
    if not requires_approval and roles:
        raise PlanValidationError(f"{owner} 无需审批时不能配置 approval_roles")


def _sha256(value: str, name: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value.casefold()):
        raise PlanValidationError(f"{name} 必须是 SHA-256 十六进制摘要")


def _finite_timestamp(value: Any, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise PlanValidationError(f"{name} 必须是有限时间戳")
    return float(value)
