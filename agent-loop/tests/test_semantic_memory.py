"""Tenant isolation, encrypted persistence, ranking, TTL, and opt-in prompt tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pi_agent_loop.memory import (
    HashingEmbeddingProvider,
    InMemoryMemoryStore,
    MemoryConflictError,
    MemoryContextProvider,
    MemoryLimitError,
    MemoryLimits,
    MemoryManager,
    MemoryPromptOptInRequiredError,
    MemoryScope,
    MemoryValidationError,
    SQLiteMemoryStore,
)
from pi_agent_loop.session import StaticJournalKeyProvider


class MutableClock:
    def __init__(self, value: int = 1_000_000) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value

    def advance(self, milliseconds: int) -> None:
        self.value += milliseconds


def memory_limits(**overrides) -> MemoryLimits:
    values = {
        "embedding_dimensions": 128,
        "max_records_per_scope": 100,
    }
    values.update(overrides)
    return MemoryLimits(**values)


class SemanticMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_hashing_fallback确定且manager返回来源和provenance(self) -> None:
        limits = memory_limits()
        embeddings = HashingEmbeddingProvider(
            dimensions=limits.embedding_dimensions
        )
        first = await embeddings.embed(("Red apple fruit",))
        second = await embeddings.embed(("Red apple fruit",))
        self.assertEqual(first, second)

        manager = MemoryManager(
            InMemoryMemoryStore(limits=limits),
            embeddings,
        )
        scope = MemoryScope("tenant-a", "user-1", "preferences")
        await manager.remember(
            scope,
            "Red apple fruit from the orchard",
            memory_id="food",
            metadata={"category": "food"},
            source="user_statement",
            provenance={"sessionId": "session-1", "turn": 2},
        )
        await manager.remember(
            scope,
            "SQLite transaction and database locking",
            memory_id="database",
            metadata={"category": "technical"},
            source="tool_result",
            provenance={"tool": "docs"},
        )

        results = await manager.recall(scope, "apple fruit", top_k=2)
        filtered = await manager.recall(
            scope,
            "apple",
            top_k=2,
            metadata_filter={"category": "food"},
        )

        self.assertEqual(results[0].record.memory_id, "food")
        self.assertGreater(results[0].score, results[1].score)
        self.assertEqual([item.record.memory_id for item in filtered], ["food"])
        self.assertEqual(filtered[0].record.source, "user_statement")
        self.assertEqual(filtered[0].record.provenance["sessionId"], "session-1")

    async def test_cosine_top_k按精确向量排序(self) -> None:
        limits = memory_limits(embedding_dimensions=3)
        store = InMemoryMemoryStore(limits=limits)
        scope = MemoryScope("tenant", "subject", "exact-ranking")
        for memory_id, vector, group in (
            ("exact", (1.0, 0.0, 0.0), "keep"),
            ("near", (0.8, 0.6, 0.0), "keep"),
            ("other", (0.0, 1.0, 0.0), "drop"),
        ):
            await store.upsert(
                scope,
                memory_id=memory_id,
                text=memory_id,
                embedding=vector,
                metadata={"group": group},
                source="unit-test",
                provenance={},
            )

        ranked = await store.search(scope, (1.0, 0.0, 0.0), top_k=2)
        filtered = await store.search(
            scope,
            (1.0, 0.0, 0.0),
            top_k=2,
            metadata_filter={"group": "keep"},
        )

        self.assertEqual([item.record.memory_id for item in ranked], ["exact", "near"])
        self.assertAlmostEqual(ranked[0].score, 1.0)
        self.assertAlmostEqual(ranked[1].score, 0.8)
        self.assertEqual([item.record.memory_id for item in filtered], ["exact", "near"])

    async def test同memory_id跨租户隔离且删除不越界(self) -> None:
        limits = memory_limits()
        manager = MemoryManager(
            InMemoryMemoryStore(limits=limits),
            HashingEmbeddingProvider(dimensions=limits.embedding_dimensions),
        )
        first_scope = MemoryScope("tenant-a", "same-user", "profile")
        second_scope = MemoryScope("tenant-b", "same-user", "profile")
        await manager.remember(
            first_scope,
            "tenant A private preference",
            memory_id="shared-id",
            source="user",
        )
        await manager.remember(
            second_scope,
            "tenant B private preference",
            memory_id="shared-id",
            source="user",
        )

        self.assertEqual(
            (await manager.get(first_scope, "shared-id")).text,  # type: ignore[union-attr]
            "tenant A private preference",
        )
        self.assertEqual(
            (await manager.get(second_scope, "shared-id")).text,  # type: ignore[union-attr]
            "tenant B private preference",
        )
        self.assertTrue(await manager.delete(first_scope, "shared-id"))
        self.assertIsNone(await manager.get(first_scope, "shared-id"))
        self.assertIsNotNone(await manager.get(second_scope, "shared-id"))

    async def test_ttl过期后不可读取和检索(self) -> None:
        clock = MutableClock()
        limits = memory_limits(max_ttl_seconds=10)
        store = InMemoryMemoryStore(limits=limits, clock=clock)
        manager = MemoryManager(
            store,
            HashingEmbeddingProvider(dimensions=limits.embedding_dimensions),
            clock=clock,
        )
        scope = MemoryScope("tenant", "user", "ttl")
        record = await manager.remember(
            scope,
            "short lived memory",
            memory_id="ttl-record",
            source="user",
            ttl_seconds=1,
        )
        clock.advance(999)
        self.assertIsNotNone(await manager.get(scope, record.memory_id))
        clock.advance(1)
        self.assertIsNone(await manager.get(scope, record.memory_id))
        self.assertEqual(await manager.recall(scope, "short lived", top_k=1), ())

    async def test冲突更新保留created并拒绝陈旧版本(self) -> None:
        clock = MutableClock()
        limits = memory_limits()
        manager = MemoryManager(
            InMemoryMemoryStore(limits=limits, clock=clock),
            HashingEmbeddingProvider(dimensions=limits.embedding_dimensions),
            clock=clock,
        )
        scope = MemoryScope("tenant", "user", "conflict")
        original = await manager.remember(
            scope,
            "version one",
            memory_id="versioned",
            source="user",
        )
        updated = await manager.remember(
            scope,
            "version two",
            memory_id="versioned",
            source="user-correction",
            expected_updated_at_ms=original.updated_at_ms,
        )

        self.assertEqual(updated.created_at_ms, original.created_at_ms)
        self.assertGreater(updated.updated_at_ms, original.updated_at_ms)
        self.assertEqual(updated.source, "user-correction")
        with self.assertRaises(MemoryConflictError):
            await manager.remember(
                scope,
                "stale overwrite",
                memory_id="versioned",
                source="stale",
                expected_updated_at_ms=original.updated_at_ms,
            )
        with self.assertRaises(MemoryConflictError):
            await manager.delete(
                scope,
                "versioned",
                expected_updated_at_ms=original.updated_at_ms,
            )
        self.assertTrue(
            await manager.delete(
                scope,
                "versioned",
                expected_updated_at_ms=updated.updated_at_ms,
            )
        )

    async def test_forget支持metadata_filter(self) -> None:
        limits = memory_limits()
        manager = MemoryManager(
            InMemoryMemoryStore(limits=limits),
            HashingEmbeddingProvider(dimensions=limits.embedding_dimensions),
        )
        scope = MemoryScope("tenant", "user", "forget")
        for memory_id, group in (("a1", "a"), ("a2", "a"), ("b1", "b")):
            await manager.remember(
                scope,
                f"memory {memory_id}",
                memory_id=memory_id,
                metadata={"group": group},
                source="test",
            )

        self.assertEqual(
            await manager.forget(scope, metadata_filter={"group": "a"}),
            2,
        )
        remaining = await manager.recall(scope, "memory", top_k=3)
        self.assertEqual([item.record.memory_id for item in remaining], ["b1"])
        self.assertEqual(await manager.forget(scope), 1)

    async def test所有资源硬上限均fail_closed(self) -> None:
        limits = memory_limits(
            embedding_dimensions=8,
            max_records_per_scope=2,
            max_text_bytes=12,
            max_metadata_bytes=20,
            max_provenance_bytes=20,
            max_query_bytes=10,
            max_top_k=2,
            max_ttl_seconds=5,
            max_source_bytes=10,
            max_filter_items=1,
        )
        manager = MemoryManager(
            InMemoryMemoryStore(limits=limits),
            HashingEmbeddingProvider(dimensions=8),
        )
        scope = MemoryScope("tenant", "user", "limits")
        with self.assertRaises(MemoryLimitError):
            await manager.remember(scope, "x" * 13, source="test")
        with self.assertRaises(MemoryLimitError):
            await manager.remember(
                scope,
                "short",
                source="test",
                metadata={"value": "x" * 20},
            )
        with self.assertRaises(MemoryLimitError):
            await manager.remember(
                scope,
                "short",
                source="test",
                provenance={"value": "x" * 20},
            )
        with self.assertRaises(MemoryLimitError):
            await manager.remember(scope, "short", source="source-too-long")
        with self.assertRaises(MemoryLimitError):
            await manager.remember(scope, "short", source="test", ttl_seconds=6)
        with self.assertRaises(MemoryLimitError):
            await manager.recall(scope, "query-too-long", top_k=1)
        with self.assertRaises(MemoryLimitError):
            await manager.recall(scope, "query", top_k=3)
        with self.assertRaises(MemoryLimitError):
            await manager.recall(
                scope,
                "query",
                top_k=1,
                metadata_filter={"a": 1, "b": 2},
            )
        await manager.remember(scope, "first", memory_id="one", source="test")
        await manager.remember(scope, "second", memory_id="two", source="test")
        with self.assertRaises(MemoryLimitError):
            await manager.remember(scope, "third", memory_id="three", source="test")
        with self.assertRaises(MemoryLimitError):
            MemoryLimits(embedding_dimensions=4097)
        with self.assertRaisesRegex(MemoryValidationError, "维度不一致"):
            MemoryManager(
                InMemoryMemoryStore(limits=limits),
                HashingEmbeddingProvider(dimensions=16),
            )

    async def test_sqlite加密持久重启租户隔离ttl及密文无原文(self) -> None:
        clock = MutableClock()
        limits = memory_limits(embedding_dimensions=64, max_ttl_seconds=10)
        keys = StaticJournalKeyProvider(
            {"memory-key-v1": b"M" * 32},
            active_key_id="memory-key-v1",
        )
        marker = "VERY_SECRET_MEMORY_PAYLOAD_7f91"
        source_marker = "private-crm-source"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "semantic-memory.sqlite3"
            store = SQLiteMemoryStore(
                path,
                key_provider=keys,
                limits=limits,
                clock=clock,
            )
            manager = MemoryManager(
                store,
                HashingEmbeddingProvider(dimensions=64),
                clock=clock,
            )
            quoted_scope = MemoryScope(
                "tenant' OR 1=1 --",
                "subject-a",
                "durable",
            )
            other_scope = MemoryScope("other-tenant", "subject-a", "durable")
            persisted = await manager.remember(
                quoted_scope,
                marker,
                memory_id="persistent",
                source=source_marker,
                metadata={"classification": "confidential", "kind": "profile"},
                provenance={"externalId": "opaque-123"},
            )
            await manager.remember(
                quoted_scope,
                "expires across restart",
                memory_id="expiring",
                source="user",
                ttl_seconds=1,
            )
            await manager.remember(
                other_scope,
                "other tenant value",
                memory_id="persistent",
                source="user",
            )
            clock.advance(1_000)

            reopened = MemoryManager(
                SQLiteMemoryStore(
                    path,
                    key_provider=keys,
                    limits=limits,
                    clock=clock,
                ),
                HashingEmbeddingProvider(dimensions=64),
                clock=clock,
            )
            loaded = await reopened.get(quoted_scope, persisted.memory_id)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.text, marker)  # type: ignore[union-attr]
            self.assertEqual(loaded.source, source_marker)  # type: ignore[union-attr]
            self.assertEqual(loaded.provenance["externalId"], "opaque-123")  # type: ignore[union-attr]
            self.assertIsNone(await reopened.get(quoted_scope, "expiring"))
            self.assertEqual(
                (await reopened.get(other_scope, "persistent")).text,  # type: ignore[union-attr]
                "other tenant value",
            )
            ranked = await reopened.recall(
                quoted_scope,
                marker,
                top_k=2,
                metadata_filter={"kind": "profile"},
            )
            self.assertEqual([item.record.memory_id for item in ranked], ["persistent"])

            database_bytes = b"".join(
                item.read_bytes()
                for item in Path(directory).iterdir()
                if item.is_file()
            )
            self.assertNotIn(marker.encode("utf-8"), database_bytes)
            self.assertNotIn(source_marker.encode("utf-8"), database_bytes)
            self.assertTrue(
                await reopened.delete(
                    quoted_scope,
                    "persistent",
                    expected_updated_at_ms=loaded.updated_at_ms,  # type: ignore[union-attr]
                )
            )
            self.assertIsNone(await reopened.get(quoted_scope, "persistent"))
            self.assertIsNotNone(await reopened.get(other_scope, "persistent"))

    async def test_context_provider必须显式opt_in且默认排除敏感记忆(self) -> None:
        limits = memory_limits()
        manager = MemoryManager(
            InMemoryMemoryStore(limits=limits),
            HashingEmbeddingProvider(dimensions=limits.embedding_dimensions),
        )
        scope = MemoryScope("tenant", "user", "prompt")
        await manager.remember(
            scope,
            "User prefers dark mode",
            memory_id="normal",
            source="user",
        )
        await manager.remember(
            scope,
            "SENSITIVE_TOKEN_SHOULD_NOT_AUTO_INJECT",
            memory_id="sensitive",
            source="secure-import",
            metadata={"sensitive": True},
        )
        disabled = MemoryContextProvider(manager)
        structured = await disabled.retrieve(scope, "user preference token", top_k=2)
        self.assertEqual(len(structured), 2)
        with self.assertRaises(MemoryPromptOptInRequiredError):
            await disabled.build_prompt_context(
                scope,
                "preference",
                opt_in=True,
            )

        enabled = MemoryContextProvider(manager, prompt_injection_enabled=True)
        with self.assertRaises(MemoryPromptOptInRequiredError):
            await enabled.build_prompt_context(scope, "preference")
        prompt_context = await enabled.build_prompt_context(
            scope,
            "preference token",
            opt_in=True,
            top_k=2,
        )
        self.assertIn("User prefers dark mode", prompt_context)
        self.assertNotIn("SENSITIVE_TOKEN_SHOULD_NOT_AUTO_INJECT", prompt_context)
        with self.assertRaises(MemoryPromptOptInRequiredError):
            await enabled.build_prompt_context(
                scope,
                "token",
                opt_in=True,
                include_sensitive=True,
            )

        sensitive_enabled = MemoryContextProvider(
            manager,
            prompt_injection_enabled=True,
            sensitive_prompt_injection_enabled=True,
        )
        sensitive_context = await sensitive_enabled.build_prompt_context(
            scope,
            "token preference",
            opt_in=True,
            include_sensitive=True,
            top_k=2,
        )
        self.assertIn("SENSITIVE_TOKEN_SHOULD_NOT_AUTO_INJECT", sensitive_context)


if __name__ == "__main__":
    unittest.main()
