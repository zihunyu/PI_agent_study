"""One-time import of legacy JSONL conversations into the unified journal."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .journal_adapters import SessionJournalOperationEventStore
from .operation_events import OperationEvent
from .operation_state import OperationLogInvariantError, replay_operation
from .operation_store import JsonlOperationEventStore


class LegacyConversationImportError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LegacyConversationImportResult:
    imported: bool
    message_count: int = 0
    operation_id: str | None = None


async def import_legacy_jsonl_conversation(
    source_path: str | Path,
    *,
    session_id: str,
    target_store: SessionJournalOperationEventStore,
) -> LegacyConversationImportResult:
    """Copy one legacy session once, preserving the source file as backup.

    Old command-line runs created an independent operation on every process
    start, so those operations did not carry previous context.  The importer
    concatenates such transcripts.  If an operation does contain explicit
    ``initialContext`` messages, it is treated as a newer authoritative
    transcript snapshot instead of being appended again.
    """

    path = Path(source_path)
    if not path.is_file():
        return LegacyConversationImportResult(False)
    owner_token = uuid4().hex
    acquired = await target_store.try_acquire_claim(
        "conversation_session_writer",
        session_id,
        owner_token,
        lease_seconds=60,
    )
    if not acquired:
        raise LegacyConversationImportError(
            f"Session {session_id} 正在使用，不能导入旧 JSONL"
        )
    try:
        if await target_store.load(session_id=session_id):
            return LegacyConversationImportResult(False)
        legacy_events = await JsonlOperationEventStore(path).load(
            session_id=session_id
        )
        if not legacy_events:
            return LegacyConversationImportResult(False)
        transcript, outcome = _merge_operation_transcripts(legacy_events)
        if not transcript:
            return LegacyConversationImportResult(False)
        operation_id = f"legacy-import-{uuid4().hex}"
        specs: list[tuple[str, dict]] = [
            (
                "operation_started",
                {
                    "configuration": {
                        "legacyImport": True,
                        "source": path.name,
                    },
                    "tools": [],
                },
            ),
            *[
                (
                    "message_appended",
                    {
                        "message": copy.deepcopy(message),
                        "initialContext": True,
                        "legacyImport": True,
                    },
                )
                for message in transcript
            ],
            ("operation_finished", {"outcome": outcome}),
        ]
        _validate_import_candidate(session_id, operation_id, specs)
        await target_store.append_batch(
            session_id,
            operation_id,
            specs,
            expected_last_sequence=-1,
        )
        return LegacyConversationImportResult(
            True,
            message_count=len(transcript),
            operation_id=operation_id,
        )
    finally:
        await target_store.release_claim(
            "conversation_session_writer",
            session_id,
            owner_token,
        )


def _merge_operation_transcripts(
    events: list[OperationEvent],
) -> tuple[list[dict], str]:
    grouped: dict[str, list[OperationEvent]] = {}
    for event in sorted(events, key=lambda item: item.sequence):
        grouped.setdefault(event.operation_id, []).append(event)
    ordered = sorted(
        grouped.values(),
        key=lambda items: min(item.sequence for item in items),
    )
    transcript: list[dict] = []
    outcome = "completed"
    for operation_events in ordered:
        try:
            state = replay_operation(operation_events)
        except OperationLogInvariantError as error:
            raise LegacyConversationImportError(
                "旧 Operation Log 无法安全导入：" + str(error)
            ) from error
        if state.phase not in {"completed", "failed", "cancelled"}:
            raise LegacyConversationImportError(
                f"旧 Operation {state.operation_id} 尚未结束，不能自动合并"
            )
        outcome = state.outcome or state.phase
        messages = copy.deepcopy(list(state.messages))
        has_initial_context = any(
            event.type == "message_appended"
            and event.data.get("initialContext") is True
            for event in operation_events
        )
        if has_initial_context:
            transcript = messages
        else:
            transcript.extend(messages)
    return transcript, outcome


def _validate_import_candidate(
    session_id: str,
    operation_id: str,
    specs: list[tuple[str, dict]],
) -> None:
    events = [
        OperationEvent(
            type=event_type,
            session_id=session_id,
            operation_id=operation_id,
            sequence=index,
            data=copy.deepcopy(data),
        )
        for index, (event_type, data) in enumerate(specs)
    ]
    try:
        replay_operation(events)
    except OperationLogInvariantError as error:
        raise LegacyConversationImportError(
            "合并后的旧对话不满足 Transcript 协议：" + str(error)
        ) from error


__all__ = [
    "LegacyConversationImportError",
    "LegacyConversationImportResult",
    "import_legacy_jsonl_conversation",
]
