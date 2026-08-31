"""Durable resource-budget value objects for autonomous plan execution."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping, Protocol

if TYPE_CHECKING:
    from .types import PlanStep


class PlanBudgetExceeded(RuntimeError):
    """A durable reservation would exceed the configured run budget."""


@dataclass(frozen=True, slots=True)
class PlanResourceUsage:
    """Strict non-negative usage delta or cumulative usage snapshot.

    ``tokens`` is deliberately provider-neutral (input + output + any billed
    reasoning tokens according to the application's meter). ``cost`` uses the
    application's configured billing currency; a run must use one currency.
    """

    plan_steps: int = 0
    step_attempts: int = 0
    tool_calls: int = 0
    model_calls: int = 0
    tokens: int = 0
    cost: float = 0.0

    def __post_init__(self) -> None:
        for name in (
            "plan_steps",
            "step_attempts",
            "tool_calls",
            "model_calls",
            "tokens",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数")
        if (
            isinstance(self.cost, bool)
            or not isinstance(self.cost, (int, float))
            or not math.isfinite(float(self.cost))
            or self.cost < 0
        ):
            raise ValueError("cost 必须是有限非负数字")
        object.__setattr__(self, "cost", float(self.cost))

    def __add__(self, other: object) -> "PlanResourceUsage":
        if not isinstance(other, PlanResourceUsage):
            return NotImplemented
        return PlanResourceUsage(
            plan_steps=self.plan_steps + other.plan_steps,
            step_attempts=self.step_attempts + other.step_attempts,
            tool_calls=self.tool_calls + other.tool_calls,
            model_calls=self.model_calls + other.model_calls,
            tokens=self.tokens + other.tokens,
            cost=self.cost + other.cost,
        )

    def subtract(self, earlier: "PlanResourceUsage") -> "PlanResourceUsage":
        """Return a monotonic meter delta, rejecting reset/counter rollback."""

        if not isinstance(earlier, PlanResourceUsage):
            raise TypeError("earlier 必须是 PlanResourceUsage")
        plan_steps = self.plan_steps - earlier.plan_steps
        step_attempts = self.step_attempts - earlier.step_attempts
        tool_calls = self.tool_calls - earlier.tool_calls
        model_calls = self.model_calls - earlier.model_calls
        tokens = self.tokens - earlier.tokens
        cost = self.cost - earlier.cost
        if (
            plan_steps < 0
            or step_attempts < 0
            or tool_calls < 0
            or model_calls < 0
            or tokens < 0
            or cost < -1e-12
        ):
            raise ValueError("Usage Meter 计数器发生回退")
        return PlanResourceUsage(
            plan_steps=plan_steps,
            step_attempts=step_attempts,
            tool_calls=tool_calls,
            model_calls=model_calls,
            tokens=tokens,
            cost=max(0.0, cost),
        )

    def to_dict(self) -> dict[str, int | float]:
        return {
            "planSteps": self.plan_steps,
            "stepAttempts": self.step_attempts,
            "toolCalls": self.tool_calls,
            "modelCalls": self.model_calls,
            "tokens": self.tokens,
            "cost": self.cost,
        }

    @classmethod
    def from_dict(cls, value: object) -> "PlanResourceUsage":
        if not isinstance(value, dict):
            raise ValueError("Plan Resource Usage 必须是对象")
        allowed = {
            "planSteps",
            "stepAttempts",
            "toolCalls",
            "modelCalls",
            "tokens",
            "cost",
        }
        if set(value) - allowed:
            raise ValueError("Plan Resource Usage 包含未知字段")
        return cls(
            plan_steps=value.get("planSteps", 0),  # type: ignore[arg-type]
            step_attempts=value.get("stepAttempts", 0),  # type: ignore[arg-type]
            tool_calls=value.get("toolCalls", 0),  # type: ignore[arg-type]
            model_calls=value.get("modelCalls", 0),  # type: ignore[arg-type]
            tokens=value.get("tokens", 0),  # type: ignore[arg-type]
            cost=value.get("cost", 0.0),  # type: ignore[arg-type]
        )


class PlanUsageMeter(Protocol):
    """Admission/settlement boundary around opaque model callbacks.

    ``reserve`` must refuse a call it cannot keep within ``remaining``.  The
    returned ticket states the hard upper bound reserved before dispatch.
    ``settle`` returns actual usage and must never silently invent zero usage.

    One reservation represents one model stage but reserves the upper bound for
    its *entire* retry tree. ``model_calls`` is the number of physical Provider
    attempts, including failed attempts. ``dispatch`` must apply the ticket's
    token/cost ceiling to each real Provider dispatch, while ``settle`` aggregates
    every attempt. An adapter that caps or reports only the final attempt is not
    a valid hard-budget meter.
    """

    def reserve(
        self,
        *,
        run_id: str,
        reservation_id: str,
        stage: str,
        remaining: PlanResourceUsage,
    ) -> "PlanUsageReservation | Awaitable[PlanUsageReservation]": ...

    def settle(
        self,
        reservation: "PlanUsageReservation",
    ) -> PlanResourceUsage | Awaitable[PlanResourceUsage]: ...

    def cancel(
        self,
        reservation: "PlanUsageReservation",
    ) -> Any | Awaitable[Any]: ...

    def dispatch(
        self,
        reservation: "PlanUsageReservation",
        callback: Callable[[], Any | Awaitable[Any]],
    ) -> Any | Awaitable[Any]:
        """Enforce the reservation across callback and every nested retry."""
        ...


@dataclass(frozen=True, slots=True)
class PlanUsageReservation:
    reservation_id: str
    stage: str
    reserved: PlanResourceUsage
    metadata: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reservation_id, str) or not self.reservation_id.strip():
            raise ValueError("usage reservation_id 不能为空")
        if not isinstance(self.stage, str) or not self.stage.strip():
            raise ValueError("usage reservation stage 不能为空")
        if not isinstance(self.reserved, PlanResourceUsage):
            raise TypeError("usage reserved 必须是 PlanResourceUsage")
        if self.reserved.model_calls < 1:
            raise ValueError("模型调用 Reservation 至少预留 1 次 model_call")
        if self.metadata is not None:
            # Keep this value opaque but safely immutable/serializable.
            import copy
            import json

            value = copy.deepcopy(dict(self.metadata))
            try:
                json.dumps(value, allow_nan=False)
            except (TypeError, ValueError) as error:
                raise ValueError("usage reservation metadata 必须是严格 JSON") from error
            object.__setattr__(self, "metadata", value)

class PlanStepResourceReserver(Protocol):
    def __call__(
        self,
        step: "PlanStep",
        step_attempt: int,
        tool_attempt: int | None,
    ) -> Any | Awaitable[Any]: ...


__all__ = [
    "PlanBudgetExceeded",
    "PlanResourceUsage",
    "PlanStepResourceReserver",
    "PlanUsageMeter",
    "PlanUsageReservation",
]
