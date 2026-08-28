"""Approval、Resume、Tool Intent 与 Write 共用的不可变动作信封。"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any


class DurableActionEnvelopeError(ValueError):
    pass


@dataclass(frozen=True, slots=True, eq=False)
class DurableActionEnvelope:
    operation_id: str
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    write_id: str
    version: int = 1

    def __post_init__(self) -> None:
        if self.version != 1:
            raise DurableActionEnvelopeError(
                f"不支持的 Durable Action Envelope 版本：{self.version}"
            )
        for label, value in (
            ("operation_id", self.operation_id),
            ("tool_call_id", self.tool_call_id),
            ("tool_name", self.tool_name),
            ("write_id", self.write_id),
        ):
            if not isinstance(value, str) or not value:
                raise DurableActionEnvelopeError(f"{label} 不能为空")
        if not isinstance(self.arguments, dict):
            raise DurableActionEnvelopeError("arguments 必须是对象")
        _validate_json_value(self.arguments, "arguments")
        object.__setattr__(self, "arguments", copy.deepcopy(self.arguments))

    def __eq__(self, other: object) -> bool:
        """按 JSON 类型严格比较，避免 Python 将 ``True`` 当成 ``1``。"""

        return isinstance(other, DurableActionEnvelope) and strict_json_equal(
            self.to_dict(),
            other.to_dict(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "operationId": self.operation_id,
            "toolCallId": self.tool_call_id,
            "toolName": self.tool_name,
            "arguments": copy.deepcopy(self.arguments),
            "writeId": self.write_id,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "DurableActionEnvelope":
        if not isinstance(value, dict):
            raise DurableActionEnvelopeError("Durable Action Envelope 必须是对象")
        version = value.get("version", 1)
        if type(version) is not int:
            raise DurableActionEnvelopeError("Envelope version 必须是整数")
        arguments = value.get("arguments")
        if not isinstance(arguments, dict):
            raise DurableActionEnvelopeError("Envelope arguments 必须是对象")
        return cls(
            version=version,
            operation_id=_text(value, "operationId"),
            tool_call_id=_text(value, "toolCallId"),
            tool_name=_text(value, "toolName"),
            arguments=arguments,
            write_id=_text(value, "writeId"),
        )

    @property
    def action_hash(self) -> str:
        return durable_action_digest(self.to_dict())

    def assert_execution(
        self,
        *,
        operation_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        write_id: str,
    ) -> None:
        actual = DurableActionEnvelope(
            operation_id=operation_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            write_id=write_id,
        )
        if not strict_json_equal(actual.to_dict(), self.to_dict()):
            raise DurableActionEnvelopeError(
                "Approval Action Envelope 与实际执行内容不一致"
            )


def durable_action_digest(value: dict[str, Any]) -> str:
    _validate_json_value(value, "durable action")
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def strict_json_equal(expected: Any, actual: Any) -> bool:
    """比较持久化动作时同时比较值与 JSON 类型。"""

    if type(expected) is not type(actual):
        return False
    if isinstance(expected, dict):
        return expected.keys() == actual.keys() and all(
            strict_json_equal(value, actual[key])
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return len(expected) == len(actual) and all(
            strict_json_equal(left, right)
            for left, right in zip(expected, actual)
        )
    return expected == actual


def _validate_json_value(value: Any, path: str) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if type(value) is int:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise DurableActionEnvelopeError(f"{path} 包含非有限数字")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise DurableActionEnvelopeError(f"{path} 的键必须是字符串")
            _validate_json_value(item, f"{path}.{key}")
        return
    raise DurableActionEnvelopeError(
        f"{path} 包含不可持久化的 JSON 类型：{type(value).__name__}"
    )


def _text(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise DurableActionEnvelopeError(f"Envelope 缺少 {key}")
    return item
