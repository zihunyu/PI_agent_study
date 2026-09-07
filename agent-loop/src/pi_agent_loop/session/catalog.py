"""Event-sourced workspace/project and conversation-session catalog.

The catalog deliberately stores only product metadata.  Model-visible messages
remain in the operation journal and are rebuilt by :mod:`context_projection`.
This keeps project/session navigation separate from transcript facts while both
remain encrypted, checksummed and auditable in the unified Session Journal.
"""

from __future__ import annotations

from .journal import SessionEventJournal

import asyncio
import copy
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from .journal import (
    JournalConflictError,
    JournalFencedClaimLostError,
    JournalPrincipal,
    SessionEvent,
    SessionEventSpec,
)
from .operation_store import ClaimLease

_CATALOG_SESSION_ID = "__workspace_catalog__"
_CATALOG_PROJECTION = "workspace_catalog"
_CATALOG_STATE_VERSION = 1
_MAX_CONFLICT_RETRIES = 20

SessionStatus = Literal["active", "archived", "deleted"]


class WorkspaceCatalogError(RuntimeError):
    """Base error for invalid catalog operations."""


class WorkspaceNotFoundError(WorkspaceCatalogError):
    pass


class ConversationSessionNotFoundError(WorkspaceCatalogError):
    pass


class SessionConfigurationMismatchError(WorkspaceCatalogError):
    """The persisted session cannot safely be opened with this Agent profile."""


@dataclass(frozen=True, slots=True)
class WorkspaceProject:
    project_id: str
    title: str
    canonical_path: str
    created_at: int
    updated_at: int
    session_ids: tuple[str, ...] = ()
    available: bool = True


@dataclass(frozen=True, slots=True)
class ConversationSession:
    session_id: str
    project_id: str | None
    title: str
    status: SessionStatus
    cwd: str
    agent_profile: str
    configuration_hash: str | None
    created_at: int
    updated_at: int
    parent_session_id: str | None = None
    fork_sequence: int | None = None
    last_activity_sequence: int | None = None


def _empty_catalog() -> dict[str, Any]:
    return {
        "projectOrder": [],
        "projects": {},
        "sessions": {},
    }


def _reduce_catalog(state: dict[str, Any], event: SessionEvent) -> dict[str, Any]:
    """Pure reducer used for event replay and candidate validation."""

    output = copy.deepcopy(state)
    projects = output["projects"]
    sessions = output["sessions"]
    payload = event.payload

    if event.event_type == "workspace_created":
        project = copy.deepcopy(payload["project"])
        project_id = project["projectId"]
        if project_id in projects:
            raise WorkspaceCatalogError(f"Project ID 已存在：{project_id}")
        projects[project_id] = project
        output["projectOrder"].append(project_id)
    elif event.event_type == "workspace_renamed":
        project = _project_entry(output, payload["projectId"])
        project["title"] = payload["title"]
        project["updatedAt"] = payload["updatedAt"]
    elif event.event_type == "workspace_deleted":
        project_id = payload["projectId"]
        _project_entry(output, project_id)
        projects.pop(project_id)
        output["projectOrder"] = [
            item for item in output["projectOrder"] if item != project_id
        ]
        for session in sessions.values():
            if session["projectId"] == project_id:
                session["projectId"] = None
                session["updatedAt"] = payload["updatedAt"]
    elif event.event_type == "session_created":
        session = copy.deepcopy(payload["session"])
        session_id = session["sessionId"]
        if session_id in sessions:
            raise WorkspaceCatalogError(f"Session ID 已存在：{session_id}")
        project_id = session["projectId"]
        if project_id is not None:
            project = _project_entry(output, project_id)
            project["sessionIds"].append(session_id)
            project["updatedAt"] = session["updatedAt"]
        sessions[session_id] = session
    elif event.event_type == "session_renamed":
        session = _session_entry(output, payload["sessionId"])
        session["title"] = payload["title"]
        session["updatedAt"] = payload["updatedAt"]
    elif event.event_type == "session_status_changed":
        session = _session_entry(output, payload["sessionId"])
        session["status"] = payload["status"]
        session["updatedAt"] = payload["updatedAt"]
    elif event.event_type == "session_configuration_bound":
        session = _session_entry(output, payload["sessionId"])
        session["configurationHash"] = payload["configurationHash"]
        session["agentProfile"] = payload["agentProfile"]
        session["updatedAt"] = payload["updatedAt"]
    elif event.event_type == "session_configuration_migrated":
        session = _session_entry(output, payload["sessionId"])
        if session["configurationHash"] != payload["fromConfigurationHash"]:
            raise WorkspaceCatalogError("Session 配置迁移的来源 Hash 与当前状态不一致")
        if session["agentProfile"] != payload["agentProfile"]:
            raise WorkspaceCatalogError("Session 配置迁移不能改变 Agent Profile")
        if payload["configurationHash"] == payload["fromConfigurationHash"]:
            raise WorkspaceCatalogError("Session 配置迁移前后 Hash 不能相同")
        session["configurationHash"] = payload["configurationHash"]
        session["updatedAt"] = payload["updatedAt"]
    elif event.event_type == "session_activity_recorded":
        session = _session_entry(output, payload["sessionId"])
        session["lastActivitySequence"] = payload.get("lastActivitySequence")
        session["updatedAt"] = payload["updatedAt"]
    elif event.event_type == "session_moved":
        session = _session_entry(output, payload["sessionId"])
        old_project_id = session["projectId"]
        new_project_id = payload["projectId"]
        if old_project_id is not None:
            old_project = _project_entry(output, old_project_id)
            old_project["sessionIds"] = [
                item
                for item in old_project["sessionIds"]
                if item != session["sessionId"]
            ]
            old_project["updatedAt"] = payload["updatedAt"]
        if new_project_id is not None:
            new_project = _project_entry(output, new_project_id)
            new_project["sessionIds"].append(session["sessionId"])
            new_project["updatedAt"] = payload["updatedAt"]
        session["projectId"] = new_project_id
        session["cwd"] = payload["cwd"]
        session["updatedAt"] = payload["updatedAt"]
    elif event.event_type == "project_sessions_reordered":
        project = _project_entry(output, payload["projectId"])
        requested = list(payload["sessionIds"])
        if set(requested) != set(project["sessionIds"]):
            raise WorkspaceCatalogError("排序必须包含 Project 当前全部 Session")
        project["sessionIds"] = requested
        project["updatedAt"] = payload["updatedAt"]
    else:
        raise WorkspaceCatalogError(f"未知 Workspace Catalog Event：{event.event_type}")
    return output


class WorkspaceSessionCatalog:
    """Encrypted event-sourced catalog for projects and conversation sessions."""

    def __init__(
        self,
        journal: SessionEventJournal,
        principal: JournalPrincipal,
    ) -> None:
        self.journal = journal
        self.principal = principal
        self._mutation_lock = asyncio.Lock()

    async def create_project(
        self,
        path: str | Path,
        *,
        title: str | None = None,
        project_id: str | None = None,
    ) -> WorkspaceProject:
        canonical = _canonical_directory(path)
        identifier = _identifier(project_id or f"project_{uuid4().hex}", "project_id")
        label = _title(title or Path(canonical).name or canonical)
        now = _now_ms()

        def build(state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            normalized = os.path.normcase(canonical)
            if any(
                os.path.normcase(item["canonicalPath"]) == normalized
                for item in state["projects"].values()
            ):
                raise WorkspaceCatalogError(f"Project Path 已存在：{canonical}")
            if identifier in state["projects"]:
                raise WorkspaceCatalogError(f"Project ID 已存在：{identifier}")
            return (
                "workspace_created",
                {
                    "project": {
                        "projectId": identifier,
                        "title": label,
                        "canonicalPath": canonical,
                        "createdAt": now,
                        "updatedAt": now,
                        "sessionIds": [],
                    }
                },
            )

        state = await self._mutate(build)
        return _project_from_entry(state["projects"][identifier])

    async def get_project(self, project_id: str) -> WorkspaceProject:
        state, _ = await self._load()
        entry = state["projects"].get(project_id)
        if entry is None:
            raise WorkspaceNotFoundError(f"Project 不存在：{project_id}")
        return _project_from_entry(entry)

    async def list_projects(self) -> list[WorkspaceProject]:
        state, _ = await self._load()
        return [
            _project_from_entry(state["projects"][project_id])
            for project_id in state["projectOrder"]
        ]

    async def rename_project(self, project_id: str, title: str) -> WorkspaceProject:
        label = _title(title)
        now = _now_ms()

        def build(state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            _project_entry(state, project_id)
            return "workspace_renamed", {
                "projectId": project_id,
                "title": label,
                "updatedAt": now,
            }

        state = await self._mutate(build)
        return _project_from_entry(state["projects"][project_id])

    async def delete_project(self, project_id: str) -> None:
        now = _now_ms()

        def build(state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            _project_entry(state, project_id)
            return "workspace_deleted", {
                "projectId": project_id,
                "updatedAt": now,
            }

        await self._mutate(build)

    async def create_session(
        self,
        *,
        project_id: str | None,
        title: str,
        cwd: str | Path | None = None,
        session_id: str | None = None,
        agent_profile: str = "default",
        configuration_hash: str | None = None,
        parent_session_id: str | None = None,
        fork_sequence: int | None = None,
        fenced_claim: ClaimLease | None = None,
        fenced_claim_lease_seconds: float = 300,
    ) -> ConversationSession:
        identifier = _identifier(session_id or f"session_{uuid4().hex}", "session_id")
        if identifier == _CATALOG_SESSION_ID:
            raise ValueError("Session ID 使用了保留值")
        label = _title(title)
        profile = _identifier(agent_profile, "agent_profile")
        digest = _optional_digest(configuration_hash)
        now = _now_ms()

        state, _ = await self._load()
        project = (
            _project_entry(state, project_id)
            if project_id is not None
            else None
        )
        base_cwd = cwd if cwd is not None else (
            project["canonicalPath"] if project is not None else None
        )
        if base_cwd is None:
            raise ValueError("未归属 Project 的 Session 必须提供 cwd")
        canonical_cwd = _canonical_directory(base_cwd)
        if project is not None:
            _require_within_project(canonical_cwd, project["canonicalPath"])

        def build(current: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            if identifier in current["sessions"]:
                raise WorkspaceCatalogError(f"Session ID 已存在：{identifier}")
            if project_id is not None:
                current_project = _project_entry(current, project_id)
                _require_within_project(
                    canonical_cwd,
                    current_project["canonicalPath"],
                )
            if parent_session_id is not None:
                parent = _session_entry(current, parent_session_id)
                if parent["status"] == "deleted":
                    raise WorkspaceCatalogError("不能从已删除 Session 分叉")
            return (
                "session_created",
                {
                    "session": {
                        "sessionId": identifier,
                        "projectId": project_id,
                        "title": label,
                        "status": "active",
                        "cwd": canonical_cwd,
                        "agentProfile": profile,
                        "configurationHash": digest,
                        "createdAt": now,
                        "updatedAt": now,
                        "parentSessionId": parent_session_id,
                        "forkSequence": fork_sequence,
                        "lastActivitySequence": None,
                    }
                },
            )

        result = await self._mutate(
            build,
            fenced_claim=fenced_claim,
            fenced_session_id=identifier,
            fenced_claim_lease_seconds=fenced_claim_lease_seconds,
        )
        return _session_from_entry(result["sessions"][identifier])

    async def fork_session(
        self,
        source_session_id: str,
        *,
        title: str,
        session_id: str | None = None,
        project_id: str | None = None,
        at_sequence: int | None = None,
    ) -> ConversationSession:
        source = await self.get_session(source_session_id, include_deleted=True)
        if source.status == "deleted":
            raise WorkspaceCatalogError("不能从已删除 Session 分叉")
        target_project_id = project_id if project_id is not None else source.project_id
        target_cwd: str | None = source.cwd
        if target_project_id != source.project_id and target_project_id is not None:
            target_cwd = (await self.get_project(target_project_id)).canonical_path
        if at_sequence is None:
            events = await self.journal.load_events(
                self.principal,
                session_id=source_session_id,
                journal_kind="operation",
            )
            at_sequence = events[-1].sequence if events else -1
        if isinstance(at_sequence, bool) or not isinstance(at_sequence, int) or at_sequence < -1:
            raise ValueError("fork_sequence 必须是大于等于 -1 的整数")
        return await self.create_session(
            project_id=target_project_id,
            title=title,
            cwd=target_cwd,
            session_id=session_id,
            agent_profile=source.agent_profile,
            configuration_hash=source.configuration_hash,
            parent_session_id=source.session_id,
            fork_sequence=at_sequence,
        )

    async def get_session(
        self,
        session_id: str,
        *,
        include_deleted: bool = False,
    ) -> ConversationSession:
        state, _ = await self._load()
        entry = state["sessions"].get(session_id)
        if entry is None or (entry["status"] == "deleted" and not include_deleted):
            raise ConversationSessionNotFoundError(f"Session 不存在：{session_id}")
        return _session_from_entry(entry)

    async def list_sessions(
        self,
        *,
        project_id: str | None = None,
        include_archived: bool = False,
        include_deleted: bool = False,
    ) -> list[ConversationSession]:
        state, _ = await self._load()
        if project_id is not None:
            project = _project_entry(state, project_id)
            ordered_ids = list(project["sessionIds"])
        else:
            ordered_ids = list(state["sessions"])
        output: list[ConversationSession] = []
        for session_id in ordered_ids:
            entry = state["sessions"][session_id]
            if entry["status"] == "deleted" and not include_deleted:
                continue
            if entry["status"] == "archived" and not include_archived:
                continue
            output.append(_session_from_entry(entry))
        return output

    async def rename_session(self, session_id: str, title: str) -> ConversationSession:
        label = _title(title)
        now = _now_ms()

        def build(state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            _active_session_entry(state, session_id)
            return "session_renamed", {
                "sessionId": session_id,
                "title": label,
                "updatedAt": now,
            }

        state = await self._mutate(build)
        return _session_from_entry(state["sessions"][session_id])

    async def archive_session(self, session_id: str) -> ConversationSession:
        return await self._set_session_status(session_id, "archived")

    async def unarchive_session(self, session_id: str) -> ConversationSession:
        return await self._set_session_status(session_id, "active")

    async def delete_session(self, session_id: str) -> None:
        await self._set_session_status(session_id, "deleted")

    async def bind_session_configuration(
        self,
        session_id: str,
        *,
        project_id: str | None,
        cwd: str | Path,
        agent_profile: str,
        configuration_hash: str,
        allow_migration: bool = False,
        fenced_claim: ClaimLease | None = None,
        fenced_claim_lease_seconds: float = 300,
    ) -> ConversationSession:
        if not isinstance(allow_migration, bool):
            raise ValueError("allow_migration 必须是 bool")
        canonical_cwd = _canonical_directory(cwd)
        profile = _identifier(agent_profile, "agent_profile")
        digest = _optional_digest(configuration_hash)
        assert digest is not None
        now = _now_ms()

        async with self._mutation_lock:
            for _ in range(_MAX_CONFLICT_RETRIES):
                state, last_sequence = await self._load()
                entry = _active_session_entry(state, session_id)
                if project_id is not None and entry["projectId"] != project_id:
                    raise SessionConfigurationMismatchError(
                        "Session 所属 Project 与打开参数不一致"
                    )
                if os.path.normcase(entry["cwd"]) != os.path.normcase(canonical_cwd):
                    raise SessionConfigurationMismatchError(
                        "Session 工作目录与打开参数不一致"
                    )
                if entry["agentProfile"] != profile:
                    raise SessionConfigurationMismatchError(
                        "Session Agent Profile 与打开参数不一致"
                    )
                current_digest = entry["configurationHash"]
                if current_digest is not None:
                    if current_digest != digest:
                        if not allow_migration:
                            raise SessionConfigurationMismatchError(
                                "Session 的模型、System Prompt 或 Tool 配置已经变化；"
                                "请新建 Session 或显式迁移配置"
                            )
                        event_type = "session_configuration_migrated"
                        payload = {
                            "sessionId": session_id,
                            "fromConfigurationHash": current_digest,
                            "configurationHash": digest,
                            "agentProfile": profile,
                            "updatedAt": now,
                        }
                    else:
                        return _session_from_entry(entry)
                else:
                    event_type = "session_configuration_bound"
                    payload = {
                        "sessionId": session_id,
                        "configurationHash": digest,
                        "agentProfile": profile,
                        "updatedAt": now,
                    }
                try:
                    appended = await self._append(
                        event_type,
                        payload,
                        expected_last_sequence=last_sequence,
                        fenced_claim=fenced_claim,
                        fenced_session_id=session_id,
                        fenced_claim_lease_seconds=fenced_claim_lease_seconds,
                    )
                except JournalFencedClaimLostError as error:
                    raise WorkspaceCatalogError(
                        "Session Writer Lease 已丢失，禁止修改配置"
                    ) from error
                except JournalConflictError:
                    continue
                result = _reduce_catalog(state, appended)
                await self._save_snapshot(result, appended.sequence)
                return _session_from_entry(result["sessions"][session_id])
        raise WorkspaceCatalogError("绑定 Session 配置时并发冲突过多")

    async def record_activity(
        self,
        session_id: str,
        *,
        last_activity_sequence: int | None,
        fenced_claim: ClaimLease | None = None,
        fenced_claim_lease_seconds: float = 300,
    ) -> ConversationSession:
        if last_activity_sequence is not None and (
            isinstance(last_activity_sequence, bool)
            or not isinstance(last_activity_sequence, int)
            or last_activity_sequence < 0
        ):
            raise ValueError("last_activity_sequence 必须是非负整数或 None")
        now = _now_ms()

        def build(state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            _active_session_entry(state, session_id)
            return "session_activity_recorded", {
                "sessionId": session_id,
                "lastActivitySequence": last_activity_sequence,
                "updatedAt": now,
            }

        state = await self._mutate(
            build,
            fenced_claim=fenced_claim,
            fenced_session_id=session_id,
            fenced_claim_lease_seconds=fenced_claim_lease_seconds,
        )
        return _session_from_entry(state["sessions"][session_id])

    async def move_session(
        self,
        session_id: str,
        *,
        project_id: str | None,
        cwd: str | Path | None = None,
    ) -> ConversationSession:
        state, _ = await self._load()
        session = _active_session_entry(state, session_id)
        project = _project_entry(state, project_id) if project_id is not None else None
        target_cwd = cwd if cwd is not None else (
            project["canonicalPath"] if project is not None else session["cwd"]
        )
        canonical_cwd = _canonical_directory(target_cwd)
        if project is not None:
            _require_within_project(canonical_cwd, project["canonicalPath"])
        now = _now_ms()

        def build(current: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            _active_session_entry(current, session_id)
            if project_id is not None:
                target = _project_entry(current, project_id)
                _require_within_project(canonical_cwd, target["canonicalPath"])
            return "session_moved", {
                "sessionId": session_id,
                "projectId": project_id,
                "cwd": canonical_cwd,
                "updatedAt": now,
            }

        result = await self._mutate(build)
        return _session_from_entry(result["sessions"][session_id])

    async def reorder_sessions(
        self,
        project_id: str,
        session_ids: list[str],
    ) -> None:
        if len(session_ids) != len(set(session_ids)):
            raise ValueError("Session 排序中存在重复 ID")
        now = _now_ms()

        def build(state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            project = _project_entry(state, project_id)
            if set(session_ids) != set(project["sessionIds"]):
                raise WorkspaceCatalogError("排序必须包含 Project 当前全部 Session")
            return "project_sessions_reordered", {
                "projectId": project_id,
                "sessionIds": list(session_ids),
                "updatedAt": now,
            }

        await self._mutate(build)

    async def _set_session_status(
        self,
        session_id: str,
        status: SessionStatus,
    ) -> ConversationSession:
        now = _now_ms()

        def build(state: dict[str, Any]) -> tuple[str, dict[str, Any]]:
            entry = _session_entry(state, session_id)
            if entry["status"] == "deleted" and status != "deleted":
                raise WorkspaceCatalogError("已删除 Session 不能恢复")
            return "session_status_changed", {
                "sessionId": session_id,
                "status": status,
                "updatedAt": now,
            }

        state = await self._mutate(build)
        return _session_from_entry(state["sessions"][session_id])

    async def _load(self) -> tuple[dict[str, Any], int]:
        replay = await self.journal.replay_projection(
            self.principal,
            session_id=_CATALOG_SESSION_ID,
            projection_name=_CATALOG_PROJECTION,
            initial_state=_empty_catalog(),
            reducer=_reduce_catalog,
            journal_kind="audit",
            target_state_version=_CATALOG_STATE_VERSION,
        )
        if not isinstance(replay.state, dict):
            raise WorkspaceCatalogError("Workspace Catalog Snapshot 结构无效")
        return replay.state, replay.last_sequence

    async def _mutate(
        self,
        build: Any,
        *,
        fenced_claim: ClaimLease | None = None,
        fenced_session_id: str | None = None,
        fenced_claim_lease_seconds: float = 300,
    ) -> dict[str, Any]:
        async with self._mutation_lock:
            for _ in range(_MAX_CONFLICT_RETRIES):
                state, last_sequence = await self._load()
                event_type, payload = build(copy.deepcopy(state))
                try:
                    appended = await self._append(
                        event_type,
                        payload,
                        expected_last_sequence=last_sequence,
                        fenced_claim=fenced_claim,
                        fenced_session_id=fenced_session_id,
                        fenced_claim_lease_seconds=fenced_claim_lease_seconds,
                    )
                except JournalFencedClaimLostError as error:
                    raise WorkspaceCatalogError(
                        "Session Writer Lease 已丢失，禁止修改 Session Catalog"
                    ) from error
                except JournalConflictError:
                    continue
                result = _reduce_catalog(state, appended)
                await self._save_snapshot(result, appended.sequence)
                return result
        raise WorkspaceCatalogError("更新 Workspace Catalog 时并发冲突过多")

    async def _append(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        expected_last_sequence: int,
        fenced_claim: ClaimLease | None = None,
        fenced_session_id: str | None = None,
        fenced_claim_lease_seconds: float = 300,
    ) -> SessionEvent:
        specs = [
            SessionEventSpec(
                journal_kind="audit",
                event_type=event_type,
                session_id=_CATALOG_SESSION_ID,
                payload=payload,
                state_version=_CATALOG_STATE_VERSION,
            )
        ]
        if fenced_claim is None:
            events = await self.journal.append_events(
                self.principal,
                specs,
                expected_last_sequence=expected_last_sequence,
            )
        else:
            _validate_session_writer_claim(
                fenced_claim,
                session_id=fenced_session_id,
            )
            events = await self.journal.append_events_if_fenced_claim(
                self.principal,
                specs,
                fenced_claim,
                renew_lease_seconds=fenced_claim_lease_seconds,
                expected_last_sequence=expected_last_sequence,
            )
        return events[0]

    async def _save_snapshot(self, state: dict[str, Any], sequence: int) -> None:
        try:
            await self.journal.save_snapshot(
                self.principal,
                session_id=_CATALOG_SESSION_ID,
                projection_name=_CATALOG_PROJECTION,
                last_sequence=sequence,
                state=state,
                state_version=_CATALOG_STATE_VERSION,
            )
        except Exception:
            # Another writer may already have stored a newer projection.  The
            # event is authoritative and the next replay will include it.
            return


def _validate_session_writer_claim(
    lease: ClaimLease,
    *,
    session_id: str | None,
) -> None:
    if (
        not session_id
        or lease.claim_type != "conversation_session_writer"
        or lease.resource_id != session_id
    ):
        raise JournalFencedClaimLostError(
            "Session Writer Claim 与目标 Session 不匹配"
        )


def _project_entry(state: dict[str, Any], project_id: str | None) -> dict[str, Any]:
    if project_id is None:
        raise WorkspaceNotFoundError("Project ID 不能为空")
    entry = state["projects"].get(project_id)
    if entry is None:
        raise WorkspaceNotFoundError(f"Project 不存在：{project_id}")
    return entry


def _session_entry(state: dict[str, Any], session_id: str) -> dict[str, Any]:
    entry = state["sessions"].get(session_id)
    if entry is None:
        raise ConversationSessionNotFoundError(f"Session 不存在：{session_id}")
    return entry


def _active_session_entry(state: dict[str, Any], session_id: str) -> dict[str, Any]:
    entry = _session_entry(state, session_id)
    if entry["status"] == "deleted":
        raise ConversationSessionNotFoundError(f"Session 已删除：{session_id}")
    return entry


def _project_from_entry(entry: dict[str, Any]) -> WorkspaceProject:
    path = str(entry["canonicalPath"])
    return WorkspaceProject(
        project_id=str(entry["projectId"]),
        title=str(entry["title"]),
        canonical_path=path,
        created_at=int(entry["createdAt"]),
        updated_at=int(entry["updatedAt"]),
        session_ids=tuple(str(item) for item in entry["sessionIds"]),
        available=Path(path).is_dir(),
    )


def _session_from_entry(entry: dict[str, Any]) -> ConversationSession:
    return ConversationSession(
        session_id=str(entry["sessionId"]),
        project_id=entry["projectId"],
        title=str(entry["title"]),
        status=entry["status"],
        cwd=str(entry["cwd"]),
        agent_profile=str(entry["agentProfile"]),
        configuration_hash=entry["configurationHash"],
        created_at=int(entry["createdAt"]),
        updated_at=int(entry["updatedAt"]),
        parent_session_id=entry["parentSessionId"],
        fork_sequence=entry["forkSequence"],
        last_activity_sequence=entry["lastActivitySequence"],
    )


def _canonical_directory(path: str | Path) -> str:
    candidate = Path(path).expanduser().resolve(strict=True)
    if not candidate.is_dir():
        raise ValueError(f"路径不是目录：{candidate}")
    return str(candidate)


def _require_within_project(cwd: str, project_path: str) -> None:
    try:
        Path(cwd).relative_to(Path(project_path))
    except ValueError as error:
        raise WorkspaceCatalogError(
            f"Session cwd 必须位于 Project 内：{cwd}"
        ) from error


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串")
    result = value.strip()
    if len(result) > 256:
        raise ValueError(f"{name} 最长 256 个字符")
    return result


def _title(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("title 必须是非空字符串")
    result = value.strip()
    if len(result) > 512:
        raise ValueError("title 最长 512 个字符")
    return result


def _optional_digest(value: str | None) -> str | None:
    if value is None:
        return None
    digest = value.strip().lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("configuration_hash 必须是 64 位十六进制 SHA-256")
    return digest


def _now_ms() -> int:
    return int(time.time() * 1000)


__all__ = [
    "ConversationSession",
    "ConversationSessionNotFoundError",
    "SessionConfigurationMismatchError",
    "WorkspaceCatalogError",
    "WorkspaceNotFoundError",
    "WorkspaceProject",
    "WorkspaceSessionCatalog",
]
