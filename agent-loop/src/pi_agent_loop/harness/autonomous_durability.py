"""Durable control and conversation projection for autonomous plan runs.

This module deliberately keeps plan execution facts in the plan journal and
conversation facts in an ordinary Operation.  A stable ``run_id`` links both
streams without teaching the model transcript reducer about planner internals.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from ..messages import assistant_message, user_message
from ..planning import (
    ClosedLoopBudget,
    ClosedLoopEvent,
    MultiIntentPlan,
    PlanBudgetExceeded,
    PlanResourceUsage,
    SessionJournalPlanStore,
)
from ..session.journal import (
    JournalConflictError,
    JournalFencedClaimLostError,
    JournalPrincipal,
    SQLiteSessionEventJournal,
    SessionEvent,
    SessionEventSpec,
    SessionStreamKey,
)
from ..session.operation_state import (
    OperationLogInvariantError,
    replay_operation,
    replay_operation_with_specs,
)
from ..session.operation_store import (
    ClaimLease,
    OperationEventStore,
    OperationStoreConflictError,
    operation_last_sequence,
)
from ..session.journal_adapters import SessionJournalOperationEventStore
from ..types import Model


_RUN_INITIALIZED = "autonomous_run_initialized"
_INITIAL_PLAN_BOUND = "autonomous_initial_plan_bound"
_PLAN_REGISTERED = "autonomous_plan_registered"
_CONVERSATION_LINKED = "autonomous_conversation_linked"
_RUN_DISPATCHABLE = "autonomous_run_dispatchable"
_LOOP_EVENT = "autonomous_closed_loop_event"
_SEGMENT_FINISHED = "autonomous_segment_finished"
_RESOURCE_RESERVED = "autonomous_resource_reserved"
_RESOURCE_SETTLED = "autonomous_resource_settled"
_RESOURCE_RELEASED = "autonomous_resource_released"
_ADMISSION_CLOSED = "autonomous_admission_closed"
_RUN_FINALIZED = "autonomous_run_finalized"
_STREAM_PREFIX = "autonomous-run:"
_RUN_CONTROLLER_CLAIM = "autonomous_run_controller"

AutonomousDurableStatus = Literal[
    "completed",
    "waiting_approval",
    "failed",
    "manual_intervention",
]


class AutonomousRunStoreError(RuntimeError):
    """The autonomous run stream is missing, conflicting, or corrupted."""


class AutonomousRunControllerConflictError(AutonomousRunStoreError):
    """Another process owns this run's validate/replan/synthesize controller."""


class AutonomousRunControllerLeaseLostError(AutonomousRunStoreError):
    """The monotonic run-controller lease was lost while work was in flight."""


@dataclass(frozen=True, slots=True)
class AutonomousRunRecord:
    run_id: str
    session_id: str
    request: str
    initial_plan_id: str
    plan_ids: tuple[str, ...]
    budget: ClosedLoopBudget
    correction_rounds_spent: int
    correction_actions_spent: int
    status: AutonomousDurableStatus | None
    response_text: str | None
    pending_approval_ids: tuple[str, ...]
    last_sequence: int
    operation_id: str | None = None
    linked: bool = False
    dispatchable: bool = False
    started_at: float = 0.0
    deadline_at: float | None = None
    settled_usage: PlanResourceUsage = PlanResourceUsage()
    active_reservations: tuple[tuple[str, PlanResourceUsage], ...] = ()
    initial_plan_bound: bool = True
    admission_closed: bool = False
    admission_close_reason: str | None = None

    @property
    def latest_plan_id(self) -> str:
        if not self.plan_ids:
            raise AutonomousRunStoreError("Autonomous Run 尚未绑定 Initial Plan")
        return self.plan_ids[-1]

    @property
    def remaining_budget(self) -> ClosedLoopBudget:
        usage = self.resource_usage
        return ClosedLoopBudget(
            max_correction_rounds=max(
                0,
                self.budget.max_correction_rounds - self.correction_rounds_spent,
            ),
            max_correction_actions=max(
                0,
                self.budget.max_correction_actions - self.correction_actions_spent,
            ),
            max_plan_steps=max(0, self.budget.max_plan_steps - usage.plan_steps),
            max_step_attempts=max(
                0, self.budget.max_step_attempts - usage.step_attempts
            ),
            max_tool_calls=max(0, self.budget.max_tool_calls - usage.tool_calls),
            max_duration_seconds=self.budget.max_duration_seconds,
            max_model_calls=(
                None
                if self.budget.max_model_calls is None
                else max(0, self.budget.max_model_calls - usage.model_calls)
            ),
            max_tokens=(
                None
                if self.budget.max_tokens is None
                else max(0, self.budget.max_tokens - usage.tokens)
            ),
            max_cost=(
                None
                if self.budget.max_cost is None
                else max(0.0, self.budget.max_cost - usage.cost)
            ),
        )

    @property
    def resource_usage(self) -> PlanResourceUsage:
        usage = self.settled_usage
        for _reservation_id, reserved in self.active_reservations:
            usage = usage + reserved
        return usage


class SessionJournalAutonomousRunStore:
    """Encrypted, append-only autonomous-run metadata on the Session journal.

    Closed-loop sequence numbers are scoped by a random segment id.  A process
    may therefore resume the same logical run while correction budget usage is
    counted across every segment instead of resetting after each crash.
    """

    def __init__(
        self,
        journal: SQLiteSessionEventJournal,
        principal: JournalPrincipal,
        *,
        session_id: str,
    ) -> None:
        if not session_id:
            raise ValueError("Autonomous Run Store session_id 不能为空")
        self.journal = journal
        self.principal = principal
        self.session_id = session_id

    async def acquire_controller(
        self,
        run_id: str,
        owner_token: str,
        *,
        lease_seconds: float,
    ) -> ClaimLease | None:
        """Acquire the one controller allowed to validate/replan this run."""

        await self.load(run_id)
        if not isinstance(owner_token, str) or not owner_token.strip():
            raise ValueError("Autonomous Controller owner_token 不能为空")
        return await self.journal.acquire_fenced_claim(
            self.principal,
            _RUN_CONTROLLER_CLAIM,
            _controller_claim_resource(self.session_id, run_id),
            owner_token,
            lease_seconds=lease_seconds,
        )

    async def renew_controller(
        self,
        run_id: str,
        lease: ClaimLease,
        *,
        lease_seconds: float,
    ) -> bool:
        self._validate_controller_lease(lease, run_id=run_id)
        return await self.journal.renew_fenced_claim(
            self.principal,
            lease,
            lease_seconds=lease_seconds,
        )

    async def release_controller(self, run_id: str, lease: ClaimLease) -> None:
        self._validate_controller_lease(lease, run_id=run_id)
        await self.journal.release_fenced_claim(self.principal, lease)

    def _validate_controller_lease(
        self,
        lease: ClaimLease,
        *,
        run_id: str,
    ) -> None:
        if (
            not isinstance(lease, ClaimLease)
            or lease.claim_type != _RUN_CONTROLLER_CLAIM
            or lease.resource_id != _controller_claim_resource(
                self.session_id,
                run_id,
            )
        ):
            raise ValueError(
                "ClaimLease 不属于目标 Autonomous Run Controller"
            )

    async def initialize(
        self,
        request: str,
        initial_plan_id: str,
        budget: ClosedLoopBudget,
        *,
        run_id: str | None = None,
        started_at: float | None = None,
        initial_usage: PlanResourceUsage | None = None,
    ) -> AutonomousRunRecord:
        resolved_run_id = run_id or str(uuid4())
        spec = self.initialization_spec(
            request,
            initial_plan_id,
            budget,
            run_id=resolved_run_id,
            started_at=started_at,
            initial_usage=initial_usage,
        )
        try:
            await self.journal.append_events(
                self.principal,
                [spec],
                expected_last_sequence=-1,
            )
        except JournalConflictError as error:
            current = await self.load(resolved_run_id)
            if (
                current.request != request
                or current.initial_plan_id != initial_plan_id
                or current.budget != budget
            ):
                raise AutonomousRunStoreError(
                    f"Autonomous Run ID 已绑定不同请求：{resolved_run_id}"
                ) from error
            return current
        return await self.load(resolved_run_id)

    async def open_admission(
        self,
        request: str,
        budget: ClosedLoopBudget,
        *,
        run_id: str,
        started_at: float | None = None,
    ) -> AutonomousRunRecord:
        """Create a provisional durable budget ledger before Planner dispatch."""

        provisional_plan_id = f"pending-plan:{run_id}"
        spec = self.initialization_spec(
            request,
            provisional_plan_id,
            budget,
            run_id=run_id,
            started_at=started_at,
            initial_usage=PlanResourceUsage(),
            provisional=True,
        )
        try:
            await self.journal.append_events(
                self.principal,
                [spec],
                expected_last_sequence=-1,
            )
        except JournalConflictError as error:
            current = await self.load(run_id)
            if current.request != request or current.budget != budget:
                raise AutonomousRunStoreError(
                    f"Autonomous Admission ID 已绑定不同请求：{run_id}"
                ) from error
            return current
        return await self.load(run_id)

    async def bind_initial_plan(
        self,
        run_id: str,
        plan_id: str,
    ) -> AutonomousRunRecord:
        """Bind a provisional budget ledger to its first persisted Plan."""

        current = await self.load(run_id)
        if current.admission_closed:
            raise AutonomousRunStoreError(
                "已关闭的 Autonomous Admission 不能绑定 Plan"
            )
        if current.initial_plan_bound:
            if current.initial_plan_id != plan_id:
                raise AutonomousRunStoreError(
                    "Autonomous Run 已绑定不同 Initial Plan"
                )
            return current
        await self._append(
            run_id,
            self._spec(
                run_id,
                _INITIAL_PLAN_BOUND,
                {"runId": run_id, "planId": plan_id},
                stable_key="initial-plan-bound",
            ),
        )
        return await self.load(run_id)

    async def bind_initial_plan_atomic(
        self,
        run_id: str,
        plan: MultiIntentPlan,
        plan_store: SessionJournalPlanStore,
    ) -> AutonomousRunRecord:
        """Atomically persist the first Plan and bind a provisional Run."""

        self._require_same_journal(plan_store)
        current = await self.load(run_id)
        if current.admission_closed:
            raise AutonomousRunStoreError(
                "已关闭的 Autonomous Admission 不能绑定 Plan"
            )
        if current.initial_plan_bound:
            if current.initial_plan_id != plan.plan_id:
                raise AutonomousRunStoreError(
                    "Autonomous Run 已绑定不同 Initial Plan"
                )
            stored = await plan_store.load(plan.plan_id)
            if (
                stored.plan.to_dict() != plan.to_dict()
                or stored.dispatchable
            ):
                raise AutonomousRunStoreError("Initial Plan 内容冲突")
            return current
        if current.resource_usage.plan_steps < len(plan.steps):
            raise AutonomousRunStoreError(
                "Initial Plan 尚未完成 Durable Step Budget Reservation"
            )
        specs = [
            # Binding Plan and Run without the Conversation user turn is only a
            # quarantine/checkpoint state.  The Plan must not become visible to
            # DurablePlanWorker; only AutonomousConversationProjector.bootstrap
            # may atomically publish all three streams as dispatchable.
            plan_store.initialization_spec(plan, dispatchable=False),
            self._spec(
                run_id,
                _INITIAL_PLAN_BOUND,
                {"runId": run_id, "planId": plan.plan_id},
                stable_key="initial-plan-bound",
            ),
        ]
        try:
            await self.journal.append_events(
                self.principal,
                specs,
                expected_stream_sequences={
                    (
                        "retry",
                        self.session_id,
                        specs[0].operation_id,
                    ): -1,
                    (
                        "retry",
                        self.session_id,
                        _stream_id(run_id),
                    ): current.last_sequence,
                },
            )
        except JournalConflictError as error:
            try:
                durable = await self.load(run_id)
                stored = await plan_store.load(plan.plan_id)
            except Exception:
                raise AutonomousRunStoreError(
                    "Initial Plan 与 Autonomous Run 原子绑定冲突"
                ) from error
            if (
                not durable.initial_plan_bound
                or durable.initial_plan_id != plan.plan_id
                or stored.plan.to_dict() != plan.to_dict()
                or stored.dispatchable
            ):
                raise AutonomousRunStoreError(
                    "Initial Plan 与 Autonomous Run 原子绑定冲突"
                ) from error
        return await self.load(run_id)

    def initialization_spec(
        self,
        request: str,
        initial_plan_id: str,
        budget: ClosedLoopBudget,
        *,
        run_id: str,
        started_at: float | None = None,
        initial_usage: PlanResourceUsage | None = None,
        provisional: bool = False,
    ) -> SessionEventSpec:
        effective_started_at = time.time() if started_at is None else started_at
        if (
            isinstance(effective_started_at, bool)
            or not isinstance(effective_started_at, (int, float))
            or effective_started_at <= 0
        ):
            raise ValueError("Autonomous Run started_at 必须是正数")
        effective_started_at = float(effective_started_at)
        usage = initial_usage or PlanResourceUsage()
        if not isinstance(usage, PlanResourceUsage):
            raise TypeError("initial_usage 必须是 PlanResourceUsage 或 None")
        _assert_usage_within_budget(usage, budget)
        deadline_at = (
            None
            if budget.max_duration_seconds is None
            else effective_started_at + float(budget.max_duration_seconds)
        )
        payload = {
            "recordType": "autonomous_run",
            "runId": run_id,
            "request": request,
            "initialPlanId": initial_plan_id,
            "budget": {
                "maxCorrectionRounds": budget.max_correction_rounds,
                "maxCorrectionActions": budget.max_correction_actions,
                "maxPlanSteps": budget.max_plan_steps,
                "maxStepAttempts": budget.max_step_attempts,
                "maxToolCalls": budget.max_tool_calls,
                "maxDurationSeconds": budget.max_duration_seconds,
                "maxModelCalls": budget.max_model_calls,
                "maxTokens": budget.max_tokens,
                "maxCost": budget.max_cost,
            },
            "startedAt": effective_started_at,
            "deadlineAt": deadline_at,
            "initialUsage": usage.to_dict(),
            "provisional": provisional,
        }
        return self._spec(
            run_id,
            _RUN_INITIALIZED,
            payload,
            stable_key="initialized",
        )

    async def load(self, run_id: str) -> AutonomousRunRecord:
        rows = await self._load_rows(run_id)
        if not rows:
            raise AutonomousRunStoreError(f"Autonomous Run 不存在：{run_id}")
        return self._decode(run_id, rows)

    async def find_by_plan_id(self, plan_id: str) -> AutonomousRunRecord | None:
        rows = await self.journal.load_events(
            self.principal,
            session_id=self.session_id,
            journal_kind="retry",
        )
        run_ids: set[str] = set()
        for row in rows:
            run_id = _run_id_from_stream(row.operation_id)
            if (
                row.event_type
                not in {
                _RUN_INITIALIZED,
                _INITIAL_PLAN_BOUND,
                _PLAN_REGISTERED,
            }
                or row.payload.get(
                    "planId",
                    row.payload.get("initialPlanId"),
                )
                != plan_id
                or run_id is None
            ):
                continue
            run_ids.add(run_id)
        if not run_ids:
            return None
        if len(run_ids) != 1:
            raise AutonomousRunStoreError(
                f"Plan 同时绑定多个 Autonomous Run：{plan_id}"
            )
        return await self.load(next(iter(run_ids)))

    async def register_plan(
        self,
        run_id: str,
        plan_id: str,
        *,
        controller_lease: ClaimLease | None = None,
        controller_lease_seconds: float | None = None,
    ) -> AutonomousRunRecord:
        payload = {"runId": run_id, "planId": plan_id}
        await self._append(
            run_id,
            self._spec(
                run_id,
                _PLAN_REGISTERED,
                payload,
                stable_key=f"plan:{plan_id}",
            ),
            controller_lease=controller_lease,
            controller_lease_seconds=controller_lease_seconds,
        )
        return await self.load(run_id)

    async def register_plan_atomic(
        self,
        run_id: str,
        plan: MultiIntentPlan,
        plan_store: SessionJournalPlanStore,
        *,
        controller_lease: ClaimLease | None = None,
        controller_lease_seconds: float | None = None,
    ) -> AutonomousRunRecord:
        """Persist a correction plan and publish its run linkage atomically."""

        self._require_same_journal(plan_store)
        current = await self.load(run_id)
        if not current.linked or not current.dispatchable:
            raise AutonomousRunStoreError(
                "Autonomous Run 尚未完成 Conversation Link，禁止发布纠正 Plan"
            )
        plan_spec = plan_store.initialization_spec(plan, dispatchable=True)
        payload = {"runId": run_id, "planId": plan.plan_id}
        run_spec = self._spec(
            run_id,
            _PLAN_REGISTERED,
            payload,
            stable_key=f"plan:{plan.plan_id}",
        )
        try:
            await self._append_events(
                [plan_spec, run_spec],
                run_id=run_id,
                expected_stream_sequences={
                    (
                        "retry",
                        self.session_id,
                        plan_spec.operation_id,
                    ): -1,
                    (
                        "retry",
                        self.session_id,
                        _stream_id(run_id),
                    ): current.last_sequence,
                },
                controller_lease=controller_lease,
                controller_lease_seconds=controller_lease_seconds,
            )
        except AutonomousRunControllerLeaseLostError:
            raise
        except JournalConflictError as error:
            # A lost response after commit is an idempotent replay only when
            # both halves are already present and byte-equivalent.
            try:
                existing_plan = await plan_store.load(plan.plan_id)
                existing_run = await self.load(run_id)
            except Exception:
                raise AutonomousRunStoreError(
                    "纠正 Plan 与 Autonomous Run 原子注册冲突"
                ) from error
            if (
                existing_plan.plan.to_dict() != plan.to_dict()
                or not existing_plan.dispatchable
                or plan.plan_id not in existing_run.plan_ids
            ):
                raise AutonomousRunStoreError(
                    "纠正 Plan 与 Autonomous Run 原子注册冲突"
                ) from error
        return await self.load(run_id)

    async def link_conversation(
        self,
        run_id: str,
        operation_id: str,
    ) -> AutonomousRunRecord:
        payload = {
            "runId": run_id,
            "operationId": operation_id,
        }
        await self._append(
            run_id,
            self._spec(
                run_id,
                _CONVERSATION_LINKED,
                payload,
                stable_key="conversation-linked",
            ),
        )
        return await self.load(run_id)

    async def mark_dispatchable(self, run_id: str) -> AutonomousRunRecord:
        await self._append(
            run_id,
            self._spec(
                run_id,
                _RUN_DISPATCHABLE,
                {"runId": run_id},
                stable_key="dispatchable",
            ),
        )
        return await self.load(run_id)

    async def reserve_resources(
        self,
        run_id: str,
        *,
        reservation_id: str,
        stage: str,
        reserved: PlanResourceUsage,
        controller_lease: ClaimLease | None = None,
        controller_lease_seconds: float | None = None,
    ) -> AutonomousRunRecord:
        """Atomically admit budget before a Step, Tool or model callback.

        A reservation is never silently refunded. A crash before dispatch may
        reuse the same stable reservation id; a dispatched attempt uses a new
        id on retry and therefore remains charged conservatively.
        """

        if not isinstance(reservation_id, str) or not reservation_id.strip():
            raise ValueError("resource reservation_id 不能为空")
        if not isinstance(stage, str) or not stage.strip():
            raise ValueError("resource reservation stage 不能为空")
        if not isinstance(reserved, PlanResourceUsage):
            raise TypeError("reserved 必须是 PlanResourceUsage")
        if reserved == PlanResourceUsage():
            raise ValueError("resource reservation 不能为空")
        await self._assert_controller_current(
            run_id,
            controller_lease,
            controller_lease_seconds,
        )
        payload = {
            "runId": run_id,
            "reservationId": reservation_id,
            "stage": stage,
            "reserved": reserved.to_dict(),
        }
        spec = self._spec(
            run_id,
            _RESOURCE_RESERVED,
            payload,
            stable_key=f"resource-reserved:{reservation_id}",
        )
        for _ in range(20):
            rows = await self._load_rows(run_id)
            if not rows:
                raise AutonomousRunStoreError(f"Autonomous Run 不存在：{run_id}")
            current = self._decode(run_id, rows)
            if current.admission_closed:
                raise AutonomousRunStoreError(
                    "Autonomous Admission 已关闭，不能再预留资源"
                )
            existing = dict(current.active_reservations).get(reservation_id)
            if existing is not None:
                if existing != reserved:
                    raise AutonomousRunStoreError(
                        "同一 Resource Reservation ID 绑定了不同预算"
                    )
                return current
            if current.status is not None:
                raise AutonomousRunStoreError("Autonomous Run 终态后不能预留资源")
            if current.deadline_at is not None and time.time() >= current.deadline_at:
                raise PlanBudgetExceeded("Autonomous Run wall-clock deadline 已到")
            _assert_usage_within_budget(current.resource_usage + reserved, current.budget)
            try:
                await self._append_events(
                    [spec],
                    run_id=run_id,
                    expected_last_sequence=rows[-1].sequence,
                    controller_lease=controller_lease,
                    controller_lease_seconds=controller_lease_seconds,
                )
            except AutonomousRunControllerLeaseLostError:
                raise
            except JournalConflictError:
                continue
            return await self.load(run_id)
        raise AutonomousRunStoreError("Resource Reservation CAS 冲突")

    async def release_resources(
        self,
        run_id: str,
        *,
        reservation_id: str,
        reason: str,
        controller_lease: ClaimLease | None = None,
        controller_lease_seconds: float | None = None,
    ) -> AutonomousRunRecord:
        """Release a reservation only when its model/Tool was never dispatched."""

        if not isinstance(reservation_id, str) or not reservation_id.strip():
            raise ValueError("resource reservation_id 不能为空")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("resource release reason 不能为空")
        await self._assert_controller_current(
            run_id,
            controller_lease,
            controller_lease_seconds,
        )
        for _ in range(20):
            rows = await self._load_rows(run_id)
            if not rows:
                raise AutonomousRunStoreError(f"Autonomous Run 不存在：{run_id}")
            current = self._decode(run_id, rows)
            reservations = dict(current.active_reservations)
            if reservation_id not in reservations:
                for row in rows:
                    if (
                        row.event_type == _RESOURCE_RELEASED
                        and row.payload.get("reservationId") == reservation_id
                    ):
                        if row.payload.get("reason") != reason:
                            raise AutonomousRunStoreError(
                                "Resource Release 重放原因冲突"
                            )
                        return current
                raise AutonomousRunStoreError("Resource Reservation 不存在或已结算")
            if current.status is not None or current.admission_closed:
                raise AutonomousRunStoreError("Autonomous Run 终态后不能释放资源")
            payload = {
                "runId": run_id,
                "reservationId": reservation_id,
                "reason": reason.strip(),
            }
            spec = self._spec(
                run_id,
                _RESOURCE_RELEASED,
                payload,
                stable_key=f"resource-released:{reservation_id}",
            )
            try:
                await self._append_events(
                    [spec],
                    run_id=run_id,
                    expected_last_sequence=rows[-1].sequence,
                    controller_lease=controller_lease,
                    controller_lease_seconds=controller_lease_seconds,
                )
            except AutonomousRunControllerLeaseLostError:
                raise
            except JournalConflictError:
                continue
            return await self.load(run_id)
        raise AutonomousRunStoreError("Resource Release CAS 冲突")

    async def settle_resources(
        self,
        run_id: str,
        *,
        reservation_id: str,
        actual: PlanResourceUsage,
        controller_lease: ClaimLease | None = None,
        controller_lease_seconds: float | None = None,
    ) -> AutonomousRunRecord:
        """Replace one durable upper-bound reservation with metered actual usage."""

        if not isinstance(actual, PlanResourceUsage):
            raise TypeError("actual 必须是 PlanResourceUsage")
        await self._assert_controller_current(
            run_id,
            controller_lease,
            controller_lease_seconds,
        )
        for _ in range(20):
            rows = await self._load_rows(run_id)
            if not rows:
                raise AutonomousRunStoreError(f"Autonomous Run 不存在：{run_id}")
            current = self._decode(run_id, rows)
            reservations = dict(current.active_reservations)
            reserved = reservations.get(reservation_id)
            if reserved is None:
                # Idempotent replay after a lost response is accepted only when
                # the exact settle event is already present.
                for row in rows:
                    if (
                        row.event_type == _RESOURCE_SETTLED
                        and row.payload.get("reservationId") == reservation_id
                    ):
                        if PlanResourceUsage.from_dict(
                            row.payload.get("actual")
                        ) != actual:
                            raise AutonomousRunStoreError(
                                "Resource Settlement 重放结果冲突"
                            )
                        return current
                raise AutonomousRunStoreError("Resource Reservation 不存在或已结算")
            if current.status is not None or current.admission_closed:
                raise AutonomousRunStoreError("Autonomous Run 终态后不能结算资源")
            _assert_settlement_within_reservation(actual, reserved)
            payload = {
                "runId": run_id,
                "reservationId": reservation_id,
                "actual": actual.to_dict(),
            }
            spec = self._spec(
                run_id,
                _RESOURCE_SETTLED,
                payload,
                stable_key=f"resource-settled:{reservation_id}",
            )
            try:
                await self._append_events(
                    [spec],
                    run_id=run_id,
                    expected_last_sequence=rows[-1].sequence,
                    controller_lease=controller_lease,
                    controller_lease_seconds=controller_lease_seconds,
                )
            except AutonomousRunControllerLeaseLostError:
                raise
            except JournalConflictError:
                continue
            return await self.load(run_id)
        raise AutonomousRunStoreError("Resource Settlement CAS 冲突")

    async def close_admission(
        self,
        run_id: str,
        *,
        reason: str,
    ) -> AutonomousRunRecord:
        """Durably close a pre-routing ledger that did not become a Plan Run."""

        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Autonomous Admission close reason 不能为空")
        current = await self.load(run_id)
        if current.initial_plan_bound:
            raise AutonomousRunStoreError("已绑定 Plan 的 Run 不能关闭 Admission")
        if current.admission_closed:
            if current.admission_close_reason != reason.strip():
                raise AutonomousRunStoreError("Autonomous Admission 关闭原因冲突")
            return current
        await self._append(
            run_id,
            self._spec(
                run_id,
                _ADMISSION_CLOSED,
                {"runId": run_id, "reason": reason.strip()},
                stable_key="admission-closed",
            ),
        )
        return await self.load(run_id)

    def _require_same_journal(self, plan_store: SessionJournalPlanStore) -> None:
        if (
            not isinstance(plan_store, SessionJournalPlanStore)
            or plan_store.journal is not self.journal
            or plan_store.principal != self.principal
            or plan_store.session_id != self.session_id
        ):
            raise AutonomousRunStoreError(
                "Autonomous Run 与 Plan 必须共享同一事务型 Session Journal"
            )

    async def append_closed_loop_event(
        self,
        run_id: str,
        segment_id: str,
        event: ClosedLoopEvent,
        *,
        controller_lease: ClaimLease | None = None,
        controller_lease_seconds: float | None = None,
    ) -> None:
        payload = {
            "runId": run_id,
            "segmentId": segment_id,
            "event": event.to_dict(),
        }
        await self._append(
            run_id,
            self._spec(
                run_id,
                _LOOP_EVENT,
                payload,
                stable_key=f"segment:{segment_id}:event:{event.sequence}",
            ),
            controller_lease=controller_lease,
            controller_lease_seconds=controller_lease_seconds,
        )

    async def finish_segment(
        self,
        run_id: str,
        segment_id: str,
        status: AutonomousDurableStatus,
        *,
        controller_lease: ClaimLease | None = None,
        controller_lease_seconds: float | None = None,
    ) -> None:
        payload = {"runId": run_id, "segmentId": segment_id, "status": status}
        await self._append(
            run_id,
            self._spec(
                run_id,
                _SEGMENT_FINISHED,
                payload,
                stable_key=f"segment:{segment_id}:finished",
            ),
            controller_lease=controller_lease,
            controller_lease_seconds=controller_lease_seconds,
        )

    async def finalize(
        self,
        run_id: str,
        *,
        status: AutonomousDurableStatus,
        response_text: str,
        pending_approval_ids: tuple[str, ...] = (),
        controller_lease: ClaimLease | None = None,
        controller_lease_seconds: float | None = None,
    ) -> AutonomousRunRecord:
        await self._assert_controller_current(
            run_id,
            controller_lease,
            controller_lease_seconds,
        )
        payload = {
            "runId": run_id,
            "status": status,
            "responseText": response_text,
            "pendingApprovalIds": list(pending_approval_ids),
        }
        current = await self.load(run_id)
        if current.status is not None:
            if (
                current.status != status
                or current.response_text != response_text
                or current.pending_approval_ids != tuple(pending_approval_ids)
            ):
                raise AutonomousRunStoreError(
                    f"Autonomous Run 终态结果冲突：{run_id}"
                )
            return current
        await self._append(
            run_id,
            self._spec(run_id, _RUN_FINALIZED, payload, stable_key="finalized"),
            controller_lease=controller_lease,
            controller_lease_seconds=controller_lease_seconds,
        )
        return await self.load(run_id)

    async def _load_rows(self, run_id: str) -> list[SessionEvent]:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id 不能为空")
        return await self.journal.load_events(
            self.principal,
            session_id=self.session_id,
            operation_id=_stream_id(run_id),
            journal_kind="retry",
        )

    async def _append_events(
        self,
        specs: list[SessionEventSpec],
        *,
        run_id: str | None = None,
        expected_last_sequence: int | None = None,
        expected_stream_sequences: dict[
            tuple[Literal["runtime", "operation", "retry", "audit"], str, str | None],
            int,
        ]
        | None = None,
        controller_lease: ClaimLease | None = None,
        controller_lease_seconds: float | None = None,
    ) -> list[SessionEvent]:
        if controller_lease is None:
            if controller_lease_seconds is not None:
                raise ValueError(
                    "controller_lease_seconds 只能与 controller_lease 一起使用"
                )
            return await self.journal.append_events(
                self.principal,
                specs,
                expected_last_sequence=expected_last_sequence,
                expected_stream_sequences=expected_stream_sequences,
            )
        if run_id is None:
            raise ValueError("Fenced Autonomous Mutation 必须提供目标 run_id")
        self._validate_controller_lease(controller_lease, run_id=run_id)
        if (
            isinstance(controller_lease_seconds, bool)
            or not isinstance(controller_lease_seconds, (int, float))
            or controller_lease_seconds <= 0
        ):
            raise ValueError(
                "Fenced Autonomous Mutation 必须提供正数 controller_lease_seconds"
            )
        try:
            return await self.journal.append_events_if_fenced_claim(
                self.principal,
                specs,
                controller_lease,
                renew_lease_seconds=float(controller_lease_seconds),
                expected_last_sequence=expected_last_sequence,
                expected_stream_sequences=expected_stream_sequences,
            )
        except JournalFencedClaimLostError as error:
            raise AutonomousRunControllerLeaseLostError(
                "Autonomous Run Controller Lease 已失效，禁止提交状态"
            ) from error

    async def _assert_controller_current(
        self,
        run_id: str,
        controller_lease: ClaimLease | None,
        controller_lease_seconds: float | None,
    ) -> None:
        if controller_lease is None:
            if controller_lease_seconds is not None:
                raise ValueError(
                    "controller_lease_seconds 只能与 controller_lease 一起使用"
                )
            return
        self._validate_controller_lease(controller_lease, run_id=run_id)
        if (
            isinstance(controller_lease_seconds, bool)
            or not isinstance(controller_lease_seconds, (int, float))
            or controller_lease_seconds <= 0
        ):
            raise ValueError(
                "Fenced Autonomous Mutation 必须提供正数 controller_lease_seconds"
            )
        if not await self.renew_controller(
            run_id,
            controller_lease,
            lease_seconds=float(controller_lease_seconds),
        ):
            raise AutonomousRunControllerLeaseLostError(
                "Autonomous Run Controller Lease 已失效"
            )

    async def _append(
        self,
        run_id: str,
        spec: SessionEventSpec,
        *,
        controller_lease: ClaimLease | None = None,
        controller_lease_seconds: float | None = None,
    ) -> None:
        await self._assert_controller_current(
            run_id,
            controller_lease,
            controller_lease_seconds,
        )
        for _ in range(20):
            rows = await self._load_rows(run_id)
            expected = rows[-1].sequence if rows else -1
            if any(row.event_id == spec.event_id for row in rows):
                return
            try:
                await self._append_events(
                    [spec],
                    run_id=run_id,
                    expected_last_sequence=expected,
                    controller_lease=controller_lease,
                    controller_lease_seconds=controller_lease_seconds,
                )
            except AutonomousRunControllerLeaseLostError:
                raise
            except JournalConflictError:
                continue
            return
        raise AutonomousRunStoreError("Autonomous Run 事件并发冲突")

    def _spec(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        stable_key: str,
    ) -> SessionEventSpec:
        return SessionEventSpec(
            "retry",
            event_type,
            self.session_id,
            copy.deepcopy(payload),
            operation_id=_stream_id(run_id),
            run_id=run_id,
            event_id=_event_id(
                self.principal.tenant_id,
                self.session_id,
                run_id,
                stable_key,
                payload,
            ),
        )

    def _decode(
        self,
        run_id: str,
        rows: list[SessionEvent],
    ) -> AutonomousRunRecord:
        first = rows[0]
        if first.event_type != _RUN_INITIALIZED:
            raise AutonomousRunStoreError("Autonomous Run 首事件不是 initialized")
        payload = first.payload
        if payload.get("recordType") != "autonomous_run" or payload.get("runId") != run_id:
            raise AutonomousRunStoreError("Autonomous Run 初始化身份损坏")
        request = payload.get("request")
        initial_plan_id = payload.get("initialPlanId")
        budget_payload = payload.get("budget")
        if (
            not isinstance(request, str)
            or not request
            or not isinstance(initial_plan_id, str)
            or not initial_plan_id
            or not isinstance(budget_payload, dict)
        ):
            raise AutonomousRunStoreError("Autonomous Run 初始化 Payload 无效")
        try:
            budget = ClosedLoopBudget(
                max_correction_rounds=budget_payload["maxCorrectionRounds"],
                max_correction_actions=budget_payload["maxCorrectionActions"],
                max_plan_steps=budget_payload.get("maxPlanSteps", 64),
                max_step_attempts=budget_payload.get("maxStepAttempts", 128),
                max_tool_calls=budget_payload.get("maxToolCalls", 128),
                max_duration_seconds=budget_payload.get(
                    "maxDurationSeconds", 900.0
                ),
                max_model_calls=budget_payload.get("maxModelCalls"),
                max_tokens=budget_payload.get("maxTokens"),
                max_cost=budget_payload.get("maxCost"),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise AutonomousRunStoreError("Autonomous Run Budget 无效") from error

        provisional = payload.get("provisional", False)
        if type(provisional) is not bool:
            raise AutonomousRunStoreError("Autonomous Run provisional 标记无效")
        initial_plan_bound = not provisional
        plans = [] if provisional else [initial_plan_id]
        correction_rounds: set[tuple[str, int]] = set()
        correction_actions: set[tuple[str, int]] = set()
        status: AutonomousDurableStatus | None = None
        response_text: str | None = None
        pending: tuple[str, ...] = ()
        operation_id: str | None = None
        linked = False
        dispatchable = False
        raw_started_at = payload.get("startedAt", first.timestamp / 1000)
        raw_deadline_at = payload.get("deadlineAt")
        if (
            isinstance(raw_started_at, bool)
            or not isinstance(raw_started_at, (int, float))
            or raw_started_at <= 0
            or (
                raw_deadline_at is not None
                and (
                    isinstance(raw_deadline_at, bool)
                    or not isinstance(raw_deadline_at, (int, float))
                    or raw_deadline_at <= raw_started_at
                )
            )
        ):
            raise AutonomousRunStoreError("Autonomous Run deadline 损坏")
        started_at = float(raw_started_at)
        deadline_at = (
            None if raw_deadline_at is None else float(raw_deadline_at)
        )
        try:
            settled_usage = PlanResourceUsage.from_dict(
                payload.get("initialUsage", {})
            )
            _assert_usage_within_budget(settled_usage, budget)
        except (TypeError, ValueError, PlanBudgetExceeded) as error:
            raise AutonomousRunStoreError("Autonomous Run Initial Usage 无效") from error
        active_reservations: dict[str, PlanResourceUsage] = {}
        admission_closed = False
        admission_close_reason: str | None = None
        terminal_seen = False
        for row in rows[1:]:
            if row.payload.get("runId") != run_id:
                raise AutonomousRunStoreError("Autonomous Run 事件身份不一致")
            if terminal_seen:
                raise AutonomousRunStoreError("Autonomous Run 终态后仍有事件")
            if row.event_type == _PLAN_REGISTERED:
                plan_id = row.payload.get("planId")
                if not isinstance(plan_id, str) or not plan_id:
                    raise AutonomousRunStoreError("Autonomous Plan 注册事件无效")
                if plan_id not in plans:
                    plans.append(plan_id)
            elif row.event_type == _INITIAL_PLAN_BOUND:
                plan_id = row.payload.get("planId")
                if (
                    not isinstance(plan_id, str)
                    or not plan_id
                    or initial_plan_bound
                    or plans
                ):
                    raise AutonomousRunStoreError(
                        "Autonomous Initial Plan Bind Event 无效"
                    )
                initial_plan_id = plan_id
                plans.append(plan_id)
                initial_plan_bound = True
            elif row.event_type == _CONVERSATION_LINKED:
                value = row.payload.get("operationId")
                if (
                    not isinstance(value, str)
                    or not value
                    or linked
                ):
                    raise AutonomousRunStoreError(
                        "Autonomous Conversation Link Event 无效"
                    )
                operation_id = value
                linked = True
            elif row.event_type == _RUN_DISPATCHABLE:
                if not linked or dispatchable or set(row.payload) != {"runId"}:
                    raise AutonomousRunStoreError(
                        "Autonomous Run Dispatchable Event 无效"
                    )
                dispatchable = True
            elif row.event_type == _RESOURCE_RESERVED:
                reservation_id = row.payload.get("reservationId")
                stage = row.payload.get("stage")
                if (
                    not isinstance(reservation_id, str)
                    or not reservation_id
                    or not isinstance(stage, str)
                    or not stage
                    or reservation_id in active_reservations
                ):
                    raise AutonomousRunStoreError(
                        "Autonomous Resource Reservation Event 无效"
                    )
                try:
                    reserved = PlanResourceUsage.from_dict(
                        row.payload.get("reserved")
                    )
                    _assert_usage_within_budget(
                        settled_usage
                        + _sum_usage(active_reservations.values())
                        + reserved,
                        budget,
                    )
                except (TypeError, ValueError, PlanBudgetExceeded) as error:
                    raise AutonomousRunStoreError(
                        "Autonomous Resource Reservation 超出预算"
                    ) from error
                active_reservations[reservation_id] = reserved
            elif row.event_type == _RESOURCE_SETTLED:
                reservation_id = row.payload.get("reservationId")
                if not isinstance(reservation_id, str) or not reservation_id:
                    raise AutonomousRunStoreError(
                        "Autonomous Resource Settlement Event 无效"
                    )
                if reservation_id not in active_reservations:
                    raise AutonomousRunStoreError(
                        "Autonomous Resource Settlement 缺少 Reservation"
                    )
                reserved = active_reservations.pop(reservation_id)
                try:
                    actual = PlanResourceUsage.from_dict(row.payload.get("actual"))
                    _assert_settlement_within_reservation(actual, reserved)
                except (TypeError, ValueError, PlanBudgetExceeded) as error:
                    raise AutonomousRunStoreError(
                        "Autonomous Resource Settlement 超出 Reservation"
                    ) from error
                settled_usage = settled_usage + actual
            elif row.event_type == _RESOURCE_RELEASED:
                reservation_id = row.payload.get("reservationId")
                reason = row.payload.get("reason")
                if (
                    not isinstance(reservation_id, str)
                    or not reservation_id
                    or not isinstance(reason, str)
                    or not reason
                    or active_reservations.pop(reservation_id, None) is None
                ):
                    raise AutonomousRunStoreError(
                        "Autonomous Resource Release Event 无效"
                    )
            elif row.event_type == _LOOP_EVENT:
                segment_id = row.payload.get("segmentId")
                event = row.payload.get("event")
                if not isinstance(segment_id, str) or not isinstance(event, dict):
                    raise AutonomousRunStoreError("Closed-loop Event Payload 无效")
                event_type = event.get("type")
                round_index = event.get("roundIndex")
                sequence = event.get("sequence")
                if (
                    not isinstance(event_type, str)
                    or isinstance(round_index, bool)
                    or not isinstance(round_index, int)
                    or isinstance(sequence, bool)
                    or not isinstance(sequence, int)
                ):
                    raise AutonomousRunStoreError("Closed-loop Event 字段无效")
                if event_type == "closed_loop_correction_planned":
                    correction_rounds.add((segment_id, sequence))
                if event_type == "closed_loop_action_started" and round_index > 0:
                    correction_actions.add((segment_id, sequence))
            elif row.event_type == _SEGMENT_FINISHED:
                _status(row.payload.get("status"))
            elif row.event_type == _RUN_FINALIZED:
                status = _status(row.payload.get("status"))
                response = row.payload.get("responseText")
                raw_pending = row.payload.get("pendingApprovalIds", [])
                if (
                    not isinstance(response, str)
                    or not response
                    or not isinstance(raw_pending, list)
                    or any(not isinstance(item, str) or not item for item in raw_pending)
                ):
                    raise AutonomousRunStoreError("Autonomous Run 终态 Payload 无效")
                response_text = response
                pending = tuple(raw_pending)
                terminal_seen = True
            elif row.event_type == _ADMISSION_CLOSED:
                reason = row.payload.get("reason")
                if (
                    initial_plan_bound
                    or linked
                    or dispatchable
                    or status is not None
                    or admission_closed
                    or not isinstance(reason, str)
                    or not reason
                    or set(row.payload) != {"runId", "reason"}
                ):
                    raise AutonomousRunStoreError(
                        "Autonomous Admission Closed Event 无效"
                    )
                admission_closed = True
                admission_close_reason = reason
                terminal_seen = True
            else:
                raise AutonomousRunStoreError(
                    f"未知 Autonomous Run 事件：{row.event_type}"
                )
        if (linked or dispatchable or status is not None) and not initial_plan_bound:
            raise AutonomousRunStoreError(
                "Autonomous Run 未绑定 Initial Plan 却进入可执行/终态"
            )
        return AutonomousRunRecord(
            run_id=run_id,
            session_id=self.session_id,
            request=request,
            initial_plan_id=initial_plan_id,
            plan_ids=tuple(plans),
            budget=budget,
            correction_rounds_spent=len(correction_rounds),
            correction_actions_spent=len(correction_actions),
            status=status,
            response_text=response_text,
            pending_approval_ids=pending,
            last_sequence=rows[-1].sequence,
            operation_id=operation_id,
            linked=linked,
            dispatchable=dispatchable,
            started_at=started_at,
            deadline_at=deadline_at,
            settled_usage=settled_usage,
            active_reservations=tuple(sorted(active_reservations.items())),
            initial_plan_bound=initial_plan_bound,
            admission_closed=admission_closed,
            admission_close_reason=admission_close_reason,
        )


@dataclass(frozen=True, slots=True)
class AutonomousConversationLink:
    session_id: str
    operation_id: str
    run_id: str
    plan_id: str


class AutonomousConversationProjector:
    """Exactly-once projection of an autonomous run into Session messages."""

    def __init__(
        self,
        store: OperationEventStore,
        *,
        session_id: str,
        model: Model,
        fenced_lease: ClaimLease | None = None,
        fenced_lease_seconds: float | None = None,
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.model = model
        if fenced_lease is not None:
            if (
                fenced_lease.claim_type != "conversation_session_writer"
                or fenced_lease.resource_id != session_id
                or fenced_lease_seconds is None
                or fenced_lease_seconds <= 0
            ):
                raise ValueError("Autonomous Projector Fenced Lease 配置无效")
        elif fenced_lease_seconds is not None:
            raise ValueError("fenced_lease_seconds 只能与 fenced_lease 一起使用")
        self.fenced_lease = fenced_lease
        self.fenced_lease_seconds = fenced_lease_seconds

    async def begin(
        self,
        request: str,
        *,
        run_id: str,
        plan_id: str,
        initial_messages: list[dict[str, Any]],
    ) -> AutonomousConversationLink:
        existing = await self.find(run_id)
        if existing is not None:
            await self._assert_request(existing, request)
            return existing
        all_events = await self.store.load(session_id=self.session_id)
        for operation_id in {event.operation_id for event in all_events}:
            events = [event for event in all_events if event.operation_id == operation_id]
            state = replay_operation(events)
            marker = state.configuration.get("autonomousTurn")
            if isinstance(marker, dict) and state.phase == "running":
                raise AutonomousRunStoreError(
                    "Session 已有未结束的 Autonomous Turn；请先恢复或人工处理"
                )

        operation_id = _conversation_operation_id(
            self.session_id,
            run_id,
        )
        specs = _conversation_event_specs(
            request,
            run_id=run_id,
            plan_id=plan_id,
            initial_messages=initial_messages,
        )
        try:
            await self._append_batch(
                self.session_id,
                operation_id,
                specs,
                expected_last_sequence=-1,
            )
        except OperationStoreConflictError:
            concurrent = await self.find(run_id)
            if concurrent is None or concurrent.operation_id != operation_id:
                raise AutonomousRunStoreError(
                    "Autonomous Conversation 并发创建冲突"
                )
            await self._assert_request(concurrent, request)
            if concurrent.plan_id != plan_id:
                raise AutonomousRunStoreError(
                    "Autonomous Conversation Plan Link 冲突"
                )
            return concurrent
        return AutonomousConversationLink(
            self.session_id,
            operation_id,
            run_id,
            plan_id,
        )

    async def bootstrap(
        self,
        request: str,
        *,
        run_id: str,
        plan: MultiIntentPlan,
        budget: ClosedLoopBudget,
        initial_messages: list[dict[str, Any]],
        run_store: SessionJournalAutonomousRunStore,
        plan_store: SessionJournalPlanStore,
    ) -> AutonomousConversationLink:
        """Atomically create Plan, Run and Conversation Link as dispatchable.

        The batch crosses three logical streams but commits in one SQLite
        transaction.  Consequently a worker can never observe a runnable Plan
        without the exact durable user turn and run identity that authorized it.
        """

        if not isinstance(self.store, SessionJournalOperationEventStore):
            raise AutonomousRunStoreError(
                "Atomic Autonomous Bootstrap 需要 Session Journal Operation Store"
            )
        run_store._require_same_journal(plan_store)
        if (
            self.store.journal is not run_store.journal
            or self.store.principal != run_store.principal
            or self.session_id != run_store.session_id
        ):
            raise AutonomousRunStoreError(
                "Plan、Run 与 Conversation 必须共享同一事务型 Journal"
            )
        operation_id = _conversation_operation_id(self.session_id, run_id)
        operation_specs = _conversation_event_specs(
            request,
            run_id=run_id,
            plan_id=plan.plan_id,
            initial_messages=initial_messages,
        )
        try:
            existing_run = await run_store.load(run_id)
        except AutonomousRunStoreError:
            existing_run = None
        run_specs: list[SessionEventSpec]
        if existing_run is None:
            run_specs = [
                run_store.initialization_spec(
                    request,
                    plan.plan_id,
                    budget,
                    run_id=run_id,
                    initial_usage=PlanResourceUsage(
                        plan_steps=len(plan.steps)
                    ),
                )
            ]
        elif not existing_run.initial_plan_bound:
            if existing_run.request != request or existing_run.budget != budget:
                raise AutonomousRunStoreError(
                    "Provisional Autonomous Run 与 Bootstrap 请求不一致"
                )
            if (
                existing_run.deadline_at is not None
                and time.time() >= existing_run.deadline_at
            ):
                raise PlanBudgetExceeded(
                    "Autonomous Run wall-clock deadline 在 Bootstrap 前已到"
                )
            if existing_run.resource_usage.plan_steps < len(plan.steps):
                raise AutonomousRunStoreError(
                    "Initial Plan 尚未完成 Durable Step Budget Reservation"
                )
            run_specs = [
                run_store._spec(
                    run_id,
                    _INITIAL_PLAN_BOUND,
                    {"runId": run_id, "planId": plan.plan_id},
                    stable_key="initial-plan-bound",
                )
            ]
        elif existing_run.initial_plan_id == plan.plan_id:
            run_specs = []
        else:
            raise AutonomousRunStoreError(
                "Autonomous Run 已绑定不同 Initial Plan"
            )
        plan_spec = plan_store.initialization_spec(plan, dispatchable=True)
        specs: list[SessionEventSpec] = [
            plan_spec,
            *run_specs,
            run_store._spec(
                run_id,
                _CONVERSATION_LINKED,
                {"runId": run_id, "operationId": operation_id},
                stable_key="conversation-linked",
            ),
            run_store._spec(
                run_id,
                _RUN_DISPATCHABLE,
                {"runId": run_id},
                stable_key="dispatchable",
            ),
            *[
                SessionEventSpec(
                    "operation",
                    event_type,
                    self.session_id,
                    copy.deepcopy(payload),
                    operation_id=operation_id,
                    event_id=_conversation_event_id(
                        run_store.principal.tenant_id,
                        self.session_id,
                        run_id,
                        index,
                        event_type,
                        payload,
                    ),
                )
                for index, (event_type, payload) in enumerate(operation_specs)
            ],
        ]
        expected_stream_sequences: dict[SessionStreamKey, int] = {
            ("retry", self.session_id, plan_spec.operation_id): -1,
            (
                "retry",
                self.session_id,
                _stream_id(run_id),
            ): (-1 if existing_run is None else existing_run.last_sequence),
            ("operation", self.session_id, operation_id): -1,
        }
        try:
            if self.fenced_lease is None:
                await run_store.journal.append_events(
                    run_store.principal,
                    specs,
                    expected_stream_sequences=expected_stream_sequences,
                )
            else:
                assert self.fenced_lease_seconds is not None
                await run_store.journal.append_events_if_fenced_claim(
                    run_store.principal,
                    specs,
                    self.fenced_lease,
                    renew_lease_seconds=self.fenced_lease_seconds,
                    expected_stream_sequences=expected_stream_sequences,
                )
        except (JournalConflictError, JournalFencedClaimLostError) as error:
            # This includes the lost-response-after-commit case.  Return the
            # stable object only after all three projections agree exactly.
            existing = await self.find(run_id)
            try:
                durable = await run_store.load(run_id)
                stored_plan = await plan_store.load(plan.plan_id)
            except Exception:
                raise AutonomousRunStoreError(
                    "Atomic Autonomous Bootstrap 冲突"
                ) from error
            if (
                existing is None
                or existing.operation_id != operation_id
                or existing.plan_id != plan.plan_id
                or durable.operation_id != operation_id
                or not durable.linked
                or not durable.dispatchable
                or stored_plan.plan.to_dict() != plan.to_dict()
                or not stored_plan.dispatchable
            ):
                raise AutonomousRunStoreError(
                    "Atomic Autonomous Bootstrap 冲突"
                ) from error
            await self._assert_request(existing, request)
            return existing
        return AutonomousConversationLink(
            self.session_id,
            operation_id,
            run_id,
            plan.plan_id,
        )

    async def find(self, run_id: str) -> AutonomousConversationLink | None:
        events = await self.store.load(session_id=self.session_id)
        matches: list[AutonomousConversationLink] = []
        grouped: dict[str, list[Any]] = {}
        for event in events:
            grouped.setdefault(event.operation_id, []).append(event)
        for operation_id, operation_events in grouped.items():
            state = replay_operation(operation_events)
            marker = state.configuration.get("autonomousTurn")
            if not isinstance(marker, dict) or marker.get("runId") != run_id:
                continue
            plan_id = marker.get("planId")
            if not isinstance(plan_id, str) or not plan_id:
                raise AutonomousRunStoreError("Autonomous Conversation Link 损坏")
            matches.append(
                AutonomousConversationLink(
                    self.session_id,
                    operation_id,
                    run_id,
                    plan_id,
                )
            )
        if len(matches) > 1:
            raise AutonomousRunStoreError("同一 Autonomous Run 重复投影")
        return matches[0] if matches else None

    async def find_unfinished(self) -> AutonomousConversationLink | None:
        """Return the single unfinished autonomous turn, if one exists."""

        events = await self.store.load(session_id=self.session_id)
        grouped: dict[str, list[Any]] = {}
        for event in events:
            grouped.setdefault(event.operation_id, []).append(event)
        matches: list[AutonomousConversationLink] = []
        for operation_id, operation_events in grouped.items():
            state = replay_operation(operation_events)
            marker = state.configuration.get("autonomousTurn")
            if not isinstance(marker, dict) or state.phase in {
                "completed",
                "failed",
                "cancelled",
            }:
                continue
            run_id = marker.get("runId")
            plan_id = marker.get("planId")
            if (
                not isinstance(run_id, str)
                or not run_id
                or not isinstance(plan_id, str)
                or not plan_id
            ):
                raise AutonomousRunStoreError(
                    "Unfinished Autonomous Conversation Link 损坏"
                )
            matches.append(
                AutonomousConversationLink(
                    self.session_id,
                    operation_id,
                    run_id,
                    plan_id,
                )
            )
        if len(matches) > 1:
            raise AutonomousRunStoreError(
                "Session 存在多个未结束 Autonomous Turn"
            )
        return matches[0] if matches else None

    async def load_user_message(
        self,
        link: AutonomousConversationLink,
    ) -> dict[str, Any]:
        """Return the exact durable user snapshot used by this run."""

        events = await self.store.load(
            session_id=self.session_id,
            operation_id=link.operation_id,
        )
        state = replay_operation(events)
        matched = [
            message
            for message in state.messages
            if message.get("role") == "user"
            and isinstance(message.get("autonomousRun"), dict)
            and message["autonomousRun"].get("runId") == link.run_id
        ]
        if len(matched) != 1:
            raise AutonomousRunStoreError(
                "Autonomous Run 缺少唯一 Durable User Message"
            )
        return copy.deepcopy(matched[0])

    async def finalize(
        self,
        link: AutonomousConversationLink,
        *,
        response_text: str,
        status: AutonomousDurableStatus,
    ) -> dict[str, Any]:
        assistant = assistant_message(
            model=self.model,
            content=[{"type": "text", "text": response_text}],
        )
        assistant["autonomousRun"] = {
            "runId": link.run_id,
            "planId": link.plan_id,
            "status": status,
            "projection": "final",
        }
        for _ in range(20):
            events = await self.store.load(
                session_id=self.session_id,
                operation_id=link.operation_id,
            )
            state = replay_operation(events)
            existing = [
                message
                for message in state.messages
                if message.get("role") == "assistant"
                and isinstance(message.get("autonomousRun"), dict)
                and message["autonomousRun"].get("runId") == link.run_id
                and message["autonomousRun"].get("projection", "final") == "final"
            ]
            if existing:
                if len(existing) != 1 or _message_text(existing[0]) != response_text:
                    raise AutonomousRunStoreError(
                        "Autonomous Assistant 最终投影冲突"
                    )
                if state.phase != "completed":
                    raise AutonomousRunStoreError(
                        "Autonomous Assistant 已存在但 Operation 未原子完成"
                    )
                return copy.deepcopy(existing[0])
            if state.phase != "running":
                raise AutonomousRunStoreError(
                    "Autonomous Operation 已结束但缺少 Assistant 结果"
                )
            specs: list[tuple[str, dict[str, Any]]] = [
                ("message_appended", {"message": copy.deepcopy(assistant)}),
                ("operation_finished", {"outcome": "completed"}),
            ]
            try:
                replay_operation_with_specs(events, specs)
                await self._append_batch(
                    self.session_id,
                    link.operation_id,
                    specs,
                    expected_last_sequence=operation_last_sequence(events),
                )
            except OperationStoreConflictError:
                continue
            except OperationLogInvariantError as error:
                raise AutonomousRunStoreError(
                    f"Autonomous Conversation 无法完成：{error}"
                ) from error
            return assistant
        raise AutonomousRunStoreError("Autonomous Assistant 投影并发冲突")

    async def project_waiting(
        self,
        link: AutonomousConversationLink,
        *,
        response_text: str,
        projection_key: str,
    ) -> dict[str, Any]:
        """Durably expose a waiting state without finishing the conversation.

        Completion outbox acknowledgement is allowed only after this projection
        commits.  The stable key makes a retry after an unknown append outcome
        idempotent while a later approval generation may publish another update.
        """

        if not isinstance(projection_key, str) or not projection_key.strip():
            raise ValueError("Waiting Projection Key 不能为空")
        assistant = assistant_message(
            model=self.model,
            content=[{"type": "text", "text": response_text}],
        )
        assistant["autonomousRun"] = {
            "runId": link.run_id,
            "planId": link.plan_id,
            "status": "waiting_approval",
            "projection": "status",
            "projectionKey": projection_key,
        }
        for _ in range(20):
            events = await self.store.load(
                session_id=self.session_id,
                operation_id=link.operation_id,
            )
            state = replay_operation(events)
            existing = [
                message
                for message in state.messages
                if message.get("role") == "assistant"
                and isinstance(message.get("autonomousRun"), dict)
                and message["autonomousRun"].get("runId") == link.run_id
                and message["autonomousRun"].get("projection") == "status"
                and message["autonomousRun"].get("projectionKey")
                == projection_key
            ]
            if existing:
                if len(existing) != 1 or _message_text(existing[0]) != response_text:
                    raise AutonomousRunStoreError(
                        "Autonomous Waiting Projection 冲突"
                    )
                if state.phase != "running":
                    raise AutonomousRunStoreError(
                        "Waiting Projection 存在但 Operation 已结束"
                    )
                return copy.deepcopy(existing[0])
            if state.phase != "running":
                raise AutonomousRunStoreError(
                    "Autonomous Operation 已结束，不能投影 Waiting 状态"
                )
            specs = [("message_appended", {"message": copy.deepcopy(assistant)})]
            try:
                replay_operation_with_specs(events, specs)
                await self._append_batch(
                    self.session_id,
                    link.operation_id,
                    specs,
                    expected_last_sequence=operation_last_sequence(events),
                )
            except OperationStoreConflictError:
                continue
            except OperationLogInvariantError as error:
                raise AutonomousRunStoreError(
                    f"Autonomous Waiting Projection 无法持久化：{error}"
                ) from error
            return assistant
        raise AutonomousRunStoreError("Autonomous Waiting Projection 并发冲突")

    async def _append_batch(
        self,
        session_id: str,
        operation_id: str,
        specs: list[tuple[str, dict[str, Any]]],
        *,
        expected_last_sequence: int,
    ) -> Any:
        if self.fenced_lease is None:
            return await self.store.append_batch(
                session_id,
                operation_id,
                specs,
                expected_last_sequence=expected_last_sequence,
            )
        append_fenced = getattr(
            self.store,
            "append_batch_if_fenced_claim",
            None,
        )
        if not callable(append_fenced):
            raise RuntimeError(
                "Autonomous Projector 的 Store 不支持原子 Fenced Append"
            )
        return await append_fenced(
            session_id,
            operation_id,
            specs,
            self.fenced_lease,
            renew_lease_seconds=self.fenced_lease_seconds,
            expected_last_sequence=expected_last_sequence,
        )

    async def _assert_request(
        self,
        link: AutonomousConversationLink,
        request: str,
    ) -> None:
        events = await self.store.load(
            session_id=self.session_id,
            operation_id=link.operation_id,
        )
        state = replay_operation(events)
        matched = [
            message
            for message in state.messages
            if message.get("role") == "user"
            and isinstance(message.get("autonomousRun"), dict)
            and message["autonomousRun"].get("runId") == link.run_id
        ]
        if len(matched) != 1 or _message_text(matched[0]) != request:
            raise AutonomousRunStoreError("Autonomous Run 与用户消息绑定冲突")


def _stream_id(run_id: str) -> str:
    return _STREAM_PREFIX + run_id


def _controller_claim_resource(session_id: str, run_id: str) -> str:
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id 不能为空")
    return f"{session_id}:autonomous-run-controller:{run_id}"


def _conversation_operation_id(session_id: str, run_id: str) -> str:
    encoded = json.dumps(
        ["autonomous-conversation", session_id, run_id],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "autonomous-turn-" + hashlib.sha256(encoded).hexdigest()[:32]


def _conversation_event_specs(
    request: str,
    *,
    run_id: str,
    plan_id: str,
    initial_messages: list[dict[str, Any]],
) -> list[tuple[str, dict[str, Any]]]:
    user = user_message(request)
    user["autonomousRun"] = {"runId": run_id, "planId": plan_id}
    return [
        (
            "operation_started",
            {
                "configuration": {
                    "autonomousTurn": {"runId": run_id, "planId": plan_id}
                },
                "tools": [],
            },
        ),
        *[
            (
                "message_appended",
                {"message": copy.deepcopy(message), "initialContext": True},
            )
            for message in initial_messages
        ],
        ("message_appended", {"message": user, "autonomousInput": True}),
    ]


def _conversation_event_id(
    tenant_id: str,
    session_id: str,
    run_id: str,
    index: int,
    event_type: str,
    payload: dict[str, Any],
) -> str:
    encoded = json.dumps(
        [tenant_id, session_id, run_id, index, event_type, payload],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda value: f"<{type(value).__name__}>",
    ).encode("utf-8")
    return "autonomous-conversation-" + hashlib.sha256(encoded).hexdigest()


def _run_id_from_stream(stream_id: str | None) -> str | None:
    if not isinstance(stream_id, str) or not stream_id.startswith(_STREAM_PREFIX):
        return None
    return stream_id.removeprefix(_STREAM_PREFIX)


def _event_id(
    tenant_id: str,
    session_id: str,
    run_id: str,
    stable_key: str,
    payload: dict[str, Any],
) -> str:
    encoded = json.dumps(
        [tenant_id, session_id, run_id, stable_key, payload],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "autonomous-" + hashlib.sha256(encoded).hexdigest()


def _status(value: Any) -> AutonomousDurableStatus:
    if value not in {
        "completed",
        "waiting_approval",
        "failed",
        "manual_intervention",
    }:
        raise AutonomousRunStoreError(f"Autonomous Run Status 无效：{value}")
    return value


def _sum_usage(values: Any) -> PlanResourceUsage:
    total = PlanResourceUsage()
    for value in values:
        if not isinstance(value, PlanResourceUsage):
            raise TypeError("Resource Usage 集合包含无效值")
        total = total + value
    return total


def _assert_usage_within_budget(
    usage: PlanResourceUsage,
    budget: ClosedLoopBudget,
) -> None:
    checks = (
        ("plan_steps", usage.plan_steps, budget.max_plan_steps),
        ("step_attempts", usage.step_attempts, budget.max_step_attempts),
        ("tool_calls", usage.tool_calls, budget.max_tool_calls),
        ("model_calls", usage.model_calls, budget.max_model_calls),
        ("tokens", usage.tokens, budget.max_tokens),
        ("cost", usage.cost, budget.max_cost),
    )
    for name, actual, limit in checks:
        if limit is not None and actual > limit:
            raise PlanBudgetExceeded(
                f"Autonomous Run {name} 预算不足：{actual} > {limit}"
            )


def _assert_settlement_within_reservation(
    actual: PlanResourceUsage,
    reserved: PlanResourceUsage,
) -> None:
    for name in (
        "plan_steps",
        "step_attempts",
        "tool_calls",
        "model_calls",
        "tokens",
        "cost",
    ):
        if getattr(actual, name) > getattr(reserved, name) + 1e-12:
            raise PlanBudgetExceeded(
                f"Usage Settlement {name} 超出预留上界"
            )


def _message_text(message: dict[str, Any]) -> str:
    return "".join(
        str(part.get("text", ""))
        for part in message.get("content", [])
        if isinstance(part, dict) and part.get("type") == "text"
    )


__all__ = [
    "AutonomousConversationLink",
    "AutonomousConversationProjector",
    "AutonomousDurableStatus",
    "AutonomousRunRecord",
    "AutonomousRunControllerConflictError",
    "AutonomousRunControllerLeaseLostError",
    "AutonomousRunStoreError",
    "SessionJournalAutonomousRunStore",
]
