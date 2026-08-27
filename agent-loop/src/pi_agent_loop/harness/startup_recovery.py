"""进程启动时扫描未完成 Durable Operation 并安全恢复。"""

from __future__ import annotations

from dataclasses import dataclass

from ..session.operation_state import replay_operation
from ..session.operation_store import OperationEventStore
from ..session.resume import DurableSessionRecovery, RecoveryCallbacks


@dataclass(frozen=True, slots=True)
class StartupRecoveryReport:
    completed: tuple[str, ...]
    waiting_approval: tuple[str, ...]
    manual_intervention: tuple[str, ...]
    failed: tuple[str, ...]


class StartupRecoveryCoordinator:
    def __init__(
        self,
        store: OperationEventStore,
        callbacks: RecoveryCallbacks,
    ) -> None:
        self.store = store
        self.callbacks = callbacks
        self.recovery = DurableSessionRecovery(store)

    async def recover_all(self) -> StartupRecoveryReport:
        events = await self.store.load()
        grouped: dict[tuple[str, str], list] = {}
        for event in events:
            grouped.setdefault(
                (event.session_id, event.operation_id),
                [],
            ).append(event)

        completed: list[str] = []
        waiting_approval: list[str] = []
        manual: list[str] = []
        failed: list[str] = []
        for (session_id, operation_id), operation_events in grouped.items():
            try:
                state = replay_operation(operation_events)
            except Exception:
                failed.append(operation_id)
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
                continue
            if result.status == "completed":
                completed.append(operation_id)
            elif result.status in {
                "waiting_approval",
                "approval_consumer_required",
                "approved_write_runtime_required",
                "recovery_claimed",
            }:
                waiting_approval.append(operation_id)
            elif result.status in {
                "manual_intervention",
                "write_reconciliation_required",
            }:
                manual.append(operation_id)
            else:
                failed.append(operation_id)
        return StartupRecoveryReport(
            completed=tuple(completed),
            waiting_approval=tuple(waiting_approval),
            manual_intervention=tuple(manual),
            failed=tuple(failed),
        )
