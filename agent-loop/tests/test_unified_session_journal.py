"""统一加密 Session Journal、治理、Snapshot 和 Migration 专项测试。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from pi_agent_loop.retry.events import RetryRecoveryManager
from pi_agent_loop.runtime.events import RuntimeEvent
from pi_agent_loop.session import (
    EventMigrationRegistry,
    JournalAccessDenied,
    JournalConflictError,
    JournalCorruptionError,
    JournalMigrationError,
    JournalPrincipal,
    JournalRedactionPolicy,
    SessionEventSpec,
    SessionJournalOperationEventStore,
    SessionJournalRetryEventStore,
    SessionJournalRuntimeEventStore,
    SQLiteSessionEventJournal,
    StateMigrationRegistry,
    StaticJournalKeyProvider,
    replay_operation,
    replay_runtime_events,
)


class UnifiedSessionJournalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "session-journal.sqlite3"
        self.keys = StaticJournalKeyProvider(
            {"key-v1": b"1" * 32, "key-v2": b"2" * 32},
            active_key_id="key-v1",
        )
        self.tenant_a = JournalPrincipal.system("tenant-a")
        self.tenant_b = JournalPrincipal.system("tenant-b")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def journal(self, **kwargs) -> SQLiteSessionEventJournal:
        return SQLiteSessionEventJournal(
            self.path,
            key_provider=self.keys,
            **kwargs,
        )

    async def test_runtime_operation_retry共享单一事件表和全局时间线(self) -> None:
        journal = self.journal()
        operations = SessionJournalOperationEventStore(journal, self.tenant_a)
        runtime = SessionJournalRuntimeEventStore(
            journal,
            self.tenant_a,
            session_id="session-1",
        )
        retries = SessionJournalRetryEventStore(
            journal,
            self.tenant_a,
            session_id="session-1",
        )

        operation = await operations.append(
            "operation_started",
            "session-1",
            "operation-1",
            {"configuration": {}, "tools": []},
        )
        await runtime.append(RuntimeEvent("run_started", "run-1", 0))
        await retries.append(
            {
                "type": "tool_retry_scheduled",
                "kind": "tool",
                "retryId": "retry-1",
                "attempt": 1,
            }
        )

        with closing(sqlite3.connect(self.path)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            timeline = connection.execute(
                "SELECT sequence, journal_kind FROM session_events ORDER BY sequence"
            ).fetchall()
        self.assertIn("session_events", tables)
        self.assertEqual(
            [kind for _sequence, kind in timeline],
            ["operation", "runtime", "retry"],
        )
        self.assertEqual(
            [sequence for sequence, _kind in timeline],
            list(range(operation.sequence, operation.sequence + 3)),
        )
        self.assertEqual(replay_operation(await operations.load()).phase, "running")
        self.assertEqual(
            replay_runtime_events(await runtime.load()).phase,
            "running",
        )

    async def test_operation_adapter批次唯一冲突整体回滚(self) -> None:
        journal = self.journal()
        store = SessionJournalOperationEventStore(journal, self.tenant_a)
        with self.assertRaisesRegex(RuntimeError, "唯一约束"):
            await store.append_batch(
                "session-1",
                "operation-1",
                [
                    (
                        "write_prepared",
                        {
                            "writeId": "write-1",
                            "idempotencyKeyHash": "same-hash",
                        },
                    ),
                    (
                        "write_prepared",
                        {
                            "writeId": "write-2",
                            "idempotencyKeyHash": "same-hash",
                        },
                    ),
                ],
                expected_last_sequence=-1,
            )
        self.assertEqual(await store.load(), [])

    async def test_runtime_source_sequence按session隔离(self) -> None:
        journal = self.journal()
        first = SessionJournalRuntimeEventStore(
            journal,
            self.tenant_a,
            session_id="session-a",
        )
        second = SessionJournalRuntimeEventStore(
            journal,
            self.tenant_a,
            session_id="session-b",
        )

        await first.append(RuntimeEvent("run_started", "run-a", 0))
        await second.append(RuntimeEvent("run_started", "run-b", 0))

        self.assertEqual([event.sequence for event in await first.load()], [0])
        self.assertEqual([event.sequence for event in await second.load()], [0])

    async def test_payload使用认证加密且只在非特权读取视图脱敏(self) -> None:
        journal = self.journal()
        await journal.append_events(
            self.tenant_a,
            [
                SessionEventSpec(
                    "operation",
                    "customer_loaded",
                    "session-secret",
                    {
                        "customerEmail": "customer@example.test",
                        "api_key": "must-never-persist",
                    },
                    operation_id="operation-secret",
                )
            ],
        )

        database_bytes = self.path.read_bytes()
        self.assertNotIn(b"customer@example.test", database_bytes)
        self.assertNotIn(b"must-never-persist", database_bytes)
        loaded = await journal.load_events(
            self.tenant_a,
            session_id="session-secret",
        )
        self.assertEqual(
            loaded[0].payload,
            {
                "customerEmail": "customer@example.test",
                "api_key": "must-never-persist",
            },
        )
        reader = JournalPrincipal(
            "reader",
            "tenant-a",
            frozenset({"journal_reader"}),
        )
        redacted = await journal.load_events(
            reader,
            session_id="session-secret",
        )
        self.assertEqual(redacted[0].payload["api_key"], "<redacted>")

    async def test_低权读取的event_migration只能观察脱敏视图(self) -> None:
        journal = self.journal(current_event_schema_version=2)
        await journal.append_events(
            self.tenant_a,
            [
                SessionEventSpec(
                    "operation",
                    "secret_event",
                    "session-secret",
                    {"password": "event-secret", "value": 1},
                    operation_id="operation-secret",
                    schema_version=1,
                )
            ],
        )
        observed: list[str] = []
        migrations = EventMigrationRegistry()

        def migrate_reader_view(payload):
            observed.append(payload["password"])
            return {**payload, "migrated": True}

        migrations.register(
            "operation",
            "secret_event",
            1,
            2,
            migrate_reader_view,
        )
        reader = JournalPrincipal(
            "reader",
            "tenant-a",
            frozenset({"journal_reader"}),
        )
        loaded = await journal.load_events(
            reader,
            session_id="session-secret",
            migration_registry=migrations,
            target_schema_version=2,
        )
        self.assertEqual(observed, ["<redacted>"])
        self.assertEqual(loaded[0].payload["password"], "<redacted>")
        self.assertTrue(loaded[0].payload["migrated"])

    async def test敏感业务参数恢复时保持原值和action_hash一致(self) -> None:
        journal = self.journal()
        original = {
            "arguments": {"password": "business-value", "token": "order-token"},
            "actionHash": "hash-created-from-original",
        }
        await journal.append_events(
            self.tenant_a,
            [
                SessionEventSpec(
                    "operation",
                    "tool_intent_recorded",
                    "session-action",
                    original,
                    operation_id="operation-action",
                )
            ],
        )

        loaded = await journal.load_events(
            self.tenant_a,
            session_id="session-action",
        )
        self.assertEqual(loaded[0].payload, original)

    async def test_tenant隔离和访问策略(self) -> None:
        journal = self.journal()
        for principal, value in ((self.tenant_a, "A"), (self.tenant_b, "B")):
            await journal.append_events(
                principal,
                [
                    SessionEventSpec(
                        "operation",
                        "value",
                        "same-session",
                        {"v": value},
                        operation_id="same-operation",
                    )
                ],
            )

        self.assertEqual(
            [event.payload["v"] for event in await journal.load_events(self.tenant_a)],
            ["A"],
        )
        self.assertEqual(
            [event.payload["v"] for event in await journal.load_events(self.tenant_b)],
            ["B"],
        )
        reader = JournalPrincipal(
            "reader",
            "tenant-a",
            frozenset({"journal_reader"}),
        )
        self.assertEqual(len(await journal.load_events(reader)), 1)
        with self.assertRaises(JournalAccessDenied):
            await journal.export_session(reader, "same-session")
        with self.assertRaises(JournalAccessDenied):
            await journal.load_events(
                JournalPrincipal("outsider", "tenant-a", frozenset({"untrusted"}))
            )

    async def test导出删除保留策略和审计事件(self) -> None:
        journal = self.journal(
            redaction_policy=JournalRedactionPolicy(default_retention_seconds=1)
        )
        expired_timestamp = 10_000
        await journal.append_events(
            self.tenant_a,
            [
                SessionEventSpec(
                    "retry",
                    "expired",
                    "expired-session",
                    {"value": 1},
                    timestamp=expired_timestamp,
                ),
                SessionEventSpec(
                    "retry",
                    "active",
                    "delete-session",
                    {"value": 2},
                ),
            ],
        )
        exported = await journal.export_session(self.tenant_a, "delete-session")
        self.assertEqual(exported[0]["payload"], {"value": 2})
        self.assertEqual(await journal.delete_session(self.tenant_a, "delete-session"), 1)
        self.assertEqual(
            await journal.purge_expired(
                self.tenant_a,
                now_ms=expired_timestamp + 1_001,
            ),
            1,
        )
        self.assertEqual(
            await journal.load_events(self.tenant_a, session_id="delete-session"),
            [],
        )
        audit_types = {
            event.event_type for event in await journal.load_audit_events(self.tenant_a)
        }
        self.assertTrue(
            {
                "journal_session_exported",
                "journal_session_deleted",
                "journal_retention_purged",
            }.issubset(audit_types)
        )

    async def test删除或purge后全局sequence永不复用(self) -> None:
        journal = self.journal(
            redaction_policy=JournalRedactionPolicy(default_retention_seconds=1)
        )
        first = await journal.append_events(
            self.tenant_a,
            [
                SessionEventSpec(
                    "retry",
                    "expired",
                    "old-session",
                    {"value": 1},
                    timestamp=1,
                )
            ],
        )
        self.assertEqual(first[0].sequence, 0)

        await journal.purge_expired(self.tenant_a, now_ms=2_000)
        after_cursor = await journal.load_events(
            self.tenant_a,
            after_sequence=0,
        )

        self.assertEqual(len(after_cursor), 1)
        self.assertEqual(after_cursor[0].event_type, "journal_retention_purged")
        self.assertGreater(after_cursor[0].sequence, first[0].sequence)

    async def testPurge只整组删除终态Operation并保留活跃流前缀(self) -> None:
        journal = self.journal()
        await journal.append_events(
            self.tenant_a,
            [
                SessionEventSpec(
                    "operation",
                    "operation_started",
                    "mixed-session",
                    {"configuration": {}, "tools": []},
                    operation_id="active-operation",
                    timestamp=1,
                    retention_seconds=1,
                ),
                SessionEventSpec(
                    "operation",
                    "operation_finished",
                    "mixed-session",
                    {"outcome": "failed"},
                    operation_id="active-operation",
                    timestamp=10_000,
                    retention_seconds=1,
                ),
                SessionEventSpec(
                    "operation",
                    "operation_started",
                    "mixed-session",
                    {"configuration": {}, "tools": []},
                    operation_id="terminal-operation",
                    timestamp=1,
                    retention_seconds=1,
                ),
                SessionEventSpec(
                    "operation",
                    "operation_finished",
                    "mixed-session",
                    {"outcome": "failed"},
                    operation_id="terminal-operation",
                    timestamp=2,
                    retention_seconds=1,
                ),
                SessionEventSpec(
                    "operation",
                    "operation_started",
                    "mixed-session",
                    {"configuration": {}, "tools": []},
                    operation_id="nonterminal-operation",
                    timestamp=1,
                    retention_seconds=1,
                ),
                SessionEventSpec(
                    "runtime",
                    "run_started",
                    "runtime-session",
                    {},
                    run_id="run-1",
                    source_sequence=0,
                    timestamp=1,
                    retention_seconds=1,
                ),
                SessionEventSpec(
                    "runtime",
                    "run_finished",
                    "runtime-session",
                    {"outcome": "completed"},
                    run_id="run-1",
                    source_sequence=1,
                    timestamp=10_000,
                    retention_seconds=1,
                ),
            ],
        )

        self.assertEqual(
            await journal.purge_expired(self.tenant_a, now_ms=2_000),
            2,
        )
        operations = SessionJournalOperationEventStore(journal, self.tenant_a)
        active = await operations.load(operation_id="active-operation")
        self.assertEqual(
            [event.type for event in active],
            ["operation_started", "operation_finished"],
        )
        self.assertEqual(replay_operation(active).phase, "failed")
        self.assertEqual(
            await operations.load(operation_id="terminal-operation"),
            [],
        )
        nonterminal = await operations.load(operation_id="nonterminal-operation")
        self.assertEqual(
            [event.type for event in nonterminal],
            ["operation_started"],
        )
        self.assertEqual(replay_operation(nonterminal).phase, "running")

        runtime = SessionJournalRuntimeEventStore(
            journal,
            self.tenant_a,
            session_id="runtime-session",
        )
        runtime_events = await runtime.load()
        self.assertEqual(
            [event.type for event in runtime_events],
            ["run_started", "run_finished"],
        )
        self.assertEqual(replay_runtime_events(runtime_events).phase, "completed")

    async def test_checksum发现数据库Payload被篡改(self) -> None:
        journal = self.journal()
        await journal.append_events(
            self.tenant_a,
            [
                SessionEventSpec(
                    "operation",
                    "safe",
                    "session-1",
                    {"value": 1},
                    operation_id="operation-1",
                )
            ],
        )
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "UPDATE session_events SET payload_ciphertext = zeroblob(32)"
            )
            connection.commit()
        with self.assertRaisesRegex(JournalCorruptionError, "Checksum"):
            await journal.load_events(self.tenant_a)

    async def test错误密钥通过AEAD_Tag被拒绝(self) -> None:
        journal = self.journal()
        await journal.append_events(
            self.tenant_a,
            [
                SessionEventSpec(
                    "operation",
                    "safe",
                    "session-1",
                    {"value": 1},
                    operation_id="operation-1",
                )
            ],
        )
        wrong_keys = StaticJournalKeyProvider(
            {"key-v1": b"x" * 32},
            active_key_id="key-v1",
        )
        # 启动时先校验租户 Manifest，因此错误 Key 会在 Store 开放读取前失败。
        with self.assertRaisesRegex(JournalCorruptionError, "Manifest HMAC"):
            SQLiteSessionEventJournal(
                self.path,
                key_provider=wrong_keys,
            )

    async def test_snapshot损坏也会被完整性扫描发现(self) -> None:
        journal = self.journal()
        await journal.save_snapshot(
            self.tenant_a,
            session_id="session-1",
            projection_name="summary",
            last_sequence=-1,
            state={"count": 1},
            state_version=1,
        )
        self.assertEqual(await journal.verify_integrity(self.tenant_a), (0, 1))
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "UPDATE session_snapshots SET state_ciphertext = zeroblob(32)"
            )
            connection.commit()
        with self.assertRaisesRegex(JournalCorruptionError, "Checksum"):
            await journal.verify_integrity(self.tenant_a)

    async def test直接SQL删除Event会被Manifest发现且不能被Append封正(self) -> None:
        journal = self.journal()
        appended = await journal.append_events(
            self.tenant_a,
            [
                SessionEventSpec(
                    "operation",
                    "step",
                    "session-1",
                    {"step": step},
                    operation_id="operation-1",
                )
                for step in (1, 2, 3)
            ],
        )
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "DELETE FROM session_events WHERE sequence = ?",
                (appended[1].sequence,),
            )
            connection.commit()

        with self.assertRaisesRegex(JournalCorruptionError, "Manifest HMAC"):
            await journal.verify_integrity(self.tenant_a)
        with self.assertRaisesRegex(JournalCorruptionError, "Manifest HMAC"):
            await journal.append_events(
                self.tenant_a,
                [
                    SessionEventSpec(
                        "operation",
                        "step",
                        "session-1",
                        {"step": 4},
                        operation_id="operation-1",
                    )
                ],
            )

    async def test直接SQL删除Snapshot会被Manifest发现(self) -> None:
        journal = self.journal()
        event = (
            await journal.append_events(
                self.tenant_a,
                [
                    SessionEventSpec(
                        "operation",
                        "step",
                        "session-1",
                        {"step": 1},
                        operation_id="operation-1",
                    )
                ],
            )
        )[0]
        await journal.save_snapshot(
            self.tenant_a,
            session_id="session-1",
            projection_name="summary",
            last_sequence=event.sequence,
            state={"count": 1},
            state_version=1,
        )
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("DELETE FROM session_snapshots")
            connection.commit()
        with self.assertRaisesRegex(JournalCorruptionError, "Manifest HMAC"):
            await journal.verify_integrity(self.tenant_a)

    async def test合法治理操作后Manifest始终同步刷新(self) -> None:
        journal = self.journal(
            redaction_policy=JournalRedactionPolicy(default_retention_seconds=1)
        )
        expired, active = await journal.append_events(
            self.tenant_a,
            [
                SessionEventSpec(
                    "retry",
                    "expired",
                    "expired-session",
                    {"value": 1},
                    timestamp=1,
                ),
                SessionEventSpec(
                    "retry",
                    "active",
                    "active-session",
                    {"value": 2},
                ),
            ],
        )
        await journal.save_snapshot(
            self.tenant_a,
            session_id="active-session",
            projection_name="summary",
            last_sequence=active.sequence,
            state={"value": 2},
            state_version=1,
        )
        await journal.verify_integrity(self.tenant_a)
        await journal.export_session(self.tenant_a, "active-session")
        await journal.verify_integrity(self.tenant_a)
        self.keys.set_active("key-v2")
        await journal.rotate_encryption_keys(self.tenant_a)
        await journal.verify_integrity(self.tenant_a)
        await journal.delete_session(self.tenant_a, "active-session")
        await journal.verify_integrity(self.tenant_a)
        await journal.purge_expired(
            self.tenant_a,
            now_ms=expired.timestamp + 1_001,
        )
        await journal.verify_integrity(self.tenant_a)

    async def test旧库首次建立Manifest后缺失清单不再静默重建(self) -> None:
        journal = self.journal()
        await journal.append_events(
            self.tenant_a,
            [
                SessionEventSpec(
                    "operation",
                    "legacy",
                    "session-1",
                    {"value": 1},
                    operation_id="operation-1",
                )
            ],
        )
        # 模拟功能发布前的旧库：有合法加密记录，但没有 Manifest 表数据/版本标记。
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("DELETE FROM session_integrity_manifests")
            connection.execute(
                """
                DELETE FROM session_journal_meta
                WHERE key = 'integrity_manifest_version'
                """
            )
            connection.execute(
                """
                UPDATE session_journal_meta SET value = '1'
                WHERE key = 'database_schema_version'
                """
            )
            connection.commit()
        with self.assertRaisesRegex(JournalCorruptionError, "默认禁止自动建立"):
            self.journal()
        migrated = self.journal(allow_legacy_integrity_bootstrap=True)
        self.assertEqual(await migrated.verify_integrity(self.tenant_a), (1, 0))

        # 已迁移库只丢 Manifest 时必须失败，不能把当前磁盘内容重新签名。
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("DELETE FROM session_integrity_manifests")
            connection.commit()
        with self.assertRaisesRegex(JournalCorruptionError, "禁止静默重建"):
            self.journal()

    async def test删除全部数据并降级Meta默认也不能重新签名(self) -> None:
        journal = self.journal()
        await journal.append_events(
            self.tenant_a,
            [
                SessionEventSpec(
                    "retry",
                    "legacy",
                    "session-1",
                    {"value": 1},
                )
            ],
        )
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("DELETE FROM session_events")
            connection.execute("DELETE FROM session_snapshots")
            connection.execute("DELETE FROM session_integrity_manifests")
            connection.execute(
                """
                DELETE FROM session_journal_meta
                WHERE key = 'integrity_manifest_version'
                """
            )
            connection.execute(
                """
                UPDATE session_journal_meta SET value = '1'
                WHERE key = 'database_schema_version'
                """
            )
            connection.execute(
                """
                UPDATE session_journal_meta SET value = '0'
                WHERE key = 'next_sequence'
                """
            )
            connection.commit()

        with self.assertRaisesRegex(JournalCorruptionError, "默认禁止自动建立"):
            self.journal()

    async def testNextSequence篡改被发现且AfterCursor不会漏新事件(self) -> None:
        journal = self.journal()
        first = (
            await journal.append_events(
                self.tenant_a,
                [
                    SessionEventSpec(
                        "retry",
                        "old",
                        "old-session",
                        {"value": 1},
                    )
                ],
            )
        )[0]
        await journal.delete_session(self.tenant_a, "old-session")
        audit = (await journal.load_audit_events(self.tenant_a))[-1]
        self.assertGreater(audit.sequence, first.sequence)

        with closing(sqlite3.connect(self.path)) as connection:
            expected_next = int(
                connection.execute(
                    """
                    SELECT value FROM session_journal_meta
                    WHERE key = 'next_sequence'
                    """
                ).fetchone()[0]
            )
            # 即使仍严格大于 MAX(sequence)，改变 High-water Mark 也必须由
            # Manifest HMAC 发现，不能只靠 PRIMARY KEY/最大值校验。
            connection.execute(
                """
                UPDATE session_journal_meta SET value = ?
                WHERE key = 'next_sequence'
                """,
                (str(expected_next + 100),),
            )
            connection.commit()

        with self.assertRaisesRegex(JournalCorruptionError, "Manifest HMAC"):
            await journal.verify_integrity(self.tenant_a)

        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                """
                UPDATE session_journal_meta SET value = ?
                WHERE key = 'next_sequence'
                """,
                (str(first.sequence),),
            )
            connection.commit()

        with self.assertRaisesRegex(JournalCorruptionError, "next_sequence|Manifest HMAC"):
            await journal.verify_integrity(self.tenant_a)
        with self.assertRaisesRegex(JournalCorruptionError, "next_sequence|Manifest HMAC"):
            await journal.append_events(
                self.tenant_a,
                [SessionEventSpec("retry", "must-not-append", "new-session", {})],
            )

        # 只为继续验证正常路径而恢复完全相同的、仍由 Manifest 签名的值。
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                """
                UPDATE session_journal_meta SET value = ?
                WHERE key = 'next_sequence'
                """,
                (str(expected_next),),
            )
            connection.commit()
        appended = (
            await journal.append_events(
                self.tenant_a,
                [SessionEventSpec("retry", "new", "new-session", {"value": 2})],
            )
        )[0]
        self.assertGreater(appended.sequence, audit.sequence)
        self.assertEqual(
            [event.sequence for event in await journal.load_events(
                self.tenant_a,
                after_sequence=audit.sequence,
            )],
            [appended.sequence],
        )

    async def test_snapshot加密原文并按Reader和Auditor返回脱敏视图(self) -> None:
        journal = self.journal()
        event = (
            await journal.append_events(
                self.tenant_a,
                [
                    SessionEventSpec(
                        "operation",
                        "step",
                        "session-1",
                        {"step": 1},
                        operation_id="operation-1",
                    )
                ],
            )
        )[0]
        await journal.save_snapshot(
            self.tenant_a,
            session_id="session-1",
            projection_name="secret-state",
            last_sequence=event.sequence,
            state={"password": "business-value", "count": 1},
            state_version=1,
        )
        privileged = await journal.load_snapshot(
            self.tenant_a,
            session_id="session-1",
            projection_name="secret-state",
        )
        self.assertEqual(privileged.state["password"], "business-value")
        for principal in (
            JournalPrincipal("reader", "tenant-a", frozenset({"journal_reader"})),
            JournalPrincipal("auditor", "tenant-a", frozenset({"journal_auditor"})),
        ):
            view = await journal.load_snapshot(
                principal,
                session_id="session-1",
                projection_name="secret-state",
            )
            self.assertEqual(view.state["password"], "<redacted>")
            self.assertEqual(view.state["count"], 1)
        observed_by_reader_migration: list[str] = []
        reader_migrations = StateMigrationRegistry()

        def migrate_reader_view(state):
            observed_by_reader_migration.append(state["password"])
            return {**state, "viewVersion": 2}

        reader_migrations.register(
            "secret-state",
            1,
            2,
            migrate_reader_view,
        )
        await journal.load_snapshot(
            JournalPrincipal("reader", "tenant-a", frozenset({"journal_reader"})),
            session_id="session-1",
            projection_name="secret-state",
            state_registry=reader_migrations,
            target_state_version=2,
        )
        self.assertEqual(observed_by_reader_migration, ["<redacted>"])
        self.assertNotIn(b"business-value", self.path.read_bytes())

    async def test_snapshot边界同序覆盖和迁移版本规则(self) -> None:
        journal = self.journal()
        with self.assertRaisesRegex(JournalConflictError, "Event Head"):
            await journal.save_snapshot(
                self.tenant_a,
                session_id="session-1",
                projection_name="summary",
                last_sequence=0,
                state={"count": 0},
                state_version=1,
            )
        event = (
            await journal.append_events(
                self.tenant_a,
                [
                    SessionEventSpec(
                        "operation",
                        "step",
                        "session-1",
                        {"step": 1},
                        operation_id="operation-1",
                    )
                ],
            )
        )[0]
        await journal.save_snapshot(
            self.tenant_a,
            session_id="session-1",
            projection_name="summary",
            last_sequence=event.sequence,
            state={"count": 1},
            state_version=1,
        )
        with self.assertRaisesRegex(JournalConflictError, "同一 last_sequence"):
            await journal.save_snapshot(
                self.tenant_a,
                session_id="session-1",
                projection_name="summary",
                last_sequence=event.sequence,
                state={"count": 999},
                state_version=2,
            )
        registry = StateMigrationRegistry()
        registry.register(
            "summary",
            1,
            2,
            lambda state: {**state, "migrated": True},
        )
        migrated = await journal.migrate_snapshot(
            self.tenant_a,
            session_id="session-1",
            projection_name="summary",
            registry=registry,
            target_state_version=2,
        )
        self.assertEqual(migrated.state_version, 2)
        with self.assertRaisesRegex(JournalMigrationError, "严格提升"):
            await journal.migrate_snapshot(
                self.tenant_a,
                session_id="session-1",
                projection_name="summary",
                registry=registry,
                target_state_version=2,
            )

    async def test旧事件逐版本upcast且不可变历史不被改写(self) -> None:
        journal = self.journal(current_event_schema_version=2)
        await journal.append_events(
            self.tenant_a,
            [
                SessionEventSpec(
                    "operation",
                    "order_named",
                    "session-1",
                    {"name": "legacy"},
                    operation_id="operation-1",
                    schema_version=1,
                )
            ],
        )
        migrations = EventMigrationRegistry()
        migrations.register(
            "operation",
            "order_named",
            1,
            2,
            lambda payload: {"orderName": payload["name"]},
        )
        migrated = await journal.run_event_migrations(
            self.tenant_a,
            migrations,
            target_version=2,
            session_id="session-1",
        )
        self.assertEqual(migrated[0].schema_version, 2)
        self.assertEqual(migrated[0].payload, {"orderName": "legacy"})
        with closing(sqlite3.connect(self.path)) as connection:
            stored_version = connection.execute(
                """
                SELECT schema_version FROM session_events
                WHERE session_id = 'session-1'
                """
            ).fetchone()[0]
        self.assertEqual(stored_version, 1)

    async def test_snapshot后只重放增量并可迁移State版本(self) -> None:
        journal = self.journal()
        first_batch = await journal.append_events(
            self.tenant_a,
            [
                SessionEventSpec(
                    "operation",
                    "added",
                    "session-1",
                    {"value": 1},
                    operation_id="operation-1",
                ),
                SessionEventSpec(
                    "operation",
                    "added",
                    "session-1",
                    {"value": 2},
                    operation_id="operation-1",
                ),
            ],
        )
        await journal.save_snapshot(
            self.tenant_a,
            session_id="session-1",
            projection_name="total",
            last_sequence=first_batch[-1].sequence,
            state={"total": 3},
            state_version=1,
        )
        await journal.append_events(
            self.tenant_a,
            [
                SessionEventSpec(
                    "operation",
                    "added",
                    "session-1",
                    {"value": 4},
                    operation_id="operation-1",
                )
            ],
        )
        replay = await journal.replay_projection(
            self.tenant_a,
            session_id="session-1",
            projection_name="total",
            initial_state={"total": 0},
            reducer=lambda state, event: {
                "total": state["total"] + event.payload["value"]
            },
            journal_kind="operation",
        )
        self.assertEqual(replay.state, {"total": 7})
        self.assertEqual(replay.applied_events, 1)

        state_migrations = StateMigrationRegistry()
        state_migrations.register(
            "total",
            1,
            2,
            lambda state: {**state, "currency": "CNY"},
        )
        migrated = await journal.migrate_snapshot(
            self.tenant_a,
            session_id="session-1",
            projection_name="total",
            registry=state_migrations,
            target_state_version=2,
        )
        self.assertEqual(migrated.state_version, 2)
        self.assertEqual(migrated.state["currency"], "CNY")

    async def test旧Key轮换后旧Key不可用仍可解密(self) -> None:
        journal = self.journal()
        await journal.append_events(
            self.tenant_a,
            [
                SessionEventSpec(
                    "operation",
                    "encrypted",
                    "session-1",
                    {"v": 1},
                    operation_id="operation-1",
                )
            ],
        )
        self.keys.set_active("key-v2")
        rotated_events, rotated_snapshots = await journal.rotate_encryption_keys(
            self.tenant_a
        )
        self.assertEqual((rotated_events, rotated_snapshots), (1, 0))
        with closing(sqlite3.connect(self.path)) as connection:
            key_ids = {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT key_id FROM session_events"
                )
            }
        self.assertEqual(key_ids, {"key-v2"})
        only_new_key = StaticJournalKeyProvider(
            {"key-v2": b"2" * 32},
            active_key_id="key-v2",
        )
        reopened = SQLiteSessionEventJournal(
            self.path,
            key_provider=only_new_key,
        )
        self.assertEqual((await reopened.load_events(self.tenant_a))[0].payload, {"v": 1})

    async def test_retry兼容Adapter可供现有RecoveryManager使用(self) -> None:
        journal = self.journal()
        store = SessionJournalRetryEventStore(
            journal,
            self.tenant_a,
            session_id="session-1",
        )
        await store.append(
            {
                "type": "tool_retry_scheduled",
                "kind": "tool",
                "retryId": "retry-1",
                "toolCallId": "call-1",
                "attempt": 1,
            }
        )
        handled: list[str] = []

        async def recover(chain) -> bool:
            handled.append(chain.retry_id)
            return True

        recovered = await RetryRecoveryManager(store).recover(recover)  # type: ignore[arg-type]
        self.assertEqual(handled, ["retry-1"])
        self.assertEqual([chain.retry_id for chain in recovered], ["retry-1"])
        self.assertEqual(await store.incomplete_chains_async(), [])

    async def test_同一模型request只能提交一个durable终态(self) -> None:
        journal = self.journal()
        store = SessionJournalRetryEventStore(
            journal,
            self.tenant_a,
            session_id="session-model-terminal",
        )
        await store.append(
            {
                "type": "model_request_completed",
                "requestId": "request-1",
                "operationId": "operation-1",
                "message": {"stopReason": "stop"},
            }
        )
        with self.assertRaises(JournalConflictError):
            await store.append(
                {
                    "type": "model_request_failed",
                    "requestId": "request-1",
                    "operationId": "operation-1",
                    "message": {"stopReason": "error"},
                }
            )

        events = await store.load()
        self.assertEqual(
            [event["type"] for event in events],
            ["model_request_completed"],
        )


if __name__ == "__main__":
    unittest.main()
