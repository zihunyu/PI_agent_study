"""进程启动时扫描未完成 Durable Operation 并安全恢复。"""

from __future__ import annotations

import time
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


class StartupRecoveryCoordinator:
    def __init__(
        self,
        store: OperationEventStore,
        callbacks: RecoveryCallbacks,
        *,
        session_id: str | None = None,
        telemetry: Telemetry | None = None,
    ) -> None:
        self.store = store
        self.callbacks = callbacks
        self.session_id = session_id
        self.recovery = DurableSessionRecovery(store)
        self.telemetry = telemetry or Telemetry()

    async def recover_all(self) -> StartupRecoveryReport:
        events = await self.store.load(session_id=self.session_id)
        grouped: dict[tuple[str, str], list] = {}
        for event in events:
            grouped.setdefault(
                (event.session_id, event.operation_id),
                [],
            ).append(event)

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
                failed.append(operation_id)
                self.telemetry.record_recovery(
                    outcome="invalid_log",
                    duration_ms=(time.monotonic() - started) * 1000,
                )
                continue
            if state.phase in {"completed", "failed", "cancelled"}:
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
