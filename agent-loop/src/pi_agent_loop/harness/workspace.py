"""High-level product API for projects and durable conversation sessions."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..session import (
    ConversationSession,
    JournalKeyProvider,
    JournalPrincipal,
    LegacyConversationImportResult,
    SessionJournalOperationEventStore,
    SQLiteSessionEventJournal,
    WorkspaceProject,
    WorkspaceSessionCatalog,
    import_legacy_jsonl_conversation,
)
from ..types import AgentTool, Model, StreamFn
from .resources import local_journal_key_provider


class DurableAgentWorkspace:
    """Project/session entry point similar to a coding-agent application's UI.

    The object is lightweight: the SQLite journal opens a connection per atomic
    operation, so callers do not need to keep a hidden database connection alive.
    """

    def __init__(
        self,
        *,
        state_dir: Path,
        key_provider: JournalKeyProvider,
        principal: JournalPrincipal,
        catalog: WorkspaceSessionCatalog,
    ) -> None:
        self.state_dir = state_dir
        self.key_provider = key_provider
        self.principal = principal
        self.catalog = catalog

    @classmethod
    def open(
        cls,
        state_dir: str | Path,
        *,
        journal_key_provider: JournalKeyProvider | None = None,
        journal_principal: JournalPrincipal | None = None,
        tenant_id: str = "local",
    ) -> "DurableAgentWorkspace":
        root = Path(state_dir)
        root.mkdir(parents=True, exist_ok=True)
        provider = journal_key_provider or local_journal_key_provider(root)
        principal = journal_principal or JournalPrincipal.system(tenant_id)
        journal = SQLiteSessionEventJournal(
            root / "agent-state.sqlite3",
            key_provider=provider,
        )
        return cls(
            state_dir=root,
            key_provider=provider,
            principal=principal,
            catalog=WorkspaceSessionCatalog(journal, principal),
        )

    async def create_project(
        self,
        path: str | Path,
        *,
        title: str | None = None,
        project_id: str | None = None,
    ) -> WorkspaceProject:
        return await self.catalog.create_project(
            path,
            title=title,
            project_id=project_id,
        )

    async def ensure_project(
        self,
        path: str | Path,
        *,
        title: str | None = None,
        project_id: str | None = None,
    ) -> WorkspaceProject:
        canonical = str(Path(path).expanduser().resolve(strict=True))
        normalized = os.path.normcase(canonical)
        for project in await self.catalog.list_projects():
            if os.path.normcase(project.canonical_path) == normalized:
                return project
        return await self.create_project(
            canonical,
            title=title,
            project_id=project_id,
        )

    async def create_session(
        self,
        project_id: str,
        *,
        title: str,
        session_id: str | None = None,
        cwd: str | Path | None = None,
        agent_profile: str = "default",
    ) -> ConversationSession:
        return await self.catalog.create_session(
            project_id=project_id,
            title=title,
            session_id=session_id,
            cwd=cwd,
            agent_profile=agent_profile,
        )

    async def fork_session(
        self,
        source_session_id: str,
        *,
        title: str,
        session_id: str | None = None,
        project_id: str | None = None,
        at_sequence: int | None = None,
    ) -> ConversationSession:
        return await self.catalog.fork_session(
            source_session_id,
            title=title,
            session_id=session_id,
            project_id=project_id,
            at_sequence=at_sequence,
        )

    async def import_legacy_jsonl_session(
        self,
        source_path: str | Path,
        *,
        session_id: str,
    ) -> LegacyConversationImportResult:
        """One-time compatibility import; the plaintext source is not deleted."""

        return await import_legacy_jsonl_conversation(
            source_path,
            session_id=session_id,
            target_store=SessionJournalOperationEventStore(
                self.catalog.journal,
                self.principal,
            ),
        )

    async def open_session(
        self,
        session_id: str,
        *,
        model: Model,
        stream_fn: StreamFn,
        system_prompt: str,
        tools: list[AgentTool],
        **host_options: Any,
    ) -> Any:
        """Resume one catalog session as an exclusive DurableAgentHost."""

        reserved = {
            "session_id",
            "state_dir",
            "project_id",
            "workspace_path",
            "session_title",
            "agent_profile",
            "resume_history",
            "exclusive_session",
            "journal_key_provider",
            "journal_principal",
            "tenant_id",
            "model",
            "stream_fn",
            "system_prompt",
            "tools",
        }
        conflicts = sorted(reserved.intersection(host_options))
        if conflicts:
            raise ValueError(
                "open_session 不允许覆盖受管参数：" + ", ".join(conflicts)
            )
        metadata = await self.catalog.get_session(session_id)
        if metadata.status == "archived":
            raise ValueError("归档 Session 需要先 unarchive_session 后才能打开")
        from .durable_agent_host import DurableAgentHost

        return await DurableAgentHost.create(
            session_id=session_id,
            state_dir=self.state_dir,
            model=model,
            stream_fn=stream_fn,
            system_prompt=system_prompt,
            tools=tools,
            project_id=metadata.project_id,
            workspace_path=metadata.cwd,
            session_title=metadata.title,
            agent_profile=metadata.agent_profile,
            resume_history=True,
            managed_session=True,
            exclusive_session=True,
            journal_key_provider=self.key_provider,
            journal_principal=self.principal,
            tenant_id=self.principal.tenant_id,
            **host_options,
        )


__all__ = ["DurableAgentWorkspace"]
