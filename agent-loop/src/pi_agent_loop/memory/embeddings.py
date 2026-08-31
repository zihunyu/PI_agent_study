"""Injectable embedding contract plus a deterministic offline fallback."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from .types import MemoryLimitError, MemoryValidationError

_FEATURES = re.compile(r"[^\W_]+", re.UNICODE)
_MAX_BATCH_ITEMS = 128
_MAX_BATCH_TEXT_BYTES = 1_048_576
_MAX_DIMENSIONS = 4096


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Async batch embedding boundary implemented by local or real providers."""

    @property
    def dimensions(self) -> int: ...

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...


class HashingEmbeddingProvider:
    """Deterministic, dependency-free fallback for offline tests and demos.

    This is feature hashing, not a learned semantic model.  Production callers
    should inject a real :class:`EmbeddingProvider` while keeping the same store
    and tenant boundary.
    """

    def __init__(self, *, dimensions: int = 256) -> None:
        if type(dimensions) is not int or not 1 <= dimensions <= _MAX_DIMENSIONS:
            raise MemoryValidationError("dimensions 必须是 1 到 4096 的整数")
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if not isinstance(texts, Sequence) or isinstance(texts, str | bytes):
            raise MemoryValidationError("embedding texts 必须是字符串序列")
        if not texts or len(texts) > _MAX_BATCH_ITEMS:
            raise MemoryLimitError("embedding batch 必须为 1 到 128 条")
        total_bytes = 0
        vectors: list[tuple[float, ...]] = []
        for text in texts:
            if not isinstance(text, str) or not text or "\x00" in text:
                raise MemoryValidationError("embedding text 必须是非空字符串")
            total_bytes += len(text.encode("utf-8"))
            if total_bytes > _MAX_BATCH_TEXT_BYTES:
                raise MemoryLimitError("embedding batch 文本超过 1 MiB 上限")
            vectors.append(self._embed_one(text))
        return tuple(vectors)

    def _embed_one(self, text: str) -> tuple[float, ...]:
        normalized = " ".join(
            unicodedata.normalize("NFKC", text).casefold().split()
        )
        features = [f"word:{item}" for item in _FEATURES.findall(normalized)]
        compact = normalized.replace(" ", "")
        if compact:
            width = min(3, len(compact))
            features.extend(
                f"char:{compact[index : index + width]}"
                for index in range(0, len(compact) - width + 1)
            )
        if not features:
            features = [f"raw:{normalized}"]
        vector = [0.0] * self._dimensions
        for feature in features:
            digest = hashlib.sha256(feature.encode("utf-8")).digest()
            index = int.from_bytes(digest[:8], "big") % self._dimensions
            sign = 1.0 if digest[8] & 1 else -1.0
            vector[index] += sign
        magnitude = math.sqrt(sum(item * item for item in vector))
        if magnitude == 0:
            # Exact collision cancellation is possible with feature hashing.
            digest = hashlib.sha256(normalized.encode("utf-8")).digest()
            vector[int.from_bytes(digest[:8], "big") % self._dimensions] = 1.0
            magnitude = 1.0
        return tuple(item / magnitude for item in vector)


__all__ = ["EmbeddingProvider", "HashingEmbeddingProvider"]
