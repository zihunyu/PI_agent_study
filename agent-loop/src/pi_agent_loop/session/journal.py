"""统一、加密、租户隔离的 SQLite Session Event Journal。

Runtime、Operation、Retry 和 Audit 共享 ``session_events`` 时间线。敏感
Payload 使用 AES-256-GCM 加密，事件元数据作为 AAD；独立 SHA-256 Checksum
用于尽早识别磁盘损坏。历史事件只读，旧事件通过迁移注册表在读取时 upcast。
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import os
import sqlite3
import time
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..async_utils import durable_to_thread
from ..messages import is_sensitive_key, redact_sensitive_text
from .migrations import (
    EventMigrationRegistry,
    JournalMigrationError,
    StateMigrationRegistry,
)
from .operation_store import ClaimLease

JournalKind = Literal["runtime", "operation", "retry", "audit"]
SessionStreamKey = tuple[JournalKind, str, str | None]
_JOURNAL_KINDS = frozenset({"runtime", "operation", "retry", "audit"})
_DATABASE_SCHEMA_VERSION = 2
_INTEGRITY_MANIFEST_VERSION = 2
_AUDIT_SESSION_ID = "_journal_audit"


class SessionJournalError(RuntimeError):
    """统一 Journal 基础错误。"""


class JournalAccessDenied(SessionJournalError):
    """Principal 没有所需权限。"""


class JournalConflictError(SessionJournalError):
    """CAS、唯一约束或 Stream Sequence 冲突。"""


class JournalFencedClaimLostError(JournalConflictError):
    """The exact owner/generation lease is no longer current and unexpired."""


class JournalCorruptionError(SessionJournalError):
    """Checksum、AEAD Tag 或持久 JSON 校验失败。"""


class JournalDeadlineExceeded(JournalConflictError):
    """事务 Claim 前业务截止时间已经到期。"""


@dataclass(frozen=True, slots=True)
class JournalPrincipal:
    principal_id: str
    tenant_id: str
    roles: frozenset[str]

    def __post_init__(self) -> None:
        if not self.principal_id or not self.tenant_id:
            raise ValueError("Journal Principal 必须包含 principal_id/tenant_id")
        if not self.roles or any(not role for role in self.roles):
            raise ValueError("Journal Principal 必须包含有效 Role")

    @classmethod
    def system(
        cls,
        tenant_id: str,
        *,
        principal_id: str = "journal-system",
    ) -> "JournalPrincipal":
        return cls(principal_id, tenant_id, frozenset({"system"}))


@dataclass(frozen=True, slots=True)
class JournalAccessPolicy:
    read_roles: frozenset[str] = frozenset(
        {"system", "journal_reader", "journal_writer", "journal_admin"}
    )
    write_roles: frozenset[str] = frozenset(
        {"system", "journal_writer", "journal_admin"}
    )
    export_roles: frozenset[str] = frozenset({"system", "journal_admin"})
    delete_roles: frozenset[str] = frozenset({"system", "journal_admin"})
    migrate_roles: frozenset[str] = frozenset({"system", "journal_admin"})
    audit_roles: frozenset[str] = frozenset(
        {"system", "journal_admin", "journal_auditor"}
    )

    def authorize(self, principal: JournalPrincipal, action: str) -> None:
        allowed = {
            "read": self.read_roles,
            "write": self.write_roles,
            "export": self.export_roles,
            "delete": self.delete_roles,
            "migrate": self.migrate_roles,
            "audit": self.audit_roles,
        }.get(action)
        if allowed is None:
            raise ValueError(f"未知 Journal Action：{action}")
        if principal.roles.isdisjoint(allowed):
            raise JournalAccessDenied(
                f"Principal {principal.principal_id} 无权执行 Journal {action}"
            )


@dataclass(frozen=True, slots=True)
class JournalRedactionPolicy:
    """对只读/审计视图脱敏；加密原文仍供受信 Runtime 恢复。"""

    sensitive_keys: frozenset[str] = frozenset(
        {
            "apikey",
            "authorization",
            "cookie",
            "credential",
            "password",
            "secret",
            "token",
            "idempotencykey",
        }
    )
    replacement: str = "<redacted>"
    redacted_read_roles: frozenset[str] = frozenset(
        {"journal_reader", "journal_auditor"}
    )
    default_retention_seconds: int | None = None
    audit_retention_seconds: int | None = None

    def __post_init__(self) -> None:
        for value in (
            self.default_retention_seconds,
            self.audit_retention_seconds,
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise ValueError("Retention Seconds 必须为正整数或 None")

    def redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            redacted: dict[str, Any] = {}
            sensitive = {
                item.casefold().replace("_", "").replace("-", "")
                for item in self.sensitive_keys
            }
            for raw_key, item in value.items():
                key = str(raw_key)
                normalized = key.casefold().replace("_", "").replace("-", "")
                redacted[key] = (
                    self.replacement
                    if normalized in sensitive or is_sensitive_key(key)
                    else self.redact(item)
                )
            return redacted
        if isinstance(value, (list, tuple)):
            return [self.redact(item) for item in value]
        if isinstance(value, str):
            return redact_sensitive_text(value, replacement=self.replacement)
        return copy.deepcopy(value)


@dataclass(frozen=True, slots=True)
class JournalEncryptionKey:
    key_id: str
    key: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not self.key_id:
            raise ValueError("Encryption Key ID 不能为空")
        if not isinstance(self.key, bytes) or len(self.key) != 32:
            raise ValueError("AES-256-GCM Key 必须恰好为 32 Bytes")


class JournalKeyProvider(Protocol):
    def active_key(self) -> JournalEncryptionKey: ...

    def get_key(self, key_id: str) -> JournalEncryptionKey: ...


class StaticJournalKeyProvider:
    """测试/本地 Key Ring；生产可实现同一 Protocol 对接 KMS。"""

    def __init__(self, keys: Mapping[str, bytes], *, active_key_id: str) -> None:
        self._keys = {
            key_id: JournalEncryptionKey(key_id, bytes(key))
            for key_id, key in keys.items()
        }
        self.set_active(active_key_id)

    def active_key(self) -> JournalEncryptionKey:
        return self._keys[self._active_key_id]

    def get_key(self, key_id: str) -> JournalEncryptionKey:
        try:
            return self._keys[key_id]
        except KeyError as error:
            raise JournalCorruptionError(f"找不到 Journal Encryption Key：{key_id}") from error

    def set_active(self, key_id: str) -> None:
        if key_id not in self._keys:
            raise ValueError(f"Active Encryption Key 不存在：{key_id}")
        self._active_key_id = key_id


class EnvironmentJournalKeyProvider(StaticJournalKeyProvider):
    """从环境变量读取 Base64 编码的 32-byte AES Key。"""

    def __init__(self, variable: str, *, key_id: str = "env-v1") -> None:
        encoded = os.environ.get(variable)
        if not encoded:
            raise RuntimeError(f"缺少 Journal Encryption Key 环境变量：{variable}")
        try:
            key = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as error:
            raise RuntimeError("Journal Encryption Key 必须是合法 Base64") from error
        super().__init__({key_id: key}, active_key_id=key_id)


@dataclass(frozen=True, slots=True)
class SessionEventSpec:
    journal_kind: JournalKind
    event_type: str
    session_id: str
    payload: dict[str, Any]
    operation_id: str | None = None
    run_id: str | None = None
    source_sequence: int | None = None
    schema_version: int = 1
    state_version: int = 1
    timestamp: int | None = None
    event_id: str | None = None
    retention_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.journal_kind not in _JOURNAL_KINDS:
            raise ValueError(f"Journal Kind 无效：{self.journal_kind}")
        if not self.event_type or not self.session_id:
            raise ValueError("Session Event 必须包含 event_type/session_id")
        if not isinstance(self.payload, dict):
            raise ValueError("Session Event Payload 必须是对象")
        if self.journal_kind == "operation" and not self.operation_id:
            raise ValueError("Operation Journal Event 必须包含 operation_id")
        if self.journal_kind == "runtime" and not self.run_id:
            raise ValueError("Runtime Journal Event 必须包含 run_id")
        _validate_version(self.schema_version, "Event Schema Version")
        _validate_version(self.state_version, "State Version")
        if self.source_sequence is not None and (
            isinstance(self.source_sequence, bool)
            or not isinstance(self.source_sequence, int)
            or self.source_sequence < 0
        ):
            raise ValueError("Source Sequence 必须是非负整数")
        if self.timestamp is not None and (
            isinstance(self.timestamp, bool)
            or not isinstance(self.timestamp, int)
            or self.timestamp < 0
        ):
            raise ValueError("Session Event Timestamp 无效")
        if self.retention_seconds is not None and (
            isinstance(self.retention_seconds, bool)
            or not isinstance(self.retention_seconds, int)
            or self.retention_seconds <= 0
        ):
            raise ValueError("Retention Seconds 必须为正整数")


@dataclass(frozen=True, slots=True)
class SessionEvent:
    event_id: str
    tenant_id: str
    session_id: str
    operation_id: str | None
    run_id: str | None
    journal_kind: JournalKind
    event_type: str
    sequence: int
    source_sequence: int | None
    schema_version: int
    state_version: int
    payload: dict[str, Any]
    timestamp: int
    actor_id: str
    expires_at: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "tenant_id": self.tenant_id,
            "session_id": self.session_id,
            "operation_id": self.operation_id,
            "run_id": self.run_id,
            "journal_kind": self.journal_kind,
            "event_type": self.event_type,
            "sequence": self.sequence,
            "source_sequence": self.source_sequence,
            "schema_version": self.schema_version,
            "state_version": self.state_version,
            "payload": copy.deepcopy(self.payload),
            "timestamp": self.timestamp,
            "actor_id": self.actor_id,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    tenant_id: str
    session_id: str
    projection_name: str
    last_sequence: int
    state_version: int
    event_schema_version: int
    state: Any
    timestamp: int
    expires_at: int | None = None


@dataclass(frozen=True, slots=True)
class ProjectionReplay:
    state: Any
    last_sequence: int
    snapshot_sequence: int
    applied_events: int


@dataclass(frozen=True, slots=True)
class SessionJournalCapabilities:
    backend_name: str
    atomic_multi_stream_append: bool
    atomic_fenced_append: bool
    supports_cross_process: bool
    supports_multi_host: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.backend_name, str) or not self.backend_name.strip():
            raise ValueError("Journal backend_name must be non-empty")
        for name in (
            "atomic_multi_stream_append",
            "atomic_fenced_append",
            "supports_cross_process",
            "supports_multi_host",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"Journal {name} must be boolean")
        if self.supports_multi_host and not self.supports_cross_process:
            raise ValueError("multi-host Journal must support cross-process access")


@runtime_checkable
class SessionEventJournal(Protocol):
    """Transactional journal contract; CAS and fencing are part of the append transaction.

    All stream heads are checked and all specs committed together, or none are.
    Claim generations are monotonic; expired/old owners cannot append or renew.
    Scope/authorization, immutable event identity, deduplication and authenticated
    payload storage remain backend responsibilities. Capabilities describe the
    deployed topology, not merely the availability of method names.
    """

    @property
    def capabilities(self) -> SessionJournalCapabilities: ...

    async def append_events(
        self,
        principal: JournalPrincipal,
        specs: list[SessionEventSpec],
        *,
        expected_last_sequence: int | None = None,
        expected_stream_sequences: Mapping[SessionStreamKey, int] | None = None,
        deadline_ms: int | None = None,
    ) -> list[SessionEvent]: ...

    async def append_events_if_fenced_claim(
        self,
        principal: JournalPrincipal,
        specs: list[SessionEventSpec],
        lease: ClaimLease,
        *,
        renew_lease_seconds: float,
        expected_last_sequence: int | None = None,
        expected_stream_sequences: Mapping[SessionStreamKey, int] | None = None,
        deadline_ms: int | None = None,
    ) -> list[SessionEvent]: ...

    async def load_events(
        self,
        principal: JournalPrincipal,
        *,
        session_id: str | None = None,
        operation_id: str | None = None,
        run_id: str | None = None,
        journal_kind: JournalKind | None = None,
        after_sequence: int | None = None,
        include_expired: bool = False,
        migration_registry: EventMigrationRegistry | None = None,
        target_schema_version: int | None = None,
    ) -> list[SessionEvent]: ...

    async def export_session(
        self, principal: JournalPrincipal, session_id: str
    ) -> list[dict[str, Any]]: ...

    async def delete_session(
        self, principal: JournalPrincipal, session_id: str
    ) -> int: ...

    async def purge_expired(
        self, principal: JournalPrincipal, *, now_ms: int | None = None
    ) -> int: ...

    async def load_audit_events(
        self, principal: JournalPrincipal
    ) -> list[SessionEvent]: ...

    async def verify_integrity(
        self, principal: JournalPrincipal
    ) -> tuple[int, int]: ...

    async def rotate_encryption_keys(
        self, principal: JournalPrincipal
    ) -> tuple[int, int]: ...

    async def run_event_migrations(
        self,
        principal: JournalPrincipal,
        registry: EventMigrationRegistry,
        *,
        target_version: int | None = None,
        session_id: str | None = None,
    ) -> list[SessionEvent]: ...

    async def save_snapshot(
        self,
        principal: JournalPrincipal,
        *,
        session_id: str,
        projection_name: str,
        last_sequence: int,
        state: Any,
        state_version: int,
        event_schema_version: int | None = None,
        retention_seconds: int | None = None,
    ) -> SessionSnapshot: ...

    async def load_snapshot(
        self,
        principal: JournalPrincipal,
        *,
        session_id: str,
        projection_name: str,
        state_registry: StateMigrationRegistry | None = None,
        target_state_version: int | None = None,
    ) -> SessionSnapshot | None: ...

    async def migrate_snapshot(
        self,
        principal: JournalPrincipal,
        *,
        session_id: str,
        projection_name: str,
        registry: StateMigrationRegistry,
        target_state_version: int,
    ) -> SessionSnapshot: ...

    async def replay_projection(
        self,
        principal: JournalPrincipal,
        *,
        session_id: str,
        projection_name: str,
        initial_state: Any,
        reducer: Callable[[Any, SessionEvent], Any],
        journal_kind: JournalKind | None = None,
        state_registry: StateMigrationRegistry | None = None,
        target_state_version: int | None = None,
        event_registry: EventMigrationRegistry | None = None,
        target_event_schema_version: int | None = None,
    ) -> ProjectionReplay: ...

    async def try_acquire_claim(
        self,
        principal: JournalPrincipal,
        claim_type: str,
        resource_id: str,
        owner_token: str,
        *,
        lease_seconds: float = 300,
    ) -> bool: ...

    async def acquire_fenced_claim(
        self,
        principal: JournalPrincipal,
        claim_type: str,
        resource_id: str,
        owner_token: str,
        *,
        lease_seconds: float = 300,
    ) -> ClaimLease | None: ...

    async def renew_fenced_claim(
        self,
        principal: JournalPrincipal,
        lease: ClaimLease,
        *,
        lease_seconds: float = 300,
    ) -> bool: ...

    async def verify_fenced_claim(
        self, principal: JournalPrincipal, lease: ClaimLease
    ) -> bool: ...

    async def release_fenced_claim(
        self, principal: JournalPrincipal, lease: ClaimLease
    ) -> None: ...

    async def release_claim(
        self,
        principal: JournalPrincipal,
        claim_type: str,
        resource_id: str,
        owner_token: str,
    ) -> None: ...


def validate_session_event_journal(
    journal: Any, *, require_multi_host: bool = False
) -> SessionEventJournal:
    if not isinstance(journal, SessionEventJournal):
        raise TypeError("Journal must implement SessionEventJournal")
    capabilities = journal.capabilities
    if not isinstance(capabilities, SessionJournalCapabilities) or not (
        capabilities.atomic_multi_stream_append
        and capabilities.atomic_fenced_append
        and capabilities.supports_cross_process
        and (not require_multi_host or capabilities.supports_multi_host)
    ):
        raise ValueError(
            "Journal lacks atomic multi-stream, CAS, fencing or requested topology guarantees"
        )
    return journal


class SQLiteSessionEventJournal:
    """统一 Session Journal、Snapshot Store 和数据治理边界。"""

    capabilities = SessionJournalCapabilities("sqlite-session-journal", True, True, True, False)

    def __init__(
        self,
        path: str | Path,
        *,
        key_provider: JournalKeyProvider,
        access_policy: JournalAccessPolicy | None = None,
        redaction_policy: JournalRedactionPolicy | None = None,
        current_event_schema_version: int = 1,
        busy_timeout_seconds: float = 30,
        allow_legacy_integrity_bootstrap: bool = False,
    ) -> None:
        if busy_timeout_seconds <= 0:
            raise ValueError("busy_timeout_seconds 必须大于 0")
        _validate_version(current_event_schema_version, "Current Event Schema Version")
        # 构造时即验证 Active Key，禁止退化成明文 Store。
        key_provider.active_key()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.key_provider = key_provider
        self.access_policy = access_policy or JournalAccessPolicy()
        self.redaction_policy = redaction_policy or JournalRedactionPolicy()
        self.current_event_schema_version = current_event_schema_version
        self.busy_timeout_seconds = busy_timeout_seconds
        # 旧库没有可信 Manifest，自动为已有数据签名会把被删改后的状态当成
        # 新基线。生产默认 fail-closed；只有一次性、受控迁移可以显式开启。
        self.allow_legacy_integrity_bootstrap = allow_legacy_integrity_bootstrap
        self._initialize_sync()

    async def append_events(
        self,
        principal: JournalPrincipal,
        specs: list[SessionEventSpec],
        *,
        expected_last_sequence: int | None = None,
        expected_stream_sequences: Mapping[SessionStreamKey, int] | None = None,
        deadline_ms: int | None = None,
    ) -> list[SessionEvent]:
        self.access_policy.authorize(principal, "write")
        if not specs:
            raise ValueError("Session Event Batch 不能为空")
        return await durable_to_thread(
            self._append_events_sync,
            principal,
            specs,
            expected_last_sequence,
            _validated_stream_heads(expected_stream_sequences),
            deadline_ms,
        )

    async def append_events_if_fenced_claim(
        self,
        principal: JournalPrincipal,
        specs: list[SessionEventSpec],
        lease: ClaimLease,
        *,
        renew_lease_seconds: float,
        expected_last_sequence: int | None = None,
        expected_stream_sequences: Mapping[SessionStreamKey, int] | None = None,
        deadline_ms: int | None = None,
    ) -> list[SessionEvent]:
        """Verify, append and renew one exact fenced lease transactionally.

        The lease is required to be live when the SQLite write transaction
        starts.  ``BEGIN IMMEDIATE`` then prevents a successor from taking the
        same claim until the events and the lease renewal commit together.
        """

        self.access_policy.authorize(principal, "write")
        if not specs:
            raise ValueError("Session Event Batch 不能为空")
        if not isinstance(lease, ClaimLease):
            raise TypeError("lease 必须是 ClaimLease")
        if (
            isinstance(renew_lease_seconds, bool)
            or not isinstance(renew_lease_seconds, (int, float))
            or renew_lease_seconds <= 0
        ):
            raise ValueError("renew_lease_seconds 必须是正数")
        return await durable_to_thread(
            self._append_events_if_fenced_claim_sync,
            principal,
            specs,
            lease,
            float(renew_lease_seconds),
            expected_last_sequence,
            _validated_stream_heads(expected_stream_sequences),
            deadline_ms,
        )

    async def load_events(
        self,
        principal: JournalPrincipal,
        *,
        session_id: str | None = None,
        operation_id: str | None = None,
        run_id: str | None = None,
        journal_kind: JournalKind | None = None,
        after_sequence: int | None = None,
        include_expired: bool = False,
        migration_registry: EventMigrationRegistry | None = None,
        target_schema_version: int | None = None,
    ) -> list[SessionEvent]:
        self.access_policy.authorize(principal, "read")
        return await durable_to_thread(
            self._load_events_sync,
            principal,
            session_id,
            operation_id,
            run_id,
            journal_kind,
            after_sequence,
            include_expired,
            migration_registry,
            target_schema_version,
        )

    def load_events_sync(self, principal: JournalPrincipal, *, session_id: str | None=None, operation_id: str | None=None, run_id: str | None=None, journal_kind: JournalKind | None=None, after_sequence: int | None=None, include_expired: bool=False, migration_registry: EventMigrationRegistry | None=None, target_schema_version: int | None=None) -> list[SessionEvent]:
        """Legacy synchronous adapter; prefer await load_events in async code."""
        self.access_policy.authorize(principal, "read")
        return self._load_events_sync(principal, session_id, operation_id, run_id, journal_kind, after_sequence, include_expired, migration_registry, target_schema_version)

    async def export_session(
        self,
        principal: JournalPrincipal,
        session_id: str,
    ) -> list[dict[str, Any]]:
        self.access_policy.authorize(principal, "export")
        if not session_id:
            raise ValueError("Export Session ID 不能为空")
        return await durable_to_thread(
            self._export_session_sync, principal, session_id
        )

    async def delete_session(
        self,
        principal: JournalPrincipal,
        session_id: str,
    ) -> int:
        self.access_policy.authorize(principal, "delete")
        if not session_id or session_id == _AUDIT_SESSION_ID:
            raise ValueError("不能删除空 Session 或 Journal Audit Session")
        return await durable_to_thread(
            self._delete_session_sync, principal, session_id
        )

    async def purge_expired(
        self,
        principal: JournalPrincipal,
        *,
        now_ms: int | None = None,
    ) -> int:
        self.access_policy.authorize(principal, "delete")
        effective_now = int(time.time() * 1000) if now_ms is None else now_ms
        if isinstance(effective_now, bool) or not isinstance(effective_now, int):
            raise ValueError("Retention now_ms 无效")
        return await durable_to_thread(
            self._purge_expired_sync, principal, effective_now
        )

    async def load_audit_events(
        self,
        principal: JournalPrincipal,
    ) -> list[SessionEvent]:
        self.access_policy.authorize(principal, "audit")
        return await durable_to_thread(self._load_audit_events_sync, principal)

    async def verify_integrity(
        self,
        principal: JournalPrincipal,
    ) -> tuple[int, int]:
        self.access_policy.authorize(principal, "audit")
        return await durable_to_thread(self._verify_integrity_sync, principal)

    async def rotate_encryption_keys(
        self,
        principal: JournalPrincipal,
    ) -> tuple[int, int]:
        """用 Key Provider 当前 Active Key 重加密本租户 Event/Snapshot。"""

        self.access_policy.authorize(principal, "migrate")
        return await durable_to_thread(self._rotate_keys_sync, principal)

    async def run_event_migrations(
        self,
        principal: JournalPrincipal,
        registry: EventMigrationRegistry,
        *,
        target_version: int | None = None,
        session_id: str | None = None,
    ) -> list[SessionEvent]:
        """验证并返回 upcast 后事件；不篡改不可变历史 Event。"""

        self.access_policy.authorize(principal, "migrate")
        target = target_version or self.current_event_schema_version
        _validate_version(target, "Migration Target Version")
        if target > self.current_event_schema_version:
            raise JournalMigrationError("Migration 目标版本高于当前 Runtime 支持版本")
        return await durable_to_thread(
            self._run_event_migrations_sync,
            principal,
            registry,
            target,
            session_id,
        )

    async def save_snapshot(
        self,
        principal: JournalPrincipal,
        *,
        session_id: str,
        projection_name: str,
        last_sequence: int,
        state: Any,
        state_version: int,
        event_schema_version: int | None = None,
        retention_seconds: int | None = None,
    ) -> SessionSnapshot:
        self.access_policy.authorize(principal, "write")
        return await durable_to_thread(
            self._save_snapshot_sync,
            principal,
            session_id,
            projection_name,
            last_sequence,
            state,
            state_version,
            event_schema_version or self.current_event_schema_version,
            retention_seconds,
        )

    async def load_snapshot(
        self,
        principal: JournalPrincipal,
        *,
        session_id: str,
        projection_name: str,
        state_registry: StateMigrationRegistry | None = None,
        target_state_version: int | None = None,
    ) -> SessionSnapshot | None:
        if principal.roles.isdisjoint(self.access_policy.read_roles):
            # Auditor 只能读取脱敏 Snapshot View，不能因此获得普通 Event Read。
            self.access_policy.authorize(principal, "audit")
        else:
            self.access_policy.authorize(principal, "read")
        return await durable_to_thread(
            self._load_snapshot_sync,
            principal,
            session_id,
            projection_name,
            state_registry,
            target_state_version,
        )

    async def migrate_snapshot(
        self,
        principal: JournalPrincipal,
        *,
        session_id: str,
        projection_name: str,
        registry: StateMigrationRegistry,
        target_state_version: int,
    ) -> SessionSnapshot:
        self.access_policy.authorize(principal, "migrate")
        return await durable_to_thread(
            self._migrate_snapshot_sync,
            principal,
            session_id,
            projection_name,
            registry,
            target_state_version,
        )

    async def replay_projection(
        self,
        principal: JournalPrincipal,
        *,
        session_id: str,
        projection_name: str,
        initial_state: Any,
        reducer: Callable[[Any, SessionEvent], Any],
        journal_kind: JournalKind | None = None,
        state_registry: StateMigrationRegistry | None = None,
        target_state_version: int | None = None,
        event_registry: EventMigrationRegistry | None = None,
        target_event_schema_version: int | None = None,
    ) -> ProjectionReplay:
        snapshot = await self.load_snapshot(
            principal,
            session_id=session_id,
            projection_name=projection_name,
            state_registry=state_registry,
            target_state_version=target_state_version,
        )
        state = copy.deepcopy(snapshot.state if snapshot else initial_state)
        snapshot_sequence = snapshot.last_sequence if snapshot else -1
        events = await self.load_events(
            principal,
            session_id=session_id,
            journal_kind=journal_kind,
            after_sequence=snapshot_sequence,
            migration_registry=event_registry,
            target_schema_version=target_event_schema_version,
        )
        for event in events:
            state = reducer(state, event)
        return ProjectionReplay(
            state=state,
            last_sequence=events[-1].sequence if events else snapshot_sequence,
            snapshot_sequence=snapshot_sequence,
            applied_events=len(events),
        )

    async def try_acquire_claim(
        self,
        principal: JournalPrincipal,
        claim_type: str,
        resource_id: str,
        owner_token: str,
        *,
        lease_seconds: float = 300,
    ) -> bool:
        self.access_policy.authorize(principal, "write")
        _validate_claim(claim_type, resource_id, owner_token, lease_seconds)
        lease = await durable_to_thread(
            self._acquire_fenced_claim_sync,
            principal,
            claim_type,
            resource_id,
            owner_token,
            lease_seconds,
        )
        return lease is not None

    async def acquire_fenced_claim(
        self,
        principal: JournalPrincipal,
        claim_type: str,
        resource_id: str,
        owner_token: str,
        *,
        lease_seconds: float = 300,
    ) -> ClaimLease | None:
        self.access_policy.authorize(principal, "write")
        _validate_claim(claim_type, resource_id, owner_token, lease_seconds)
        return await durable_to_thread(
            self._acquire_fenced_claim_sync,
            principal,
            claim_type,
            resource_id,
            owner_token,
            lease_seconds,
        )

    async def renew_fenced_claim(
        self,
        principal: JournalPrincipal,
        lease: ClaimLease,
        *,
        lease_seconds: float = 300,
    ) -> bool:
        self.access_policy.authorize(principal, "write")
        _validate_claim(
            lease.claim_type,
            lease.resource_id,
            lease.owner_token,
            lease_seconds,
        )
        return await durable_to_thread(
            self._renew_fenced_claim_sync,
            principal,
            lease,
            lease_seconds,
        )

    async def verify_fenced_claim(
        self,
        principal: JournalPrincipal,
        lease: ClaimLease,
    ) -> bool:
        self.access_policy.authorize(principal, "read")
        return await durable_to_thread(
            self._verify_fenced_claim_sync,
            principal,
            lease,
        )

    async def release_fenced_claim(
        self,
        principal: JournalPrincipal,
        lease: ClaimLease,
    ) -> None:
        self.access_policy.authorize(principal, "write")
        await durable_to_thread(
            self._release_fenced_claim_sync,
            principal,
            lease,
        )

    async def release_claim(
        self,
        principal: JournalPrincipal,
        claim_type: str,
        resource_id: str,
        owner_token: str,
    ) -> None:
        self.access_policy.authorize(principal, "write")
        await durable_to_thread(
            self._release_claim_sync,
            principal,
            claim_type,
            resource_id,
            owner_token,
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
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize_sync(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            preexisting_store = bool(
                connection.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name IN (
                        'session_journal_meta', 'session_events',
                        'session_snapshots', 'session_integrity_manifests'
                    )
                    LIMIT 1
                    """
                ).fetchone()
            )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS session_journal_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS session_events (
                    sequence INTEGER PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    tenant_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    operation_id TEXT,
                    run_id TEXT,
                    journal_kind TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    source_sequence INTEGER,
                    schema_version INTEGER NOT NULL,
                    state_version INTEGER NOT NULL,
                    timestamp INTEGER NOT NULL,
                    actor_id TEXT NOT NULL,
                    key_id TEXT NOT NULL,
                    nonce BLOB NOT NULL,
                    payload_ciphertext BLOB NOT NULL,
                    checksum TEXT NOT NULL,
                    expires_at INTEGER,
                    approval_id TEXT,
                    write_id TEXT,
                    idempotency_key_hash TEXT,
                    CHECK (journal_kind IN ('runtime','operation','retry','audit')),
                    CHECK (schema_version >= 1),
                    CHECK (state_version >= 1)
                );

                CREATE INDEX IF NOT EXISTS idx_session_events_timeline
                ON session_events(tenant_id, session_id, sequence);

                CREATE INDEX IF NOT EXISTS idx_session_events_operation
                ON session_events(tenant_id, operation_id, sequence);

                CREATE INDEX IF NOT EXISTS idx_session_events_run
                ON session_events(tenant_id, run_id, sequence);

                CREATE INDEX IF NOT EXISTS idx_session_events_expiry
                ON session_events(tenant_id, expires_at);

                CREATE UNIQUE INDEX IF NOT EXISTS uq_session_approval_request
                ON session_events(tenant_id, approval_id)
                WHERE event_type = 'approval_requested' AND approval_id IS NOT NULL;

                CREATE UNIQUE INDEX IF NOT EXISTS uq_session_write_prepare
                ON session_events(tenant_id, write_id)
                WHERE event_type = 'write_prepared' AND write_id IS NOT NULL;

                CREATE UNIQUE INDEX IF NOT EXISTS uq_session_write_idempotency
                ON session_events(tenant_id, idempotency_key_hash)
                WHERE event_type = 'write_prepared'
                  AND idempotency_key_hash IS NOT NULL;

                DROP INDEX IF EXISTS uq_runtime_source_sequence;

                CREATE UNIQUE INDEX uq_runtime_source_sequence
                ON session_events(tenant_id, session_id, source_sequence)
                WHERE journal_kind = 'runtime' AND source_sequence IS NOT NULL;

                CREATE TABLE IF NOT EXISTS session_snapshots (
                    tenant_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    projection_name TEXT NOT NULL,
                    last_sequence INTEGER NOT NULL,
                    state_version INTEGER NOT NULL,
                    event_schema_version INTEGER NOT NULL,
                    timestamp INTEGER NOT NULL,
                    key_id TEXT NOT NULL,
                    nonce BLOB NOT NULL,
                    state_ciphertext BLOB NOT NULL,
                    checksum TEXT NOT NULL,
                    expires_at INTEGER,
                    PRIMARY KEY (tenant_id, session_id, projection_name)
                );

                CREATE TABLE IF NOT EXISTS session_claims (
                    tenant_id TEXT NOT NULL,
                    claim_type TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    owner_token TEXT NOT NULL,
                    lease_expires_at INTEGER NOT NULL,
                    generation INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (tenant_id, claim_type, resource_id)
                );

                CREATE TABLE IF NOT EXISTS session_integrity_manifests (
                    tenant_id TEXT PRIMARY KEY,
                    manifest_version INTEGER NOT NULL,
                    key_id TEXT NOT NULL,
                    event_count INTEGER NOT NULL,
                    snapshot_count INTEGER NOT NULL,
                    manifest_hmac TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    CHECK (manifest_version >= 1),
                    CHECK (event_count >= 0),
                    CHECK (snapshot_count >= 0)
                );
                """
            )
            claim_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(session_claims)")
            }
            if "generation" not in claim_columns:
                # Claims are unsigned coordination metadata. This additive
                # migration does not alter the protected event/snapshot roots.
                connection.execute(
                    "ALTER TABLE session_claims "
                    "ADD COLUMN generation INTEGER NOT NULL DEFAULT 1"
                )
            row = connection.execute(
                "SELECT value FROM session_journal_meta WHERE key = 'database_schema_version'"
            ).fetchone()
            if row is None:
                if preexisting_store:
                    raise JournalCorruptionError(
                        "已有 Session Journal Schema 但缺少 Database Schema Version"
                    )
                # 只有真正的全新空库可以自动建立可信根。已有 Schema 即使已被
                # 清空也不能当成新库，否则攻击者可删数据/标记后重新签名。
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO session_journal_meta(key, value) VALUES
                        ('database_schema_version', ?),
                        ('integrity_manifest_version', ?),
                        ('next_sequence', '0')
                    """,
                    (
                        str(_DATABASE_SCHEMA_VERSION),
                        str(_INTEGRITY_MANIFEST_VERSION),
                    ),
                )
                connection.execute("COMMIT")
                database_schema_version = _DATABASE_SCHEMA_VERSION
            else:
                database_schema_version = int(row["value"])
            if database_schema_version not in {1, _DATABASE_SCHEMA_VERSION}:
                raise RuntimeError(
                    "不支持的 Session Journal Database Schema Version："
                    f"{database_schema_version}"
                )
            sequence_row = connection.execute(
                "SELECT value FROM session_journal_meta WHERE key = 'next_sequence'"
            ).fetchone()
            if sequence_row is None:
                if (
                    database_schema_version != 1
                    or not self.allow_legacy_integrity_bootstrap
                ):
                    raise JournalCorruptionError(
                        "已有 Session Journal 缺少 next_sequence"
                    )
                connection.execute(
                    """
                    INSERT INTO session_journal_meta(key, value)
                    SELECT 'next_sequence',
                           CAST(COALESCE(MAX(sequence), -1) + 1 AS TEXT)
                    FROM session_events
                    """
                )
            self._initialize_integrity_manifests_sync(
                connection,
                database_schema_version=database_schema_version,
            )

    def _initialize_integrity_manifests_sync(
        self,
        connection: sqlite3.Connection,
        *,
        database_schema_version: int,
    ) -> None:
        """验证可信根，或在显式授权下完成一次性旧库迁移。"""

        connection.execute("BEGIN IMMEDIATE")
        try:
            version_row = connection.execute(
                """
                SELECT value FROM session_journal_meta
                WHERE key = 'integrity_manifest_version'
                """
            ).fetchone()
            tenants = self._manifest_tenant_ids_sync(connection)
            if database_schema_version == 1:
                if not self.allow_legacy_integrity_bootstrap:
                    raise JournalCorruptionError(
                        "已有 Legacy Session Journal 默认禁止自动建立 Integrity "
                        "Manifest；请在受控迁移中显式设置 "
                        "allow_legacy_integrity_bootstrap=True"
                    )
                if version_row is not None:
                    raise JournalCorruptionError(
                        "Legacy Session Journal 出现非预期 Integrity Manifest 标记"
                    )
                partial = connection.execute(
                    "SELECT COUNT(*) AS value FROM session_integrity_manifests"
                ).fetchone()
                if int(partial["value"]) != 0:
                    raise JournalCorruptionError(
                        "Integrity Manifest Migration 标记缺失但已存在清单"
                    )
                # 旧库只允许在这一条显式迁移路径中建立初始清单。先验证每条
                # AEAD/Checksum，不能把已损坏旧数据直接签成可信状态。
                for tenant_id in tenants:
                    event_rows, snapshot_rows = self._protected_rows_sync(
                        connection, tenant_id
                    )
                    for row in event_rows:
                        self._decode_event_row(row)
                    for row in snapshot_rows:
                        self._decode_snapshot_row(row)
                # 先固定最终关键 Meta，再按新算法签名。保留旧 allocator 的更高
                # High-water Mark，无法证明历史时绝不主动向后压缩。
                max_sequence = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(sequence), -1) AS value FROM session_events"
                    ).fetchone()["value"]
                )
                sequence_row = connection.execute(
                    """
                    SELECT value FROM session_journal_meta
                    WHERE key = 'next_sequence'
                    """
                ).fetchone()
                try:
                    next_sequence = int(sequence_row["value"])
                except (TypeError, ValueError):
                    next_sequence = max_sequence + 1
                next_sequence = max(next_sequence, max_sequence + 1)
                connection.execute(
                    """
                    UPDATE session_journal_meta SET value = ?
                    WHERE key = 'database_schema_version'
                    """,
                    (str(_DATABASE_SCHEMA_VERSION),),
                )
                connection.execute(
                    """
                    INSERT INTO session_journal_meta(key, value)
                    VALUES ('integrity_manifest_version', ?)
                    """,
                    (str(_INTEGRITY_MANIFEST_VERSION),),
                )
                connection.execute(
                    """
                    UPDATE session_journal_meta SET value = ?
                    WHERE key = 'next_sequence'
                    """,
                    (str(next_sequence),),
                )
                self._validate_critical_meta_sync(connection)
                self._refresh_all_manifests_sync(connection)
            else:
                if version_row is None:
                    raise JournalCorruptionError(
                        "已迁移 Session Journal 缺少 Integrity Manifest 标记"
                    )
                manifest_version = int(version_row["value"])
                if manifest_version == 1:
                    if not self.allow_legacy_integrity_bootstrap:
                        raise JournalCorruptionError(
                            "Legacy Integrity Manifest 必须通过显式受控迁移升级"
                        )
                    for tenant_id in tenants:
                        self._verify_legacy_manifest_sync(connection, tenant_id)
                    connection.execute(
                        """
                        UPDATE session_journal_meta SET value = ?
                        WHERE key = 'integrity_manifest_version'
                        """,
                        (str(_INTEGRITY_MANIFEST_VERSION),),
                    )
                    self._validate_critical_meta_sync(connection)
                    self._refresh_all_manifests_sync(connection)
                elif manifest_version != _INTEGRITY_MANIFEST_VERSION:
                    raise JournalCorruptionError(
                        "不支持的 Integrity Manifest Version："
                        f"{version_row['value']}"
                    )
                else:
                    self._verify_all_manifests_sync(connection)
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _manifest_tenant_ids_sync(connection: sqlite3.Connection) -> list[str]:
        return [
            str(row["tenant_id"])
            for row in connection.execute(
                """
                SELECT tenant_id FROM session_events
                UNION
                SELECT tenant_id FROM session_snapshots
                UNION
                SELECT tenant_id FROM session_integrity_manifests
                ORDER BY tenant_id
                """
            ).fetchall()
        ]

    @staticmethod
    def _protected_rows_sync(
        connection: sqlite3.Connection,
        tenant_id: str,
    ) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
        event_rows = connection.execute(
            "SELECT * FROM session_events WHERE tenant_id = ? ORDER BY sequence",
            (tenant_id,),
        ).fetchall()
        snapshot_rows = connection.execute(
            """
            SELECT * FROM session_snapshots
            WHERE tenant_id = ? ORDER BY session_id, projection_name
            """,
            (tenant_id,),
        ).fetchall()
        return event_rows, snapshot_rows

    @staticmethod
    def _critical_meta_rows_sync(
        connection: sqlite3.Connection,
    ) -> list[sqlite3.Row]:
        # 覆盖整个 Meta 表而不只是当前三个 Key，使以后新增的安全关键游标
        # 默认也进入签名域，不会因忘记更新白名单而裸奔。
        return connection.execute(
            "SELECT key, value FROM session_journal_meta ORDER BY key"
        ).fetchall()

    @staticmethod
    def _validate_critical_meta_sync(connection: sqlite3.Connection) -> int:
        meta = {
            str(row["key"]): str(row["value"])
            for row in SQLiteSessionEventJournal._critical_meta_rows_sync(connection)
        }
        required = {
            "database_schema_version",
            "integrity_manifest_version",
            "next_sequence",
        }
        missing = sorted(required.difference(meta))
        if missing:
            raise JournalCorruptionError(
                "Session Journal 缺少关键 Meta：" + ", ".join(missing)
            )
        try:
            database_schema_version = int(meta["database_schema_version"])
            manifest_version = int(meta["integrity_manifest_version"])
            next_sequence = int(meta["next_sequence"])
        except ValueError as error:
            raise JournalCorruptionError("Session Journal 关键 Meta 不是合法整数") from error
        if database_schema_version != _DATABASE_SCHEMA_VERSION:
            raise JournalCorruptionError("Session Journal Database Schema Version 无效")
        if manifest_version != _INTEGRITY_MANIFEST_VERSION:
            raise JournalCorruptionError("Session Journal Integrity Manifest 版本无效")
        max_sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence), -1) AS value FROM session_events"
            ).fetchone()["value"]
        )
        if next_sequence <= max_sequence:
            raise JournalCorruptionError(
                "Session Journal next_sequence 已回退或与现存 Event 冲突："
                f"next={next_sequence}, max={max_sequence}"
            )
        if next_sequence < 0:
            raise JournalCorruptionError("Session Journal next_sequence 不能为负数")
        return next_sequence

    def _refresh_all_manifests_sync(self, connection: sqlite3.Connection) -> None:
        for tenant_id in self._manifest_tenant_ids_sync(connection):
            self._refresh_manifest_sync(connection, tenant_id)

    def _verify_all_manifests_sync(self, connection: sqlite3.Connection) -> None:
        self._validate_critical_meta_sync(connection)
        for tenant_id in self._manifest_tenant_ids_sync(connection):
            self._verify_manifest_sync(connection, tenant_id)

    def _verify_legacy_manifest_sync(
        self,
        connection: sqlite3.Connection,
        tenant_id: str,
    ) -> None:
        """升级前验证 v1 Manifest；只允许受控迁移路径调用。"""

        event_rows, snapshot_rows = self._protected_rows_sync(connection, tenant_id)
        manifest = connection.execute(
            "SELECT * FROM session_integrity_manifests WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()
        if manifest is None or int(manifest["manifest_version"]) != 1:
            raise JournalCorruptionError("Legacy Tenant Integrity Manifest 缺失或无效")
        key = self.key_provider.get_key(str(manifest["key_id"]))
        digest, event_count, snapshot_count = self._manifest_digest_from_rows(
            tenant_id,
            key,
            event_rows,
            snapshot_rows,
            meta_rows=[],
            manifest_version=1,
        )
        if (
            int(manifest["event_count"]) != event_count
            or int(manifest["snapshot_count"]) != snapshot_count
            or not hmac.compare_digest(str(manifest["manifest_hmac"]), digest)
        ):
            raise JournalCorruptionError(
                f"Tenant {tenant_id} Legacy Integrity Manifest HMAC 校验失败"
            )

    def _refresh_manifest_sync(
        self,
        connection: sqlite3.Connection,
        tenant_id: str,
    ) -> None:
        key = self.key_provider.active_key()
        digest, event_count, snapshot_count = self._manifest_digest_sync(
            connection,
            tenant_id,
            key,
        )
        connection.execute(
            """
            INSERT INTO session_integrity_manifests(
                tenant_id, manifest_version, key_id, event_count,
                snapshot_count, manifest_hmac, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id) DO UPDATE SET
                manifest_version = excluded.manifest_version,
                key_id = excluded.key_id,
                event_count = excluded.event_count,
                snapshot_count = excluded.snapshot_count,
                manifest_hmac = excluded.manifest_hmac,
                updated_at = excluded.updated_at
            """,
            (
                tenant_id,
                _INTEGRITY_MANIFEST_VERSION,
                key.key_id,
                event_count,
                snapshot_count,
                digest,
                int(time.time() * 1000),
            ),
        )

    def _verify_manifest_sync(
        self,
        connection: sqlite3.Connection,
        tenant_id: str,
    ) -> None:
        self._validate_critical_meta_sync(connection)
        version_row = connection.execute(
            """
            SELECT value FROM session_journal_meta
            WHERE key = 'integrity_manifest_version'
            """
        ).fetchone()
        if version_row is None:
            raise JournalCorruptionError("Session Journal 缺少 Integrity Manifest 标记")
        if int(version_row["value"]) != _INTEGRITY_MANIFEST_VERSION:
            raise JournalCorruptionError("Session Journal Integrity Manifest 版本无效")
        event_rows, snapshot_rows = self._protected_rows_sync(connection, tenant_id)
        manifest = connection.execute(
            """
            SELECT * FROM session_integrity_manifests WHERE tenant_id = ?
            """,
            (tenant_id,),
        ).fetchone()
        if manifest is None:
            if not event_rows and not snapshot_rows:
                return
            raise JournalCorruptionError(
                f"Tenant {tenant_id} 缺少 Integrity Manifest，禁止静默重建"
            )
        if int(manifest["manifest_version"]) != _INTEGRITY_MANIFEST_VERSION:
            raise JournalCorruptionError("Tenant Integrity Manifest 版本无效")
        key = self.key_provider.get_key(str(manifest["key_id"]))
        digest, event_count, snapshot_count = self._manifest_digest_from_rows(
            tenant_id,
            key,
            event_rows,
            snapshot_rows,
            meta_rows=self._critical_meta_rows_sync(connection),
            manifest_version=_INTEGRITY_MANIFEST_VERSION,
        )
        if (
            int(manifest["event_count"]) != event_count
            or int(manifest["snapshot_count"]) != snapshot_count
            or not hmac.compare_digest(str(manifest["manifest_hmac"]), digest)
        ):
            raise JournalCorruptionError(
                f"Tenant {tenant_id} Integrity Manifest HMAC 校验失败"
            )

    def _manifest_digest_sync(
        self,
        connection: sqlite3.Connection,
        tenant_id: str,
        key: JournalEncryptionKey,
    ) -> tuple[str, int, int]:
        event_rows, snapshot_rows = self._protected_rows_sync(connection, tenant_id)
        return self._manifest_digest_from_rows(
            tenant_id,
            key,
            event_rows,
            snapshot_rows,
            meta_rows=self._critical_meta_rows_sync(connection),
            manifest_version=_INTEGRITY_MANIFEST_VERSION,
        )

    @staticmethod
    def _manifest_digest_from_rows(
        tenant_id: str,
        key: JournalEncryptionKey,
        event_rows: list[sqlite3.Row],
        snapshot_rows: list[sqlite3.Row],
        *,
        meta_rows: list[sqlite3.Row],
        manifest_version: int,
    ) -> tuple[str, int, int]:
        manifest_key = hmac.new(
            key.key,
            (
                "pi-agent-loop/session-integrity-manifest-key/"
                f"v{manifest_version}"
            ).encode("ascii"),
            hashlib.sha256,
        ).digest()
        digest = hmac.new(manifest_key, digestmod=hashlib.sha256)
        _update_manifest_digest(
            digest,
            {
                "recordType": "manifest_header",
                "tenantId": tenant_id,
                "version": manifest_version,
            },
        )
        for row in meta_rows:
            _update_manifest_digest(
                digest,
                {
                    "recordType": "session_journal_meta",
                    "key": str(row["key"]),
                    "value": str(row["value"]),
                },
            )
        for row in event_rows:
            _update_manifest_digest(digest, _manifest_event_record(row))
        for row in snapshot_rows:
            _update_manifest_digest(digest, _manifest_snapshot_record(row))
        _update_manifest_digest(
            digest,
            {
                "recordType": "manifest_footer",
                "eventCount": len(event_rows),
                "snapshotCount": len(snapshot_rows),
            },
        )
        return digest.hexdigest(), len(event_rows), len(snapshot_rows)

    def _append_events_sync(
        self,
        principal: JournalPrincipal,
        specs: list[SessionEventSpec],
        expected_last_sequence: int | None,
        expected_stream_sequences: dict[SessionStreamKey, int] | None,
        deadline_ms: int | None,
    ) -> list[SessionEvent]:
        return self._append_events_transaction_sync(
            principal,
            specs,
            expected_last_sequence,
            expected_stream_sequences,
            deadline_ms,
            lease=None,
        )

    def _append_events_if_fenced_claim_sync(
        self,
        principal: JournalPrincipal,
        specs: list[SessionEventSpec],
        lease: ClaimLease,
        renew_lease_seconds: float,
        expected_last_sequence: int | None,
        expected_stream_sequences: dict[SessionStreamKey, int] | None,
        deadline_ms: int | None,
    ) -> list[SessionEvent]:
        return self._append_events_transaction_sync(
            principal,
            specs,
            expected_last_sequence,
            expected_stream_sequences,
            deadline_ms,
            lease=lease,
            renew_lease_seconds=renew_lease_seconds,
        )

    def _append_events_transaction_sync(
        self,
        principal: JournalPrincipal,
        specs: list[SessionEventSpec],
        expected_last_sequence: int | None,
        expected_stream_sequences: dict[SessionStreamKey, int] | None,
        deadline_ms: int | None,
        *,
        lease: ClaimLease | None,
        renew_lease_seconds: float | None = None,
    ) -> list[SessionEvent]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if lease is not None:
                now = int(time.time() * 1000)
                owned = connection.execute(
                    """
                    SELECT 1 FROM session_claims
                    WHERE tenant_id = ? AND claim_type = ? AND resource_id = ?
                      AND owner_token = ? AND generation = ?
                      AND lease_expires_at > ?
                    """,
                    (
                        principal.tenant_id,
                        lease.claim_type,
                        lease.resource_id,
                        lease.owner_token,
                        lease.generation,
                        now,
                    ),
                ).fetchone()
                if owned is None:
                    raise JournalFencedClaimLostError(
                        "Fenced Claim 已失效，禁止追加 Session Event"
                    )
            # BEGIN IMMEDIATE prevents a successor from taking over between
            # this lease check and the event insert. Integrity verification can
            # be comparatively expensive, so it must happen after ownership is
            # fenced by the write transaction rather than before the check.
            self._verify_all_manifests_sync(connection)
            if deadline_ms is not None and int(time.time() * 1000) >= deadline_ms:
                raise JournalDeadlineExceeded(
                    f"Session Journal 事务截止时间已到：{deadline_ms}"
                )
            if (
                expected_last_sequence is not None
                and expected_stream_sequences is not None
            ):
                raise ValueError(
                    "expected_last_sequence 与 expected_stream_sequences "
                    "不能同时使用"
                )
            if expected_last_sequence is not None:
                first = specs[0]
                if any(
                    spec.journal_kind != first.journal_kind
                    or spec.session_id != first.session_id
                    or spec.operation_id != first.operation_id
                    for spec in specs
                ):
                    raise ValueError("带 CAS 的 Event Batch 必须属于同一 Stream")
                current = self._last_stream_sequence(
                    connection,
                    principal.tenant_id,
                    first.journal_kind,
                    first.session_id,
                    first.operation_id,
                )
                if current != expected_last_sequence:
                    raise JournalConflictError(
                        "Session Journal Version 冲突："
                        f"expected={expected_last_sequence}, actual={current}"
                    )
            if expected_stream_sequences is not None:
                batch_streams: set[SessionStreamKey] = {
                    (
                        spec.journal_kind,
                        spec.session_id,
                        spec.operation_id,
                    )
                    for spec in specs
                }
                if batch_streams != set(expected_stream_sequences):
                    raise ValueError(
                        "expected_stream_sequences 必须精确覆盖 Event Batch "
                        f"的全部 Stream：expected={set(expected_stream_sequences)!r}, "
                        f"actual={batch_streams!r}"
                    )
                for stream, expected in expected_stream_sequences.items():
                    journal_kind, session_id, operation_id = stream
                    current = self._last_stream_sequence(
                        connection,
                        principal.tenant_id,
                        journal_kind,
                        session_id,
                        operation_id,
                    )
                    if current != expected:
                        raise JournalConflictError(
                            "Session Journal Multi-Stream Version 冲突："
                            f"stream={stream!r}, expected={expected}, actual={current}"
                        )
            appended = self._insert_specs_sync(connection, principal, specs)
            if lease is not None:
                assert renew_lease_seconds is not None
                renewed_until = int(time.time() * 1000) + int(
                    renew_lease_seconds * 1000
                )
                cursor = connection.execute(
                    """
                    UPDATE session_claims SET lease_expires_at = ?
                    WHERE tenant_id = ? AND claim_type = ? AND resource_id = ?
                      AND owner_token = ? AND generation = ?
                    """,
                    (
                        renewed_until,
                        principal.tenant_id,
                        lease.claim_type,
                        lease.resource_id,
                        lease.owner_token,
                        lease.generation,
                    ),
                )
                if cursor.rowcount != 1:
                    raise JournalFencedClaimLostError(
                        "Fenced Claim 在提交事件时已失效"
                    )
            connection.execute("COMMIT")
            return appended
        except (JournalConflictError, JournalDeadlineExceeded):
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        except sqlite3.IntegrityError as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise JournalConflictError(
                f"Session Journal 唯一约束冲突：{error}"
            ) from error
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _insert_specs_sync(
        self,
        connection: sqlite3.Connection,
        principal: JournalPrincipal,
        specs: list[SessionEventSpec],
    ) -> list[SessionEvent]:
        # 不能只依赖 INTEGER PRIMARY KEY 拒绝碰撞：合法删除会留下 gap，若
        # allocator 被回退到 gap，插入仍会成功并让 after_sequence 游标漏事件。
        next_sequence = self._validate_critical_meta_sync(connection)
        connection.execute(
            "UPDATE session_journal_meta SET value = ? WHERE key = 'next_sequence'",
            (str(next_sequence + len(specs)),),
        )
        appended: list[SessionEvent] = []
        for spec in specs:
            if spec.schema_version > self.current_event_schema_version:
                raise ValueError(
                    "不能写入高于 Runtime 支持范围的 Event Schema Version"
                )
            if spec.journal_kind == "runtime":
                if spec.source_sequence is None:
                    raise ValueError("Runtime Journal Event 必须包含 Source Sequence")
                row = connection.execute(
                    """
                    SELECT COALESCE(MAX(source_sequence), -1) AS value
                    FROM session_events
                    WHERE tenant_id = ? AND session_id = ?
                      AND journal_kind = 'runtime'
                    """,
                    (principal.tenant_id, spec.session_id),
                ).fetchone()
                if spec.source_sequence <= int(row["value"]):
                    raise JournalConflictError("Runtime Source Sequence 必须单调递增")
            sequence = next_sequence
            next_sequence += 1
            timestamp = (
                spec.timestamp
                if spec.timestamp is not None
                else int(time.time() * 1000)
            )
            event_id = spec.event_id or str(uuid4())
            retention = (
                spec.retention_seconds
                if spec.retention_seconds is not None
                else (
                    self.redaction_policy.audit_retention_seconds
                    if spec.journal_kind == "audit"
                    else self.redaction_policy.default_retention_seconds
                )
            )
            expires_at = timestamp + retention * 1000 if retention else None
            # 必须加密保存可恢复原文。若在这里不可逆脱敏 password/token 等业务
            # 参数，Action Hash 与恢复输入会不一致；字段脱敏属于授权读取视图。
            payload = copy.deepcopy(spec.payload)
            _canonical_json_bytes(payload)
            key = self.key_provider.active_key()
            metadata = _event_metadata(
                event_id=event_id,
                tenant_id=principal.tenant_id,
                session_id=spec.session_id,
                operation_id=spec.operation_id,
                run_id=spec.run_id,
                journal_kind=spec.journal_kind,
                event_type=spec.event_type,
                sequence=sequence,
                source_sequence=spec.source_sequence,
                schema_version=spec.schema_version,
                state_version=spec.state_version,
                timestamp=timestamp,
                actor_id=principal.principal_id,
                expires_at=expires_at,
                key_id=key.key_id,
            )
            nonce, ciphertext, checksum = _encrypt_value(key, payload, metadata)
            connection.execute(
                """
                INSERT INTO session_events(
                    sequence, event_id, tenant_id, session_id, operation_id,
                    run_id, journal_kind, event_type, source_sequence,
                    schema_version, state_version, timestamp, actor_id, key_id,
                    nonce, payload_ciphertext, checksum, expires_at,
                    approval_id, write_id, idempotency_key_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sequence,
                    event_id,
                    principal.tenant_id,
                    spec.session_id,
                    spec.operation_id,
                    spec.run_id,
                    spec.journal_kind,
                    spec.event_type,
                    spec.source_sequence,
                    spec.schema_version,
                    spec.state_version,
                    timestamp,
                    principal.principal_id,
                    key.key_id,
                    nonce,
                    ciphertext,
                    checksum,
                    expires_at,
                    _optional_text(payload.get("approvalId")),
                    _optional_text(payload.get("writeId")),
                    _optional_text(payload.get("idempotencyKeyHash")),
                ),
            )
            appended.append(
                SessionEvent(
                    event_id,
                    principal.tenant_id,
                    spec.session_id,
                    spec.operation_id,
                    spec.run_id,
                    spec.journal_kind,
                    spec.event_type,
                    sequence,
                    spec.source_sequence,
                    spec.schema_version,
                    spec.state_version,
                    payload,
                    timestamp,
                    principal.principal_id,
                    expires_at,
                )
            )
        # next_sequence 是全局 High-water Mark；它进入每个 Tenant Manifest，
        # 因此更新 allocator 后必须在同一事务刷新全部 Manifest。
        self._refresh_all_manifests_sync(connection)
        return appended

    def _load_events_sync(
        self,
        principal: JournalPrincipal,
        session_id: str | None,
        operation_id: str | None,
        run_id: str | None,
        journal_kind: JournalKind | None,
        after_sequence: int | None,
        include_expired: bool,
        migration_registry: EventMigrationRegistry | None,
        target_schema_version: int | None,
    ) -> list[SessionEvent]:
        with closing(self._connect()) as connection:
            rows = self._select_event_rows(
                connection,
                principal.tenant_id,
                session_id=session_id,
                operation_id=operation_id,
                run_id=run_id,
                journal_kind=journal_kind,
                after_sequence=after_sequence,
                include_expired=include_expired,
            )
        redact_view = not principal.roles.isdisjoint(
            self.redaction_policy.redacted_read_roles
        )
        decoded = [
            self._decode_event_row(
                row,
                migration_registry,
                target_schema_version,
                redact_before_migration=redact_view,
            )
            for row in rows
        ]
        if redact_view:
            return [
                replace(
                    event,
                    payload=self.redaction_policy.redact(event.payload),
                )
                for event in decoded
            ]
        return decoded

    def _select_event_rows(
        self,
        connection: sqlite3.Connection,
        tenant_id: str,
        *,
        session_id: str | None = None,
        operation_id: str | None = None,
        run_id: str | None = None,
        journal_kind: JournalKind | None = None,
        after_sequence: int | None = None,
        include_expired: bool = False,
    ) -> list[sqlite3.Row]:
        conditions = ["tenant_id = ?"]
        values: list[Any] = [tenant_id]
        for column, value in (
            ("session_id", session_id),
            ("operation_id", operation_id),
            ("run_id", run_id),
            ("journal_kind", journal_kind),
        ):
            if value is not None:
                conditions.append(f"{column} = ?")
                values.append(value)
        if after_sequence is not None:
            conditions.append("sequence > ?")
            values.append(after_sequence)
        if not include_expired:
            # Business Event 的 expires_at 是“可进入原子 Purge”的时间，不是
            # 单行可见性开关。若先隐藏过期前缀、却保留活跃后缀，Operation
            # Replay 在真正 Purge 前就已经损坏。Audit 仍可按读取时效隐藏。
            conditions.append(
                "(journal_kind != 'audit' OR expires_at IS NULL OR expires_at > ?)"
            )
            values.append(int(time.time() * 1000))
        return connection.execute(
            "SELECT * FROM session_events WHERE "
            + " AND ".join(conditions)
            + " ORDER BY sequence",
            values,
        ).fetchall()

    def _decode_event_row(
        self,
        row: sqlite3.Row,
        migration_registry: EventMigrationRegistry | None = None,
        target_schema_version: int | None = None,
        *,
        redact_before_migration: bool = False,
    ) -> SessionEvent:
        metadata = _event_metadata_from_row(row)
        key = self.key_provider.get_key(str(row["key_id"]))
        payload = _decrypt_value(
            key,
            bytes(row["nonce"]),
            bytes(row["payload_ciphertext"]),
            str(row["checksum"]),
            metadata,
        )
        if not isinstance(payload, dict):
            raise JournalCorruptionError("Session Event Payload 必须是对象")
        if redact_before_migration:
            # A read-only caller may supply a migration registry.  Redact before
            # invoking that extension callback so it cannot observe plaintext.
            payload = self.redaction_policy.redact(payload)
        version = int(row["schema_version"])
        if version > self.current_event_schema_version:
            raise JournalMigrationError("持久 Event 版本高于当前 Runtime 支持版本")
        if target_schema_version is not None and target_schema_version < version:
            raise JournalMigrationError("Event Migration 目标版本不能低于持久版本")
        if target_schema_version is not None and version < target_schema_version:
            if migration_registry is None:
                raise JournalMigrationError("旧 Event 缺少 Migration Registry")
            payload, version = migration_registry.migrate(
                str(row["journal_kind"]),
                str(row["event_type"]),
                payload,
                version,
                target_schema_version,
            )
        return SessionEvent(
            event_id=str(row["event_id"]),
            tenant_id=str(row["tenant_id"]),
            session_id=str(row["session_id"]),
            operation_id=_optional_text(row["operation_id"]),
            run_id=_optional_text(row["run_id"]),
            journal_kind=str(row["journal_kind"]),  # type: ignore[arg-type]
            event_type=str(row["event_type"]),
            sequence=int(row["sequence"]),
            source_sequence=(
                int(row["source_sequence"])
                if row["source_sequence"] is not None
                else None
            ),
            schema_version=version,
            state_version=int(row["state_version"]),
            payload=payload,
            timestamp=int(row["timestamp"]),
            actor_id=str(row["actor_id"]),
            expires_at=(
                int(row["expires_at"]) if row["expires_at"] is not None else None
            ),
        )

    def _export_session_sync(
        self,
        principal: JournalPrincipal,
        session_id: str,
    ) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._verify_all_manifests_sync(connection)
            rows = self._select_event_rows(
                connection,
                principal.tenant_id,
                session_id=session_id,
                include_expired=True,
            )
            result = [self._decode_event_row(row).to_dict() for row in rows]
            self._append_audit_sync(
                connection,
                principal,
                "journal_session_exported",
                {"targetSessionId": session_id, "eventCount": len(result)},
            )
            connection.execute("COMMIT")
            return result
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _delete_session_sync(
        self,
        principal: JournalPrincipal,
        session_id: str,
    ) -> int:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._verify_all_manifests_sync(connection)
            cursor = connection.execute(
                "DELETE FROM session_events WHERE tenant_id = ? AND session_id = ?",
                (principal.tenant_id, session_id),
            )
            connection.execute(
                "DELETE FROM session_snapshots WHERE tenant_id = ? AND session_id = ?",
                (principal.tenant_id, session_id),
            )
            deleted = int(cursor.rowcount)
            self._append_audit_sync(
                connection,
                principal,
                "journal_session_deleted",
                {"targetSessionId": session_id, "deletedEventCount": deleted},
            )
            connection.execute("COMMIT")
            return deleted
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _purge_expired_sync(
        self,
        principal: JournalPrincipal,
        now_ms: int,
    ) -> int:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._verify_all_manifests_sync(connection)
            rows = connection.execute(
                """
                SELECT * FROM session_events
                WHERE tenant_id = ? AND journal_kind != 'audit'
                ORDER BY sequence
                """,
                (principal.tenant_id,),
            ).fetchall()
            operation_groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
            session_groups: dict[str, list[sqlite3.Row]] = {}
            for row in rows:
                session_id = str(row["session_id"])
                session_groups.setdefault(session_id, []).append(row)
                if (
                    str(row["journal_kind"]) == "operation"
                    and row["operation_id"] is not None
                ):
                    key = (session_id, str(row["operation_id"]))
                    operation_groups.setdefault(key, []).append(row)

            safe_operations: set[tuple[str, str]] = set()
            for key, group in operation_groups.items():
                if not all(_row_is_expired(row, now_ms) for row in group):
                    continue
                if self._expired_operation_is_terminal(group):
                    safe_operations.add(key)

            # Runtime/Retry 等没有独立 Operation 边界的流按整个 Session 判断；
            # 只要同 Session 仍有未过期或无法验证终态的 Operation，就保留全部
            # 无 Operation 边界事件，避免删掉可恢复状态机的前缀。
            safe_unscoped_sessions: set[str] = set()
            for session_id, group in session_groups.items():
                unscoped = [
                    row
                    for row in group
                    if str(row["journal_kind"]) != "operation"
                    or row["operation_id"] is None
                ]
                if not unscoped or not all(
                    _row_is_expired(row, now_ms) for row in group
                ):
                    continue
                operation_keys = {
                    (session_id, str(row["operation_id"]))
                    for row in group
                    if str(row["journal_kind"]) == "operation"
                    and row["operation_id"] is not None
                }
                if operation_keys.issubset(safe_operations):
                    safe_unscoped_sessions.add(session_id)

            deleted_events = 0
            for session_id, operation_id in sorted(safe_operations):
                cursor = connection.execute(
                    """
                    DELETE FROM session_events
                    WHERE tenant_id = ? AND session_id = ?
                      AND journal_kind = 'operation' AND operation_id = ?
                    """,
                    (principal.tenant_id, session_id, operation_id),
                )
                deleted_events += int(cursor.rowcount)
            for session_id in sorted(safe_unscoped_sessions):
                cursor = connection.execute(
                    """
                    DELETE FROM session_events
                    WHERE tenant_id = ? AND session_id = ?
                      AND journal_kind != 'audit'
                      AND (journal_kind != 'operation' OR operation_id IS NULL)
                    """,
                    (principal.tenant_id, session_id),
                )
                deleted_events += int(cursor.rowcount)
            snapshot_cursor = connection.execute(
                """
                DELETE FROM session_snapshots
                WHERE tenant_id = ? AND expires_at IS NOT NULL AND expires_at <= ?
                """,
                (principal.tenant_id, now_ms),
            )
            deleted = deleted_events + int(snapshot_cursor.rowcount)
            self._append_audit_sync(
                connection,
                principal,
                "journal_retention_purged",
                {"deletedRecordCount": deleted, "cutoffTimestamp": now_ms},
            )
            connection.execute("COMMIT")
            return deleted
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _expired_operation_is_terminal(
        self,
        rows: list[sqlite3.Row],
    ) -> bool:
        from .operation_events import OperationEvent
        from .operation_state import replay_operation

        try:
            events = [
                OperationEvent(
                    type=event.event_type,
                    session_id=event.session_id,
                    operation_id=event.operation_id or "",
                    sequence=event.sequence,
                    timestamp=event.timestamp,
                    data=event.payload,
                )
                for event in (self._decode_event_row(row) for row in rows)
            ]
            return replay_operation(events).phase in {
                "completed",
                "failed",
                "cancelled",
            }
        except JournalCorruptionError:
            raise
        except Exception:
            # Retention 必须 fail-safe：无法重放证明终态就保留，而不是猜测可删。
            return False

    def _load_audit_events_sync(
        self,
        principal: JournalPrincipal,
    ) -> list[SessionEvent]:
        with closing(self._connect()) as connection:
            rows = self._select_event_rows(
                connection,
                principal.tenant_id,
                journal_kind="audit",
                include_expired=False,
            )
        return [self._decode_event_row(row) for row in rows]

    def _verify_integrity_sync(
        self,
        principal: JournalPrincipal,
    ) -> tuple[int, int]:
        connection = self._connect()
        try:
            # 同一个 SQLite Read Transaction 中先校验逐行 AEAD/Checksum，再校验
            # Tenant Manifest，避免并发合法写造成两次读取落在不同数据库快照。
            connection.execute("BEGIN")
            event_rows = self._select_event_rows(
                connection,
                principal.tenant_id,
                include_expired=True,
            )
            snapshot_rows = connection.execute(
                "SELECT * FROM session_snapshots WHERE tenant_id = ?",
                (principal.tenant_id,),
            ).fetchall()
            for row in event_rows:
                self._decode_event_row(row)
            for row in snapshot_rows:
                self._decode_snapshot_row(row)
            self._verify_all_manifests_sync(connection)
            connection.execute("COMMIT")
            return len(event_rows), len(snapshot_rows)
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _rotate_keys_sync(
        self,
        principal: JournalPrincipal,
    ) -> tuple[int, int]:
        active = self.key_provider.active_key()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._verify_all_manifests_sync(connection)
            event_rows = connection.execute(
                "SELECT * FROM session_events WHERE tenant_id = ?",
                (principal.tenant_id,),
            ).fetchall()
            event_count = 0
            for row in event_rows:
                if str(row["key_id"]) == active.key_id:
                    self._decode_event_row(row)
                    continue
                event = self._decode_event_row(row)
                metadata = _event_metadata_from_row(row, key_id=active.key_id)
                nonce, ciphertext, checksum = _encrypt_value(
                    active, event.payload, metadata
                )
                connection.execute(
                    """
                    UPDATE session_events
                    SET key_id = ?, nonce = ?, payload_ciphertext = ?, checksum = ?
                    WHERE tenant_id = ? AND sequence = ?
                    """,
                    (
                        active.key_id,
                        nonce,
                        ciphertext,
                        checksum,
                        principal.tenant_id,
                        event.sequence,
                    ),
                )
                event_count += 1
            snapshot_rows = connection.execute(
                "SELECT * FROM session_snapshots WHERE tenant_id = ?",
                (principal.tenant_id,),
            ).fetchall()
            snapshot_count = 0
            for row in snapshot_rows:
                if str(row["key_id"]) == active.key_id:
                    self._decode_snapshot_row(row)
                    continue
                snapshot = self._decode_snapshot_row(row)
                metadata = _snapshot_metadata_from_row(row, key_id=active.key_id)
                nonce, ciphertext, checksum = _encrypt_value(
                    active, snapshot.state, metadata
                )
                connection.execute(
                    """
                    UPDATE session_snapshots
                    SET key_id = ?, nonce = ?, state_ciphertext = ?, checksum = ?
                    WHERE tenant_id = ? AND session_id = ? AND projection_name = ?
                    """,
                    (
                        active.key_id,
                        nonce,
                        ciphertext,
                        checksum,
                        principal.tenant_id,
                        snapshot.session_id,
                        snapshot.projection_name,
                    ),
                )
                snapshot_count += 1
            self._append_audit_sync(
                connection,
                principal,
                "journal_keys_rotated",
                {
                    "keyId": active.key_id,
                    "eventCount": event_count,
                    "snapshotCount": snapshot_count,
                },
            )
            connection.execute("COMMIT")
            return event_count, snapshot_count
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _run_event_migrations_sync(
        self,
        principal: JournalPrincipal,
        registry: EventMigrationRegistry,
        target_version: int,
        session_id: str | None,
    ) -> list[SessionEvent]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._verify_all_manifests_sync(connection)
            rows = self._select_event_rows(
                connection,
                principal.tenant_id,
                session_id=session_id,
                include_expired=True,
            )
            migrated = [
                self._decode_event_row(row, registry, target_version) for row in rows
            ]
            self._append_audit_sync(
                connection,
                principal,
                "journal_event_migrations_validated",
                {
                    "targetSchemaVersion": target_version,
                    "targetSessionId": session_id,
                    "eventCount": len(migrated),
                },
            )
            connection.execute("COMMIT")
            return migrated
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _save_snapshot_sync(
        self,
        principal: JournalPrincipal,
        session_id: str,
        projection_name: str,
        last_sequence: int,
        state: Any,
        state_version: int,
        event_schema_version: int,
        retention_seconds: int | None,
        allow_same_sequence_for_migration: bool = False,
    ) -> SessionSnapshot:
        if not session_id or not projection_name:
            raise ValueError("Snapshot 必须包含 session_id/projection_name")
        if isinstance(last_sequence, bool) or not isinstance(last_sequence, int) or last_sequence < -1:
            raise ValueError("Snapshot last_sequence 必须大于等于 -1")
        _validate_version(state_version, "Snapshot State Version")
        _validate_version(event_schema_version, "Snapshot Event Schema Version")
        if retention_seconds is not None and (
            isinstance(retention_seconds, bool)
            or not isinstance(retention_seconds, int)
            or retention_seconds <= 0
        ):
            raise ValueError("Snapshot Retention Seconds 必须为正整数")
        # Snapshot 是恢复事实，必须像 Event 一样加密原文；脱敏只发生在授权读取视图。
        stored_state = copy.deepcopy(state)
        _canonical_json_bytes(stored_state)
        timestamp = int(time.time() * 1000)
        retention = (
            retention_seconds
            if retention_seconds is not None
            else self.redaction_policy.default_retention_seconds
        )
        expires_at = timestamp + retention * 1000 if retention else None
        key = self.key_provider.active_key()
        metadata = _snapshot_metadata(
            tenant_id=principal.tenant_id,
            session_id=session_id,
            projection_name=projection_name,
            last_sequence=last_sequence,
            state_version=state_version,
            event_schema_version=event_schema_version,
            timestamp=timestamp,
            expires_at=expires_at,
            key_id=key.key_id,
        )
        nonce, ciphertext, checksum = _encrypt_value(key, stored_state, metadata)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._verify_all_manifests_sync(connection)
            head = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), -1) AS value
                    FROM session_events
                    WHERE tenant_id = ? AND session_id = ?
                    """,
                    (principal.tenant_id, session_id),
                ).fetchone()["value"]
            )
            if last_sequence > head:
                raise JournalConflictError(
                    "Snapshot last_sequence 超过当前 Tenant/Session Event Head："
                    f"snapshot={last_sequence}, head={head}"
                )
            existing = connection.execute(
                """
                SELECT last_sequence, state_version FROM session_snapshots
                WHERE tenant_id = ? AND session_id = ? AND projection_name = ?
                """,
                (principal.tenant_id, session_id, projection_name),
            ).fetchone()
            if existing is not None:
                existing_sequence = int(existing["last_sequence"])
                existing_state_version = int(existing["state_version"])
                if last_sequence < existing_sequence:
                    raise JournalConflictError("不能用旧 Snapshot 覆盖较新 Snapshot")
                if last_sequence == existing_sequence:
                    if not allow_same_sequence_for_migration:
                        raise JournalConflictError(
                            "同一 last_sequence 的 Snapshot 不允许覆盖"
                        )
                    if state_version <= existing_state_version:
                        raise JournalConflictError(
                            "Snapshot Migration 必须严格提升 state_version"
                        )
                elif state_version < existing_state_version:
                    raise JournalConflictError("Snapshot State Version 不允许回退")
            connection.execute(
                """
                INSERT INTO session_snapshots(
                    tenant_id, session_id, projection_name, last_sequence,
                    state_version, event_schema_version, timestamp, key_id,
                    nonce, state_ciphertext, checksum, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, session_id, projection_name) DO UPDATE SET
                    last_sequence = excluded.last_sequence,
                    state_version = excluded.state_version,
                    event_schema_version = excluded.event_schema_version,
                    timestamp = excluded.timestamp,
                    key_id = excluded.key_id,
                    nonce = excluded.nonce,
                    state_ciphertext = excluded.state_ciphertext,
                    checksum = excluded.checksum,
                    expires_at = excluded.expires_at
                """,
                (
                    principal.tenant_id,
                    session_id,
                    projection_name,
                    last_sequence,
                    state_version,
                    event_schema_version,
                    timestamp,
                    key.key_id,
                    nonce,
                    ciphertext,
                    checksum,
                    expires_at,
                ),
            )
            self._refresh_manifest_sync(connection, principal.tenant_id)
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        return SessionSnapshot(
            principal.tenant_id,
            session_id,
            projection_name,
            last_sequence,
            state_version,
            event_schema_version,
            stored_state,
            timestamp,
            expires_at,
        )

    def _load_snapshot_sync(
        self,
        principal: JournalPrincipal,
        session_id: str,
        projection_name: str,
        state_registry: StateMigrationRegistry | None,
        target_state_version: int | None,
        redact_view: bool = True,
    ) -> SessionSnapshot | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM session_snapshots
                WHERE tenant_id = ? AND session_id = ? AND projection_name = ?
                  AND (expires_at IS NULL OR expires_at > ?)
                """,
                (
                    principal.tenant_id,
                    session_id,
                    projection_name,
                    int(time.time() * 1000),
                ),
            ).fetchone()
        if row is None:
            return None
        snapshot = self._decode_snapshot_row(row)
        redact_view = redact_view and not principal.roles.isdisjoint(
            self.redaction_policy.redacted_read_roles
        )
        if redact_view:
            # 在调用外部提供的 State Migration 前先脱敏，避免 Reader/Auditor
            # 通过自定义 Migration Callback 观察加密原文。
            snapshot = replace(
                snapshot,
                state=self.redaction_policy.redact(snapshot.state),
            )
        if (
            target_state_version is not None
            and target_state_version < snapshot.state_version
        ):
            raise JournalMigrationError("State Migration 目标版本不能低于 Snapshot 版本")
        if target_state_version is not None and snapshot.state_version < target_state_version:
            if state_registry is None:
                raise JournalMigrationError("旧 Snapshot 缺少 State Migration Registry")
            state, version = state_registry.migrate(
                projection_name,
                snapshot.state,
                snapshot.state_version,
                target_state_version,
            )
            snapshot = replace(snapshot, state=state, state_version=version)
        return snapshot

    def _decode_snapshot_row(self, row: sqlite3.Row) -> SessionSnapshot:
        metadata = _snapshot_metadata_from_row(row)
        state = _decrypt_value(
            self.key_provider.get_key(str(row["key_id"])),
            bytes(row["nonce"]),
            bytes(row["state_ciphertext"]),
            str(row["checksum"]),
            metadata,
        )
        return SessionSnapshot(
            tenant_id=str(row["tenant_id"]),
            session_id=str(row["session_id"]),
            projection_name=str(row["projection_name"]),
            last_sequence=int(row["last_sequence"]),
            state_version=int(row["state_version"]),
            event_schema_version=int(row["event_schema_version"]),
            state=state,
            timestamp=int(row["timestamp"]),
            expires_at=(
                int(row["expires_at"]) if row["expires_at"] is not None else None
            ),
        )

    def _migrate_snapshot_sync(
        self,
        principal: JournalPrincipal,
        session_id: str,
        projection_name: str,
        registry: StateMigrationRegistry,
        target_state_version: int,
    ) -> SessionSnapshot:
        snapshot = self._load_snapshot_sync(
            principal,
            session_id,
            projection_name,
            None,
            None,
            False,
        )
        if snapshot is None:
            raise SessionJournalError("待迁移 Snapshot 不存在")
        if target_state_version <= snapshot.state_version:
            raise JournalMigrationError(
                "Snapshot Migration 必须严格提升 state_version"
            )
        state, migrated_version = registry.migrate(
            projection_name,
            snapshot.state,
            snapshot.state_version,
            target_state_version,
        )
        migrated = self._save_snapshot_sync(
            principal,
            session_id,
            projection_name,
            snapshot.last_sequence,
            state,
            migrated_version,
            snapshot.event_schema_version,
            None,
            True,
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._verify_all_manifests_sync(connection)
            self._append_audit_sync(
                connection,
                principal,
                "journal_snapshot_migrated",
                {
                    "targetSessionId": session_id,
                    "projectionName": projection_name,
                    "stateVersion": target_state_version,
                },
            )
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        return migrated

    def _acquire_fenced_claim_sync(
        self,
        principal: JournalPrincipal,
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
                FROM session_claims
                WHERE tenant_id = ? AND claim_type = ? AND resource_id = ?
                """,
                (principal.tenant_id, claim_type, resource_id),
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
                INSERT INTO session_claims(
                    tenant_id, claim_type, resource_id, owner_token,
                    lease_expires_at, generation
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, claim_type, resource_id) DO UPDATE SET
                    owner_token = excluded.owner_token,
                    lease_expires_at = excluded.lease_expires_at,
                    generation = excluded.generation
                """,
                (
                    principal.tenant_id,
                    claim_type,
                    resource_id,
                    owner_token,
                    expires,
                    generation,
                ),
            )
            connection.execute("COMMIT")
            return ClaimLease(claim_type, resource_id, owner_token, generation)
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _release_claim_sync(
        self,
        principal: JournalPrincipal,
        claim_type: str,
        resource_id: str,
        owner_token: str,
    ) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE session_claims
                SET lease_expires_at = 0, generation = generation + 1
                WHERE tenant_id = ? AND claim_type = ?
                  AND resource_id = ? AND owner_token = ?
                """,
                (principal.tenant_id, claim_type, resource_id, owner_token),
            )

    def _renew_fenced_claim_sync(
        self,
        principal: JournalPrincipal,
        lease: ClaimLease,
        lease_seconds: float,
    ) -> bool:
        now = int(time.time() * 1000)
        expires = now + int(lease_seconds * 1000)
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE session_claims SET lease_expires_at = ?
                WHERE tenant_id = ? AND claim_type = ? AND resource_id = ?
                  AND owner_token = ? AND generation = ?
                  AND lease_expires_at > ?
                """,
                (
                    expires,
                    principal.tenant_id,
                    lease.claim_type,
                    lease.resource_id,
                    lease.owner_token,
                    lease.generation,
                    now,
                ),
            )
            return cursor.rowcount == 1

    def _verify_fenced_claim_sync(
        self,
        principal: JournalPrincipal,
        lease: ClaimLease,
    ) -> bool:
        now = int(time.time() * 1000)
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT 1 FROM session_claims
                WHERE tenant_id = ? AND claim_type = ? AND resource_id = ?
                  AND owner_token = ? AND generation = ?
                  AND lease_expires_at > ?
                """,
                (
                    principal.tenant_id,
                    lease.claim_type,
                    lease.resource_id,
                    lease.owner_token,
                    lease.generation,
                    now,
                ),
            ).fetchone()
        return row is not None

    def _release_fenced_claim_sync(
        self,
        principal: JournalPrincipal,
        lease: ClaimLease,
    ) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE session_claims
                SET lease_expires_at = 0, generation = generation + 1
                WHERE tenant_id = ? AND claim_type = ? AND resource_id = ?
                  AND owner_token = ? AND generation = ?
                """,
                (
                    principal.tenant_id,
                    lease.claim_type,
                    lease.resource_id,
                    lease.owner_token,
                    lease.generation,
                ),
            )

    def _append_audit_sync(
        self,
        connection: sqlite3.Connection,
        principal: JournalPrincipal,
        event_type: str,
        payload: dict[str, Any],
    ) -> SessionEvent:
        return self._insert_specs_sync(
            connection,
            principal,
            [
                SessionEventSpec(
                    "audit",
                    event_type,
                    _AUDIT_SESSION_ID,
                    payload,
                    schema_version=self.current_event_schema_version,
                )
            ],
        )[0]

    @staticmethod
    def _last_stream_sequence(
        connection: sqlite3.Connection,
        tenant_id: str,
        journal_kind: JournalKind,
        session_id: str,
        operation_id: str | None,
    ) -> int:
        row = connection.execute(
            """
            SELECT COALESCE(MAX(sequence), -1) AS value
            FROM session_events
            WHERE tenant_id = ? AND journal_kind = ? AND session_id = ?
              AND operation_id IS ?
            """,
            (tenant_id, journal_kind, session_id, operation_id),
        ).fetchone()
        return int(row["value"])


def _validated_stream_heads(
    value: Mapping[SessionStreamKey, int] | None,
) -> dict[SessionStreamKey, int] | None:
    """Copy and validate a multi-stream CAS precondition for a worker thread."""

    if value is None:
        return None
    if not isinstance(value, Mapping) or not value:
        raise ValueError("expected_stream_sequences 必须是非空 Mapping")
    validated: dict[SessionStreamKey, int] = {}
    for raw_stream, expected in value.items():
        if not isinstance(raw_stream, tuple) or len(raw_stream) != 3:
            raise ValueError(
                "expected_stream_sequences Key 必须为 "
                "(journal_kind, session_id, operation_id)"
            )
        journal_kind, session_id, operation_id = raw_stream
        if journal_kind not in _JOURNAL_KINDS:
            raise ValueError(f"Journal Kind 无效：{journal_kind}")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("Stream session_id 不能为空")
        if operation_id is not None and (
            not isinstance(operation_id, str) or not operation_id
        ):
            raise ValueError("Stream operation_id 必须是非空字符串或 None")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < -1:
            raise ValueError("Stream Expected Sequence 必须是不小于 -1 的整数")
        stream: SessionStreamKey = (journal_kind, session_id, operation_id)
        validated[stream] = expected
    return validated


def _event_metadata(**values: Any) -> dict[str, Any]:
    return {"recordType": "session_event", **values}


def _event_metadata_from_row(
    row: sqlite3.Row,
    *,
    key_id: str | None = None,
) -> dict[str, Any]:
    return _event_metadata(
        event_id=str(row["event_id"]),
        tenant_id=str(row["tenant_id"]),
        session_id=str(row["session_id"]),
        operation_id=_optional_text(row["operation_id"]),
        run_id=_optional_text(row["run_id"]),
        journal_kind=str(row["journal_kind"]),
        event_type=str(row["event_type"]),
        sequence=int(row["sequence"]),
        source_sequence=(
            int(row["source_sequence"])
            if row["source_sequence"] is not None
            else None
        ),
        schema_version=int(row["schema_version"]),
        state_version=int(row["state_version"]),
        timestamp=int(row["timestamp"]),
        actor_id=str(row["actor_id"]),
        expires_at=(
            int(row["expires_at"]) if row["expires_at"] is not None else None
        ),
        key_id=key_id or str(row["key_id"]),
    )


def _snapshot_metadata(**values: Any) -> dict[str, Any]:
    return {"recordType": "session_snapshot", **values}


def _snapshot_metadata_from_row(
    row: sqlite3.Row,
    *,
    key_id: str | None = None,
) -> dict[str, Any]:
    return _snapshot_metadata(
        tenant_id=str(row["tenant_id"]),
        session_id=str(row["session_id"]),
        projection_name=str(row["projection_name"]),
        last_sequence=int(row["last_sequence"]),
        state_version=int(row["state_version"]),
        event_schema_version=int(row["event_schema_version"]),
        timestamp=int(row["timestamp"]),
        expires_at=(
            int(row["expires_at"]) if row["expires_at"] is not None else None
        ),
        key_id=key_id or str(row["key_id"]),
    )


def _update_manifest_digest(
    digest: hmac.HMAC,
    record: dict[str, Any],
) -> None:
    encoded = _canonical_json_bytes(record)
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def _row_is_expired(row: sqlite3.Row, now_ms: int) -> bool:
    expires_at = row["expires_at"]
    return expires_at is not None and int(expires_at) <= now_ms


def _manifest_event_record(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "recordType": "session_event",
        "sequence": int(row["sequence"]),
        "eventId": str(row["event_id"]),
        "tenantId": str(row["tenant_id"]),
        "sessionId": str(row["session_id"]),
        "operationId": _optional_text(row["operation_id"]),
        "runId": _optional_text(row["run_id"]),
        "journalKind": str(row["journal_kind"]),
        "eventType": str(row["event_type"]),
        "sourceSequence": (
            int(row["source_sequence"])
            if row["source_sequence"] is not None
            else None
        ),
        "schemaVersion": int(row["schema_version"]),
        "stateVersion": int(row["state_version"]),
        "timestamp": int(row["timestamp"]),
        "actorId": str(row["actor_id"]),
        "keyId": str(row["key_id"]),
        "nonce": base64.b64encode(bytes(row["nonce"])).decode("ascii"),
        "ciphertext": base64.b64encode(bytes(row["payload_ciphertext"])).decode(
            "ascii"
        ),
        "checksum": str(row["checksum"]),
        "expiresAt": (
            int(row["expires_at"]) if row["expires_at"] is not None else None
        ),
        "approvalId": _optional_text(row["approval_id"]),
        "writeId": _optional_text(row["write_id"]),
        "idempotencyKeyHash": _optional_text(row["idempotency_key_hash"]),
    }


def _manifest_snapshot_record(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "recordType": "session_snapshot",
        "tenantId": str(row["tenant_id"]),
        "sessionId": str(row["session_id"]),
        "projectionName": str(row["projection_name"]),
        "lastSequence": int(row["last_sequence"]),
        "stateVersion": int(row["state_version"]),
        "eventSchemaVersion": int(row["event_schema_version"]),
        "timestamp": int(row["timestamp"]),
        "keyId": str(row["key_id"]),
        "nonce": base64.b64encode(bytes(row["nonce"])).decode("ascii"),
        "ciphertext": base64.b64encode(bytes(row["state_ciphertext"])).decode(
            "ascii"
        ),
        "checksum": str(row["checksum"]),
        "expiresAt": (
            int(row["expires_at"]) if row["expires_at"] is not None else None
        ),
    }


def _encrypt_value(
    key: JournalEncryptionKey,
    value: Any,
    metadata: dict[str, Any],
) -> tuple[bytes, bytes, str]:
    nonce = os.urandom(12)
    aad = _canonical_json_bytes(metadata)
    plaintext = _canonical_json_bytes(value)
    ciphertext = AESGCM(key.key).encrypt(nonce, plaintext, aad)
    checksum = hashlib.sha256(aad + nonce + ciphertext).hexdigest()
    return nonce, ciphertext, checksum


def _decrypt_value(
    key: JournalEncryptionKey,
    nonce: bytes,
    ciphertext: bytes,
    checksum: str,
    metadata: dict[str, Any],
) -> Any:
    aad = _canonical_json_bytes(metadata)
    actual = hashlib.sha256(aad + nonce + ciphertext).hexdigest()
    if not hmac.compare_digest(actual, checksum):
        raise JournalCorruptionError("Session Journal Checksum 校验失败")
    try:
        plaintext = AESGCM(key.key).decrypt(nonce, ciphertext, aad)
    except InvalidTag as error:
        raise JournalCorruptionError("Session Journal AEAD Tag 校验失败") from error
    try:
        return json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise JournalCorruptionError("Session Journal 解密 Payload 不是合法 JSON") from error


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("Session Journal 只允许持久化合法 JSON") from error


def _validate_version(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} 必须是正整数")


def _validate_claim(
    claim_type: str,
    resource_id: str,
    owner_token: str,
    lease_seconds: float,
) -> None:
    if not claim_type or not resource_id or not owner_token:
        raise ValueError("Claim 必须包含 type/resource/owner")
    if lease_seconds <= 0:
        raise ValueError("Claim lease_seconds 必须大于 0")


def _optional_text(value: Any) -> str | None:
    return str(value) if value is not None and str(value) else None


@runtime_checkable
class SynchronousSessionEventJournal(Protocol):
    def load_events_sync(
        self,
        principal: JournalPrincipal,
        *,
        session_id: str | None = None,
        operation_id: str | None = None,
        run_id: str | None = None,
        journal_kind: JournalKind | None = None,
        after_sequence: int | None = None,
        include_expired: bool = False,
        migration_registry: EventMigrationRegistry | None = None,
        target_schema_version: int | None = None,
    ) -> list[SessionEvent]: ...
