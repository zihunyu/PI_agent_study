"""Durable Host 持久资源的装配与生命周期。"""

from __future__ import annotations

import inspect
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..retry.events import JsonlRetryEventStore
from ..session import (
    JournalKeyProvider,
    JournalPrincipal,
    JsonlOperationEventStore,
    JsonlRuntimeEventStore,
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
        if store_backend == "journal":
            provider = journal_key_provider or _local_journal_key_provider(root)
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
            operation_store: OperationEventStore = (
                SessionJournalOperationEventStore(journal, principal)
            )
            retry_store: Any = SessionJournalRetryEventStore(
                journal,
                principal,
                session_id=session_id,
            )
        elif store_backend == "sqlite":
            path = root / "agent-state.sqlite3"
            runtime_store = SQLiteRuntimeEventStore(path)
            operation_store: OperationEventStore = SQLiteOperationEventStore(path)
            retry_store = JsonlRetryEventStore(root / "retry-events.jsonl")
        elif store_backend == "jsonl":
            runtime_store = JsonlRuntimeEventStore(root / "runtime-events.jsonl")
            operation_store = JsonlOperationEventStore(root / "operation-events.jsonl")
            retry_store = JsonlRetryEventStore(root / "retry-events.jsonl")
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


def _local_journal_key_provider(root: Path) -> StaticJournalKeyProvider:
    """为单机默认模式保存独立数据密钥；生产应显式注入 KMS Provider。"""

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


__all__ = ["DurableHostResources"]
