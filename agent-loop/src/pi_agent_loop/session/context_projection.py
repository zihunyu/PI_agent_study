"""Rebuild model-visible conversation context from durable operation events."""

from __future__ import annotations

import copy
from dataclasses import dataclass

from .catalog import (
    ConversationSessionNotFoundError,
    WorkspaceSessionCatalog,
)
from .operation_events import OperationEvent
from .operation_state import OperationLogInvariantError, replay_operation
from .operation_store import OperationEventStore


class SessionContextProjectionError(RuntimeError):
    """A durable conversation transcript cannot be projected safely."""


@dataclass(frozen=True, slots=True)
class SessionContext:
    session_id: str
    messages: tuple[dict, ...]
    last_sequence: int
    operation_id: str | None
    inherited_from_session_id: str | None = None

    def copy_messages(self) -> list[dict]:
        return copy.deepcopy(list(self.messages))


class SessionContextProjection:
    """Select the latest operation snapshot for a logical conversation.

    Every new durable operation starts by recording the complete input context.
    Consequently the newest operation is an authoritative transcript snapshot;
    summing messages across operations would duplicate the old history.
    """

    def __init__(
        self,
        operation_store: OperationEventStore,
        *,
        catalog: WorkspaceSessionCatalog | None = None,
    ) -> None:
        self.operation_store = operation_store
        self.catalog = catalog

    async def project(
        self,
        session_id: str,
        *,
        at_sequence: int | None = None,
    ) -> SessionContext:
        return await self._project(
            session_id,
            at_sequence=at_sequence,
            visited=frozenset(),
        )

    async def _project(
        self,
        session_id: str,
        *,
        at_sequence: int | None,
        visited: frozenset[str],
    ) -> SessionContext:
        if not session_id:
            raise ValueError("session_id 不能为空")
        if at_sequence is not None and (
            isinstance(at_sequence, bool)
            or not isinstance(at_sequence, int)
            or at_sequence < -1
        ):
            raise ValueError("at_sequence 必须是大于等于 -1 的整数或 None")
        if session_id in visited:
            raise SessionContextProjectionError("Session 分叉关系存在循环")

        events = await self.operation_store.load(session_id=session_id)
        if at_sequence is not None:
            events = [event for event in events if event.sequence <= at_sequence]
        if events:
            return self._latest_operation_context(session_id, events)

        if self.catalog is None:
            return SessionContext(session_id, (), -1, None)
        try:
            metadata = await self.catalog.get_session(
                session_id,
                include_deleted=True,
            )
        except ConversationSessionNotFoundError:
            return SessionContext(session_id, (), -1, None)
        if metadata.parent_session_id is None:
            return SessionContext(session_id, (), -1, None)
        parent = await self._project(
            metadata.parent_session_id,
            at_sequence=metadata.fork_sequence,
            visited=visited | {session_id},
        )
        return SessionContext(
            session_id=session_id,
            messages=parent.messages,
            last_sequence=parent.last_sequence,
            operation_id=None,
            inherited_from_session_id=metadata.parent_session_id,
        )

    @staticmethod
    def _latest_operation_context(
        session_id: str,
        events: list[OperationEvent],
    ) -> SessionContext:
        grouped: dict[str, list[OperationEvent]] = {}
        for event in sorted(events, key=lambda item: item.sequence):
            grouped.setdefault(event.operation_id, []).append(event)
        states = []
        try:
            for operation_events in grouped.values():
                states.append(replay_operation(operation_events))
        except OperationLogInvariantError as error:
            raise SessionContextProjectionError(
                f"Session {session_id} 的 Operation Log 无法重放：{error}"
            ) from error
        latest = max(states, key=lambda item: item.last_sequence)
        return SessionContext(
            session_id=session_id,
            messages=tuple(copy.deepcopy(list(latest.messages))),
            last_sequence=latest.last_sequence,
            operation_id=latest.operation_id,
        )


__all__ = [
    "SessionContext",
    "SessionContextProjection",
    "SessionContextProjectionError",
]
