"""统一装配 Agent、持久状态、恢复、Approval 和写操作的 Host。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..agent import Agent
from ..approval import ApprovalService
from ..cancellation import CancellationToken
from ..model_policy import ModelRequestPolicy
from ..planning import (
    ApprovalBarrier,
    HybridRequestPlanner,
    IntentPlanPolicy,
    MultiIntentPlan,
    PlanExecutionResult,
    SessionJournalPlanStore,
)
from ..retry.circuit_breaker import CircuitBreaker
from ..retry.compaction import CompactionRetryPolicy
from ..retry.types import ModelRetryPolicy
from ..routing.capabilities import CapabilityRegistry
from ..routing.routed_agent import RoutedAgent
from ..runtime import RuntimeStateTracker, Telemetry
from ..session import (
    ConversationSession,
    DurableOperationRecorder,
    JournalKeyProvider,
    JournalPrincipal,
    OperationEventStore,
    SessionContextProjection,
    WorkspaceSessionCatalog,
)
from ..security import VerifiedIdentity
from ..tool_runtime import ResourceLockBackend, ToolDispatchRuntime
from ..types import AgentTool, Model, StreamFn
from ..writes import WriteOperationService
from .approval_gateway import ApprovalResumeCoordinator
from .approval import DurableApprovalWorkflow
from .factory import DurableHostFactory, DurableHostSettings
from .lifecycle import DurableHostLifecycle
from .model_runtime_adapter import RecoverableModelRuntime, TokenPricing
from .plans import DurablePlanWorkflow
from .resources import DurableHostResources
from .session_runtime import SessionWriterLease
from .startup_recovery import StartupRecoveryCoordinator, StartupRecoveryReport
from .tool_runtime_adapter import RecoverableToolRuntime


@dataclass(frozen=True, slots=True)
class DurableHostPromptResult:
    result: Any
    operation_id: str | None
    approval_id: str | None = None


class DurableAgentHost:
    """P1 统一编排入口；具体业务通过 Router、Tool 和 Adapter 注入。"""

    def __init__(self) -> None:
        # 使用 async create()；私有字段在创建完成前不对外可用。
        self.session_id: str
        self.model: Model
        self.agent: Agent
        self.routed_agent: RoutedAgent | None
        self.runtime_tracker: RuntimeStateTracker
        self.operation_recorder: DurableOperationRecorder
        self.operation_store: OperationEventStore
        self.approvals: ApprovalService
        self.approval_resume: ApprovalResumeCoordinator
        self.writes: WriteOperationService
        self.model_runtime: RecoverableModelRuntime
        self.tool_runtime: RecoverableToolRuntime
        self.tool_dispatch_runtime: ToolDispatchRuntime
        self.startup_recovery: StartupRecoveryCoordinator
        self.startup_recovery_report: StartupRecoveryReport | None
        self.pending_approval_resumes: tuple[str, ...]
        self.recovered_approval_resumes: tuple[str, ...]
        self.resources: DurableHostResources
        self.lifecycle: DurableHostLifecycle
        self.approval_workflow: DurableApprovalWorkflow
        self.telemetry: Telemetry
        self.plan_store: SessionJournalPlanStore | None
        self.plan_workflow: DurablePlanWorkflow
        self.project_id: str | None
        self.session_metadata: ConversationSession | None
        self.session_catalog: WorkspaceSessionCatalog | None
        self.context_projection: SessionContextProjection
        self.session_writer_lease: SessionWriterLease | None

    @classmethod
    async def create(
        cls,
        *,
        session_id: str,
        state_dir: str | Path,
        model: Model,
        stream_fn: StreamFn,
        system_prompt: str,
        tools: list[AgentTool],
        router: Any | None = None,
        capabilities: CapabilityRegistry | None = None,
        before_tool_call: Callable[..., Any] | None = None,
        after_tool_call: Callable[..., Any] | None = None,
        reconcile_tool: Callable[..., Any] | None = None,
        authorize_never_replay: Callable[..., Any] | None = None,
        max_turns: int | None = None,
        max_tool_calls: int | None = None,
        max_parallel_tools: int | None = None,
        default_tool_timeout_seconds: float | None = None,
        default_lock_timeout_seconds: float = 30,
        tenant_tool_limits: dict[str, int] | None = None,
        resource_lock_backend: ResourceLockBackend | None = None,
        max_tool_update_tasks: int = 64,
        model_retry_policy: ModelRetryPolicy | None = None,
        model_circuit_breaker: CircuitBreaker | None = None,
        compaction_policy: CompactionRetryPolicy | None = None,
        compactor: Callable[..., Any] | None = None,
        telemetry: Telemetry | None = None,
        pricing: TokenPricing | Mapping[str, TokenPricing] | None = None,
        model_event_buffer_size: int = 256,
        model_event_buffer_bytes: int = 4 * 1024 * 1024,
        auto_recover: bool = True,
        approval_resume_handler: Callable[[dict[str, Any]], Any] | None = None,
        approval_consumer_resolver: Callable[[Any], Any] | None = None,
        store_backend: Literal["journal", "sqlite", "jsonl"] = "journal",
        journal_key_provider: JournalKeyProvider | None = None,
        journal_principal: JournalPrincipal | None = None,
        tenant_id: str = "local",
        owned_resources: tuple[Any, ...] = (),
        planner: HybridRequestPlanner | None = None,
        plan_policies: Mapping[str, IntentPlanPolicy] | None = None,
        plan_step_executor: Callable[..., Any] | None = None,
        plan_approval_barrier: ApprovalBarrier | None = None,
        max_parallel_plan_steps: int = 4,
        plan_lease_seconds: float = 30,
        project_id: str | None = None,
        workspace_path: str | Path | None = None,
        session_title: str | None = None,
        agent_profile: str = "default",
        resume_history: bool = True,
        managed_session: bool = False,
        exclusive_session: bool | None = None,
        session_writer_lease_seconds: float = 30,
    ) -> "DurableAgentHost":
        settings = DurableHostSettings(
            session_id=session_id,
            state_dir=state_dir,
            model=model,
            stream_fn=stream_fn,
            system_prompt=system_prompt,
            tools=tools,
            router=router,
            capabilities=capabilities,
            before_tool_call=before_tool_call,
            after_tool_call=after_tool_call,
            reconcile_tool=reconcile_tool,
            authorize_never_replay=authorize_never_replay,
            max_turns=max_turns,
            max_tool_calls=max_tool_calls,
            max_parallel_tools=max_parallel_tools,
            default_tool_timeout_seconds=default_tool_timeout_seconds,
            default_lock_timeout_seconds=default_lock_timeout_seconds,
            tenant_tool_limits=tenant_tool_limits,
            resource_lock_backend=resource_lock_backend,
            max_tool_update_tasks=max_tool_update_tasks,
            model_retry_policy=model_retry_policy,
            model_circuit_breaker=model_circuit_breaker,
            compaction_policy=compaction_policy,
            compactor=compactor,
            telemetry=telemetry,
            pricing=pricing,
            model_event_buffer_size=model_event_buffer_size,
            model_event_buffer_bytes=model_event_buffer_bytes,
            auto_recover=auto_recover,
            approval_resume_handler=approval_resume_handler,
            approval_consumer_resolver=approval_consumer_resolver,
            store_backend=store_backend,
            journal_key_provider=journal_key_provider,
            journal_principal=journal_principal,
            tenant_id=tenant_id,
            owned_resources=owned_resources,
            planner=planner,
            plan_policies=plan_policies,
            plan_step_executor=plan_step_executor,
            plan_approval_barrier=plan_approval_barrier,
            max_parallel_plan_steps=max_parallel_plan_steps,
            plan_lease_seconds=plan_lease_seconds,
            project_id=project_id,
            workspace_path=workspace_path,
            session_title=session_title,
            agent_profile=agent_profile,
            resume_history=resume_history,
            managed_session=managed_session or project_id is not None,
            exclusive_session=(
                managed_session or project_id is not None
                if exclusive_session is None
                else exclusive_session
            ),
            session_writer_lease_seconds=session_writer_lease_seconds,
        )
        return await DurableHostFactory().create(cls, settings)

    async def plan(self, request: str) -> MultiIntentPlan:
        """Create and durably initialize a validated Multi-Intent plan."""

        return await self.lifecycle.run(lambda: self.plan_workflow.plan(request))

    async def execute_plan(
        self,
        plan_id: str,
        *,
        cancellation: CancellationToken | None = None,
    ) -> PlanExecutionResult:
        """Execute a persisted plan under a renewable cross-worker lease."""

        return await self.lifecycle.run(
            lambda: self.plan_workflow.execute(plan_id, cancellation=cancellation)
        )

    async def resume_plan(
        self,
        plan_id: str,
        *,
        cancellation: CancellationToken | None = None,
    ) -> PlanExecutionResult:
        """Reload, replay and continue a previously interrupted plan."""

        return await self.lifecycle.run(
            lambda: self.plan_workflow.resume(plan_id, cancellation=cancellation)
        )

    async def prompt(
        self,
        text: str,
        *,
        requester: VerifiedIdentity | None = None,
        approval_role: str = "approver",
        idempotency_key: str | None = None,
    ) -> DurableHostPromptResult:
        return await self.lifecycle.run(
            lambda: self._prompt_and_record_activity(
                text,
                requester=requester,
                approval_role=approval_role,
                idempotency_key=idempotency_key,
            )
        )

    async def _prompt_and_record_activity(
        self,
        text: str,
        *,
        requester: VerifiedIdentity | None,
        approval_role: str,
        idempotency_key: str | None,
    ) -> DurableHostPromptResult:
        if self.session_writer_lease is not None:
            self.session_writer_lease.assert_owned()
        result = await self._prompt_impl(
            text,
            requester=requester,
            approval_role=approval_role,
            idempotency_key=idempotency_key,
        )
        if self.session_writer_lease is not None:
            self.session_writer_lease.assert_owned()
        if self.session_catalog is not None and self.session_metadata is not None:
            context = await self.context_projection.project(self.session_id)
            self.session_metadata = await self.session_catalog.record_activity(
                self.session_id,
                last_activity_sequence=(
                    context.last_sequence if context.last_sequence >= 0 else None
                ),
            )
        return result

    async def _prompt_impl(
        self,
        text: str,
        *,
        requester: VerifiedIdentity | None,
        approval_role: str,
        idempotency_key: str | None,
    ) -> DurableHostPromptResult:
        if self.routed_agent is None:
            await self.agent.prompt(text)
            return DurableHostPromptResult(
                result=None,
                operation_id=self.operation_recorder.last_operation_id,
            )

        result = await self.routed_agent.prompt(text)
        if result.decision.status != "in_scope_approval_required":
            return DurableHostPromptResult(
                result=result,
                operation_id=self.operation_recorder.last_operation_id,
            )
        approval_id = await self.approval_workflow.prepare(
            text=text,
            routed_result=result,
            requester=requester,
            approval_role=approval_role,
            idempotency_key=idempotency_key,
        )
        return DurableHostPromptResult(
            result=result,
            operation_id=self.operation_recorder.operation_id,
            approval_id=approval_id,
        )

    async def approve_and_resume(
        self,
        approval_id: str,
        *,
        approver: VerifiedIdentity,
        consumer: VerifiedIdentity,
        idempotency_key: str,
        write_handler: Callable[..., Any],
    ) -> Any:
        return await self.lifecycle.run(
            lambda: self.approval_workflow.approve_and_resume(
                approval_id,
                approver=approver,
                consumer=consumer,
                idempotency_key=idempotency_key,
                write_handler=write_handler,
            )
        )

    async def _request_approval_final_model(
        self,
        *,
        operation_id: str,
        approval_id: str,
        policy: ModelRequestPolicy,
    ) -> dict[str, Any]:
        return await self.approval_workflow.request_final_model(
            operation_id=operation_id,
            approval_id=approval_id,
            policy=policy,
        )

    async def _finish_approval_model_request(
        self,
        *,
        operation_id: str,
        approval_id: str,
        request_id: str,
        event_type: str,
        data: dict[str, Any],
    ) -> dict[str, Any] | None:
        return await self.approval_workflow.finish_model_request(
            operation_id=operation_id,
            approval_id=approval_id,
            request_id=request_id,
            event_type=event_type,
            data=data,
        )

    async def _materialize_write_tool_result(
        self,
        operation_id: str,
        tool_result: dict[str, Any],
    ):
        return await self.approval_workflow.materialize_write_tool_result(
            operation_id,
            tool_result,
        )

    async def _finish_persisted_operation(
        self,
        operation_id: str,
        outcome: str,
    ) -> None:
        await self.approval_workflow.finish_persisted_operation(
            operation_id,
            outcome,
        )

    async def recover_on_startup(self) -> StartupRecoveryReport:
        return await self.lifecycle.run(self.startup_recovery.recover_all)

    async def close(self) -> None:
        await self.lifecycle.close()
