"""Validated, immutable generation settings for the built-in Provider."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .errors import ProviderConfigError, ProviderProtocolError

GENERATION_FIELDS = frozenset(
    {"max_tokens", "max_completion_tokens", "temperature", "top_p", "stream_options"}
)


@dataclass(frozen=True, slots=True)
class GenerationOptions:
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    include_usage: bool | None = None

    def __post_init__(self) -> None:
        if self.max_tokens is not None and self.max_completion_tokens is not None:
            raise ProviderConfigError(
                "max_tokens 与 max_completion_tokens 不能同时设置"
            )
        for name in ("max_tokens", "max_completion_tokens"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value <= 0):
                raise ProviderConfigError(f"{name} 必须是正整数")
        for name, ceiling in (("temperature", 2), ("top_p", 1)):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 <= value <= ceiling
                or not math.isfinite(value)
            ):
                raise ProviderConfigError(
                    f"{name} 必须是 0 到 {ceiling} 之间的有限数值"
                )
        if self.include_usage is not None and type(self.include_usage) is not bool:
            raise ProviderConfigError("include_usage 必须是布尔值")

    @classmethod
    def from_mapping(cls, value: Any) -> GenerationOptions:
        if not isinstance(value, Mapping):
            raise ProviderConfigError("generation 必须是对象")
        if set(value) - GENERATION_FIELDS:
            raise ProviderConfigError("generation 包含不支持的字段")
        stream = value.get("stream_options", {})
        if not isinstance(stream, Mapping) or set(stream) - {"include_usage"}:
            raise ProviderConfigError("stream_options 只允许 include_usage")
        return cls(
            max_tokens=value.get("max_tokens"),
            max_completion_tokens=value.get("max_completion_tokens"),
            temperature=value.get("temperature"),
            top_p=value.get("top_p"),
            include_usage=stream.get("include_usage"),
        )

    def to_payload(self) -> dict[str, Any]:
        result = {
            name: getattr(self, name)
            for name in ("max_tokens", "max_completion_tokens", "temperature", "top_p")
            if getattr(self, name) is not None
        }
        if self.include_usage is not None:
            result["stream_options"] = {"include_usage": self.include_usage}
        return result

    def resolve(self, overrides: Mapping[str, Any]) -> dict[str, Any]:
        # Runtime options contain other framework fields; only generation keys
        # are eligible for the wire. There is deliberately no raw-body override.
        values = self.to_payload()
        values.update(
            {key: overrides[key] for key in GENERATION_FIELDS if key in overrides}
        )
        try:
            return self.from_mapping(values).to_payload()
        except ProviderConfigError as error:
            raise ProviderProtocolError(str(error)) from None
