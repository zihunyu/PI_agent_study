"""可持久化的单次模型请求策略快照。"""

from __future__ import annotations

import copy
from dataclasses import dataclass
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
    # None 表示该请求没有声明参数约束；{} 表示明确要求唯一 Tool Call
    # 的 arguments 必须是空对象。两者必须在持久化往返后保持区别。
    expected_tool_arguments: dict[str, Any] | None = None
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
        if self.expected_tool_arguments is None:
            arguments = None
        elif not isinstance(self.expected_tool_arguments, dict):
            raise ModelRequestPolicyError(
                "expected_tool_arguments 必须是对象或 None"
            )
        else:
            arguments = copy.deepcopy(self.expected_tool_arguments)
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
            "expectedToolArguments": copy.deepcopy(self.expected_tool_arguments),
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
                value.get("expectedToolArguments")
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
    if not tool_calls_match_expected_arguments(
        calls,
        policy.expected_tool_arguments,
    ):
        raise ModelRequestPolicyError(
            "模型必须在单个 Tool Call 中使用与持久策略完全一致的参数，"
            "禁止跨调用拼凑或添加额外参数"
        )


def validate_recoverable_model_response(
    message: dict[str, Any],
    policy: ModelRequestPolicy,
) -> None:
    """验证 Recovery Runtime/Callback 可以安全落盘的成功响应。"""

    stop_reason = _model_stop_reason(message)
    if stop_reason in {"error", "aborted"}:
        raise ModelRequestPolicyError(
            str(message.get("errorMessage", "恢复模型请求失败"))
        )
    if stop_reason == "length":
        raise ModelRequestPolicyError(
            "恢复模型响应达到长度上限，禁止完成 Operation"
        )
    if stop_reason not in {"stop", "toolUse"}:
        raise ModelRequestPolicyError(
            f"恢复模型响应终止原因无效：{stop_reason}"
        )
    calls = _tool_calls(message)
    if stop_reason == "toolUse" and not calls:
        raise ModelRequestPolicyError(
            "模型响应声明 toolUse，但没有 Tool Call"
        )
    if stop_reason == "stop" and calls:
        raise ModelRequestPolicyError(
            "模型响应包含 Tool Call，但 stopReason 不是 toolUse"
        )
    validate_model_response_policy(message, policy)


def validate_persisted_model_response(
    message: dict[str, Any],
    policy: ModelRequestPolicy | None,
) -> None:
    """重放 Model Request Completed 时按该 Request 的策略验证响应。

    error/aborted 是 Recorder 会持久化的失败响应，因此允许重放，但禁止
    其中携带可被 Recovery 误执行的 Tool Call。成功响应则必须拥有且满足
    本次 Request 自身的 Policy；不能借用后续 active policy。
    """

    stop_reason = _model_stop_reason(message)
    calls = _tool_calls(message)
    if stop_reason in {"error", "aborted"}:
        if calls:
            raise ModelRequestPolicyError(
                "失败的模型响应禁止携带可执行 Tool Call"
            )
        return
    if policy is None:
        raise ModelRequestPolicyError(
            "Model Request Completed 缺少该 Request 的持久化策略"
        )
    validate_recoverable_model_response(message, policy)


def tool_calls_match_expected_arguments(
    calls: list[dict[str, Any]],
    expected_arguments: dict[str, Any] | None,
) -> bool:
    """按严格 JSON 类型递归匹配唯一 Tool Call 的完整参数。"""

    if expected_arguments is None:
        return True
    return (
        len(calls) == 1
        and isinstance(calls[0].get("arguments"), dict)
        and _strict_json_equal(
            expected_arguments,
            calls[0]["arguments"],
        )
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
            stream_options.get("expected_tool_arguments")
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


def _model_stop_reason(message: dict[str, Any]) -> Any:
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ModelRequestPolicyError("模型响应必须是 Assistant Message")
    content = message.get("content")
    if not isinstance(content, list):
        raise ModelRequestPolicyError("模型响应 content 必须是数组")
    return message.get("stopReason")


def _tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content", [])
    if not isinstance(content, list):
        raise ModelRequestPolicyError("模型响应 content 必须是数组")
    return [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") == "toolCall"
    ]


def _strict_json_equal(expected: Any, actual: Any) -> bool:
    # Python 的 True == 1；Policy 边界不能采用这种宽松比较。
    if type(expected) is not type(actual):
        return False
    if isinstance(expected, dict):
        if expected.keys() != actual.keys():
            return False
        return all(
            _strict_json_equal(value, actual[key])
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return len(expected) == len(actual) and all(
            _strict_json_equal(left, right)
            for left, right in zip(expected, actual)
        )
    return expected == actual
