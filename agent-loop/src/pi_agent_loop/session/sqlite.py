"""SQLite 单机多进程事务 Store。"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from ..async_utils import durable_to_thread
from ..runtime.events import RuntimeEvent
from .operation_events import OperationEvent
from .operation_store import (
    ClaimLease,
    OperationEventSpec,
    OperationStoreConflictError,
    OperationStoreFencedClaimLostError,
    _check_deadline,
    _check_expected,
    _validate_batch,
    _validate_claim,
    validate_fenced_claim_scope,
)
from .store import (
    RuntimeStoreConflictError,
    RuntimeStoreFencedClaimLostError,
    validate_runtime_fenced_claim_scope,
)

_SCHEMA_VERSION = 2


class SQLiteRuntimeStoreMigrationRequiredError(RuntimeError):
    """Legacy Runtime rows cannot be assigned to a Session implicitly."""


def migrate_legacy_sqlite_runtime_events(
    path: str | Path,
    *,
    session_id: str,
    busy_timeout_seconds: float = 30,
) -> int:
    """Explicitly bind every legacy Runtime row in ``path`` to one Session.

    Version-1 ``runtime_events`` rows did not contain a Session identity.  The
    caller must therefore name the only Session that owned that historical
    stream; guessing from ``run_id`` or Operation rows would permit one
    conversation to inherit another conversation's runtime state.

    Returns the number of migrated Runtime rows.  Calling this for an already
    partitioned database is an idempotent no-op.
    """

    if not session_id:
        raise ValueError("legacy Runtime Migration 的 session_id 不能为空")
    if busy_timeout_seconds <= 0:
        raise ValueError("busy_timeout_seconds 必须大于 0")
    database = Path(path)
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        database,
        timeout=busy_timeout_seconds,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            f"PRAGMA busy_timeout={int(busy_timeout_seconds * 1000)}"
        )
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current not in {0, 1, _SCHEMA_VERSION}:
            raise RuntimeError(f"不支持的 SQLite Store Schema 版本：{current}")
        columns = _runtime_table_columns(connection)
        if not columns:
            _create_runtime_events_schema(connection)
            migrated = 0
        elif "session_id" in columns:
            _validate_runtime_events_schema(columns)
            migrated = 0
        else:
            _validate_legacy_runtime_events_schema(columns)
            migrated = int(
                connection.execute(
                    "SELECT COUNT(*) FROM runtime_events"
                ).fetchone()[0]
            )
            connection.execute("DROP INDEX IF EXISTS idx_runtime_run")
            connection.execute(
                "ALTER TABLE runtime_events RENAME TO runtime_events_legacy_v1"
            )
            _create_runtime_events_schema(connection)
            connection.execute(
                """
                INSERT INTO runtime_events(
                    session_id, sequence, type, run_id, timestamp, data_json
                )
                SELECT ?, sequence, type, run_id, timestamp, data_json
                FROM runtime_events_legacy_v1
                ORDER BY sequence
                """,
                (session_id,),
            )
            connection.execute("DROP TABLE runtime_events_legacy_v1")
        connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        connection.execute("COMMIT")
        return migrated
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


class _SQLiteStoreBase:
    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_seconds: float = 30,
    ) -> None:
        if busy_timeout_seconds <= 0:
            raise ValueError("busy_timeout_seconds 必须大于 0")
        self.path = Path(path)
        self.busy_timeout_seconds = busy_timeout_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_sync()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_seconds,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(
            f"PRAGMA busy_timeout={int(self.busy_timeout_seconds * 1000)}"
        )
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize_sync(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS operation_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    timestamp INTEGER NOT NULL,
                    data_json TEXT NOT NULL,
                    approval_id TEXT,
                    write_id TEXT,
                    idempotency_key_hash TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_operation_identity
                ON operation_events(session_id, operation_id, sequence);

                CREATE INDEX IF NOT EXISTS idx_operation_approval
                ON operation_events(approval_id, sequence);

                CREATE INDEX IF NOT EXISTS idx_operation_write
                ON operation_events(write_id, sequence);

                CREATE UNIQUE INDEX IF NOT EXISTS uq_approval_request
                ON operation_events(approval_id)
                WHERE type = 'approval_requested' AND approval_id IS NOT NULL;

                CREATE UNIQUE INDEX IF NOT EXISTS uq_write_prepare
                ON operation_events(write_id)
                WHERE type = 'write_prepared' AND write_id IS NOT NULL;

                CREATE UNIQUE INDEX IF NOT EXISTS uq_write_idempotency
                ON operation_events(idempotency_key_hash)
                WHERE type = 'write_prepared'
                  AND idempotency_key_hash IS NOT NULL;

                CREATE TABLE IF NOT EXISTS operation_claims (
                    claim_type TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    owner_token TEXT NOT NULL,
                    lease_expires_at INTEGER NOT NULL,
                    generation INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (claim_type, resource_id)
                );
                """
            )
            claim_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(operation_claims)")
            }
            if "generation" not in claim_columns:
                # Additive claim-only migration. Claim rows are coordination
                # metadata, not historical events, so no event-schema upcast is
                # required. Existing active owners become generation 1.
                connection.execute(
                    "ALTER TABLE operation_claims "
                    "ADD COLUMN generation INTEGER NOT NULL DEFAULT 1"
                )
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current not in {0, 1, _SCHEMA_VERSION}:
                raise RuntimeError(
                    f"不支持的 SQLite Store Schema 版本：{current}"
                )
            _ensure_runtime_events_schema(connection)
            connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")


class SQLiteOperationEventStore(_SQLiteStoreBase):
    """带 CAS、批量事务、唯一约束和跨进程 Lease Claim 的 Store。"""

    supports_atomic_transactions = True
    supports_cross_process_claims = True

    async def append(self, event_type, session_id, operation_id, data=None):
        return (
            await self.append_batch(
                session_id,
                operation_id,
                [(event_type, data or {})],
            )
        )[0]

    async def append_batch(
        self,
        session_id: str,
        operation_id: str,
        events: list[OperationEventSpec],
        *,
        expected_last_sequence: int | None = None,
        deadline_ms: int | None = None,
    ) -> list[OperationEvent]:
        _validate_batch(events)
        return await durable_to_thread(
            self._append_batch_sync,
            session_id,
            operation_id,
            events,
            expected_last_sequence,
            deadline_ms,
            None,
            None,
        )

    async def append_batch_if_fenced_claim(
        self,
        session_id: str,
        operation_id: str,
        events: list[OperationEventSpec],
        lease: ClaimLease,
        *,
        renew_lease_seconds: float,
        expected_last_sequence: int | None = None,
        deadline_ms: int | None = None,
        expected_claim_entity_id: str | None = None,
    ) -> list[OperationEvent]:
        _validate_batch(events)
        _validate_claim(
            lease.claim_type,
            lease.resource_id,
            lease.owner_token,
            renew_lease_seconds,
        )
        validate_fenced_claim_scope(
            lease,
            session_id=session_id,
            operation_id=operation_id,
            expected_entity_id=expected_claim_entity_id,
        )
        return await durable_to_thread(
            self._append_batch_sync,
            session_id,
            operation_id,
            events,
            expected_last_sequence,
            deadline_ms,
            lease,
            float(renew_lease_seconds),
        )

    def _append_batch_sync(
        self,
        session_id: str,
        operation_id: str,
        events: list[OperationEventSpec],
        expected_last_sequence: int | None,
        deadline_ms: int | None,
        lease: ClaimLease | None,
        renew_lease_seconds: float | None,
    ) -> list[OperationEvent]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if lease is not None:
                now = int(time.time() * 1000)
                owned = connection.execute(
                    """
                    SELECT 1 FROM operation_claims
                    WHERE claim_type = ? AND resource_id = ?
                      AND owner_token = ? AND generation = ?
                      AND lease_expires_at > ?
                    """,
                    (
                        lease.claim_type,
                        lease.resource_id,
                        lease.owner_token,
                        lease.generation,
                        now,
                    ),
                ).fetchone()
                if owned is None:
                    raise OperationStoreFencedClaimLostError(
                        "Fenced Claim 已失效，禁止追加 Operation Event"
                    )
            row = connection.execute(
                """
                SELECT COALESCE(MAX(sequence), -1) AS value
                FROM operation_events
                WHERE session_id = ? AND operation_id = ?
                """,
                (session_id, operation_id),
            ).fetchone()
            _check_expected(int(row["value"]), expected_last_sequence)
            _check_deadline(deadline_ms)
            appended: list[OperationEvent] = []
            for event_type, raw_data in events:
                data = dict(raw_data)
                timestamp = int(time.time() * 1000)
                cursor = connection.execute(
                    """
                    INSERT INTO operation_events(
                        type, session_id, operation_id, timestamp, data_json,
                        approval_id, write_id, idempotency_key_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_type,
                        session_id,
                        operation_id,
                        timestamp,
                        json.dumps(
                            data,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        _optional_text(data.get("approvalId")),
                        _optional_text(data.get("writeId")),
                        _optional_text(data.get("idempotencyKeyHash")),
                    ),
                )
                sequence = cursor.lastrowid
                if sequence is None:
                    raise OperationStoreConflictError(
                        "SQLite 未返回 Operation Event sequence"
                    )
                appended.append(
                    OperationEvent(
                        type=event_type,
                        session_id=session_id,
                        operation_id=operation_id,
                        sequence=sequence,
                        timestamp=timestamp,
                        data=data,
                    )
                )
            if lease is not None:
                assert renew_lease_seconds is not None
                renewed_until = int(time.time() * 1000) + int(
                    renew_lease_seconds * 1000
                )
                cursor = connection.execute(
                    """
                    UPDATE operation_claims SET lease_expires_at = ?
                    WHERE claim_type = ? AND resource_id = ?
                      AND owner_token = ? AND generation = ?
                    """,
                    (
                        renewed_until,
                        lease.claim_type,
                        lease.resource_id,
                        lease.owner_token,
                        lease.generation,
                    ),
                )
                if cursor.rowcount != 1:
                    raise OperationStoreFencedClaimLostError(
                        "Fenced Claim 在 Operation 提交时已失效"
                    )
            connection.execute("COMMIT")
            return appended
        except OperationStoreConflictError:
            connection.execute("ROLLBACK")
            raise
        except sqlite3.IntegrityError as error:
            connection.execute("ROLLBACK")
            raise OperationStoreConflictError(
                f"SQLite Operation 唯一约束冲突：{error}"
            ) from error
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    async def load(self, *, session_id=None, operation_id=None):
        return await durable_to_thread(
            self._load_sync,
            session_id,
            operation_id,
        )

    def _load_sync(
        self,
        session_id: str | None,
        operation_id: str | None,
    ) -> list[OperationEvent]:
        conditions: list[str] = []
        values: list[str] = []
        if session_id is not None:
            conditions.append("session_id = ?")
            values.append(session_id)
        if operation_id is not None:
            conditions.append("operation_id = ?")
            values.append(operation_id)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT sequence, type, session_id, operation_id,
                       timestamp, data_json
                FROM operation_events
                """
                + where
                + " ORDER BY sequence",
                values,
            ).fetchall()
        return [_operation_event(row) for row in rows]

    async def try_acquire_claim(
        self,
        claim_type: str,
        resource_id: str,
        owner_token: str,
        *,
        lease_seconds: float = 300,
    ) -> bool:
        _validate_claim(claim_type, resource_id, owner_token, lease_seconds)
        lease = await durable_to_thread(
            self._acquire_fenced_claim_sync,
            claim_type,
            resource_id,
            owner_token,
            lease_seconds,
        )
        return lease is not None

    async def acquire_fenced_claim(
        self,
        claim_type: str,
        resource_id: str,
        owner_token: str,
        *,
        lease_seconds: float = 300,
    ) -> ClaimLease | None:
        _validate_claim(claim_type, resource_id, owner_token, lease_seconds)
        return await durable_to_thread(
            self._acquire_fenced_claim_sync,
            claim_type,
            resource_id,
            owner_token,
            lease_seconds,
        )

    def _acquire_fenced_claim_sync(
        self,
        claim_type: str,
        resource_id: str,
        owner_token: str,
        lease_seconds: float,
    ) -> ClaimLease | None:
        now = int(time.time() * 1000)
        expires = now + int(lease_seconds * 1000)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT owner_token, lease_expires_at, generation
                FROM operation_claims
                WHERE claim_type = ? AND resource_id = ?
                """,
                (claim_type, resource_id),
            ).fetchone()
            if (
                row is not None
                and str(row["owner_token"]) != owner_token
                and int(row["lease_expires_at"]) > now
            ):
                connection.execute("ROLLBACK")
                return None
            if (
                row is not None
                and str(row["owner_token"]) == owner_token
                and int(row["lease_expires_at"]) > now
            ):
                generation = int(row["generation"])
            else:
                generation = int(row["generation"]) + 1 if row is not None else 1
            connection.execute(
                """
                INSERT INTO operation_claims(
                    claim_type, resource_id, owner_token, lease_expires_at,
                    generation
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(claim_type, resource_id) DO UPDATE SET
                    owner_token = excluded.owner_token,
                    lease_expires_at = excluded.lease_expires_at,
                    generation = excluded.generation
                """,
                (claim_type, resource_id, owner_token, expires, generation),
            )
            connection.execute("COMMIT")
            return ClaimLease(claim_type, resource_id, owner_token, generation)
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    async def release_claim(
        self,
        claim_type: str,
        resource_id: str,
        owner_token: str,
    ) -> None:
        await durable_to_thread(
            self._release_claim_sync,
            claim_type,
            resource_id,
            owner_token,
        )

    def _release_claim_sync(
        self,
        claim_type: str,
        resource_id: str,
        owner_token: str,
    ) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE operation_claims
                SET lease_expires_at = 0, generation = generation + 1
                WHERE claim_type = ? AND resource_id = ? AND owner_token = ?
                """,
                (claim_type, resource_id, owner_token),
            )

    async def renew_fenced_claim(
        self,
        lease: ClaimLease,
        *,
        lease_seconds: float = 300,
    ) -> bool:
        _validate_claim(
            lease.claim_type,
            lease.resource_id,
            lease.owner_token,
            lease_seconds,
        )
        return await durable_to_thread(
            self._renew_fenced_claim_sync,
            lease,
            lease_seconds,
        )

    def _renew_fenced_claim_sync(
        self,
        lease: ClaimLease,
        lease_seconds: float,
    ) -> bool:
        now = int(time.time() * 1000)
        expires = now + int(lease_seconds * 1000)
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE operation_claims SET lease_expires_at = ?
                WHERE claim_type = ? AND resource_id = ?
                  AND owner_token = ? AND generation = ?
                  AND lease_expires_at > ?
                """,
                (
                    expires,
                    lease.claim_type,
                    lease.resource_id,
                    lease.owner_token,
                    lease.generation,
                    now,
                ),
            )
            return cursor.rowcount == 1

    async def verify_fenced_claim(self, lease: ClaimLease) -> bool:
        return await durable_to_thread(self._verify_fenced_claim_sync, lease)

    def _verify_fenced_claim_sync(self, lease: ClaimLease) -> bool:
        now = int(time.time() * 1000)
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT 1 FROM operation_claims
                WHERE claim_type = ? AND resource_id = ?
                  AND owner_token = ? AND generation = ?
                  AND lease_expires_at > ?
                """,
                (
                    lease.claim_type,
                    lease.resource_id,
                    lease.owner_token,
                    lease.generation,
                    now,
                ),
            ).fetchone()
        return row is not None

    async def release_fenced_claim(self, lease: ClaimLease) -> None:
        await durable_to_thread(self._release_fenced_claim_sync, lease)

    def _release_fenced_claim_sync(self, lease: ClaimLease) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE operation_claims
                SET lease_expires_at = 0, generation = generation + 1
                WHERE claim_type = ? AND resource_id = ?
                  AND owner_token = ? AND generation = ?
                """,
                (
                    lease.claim_type,
                    lease.resource_id,
                    lease.owner_token,
                    lease.generation,
                ),
            )


class SQLiteRuntimeEventStore(_SQLiteStoreBase):
    """Session-bound Runtime stream in a shared SQLite database.

    Runtime ``sequence`` is local to one Session.  Binding the Store at
    construction makes it impossible for replay or CAS to observe a different
    Session merely because both Hosts use the same ``agent-state.sqlite3``.
    """

    supports_fenced_runtime_append = True

    def __init__(
        self,
        path: str | Path,
        *,
        session_id: str,
        busy_timeout_seconds: float = 30,
    ) -> None:
        if not session_id:
            raise ValueError("SQLite Runtime Store 的 session_id 不能为空")
        self.session_id = session_id
        super().__init__(path, busy_timeout_seconds=busy_timeout_seconds)

    async def append(self, event: RuntimeEvent) -> None:
        events = await self.load()
        expected = events[-1].sequence if events else -1
        await self.append_cas(event, expected_last_sequence=expected)

    async def append_cas(
        self,
        event: RuntimeEvent,
        *,
        expected_last_sequence: int,
    ) -> None:
        await durable_to_thread(
            self._append_sync,
            event,
            expected_last_sequence,
            None,
            None,
        )

    async def append_cas_if_fenced_claim(
        self,
        event: RuntimeEvent,
        lease: ClaimLease,
        *,
        renew_lease_seconds: float,
        expected_last_sequence: int,
    ) -> None:
        _validate_claim(
            lease.claim_type,
            lease.resource_id,
            lease.owner_token,
            renew_lease_seconds,
        )
        validate_runtime_fenced_claim_scope(
            lease,
            session_id=self.session_id,
        )
        await durable_to_thread(
            self._append_sync,
            event,
            expected_last_sequence,
            lease,
            float(renew_lease_seconds),
        )

    def _append_sync(
        self,
        event: RuntimeEvent,
        expected_last_sequence: int,
        lease: ClaimLease | None,
        renew_lease_seconds: float | None,
    ) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if lease is not None:
                now = int(time.time() * 1000)
                owned = connection.execute(
                    """
                    SELECT 1 FROM operation_claims
                    WHERE claim_type = ? AND resource_id = ?
                      AND owner_token = ? AND generation = ?
                      AND lease_expires_at > ?
                    """,
                    (
                        lease.claim_type,
                        lease.resource_id,
                        lease.owner_token,
                        lease.generation,
                        now,
                    ),
                ).fetchone()
                if owned is None:
                    raise RuntimeStoreFencedClaimLostError(
                        "Runtime Recovery Fenced Claim 已失效"
                    )
            row = connection.execute(
                """
                SELECT COALESCE(MAX(sequence), -1) AS value
                FROM runtime_events
                WHERE session_id = ?
                """,
                (self.session_id,),
            ).fetchone()
            current = int(row["value"])
            if current != expected_last_sequence or event.sequence != current + 1:
                raise RuntimeStoreConflictError(
                    "Runtime Version 冲突："
                    f"session={self.session_id}, "
                    f"expected={expected_last_sequence}, actual={current}"
                )
            connection.execute(
                """
                INSERT INTO runtime_events(
                    session_id, sequence, type, run_id, timestamp, data_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    self.session_id,
                    event.sequence,
                    event.type,
                    event.run_id,
                    event.timestamp,
                    json.dumps(
                        event.data,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                ),
            )
            if lease is not None:
                assert renew_lease_seconds is not None
                renewed_until = int(time.time() * 1000) + int(
                    renew_lease_seconds * 1000
                )
                cursor = connection.execute(
                    """
                    UPDATE operation_claims SET lease_expires_at = ?
                    WHERE claim_type = ? AND resource_id = ?
                      AND owner_token = ? AND generation = ?
                    """,
                    (
                        renewed_until,
                        lease.claim_type,
                        lease.resource_id,
                        lease.owner_token,
                        lease.generation,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeStoreFencedClaimLostError(
                        "Runtime Recovery Claim 在提交事件时已失效"
                    )
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    async def load(self) -> list[RuntimeEvent]:
        return await durable_to_thread(self._load_sync)

    def _load_sync(self) -> list[RuntimeEvent]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT sequence, type, run_id, timestamp, data_json
                FROM runtime_events
                WHERE session_id = ?
                ORDER BY sequence
                """,
                (self.session_id,),
            ).fetchall()
        return [
            RuntimeEvent.from_dict(
                {
                    "type": str(row["type"]),
                    "runId": str(row["run_id"]),
                    "sequence": int(row["sequence"]),
                    "timestamp": int(row["timestamp"]),
                    "data": _json_object(row["data_json"]),
                }
            )
            for row in rows
        ]


def _runtime_table_columns(
    connection: sqlite3.Connection,
) -> dict[str, sqlite3.Row]:
    return {
        str(row["name"]): row
        for row in connection.execute("PRAGMA table_info(runtime_events)")
    }


def _create_runtime_events_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_events (
            session_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            type TEXT NOT NULL,
            run_id TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            data_json TEXT NOT NULL,
            PRIMARY KEY (session_id, sequence)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_runtime_run
        ON runtime_events(session_id, run_id, sequence)
        """
    )


def _ensure_runtime_events_schema(connection: sqlite3.Connection) -> None:
    columns = _runtime_table_columns(connection)
    if not columns:
        _create_runtime_events_schema(connection)
        return
    if "session_id" in columns:
        _validate_runtime_events_schema(columns)
        _create_runtime_events_schema(connection)
        return
    _validate_legacy_runtime_events_schema(columns)
    row_count = int(
        connection.execute("SELECT COUNT(*) FROM runtime_events").fetchone()[0]
    )
    if row_count:
        raise SQLiteRuntimeStoreMigrationRequiredError(
            "旧 SQLite runtime_events 含有未绑定 Session 的历史事件；"
            "请先调用 migrate_legacy_sqlite_runtime_events(path, "
            "session_id=...) 显式指定这些事件所属的唯一 Session"
        )
    # With no historical fact to attribute, replacing the empty v1 table is a
    # lossless schema upgrade rather than an identity migration.
    connection.execute("DROP INDEX IF EXISTS idx_runtime_run")
    connection.execute("DROP TABLE runtime_events")
    _create_runtime_events_schema(connection)


def _validate_runtime_events_schema(
    columns: dict[str, sqlite3.Row],
) -> None:
    required = {
        "session_id",
        "sequence",
        "type",
        "run_id",
        "timestamp",
        "data_json",
    }
    if set(columns) != required:
        raise RuntimeError("SQLite runtime_events Schema 不受支持")
    if int(columns["session_id"]["pk"]) != 1 or int(columns["sequence"]["pk"]) != 2:
        raise RuntimeError(
            "SQLite runtime_events 必须使用 (session_id, sequence) 复合主键"
        )


def _validate_legacy_runtime_events_schema(
    columns: dict[str, sqlite3.Row],
) -> None:
    required = {"sequence", "type", "run_id", "timestamp", "data_json"}
    if set(columns) != required or int(columns["sequence"]["pk"]) != 1:
        raise RuntimeError("旧 SQLite runtime_events Schema 不受支持，禁止猜测迁移")


def _operation_event(row: sqlite3.Row) -> OperationEvent:
    return OperationEvent(
        type=str(row["type"]),
        session_id=str(row["session_id"]),
        operation_id=str(row["operation_id"]),
        sequence=int(row["sequence"]),
        timestamp=int(row["timestamp"]),
        data=_json_object(row["data_json"]),
    )


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value))
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise ValueError("SQLite Event data_json 无效") from error
    if not isinstance(parsed, dict):
        raise ValueError("SQLite Event data_json 必须是对象")
    return parsed


def _optional_text(value: Any) -> str | None:
    return str(value) if value is not None and str(value) else None
