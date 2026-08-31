"""Opt-in, tenant-isolated semantic memory with encrypted persistence."""

from .embeddings import EmbeddingProvider, HashingEmbeddingProvider
from .manager import MemoryContextProvider, MemoryManager
from .store import InMemoryMemoryStore, MemoryStore, SQLiteMemoryStore
from .types import (
    MemoryConflictError,
    MemoryCorruptionError,
    MemoryLimitError,
    MemoryLimits,
    MemoryPromptOptInRequiredError,
    MemoryRecord,
    MemoryScope,
    MemorySearchResult,
    MemoryValidationError,
    SemanticMemoryError,
)

__all__ = [
    "EmbeddingProvider",
    "HashingEmbeddingProvider",
    "InMemoryMemoryStore",
    "MemoryConflictError",
    "MemoryContextProvider",
    "MemoryCorruptionError",
    "MemoryLimitError",
    "MemoryLimits",
    "MemoryManager",
    "MemoryPromptOptInRequiredError",
    "MemoryRecord",
    "MemoryScope",
    "MemorySearchResult",
    "MemoryStore",
    "MemoryValidationError",
    "SemanticMemoryError",
    "SQLiteMemoryStore",
]
