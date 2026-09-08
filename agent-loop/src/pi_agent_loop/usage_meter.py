"""A usable PlanUsageMeter backed by actual physical Runtime attempts."""

from __future__ import annotations

from typing import Any

from .model_attempts import (
    ModelAttemptAdmissionSnapshot,
    current_model_attempt_admission_scope,
)
from .planning.resources import (
    PlanBudgetExceeded,
    PlanResourceUsage,
    PlanUsageReservation,
)


class RuntimeUsageMeter:
    """Per-stage ceilings inside the runner's durable, aggregate reservation.

    Token/cost limits are enforced by the injected Provider admission adapter.
    Unsupported hard ceilings are rejected by that adapter before dispatch.
    Missing usage keeps the durable reservation outstanding; it is never zero.
    """

    def __init__(
        self,
        *,
        per_stage: PlanResourceUsage = PlanResourceUsage(
            model_calls=4, tokens=100_000, cost=10
        ),
    ) -> None:
        if per_stage.model_calls < 1:
            raise ValueError("per-stage budget requires at least one model attempt")
        self.per_stage = per_stage
        self._snapshots: dict[str, ModelAttemptAdmissionSnapshot] = {}

    def reserve(
        self,
        *,
        run_id: str,
        reservation_id: str,
        stage: str,
        remaining: PlanResourceUsage,
    ) -> PlanUsageReservation:
        if remaining.model_calls < 1:
            raise PlanBudgetExceeded("model-call budget exhausted")
        return PlanUsageReservation(
            reservation_id,
            stage,
            PlanResourceUsage(
                model_calls=min(self.per_stage.model_calls, remaining.model_calls),
                tokens=min(self.per_stage.tokens, remaining.tokens),
                cost=min(self.per_stage.cost, remaining.cost),
            ),
        )

    async def dispatch(self, reservation: PlanUsageReservation, callback: Any) -> Any:
        scope = current_model_attempt_admission_scope()
        if scope is None or scope.reservation_id != reservation.reservation_id:
            raise PlanBudgetExceeded(
                "meter requires the runner's physical admission scope"
            )
        try:
            return await callback()
        finally:
            self._snapshots[reservation.reservation_id] = await scope.snapshot()

    def settle(self, reservation: PlanUsageReservation) -> PlanResourceUsage:
        snapshot = self._snapshots.pop(reservation.reservation_id, None)
        if snapshot is None or snapshot.unknown_attempts or snapshot.in_flight_attempts:
            raise PlanBudgetExceeded(
                "model usage is unknown; retain the durable reservation"
            )
        return PlanResourceUsage(
            model_calls=snapshot.model_calls, tokens=snapshot.tokens, cost=snapshot.cost
        )

    def cancel(self, reservation: PlanUsageReservation) -> None:
        snapshot = self._snapshots.get(reservation.reservation_id)
        if snapshot is not None and (
            snapshot.model_calls
            or snapshot.unknown_attempts
            or snapshot.in_flight_attempts
        ):
            raise PlanBudgetExceeded("a dispatched reservation cannot be released")
        self._snapshots.pop(reservation.reservation_id, None)
