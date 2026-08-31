"""Trusted execute-validate-correct orchestration.

The controller in this module is intentionally independent from ``PlanExecutor``.
Applications can wrap a plan step, a tool dispatch or another domain operation in
the same closed-loop boundary without teaching the generic planner business rules.

Only the application supplied ``ResultValidator`` and ``CorrectionPlanner`` may
decide whether a result is acceptable and which safe correction to run.  The
controller itself owns the non-bypassable safety rules: write/never-replay actions
are executed at most once, outcome-unknown is terminal manual intervention, and
all correction work is bounded.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeAlias, cast

from ..cancellation import CancellationToken, OperationCancelledError
from ..messages import public_error_message
from ..retry.errors import OutcomeUnknownToolError

ClosedLoopReplayPolicy: TypeAlias = Literal["safe", "never"]
ClosedLoopStatus: TypeAlias = Literal[
    "completed",
    "failed",
    "manual_intervention",
    "suspended",
]
ValidationStatus: TypeAlias = Literal[
    "valid",
    "invalid",
    "outcome_unknown",
    "suspended",
]


class ClosedLoopValidationError(ValueError):
    """Raised for invalid trusted closed-loop configuration or callback output."""


@dataclass(frozen=True, slots=True)
class ClosedLoopAction:
    """One initial or corrective action.

    ``write`` is independent metadata rather than something inferred from the
    payload.  A write must be ``never`` replay.  The controller permits an initial
    write because an enclosing approval/write coordinator may already have
    authorised it, but it never automatically executes a write correction.
    """

    action_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    replay_policy: ClosedLoopReplayPolicy = "safe"
    write: bool = False

    def __post_init__(self) -> None:
        _non_empty(self.action_id, "action_id")
        if self.replay_policy not in {"safe", "never"}:
            raise ClosedLoopValidationError("replay_policy must be safe or never")
        if not isinstance(self.write, bool):
            raise ClosedLoopValidationError("write must be a boolean")
        if self.write and self.replay_policy != "never":
            raise ClosedLoopValidationError("write actions must use never replay")
        payload = copy.deepcopy(dict(self.payload))
        _strict_json(payload, "action payload")
        object.__setattr__(self, "payload", payload)

    @property
    def automatic_correction_safe(self) -> bool:
        return not self.write and self.replay_policy == "safe"


@dataclass(frozen=True, slots=True)
class ResultValidation:
    """A trusted validator decision, never a model self-reported confidence."""

    status: ValidationStatus
    issues: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in {"valid", "invalid", "outcome_unknown", "suspended"}:
            raise ClosedLoopValidationError("validation status is invalid")
        issues = tuple(self.issues)
        if any(not isinstance(item, str) or not item.strip() for item in issues):
            raise ClosedLoopValidationError("validation issues must be non-empty strings")
        if self.status == "valid" and issues:
            raise ClosedLoopValidationError("valid validation cannot contain issues")
        if self.status != "valid" and not issues:
            raise ClosedLoopValidationError(
                "invalid/outcome_unknown/suspended validation must contain an issue"
            )
        details = copy.deepcopy(dict(self.details))
        _strict_json(details, "validation details")
        object.__setattr__(self, "issues", issues)
        object.__setattr__(self, "details", details)

    @classmethod
    def valid(cls, *, details: Mapping[str, Any] | None = None) -> "ResultValidation":
        return cls("valid", details={} if details is None else details)

    @classmethod
    def invalid(
        cls,
        *issues: str,
        details: Mapping[str, Any] | None = None,
    ) -> "ResultValidation":
        return cls("invalid", tuple(issues), {} if details is None else details)

    @classmethod
    def unknown(
        cls,
        reason: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> "ResultValidation":
        return cls(
            "outcome_unknown",
            (reason,),
            {} if details is None else details,
        )

    @classmethod
    def suspended(
        cls,
        reason: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> "ResultValidation":
        """Stop safely for an externally resumable condition such as approval."""

        return cls(
            "suspended",
            (reason,),
            {} if details is None else details,
        )


@dataclass(frozen=True, slots=True)
class CorrectionPlan:
    """Corrections proposed by the trusted replanner for one round."""

    actions: tuple[ClosedLoopAction, ...]
    reason: str

    def __post_init__(self) -> None:
        _non_empty(self.reason, "correction reason")
        actions = tuple(self.actions)
        identifiers = [item.action_id for item in actions]
        if len(identifiers) != len(set(identifiers)):
            raise ClosedLoopValidationError(
                "correction action_id values must be unique within one round"
            )
        object.__setattr__(self, "actions", actions)


@dataclass(frozen=True, slots=True)
class ClosedLoopBudget:
    max_correction_rounds: int = 2
    max_correction_actions: int = 4
    max_plan_steps: int = 64
    max_step_attempts: int = 128
    max_tool_calls: int = 128
    max_duration_seconds: float | None = 900.0
    max_model_calls: int | None = None
    max_tokens: int | None = None
    max_cost: float | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("max_correction_rounds", self.max_correction_rounds),
            ("max_correction_actions", self.max_correction_actions),
            ("max_plan_steps", self.max_plan_steps),
            ("max_step_attempts", self.max_step_attempts),
            ("max_tool_calls", self.max_tool_calls),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ClosedLoopValidationError(f"{name} must be a non-negative integer")
        for name, optional_count in (
            ("max_model_calls", self.max_model_calls),
            ("max_tokens", self.max_tokens),
        ):
            if optional_count is not None and (
                isinstance(optional_count, bool)
                or not isinstance(optional_count, int)
                or optional_count < 0
            ):
                raise ClosedLoopValidationError(
                    f"{name} must be a non-negative integer or None"
                )
        if self.max_duration_seconds is not None and (
            isinstance(self.max_duration_seconds, bool)
            or not isinstance(self.max_duration_seconds, (int, float))
            or not math.isfinite(float(self.max_duration_seconds))
            or self.max_duration_seconds <= 0
        ):
            raise ClosedLoopValidationError(
                "max_duration_seconds must be a finite positive number or None"
            )
        if self.max_cost is not None and (
            isinstance(self.max_cost, bool)
            or not isinstance(self.max_cost, (int, float))
            or not math.isfinite(float(self.max_cost))
            or self.max_cost < 0
        ):
            raise ClosedLoopValidationError(
                "max_cost must be a finite non-negative number or None"
            )


@dataclass(frozen=True, slots=True)
class ClosedLoopAttempt:
    action: ClosedLoopAction
    round_index: int
    result: Any = None
    error: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.round_index, bool)
            or not isinstance(self.round_index, int)
            or self.round_index < 0
        ):
            raise ClosedLoopValidationError("round_index must be a non-negative integer")
        if self.error is not None and (
            not isinstance(self.error, str) or not self.error.strip()
        ):
            raise ClosedLoopValidationError("attempt error must be non-empty or None")


@dataclass(frozen=True, slots=True)
class ClosedLoopObservation:
    """Immutable-by-convention snapshot supplied to trusted callbacks."""

    initial_action: ClosedLoopAction
    attempts: tuple[ClosedLoopAttempt, ...]
    validations: tuple[ResultValidation, ...]
    correction_rounds: int

    @property
    def latest_attempt(self) -> ClosedLoopAttempt:
        if not self.attempts:
            raise ClosedLoopValidationError("closed-loop observation has no attempt")
        return self.attempts[-1]


@dataclass(frozen=True, slots=True)
class ClosedLoopEvent:
    sequence: int
    type: str
    round_index: int
    action_id: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            raise ClosedLoopValidationError("event sequence must be a positive integer")
        _non_empty(self.type, "event type")
        if (
            isinstance(self.round_index, bool)
            or not isinstance(self.round_index, int)
            or self.round_index < 0
        ):
            raise ClosedLoopValidationError("event round_index must be non-negative")
        if self.action_id is not None:
            _non_empty(self.action_id, "event action_id")
        data = copy.deepcopy(dict(self.data))
        _strict_json(data, "event data")
        object.__setattr__(self, "data", data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "type": self.type,
            "roundIndex": self.round_index,
            "actionId": self.action_id,
            "data": copy.deepcopy(dict(self.data)),
        }


@dataclass(frozen=True, slots=True)
class ClosedLoopResult:
    status: ClosedLoopStatus
    attempts: tuple[ClosedLoopAttempt, ...]
    validations: tuple[ResultValidation, ...]
    events: tuple[ClosedLoopEvent, ...]
    correction_rounds: int
    correction_actions: int
    reason: str | None = None
    event_sink_errors: tuple[str, ...] = ()


class ResultValidator(Protocol):
    """Trusted application validator; do not implement with model self-report alone."""

    def __call__(
        self,
        observation: ClosedLoopObservation,
        cancellation: CancellationToken,
    ) -> ResultValidation | Awaitable[ResultValidation]: ...


class CorrectionPlanner(Protocol):
    """Trusted replanner that may propose only bounded, safe corrections."""

    def __call__(
        self,
        observation: ClosedLoopObservation,
        validation: ResultValidation,
        cancellation: CancellationToken,
    ) -> CorrectionPlan | None | Awaitable[CorrectionPlan | None]: ...


# Replanner is the commonly used product name for the same trusted boundary.
Replanner = CorrectionPlanner

ClosedLoopActionExecutor: TypeAlias = Callable[
    [ClosedLoopAction, CancellationToken],
    Any,
]
ClosedLoopEventSink: TypeAlias = Callable[[ClosedLoopEvent], Any]


class ClosedLoopExecutor:
    """Run one initial action and bounded safe correction rounds."""

    def __init__(
        self,
        action_executor: ClosedLoopActionExecutor,
        result_validator: ResultValidator,
        correction_planner: CorrectionPlanner,
        *,
        budget: ClosedLoopBudget | None = None,
        event_sink: ClosedLoopEventSink | None = None,
    ) -> None:
        if not callable(action_executor):
            raise TypeError("action_executor must be callable")
        if not callable(result_validator):
            raise TypeError("result_validator must be callable")
        if not callable(correction_planner):
            raise TypeError("correction_planner must be callable")
        if budget is not None and not isinstance(budget, ClosedLoopBudget):
            raise TypeError("budget must be ClosedLoopBudget or None")
        if event_sink is not None and not callable(event_sink):
            raise TypeError("event_sink must be callable or None")
        self.action_executor = action_executor
        self.result_validator = result_validator
        self.correction_planner = correction_planner
        self.budget = budget or ClosedLoopBudget()
        self.event_sink = event_sink
        self._run_lock = asyncio.Lock()

    async def execute(
        self,
        initial_action: ClosedLoopAction,
        *,
        cancellation: CancellationToken | None = None,
    ) -> ClosedLoopResult:
        if not isinstance(initial_action, ClosedLoopAction):
            raise TypeError("initial_action must be ClosedLoopAction")
        token = cancellation or CancellationToken()
        async with self._run_lock:
            return await self._execute_locked(initial_action, token)

    async def _execute_locked(
        self,
        initial_action: ClosedLoopAction,
        token: CancellationToken,
    ) -> ClosedLoopResult:
        attempts: list[ClosedLoopAttempt] = []
        validations: list[ResultValidation] = []
        events: list[ClosedLoopEvent] = []
        event_sink_errors: list[str] = []
        correction_rounds = 0
        correction_actions = 0

        async def emit(
            event_type: str,
            *,
            round_index: int,
            action_id: str | None = None,
            data: Mapping[str, Any] | None = None,
        ) -> None:
            event = ClosedLoopEvent(
                sequence=len(events) + 1,
                type=event_type,
                round_index=round_index,
                action_id=action_id,
                data={} if data is None else data,
            )
            events.append(event)
            if self.event_sink is not None:
                try:
                    value = self.event_sink(copy.deepcopy(event))
                    if inspect.isawaitable(value):
                        await cast(Awaitable[Any], value)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    # The optional sink is an observer, not the transaction log.
                    # A UI/telemetry failure must never rewrite a completed side
                    # effect. Durable callers should persist the returned ordered
                    # events transactionally at their own storage boundary.
                    event_sink_errors.append(
                        f"{event.type}: "
                        + _public_exception_summary(
                            error,
                            fallback="Closed-loop event sink failed",
                        )
                    )

        def finish(status: ClosedLoopStatus, reason: str | None = None) -> ClosedLoopResult:
            return ClosedLoopResult(
                status=status,
                attempts=tuple(copy.deepcopy(attempts)),
                validations=tuple(copy.deepcopy(validations)),
                events=tuple(copy.deepcopy(events)),
                correction_rounds=correction_rounds,
                correction_actions=correction_actions,
                reason=reason,
                event_sink_errors=tuple(event_sink_errors),
            )

        async def manual(
            reason: str,
            *,
            round_index: int,
            action_id: str | None,
        ) -> ClosedLoopResult:
            await emit(
                "closed_loop_manual_intervention",
                round_index=round_index,
                action_id=action_id,
                data={"reason": reason},
            )
            return finish("manual_intervention", reason)

        async def failed(reason: str, *, round_index: int) -> ClosedLoopResult:
            await emit(
                "closed_loop_failed",
                round_index=round_index,
                data={"reason": reason},
            )
            return finish("failed", reason)

        async def run_action(
            action: ClosedLoopAction,
            round_index: int,
        ) -> tuple[ClosedLoopAttempt, str | None]:
            token.throw_if_cancelled()
            await emit(
                "closed_loop_action_started",
                round_index=round_index,
                action_id=action.action_id,
                data={
                    "write": action.write,
                    "replayPolicy": action.replay_policy,
                },
            )
            try:
                value = await _invoke_cancellable(
                    self.action_executor,
                    action,
                    token,
                    cancellation=token,
                )
            except OperationCancelledError:
                if not action.automatic_correction_safe:
                    reason = (
                        f"unsafe action {action.action_id} was cancelled after dispatch; "
                        "manual reconciliation is required"
                    )
                    attempt = ClosedLoopAttempt(
                        action=action,
                        round_index=round_index,
                        error=reason,
                    )
                    attempts.append(attempt)
                    await emit(
                        "closed_loop_action_outcome_unknown",
                        round_index=round_index,
                        action_id=action.action_id,
                        data={"reason": reason},
                    )
                    return attempt, reason
                raise
            except asyncio.CancelledError:
                raise
            except Exception as error:
                error_type = type(error).__name__
                unknown = _is_outcome_unknown(error)
                public_reason = public_error_message(
                    error,
                    fallback=(
                        "Closed-loop action outcome is unknown"
                        if unknown or not action.automatic_correction_safe
                        else "Closed-loop action failed"
                    ),
                )
                reason = f"{public_reason} [{error_type}]"
                if unknown or not action.automatic_correction_safe:
                    reason = (
                        reason
                        if unknown
                        else (
                            f"unsafe action {action.action_id} failed after dispatch; "
                            f"manual reconciliation is required: {reason}"
                        )
                    )
                    attempt = ClosedLoopAttempt(
                        action=action,
                        round_index=round_index,
                        error=reason,
                    )
                    attempts.append(attempt)
                    await emit(
                        "closed_loop_action_outcome_unknown",
                        round_index=round_index,
                        action_id=action.action_id,
                        data={
                            "reason": reason,
                            "errorCode": "closed_loop_action_outcome_unknown",
                            "errorType": error_type,
                        },
                    )
                    return attempt, reason
                attempt = ClosedLoopAttempt(
                    action=action,
                    round_index=round_index,
                    error=reason,
                )
                attempts.append(attempt)
                await emit(
                    "closed_loop_action_failed",
                    round_index=round_index,
                    action_id=action.action_id,
                    data={
                        "error": reason,
                        "errorCode": "closed_loop_action_failed",
                        "errorType": error_type,
                    },
                )
                return attempt, None

            attempt = ClosedLoopAttempt(
                action=action,
                round_index=round_index,
                result=copy.deepcopy(value),
            )
            attempts.append(attempt)
            if _is_outcome_unknown(value):
                reason = f"action {action.action_id} returned outcome_unknown"
                await emit(
                    "closed_loop_action_outcome_unknown",
                    round_index=round_index,
                    action_id=action.action_id,
                    data={"reason": reason},
                )
                return attempt, reason
            await emit(
                "closed_loop_action_succeeded",
                round_index=round_index,
                action_id=action.action_id,
                data={"resultType": type(value).__name__},
            )
            return attempt, None

        try:
            token.throw_if_cancelled()
            await emit(
                "closed_loop_started",
                round_index=0,
                action_id=initial_action.action_id,
                data={
                    "maxCorrectionRounds": self.budget.max_correction_rounds,
                    "maxCorrectionActions": self.budget.max_correction_actions,
                },
            )
            _initial_attempt, unsafe_reason = await run_action(initial_action, 0)
            if unsafe_reason is not None:
                return await manual(
                    unsafe_reason,
                    round_index=0,
                    action_id=initial_action.action_id,
                )

            while True:
                token.throw_if_cancelled()
                observation = _observation(
                    initial_action,
                    attempts,
                    validations,
                    correction_rounds,
                )
                await emit(
                    "closed_loop_validation_started",
                    round_index=correction_rounds,
                    action_id=observation.latest_attempt.action.action_id,
                )
                try:
                    validation_value = await _invoke_cancellable(
                        self.result_validator,
                        observation,
                        token,
                        cancellation=token,
                    )
                except (asyncio.CancelledError, OperationCancelledError):
                    raise
                except Exception as error:
                    reason = _public_exception_summary(
                        error,
                        fallback="Result validator failed",
                    )
                    if not initial_action.automatic_correction_safe:
                        return await manual(
                            reason,
                            round_index=correction_rounds,
                            action_id=initial_action.action_id,
                        )
                    return await failed(reason, round_index=correction_rounds)
                if not isinstance(validation_value, ResultValidation):
                    reason = (
                        "trusted result validator returned an invalid contract value"
                    )
                    if not initial_action.automatic_correction_safe:
                        return await manual(
                            reason,
                            round_index=correction_rounds,
                            action_id=initial_action.action_id,
                        )
                    return await failed(reason, round_index=correction_rounds)
                validation = copy.deepcopy(validation_value)
                validations.append(validation)
                await emit(
                    "closed_loop_validation_finished",
                    round_index=correction_rounds,
                    action_id=observation.latest_attempt.action.action_id,
                    data={
                        "status": validation.status,
                        "issues": list(validation.issues),
                    },
                )

                if validation.status == "outcome_unknown":
                    return await manual(
                        validation.issues[0],
                        round_index=correction_rounds,
                        action_id=observation.latest_attempt.action.action_id,
                    )
                if validation.status == "suspended":
                    reason = validation.issues[0]
                    await emit(
                        "closed_loop_suspended",
                        round_index=correction_rounds,
                        action_id=observation.latest_attempt.action.action_id,
                        data={"reason": reason},
                    )
                    return finish("suspended", reason)
                if validation.status == "valid":
                    await emit(
                        "closed_loop_completed",
                        round_index=correction_rounds,
                        action_id=observation.latest_attempt.action.action_id,
                    )
                    return finish("completed")

                if not initial_action.automatic_correction_safe:
                    return await manual(
                        "write/never-replay initial action failed validation; "
                        "automatic correction is prohibited",
                        round_index=correction_rounds,
                        action_id=initial_action.action_id,
                    )
                if correction_rounds >= self.budget.max_correction_rounds:
                    await emit(
                        "closed_loop_budget_exhausted",
                        round_index=correction_rounds,
                        data={"budget": "correction_rounds"},
                    )
                    return await failed(
                        "maximum correction rounds exhausted",
                        round_index=correction_rounds,
                    )

                observation = _observation(
                    initial_action,
                    attempts,
                    validations,
                    correction_rounds,
                )
                try:
                    plan_value = await _invoke_cancellable(
                        self.correction_planner,
                        observation,
                        validation,
                        token,
                        cancellation=token,
                    )
                except (asyncio.CancelledError, OperationCancelledError):
                    raise
                except Exception as error:
                    return await failed(
                        _public_exception_summary(
                            error,
                            fallback="Correction planner failed",
                        ),
                        round_index=correction_rounds,
                    )
                if plan_value is None:
                    return await failed(
                        "correction planner produced no plan",
                        round_index=correction_rounds,
                    )
                if not isinstance(plan_value, CorrectionPlan):
                    return await failed(
                        "trusted correction planner returned an invalid contract value",
                        round_index=correction_rounds,
                    )
                plan = copy.deepcopy(plan_value)
                if not plan.actions:
                    return await failed(
                        "correction plan contains no actions",
                        round_index=correction_rounds,
                    )

                unsafe = next(
                    (
                        action
                        for action in plan.actions
                        if not action.automatic_correction_safe
                    ),
                    None,
                )
                if unsafe is not None:
                    await emit(
                        "closed_loop_correction_rejected",
                        round_index=correction_rounds + 1,
                        action_id=unsafe.action_id,
                        data={
                            "reason": "write_or_never_replay_correction",
                            "plannedActionCount": len(plan.actions),
                            "executedActionCount": 0,
                        },
                    )
                    return await manual(
                        "correction plan contains a write/never-replay action; "
                        "the entire correction batch was not executed",
                        round_index=correction_rounds + 1,
                        action_id=unsafe.action_id,
                    )

                remaining_actions = (
                    self.budget.max_correction_actions - correction_actions
                )
                if len(plan.actions) > remaining_actions:
                    await emit(
                        "closed_loop_budget_exhausted",
                        round_index=correction_rounds,
                        data={
                            "budget": "correction_actions",
                            "remaining": remaining_actions,
                            "requested": len(plan.actions),
                        },
                    )
                    return await failed(
                        "maximum correction action budget exhausted",
                        round_index=correction_rounds,
                    )

                correction_rounds += 1
                await emit(
                    "closed_loop_correction_planned",
                    round_index=correction_rounds,
                    data={
                        "reason": plan.reason,
                        "actionIds": [action.action_id for action in plan.actions],
                    },
                )
                for action in plan.actions:
                    _attempt, unknown_reason = await run_action(
                        action,
                        correction_rounds,
                    )
                    correction_actions += 1
                    if unknown_reason is not None:
                        return await manual(
                            unknown_reason,
                            round_index=correction_rounds,
                            action_id=action.action_id,
                        )
                    if attempts[-1].error is not None:
                        # Revalidate the observed failure before asking for a new
                        # correction round; do not partially continue this plan.
                        break
        except OperationCancelledError:
            await emit(
                "closed_loop_cancelled",
                round_index=correction_rounds,
                data={"reason": token.reason},
            )
            raise
        except asyncio.CancelledError:
            # Preserve task cancellation.  The in-memory event remains ordered;
            # shield the sink long enough to make a best effort at audit closure.
            event_task = asyncio.create_task(
                emit(
                    "closed_loop_cancelled",
                    round_index=correction_rounds,
                    data={"reason": "executor task cancelled"},
                )
            )
            await asyncio.shield(event_task)
            raise


def _observation(
    initial_action: ClosedLoopAction,
    attempts: list[ClosedLoopAttempt],
    validations: list[ResultValidation],
    correction_rounds: int,
) -> ClosedLoopObservation:
    return ClosedLoopObservation(
        initial_action=copy.deepcopy(initial_action),
        attempts=tuple(copy.deepcopy(attempts)),
        validations=tuple(copy.deepcopy(validations)),
        correction_rounds=correction_rounds,
    )


async def _invoke_cancellable(
    callback: Callable[..., Any],
    *args: Any,
    cancellation: CancellationToken,
) -> Any:
    cancellation.throw_if_cancelled()
    value = callback(*args)
    if not inspect.isawaitable(value):
        cancellation.throw_if_cancelled()
        return value

    operation = asyncio.ensure_future(cast(Awaitable[Any], value))
    cancellation_wait = asyncio.create_task(cancellation.wait())
    try:
        done, _pending = await asyncio.wait(
            {operation, cancellation_wait},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancellation_wait in done:
            if not operation.done():
                operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            cancellation.throw_if_cancelled()
        result = await operation
        cancellation.throw_if_cancelled()
        return result
    finally:
        if not operation.done():
            operation.cancel()
        if not cancellation_wait.done():
            cancellation_wait.cancel()
        await asyncio.gather(operation, cancellation_wait, return_exceptions=True)


def _public_exception_summary(error: BaseException, *, fallback: str) -> str:
    """Keep diagnostic type while excluding untrusted exception text."""

    return (
        f"{public_error_message(error, fallback=fallback)} "
        f"[{type(error).__name__}]"
    )


def _is_outcome_unknown(value: Any, *, _seen: set[int] | None = None) -> bool:
    if isinstance(value, OutcomeUnknownToolError):
        return True
    if getattr(value, "outcome_unknown", False) is True:
        return True
    seen = set() if _seen is None else _seen
    identifier = id(value)
    if identifier in seen:
        return False
    seen.add(identifier)

    if isinstance(value, Mapping):
        if value.get("outcomeUnknown") is True or value.get("outcome_unknown") is True:
            return True
        if value.get("code") == "outcome_unknown":
            return True
        for key in ("details", "result"):
            nested = value.get(key)
            if nested is not None and _is_outcome_unknown(nested, _seen=seen):
                return True
        return False
    for name in ("details", "result"):
        nested = getattr(value, name, None)
        if nested is not None and _is_outcome_unknown(nested, _seen=seen):
            return True
    return False


def _strict_json(value: Any, name: str) -> None:
    import json

    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ClosedLoopValidationError(f"{name} must be strict JSON: {error}") from error


def _non_empty(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ClosedLoopValidationError(f"{name} must be a non-empty string")


__all__ = [
    "ClosedLoopAction",
    "ClosedLoopActionExecutor",
    "ClosedLoopAttempt",
    "ClosedLoopBudget",
    "ClosedLoopEvent",
    "ClosedLoopEventSink",
    "ClosedLoopExecutor",
    "ClosedLoopObservation",
    "ClosedLoopReplayPolicy",
    "ClosedLoopResult",
    "ClosedLoopStatus",
    "ClosedLoopValidationError",
    "CorrectionPlan",
    "CorrectionPlanner",
    "Replanner",
    "ResultValidation",
    "ResultValidator",
    "ValidationStatus",
]
