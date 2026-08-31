"""进程启动时扫描未完成 Durable Operation 并安全恢复。"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..runtime.telemetry import Telemetry
from ..session.operation_state import replay_operation
from ..session.operation_store import OperationEventStore
from ..session.resume import DurableSessionRecovery, RecoveryCallbacks


@dataclass(frozen=True, slots=True)
class StartupRecoveryReport:
    completed: tuple[str, ...]
    waiting_approval: tuple[str, ...]
    ready_to_resume: tuple[str, ...]
    manual_intervention: tuple[str, ...]
    failed: tuple[str, ...]
    auto_recoverable: tuple[str, ...] = ()

    @property
    def blocking_operation_ids(self) -> tuple[str, ...]:
        """Return operations that make another durable write unsafe.

        ``waiting_approval`` is deliberately not a blocker: it has not crossed
        the external-effect boundary yet.  Every other unfinished or
        untrustworthy result must be reconciled before this Session can accept
        a new prompt/write.
        """

        return tuple(
            dict.fromkeys(
                (
                    *self.ready_to_resume,
                    *self.manual_intervention,
                    *self.failed,
                )
            )
        )

    @property
    def blocked(self) -> bool:
        return bool(self.blocking_operation_ids)

    @property
    def hard_blocking_operation_ids(self) -> tuple[str, ...]:
        """Facts that prohibit *any* other recovery side effect."""

        return tuple(
            dict.fromkeys((*self.manual_intervention, *self.failed))
        )

    @property
    def hard_blocked(self) -> bool:
        return bool(self.hard_blocking_operation_ids)


class StartupRecoveryBlockedError(RuntimeError):
    """Raised when startup recovery found facts that need reconciliation."""

    def __init__(self, report: StartupRecoveryReport) -> None:
        self.report = report
        operation_ids = ", ".join(report.blocking_operation_ids)
        super().__init__(
            "Session 启动恢复尚未安全完成，禁止继续 Prompt 或持久写操作；"
            f"请核对并持久化处理结果后重新运行恢复。Operation：{operation_ids}"
        )


class StartupRecoveryCoordinator:
    def __init__(
        self,
        store: OperationEventStore,
        callbacks: RecoveryCallbacks,
        *,
        session_id: str | None = None,
        telemetry: Telemetry | None = None,
        autonomous_resume: Callable[[str, str], Awaitable[str]] | None = None,
    ) -> None:
        self.store = store
        self.callbacks = callbacks
        self.session_id = session_id
        self.recovery = DurableSessionRecovery(store)
        self.telemetry = telemetry or Telemetry()
        self.autonomous_resume = autonomous_resume

    async def inspect_all(self) -> StartupRecoveryReport:
        """Purely inspect every operation before any recovery callback runs.

        This is the startup safety barrier.  In particular, it prevents an
        earlier operation from resuming a Tool/Approval side effect before a
        later operation in the same Session reveals an unknown write outcome.
        """

        events = await self.store.load(session_id=self.session_id)
        grouped = _group_events(events)
        waiting: list[str] = []
        ready: list[str] = []
        manual: list[str] = []
        failed: list[str] = []
        automatic: list[str] = []
        for (_session_id, operation_id), operation_events in grouped.items():
            try:
                state = replay_operation(operation_events)
            except Exception:
                if _legacy_terminal_uncertain_write_ids(operation_events):
                    manual.append(operation_id)
                else:
                    failed.append(operation_id)
                continue

            uncertain_writes = tuple(
                write.write_id
                for write in state.writes.values()
                if write.state in {"submitting", "outcome_unknown", "reconciling"}
            )
            pending_resume = _has_actionable_approval_resume(
                operation_events,
                state,
            )
            if uncertain_writes:
                manual.append(operation_id)
                continue
            if pending_resume:
                ready.append(operation_id)
                continue
            if state.phase in {"completed", "failed", "cancelled"}:
                continue

            marker = _autonomous_marker(state.configuration)
            if marker is not None:
                if self.autonomous_resume is None:
                    ready.append(operation_id)
                else:
                    automatic.append(operation_id)
                continue

            plan = self.recovery.planner.plan(state)
            kinds = {action.kind for action in plan.actions}
            if not kinds:
                automatic.append(operation_id)
            elif kinds.intersection({"manual_intervention", "reconcile_write"}):
                manual.append(operation_id)
            elif "reconcile_tool" in kinds:
                manual.append(operation_id)
            elif kinds.intersection(
                {
                    "consume_approval",
                    "execute_tool",
                    "resume_approved_write",
                }
            ):
                ready.append(operation_id)
            elif kinds == {"wait_for_approval"}:
                waiting.append(operation_id)
            elif kinds.issubset(
                {
                    "continue_model",
                    "finalize_rejected_approval",
                    "finish_operation",
                    "materialize_tool_result",
                    "replay_safe_tool",
                    "retry_model_request",
                }
            ):
                automatic.append(operation_id)
            else:
                manual.append(operation_id)

        return StartupRecoveryReport(
            completed=(),
            waiting_approval=tuple(waiting),
            ready_to_resume=tuple(ready),
            manual_intervention=tuple(manual),
            failed=tuple(failed),
            auto_recoverable=tuple(automatic),
        )

    async def recover_all(self) -> StartupRecoveryReport:
        inspection = await self.inspect_all()
        if inspection.blocked:
            return inspection
        return await self._recover_all_inspected()

    async def _recover_all_inspected(self) -> StartupRecoveryReport:
        """Execute only after a complete pure Session inspection succeeded."""

        events = await self.store.load(session_id=self.session_id)
        grouped = _group_events(events)

        completed: list[str] = []
        waiting_approval: list[str] = []
        ready_to_resume: list[str] = []
        manual: list[str] = []
        failed: list[str] = []
        for (session_id, operation_id), operation_events in grouped.items():
            started = time.monotonic()
            try:
                state = replay_operation(operation_events)
            except Exception:
                # 新 Reducer 会拒绝旧版本曾产生的“Operation 已终态但 Write
                # 仍不确定”矛盾日志。它不是可自动修复的普通坏日志，必须显式
                # 升级为人工核对，避免把可能已发生的副作用当作失败跳过。
                legacy_uncertain = _legacy_terminal_uncertain_write_ids(
                    operation_events
                )
                if legacy_uncertain:
                    manual.append(operation_id)
                    outcome = "terminal_operation_with_uncertain_write"
                else:
                    failed.append(operation_id)
                    outcome = "invalid_log"
                self.telemetry.record_recovery(
                    outcome=outcome,
                    duration_ms=(time.monotonic() - started) * 1000,
                )
                continue
            terminal_uncertain_writes = tuple(
                write.write_id
                for write in state.writes.values()
                if write.state in {"submitting", "outcome_unknown", "reconciling"}
            )
            if (
                state.phase in {"completed", "failed", "cancelled"}
                and terminal_uncertain_writes
            ):
                # 兼容读取旧版本曾写出的矛盾日志。Operation 已进入不可追加
                # 终态，不能安全自动 Reconcile，但也绝不能静默当作普通失败跳过。
                manual.append(operation_id)
                self.telemetry.record_recovery(
                    outcome="terminal_operation_with_uncertain_write",
                    duration_ms=(time.monotonic() - started) * 1000,
                )
                continue
            if state.phase in {"completed", "failed", "cancelled"}:
                continue
            marker = _autonomous_marker(state.configuration)
            if marker is not None:
                if self.autonomous_resume is None:
                    ready_to_resume.append(operation_id)
                    continue
                try:
                    status = await self.autonomous_resume(*marker)
                except Exception:
                    failed.append(operation_id)
                    self.telemetry.record_recovery(
                        outcome="autonomous_resume_failed",
                        duration_ms=(time.monotonic() - started) * 1000,
                    )
                    continue
                if status == "waiting_approval":
                    waiting_approval.append(operation_id)
                elif status in {"completed", "failed", "manual_intervention"}:
                    completed.append(operation_id)
                else:
                    failed.append(operation_id)
                self.telemetry.record_recovery(
                    outcome=f"autonomous_{status}",
                    duration_ms=(time.monotonic() - started) * 1000,
                )
                continue
            try:
                result = await self.recovery.resume(
                    session_id=session_id,
                    operation_id=operation_id,
                    callbacks=self.callbacks,
                )
            except Exception:
                failed.append(operation_id)
                self.telemetry.record_recovery(
                    outcome="failed",
                    duration_ms=(time.monotonic() - started) * 1000,
                )
                continue
            if result.status == "completed":
                completed.append(operation_id)
            elif result.status == "waiting_approval":
                waiting_approval.append(operation_id)
            elif result.status in {
                "approval_consumer_required",
                "approved_write_runtime_required",
                "recovery_claimed",
            }:
                ready_to_resume.append(operation_id)
            elif result.status in {
                "manual_intervention",
                "write_reconciliation_required",
            }:
                manual.append(operation_id)
            else:
                failed.append(operation_id)
            self.telemetry.record_recovery(
                outcome=result.status,
                duration_ms=(time.monotonic() - started) * 1000,
            )
        return StartupRecoveryReport(
            completed=tuple(completed),
            waiting_approval=tuple(waiting_approval),
            ready_to_resume=tuple(ready_to_resume),
            manual_intervention=tuple(manual),
            failed=tuple(failed),
        )


def _autonomous_marker(configuration: dict) -> tuple[str, str] | None:
    marker = configuration.get("autonomousTurn")
    if not isinstance(marker, dict):
        return None
    plan_id = marker.get("planId")
    run_id = marker.get("runId")
    if not isinstance(plan_id, str) or not plan_id or not isinstance(run_id, str) or not run_id:
        raise ValueError("Autonomous Turn Linkage 无效")
    return plan_id, run_id


def _group_events(events: list) -> dict[tuple[str, str], list]:
    grouped: dict[tuple[str, str], list] = {}
    for event in events:
        grouped.setdefault((event.session_id, event.operation_id), []).append(event)
    return grouped


def _has_actionable_approval_resume(events: list, state) -> bool:
    registered = {
        str(event.data.get("approvalId"))
        for event in events
        if event.type == "approval_resume_registered"
        and event.data.get("approvalId")
    }
    terminal = {
        str(event.data.get("approvalId"))
        for event in events
        if event.type
        in {
            "approval_resume_completed",
            "approval_resume_failed",
            "approval_resume_cancelled",
        }
        and event.data.get("approvalId")
    }
    for approval_id in registered - terminal:
        approval = state.approvals.get(approval_id)
        if approval is not None and (
            approval.state in {"approved", "consumed", "resume_started"}
            or approval.resume_state == "started"
        ):
            return True
    return False


def _legacy_terminal_uncertain_write_ids(events: list) -> tuple[str, ...]:
    if not any(event.type == "operation_finished" for event in events):
        return ()
    uncertain: set[str] = set()
    for event in sorted(events, key=lambda item: item.sequence):
        write_id = event.data.get("writeId")
        if not isinstance(write_id, str) or not write_id:
            continue
        if event.type in {
            "write_submitting",
            "write_outcome_unknown",
            "write_reconciling",
            "write_reconcile_failed",
            "write_reconcile_deferred",
        }:
            uncertain.add(write_id)
        elif event.type in {"write_succeeded", "write_failed"}:
            uncertain.discard(write_id)
    return tuple(sorted(uncertain))
