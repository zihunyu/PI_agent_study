"""Validated public types for tenant-scoped semantic memory."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

_ABSOLUTE_MAX_EMBEDDING_DIMENSIONS = 4096
_ABSOLUTE_MAX_RECORD_TEXT_BYTES = 1_048_576
_DEFAULT_SCOPE_COMPONENT_BYTES = 256


class SemanticMemoryError(RuntimeError):
    """Base error for semantic-memory operations."""


class MemoryValidationError(SemanticMemoryError, ValueError):
    """A caller supplied an invalid scope, record, filter, or embedding."""


class MemoryLimitError(MemoryValidationError):
    """A configured hard resource limit was exceeded."""


class MemoryConflictError(SemanticMemoryError):
    """An optimistic update/delete token no longer matches the stored record."""


class MemoryCorruptionError(SemanticMemoryError):
    """Encrypted SQLite state failed integrity or schema validation."""


class MemoryPromptOptInRequiredError(SemanticMemoryError):
    """Prompt rendering was requested without the required explicit opt-in."""


@dataclass(frozen=True, slots=True)
class MemoryLimits:
    """Hard bounds applied by managers and every store implementation."""

    max_records_per_scope: int = 10_000
    max_text_bytes: int = 32_768
    max_metadata_bytes: int = 16_384
    max_provenance_bytes: int = 16_384
    max_query_bytes: int = 16_384
    max_top_k: int = 50
    embedding_dimensions: int = 256
    max_ttl_seconds: int = 365 * 24 * 60 * 60
    max_scope_component_bytes: int = _DEFAULT_SCOPE_COMPONENT_BYTES
    max_source_bytes: int = 512
    max_filter_items: int = 32
    max_context_bytes: int = 16_384

    def __post_init__(self) -> None:
        for name in (
            "max_records_per_scope",
            "max_text_bytes",
            "max_metadata_bytes",
            "max_provenance_bytes",
            "max_query_bytes",
            "max_top_k",
            "embedding_dimensions",
            "max_ttl_seconds",
            "max_scope_component_bytes",
            "max_source_bytes",
            "max_filter_items",
            "max_context_bytes",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise MemoryValidationError(f"{name} 必须是正整数")
        if self.embedding_dimensions > _ABSOLUTE_MAX_EMBEDDING_DIMENSIONS:
            raise MemoryLimitError(
                "embedding_dimensions 超过 4096 维绝对上限"
            )
        if self.max_text_bytes > _ABSOLUTE_MAX_RECORD_TEXT_BYTES:
            raise MemoryLimitError("max_text_bytes 超过 1 MiB 绝对上限")


@dataclass(frozen=True, slots=True)
class MemoryScope:
    """The mandatory isolation boundary for all memory operations."""

    tenant_id: str
    subject_id: str
    namespace: str

    def __post_init__(self) -> None:
        for name in ("tenant_id", "subject_id", "namespace"):
            object.__setattr__(
                self,
                name,
                _bounded_text(
                    getattr(self, name),
                    name,
                    _DEFAULT_SCOPE_COMPONENT_BYTES,
                ),
            )

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.tenant_id, self.subject_id, self.namespace)


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """One encrypted-at-rest semantic memory and its trusted provenance."""

    memory_id: str
    tenant_id: str
    subject_id: str
    namespace: str
    text: str
    embedding: tuple[float, ...] = field(repr=False)
    metadata: Mapping[str, Any]
    source: str
    created_at_ms: int
    updated_at_ms: int
    expires_at_ms: int | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("memory_id", "tenant_id", "subject_id", "namespace"):
            object.__setattr__(
                self,
                name,
                _bounded_text(
                    getattr(self, name),
                    name,
                    _DEFAULT_SCOPE_COMPONENT_BYTES,
                ),
            )
        text = _plain_text(self.text, "text")
        if len(text.encode("utf-8")) > _ABSOLUTE_MAX_RECORD_TEXT_BYTES:
            raise MemoryLimitError("memory text 超过 1 MiB 绝对上限")
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "source", _bounded_text(self.source, "source", 4096))
        _timestamp(self.created_at_ms, "created_at_ms")
        _timestamp(self.updated_at_ms, "updated_at_ms")
        if self.updated_at_ms < self.created_at_ms:
            raise MemoryValidationError("updated_at_ms 不能早于 created_at_ms")
        if self.expires_at_ms is not None:
            _timestamp(self.expires_at_ms, "expires_at_ms")
            if self.expires_at_ms <= self.created_at_ms:
                raise MemoryValidationError("expires_at_ms 必须晚于 created_at_ms")
        object.__setattr__(
            self,
            "embedding",
            validate_embedding(self.embedding),
        )
        object.__setattr__(
            self,
            "metadata",
            freeze_json_mapping(self.metadata, "metadata"),
        )
        object.__setattr__(
            self,
            "provenance",
            freeze_json_mapping(self.provenance, "provenance"),
        )

    @property
    def scope(self) -> MemoryScope:
        return MemoryScope(self.tenant_id, self.subject_id, self.namespace)

    def to_dict(self, *, include_embedding: bool = False) -> dict[str, Any]:
        value: dict[str, Any] = {
            "memoryId": self.memory_id,
            "tenantId": self.tenant_id,
            "subjectId": self.subject_id,
            "namespace": self.namespace,
            "text": self.text,
            "metadata": thaw_json(self.metadata),
            "source": self.source,
            "createdAtMs": self.created_at_ms,
            "updatedAtMs": self.updated_at_ms,
            "expiresAtMs": self.expires_at_ms,
            "provenance": thaw_json(self.provenance),
        }
        if include_embedding:
            value["embedding"] = list(self.embedding)
        return value


@dataclass(frozen=True, slots=True)
class MemorySearchResult:
    record: MemoryRecord
    score: float

    def __post_init__(self) -> None:
        if not isinstance(self.record, MemoryRecord):
            raise MemoryValidationError("record 必须是 MemoryRecord")
        if (
            isinstance(self.score, bool)
            or not isinstance(self.score, int | float)
            or not math.isfinite(float(self.score))
            or not -1.0 <= float(self.score) <= 1.0
        ):
            raise MemoryValidationError("cosine score 必须是 -1 到 1 的有限数字")
        object.__setattr__(self, "score", float(self.score))

    def to_dict(self) -> dict[str, Any]:
        return {"score": self.score, "record": self.record.to_dict()}


def validate_embedding(value: Any, *, dimensions: int | None = None) -> tuple[float, ...]:
    if not isinstance(value, list | tuple) or not value:
        raise MemoryValidationError("embedding 必须是非空数字数组")
    if len(value) > _ABSOLUTE_MAX_EMBEDDING_DIMENSIONS:
        raise MemoryLimitError("embedding 超过 4096 维绝对上限")
    if dimensions is not None and len(value) != dimensions:
        raise MemoryValidationError(
            f"embedding 维度必须为 {dimensions}，实际为 {len(value)}"
        )
    result: list[float] = []
    magnitude = 0.0
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int | float):
            raise MemoryValidationError("embedding 只能包含数字")
        number = float(item)
        if not math.isfinite(number):
            raise MemoryValidationError("embedding 禁止 NaN/Infinity")
        result.append(number)
        magnitude += number * number
    if magnitude <= 0:
        raise MemoryValidationError("embedding 不能是零向量")
    return tuple(result)


def freeze_json_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MemoryValidationError(f"{name} 必须是 JSON 对象")
    frozen = _freeze_json(value, name, depth=0)
    if not isinstance(frozen, Mapping):  # pragma: no cover - guarded above
        raise MemoryValidationError(f"{name} 必须是 JSON 对象")
    return frozen


def thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value


def canonical_json_bytes(value: Any, name: str) -> bytes:
    try:
        return json.dumps(
            thaw_json(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise MemoryValidationError(f"{name} 必须是合法 JSON") from error


def validate_scope_limits(scope: MemoryScope, limits: MemoryLimits) -> None:
    if not isinstance(scope, MemoryScope):
        raise MemoryValidationError("scope 必须是 MemoryScope")
    for name in ("tenant_id", "subject_id", "namespace"):
        if len(getattr(scope, name).encode("utf-8")) > limits.max_scope_component_bytes:
            raise MemoryLimitError(f"scope.{name} 超过字节上限")


def validate_record_payload(
    *,
    scope: MemoryScope,
    memory_id: str,
    text: str,
    embedding: Any,
    metadata: Mapping[str, Any],
    source: str,
    provenance: Mapping[str, Any],
    limits: MemoryLimits,
) -> tuple[str, str, tuple[float, ...], Mapping[str, Any], str, Mapping[str, Any]]:
    validate_scope_limits(scope, limits)
    normalized_id = _bounded_text(
        memory_id,
        "memory_id",
        limits.max_scope_component_bytes,
    )
    normalized_text = _plain_text(text, "text")
    if len(normalized_text.encode("utf-8")) > limits.max_text_bytes:
        raise MemoryLimitError("memory text 超过字节上限")
    normalized_embedding = validate_embedding(
        embedding,
        dimensions=limits.embedding_dimensions,
    )
    normalized_metadata = freeze_json_mapping(metadata, "metadata")
    if len(canonical_json_bytes(normalized_metadata, "metadata")) > limits.max_metadata_bytes:
        raise MemoryLimitError("metadata 超过字节上限")
    normalized_source = _bounded_text(source, "source", limits.max_source_bytes)
    normalized_provenance = freeze_json_mapping(provenance, "provenance")
    if (
        len(canonical_json_bytes(normalized_provenance, "provenance"))
        > limits.max_provenance_bytes
    ):
        raise MemoryLimitError("provenance 超过字节上限")
    return (
        normalized_id,
        normalized_text,
        normalized_embedding,
        normalized_metadata,
        normalized_source,
        normalized_provenance,
    )


def validate_metadata_filter(
    value: Mapping[str, Any] | None,
    limits: MemoryLimits,
) -> Mapping[str, Any]:
    if value is None:
        return MappingProxyType({})
    result = freeze_json_mapping(value, "metadata_filter")
    if len(result) > limits.max_filter_items:
        raise MemoryLimitError("metadata_filter 条目数超过上限")
    if len(canonical_json_bytes(result, "metadata_filter")) > limits.max_metadata_bytes:
        raise MemoryLimitError("metadata_filter 超过字节上限")
    return result


def metadata_matches(metadata: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    return all(
        key in metadata and thaw_json(metadata[key]) == thaw_json(value)
        for key, value in expected.items()
    )


def _freeze_json(value: Any, name: str, *, depth: int) -> Any:
    if depth > 32:
        raise MemoryLimitError(f"{name} JSON 嵌套超过 32 层")
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise MemoryValidationError(f"{name} 禁止 NaN/Infinity")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str or not key or "\x00" in key:
                raise MemoryValidationError(f"{name} JSON key 必须是非空字符串")
            frozen[key] = _freeze_json(item, f"{name}.{key}", depth=depth + 1)
        return MappingProxyType(frozen)
    if isinstance(value, list | tuple):
        return tuple(
            _freeze_json(item, f"{name}[{index}]", depth=depth + 1)
            for index, item in enumerate(value)
        )
    raise MemoryValidationError(
        f"{name} 包含非 JSON 类型：{type(value).__name__}"
    )


def _bounded_text(value: Any, name: str, max_bytes: int) -> str:
    normalized = _plain_text(value, name).strip()
    if not normalized:
        raise MemoryValidationError(f"{name} 不能为空")
    if len(normalized.encode("utf-8")) > max_bytes:
        raise MemoryLimitError(f"{name} 超过字节上限")
    return normalized


def _plain_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MemoryValidationError(f"{name} 必须是非空字符串")
    if "\x00" in value:
        raise MemoryValidationError(f"{name} 禁止 NUL 字符")
    return value


def _timestamp(value: Any, name: str) -> None:
    if type(value) is not int or value < 0:
        raise MemoryValidationError(f"{name} 必须是非负整数毫秒时间戳")


__all__ = [
    "MemoryConflictError",
    "MemoryCorruptionError",
    "MemoryLimitError",
    "MemoryLimits",
    "MemoryPromptOptInRequiredError",
    "MemoryRecord",
    "MemoryScope",
    "MemorySearchResult",
    "MemoryValidationError",
    "SemanticMemoryError",
]
