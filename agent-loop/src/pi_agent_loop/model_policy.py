"""可持久化的单次模型请求策略快照。"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any


class ModelRequestPolicyError(ValueError):
    """模型请求策略缺失、损坏或无法由当前 Runtime 满足。"""


@dataclass(frozen=True, slots=True)
class ModelRequestPolicy:
    """恢复一次模型请求所需的最小安全策略。

    策略按每次 Model Request 持久化，而不是按整个 Operation 持久化，因为
    required Tool Call 完成后的下一轮通常会切换为 auto/none。
    """

    visible_tool_names: tuple[str, ...] = ()
    tool_choice: str | dict[str, Any] = "none"
    required_capabilities: tuple[str, ...] = ()
    allowed_tool_names: tuple[str, ...] = ()
    expected_tool_arguments: dict[str, Any] = field(default_factory=dict)
    continuation_policy: "ModelRequestPolicy | None" = None
    version: int = 1

    def __post_init__(self) -> None:
        if self.version != 1:
            raise ModelRequestPolicyError(
                f"不支持的 Model Request Policy 版本：{self.version}"
            )
        visible = _names(self.visible_tool_names, "visible_tool_names")
        allowed = _names(self.allowed_tool_names, "allowed_tool_names")
        required = _names(
            self.required_capabilities,
            "required_capabilities",
        )
        if not set(allowed).issubset(visible):
            raise ModelRequestPolicyError(
                "allowed_tool_names 必须是 visible_tool_names 的子集"
            )
        choice = _tool_choice(self.tool_choice)
        named = _named_tool(choice)
        if named is not None and named not in visible:
            raise ModelRequestPolicyError(
                f"强制工具 {named} 不在 visible_tool_names 中"
            )
        if choice == "required" and not visible:
            raise ModelRequestPolicyError(
                "tool_choice=required 时必须至少暴露一个工具"
            )
        arguments = copy.deepcopy(dict(self.expected_tool_arguments))
        continuation = self.continuation_policy
        if continuation is not None and not isinstance(
            continuation,
            ModelRequestPolicy,
        ):
            raise ModelRequestPolicyError(
                "continuation_policy 必须是 ModelRequestPolicy"
            )
        object.__setattr__(self, "visible_tool_names", visible)
        object.__setattr__(self, "allowed_tool_names", allowed)
        object.__setattr__(self, "required_capabilities", required)
        object.__setattr__(self, "tool_choice", choice)
        object.__setattr__(self, "expected_tool_arguments", arguments)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "visibleToolNames": list(self.visible_tool_names),
            "toolChoice": copy.deepcopy(self.tool_choice),
            "requiredCapabilities": list(self.required_capabilities),
            "allowedToolNames": list(self.allowed_tool_names),
            "expectedToolArguments": copy.deepcopy(
                self.expected_tool_arguments
            ),
            "continuationPolicy": (
                self.continuation_policy.to_dict()
                if self.continuation_policy is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ModelRequestPolicy":
        if not isinstance(value, dict):
            raise ModelRequestPolicyError("Model Request Policy 必须是对象")
        return cls(
            version=int(value.get("version", 1)),
            visible_tool_names=_json_names(
                value.get("visibleToolNames", []),
                "visibleToolNames",
            ),
            tool_choice=copy.deepcopy(value.get("toolChoice", "none")),
            required_capabilities=_json_names(
                value.get("requiredCapabilities", []),
                "requiredCapabilities",
            ),
            allowed_tool_names=_json_names(
                value.get("allowedToolNames", []),
                "allowedToolNames",
            ),
            expected_tool_arguments=copy.deepcopy(
                value.get("expectedToolArguments", {})
            ),
            continuation_policy=(
                cls.from_dict(value["continuationPolicy"])
                if value.get("continuationPolicy") is not None
                else None
            ),
        )

    @classmethod
    def no_tools(cls) -> "ModelRequestPolicy":
        return cls(visible_tool_names=(), tool_choice="none")


def validate_model_response_policy(
    message: dict[str, Any],
    policy: ModelRequestPolicy,
) -> None:
    """在 Runtime 边界再次验证可见工具、Tool Choice 和关键参数。"""

    calls = [
        block
        for block in message.get("content", [])
        if isinstance(block, dict) and block.get("type") == "toolCall"
    ]
    called_names = tuple(str(call.get("name", "")) for call in calls)
    unexpected = [
        name for name in called_names if name not in policy.visible_tool_names
    ]
    if unexpected:
        raise ModelRequestPolicyError(
            "模型调用了策略不可见的工具：" + "、".join(unexpected)
        )
    allowed = set(policy.allowed_tool_names)
    if allowed:
        forbidden = [name for name in called_names if name not in allowed]
        if forbidden:
            raise ModelRequestPolicyError(
                "模型调用了当前请求不允许的工具：" + "、".join(forbidden)
            )
    if policy.tool_choice == "none" and calls:
        raise ModelRequestPolicyError("当前模型请求禁止 Tool Call")
    if policy.tool_choice == "required" and not calls:
        raise ModelRequestPolicyError("当前模型请求必须产生 Tool Call")
    named = _named_tool(policy.tool_choice)
    if named is not None and named not in called_names:
        raise ModelRequestPolicyError(f"模型没有调用强制工具：{named}")
    if policy.expected_tool_arguments:
        mismatched = [
            field
            for field, expected in policy.expected_tool_arguments.items()
            if not any(
                isinstance(call.get("arguments"), dict)
                and str(call["arguments"].get(field, "")) == str(expected)
                for call in calls
            )
        ]
        if mismatched:
            raise ModelRequestPolicyError(
                "模型工具调用没有使用持久策略中的参数："
                + "、".join(str(field) for field in mismatched)
            )


def capture_model_request_policy(
    visible_tool_names: list[str] | tuple[str, ...],
    stream_options: dict[str, Any],
) -> ModelRequestPolicy:
    """从当前 Turn 的工具 Context 和 Provider Options 生成策略快照。"""

    visible = tuple(visible_tool_names)
    default_choice = "auto" if visible else "none"
    return ModelRequestPolicy(
        visible_tool_names=visible,
        tool_choice=copy.deepcopy(
            stream_options.get("tool_choice", default_choice)
        ),
        required_capabilities=tuple(
            stream_options.get("required_capabilities", ())
        ),
        allowed_tool_names=tuple(
            stream_options.get("allowed_tool_names", ())
        ),
        expected_tool_arguments=copy.deepcopy(
            stream_options.get("expected_tool_arguments", {})
        ),
        continuation_policy=(
            ModelRequestPolicy.from_dict(
                stream_options["recovery_continuation_policy"]
            )
            if stream_options.get("recovery_continuation_policy") is not None
            else None
        ),
    )


def _names(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if not isinstance(values, tuple) or any(
        not isinstance(value, str) for value in values
    ):
        raise ModelRequestPolicyError(f"{label} 必须是字符串元组")
    normalized = tuple(values)
    if any(not value for value in normalized):
        raise ModelRequestPolicyError(f"{label} 不能包含空名称")
    if len(set(normalized)) != len(normalized):
        raise ModelRequestPolicyError(f"{label} 不能包含重复名称")
    return normalized


def _json_names(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise ModelRequestPolicyError(f"{label} 必须是字符串数组")
    return tuple(value)


def _tool_choice(value: Any) -> str | dict[str, Any]:
    if isinstance(value, str) and value in {"none", "auto", "required"}:
        return value
    if not isinstance(value, dict):
        raise ModelRequestPolicyError("tool_choice 必须是 none/auto/required/named")
    function = value.get("function")
    if (
        value.get("type") != "function"
        or not isinstance(function, dict)
        or not isinstance(function.get("name"), str)
        or not function["name"]
    ):
        raise ModelRequestPolicyError("named tool_choice 缺少 function.name")
    return copy.deepcopy(value)


def _named_tool(value: str | dict[str, Any]) -> str | None:
    if not isinstance(value, dict):
        return None
    return str(value["function"]["name"])
