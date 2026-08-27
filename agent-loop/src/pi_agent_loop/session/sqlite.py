"""SQLite 单机多进程事务 Store。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from ..runtime.events import RuntimeEvent
from .operation_events import OperationEvent
from .operation_store import (
    OperationEventSpec,
    OperationStoreConflictError,
    _check_expected,
    _validate_batch,
    _validate_claim,
)

_SCHEMA_VERSION = 1


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

                CREATE TABLE IF NOT EXISTS runtime_events (
                    sequence INTEGER PRIMARY KEY,
                    type TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    timestamp INTEGER NOT NULL,
                    data_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_runtime_run
                ON runtime_events(run_id, sequence);

                CREATE TABLE IF NOT EXISTS operation_claims (
                    claim_type TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    owner_token TEXT NOT NULL,
                    lease_expires_at INTEGER NOT NULL,
                    PRIMARY KEY (claim_type, resource_id)
                );
                """
            )
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current not in {0, _SCHEMA_VERSION}:
                raise RuntimeError(
                    f"不支持的 SQLite Store Schema 版本：{current}"
                )
            connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")


class SQLiteOperationEventStore(_SQLiteStoreBase):
    """带 CAS、批量事务、唯一约束和跨进程 Lease Claim 的 Store。"""

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
    ) -> list[OperationEvent]:
        _validate_batch(events)
        return await asyncio.to_thread(
            self._append_batch_sync,
            session_id,
            operation_id,
            events,
            expected_last_sequence,
        )

    def _append_batch_sync(
        self,
        session_id: str,
        operation_id: str,
        events: list[OperationEventSpec],
        expected_last_sequence: int | None,
    ) -> list[OperationEvent]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT COALESCE(MAX(sequence), -1) AS value
                FROM operation_events
                WHERE session_id = ? AND operation_id = ?
                """,
                (session_id, operation_id),
            ).fetchone()
            _check_expected(int(row["value"]), expected_last_sequence)
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
                appended.append(
                    OperationEvent(
                        type=event_type,
                        session_id=session_id,
                        operation_id=operation_id,
                        sequence=int(cursor.lastrowid),
                        timestamp=timestamp,
                        data=data,
                    )
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
        return await asyncio.to_thread(
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
        return await asyncio.to_thread(
            self._try_acquire_claim_sync,
            claim_type,
            resource_id,
            owner_token,
            lease_seconds,
        )

    def _try_acquire_claim_sync(
        self,
        claim_type: str,
        resource_id: str,
        owner_token: str,
        lease_seconds: float,
    ) -> bool:
        now = int(time.time() * 1000)
        expires = now + int(lease_seconds * 1000)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT owner_token, lease_expires_at
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
                return False
            connection.execute(
                """
                INSERT INTO operation_claims(
                    claim_type, resource_id, owner_token, lease_expires_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(claim_type, resource_id) DO UPDATE SET
                    owner_token = excluded.owner_token,
                    lease_expires_at = excluded.lease_expires_at
                """,
                (claim_type, resource_id, owner_token, expires),
            )
            connection.execute("COMMIT")
            return True
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
        await asyncio.to_thread(
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
                DELETE FROM operation_claims
                WHERE claim_type = ? AND resource_id = ? AND owner_token = ?
                """,
                (claim_type, resource_id, owner_token),
            )


class SQLiteRuntimeEventStore(_SQLiteStoreBase):
    """与 Operation Event 共用同一 SQLite 文件的 Runtime Store。"""

    async def append(self, event: RuntimeEvent) -> None:
        await asyncio.to_thread(self._append_sync, event)

    def _append_sync(self, event: RuntimeEvent) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), -1) AS value FROM runtime_events"
            ).fetchone()
            if event.sequence <= int(row["value"]):
                raise ValueError("Runtime Event sequence 必须单调递增")
            connection.execute(
                """
                INSERT INTO runtime_events(
                    sequence, type, run_id, timestamp, data_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
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
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    async def load(self) -> list[RuntimeEvent]:
        return await asyncio.to_thread(self._load_sync)

    def _load_sync(self) -> list[RuntimeEvent]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT sequence, type, run_id, timestamp, data_json
                FROM runtime_events
                ORDER BY sequence
                """
            ).fetchall()
        return [
            RuntimeEvent(
                type=str(row["type"]),
                run_id=str(row["run_id"]),
                sequence=int(row["sequence"]),
                timestamp=int(row["timestamp"]),
                data=_json_object(row["data_json"]),
            )
            for row in rows
        ]


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
