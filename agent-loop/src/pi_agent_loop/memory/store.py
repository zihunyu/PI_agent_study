"""Tenant-isolated in-memory and encrypted SQLite semantic-memory stores."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import os
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..async_utils import durable_to_thread
from ..session.journal import JournalEncryptionKey, JournalKeyProvider
from .types import (
    MemoryConflictError,
    MemoryCorruptionError,
    MemoryLimitError,
    MemoryLimits,
    MemoryRecord,
    MemoryScope,
    MemorySearchResult,
    MemoryValidationError,
    canonical_json_bytes,
    metadata_matches,
    thaw_json,
    validate_embedding,
    validate_metadata_filter,
    validate_record_payload,
    validate_scope_limits,
)

Clock = Callable[[], int]


@runtime_checkable
class MemoryStore(Protocol):
    """Persistence contract; every operation requires an exact tenant scope."""

    @property
    def limits(self) -> MemoryLimits: ...

    async def upsert(
        self,
        scope: MemoryScope,
        *,
        memory_id: str,
        text: str,
        embedding: Sequence[float],
        metadata: Mapping[str, Any],
        source: str,
        provenance: Mapping[str, Any],
        expires_at_ms: int | None = None,
        expected_updated_at_ms: int | None = None,
    ) -> MemoryRecord: ...

    async def get(self, scope: MemoryScope, memory_id: str) -> MemoryRecord | None: ...

    async def search(
        self,
        scope: MemoryScope,
        query_embedding: Sequence[float],
        *,
        top_k: int,
        metadata_filter: Mapping[str, Any] | None = None,
    ) -> tuple[MemorySearchResult, ...]: ...

    async def delete(
        self,
        scope: MemoryScope,
        memory_id: str,
        *,
        expected_updated_at_ms: int | None = None,
    ) -> bool: ...

    async def forget(
        self,
        scope: MemoryScope,
        *,
        metadata_filter: Mapping[str, Any] | None = None,
    ) -> int: ...


class InMemoryMemoryStore:
    """Bounded deterministic store for tests and non-persistent local use."""

    def __init__(
        self,
        *,
        limits: MemoryLimits | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._limits = limits or MemoryLimits()
        self._clock = clock or _system_now_ms
        self._records: dict[tuple[str, str, str, str], MemoryRecord] = {}
        self._lock = asyncio.Lock()

    @property
    def limits(self) -> MemoryLimits:
        return self._limits

    async def upsert(
        self,
        scope: MemoryScope,
        *,
        memory_id: str,
        text: str,
        embedding: Sequence[float],
        metadata: Mapping[str, Any],
        source: str,
        provenance: Mapping[str, Any],
        expires_at_ms: int | None = None,
        expected_updated_at_ms: int | None = None,
    ) -> MemoryRecord:
        payload = _validated_write(
            scope,
            memory_id=memory_id,
            text=text,
            embedding=embedding,
            metadata=metadata,
            source=source,
            provenance=provenance,
            expires_at_ms=expires_at_ms,
            expected_updated_at_ms=expected_updated_at_ms,
            limits=self.limits,
            now_ms=self._clock(),
        )
        now_ms = payload.now_ms
        key = (*scope.key, payload.memory_id)
        async with self._lock:
            self._purge_expired_scope(scope, now_ms)
            current = self._records.get(key)
            _check_expected_version(current, payload.expected_updated_at_ms)
            if current is None:
                if self._scope_count(scope) >= self.limits.max_records_per_scope:
                    raise MemoryLimitError("scope memory record 数量超过上限")
                created_at_ms = now_ms
                updated_at_ms = now_ms
            else:
                created_at_ms = current.created_at_ms
                updated_at_ms = max(now_ms, current.updated_at_ms + 1)
            record = payload.to_record(
                created_at_ms=created_at_ms,
                updated_at_ms=updated_at_ms,
            )
            self._records[key] = record
            return record

    async def get(self, scope: MemoryScope, memory_id: str) -> MemoryRecord | None:
        validate_scope_limits(scope, self.limits)
        normalized_id = _validated_memory_id(memory_id, self.limits)
        now_ms = self._clock()
        key = (*scope.key, normalized_id)
        async with self._lock:
            record = self._records.get(key)
            if record is not None and _is_expired(record, now_ms):
                del self._records[key]
                return None
            return record

    async def search(
        self,
        scope: MemoryScope,
        query_embedding: Sequence[float],
        *,
        top_k: int,
        metadata_filter: Mapping[str, Any] | None = None,
    ) -> tuple[MemorySearchResult, ...]:
        query, limit, expected = _validated_search(
            scope,
            query_embedding,
            top_k,
            metadata_filter,
            self.limits,
        )
        now_ms = self._clock()
        async with self._lock:
            self._purge_expired_scope(scope, now_ms)
            records = tuple(
                record
                for key, record in self._records.items()
                if key[:3] == scope.key and metadata_matches(record.metadata, expected)
            )
        return _rank(records, query, limit)

    async def delete(
        self,
        scope: MemoryScope,
        memory_id: str,
        *,
        expected_updated_at_ms: int | None = None,
    ) -> bool:
        validate_scope_limits(scope, self.limits)
        normalized_id = _validated_memory_id(memory_id, self.limits)
        _validate_expected_version(expected_updated_at_ms)
        key = (*scope.key, normalized_id)
        async with self._lock:
            current = self._records.get(key)
            if current is None:
                if expected_updated_at_ms is not None:
                    raise MemoryConflictError("memory 不存在，无法匹配 expected version")
                return False
            _check_expected_version(current, expected_updated_at_ms)
            del self._records[key]
            return True

    async def forget(
        self,
        scope: MemoryScope,
        *,
        metadata_filter: Mapping[str, Any] | None = None,
    ) -> int:
        validate_scope_limits(scope, self.limits)
        expected = validate_metadata_filter(metadata_filter, self.limits)
        async with self._lock:
            keys = tuple(
                key
                for key, record in self._records.items()
                if key[:3] == scope.key and metadata_matches(record.metadata, expected)
            )
            for key in keys:
                del self._records[key]
            return len(keys)

    def _purge_expired_scope(self, scope: MemoryScope, now_ms: int) -> None:
        expired = tuple(
            key
            for key, record in self._records.items()
            if key[:3] == scope.key and _is_expired(record, now_ms)
        )
        for key in expired:
            del self._records[key]

    def _scope_count(self, scope: MemoryScope) -> int:
        return sum(key[:3] == scope.key for key in self._records)


class SQLiteMemoryStore:
    """AES-256-GCM encrypted persistent store with parameterized SQLite I/O."""

    def __init__(
        self,
        path: str | Path,
        *,
        key_provider: JournalKeyProvider,
        limits: MemoryLimits | None = None,
        clock: Clock | None = None,
        busy_timeout_seconds: float = 30,
    ) -> None:
        if (
            isinstance(busy_timeout_seconds, bool)
            or not isinstance(busy_timeout_seconds, int | float)
            or not math.isfinite(float(busy_timeout_seconds))
            or busy_timeout_seconds <= 0
        ):
            raise MemoryValidationError("busy_timeout_seconds 必须是正有限数字")
        active_key = key_provider.active_key()
        if not isinstance(active_key, JournalEncryptionKey):
            raise MemoryValidationError(
                "key_provider.active_key() 必须返回 JournalEncryptionKey"
            )
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.key_provider = key_provider
        self._limits = limits or MemoryLimits()
        self._clock = clock or _system_now_ms
        self.busy_timeout_seconds = float(busy_timeout_seconds)
        self._initialize_sync()

    @property
    def limits(self) -> MemoryLimits:
        return self._limits

    async def upsert(
        self,
        scope: MemoryScope,
        *,
        memory_id: str,
        text: str,
        embedding: Sequence[float],
        metadata: Mapping[str, Any],
        source: str,
        provenance: Mapping[str, Any],
        expires_at_ms: int | None = None,
        expected_updated_at_ms: int | None = None,
    ) -> MemoryRecord:
        payload = _validated_write(
            scope,
            memory_id=memory_id,
            text=text,
            embedding=embedding,
            metadata=metadata,
            source=source,
            provenance=provenance,
            expires_at_ms=expires_at_ms,
            expected_updated_at_ms=expected_updated_at_ms,
            limits=self.limits,
            now_ms=self._clock(),
        )
        return cast(
            MemoryRecord,
            await durable_to_thread(self._upsert_sync, payload),
        )

    async def get(self, scope: MemoryScope, memory_id: str) -> MemoryRecord | None:
        validate_scope_limits(scope, self.limits)
        normalized_id = _validated_memory_id(memory_id, self.limits)
        return cast(
            MemoryRecord | None,
            await durable_to_thread(
                self._get_sync,
                scope,
                normalized_id,
                self._clock(),
            ),
        )

    async def search(
        self,
        scope: MemoryScope,
        query_embedding: Sequence[float],
        *,
        top_k: int,
        metadata_filter: Mapping[str, Any] | None = None,
    ) -> tuple[MemorySearchResult, ...]:
        query, limit, expected = _validated_search(
            scope,
            query_embedding,
            top_k,
            metadata_filter,
            self.limits,
        )
        return cast(
            tuple[MemorySearchResult, ...],
            await durable_to_thread(
                self._search_sync,
                scope,
                query,
                limit,
                expected,
                self._clock(),
            ),
        )

    async def delete(
        self,
        scope: MemoryScope,
        memory_id: str,
        *,
        expected_updated_at_ms: int | None = None,
    ) -> bool:
        validate_scope_limits(scope, self.limits)
        normalized_id = _validated_memory_id(memory_id, self.limits)
        _validate_expected_version(expected_updated_at_ms)
        return cast(
            bool,
            await durable_to_thread(
                self._delete_sync,
                scope,
                normalized_id,
                expected_updated_at_ms,
            ),
        )

    async def forget(
        self,
        scope: MemoryScope,
        *,
        metadata_filter: Mapping[str, Any] | None = None,
    ) -> int:
        validate_scope_limits(scope, self.limits)
        expected = validate_metadata_filter(metadata_filter, self.limits)
        return cast(
            int,
            await durable_to_thread(self._forget_sync, scope, expected),
        )

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
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize_sync(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS semantic_memory_records (
                    tenant_id TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    memory_id TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    expires_at_ms INTEGER,
                    key_id TEXT NOT NULL,
                    nonce BLOB NOT NULL,
                    payload_ciphertext BLOB NOT NULL,
                    checksum TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    PRIMARY KEY (tenant_id, subject_id, namespace, memory_id)
                );

                CREATE INDEX IF NOT EXISTS idx_semantic_memory_scope_expiry
                ON semantic_memory_records(
                    tenant_id, subject_id, namespace, expires_at_ms
                );
                """
            )

    def _upsert_sync(self, payload: _ValidatedWrite) -> MemoryRecord:
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._purge_expired_sync(connection, payload.scope, payload.now_ms)
                row = connection.execute(
                    """
                    SELECT created_at_ms, updated_at_ms
                    FROM semantic_memory_records
                    WHERE tenant_id = ? AND subject_id = ?
                      AND namespace = ? AND memory_id = ?
                    """,
                    (*payload.scope.key, payload.memory_id),
                ).fetchone()
                current_version = int(row["updated_at_ms"]) if row is not None else None
                _check_expected_token(current_version, payload.expected_updated_at_ms)
                if row is None:
                    count = int(
                        connection.execute(
                            """
                            SELECT COUNT(*) AS count
                            FROM semantic_memory_records
                            WHERE tenant_id = ? AND subject_id = ? AND namespace = ?
                            """,
                            payload.scope.key,
                        ).fetchone()["count"]
                    )
                    if count >= self.limits.max_records_per_scope:
                        raise MemoryLimitError("scope memory record 数量超过上限")
                    created_at_ms = payload.now_ms
                    updated_at_ms = payload.now_ms
                else:
                    created_at_ms = int(row["created_at_ms"])
                    updated_at_ms = max(
                        payload.now_ms,
                        int(row["updated_at_ms"]) + 1,
                    )
                record = payload.to_record(
                    created_at_ms=created_at_ms,
                    updated_at_ms=updated_at_ms,
                )
                key = self.key_provider.active_key()
                nonce, ciphertext, checksum = _encrypt_record(record, key)
                connection.execute(
                    """
                    INSERT INTO semantic_memory_records(
                        tenant_id, subject_id, namespace, memory_id,
                        created_at_ms, updated_at_ms, expires_at_ms,
                        key_id, nonce, payload_ciphertext, checksum, schema_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tenant_id, subject_id, namespace, memory_id)
                    DO UPDATE SET
                        created_at_ms = excluded.created_at_ms,
                        updated_at_ms = excluded.updated_at_ms,
                        expires_at_ms = excluded.expires_at_ms,
                        key_id = excluded.key_id,
                        nonce = excluded.nonce,
                        payload_ciphertext = excluded.payload_ciphertext,
                        checksum = excluded.checksum,
                        schema_version = excluded.schema_version
                    """,
                    (
                        *payload.scope.key,
                        payload.memory_id,
                        record.created_at_ms,
                        record.updated_at_ms,
                        record.expires_at_ms,
                        key.key_id,
                        nonce,
                        ciphertext,
                        checksum,
                        1,
                    ),
                )
                connection.execute("COMMIT")
                return record
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def _get_sync(
        self,
        scope: MemoryScope,
        memory_id: str,
        now_ms: int,
    ) -> MemoryRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM semantic_memory_records
                WHERE tenant_id = ? AND subject_id = ?
                  AND namespace = ? AND memory_id = ?
                  AND (expires_at_ms IS NULL OR expires_at_ms > ?)
                """,
                (*scope.key, memory_id, now_ms),
            ).fetchone()
        return None if row is None else self._decode_row(row)

    def _search_sync(
        self,
        scope: MemoryScope,
        query: tuple[float, ...],
        top_k: int,
        expected: Mapping[str, Any],
        now_ms: int,
    ) -> tuple[MemorySearchResult, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM semantic_memory_records
                WHERE tenant_id = ? AND subject_id = ? AND namespace = ?
                  AND (expires_at_ms IS NULL OR expires_at_ms > ?)
                ORDER BY updated_at_ms DESC, memory_id ASC
                LIMIT ?
                """,
                (*scope.key, now_ms, self.limits.max_records_per_scope + 1),
            ).fetchall()
        if len(rows) > self.limits.max_records_per_scope:
            raise MemoryCorruptionError("SQLite scope record 数量超过可信上限")
        records = tuple(
            record
            for record in (self._decode_row(row) for row in rows)
            if metadata_matches(record.metadata, expected)
        )
        return _rank(records, query, top_k)

    def _delete_sync(
        self,
        scope: MemoryScope,
        memory_id: str,
        expected_updated_at_ms: int | None,
    ) -> bool:
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT updated_at_ms FROM semantic_memory_records
                    WHERE tenant_id = ? AND subject_id = ?
                      AND namespace = ? AND memory_id = ?
                    """,
                    (*scope.key, memory_id),
                ).fetchone()
                current_version = int(row["updated_at_ms"]) if row is not None else None
                _check_expected_token(current_version, expected_updated_at_ms)
                if row is None:
                    connection.execute("COMMIT")
                    return False
                connection.execute(
                    """
                    DELETE FROM semantic_memory_records
                    WHERE tenant_id = ? AND subject_id = ?
                      AND namespace = ? AND memory_id = ?
                    """,
                    (*scope.key, memory_id),
                )
                connection.execute("COMMIT")
                return True
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def _forget_sync(
        self,
        scope: MemoryScope,
        expected: Mapping[str, Any],
    ) -> int:
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                if not expected:
                    cursor = connection.execute(
                        """
                        DELETE FROM semantic_memory_records
                        WHERE tenant_id = ? AND subject_id = ? AND namespace = ?
                        """,
                        scope.key,
                    )
                    deleted = cursor.rowcount
                else:
                    rows = connection.execute(
                        """
                        SELECT * FROM semantic_memory_records
                        WHERE tenant_id = ? AND subject_id = ? AND namespace = ?
                        LIMIT ?
                        """,
                        (*scope.key, self.limits.max_records_per_scope + 1),
                    ).fetchall()
                    if len(rows) > self.limits.max_records_per_scope:
                        raise MemoryCorruptionError(
                            "SQLite scope record 数量超过可信上限"
                        )
                    ids = tuple(
                        record.memory_id
                        for record in (self._decode_row(row) for row in rows)
                        if metadata_matches(record.metadata, expected)
                    )
                    for memory_id in ids:
                        connection.execute(
                            """
                            DELETE FROM semantic_memory_records
                            WHERE tenant_id = ? AND subject_id = ?
                              AND namespace = ? AND memory_id = ?
                            """,
                            (*scope.key, memory_id),
                        )
                    deleted = len(ids)
                connection.execute("COMMIT")
                return max(0, deleted)
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def _purge_expired_sync(
        self,
        connection: sqlite3.Connection,
        scope: MemoryScope,
        now_ms: int,
    ) -> None:
        connection.execute(
            """
            DELETE FROM semantic_memory_records
            WHERE tenant_id = ? AND subject_id = ? AND namespace = ?
              AND expires_at_ms IS NOT NULL AND expires_at_ms <= ?
            """,
            (*scope.key, now_ms),
        )

    def _decode_row(self, row: sqlite3.Row) -> MemoryRecord:
        if int(row["schema_version"]) != 1:
            raise MemoryCorruptionError("不支持的 memory payload schema version")
        metadata = _row_aad(row)
        try:
            key = self.key_provider.get_key(str(row["key_id"]))
            payload = _decrypt_payload(
                key,
                bytes(row["nonce"]),
                bytes(row["payload_ciphertext"]),
                str(row["checksum"]),
                metadata,
            )
            if not isinstance(payload, dict) or set(payload) != {
                "text",
                "embedding",
                "metadata",
                "source",
                "provenance",
            }:
                raise MemoryCorruptionError("memory payload schema 无效")
            return MemoryRecord(
                memory_id=str(row["memory_id"]),
                tenant_id=str(row["tenant_id"]),
                subject_id=str(row["subject_id"]),
                namespace=str(row["namespace"]),
                text=payload["text"],
                embedding=tuple(payload["embedding"]),
                metadata=payload["metadata"],
                source=payload["source"],
                created_at_ms=int(row["created_at_ms"]),
                updated_at_ms=int(row["updated_at_ms"]),
                expires_at_ms=(
                    int(row["expires_at_ms"])
                    if row["expires_at_ms"] is not None
                    else None
                ),
                provenance=payload["provenance"],
            )
        except MemoryCorruptionError:
            raise
        except Exception as error:
            raise MemoryCorruptionError(
                "无法解密或验证 SQLite memory payload"
            ) from error


class _ValidatedWrite:
    def __init__(
        self,
        *,
        scope: MemoryScope,
        memory_id: str,
        text: str,
        embedding: tuple[float, ...],
        metadata: Mapping[str, Any],
        source: str,
        provenance: Mapping[str, Any],
        expires_at_ms: int | None,
        expected_updated_at_ms: int | None,
        now_ms: int,
    ) -> None:
        self.scope = scope
        self.memory_id = memory_id
        self.text = text
        self.embedding = embedding
        self.metadata = metadata
        self.source = source
        self.provenance = provenance
        self.expires_at_ms = expires_at_ms
        self.expected_updated_at_ms = expected_updated_at_ms
        self.now_ms = now_ms

    def to_record(self, *, created_at_ms: int, updated_at_ms: int) -> MemoryRecord:
        return MemoryRecord(
            memory_id=self.memory_id,
            tenant_id=self.scope.tenant_id,
            subject_id=self.scope.subject_id,
            namespace=self.scope.namespace,
            text=self.text,
            embedding=self.embedding,
            metadata=self.metadata,
            source=self.source,
            created_at_ms=created_at_ms,
            updated_at_ms=updated_at_ms,
            expires_at_ms=self.expires_at_ms,
            provenance=self.provenance,
        )


def _validated_write(
    scope: MemoryScope,
    *,
    memory_id: str,
    text: str,
    embedding: Sequence[float],
    metadata: Mapping[str, Any],
    source: str,
    provenance: Mapping[str, Any],
    expires_at_ms: int | None,
    expected_updated_at_ms: int | None,
    limits: MemoryLimits,
    now_ms: int,
) -> _ValidatedWrite:
    _validate_clock(now_ms)
    _validate_expected_version(expected_updated_at_ms)
    (
        normalized_id,
        normalized_text,
        normalized_embedding,
        normalized_metadata,
        normalized_source,
        normalized_provenance,
    ) = validate_record_payload(
        scope=scope,
        memory_id=memory_id,
        text=text,
        embedding=embedding,
        metadata=metadata,
        source=source,
        provenance=provenance,
        limits=limits,
    )
    if expires_at_ms is not None:
        if type(expires_at_ms) is not int or expires_at_ms <= now_ms:
            raise MemoryValidationError("expires_at_ms 必须是未来毫秒时间戳")
        if expires_at_ms - now_ms > limits.max_ttl_seconds * 1000:
            raise MemoryLimitError("memory TTL 超过上限")
    return _ValidatedWrite(
        scope=scope,
        memory_id=normalized_id,
        text=normalized_text,
        embedding=normalized_embedding,
        metadata=normalized_metadata,
        source=normalized_source,
        provenance=normalized_provenance,
        expires_at_ms=expires_at_ms,
        expected_updated_at_ms=expected_updated_at_ms,
        now_ms=now_ms,
    )


def _validated_search(
    scope: MemoryScope,
    query_embedding: Sequence[float],
    top_k: int,
    metadata_filter: Mapping[str, Any] | None,
    limits: MemoryLimits,
) -> tuple[tuple[float, ...], int, Mapping[str, Any]]:
    validate_scope_limits(scope, limits)
    if type(top_k) is not int or top_k <= 0:
        raise MemoryValidationError("top_k 必须是正整数")
    if top_k > limits.max_top_k:
        raise MemoryLimitError("top_k 超过上限")
    query = validate_embedding(
        query_embedding,
        dimensions=limits.embedding_dimensions,
    )
    expected = validate_metadata_filter(metadata_filter, limits)
    return query, top_k, expected


def _rank(
    records: Sequence[MemoryRecord],
    query: tuple[float, ...],
    top_k: int,
) -> tuple[MemorySearchResult, ...]:
    results = [
        MemorySearchResult(record, _cosine(query, record.embedding))
        for record in records
    ]
    results.sort(
        key=lambda result: (
            -result.score,
            -result.record.updated_at_ms,
            result.record.memory_id,
        )
    )
    return tuple(results[:top_k])


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise MemoryCorruptionError("stored embedding 维度与 query 不一致")
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(item * item for item in left))
    right_norm = math.sqrt(sum(item * item for item in right))
    if left_norm <= 0 or right_norm <= 0:
        raise MemoryCorruptionError("stored embedding 是零向量")
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


def _encrypt_record(
    record: MemoryRecord,
    key: JournalEncryptionKey,
) -> tuple[bytes, bytes, str]:
    metadata = _record_aad(record, key.key_id)
    payload = {
        "text": record.text,
        "embedding": list(record.embedding),
        "metadata": thaw_json(record.metadata),
        "source": record.source,
        "provenance": thaw_json(record.provenance),
    }
    aad = canonical_json_bytes(metadata, "memory aad")
    plaintext = canonical_json_bytes(payload, "memory payload")
    nonce = os.urandom(12)
    ciphertext = AESGCM(key.key).encrypt(nonce, plaintext, aad)
    checksum = hashlib.sha256(aad + nonce + ciphertext).hexdigest()
    return nonce, ciphertext, checksum


def _decrypt_payload(
    key: JournalEncryptionKey,
    nonce: bytes,
    ciphertext: bytes,
    checksum: str,
    metadata: Mapping[str, Any],
) -> Any:
    if len(nonce) != 12:
        raise MemoryCorruptionError("memory AES-GCM nonce 长度无效")
    aad = canonical_json_bytes(metadata, "memory aad")
    actual = hashlib.sha256(aad + nonce + ciphertext).hexdigest()
    if not hmac.compare_digest(actual, checksum):
        raise MemoryCorruptionError("memory payload checksum 校验失败")
    try:
        plaintext = AESGCM(key.key).decrypt(nonce, ciphertext, aad)
    except InvalidTag as error:
        raise MemoryCorruptionError("memory payload AES-GCM tag 校验失败") from error
    try:
        value = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MemoryCorruptionError("memory payload 不是合法 JSON") from error
    return value


def _record_aad(record: MemoryRecord, key_id: str) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "tenantId": record.tenant_id,
        "subjectId": record.subject_id,
        "namespace": record.namespace,
        "memoryId": record.memory_id,
        "createdAtMs": record.created_at_ms,
        "updatedAtMs": record.updated_at_ms,
        "expiresAtMs": record.expires_at_ms,
        "keyId": key_id,
    }


def _row_aad(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "schemaVersion": int(row["schema_version"]),
        "tenantId": str(row["tenant_id"]),
        "subjectId": str(row["subject_id"]),
        "namespace": str(row["namespace"]),
        "memoryId": str(row["memory_id"]),
        "createdAtMs": int(row["created_at_ms"]),
        "updatedAtMs": int(row["updated_at_ms"]),
        "expiresAtMs": (
            int(row["expires_at_ms"]) if row["expires_at_ms"] is not None else None
        ),
        "keyId": str(row["key_id"]),
    }


def _check_expected_version(
    current: MemoryRecord | None,
    expected_updated_at_ms: int | None,
) -> None:
    _check_expected_token(
        current.updated_at_ms if current is not None else None,
        expected_updated_at_ms,
    )


def _check_expected_token(current: int | None, expected: int | None) -> None:
    if expected is None:
        return
    if current != expected:
        raise MemoryConflictError(
            f"memory version 冲突：expected={expected} current={current}"
        )


def _validate_expected_version(value: int | None) -> None:
    if value is not None and (type(value) is not int or value < 0):
        raise MemoryValidationError("expected_updated_at_ms 必须是非负整数或 None")


def _validated_memory_id(value: str, limits: MemoryLimits) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise MemoryValidationError("memory_id 必须是非空字符串")
    normalized = value.strip()
    if len(normalized.encode("utf-8")) > limits.max_scope_component_bytes:
        raise MemoryLimitError("memory_id 超过字节上限")
    return normalized


def _validate_clock(value: Any) -> None:
    if type(value) is not int or value < 0:
        raise MemoryValidationError("clock 必须返回非负整数毫秒时间戳")


def _is_expired(record: MemoryRecord, now_ms: int) -> bool:
    return record.expires_at_ms is not None and record.expires_at_ms <= now_ms


def _system_now_ms() -> int:
    return int(time.time() * 1000)


__all__ = ["InMemoryMemoryStore", "MemoryStore", "SQLiteMemoryStore"]
