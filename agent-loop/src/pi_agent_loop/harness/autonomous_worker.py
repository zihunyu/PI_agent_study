"""Background-worker completion bridge for durable autonomous conversations."""

from __future__ import annotations

from ..cancellation import CancellationToken
from ..planning import PlanCompletionEnvelope, PlanExecutionResult
from .autonomous import AutonomousPlanRunner
from .autonomous_durability import (
    AutonomousConversationProjector,
    AutonomousRunStoreError,
)


class AutonomousPlanCompletionProjector:
    """Continue validate/replan/synthesize and project a worker result.

    Pass an instance as ``DurablePlanWorker(completion_handler=...)``.  The
    worker's plan execution is only the first half of autonomous processing;
    this bridge makes the second half explicit and reports projection failures
    as ``completion_error`` instead of silently losing the final answer.
    """

    def __init__(
        self,
        runner: AutonomousPlanRunner,
        projector: AutonomousConversationProjector,
    ) -> None:
        self.runner = runner
        self.projector = projector

    async def __call__(
        self,
        execution: PlanExecutionResult,
        cancellation: CancellationToken,
        envelope: PlanCompletionEnvelope | None = None,
    ) -> str:
        result = await self.runner.resume(
            execution.state.plan_id,
            cancellation=cancellation,
        )
        if result.closed_loop_run_id is None:
            raise AutonomousRunStoreError(
                "Worker 完成的 Plan 没有 durable closed-loop run linkage"
            )
        link = await self.projector.find(result.closed_loop_run_id)
        if link is None:
            raise AutonomousRunStoreError(
                "Worker 完成的 Plan 没有 Session conversation linkage"
            )
        if result.status != "waiting_approval":
            await self.projector.finalize(
                link,
                response_text=result.response_text,
                status=result.status,
            )
        else:
            store = self.runner.workflow.store
            if store is None:
                raise AutonomousRunStoreError(
                    "Waiting Autonomous Plan 缺少 durable Plan Store"
                )
            completion = await store.load(result.plan_id)
            await self.projector.project_waiting(
                link,
                response_text=result.response_text,
                projection_key=(
                    envelope.delivery_id
                    if envelope is not None
                    else (
                        completion.completion_envelope.delivery_id
                        if completion.completion_envelope is not None
                        else str(completion.completion_generation)
                    )
                ),
            )
        await self.runner.acknowledge_run_completions(
            result.closed_loop_run_id,
            exclude_plan_id=(
                envelope.plan_id if envelope is not None else execution.state.plan_id
            ),
        )
        return result.status


__all__ = ["AutonomousPlanCompletionProjector"]
