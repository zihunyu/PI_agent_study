"""Durable Host 持久资源的装配与生命周期。"""

from __future__ import annotations

import inspect
import hashlib
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any, Literal, TypeAlias

from ..retry.events import JsonlRetryEventStore
from ..session import (
    JournalKeyProvider,
    JournalPrincipal,
    OperationEventStore,
    SessionJournalOperationEventStore,
    SessionJournalRetryEventStore,
    SessionJournalRuntimeEventStore,
    SQLiteOperationEventStore,
    SQLiteRuntimeEventStore,
    SQLiteSessionEventJournal,
    StaticJournalKeyProvider,
)


@dataclass(slots=True)
class DurableHostResources:
    root: Path
    operation_store: OperationEventStore
    runtime_store: Any
    retry_store: Any
    journal: SQLiteSessionEventJournal | None = None
    journal_principal: JournalPrincipal | None = None
    owned_resources: list[Any] = field(default_factory=list)

    @classmethod
    def create(
        cls,
        state_dir: str | Path,
        *,
        session_id: str,
        store_backend: Literal["journal", "sqlite", "jsonl"] = "journal",
        journal_key_provider: JournalKeyProvider | None = None,
        journal_principal: JournalPrincipal | None = None,
        tenant_id: str = "local",
    ) -> "DurableHostResources":
        if not session_id:
            raise ValueError("session_id 不能为空")
        root = Path(state_dir)
        root.mkdir(parents=True, exist_ok=True)
        journal: SQLiteSessionEventJournal | None = None
        principal: JournalPrincipal | None = None
        runtime_store: Any
        operation_store: OperationEventStore
        retry_store: Any
        if store_backend == "journal":
            provider = journal_key_provider or local_journal_key_provider(root)
            principal = journal_principal or JournalPrincipal.system(tenant_id)
            journal = SQLiteSessionEventJournal(
                root / "agent-state.sqlite3",
                key_provider=provider,
            )
            runtime_store = SessionJournalRuntimeEventStore(
                journal,
                principal,
                session_id=session_id,
            )
            operation_store = (
                SessionJournalOperationEventStore(journal, principal)
            )
            retry_store = SessionJournalRetryEventStore(
                journal,
                principal,
                session_id=session_id,
            )
        elif store_backend == "sqlite":
            path = root / "agent-state.sqlite3"
            runtime_store = SQLiteRuntimeEventStore(
                path,
                session_id=session_id,
            )
            operation_store = SQLiteOperationEventStore(path)
            retry_key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
            retry_store = JsonlRetryEventStore(
                root / "legacy-sqlite-retry" / f"{retry_key}.jsonl"
            )
        elif store_backend == "jsonl":
            raise ValueError(
                "DurableAgentHost 不支持 JSONL Backend：JSONL 仅保留为底层"
                "单进程/离线迁移兼容 Store；请使用 journal、sqlite 或注入"
                "支持事务 Claim 的 resource_factory"
            )
        else:
            raise ValueError(f"不支持的 Store Backend：{store_backend}")
        return cls(
            root,
            operation_store,
            runtime_store,
            retry_store,
            journal,
            principal,
        )

    def own(self, resource: Any) -> Any:
        if resource is not None and not any(
            item is resource for item in self.owned_resources
        ):
            self.owned_resources.append(resource)
        return resource

    def disown(self, resource: Any) -> None:
        """Remove one caller-owned resource from Host lifecycle management."""

        self.owned_resources[:] = [
            item for item in self.owned_resources if item is not resource
        ]

    async def close(self) -> None:
        errors: list[BaseException] = []
        failed: list[Any] = []
        for resource in reversed(tuple(self.owned_resources)):
            try:
                closer = getattr(resource, "aclose", None) or getattr(
                    resource, "close", None
                )
                if closer is None:
                    continue
                value = closer()
                if inspect.isawaitable(value):
                    await value
            except BaseException as error:
                errors.append(error)
                failed.append(resource)
        # Successfully closed resources are removed. Failed resources remain in
        # their original ownership order so a later close() can retry them.
        self.owned_resources[:] = list(reversed(failed))
        if errors:
            raise BaseExceptionGroup("关闭 Durable Host 资源失败", errors)


@dataclass(frozen=True, slots=True)
class DurableResourceRequest:
    """外部 Store Adapter 创建一组 Host 资源时收到的稳定参数。"""

    state_dir: Path
    session_id: str
    tenant_id: str
    journal_key_provider: JournalKeyProvider | None = None
    journal_principal: JournalPrincipal | None = None


DurableHostResourceFactory: TypeAlias = Callable[
    [DurableResourceRequest],
    DurableHostResources | Awaitable[DurableHostResources],
]


def local_journal_key_provider(root: str | Path) -> StaticJournalKeyProvider:
    """为单机默认模式保存独立数据密钥；生产应显式注入 KMS Provider。"""

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / ".agent-journal.key"
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(secrets.token_bytes(32))
            stream.flush()
            os.fsync(stream.fileno())
    key = b""
    for _ in range(100):
        try:
            key = path.read_bytes()
        except FileNotFoundError:
            key = b""
        if len(key) == 32:
            break
        time.sleep(0.01)
    if len(key) != 32:
        raise RuntimeError(
            f"本地 Journal Key 必须是 32 Bytes：{path}"
        )
    try:
        path.chmod(0o600)
    except OSError:
        # Windows ACL 由部署环境管理；KMS Provider 不依赖本地 Key 文件。
        pass
    return StaticJournalKeyProvider(
        {"local-file-v1": key},
        active_key_id="local-file-v1",
    )


__all__ = [
    "DurableHostResourceFactory",
    "DurableHostResources",
    "DurableResourceRequest",
    "local_journal_key_provider",
]
