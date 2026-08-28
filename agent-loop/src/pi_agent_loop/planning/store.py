"""Durable Multi-Intent plan storage on the unified Session Journal.

Plan records use the ``retry`` journal kind because they are execution-control
events rather than Agent transcript events.  ``operation_id`` is namespaced by
plan id, so Journal CAS provides one append-only stream per plan without
changing the core Journal schema or confusing the Operation reducer.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import uuid4

from ..session.journal import (
    JournalConflictError,
    JournalPrincipal,
    SQLiteSessionEventJournal,
    SessionEvent,
    SessionEventSpec,
)
from .graph import DependencyGraph
from .state_machine import TaskStateMachine
from .types import MultiIntentPlan, PlanEvent, PlanExecutionState, PlanValidationError

_PLAN_INITIALIZED = "plan_initialized"
_PLAN_EVENT = "plan_event"
_PLAN_CLAIM = "plan_execution"


class PlanStoreError(RuntimeError):
    pass


class PlanNotFoundError(PlanStoreError):
    pass


class PlanStoreConflictError(PlanStoreError):
    pass


class PlanStoreCorruptionError(PlanStoreError):
    pass


class PlanExecutionConflictError(PlanStoreConflictError):
    """Another worker currently owns the plan execution lease."""


class PlanExecutionLeaseLostError(PlanStoreConflictError):
    """The plan lease could not be renewed and execution must stop."""


@dataclass(frozen=True, slots=True)
class DurablePlanRecord:
    session_id: str
    plan: MultiIntentPlan
    state: PlanExecutionState
    events: tuple[PlanEvent, ...]
    initialized_sequence: int
    last_journal_sequence: int


class SessionJournalPlanStore:
    """CAS-backed plan/event projection scoped to one tenant and session."""

    def __init__(
        self,
        journal: SQLiteSessionEventJournal,
        principal: JournalPrincipal,
        *,
        session_id: str,
    ) -> None:
        if not session_id:
            raise ValueError("Plan Store session_id 不能为空")
        self.journal = journal
        self.principal = principal
        self.session_id = session_id

    async def initialize(self, plan: MultiIntentPlan) -> DurablePlanRecord:
        # Validate the dependency graph before making the plan durable.
        DependencyGraph(plan)
        payload = {
            "recordType": "plan",
            "planId": plan.plan_id,
            "plan": plan.to_dict(),
        }
        spec = SessionEventSpec(
            "retry",
            _PLAN_INITIALIZED,
            self.session_id,
            payload,
            operation_id=_stream_id(plan.plan_id),
            event_id=_stable_event_id(
                self.principal.tenant_id,
                self.session_id,
                plan.plan_id,
                "initialized",
                payload,
            ),
        )
        try:
            await self.journal.append_events(
                self.principal,
                [spec],
                expected_last_sequence=-1,
            )
        except JournalConflictError as error:
            # Initialization is idempotent only for byte-equivalent plans.
            try:
                current = await self.load(plan.plan_id)
            except PlanNotFoundError:
                raise PlanStoreConflictError(str(error)) from error
            if current.plan.to_dict() != plan.to_dict():
                raise PlanStoreConflictError(
                    f"Plan ID 已绑定到不同计划：{plan.plan_id}"
                ) from error
            return current
        return await self.load(plan.plan_id)

    async def load(self, plan_id: str) -> DurablePlanRecord:
        _require_text(plan_id, "plan_id")
        rows = await self.journal.load_events(
            self.principal,
            session_id=self.session_id,
            operation_id=_stream_id(plan_id),
            journal_kind="retry",
        )
        if not rows:
            raise PlanNotFoundError(f"Plan 不存在：{plan_id}")
        return self._decode(plan_id, rows)

    async def append_event(
        self,
        plan_id: str,
        event: PlanEvent,
        *,
        expected_last_sequence: int,
        run_id: str | None = None,
    ) -> DurablePlanRecord:
        current = await self.load(plan_id)
        if current.last_journal_sequence != expected_last_sequence:
            raise PlanStoreConflictError(
                "Plan Journal CAS 冲突："
                f"expected={expected_last_sequence}, actual={current.last_journal_sequence}"
            )
        try:
            next_state = TaskStateMachine(current.plan).apply(current.state, event)
        except (KeyError, PlanValidationError) as error:
            raise PlanStoreConflictError(f"Plan Event 与当前状态不匹配：{error}") from error
        payload = {
            "recordType": "event",
            "planId": plan_id,
            "event": event.to_dict(),
        }
        spec = SessionEventSpec(
            "retry",
            _PLAN_EVENT,
            self.session_id,
            payload,
            operation_id=_stream_id(plan_id),
            run_id=run_id,
            source_sequence=event.sequence,
            state_version=event.schema_version,
            event_id=_stable_event_id(
                self.principal.tenant_id,
                self.session_id,
                plan_id,
                str(event.sequence),
                payload,
            ),
        )
        try:
            appended = await self.journal.append_events(
                self.principal,
                [spec],
                expected_last_sequence=expected_last_sequence,
            )
        except JournalConflictError as error:
            # A response may be lost after SQLite committed. Treat an identical
            # event at the same plan sequence as an idempotent retry.
            latest = await self.load(plan_id)
            existing = next(
                (item for item in latest.events if item.sequence == event.sequence),
                None,
            )
            if existing == event:
                return latest
            raise PlanStoreConflictError(str(error)) from error
        return DurablePlanRecord(
            session_id=self.session_id,
            plan=current.plan,
            state=next_state,
            events=(*current.events, event),
            initialized_sequence=current.initialized_sequence,
            last_journal_sequence=appended[0].sequence,
        )

    async def try_acquire_execution(
        self,
        plan_id: str,
        owner_token: str,
        *,
        lease_seconds: float,
    ) -> bool:
        _require_text(plan_id, "plan_id")
        _require_text(owner_token, "owner_token")
        return await self.journal.try_acquire_claim(
            self.principal,
            _PLAN_CLAIM,
            _claim_resource(self.session_id, plan_id),
            owner_token,
            lease_seconds=lease_seconds,
        )

    async def release_execution(self, plan_id: str, owner_token: str) -> None:
        await self.journal.release_claim(
            self.principal,
            _PLAN_CLAIM,
            _claim_resource(self.session_id, plan_id),
            owner_token,
        )

    def new_owner_token(self) -> str:
        return str(uuid4())

    def _decode(
        self,
        requested_plan_id: str,
        rows: list[SessionEvent],
    ) -> DurablePlanRecord:
        initialized = [row for row in rows if row.event_type == _PLAN_INITIALIZED]
        if len(initialized) != 1 or rows[0].event_type != _PLAN_INITIALIZED:
            raise PlanStoreCorruptionError("Plan Stream 必须且只能以一个初始化事件开始")
        first = initialized[0]
        try:
            if set(first.payload) != {"recordType", "planId", "plan"}:
                raise PlanValidationError("Plan 初始化 Payload 字段无效")
            if (
                first.payload.get("recordType") != "plan"
                or first.payload.get("planId") != requested_plan_id
                or not isinstance(first.payload.get("plan"), dict)
            ):
                raise PlanValidationError("Plan 初始化 Payload 身份无效")
            plan = MultiIntentPlan.from_dict(first.payload["plan"])
            if plan.plan_id != requested_plan_id:
                raise PlanValidationError("Plan Stream 与持久 Plan ID 不匹配")
            machine = TaskStateMachine(plan)
            events: list[PlanEvent] = []
            for row in rows[1:]:
                if row.event_type != _PLAN_EVENT:
                    raise PlanValidationError(
                        f"Plan Stream 包含未知 Event Type：{row.event_type}"
                    )
                if set(row.payload) != {"recordType", "planId", "event"}:
                    raise PlanValidationError("Plan Event Payload 字段无效")
                if (
                    row.payload.get("recordType") != "event"
                    or row.payload.get("planId") != requested_plan_id
                    or not isinstance(row.payload.get("event"), dict)
                ):
                    raise PlanValidationError("Plan Event Payload 身份无效")
                event = PlanEvent.from_dict(row.payload["event"])
                if row.source_sequence != event.sequence:
                    raise PlanValidationError("Plan Event Source Sequence 不匹配")
                events.append(event)
            state = machine.replay(events)
        except (KeyError, TypeError, ValueError, PlanValidationError) as error:
            raise PlanStoreCorruptionError(
                f"Plan Stream 无法安全重放：{requested_plan_id}: {error}"
            ) from error
        return DurablePlanRecord(
            session_id=self.session_id,
            plan=plan,
            state=state,
            events=tuple(events),
            initialized_sequence=first.sequence,
            last_journal_sequence=rows[-1].sequence,
        )


def _stream_id(plan_id: str) -> str:
    return f"plan:{plan_id}"


def _claim_resource(session_id: str, plan_id: str) -> str:
    return f"{session_id}:plan:{plan_id}"


def _stable_event_id(
    tenant_id: str,
    session_id: str,
    plan_id: str,
    suffix: str,
    payload: dict[str, object],
) -> str:
    encoded = json.dumps(
        [tenant_id, session_id, plan_id, suffix, payload],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "plan-" + hashlib.sha256(encoded).hexdigest()


def _require_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 不能为空")


__all__ = [
    "DurablePlanRecord",
    "PlanExecutionConflictError",
    "PlanExecutionLeaseLostError",
    "PlanNotFoundError",
    "PlanStoreConflictError",
    "PlanStoreCorruptionError",
    "PlanStoreError",
    "SessionJournalPlanStore",
]
