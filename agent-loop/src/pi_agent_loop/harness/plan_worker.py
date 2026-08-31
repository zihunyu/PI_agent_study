"""Fenced background worker for durable Multi-Intent plans."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeAlias, cast
from uuid import uuid4

from ..cancellation import CancellationToken, OperationCancelledError
from ..planning import (
    DurablePlanStore,
    DurablePlanRecord,
    PlanCompletionEnvelope,
    PlanExecutionResult,
    PlanExecutionConflictError,
    PlanExecutionLeaseLostError,
    ResultSynthesizer,
    validate_durable_plan_store,
)
from ..planning.types import PlanPhase
from ..session.operation_store import ClaimLease
from .plans import DurablePlanWorkflow, PlanWorkflowUnavailableError


PlanWorkerItemStatus: TypeAlias = Literal[
    "completed",
    "waiting_approval",
    "failed",
    "manual_intervention",
    "conflict",
    "skipped",
    "cancelled",
    "lease_lost",
    "completion_error",
    "error",
]
PlanWorkerBatchSink = Callable[["PlanWorkerBatchResult"], Any]
PlanWorkerCompletionHandler = Callable[..., str | None | Awaitable[str | None]]


class _AcquireCompletion(Protocol):
    def __call__(
        self,
        plan_id: str,
        owner_token: str,
        *,
        lease_seconds: float,
    ) -> Awaitable[ClaimLease | None]: ...


class _RenewCompletion(Protocol):
    def __call__(
        self,
        lease: ClaimLease,
        *,
        lease_seconds: float,
    ) -> Awaitable[bool]: ...


class _AckCompletion(Protocol):
    def __call__(
        self,
        plan_id: str,
        lease: ClaimLease,
        *,
        lease_seconds: float,
        envelope: PlanCompletionEnvelope,
    ) -> Awaitable[DurablePlanRecord]: ...


class _ReleaseCompletion(Protocol):
    def __call__(self, lease: ClaimLease) -> Awaitable[None]: ...


@dataclass(frozen=True, slots=True)
class PlanWorkerItemResult:
    """One immutable plan-attempt outcome.

    ``conflict`` means another healthy Worker won the lease.  It is deliberately
    separate from ``failed`` and ``error`` so normal distributed contention does
    not page operators or trigger a retry storm.
    """

    plan_id: str
    status: PlanWorkerItemStatus
    discovered_phase: PlanPhase
    final_phase: PlanPhase | None = None
    error_code: str | None = None
    completion_status: str | None = None

    @property
    def is_conflict(self) -> bool:
        return self.status == "conflict"

    @property
    def is_worker_error(self) -> bool:
        return self.status in {"lease_lost", "completion_error", "error"}


@dataclass(frozen=True, slots=True)
class PlanWorkerBatchResult:
    worker_id: str
    discovered_plan_ids: tuple[str, ...]
    items: tuple[PlanWorkerItemResult, ...]
    cancelled: bool = False

    @property
    def conflict_count(self) -> int:
        return sum(item.is_conflict for item in self.items)

    @property
    def worker_error_count(self) -> int:
        return sum(item.is_worker_error for item in self.items)

    @property
    def executed_count(self) -> int:
        return sum(
            item.status
            in {"completed", "waiting_approval", "failed", "manual_intervention"}
            for item in self.items
        )


@dataclass(frozen=True, slots=True)
class PlanWorkerServeResult:
    worker_id: str
    batches: int
    discovered: int
    executed: int
    conflicts: int
    worker_errors: int
    cancelled: bool
    last_batch: PlanWorkerBatchResult | None = None


class DurablePlanWorker:
    """Poll a Session's durable Plan streams and execute them with fencing.

    The Worker never claims work by scanning alone.  ``DurablePlanWorkflow``
    remains the sole execution path and obtains a monotonic fenced lease before
    loading or mutating a Plan.  This makes several processes polling the same
    transactional Journal safe: one wins, the others report a normal
    conflict and move on.
    """

    def __init__(
        self,
        workflow: DurablePlanWorkflow,
        *,
        worker_id: str | None = None,
        max_concurrent_plans: int = 4,
        max_batch_size: int = 100,
        poll_interval_seconds: float = 1.0,
        completion_handler: PlanWorkerCompletionHandler | None = None,
        distributed_execution: bool = False,
    ) -> None:
        store = workflow.store
        if store is None:
            raise PlanWorkflowUnavailableError(
                "DurablePlanWorker 需要可持久化的 Plan Store"
            )
        if type(distributed_execution) is not bool:
            raise TypeError("distributed_execution 必须是布尔值")
        _positive_int(max_concurrent_plans, "max_concurrent_plans")
        _positive_int(max_batch_size, "max_batch_size")
        if (
            isinstance(poll_interval_seconds, bool)
            or not isinstance(poll_interval_seconds, (int, float))
            or poll_interval_seconds <= 0
        ):
            raise ValueError("poll_interval_seconds 必须是正数")
        resolved_worker_id = worker_id or str(uuid4())
        if not isinstance(resolved_worker_id, str) or not resolved_worker_id.strip():
            raise ValueError("worker_id 不能为空")
        self.workflow = workflow
        effective_distributed = distributed_execution or (
            isinstance(workflow, DurablePlanWorkflow)
            and workflow.distributed_execution
        )
        if isinstance(workflow, DurablePlanWorkflow) or effective_distributed:
            self.store = validate_durable_plan_store(
                store,
                require_cross_process=True,
                require_multi_host=effective_distributed,
            )
        else:
            # Small test/composition facades remain possible, while the real
            # DurablePlanWorkflow and every strict distributed Worker always
            # cross the validated store contract above.
            self.store = cast(DurablePlanStore, store)
        self.distributed_execution = effective_distributed
        self.worker_id = resolved_worker_id
        self.max_concurrent_plans = max_concurrent_plans
        self.max_batch_size = max_batch_size
        self.poll_interval_seconds = float(poll_interval_seconds)
        if completion_handler is not None and not callable(completion_handler):
            raise TypeError("completion_handler 必须可调用或为 None")
        durable_outbox = all(
            callable(getattr(self.store, name, None))
            for name in (
                "acquire_completion",
                "ack_completion",
                "renew_completion",
                "release_completion_lease",
            )
        )
        if (
            completion_handler is not None
            and durable_outbox
            and not _accepts_completion_envelope(completion_handler)
        ):
            raise TypeError(
                "Durable completion_handler 必须接收 "
                "(result, cancellation, envelope)，用于稳定 delivery_id 去重"
            )
        self.completion_handler = completion_handler
        self._scan_offset = 0

    async def poll(
        self,
        *,
        cancellation: CancellationToken | None = None,
    ) -> PlanWorkerBatchResult:
        """Poll once.  Alias kept explicit for scheduler/worker integrations."""

        return await self.run_once(cancellation=cancellation)

    async def run_once(
        self,
        *,
        cancellation: CancellationToken | None = None,
    ) -> PlanWorkerBatchResult:
        token = cancellation or CancellationToken()
        if token.cancelled:
            return PlanWorkerBatchResult(self.worker_id, (), (), cancelled=True)

        # Rotate the candidate window.  Without this, a permanently claimed
        # early Plan could occupy every bounded batch and starve all later work.
        all_records = await self.store.list_runnable_plans()
        if self.completion_handler is not None:
            list_completion = getattr(
                self.store,
                "list_completion_pending_plans",
                None,
            )
            if callable(list_completion):
                completion_records = await list_completion()
                by_plan_id = {
                    record.plan.plan_id: record for record in all_records
                }
                for record in completion_records:
                    by_plan_id.setdefault(record.plan.plan_id, record)
                all_records = tuple(by_plan_id.values())
        records = self._next_candidate_window(all_records)
        discovered = tuple(record.plan.plan_id for record in records)
        if not records:
            return PlanWorkerBatchResult(
                self.worker_id,
                discovered,
                (),
                cancelled=token.cancelled,
            )

        semaphore = asyncio.Semaphore(self.max_concurrent_plans)

        async def execute(record) -> PlanWorkerItemResult:
            async with semaphore:
                return await self._execute_candidate(record.plan.plan_id, token)

        tasks = [
            asyncio.create_task(
                execute(record),
                name=f"durable-plan-worker:{self.worker_id}:{record.plan.plan_id}",
            )
            for record in records
        ]
        try:
            items = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return PlanWorkerBatchResult(
            self.worker_id,
            discovered,
            tuple(items),
            cancelled=token.cancelled,
        )

    def _next_candidate_window(
        self,
        records: tuple[DurablePlanRecord, ...],
    ) -> tuple[DurablePlanRecord, ...]:
        if not records:
            self._scan_offset = 0
            return ()
        start = self._scan_offset % len(records)
        ordered = (*records[start:], *records[:start])
        selected = ordered[: self.max_batch_size]
        self._scan_offset = (start + len(selected)) % len(records)
        return selected

    async def serve(
        self,
        *,
        cancellation: CancellationToken | None = None,
        on_batch: PlanWorkerBatchSink | None = None,
        max_batches: int | None = None,
    ) -> PlanWorkerServeResult:
        """Continuously poll until cancelled (or an optional batch bound).

        Only aggregate counters and the latest batch are retained, so a
        long-running Worker does not grow memory with its lifetime.
        """

        if max_batches is not None:
            _positive_int(max_batches, "max_batches")
        token = cancellation or CancellationToken()
        batches = discovered = executed = conflicts = worker_errors = 0
        last_batch: PlanWorkerBatchResult | None = None
        while not token.cancelled and (
            max_batches is None or batches < max_batches
        ):
            batch = await self.run_once(cancellation=token)
            last_batch = batch
            batches += 1
            discovered += len(batch.discovered_plan_ids)
            executed += batch.executed_count
            conflicts += batch.conflict_count
            worker_errors += batch.worker_error_count
            if on_batch is not None:
                value = on_batch(batch)
                if inspect.isawaitable(value):
                    await cast(Awaitable[Any], value)
            if token.cancelled or (max_batches is not None and batches >= max_batches):
                break
            await _wait_for_poll(token, self.poll_interval_seconds)
        return PlanWorkerServeResult(
            worker_id=self.worker_id,
            batches=batches,
            discovered=discovered,
            executed=executed,
            conflicts=conflicts,
            worker_errors=worker_errors,
            cancelled=token.cancelled,
            last_batch=last_batch,
        )

    async def _execute_candidate(
        self,
        plan_id: str,
        cancellation: CancellationToken,
    ) -> PlanWorkerItemResult:
        plan_cancellation = cancellation.create_child()
        try:
            plan_cancellation.throw_if_cancelled()
            current = await self.store.load(plan_id)
            discovered_phase = current.state.phase
            result: PlanExecutionResult | None = None
            if discovered_phase in {"pending", "running"}:
                result = await self.workflow.execute(
                    plan_id,
                    cancellation=plan_cancellation,
                )
            elif not (
                self.completion_handler is not None
                and getattr(current, "completion_pending", False)
            ):
                return PlanWorkerItemResult(
                    plan_id,
                    "skipped",
                    discovered_phase,
                    final_phase=discovered_phase,
                )
            if result is None:
                result = PlanExecutionResult(
                    current.state,
                    ResultSynthesizer().synthesize(
                        current.plan,
                        current.state,
                    ),
                    (),
                )
            if result.state.phase in {"pending", "running"}:
                return PlanWorkerItemResult(
                    plan_id,
                    "error",
                    discovered_phase,
                    final_phase=result.state.phase,
                    error_code="executor_returned_nonterminal_phase",
                )
            completion_status: str | None = None
            if self.completion_handler is not None:
                delivery = await self._deliver_completion(
                    plan_id,
                    result,
                    plan_cancellation,
                )
                if isinstance(delivery, PlanWorkerItemResult):
                    return PlanWorkerItemResult(
                        plan_id,
                        delivery.status,
                        discovered_phase,
                        final_phase=result.state.phase,
                        error_code=delivery.error_code,
                    )
                completion_status = delivery
            return PlanWorkerItemResult(
                plan_id,
                cast(PlanWorkerItemStatus, result.state.phase),
                discovered_phase,
                final_phase=result.state.phase,
                completion_status=completion_status,
            )
        except PlanExecutionConflictError:
            # Expected healthy contention; deliberately not an execution error.
            current = await self.store.load(plan_id)
            return PlanWorkerItemResult(
                plan_id,
                "conflict",
                current.state.phase,
                final_phase=current.state.phase,
                error_code="execution_lease_conflict",
            )
        except PlanExecutionLeaseLostError:
            current = await self.store.load(plan_id)
            return PlanWorkerItemResult(
                plan_id,
                "lease_lost",
                current.state.phase,
                final_phase=current.state.phase,
                error_code="execution_lease_lost",
            )
        except OperationCancelledError:
            current = await self.store.load(plan_id)
            return PlanWorkerItemResult(
                plan_id,
                "cancelled",
                current.state.phase,
                final_phase=current.state.phase,
                error_code="worker_cancelled",
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Do not copy callback exception text into worker reports: arbitrary
            # tool/provider errors may contain credentials or business payloads.
            current = await self.store.load(plan_id)
            return PlanWorkerItemResult(
                plan_id,
                "error",
                current.state.phase,
                final_phase=current.state.phase,
                error_code=type(error).__name__,
            )
        finally:
            plan_cancellation.detach()

    async def _deliver_completion(
        self,
        plan_id: str,
        result: PlanExecutionResult,
        cancellation: CancellationToken,
    ) -> str | None | PlanWorkerItemResult:
        assert self.completion_handler is not None
        acquire = getattr(self.store, "acquire_completion", None)
        ack = getattr(self.store, "ack_completion", None)
        renew = getattr(self.store, "renew_completion", None)
        release = getattr(self.store, "release_completion_lease", None)
        durable_outbox = all(callable(item) for item in (acquire, ack, renew, release))
        if not durable_outbox:
            return await self._invoke_completion_handler(
                result,
                cancellation,
                None,
            )
        acquire_completion = cast(_AcquireCompletion, acquire)
        ack_completion = cast(_AckCompletion, ack)
        renew_completion = cast(_RenewCompletion, renew)
        release_completion = cast(_ReleaseCompletion, release)

        lease_seconds = float(getattr(self.workflow, "lease_seconds", 30.0))
        lease = await acquire_completion(
            plan_id,
            self.store.new_owner_token(),
            lease_seconds=lease_seconds,
        )
        if lease is None:
            current = await self.store.load(plan_id)
            if not getattr(current, "completion_pending", False):
                return None
            return PlanWorkerItemResult(
                plan_id,
                "conflict",
                current.state.phase,
                final_phase=current.state.phase,
                error_code="completion_lease_conflict",
            )

        claimed = await self.store.load(plan_id)
        if not getattr(claimed, "completion_pending", False):
            await release_completion(lease)
            return None
        envelope = getattr(claimed, "completion_envelope", None)
        if not isinstance(envelope, PlanCompletionEnvelope):
            await release_completion(lease)
            return PlanWorkerItemResult(
                plan_id,
                "completion_error",
                claimed.state.phase,
                final_phase=claimed.state.phase,
                error_code="completion_envelope_missing",
            )
        # The candidate/result snapshot may predate an approval transition or
        # another completion generation.  Always deliver the state protected by
        # the acquired completion lease, never a stale terminal snapshot.
        result = PlanExecutionResult(
            claimed.state,
            ResultSynthesizer().synthesize(claimed.plan, claimed.state),
            (),
        )

        completion_token = cancellation.create_child()
        lease_lost = asyncio.Event()

        async def heartbeat() -> None:
            interval = max(0.001, min(lease_seconds / 3, 5.0))
            try:
                while True:
                    await asyncio.sleep(interval)
                    if not await renew_completion(
                        lease,
                        lease_seconds=lease_seconds,
                    ):
                        lease_lost.set()
                        completion_token.cancel("Plan Completion Lease 已丢失")
                        return
            except asyncio.CancelledError:
                raise
            except BaseException:
                lease_lost.set()
                completion_token.cancel("Plan Completion Lease 续租失败")

        renewal = asyncio.create_task(
            heartbeat(),
            name=f"plan-completion-lease:{plan_id}",
        )
        try:
            try:
                status = await self._invoke_completion_handler(
                    result,
                    completion_token,
                    envelope,
                )
            except (asyncio.CancelledError, OperationCancelledError):
                if lease_lost.is_set():
                    return PlanWorkerItemResult(
                        plan_id,
                        "lease_lost",
                        result.state.phase,
                        final_phase=result.state.phase,
                        error_code="completion_lease_lost",
                    )
                raise
            except Exception as error:
                return PlanWorkerItemResult(
                    plan_id,
                    "completion_error",
                    result.state.phase,
                    final_phase=result.state.phase,
                    error_code=f"completion_handler:{type(error).__name__}",
                )
            if lease_lost.is_set():
                return PlanWorkerItemResult(
                    plan_id,
                    "lease_lost",
                    result.state.phase,
                    final_phase=result.state.phase,
                    error_code="completion_lease_lost",
                )
            try:
                await ack_completion(
                    plan_id,
                    lease,
                    lease_seconds=lease_seconds,
                    envelope=envelope,
                )
            except PlanExecutionLeaseLostError:
                return PlanWorkerItemResult(
                    plan_id,
                    "lease_lost",
                    result.state.phase,
                    final_phase=result.state.phase,
                    error_code="completion_ack_lease_lost",
                )
            except Exception as error:
                # The projection may already be durable while the ack either
                # failed before commit or its response was lost after commit.
                # Never invoke a compensating projection here. The pending
                # record (when still present) drives an idempotent retry.
                return PlanWorkerItemResult(
                    plan_id,
                    "completion_error",
                    result.state.phase,
                    final_phase=result.state.phase,
                    error_code=f"completion_ack:{type(error).__name__}",
                )
            return status
        finally:
            if not renewal.done():
                renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)
            await release_completion(lease)
            completion_token.detach()

    async def _invoke_completion_handler(
        self,
        result: PlanExecutionResult,
        cancellation: CancellationToken,
        envelope: PlanCompletionEnvelope | None,
    ) -> str | None:
        assert self.completion_handler is not None
        handler = self.completion_handler
        if envelope is not None:
            if not _accepts_completion_envelope(handler):
                raise TypeError(
                    "Durable completion_handler 不接受 Completion Envelope"
                )
            value = handler(result, cancellation, envelope)
        else:
            value = handler(result, cancellation)
        completion_status = (
            await cast(Awaitable[str | None], value)
            if inspect.isawaitable(value)
            else value
        )
        if completion_status is not None and (
            not isinstance(completion_status, str)
            or not completion_status.strip()
        ):
            raise TypeError("completion_handler 必须返回非空字符串或 None")
        return completion_status


async def _wait_for_poll(
    cancellation: CancellationToken,
    seconds: float,
) -> None:
    timer = asyncio.create_task(asyncio.sleep(seconds), name="plan-worker-poll-timer")
    cancelled = asyncio.create_task(
        cancellation.wait(), name="plan-worker-poll-cancellation"
    )
    try:
        await asyncio.wait({timer, cancelled}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (timer, cancelled):
            if not task.done():
                task.cancel()
        await asyncio.gather(timer, cancelled, return_exceptions=True)


def _positive_int(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} 必须是大于 0 的整数")


def _accepts_completion_envelope(handler: PlanWorkerCompletionHandler) -> bool:
    try:
        inspect.signature(handler).bind(object(), object(), object())
    except (TypeError, ValueError):
        return False
    return True


__all__ = [
    "DurablePlanWorker",
    "PlanWorkerBatchResult",
    "PlanWorkerBatchSink",
    "PlanWorkerCompletionHandler",
    "PlanWorkerItemResult",
    "PlanWorkerItemStatus",
    "PlanWorkerServeResult",
]
