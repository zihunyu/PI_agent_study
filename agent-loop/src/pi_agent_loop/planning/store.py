"""Durable Multi-Intent plan storage on the unified Session Journal.

Plan records use the ``retry`` journal kind because they are execution-control
events rather than Agent transcript events.  ``operation_id`` is namespaced by
plan id, so Journal CAS provides one append-only stream per plan without
changing the core Journal schema or confusing the Operation reducer.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import uuid4

from ..session.journal import (
    JournalConflictError,
    JournalFencedClaimLostError,
    JournalPrincipal,
    SQLiteSessionEventJournal,
    SessionEvent,
    SessionEventSpec,
)
from ..session.operation_store import ClaimLease, fenced_claim_resource_id
from .graph import DependencyGraph
from .state_machine import TaskStateMachine
from .store_protocol import DurablePlanStoreCapabilities
from .types import (
    MultiIntentPlan,
    PlanEvent,
    PlanExecutionState,
    PlanPhase,
    PlanValidationError,
)

_PLAN_INITIALIZED = "plan_initialized"
_PLAN_EVENT = "plan_event"
_PLAN_DISPATCHABLE = "plan_dispatchable"
_PLAN_COMPLETION_PENDING = "plan_completion_pending"
_PLAN_COMPLETION_ACKED = "plan_completion_acked"
_PLAN_CLAIM = "plan_execution"
_PLAN_COMPLETION_CLAIM = "plan_completion"


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
class PlanCompletionEnvelope:
    """Immutable identity of one at-least-once completion delivery."""

    plan_id: str
    delivery_id: str
    generation: int
    target_phase: PlanPhase
    plan_state_version: int
    state_digest: str

    def __post_init__(self) -> None:
        _require_text(self.plan_id, "completion plan_id")
        _require_text(self.delivery_id, "completion delivery_id")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise ValueError("completion generation 必须是正整数")
        if self.target_phase not in {
            "waiting_approval",
            "completed",
            "failed",
            "manual_intervention",
        }:
            raise ValueError("completion target_phase 不是可投递状态")
        if (
            isinstance(self.plan_state_version, bool)
            or not isinstance(self.plan_state_version, int)
            or self.plan_state_version < 1
        ):
            raise ValueError("completion plan_state_version 必须是正整数")
        if len(self.state_digest) != 64:
            raise ValueError("completion state_digest 必须是 SHA-256")

    def to_payload(self) -> dict[str, object]:
        return {
            "recordType": "completion_pending",
            "planId": self.plan_id,
            "deliveryId": self.delivery_id,
            "generation": self.generation,
            "targetPhase": self.target_phase,
            "planStateVersion": self.plan_state_version,
            "stateDigest": self.state_digest,
        }


@dataclass(frozen=True, slots=True)
class DurablePlanRecord:
    session_id: str
    plan: MultiIntentPlan
    state: PlanExecutionState
    events: tuple[PlanEvent, ...]
    initialized_sequence: int
    last_journal_sequence: int
    dispatchable: bool = True
    completion_pending: bool = False
    completion_generation: int = 0
    completion_acked_generation: int = 0
    completion_envelope: PlanCompletionEnvelope | None = None


class SessionJournalPlanStore:
    """CAS-backed plan/event projection scoped to one tenant and session.

    The SQLite Journal is safe for several processes on the *same host*.  A
    local database file is not a multi-host coordination service, even when a
    network filesystem happens to make the path visible elsewhere.
    """

    capabilities = DurablePlanStoreCapabilities(
        backend_name="sqlite-session-journal",
        atomic_fenced_append=True,
        supports_cross_process=True,
        supports_multi_host=False,
    )

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

    @property
    def tenant_id(self) -> str:
        return self.principal.tenant_id

    async def initialize(
        self,
        plan: MultiIntentPlan,
        *,
        dispatchable: bool = True,
    ) -> DurablePlanRecord:
        # Validate the dependency graph before making the plan durable.
        DependencyGraph(plan)
        if type(dispatchable) is not bool:
            raise TypeError("dispatchable 必须是布尔值")
        spec = self.initialization_spec(plan, dispatchable=dispatchable)
        try:
            await self.journal.append_events(
                self.principal,
                [spec],
                expected_last_sequence=-1,
            )
        except JournalConflictError as error:
            # Initialization is idempotent only for byte-equivalent plans and
            # the same initial dispatch boundary.
            try:
                current = await self.load(plan.plan_id)
            except PlanNotFoundError:
                raise PlanStoreConflictError(str(error)) from error
            if (
                current.plan.to_dict() != plan.to_dict()
                or current.dispatchable != dispatchable
            ):
                raise PlanStoreConflictError(
                    f"Plan ID 已绑定到不同计划或 Dispatch 状态：{plan.plan_id}"
                ) from error
            return current
        return await self.load(plan.plan_id)

    def initialization_spec(
        self,
        plan: MultiIntentPlan,
        *,
        dispatchable: bool,
    ) -> SessionEventSpec:
        """Build the initialization fact for an atomic cross-stream bootstrap.

        The caller may append this spec together with autonomous-run and
        conversation-link facts in one Journal transaction.  A plan created as
        non-dispatchable is invisible to workers until ``mark_dispatchable``.
        """

        DependencyGraph(plan)
        if type(dispatchable) is not bool:
            raise TypeError("dispatchable 必须是布尔值")
        payload = {
            "recordType": "plan",
            "planId": plan.plan_id,
            "plan": plan.to_dict(),
            "dispatchable": dispatchable,
        }
        return SessionEventSpec(
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

    async def mark_dispatchable(self, plan_id: str) -> DurablePlanRecord:
        """Publish a prepared plan after its durable run/conversation link exists."""

        for _ in range(20):
            current = await self.load(plan_id)
            if current.dispatchable:
                return current
            payload = {
                "recordType": "dispatchable",
                "planId": plan_id,
            }
            spec = SessionEventSpec(
                "retry",
                _PLAN_DISPATCHABLE,
                self.session_id,
                payload,
                operation_id=_stream_id(plan_id),
                event_id=_stable_event_id(
                    self.principal.tenant_id,
                    self.session_id,
                    plan_id,
                    "dispatchable",
                    payload,
                ),
            )
            try:
                await self.journal.append_events(
                    self.principal,
                    [spec],
                    expected_last_sequence=current.last_journal_sequence,
                )
            except JournalConflictError:
                continue
            return await self.load(plan_id)
        raise PlanStoreConflictError("Plan Dispatchable 状态并发冲突")

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

    async def list_runnable_plans(
        self,
        *,
        limit: int | None = None,
    ) -> tuple[DurablePlanRecord, ...]:
        """列出当前 Session 中可由后台 Worker 接管的 Plan。

        这里只把 ``pending`` 和进程中断后遗留的 ``running`` 当作
        runnable。``waiting_approval``、``manual_intervention`` 以及所有
        终态都不会进入工作队列。扫描结果只是候选快照；真正的唯一执行权
        仍必须由 ``acquire_execution`` 的 fenced lease 决定。

        每个候选都会通过 ``load`` 完整重放，而不是相信初始化事件中的
        Plan ID 或缓存状态。遇到伪造/损坏的 Plan Stream 时会 fail closed，
        避免 Worker 在不完整状态上继续产生副作用。
        """

        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
        ):
            raise ValueError("Plan runnable limit 必须是大于 0 的整数或 None")
        rows = await self.journal.load_events(
            self.principal,
            session_id=self.session_id,
            journal_kind="retry",
        )
        plan_ids: list[str] = []
        seen: set[str] = set()
        for row in rows:
            if row.event_type != _PLAN_INITIALIZED:
                continue
            payload = row.payload
            if set(payload) not in (
                {"recordType", "planId", "plan"},
                {"recordType", "planId", "plan", "dispatchable"},
            ):
                raise PlanStoreCorruptionError("Plan 初始化索引 Payload 字段无效")
            plan_id = payload.get("planId")
            if (
                payload.get("recordType") != "plan"
                or not isinstance(plan_id, str)
                or not plan_id.strip()
                or (
                    "dispatchable" in payload
                    and type(payload.get("dispatchable")) is not bool
                )
                or row.operation_id != _stream_id(plan_id)
            ):
                raise PlanStoreCorruptionError("Plan 初始化索引身份无效")
            if plan_id in seen:
                raise PlanStoreCorruptionError(
                    f"Plan 初始化索引重复：{plan_id}"
                )
            seen.add(plan_id)
            plan_ids.append(plan_id)

        runnable: list[DurablePlanRecord] = []
        for plan_id in plan_ids:
            record = await self.load(plan_id)
            if record.dispatchable and record.state.phase in {"pending", "running"}:
                runnable.append(record)
        if limit is not None:
            runnable = runnable[:limit]
        return tuple(runnable)

    async def append_event(
        self,
        plan_id: str,
        event: PlanEvent,
        *,
        expected_last_sequence: int,
        run_id: str | None = None,
        lease: ClaimLease | None = None,
        lease_seconds: float | None = None,
        validated_current: DurablePlanRecord | None = None,
    ) -> DurablePlanRecord:
        if lease is not None:
            self._validate_execution_lease(lease, plan_id=plan_id)
            if lease_seconds is None or lease_seconds <= 0:
                raise ValueError(
                    "Fenced Plan Append 必须提供正数 lease_seconds"
                )
        elif lease_seconds is not None:
            raise ValueError("lease_seconds 只能与 ClaimLease 一起提供")
        if validated_current is None:
            current = await self.load(plan_id)
        else:
            current = validated_current
            if (
                current.session_id != self.session_id
                or current.plan.plan_id != plan_id
                or current.last_journal_sequence != expected_last_sequence
            ):
                raise PlanStoreConflictError(
                    "validated_current 与目标 Plan/CAS Head 不匹配"
                )
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
        specs = [
            SessionEventSpec(
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
        ]
        completion_generation = current.completion_generation
        completion_pending = current.completion_pending
        completion_envelope = current.completion_envelope
        if next_state.phase in {
            "waiting_approval",
            "completed",
            "failed",
            "manual_intervention",
        }:
            candidate = _completion_envelope(
                self.principal.tenant_id,
                self.session_id,
                plan_id,
                completion_generation + 1,
                next_state,
            )
            # A state transition supersedes an in-flight older delivery. The
            # old worker may finish its idempotent projection, but its envelope
            # can no longer acknowledge this newer state.
            if (
                completion_envelope is None
                or completion_envelope.target_phase != candidate.target_phase
                or completion_envelope.plan_state_version
                != candidate.plan_state_version
                or completion_envelope.state_digest != candidate.state_digest
            ):
                completion_generation = candidate.generation
                completion_envelope = candidate
                pending_payload = candidate.to_payload()
            else:
                pending_payload = None
        else:
            pending_payload = None
        if pending_payload is not None:
            specs.append(
                SessionEventSpec(
                    "retry",
                    _PLAN_COMPLETION_PENDING,
                    self.session_id,
                    pending_payload,
                    operation_id=_stream_id(plan_id),
                    run_id=run_id,
                    event_id=_stable_event_id(
                        self.principal.tenant_id,
                        self.session_id,
                        plan_id,
                        f"completion:{completion_generation}:pending",
                        pending_payload,
                    ),
                )
            )
            completion_pending = True
        try:
            if lease is None:
                appended = await self.journal.append_events(
                    self.principal,
                    specs,
                    expected_last_sequence=expected_last_sequence,
                )
            else:
                append_fenced = getattr(
                    self.journal,
                    "append_events_if_fenced_claim",
                    None,
                )
                if not callable(append_fenced):
                    raise PlanExecutionLeaseLostError(
                        "Plan Store 不支持原子 Fenced Append"
                    )
                appended = await append_fenced(
                    self.principal,
                    specs,
                    lease,
                    renew_lease_seconds=lease_seconds,
                    expected_last_sequence=expected_last_sequence,
                )
        except JournalFencedClaimLostError as error:
            raise PlanExecutionLeaseLostError(str(error)) from error
        except JournalConflictError as error:
            # A response may be lost after SQLite committed. Treat an identical
            # event at the same plan sequence as an idempotent retry.
            if lease is not None:
                raise PlanStoreConflictError(str(error)) from error
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
            last_journal_sequence=appended[-1].sequence,
            dispatchable=current.dispatchable,
            completion_pending=completion_pending,
            completion_generation=completion_generation,
            completion_acked_generation=current.completion_acked_generation,
            completion_envelope=completion_envelope,
        )

    async def list_completion_pending_plans(
        self,
        *,
        limit: int | None = None,
    ) -> tuple[DurablePlanRecord, ...]:
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
        ):
            raise ValueError("Plan completion limit 必须是大于 0 的整数或 None")
        rows = await self.journal.load_events(
            self.principal,
            session_id=self.session_id,
            journal_kind="retry",
        )
        plan_ids = [
            str(row.payload["planId"])
            for row in rows
            if row.event_type == _PLAN_INITIALIZED
            and isinstance(row.payload.get("planId"), str)
        ]
        pending: list[DurablePlanRecord] = []
        for plan_id in dict.fromkeys(plan_ids):
            record = await self.load(plan_id)
            if record.dispatchable and record.completion_pending:
                pending.append(record)
        return tuple(pending[:limit] if limit is not None else pending)

    async def acquire_completion(
        self,
        plan_id: str,
        owner_token: str,
        *,
        lease_seconds: float,
    ) -> ClaimLease | None:
        record = await self.load(plan_id)
        if not record.completion_pending:
            return None
        return await self.journal.acquire_fenced_claim(
            self.principal,
            _PLAN_COMPLETION_CLAIM,
            _completion_claim_resource(self.session_id, plan_id),
            owner_token,
            lease_seconds=lease_seconds,
        )

    async def renew_completion(
        self,
        lease: ClaimLease,
        *,
        lease_seconds: float,
    ) -> bool:
        self._validate_completion_lease(lease)
        return await self.journal.renew_fenced_claim(
            self.principal,
            lease,
            lease_seconds=lease_seconds,
        )

    async def ack_completion(
        self,
        plan_id: str,
        lease: ClaimLease,
        *,
        lease_seconds: float,
        envelope: PlanCompletionEnvelope,
    ) -> DurablePlanRecord:
        self._validate_completion_lease(lease, plan_id=plan_id)
        if not isinstance(envelope, PlanCompletionEnvelope):
            raise TypeError("completion envelope 类型无效")
        if envelope.plan_id != plan_id:
            raise ValueError("completion envelope 不属于目标 Plan")
        current = await self.load(plan_id)
        if not current.completion_pending:
            if (
                current.completion_envelope == envelope
                and current.completion_acked_generation >= envelope.generation
            ):
                return current
            raise PlanStoreConflictError("Completion Envelope 已被其他代际替换")
        if current.completion_envelope != envelope:
            raise PlanStoreConflictError("Completion Envelope 已过期，禁止 Ack")
        payload = {
            "recordType": "completion_acked",
            "planId": plan_id,
            "deliveryId": envelope.delivery_id,
            "generation": envelope.generation,
        }
        spec = SessionEventSpec(
            "retry",
            _PLAN_COMPLETION_ACKED,
            self.session_id,
            payload,
            operation_id=_stream_id(plan_id),
            event_id=_stable_event_id(
                self.principal.tenant_id,
                self.session_id,
                plan_id,
                f"completion:{envelope.generation}:acked",
                payload,
            ),
        )
        try:
            await self.journal.append_events_if_fenced_claim(
                self.principal,
                [spec],
                lease,
                renew_lease_seconds=lease_seconds,
                expected_last_sequence=current.last_journal_sequence,
            )
        except JournalFencedClaimLostError as error:
            raise PlanExecutionLeaseLostError(str(error)) from error
        except JournalConflictError as error:
            latest = await self.load(plan_id)
            if (
                not latest.completion_pending
                and latest.completion_envelope == envelope
                and latest.completion_acked_generation >= envelope.generation
            ):
                return latest
            raise PlanStoreConflictError(str(error)) from error
        return await self.load(plan_id)

    async def release_completion_lease(self, lease: ClaimLease) -> None:
        self._validate_completion_lease(lease)
        await self.journal.release_fenced_claim(self.principal, lease)

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

    async def acquire_execution(
        self,
        plan_id: str,
        owner_token: str,
        *,
        lease_seconds: float,
    ) -> ClaimLease | None:
        """取得带单调 generation 的 Plan Execution Lease。

        ``try_acquire_execution`` 保留给旧调用者；可靠 Worker 必须保存本方法
        返回的完整 Lease，并在续租、提交边界和释放时使用同一代际。
        """

        _require_text(plan_id, "plan_id")
        _require_text(owner_token, "owner_token")
        return await self.journal.acquire_fenced_claim(
            self.principal,
            _PLAN_CLAIM,
            _claim_resource(self.session_id, plan_id),
            owner_token,
            lease_seconds=lease_seconds,
        )

    async def renew_execution(
        self,
        lease: ClaimLease,
        *,
        lease_seconds: float,
    ) -> bool:
        self._validate_execution_lease(lease)
        return await self.journal.renew_fenced_claim(
            self.principal,
            lease,
            lease_seconds=lease_seconds,
        )

    async def verify_execution(self, lease: ClaimLease) -> bool:
        self._validate_execution_lease(lease)
        return await self.journal.verify_fenced_claim(self.principal, lease)

    async def release_execution_lease(self, lease: ClaimLease) -> None:
        self._validate_execution_lease(lease)
        await self.journal.release_fenced_claim(self.principal, lease)

    async def release_execution(self, plan_id: str, owner_token: str) -> None:
        """旧版 owner-only 释放接口；新 Worker 应调用 release_execution_lease。"""

        await self.journal.release_claim(
            self.principal,
            _PLAN_CLAIM,
            _claim_resource(self.session_id, plan_id),
            owner_token,
        )

    def _validate_execution_lease(
        self,
        lease: ClaimLease,
        *,
        plan_id: str | None = None,
    ) -> None:
        if lease.claim_type != _PLAN_CLAIM:
            raise ValueError("ClaimLease 不属于当前 Session 的 Plan Execution")
        if plan_id is None:
            try:
                scope = json.loads(lease.resource_id)
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                raise ValueError(
                    "ClaimLease 不属于当前 Session 的 Plan Execution"
                ) from error
            if (
                not isinstance(scope, dict)
                or scope.get("claimType") != _PLAN_CLAIM
                or scope.get("sessionId") != self.session_id
                or not isinstance(scope.get("entityId"), str)
                or not scope["entityId"]
                or scope.get("operationId") is not None
            ):
                raise ValueError(
                    "ClaimLease 不属于当前 Session 的 Plan Execution"
                )
        elif lease.resource_id != _claim_resource(self.session_id, plan_id):
            raise ValueError("ClaimLease 不属于目标 Plan")

    def _validate_completion_lease(
        self,
        lease: ClaimLease,
        *,
        plan_id: str | None = None,
    ) -> None:
        prefix = f"{self.session_id}:plan-completion:"
        if (
            lease.claim_type != _PLAN_COMPLETION_CLAIM
            or not lease.resource_id.startswith(prefix)
            or len(lease.resource_id) == len(prefix)
        ):
            raise ValueError("ClaimLease 不属于当前 Session 的 Plan Completion")
        if (
            plan_id is not None
            and lease.resource_id
            != _completion_claim_resource(self.session_id, plan_id)
        ):
            raise ValueError("ClaimLease 不属于目标 Plan Completion")

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
            if set(first.payload) not in (
                {"recordType", "planId", "plan"},
                {"recordType", "planId", "plan", "dispatchable"},
            ):
                raise PlanValidationError("Plan 初始化 Payload 字段无效")
            if (
                first.payload.get("recordType") != "plan"
                or first.payload.get("planId") != requested_plan_id
                or not isinstance(first.payload.get("plan"), dict)
                or (
                    "dispatchable" in first.payload
                    and type(first.payload.get("dispatchable")) is not bool
                )
            ):
                raise PlanValidationError("Plan 初始化 Payload 身份无效")
            plan = MultiIntentPlan.from_dict(first.payload["plan"])
            if plan.plan_id != requested_plan_id:
                raise PlanValidationError("Plan Stream 与持久 Plan ID 不匹配")
            machine = TaskStateMachine(plan)
            events: list[PlanEvent] = []
            # Streams written before the dispatch gate existed were runnable
            # by definition. Preserve that upgrade behavior while all newly
            # prepared autonomous plans carry an explicit false boundary.
            dispatchable = bool(first.payload.get("dispatchable", True))
            completion_pending = False
            completion_generation = 0
            completion_acked_generation = 0
            completion_envelope: PlanCompletionEnvelope | None = None
            state = machine.initial_state()
            for row in rows[1:]:
                if row.event_type == _PLAN_DISPATCHABLE:
                    if (
                        set(row.payload) != {"recordType", "planId"}
                        or row.payload.get("recordType") != "dispatchable"
                        or row.payload.get("planId") != requested_plan_id
                        or dispatchable
                    ):
                        raise PlanValidationError("Plan Dispatchable Event 无效")
                    dispatchable = True
                    continue
                if row.event_type == _PLAN_COMPLETION_PENDING:
                    generation = row.payload.get("generation")
                    legacy = set(row.payload) == {
                        "recordType",
                        "planId",
                        "generation",
                        "phase",
                    }
                    modern = set(row.payload) == {
                        "recordType",
                        "planId",
                        "deliveryId",
                        "generation",
                        "targetPhase",
                        "planStateVersion",
                        "stateDigest",
                    }
                    if (
                        not (legacy or modern)
                        or row.payload.get("recordType") != "completion_pending"
                        or row.payload.get("planId") != requested_plan_id
                        or isinstance(generation, bool)
                        or not isinstance(generation, int)
                        or generation != completion_generation + 1
                    ):
                        raise PlanValidationError("Plan Completion Pending Event 无效")
                    expected = _completion_envelope(
                        self.principal.tenant_id,
                        self.session_id,
                        requested_plan_id,
                        generation,
                        state,
                    )
                    if legacy:
                        if row.payload.get("phase") != state.phase:
                            raise PlanValidationError(
                                "Legacy Completion Phase 与 Plan State 不匹配"
                            )
                    elif any(
                        (
                            row.payload.get("deliveryId") != expected.delivery_id,
                            row.payload.get("targetPhase") != expected.target_phase,
                            row.payload.get("planStateVersion")
                            != expected.plan_state_version,
                            row.payload.get("stateDigest") != expected.state_digest,
                        )
                    ):
                        raise PlanValidationError(
                            "Completion Envelope 与 Plan State 不匹配"
                        )
                    completion_generation = generation
                    completion_envelope = expected
                    completion_pending = True
                    continue
                if row.event_type == _PLAN_COMPLETION_ACKED:
                    generation = row.payload.get("generation")
                    legacy = set(row.payload) == {
                        "recordType",
                        "planId",
                        "generation",
                    }
                    modern = set(row.payload) == {
                        "recordType",
                        "planId",
                        "deliveryId",
                        "generation",
                    }
                    if (
                        not (legacy or modern)
                        or row.payload.get("recordType") != "completion_acked"
                        or row.payload.get("planId") != requested_plan_id
                        or generation != completion_generation
                        or not completion_pending
                        or completion_envelope is None
                        or (
                            modern
                            and row.payload.get("deliveryId")
                            != completion_envelope.delivery_id
                        )
                    ):
                        raise PlanValidationError("Plan Completion Ack Event 无效")
                    completion_pending = False
                    completion_acked_generation = completion_generation
                    continue
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
                state = machine.apply(state, event)
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
            dispatchable=dispatchable,
            completion_pending=completion_pending,
            completion_generation=completion_generation,
            completion_acked_generation=completion_acked_generation,
            completion_envelope=completion_envelope,
        )


def _stream_id(plan_id: str) -> str:
    return f"plan:{plan_id}"


def _claim_resource(session_id: str, plan_id: str) -> str:
    return fenced_claim_resource_id(
        _PLAN_CLAIM,
        session_id=session_id,
        entity_id=plan_id,
    )


def _completion_claim_resource(session_id: str, plan_id: str) -> str:
    return f"{session_id}:plan-completion:{plan_id}"


def _completion_envelope(
    tenant_id: str,
    session_id: str,
    plan_id: str,
    generation: int,
    state: PlanExecutionState,
) -> PlanCompletionEnvelope:
    state_digest = hashlib.sha256(
        json.dumps(
            state.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    identity = {
        "tenantId": tenant_id,
        "sessionId": session_id,
        "planId": plan_id,
        "generation": generation,
        "targetPhase": state.phase,
        "planStateVersion": state.version,
        "stateDigest": state_digest,
    }
    delivery_id = "plan-completion-" + hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return PlanCompletionEnvelope(
        plan_id=plan_id,
        delivery_id=delivery_id,
        generation=generation,
        target_phase=state.phase,
        plan_state_version=state.version,
        state_digest=state_digest,
    )


def _stable_event_id(
    tenant_id: str,
    session_id: str,
    plan_id: str,
    suffix: str,
    payload: Mapping[str, object],
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
    "PlanCompletionEnvelope",
    "PlanExecutionConflictError",
    "PlanExecutionLeaseLostError",
    "PlanNotFoundError",
    "PlanStoreConflictError",
    "PlanStoreCorruptionError",
    "PlanStoreError",
    "SessionJournalPlanStore",
]
