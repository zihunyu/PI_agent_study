"""业务路由、能力匹配和工具策略的公共类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Literal, TypeAlias, cast

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

RequestStatus: TypeAlias = Literal[
    "in_scope_no_tool",
    "in_scope_tool_ready",
    "in_scope_plan_required",
    "in_scope_need_clarification",
    "in_scope_capability_missing",
    "in_scope_approval_required",
    "permission_denied",
    "out_of_scope",
    "prohibited",
]
ToolChoiceMode: TypeAlias = Literal["none", "auto", "required", "named"]
RiskLevel: TypeAlias = Literal["low", "medium", "high", "critical"]

_RISK_ORDER: dict[str, int] = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "critical": 3,
}


def validate_risk_level(value: str) -> RiskLevel:
    """Validate and normalize one public Router risk level."""

    normalized = value.strip().casefold() if isinstance(value, str) else ""
    if normalized not in _RISK_ORDER:
        raise ValueError("risk 必须是 low、medium、high 或 critical")
    return cast(RiskLevel, normalized)


def highest_risk_level(values: tuple[str, ...] | list[str]) -> RiskLevel:
    """Return the highest validated risk without relying on lexical order."""

    if not values:
        return "low"
    normalized = [validate_risk_level(value) for value in values]
    return max(normalized, key=lambda item: _RISK_ORDER[item])


@dataclass(frozen=True, slots=True)
class ToolChoicePolicy:
    """一次模型请求的工具选择策略。"""

    mode: ToolChoiceMode
    tool_name: str | None = None

    def __post_init__(self) -> None:
        if self.mode == "named":
            if not self.tool_name:
                raise ValueError("named ToolChoicePolicy 必须提供 tool_name")
        elif self.tool_name is not None:
            raise ValueError(f"{self.mode} ToolChoicePolicy 不能提供 tool_name")

    def to_openai(self) -> str | dict:
        if self.mode != "named":
            return self.mode
        return {
            "type": "function",
            "function": {"name": self.tool_name},
        }


@dataclass(frozen=True, slots=True)
class RequestDecision:
    """Router 对一次用户请求的结构化决定。"""

    status: RequestStatus
    reason: str
    message: str
    domain: str | None = None
    intent: str | None = None
    extracted_fields: dict[str, JsonValue] = field(default_factory=dict)
    missing_fields: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    missing_capabilities: tuple[str, ...] = ()
    selected_tools: tuple[str, ...] = ()
    requires_approval: bool = False
    side_effect: bool = False
    risk: RiskLevel = "low"
    task_id: str | None = None
    depends_on: tuple[str, ...] = ()
    routing_source: Literal["rule", "model"] = "rule"
    confidence: float | None = None
    component_decisions: tuple["RequestDecision", ...] = ()
    task_decision: "TaskDecision | None" = None
    tool_policy: ToolChoicePolicy = field(
        default_factory=lambda: ToolChoicePolicy("none")
    )

    def __post_init__(self) -> None:
        normalized_risk = validate_risk_level(self.risk)
        if normalized_risk != self.risk:
            object.__setattr__(self, "risk", normalized_risk)
        if self.task_id is not None and (
            not isinstance(self.task_id, str) or not self.task_id.strip()
        ):
            raise ValueError("task_id 必须是非空字符串或 None")
        if any(not isinstance(item, str) or not item.strip() for item in self.depends_on):
            raise ValueError("depends_on 不能包含空 task_id")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("depends_on 不能重复")


@dataclass(frozen=True, slots=True)
class TaskDependencyHint:
    """Untrusted ordering hint emitted by Router for later Planner validation."""

    task_id: str
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise ValueError("TaskDependencyHint.task_id 必须是非空字符串")
        if any(
            not isinstance(item, str) or not item.strip()
            for item in self.depends_on
        ):
            raise ValueError("TaskDependencyHint.depends_on 不能包含空值")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("TaskDependencyHint.depends_on 不能重复")


@dataclass(frozen=True, slots=True)
class TaskDecision:
    """Structured multi-intent hand-off produced by the Router.

    It is deliberately a decision input, not an executable plan.  A Planner must
    still validate dependencies, authorization, parameters and approvals before
    producing durable steps.
    """

    components: tuple[RequestDecision, ...]
    dependencies: tuple[TaskDependencyHint, ...] = ()
    approval_required_tasks: tuple[str, ...] = ()
    side_effect_tasks: tuple[str, ...] = ()
    clarification_tasks: tuple[str, ...] = ()
    missing_capability_tasks: tuple[str, ...] = ()
    permission_denied_tasks: tuple[str, ...] = ()
    highest_risk: RiskLevel = "low"

    def __post_init__(self) -> None:
        if len(self.components) < 2:
            raise ValueError("TaskDecision 至少需要两个 Intent 组件")
        task_ids = tuple(component.task_id for component in self.components)
        if any(task_id is None for task_id in task_ids):
            raise ValueError("TaskDecision 每个组件都必须包含 task_id")
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("TaskDecision task_id 不能重复")
        known_task_ids = set(task_ids)
        if len(self.dependencies) != len(
            {hint.task_id for hint in self.dependencies}
        ):
            raise ValueError("TaskDecision.dependencies 的 task_id 不能重复")
        for values, label in (
            (self.approval_required_tasks, "approval_required_tasks"),
            (self.side_effect_tasks, "side_effect_tasks"),
            (self.clarification_tasks, "clarification_tasks"),
            (self.missing_capability_tasks, "missing_capability_tasks"),
            (self.permission_denied_tasks, "permission_denied_tasks"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"TaskDecision.{label} 不能重复")
            if any(value not in known_task_ids for value in values):
                raise ValueError(f"TaskDecision.{label} 引用了未知 task_id")
        if any(
            hint.task_id not in known_task_ids
            or any(value not in known_task_ids for value in hint.depends_on)
            for hint in self.dependencies
        ):
            raise ValueError("TaskDecision.dependencies 引用了未知 task_id")
        dependency_map = {
            hint.task_id: hint.depends_on for hint in self.dependencies
        }
        component_dependency_map = {
            cast(str, component.task_id): component.depends_on
            for component in self.components
            if component.depends_on
        }
        if dependency_map != component_dependency_map:
            raise ValueError(
                "TaskDecision.dependencies 与组件 depends_on 不一致"
            )
        normalized_risk = validate_risk_level(self.highest_risk)
        if normalized_risk != self.highest_risk:
            object.__setattr__(self, "highest_risk", normalized_risk)

    @property
    def ready_for_planner(self) -> bool:
        """Whether all components have trusted Intent and capability inputs."""

        return not (
            self.clarification_tasks
            or self.missing_capability_tasks
            or self.permission_denied_tasks
        ) and all(
            component.status
            in {
                "in_scope_no_tool",
                "in_scope_tool_ready",
                "in_scope_approval_required",
            }
            for component in self.components
        )


@dataclass(frozen=True, slots=True)
class RouteAuthorizationContext:
    """Trusted routing facts presented to an application authorization policy.

    ``arguments`` is a detached snapshot: a policy may inspect or even mutate
    nested values without changing the Router decision that can later reach the
    Planner.  Capabilities and tool names have already been resolved through
    the Host-owned Intent catalog and ``CapabilityRegistry``.
    """

    domain: str | None
    intent: str
    arguments: Mapping[str, JsonValue]
    required_capabilities: tuple[str, ...]
    selected_tools: tuple[str, ...]
    requires_approval: bool
    side_effect: bool
    risk: RiskLevel
    task_id: str | None = None


@dataclass(frozen=True, slots=True)
class RouteAuthorizationDecision:
    """Fail-closed result returned by an application authorization policy."""

    allowed: bool
    reason: str = "authorized"
    message: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.allowed, bool):
            raise TypeError("RouteAuthorizationDecision.allowed 必须是 bool")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("RouteAuthorizationDecision.reason 不能为空")
        if not isinstance(self.message, str):
            raise TypeError("RouteAuthorizationDecision.message 必须是字符串")

    @classmethod
    def allow(cls) -> "RouteAuthorizationDecision":
        return cls(True)

    @classmethod
    def deny(
        cls,
        reason: str,
        message: str = "当前身份没有执行该请求的权限。",
    ) -> "RouteAuthorizationDecision":
        return cls(False, reason=reason, message=message)


@dataclass(frozen=True, slots=True)
class ToolGuardViolation:
    """模型响应违反必须使用工具策略。"""

    code: str
    message: str
    required_capabilities: tuple[str, ...]
    called_tools: tuple[str, ...]
    missing_capabilities: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RoutedPromptResult:
    """高层 RoutedAgent 的一次处理结果。"""

    decision: RequestDecision
    model_called: bool
    response_text: str
    error_code: str | None = None
