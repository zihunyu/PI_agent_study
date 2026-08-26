"""业务路由、能力匹配和工具策略的公共类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypeAlias

RequestStatus: TypeAlias = Literal[
    "in_scope_no_tool",
    "in_scope_tool_ready",
    "in_scope_need_clarification",
    "in_scope_capability_missing",
    "in_scope_approval_required",
    "out_of_scope",
    "prohibited",
]
ToolChoiceMode: TypeAlias = Literal["none", "auto", "required", "named"]


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
    extracted_fields: dict[str, str] = field(default_factory=dict)
    missing_fields: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    missing_capabilities: tuple[str, ...] = ()
    selected_tools: tuple[str, ...] = ()
    requires_approval: bool = False
    routing_source: Literal["rule", "model"] = "rule"
    confidence: float | None = None
    tool_policy: ToolChoicePolicy = field(
        default_factory=lambda: ToolChoicePolicy("none")
    )


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
