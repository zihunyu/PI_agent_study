"""统一装配 Agent、持久状态、恢复、Approval 和写操作的 Host。"""

from __future__ import annotations

from ..routing.routed_agent import RuntimeBoundRouter

from ..planning.store_protocol import JournalPlanStore

from ..execution_policy import ExecutionPolicy

from ..business import BusinessBundle
from ..general import GeneralAgentBundle
from ..context import ContextBudget

import inspect
import asyncio
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from ..agent import Agent
from ..approval import ApprovalService
from ..cancellation import CancellationToken
from ..model_policy import ModelRequestPolicy
from ..planning import (
    ApprovalBarrier,
    ClosedLoopBudget,
    DurablePlanStore,
    HybridRequestPlanner,
    IntentPlanPolicy,
    MultiIntentPlan,
    PlanExecutionResult,
)
from ..retry.circuit_breaker import CircuitBreaker
from ..retry.compaction import CompactionRetryPolicy
from ..retry.types import ModelRetryPolicy
from ..routing.capabilities import CapabilityRegistry
from ..routing.routed_agent import RoutedAgent
from ..routing.types import RequestDecision
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
from ..security import VerifiedIdentity, VerifiedIdentityValidator
from ..tool_runtime import ResourceLockBackend, ToolDispatchRuntime
from ..types import (
    AgentTool,
    Model,
    StreamFn,
    ToolAuthorization,
    ToolDispatchContext,
)
from ..messages import assistant_message, user_message
from ..writes import WriteOperationService
from .approval_gateway import ApprovalResumeCoordinator
from .approval import (
    DurableApprovalAction,
    DurableApprovalBatch,
    DurableApprovalBatchResult,
    DurableApprovalExecution,
    DurableApprovalWorkflow,
)
from .autonomous import (
    AutonomousPlanResult,
    AutonomousPlanRunner,
    PlanReplanner,
    PlanResultSynthesizer,
    PlanResultValidator,
)
from .autonomous_durability import (
    AutonomousConversationProjector,
    JournalRunStore,
)
from .factory import DurableHostFactory, DurableHostSettings
from .lifecycle import DurableHostLifecycle
from .model_runtime_adapter import RecoverableModelRuntime, TokenPricing
from .plans import DurablePlanWorkflow
from .resources import DurableHostResourceFactory, DurableHostResources
from .session_runtime import SessionWriterLease
from .startup_recovery import (
    StartupRecoveryBlockedError,
    StartupRecoveryCoordinator,
    StartupRecoveryReport,
)
from .tool_runtime_adapter import RecoverableToolRuntime


@dataclass(frozen=True, slots=True)
class DurableHostPromptResult:
    result: Any
    operation_id: str | None
    approval_id: str | None = None
    autonomous_result: AutonomousPlanResult | None = None
    plan_id: str | None = None
    pending_approval_ids: tuple[str, ...] = ()


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
        self.plan_store: DurablePlanStore | None
        self.distributed_execution: bool
        self.plan_workflow: DurablePlanWorkflow
        self.autonomous_plan_runner: AutonomousPlanRunner | None
        self.autonomous_run_store: JournalRunStore | None
        self.autonomous_conversation_projector: AutonomousConversationProjector
        self.project_id: str | None
        self.session_metadata: ConversationSession | None
        self.session_catalog: WorkspaceSessionCatalog | None
        self.context_projection: SessionContextProjection
        self.session_writer_lease: SessionWriterLease | None
        self.tool_identity: VerifiedIdentity | None
        self.general_task_mode: bool
        self._submission_lock = asyncio.Lock()

    @classmethod
    async def create(
        cls,
        *,
        session_id: str,
        state_dir: str | Path,
        model: Model,
        stream_fn: StreamFn,
        system_prompt: str,
        tools: list[AgentTool] | None = None,
        business_bundle: BusinessBundle | None = None,
        general_bundle: GeneralAgentBundle | None = None,
        execution_policy: ExecutionPolicy | None = None,
        router: Any | None = None,
        capabilities: CapabilityRegistry | None = None,
        router_policy_version: str = "1",
        approval_policy_version: str = "1",
        plan_policy_version: str = "1",
        security_policy_version: str = "1",
        before_tool_call: Callable[..., Any] | None = None,
        after_tool_call: Callable[..., Any] | None = None,
        reconcile_tool: Callable[..., Any] | None = None,
        reconcile_write: Callable[..., Any] | None = None,
        authorize_never_replay: Callable[..., Any] | None = None,
        tool_identity: VerifiedIdentity | None = None,
        approval_identity_validator: VerifiedIdentityValidator | None = None,
        tool_authorization: ToolAuthorization | None = None,
        require_tool_identity: bool = False,
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
        approval_resume_handler: Callable[..., Any] | None = None,
        approval_consumer_resolver: Callable[[Any], Any] | None = None,
        store_backend: Literal["journal", "sqlite", "jsonl"] = "journal",
        journal_key_provider: JournalKeyProvider | None = None,
        journal_principal: JournalPrincipal | None = None,
        tenant_id: str = "local",
        owned_resources: tuple[Any, ...] = (),
        resource_factory: DurableHostResourceFactory | None = None,
        planner: HybridRequestPlanner | None = None,
        plan_policies: Mapping[str, IntentPlanPolicy] | None = None,
        plan_store: DurablePlanStore | None = None,
        distributed_execution: bool = False,
        plan_step_executor: Callable[..., Any] | None = None,
        plan_tool_bindings: Mapping[str, str] | None = None,
        plan_write_metadata_provider: Callable[..., Any] | None = None,
        plan_approval_barrier: ApprovalBarrier | None = None,
        max_parallel_plan_steps: int = 4,
        plan_lease_seconds: float = 30,
        auto_plan_complex_requests: bool = True,
        plan_result_validator: PlanResultValidator | None = None,
        plan_replanner: PlanReplanner | None = None,
        plan_result_synthesizer: PlanResultSynthesizer | None = None,
        plan_correction_budget: ClosedLoopBudget | None = None,
        plan_usage_meter: Any | None = None,
        project_id: str | None = None,
        workspace_path: str | Path | None = None,
        session_title: str | None = None,
        agent_profile: str = "default",
        resume_history: bool = True,
        managed_session: bool = False,
        exclusive_session: bool | None = None,
        session_writer_lease_seconds: float = 30,
        allow_configuration_migration: bool = False,
    ) -> "DurableAgentHost":
        if general_bundle is not None:
            if exclusive_session is False:
                raise ValueError("general tasks require an exclusive managed session")
            managed_session = True
            if not isinstance(general_bundle, GeneralAgentBundle):
                raise TypeError("general_bundle must be GeneralAgentBundle")
            artifact_scope = general_bundle.validator.artifacts.records
            if artifact_scope.session_id != session_id or artifact_scope.principal.tenant_id != tenant_id:
                raise ValueError("general artifact store must match the Host tenant and session")
            if any(value is not None for value in (business_bundle, tools, router, capabilities, planner, plan_policies, plan_tool_bindings, plan_step_executor, plan_result_validator, plan_replanner, plan_result_synthesizer)):
                raise ValueError("general_bundle cannot be mixed with business or legacy execution configuration")
            tools = list(general_bundle.tools)
            capabilities = general_bundle.capabilities
            router, planner, plan_replanner, plan_result_synthesizer = general_bundle.assemble(model, stream_fn)
            plan_policies = general_bundle.policies
            plan_tool_bindings = general_bundle.tool_bindings
            plan_result_validator = general_bundle.validator
            owned_resources = (*owned_resources, *general_bundle.resources)
            if execution_policy is None:
                window = model.context_window if model.context_window > 0 else 32_000
                reserve = min(model.max_tokens or 4096, max(1, window // 4))
                execution_policy = ExecutionPolicy(context_budget=ContextBudget(window, output_reserve=reserve, safety_margin=min(512, max(0, window // 16))))
            if not auto_plan_complex_requests:
                raise ValueError("general tasks require the durable autonomous runner")
        if business_bundle is not None:
            if not isinstance(business_bundle, BusinessBundle):
                raise TypeError("business_bundle must be BusinessBundle")
            if any(value is not None for value in (tools, router, capabilities, planner, plan_policies, plan_tool_bindings, plan_step_executor)):
                raise ValueError("business_bundle cannot be mixed with tools, router, capabilities or plan configuration")
            business_bundle.validate()
            tools = list(business_bundle.tools)
            capabilities = business_bundle.capabilities
            router = business_bundle.create_router(model=model, stream_fn=stream_fn)
            if business_bundle.plan_policies:
                plan_policies = business_bundle.plan_policies
                plan_tool_bindings = business_bundle.plan_tool_bindings
                planner = business_bundle.create_planner(model=model, stream_fn=stream_fn)
        settings = DurableHostSettings(
            session_id=session_id,
            state_dir=state_dir,
            model=model,
            stream_fn=stream_fn,
            system_prompt=system_prompt,
            tools=[] if tools is None else tools,
            execution_policy=execution_policy,
            general_task_mode=general_bundle is not None,
            router=router,
            capabilities=capabilities,
            router_policy_version=router_policy_version,
            approval_policy_version=approval_policy_version,
            plan_policy_version=plan_policy_version,
            security_policy_version=security_policy_version,
            before_tool_call=before_tool_call,
            after_tool_call=after_tool_call,
            reconcile_tool=reconcile_tool,
            reconcile_write=reconcile_write,
            authorize_never_replay=authorize_never_replay,
            tool_identity=tool_identity,
            approval_identity_validator=approval_identity_validator,
            tool_authorization=tool_authorization,
            require_tool_identity=require_tool_identity,
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
            resource_factory=resource_factory,
            planner=planner,
            plan_policies=plan_policies,
            plan_store=plan_store,
            distributed_execution=distributed_execution,
            plan_step_executor=plan_step_executor,
            plan_tool_bindings=plan_tool_bindings,
            plan_write_metadata_provider=plan_write_metadata_provider,
            plan_approval_barrier=plan_approval_barrier,
            max_parallel_plan_steps=max_parallel_plan_steps,
            plan_lease_seconds=plan_lease_seconds,
            auto_plan_complex_requests=auto_plan_complex_requests,
            plan_result_validator=plan_result_validator,
            plan_replanner=plan_replanner,
            plan_result_synthesizer=plan_result_synthesizer,
            plan_correction_budget=plan_correction_budget,
            plan_usage_meter=plan_usage_meter,
            project_id=project_id,
            workspace_path=workspace_path,
            session_title=session_title,
            agent_profile=agent_profile,
            resume_history=resume_history,
            managed_session=managed_session or project_id is not None,
            exclusive_session=(
                True if exclusive_session is None else exclusive_session
            ),
            session_writer_lease_seconds=session_writer_lease_seconds,
            allow_configuration_migration=allow_configuration_migration,
        )
        return await DurableHostFactory().create(cls, settings)

    async def plan(self, request: str) -> MultiIntentPlan:
        """Create and durably initialize a validated Multi-Intent plan."""

        return await self.lifecycle.run(
            lambda: self._run_with_writer_lease(
                lambda: self.plan_workflow.plan(request)
            )
        )

    async def execute_plan(
        self,
        plan_id: str,
        *,
        cancellation: CancellationToken | None = None,
    ) -> PlanExecutionResult:
        """Execute a persisted plan under a renewable cross-worker lease."""

        return await self.lifecycle.run(
            lambda: self._run_with_writer_lease(
                lambda: self.plan_workflow.execute(
                    plan_id,
                    cancellation=cancellation,
                )
            )
        )

    async def resume_plan(
        self,
        plan_id: str,
        *,
        cancellation: CancellationToken | None = None,
    ) -> PlanExecutionResult:
        """Reload, replay and continue a previously interrupted plan."""

        return await self.lifecycle.run(
            lambda: self._run_with_writer_lease(
                lambda: self.plan_workflow.resume(
                    plan_id,
                    cancellation=cancellation,
                )
            )
        )

    async def resume_autonomous_plan(
        self,
        plan_id: str,
        *,
        cancellation: CancellationToken | None = None,
    ) -> DurableHostPromptResult:
        """Resume a suspended autonomous plan, validate it and persist its answer."""

        async def resume() -> DurableHostPromptResult:
            await self._verify_writer_lease()
            self._ensure_startup_recovery_unblocked()
            if self.autonomous_plan_runner is None:
                raise RuntimeError("Host 未配置 AutonomousPlanRunner")
            autonomous = await self.autonomous_plan_runner.resume(
                plan_id,
                cancellation=cancellation,
            )
            link = (
                await self.autonomous_conversation_projector.find(
                    autonomous.closed_loop_run_id
                )
                if autonomous.closed_loop_run_id is not None
                else None
            )
            if link is not None and autonomous.status != "waiting_approval":
                assistant = await self.autonomous_conversation_projector.finalize(
                    link,
                    response_text=autonomous.response_text,
                    status=autonomous.status,
                )
                self._append_autonomous_message_once(assistant)
                operation_id = link.operation_id
            elif link is not None:
                if self.plan_store is None:
                    raise RuntimeError("Waiting Autonomous Plan 缺少 Plan Store")
                completion = await self.plan_store.load(autonomous.plan_id)
                waiting = await self.autonomous_conversation_projector.project_waiting(
                    link,
                    response_text=autonomous.response_text,
                    projection_key=(
                        completion.completion_envelope.delivery_id
                        if completion.completion_envelope is not None
                        else str(completion.completion_generation)
                    ),
                )
                self._append_autonomous_message_once(waiting)
                operation_id = link.operation_id
            else:
                # Compatibility for plans created before autonomous run linkage
                # existed. New runs always take the exactly-once path above.
                operation_id = await self._record_autonomous_exchange(
                    None,
                    autonomous.response_text,
                )
            if autonomous.closed_loop_run_id is not None:
                await self.autonomous_plan_runner.acknowledge_run_completions(
                    autonomous.closed_loop_run_id
                )
            else:
                await self.autonomous_plan_runner.acknowledge_completion(
                    autonomous.plan_id
                )
            await self._verify_writer_lease()
            await self._record_session_activity()
            return DurableHostPromptResult(
                result=autonomous,
                operation_id=operation_id,
                autonomous_result=autonomous,
                plan_id=autonomous.plan_id,
                pending_approval_ids=autonomous.pending_approval_ids,
            )

        return await self.lifecycle.run(resume)

    async def submit_task(
        self,
        request_id: str,
        text: str,
        *,
        cancellation: CancellationToken | None = None,
    ) -> DurableHostPromptResult:
        """Idempotent general-task submission, including crash recovery.

        A stable request ID is bound to exactly one request in this session.
        Re-delivery resumes the existing run and its durable budgets/approvals.
        """
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 160:
            raise ValueError("request_id must be a bounded non-empty string")
        if not isinstance(text, str) or not text.strip() or len(text) > 128_000:
            raise ValueError("task text must be bounded and non-empty")
        run_id = "submission-" + hashlib.sha256(request_id.encode()).hexdigest()

        async def submit() -> DurableHostPromptResult:
            async with self._submission_lock:
                token = cancellation or CancellationToken()
                token.throw_if_cancelled()
                await self._verify_writer_lease()
                self._ensure_startup_recovery_unblocked()
                runner = self.autonomous_plan_runner
                run_store = self.autonomous_run_store
                if not self.general_task_mode or runner is None or run_store is None or not isinstance(self.plan_store, JournalPlanStore):
                    raise RuntimeError("submit_task requires a durable general-task Host")
                link = await self.autonomous_conversation_projector.find(run_id)
                if link is not None:
                    record = await run_store.load(run_id)
                    if record.request != text:
                        raise ValueError("request_id is already bound to different task text")
                    return await self.resume_autonomous_plan(record.latest_plan_id, cancellation=token)
                unfinished = await self.autonomous_conversation_projector.find_unfinished()
                if unfinished is not None:
                    raise RuntimeError("another task in this session must be resumed first")
                self.agent.tool_dispatch_context = ToolDispatchContext(
                    identity=self.tool_identity,
                    tenant_id=self.agent.tenant_id,
                    fencing_token=self.session_writer_lease.fencing_token if self.session_writer_lease else None,
                    fencing_scope=f"conversation_session_writer:{self.session_id}" if self.session_writer_lease else None,
                )
                prepared = await runner.prepare(text, run_id=run_id, defer_persistence_for_conversation=True, cancellation=token)
                link = await self.autonomous_conversation_projector.bootstrap(
                    text, run_id=prepared.run_id, plan=prepared.plan, budget=runner.budget,
                    initial_messages=list(self.agent.state.messages), run_store=run_store, plan_store=self.plan_store,
                )
                self._append_autonomous_message_once(await self.autonomous_conversation_projector.load_user_message(link))
                # resume uses the same Plan/Run/Conversation execution and
                # projection path as an ordinary recovered autonomous request.
                return await self.resume_autonomous_plan(prepared.plan.plan_id, cancellation=token)

        return await self.lifecycle.run(submit)

    async def prompt(
        self,
        text: str,
        *,
        cancellation: CancellationToken | None = None,
        requester: VerifiedIdentity | None = None,
        approval_role: str = "approver",
        idempotency_key: str | None = None,
        entity_id: str | None = None,
        expected_entity_version: int | None = None,
        business_preconditions: dict[str, Any] | None = None,
    ) -> DurableHostPromptResult:
        return await self.lifecycle.run(
            lambda: self._prompt_and_record_activity(
                text,
                cancellation=cancellation,
                requester=requester,
                approval_role=approval_role,
                idempotency_key=idempotency_key,
                entity_id=entity_id,
                expected_entity_version=expected_entity_version,
                business_preconditions=business_preconditions,
            )
        )

    async def _prompt_and_record_activity(
        self,
        text: str,
        *,
        cancellation: CancellationToken | None,
        requester: VerifiedIdentity | None,
        approval_role: str,
        idempotency_key: str | None,
        entity_id: str | None,
        expected_entity_version: int | None,
        business_preconditions: dict[str, Any] | None,
    ) -> DurableHostPromptResult:
        await self._verify_writer_lease()
        self._ensure_startup_recovery_unblocked()
        unfinished = await self.autonomous_conversation_projector.find_unfinished()
        if unfinished is not None:
            raise RuntimeError(
                "Session 仍有未结束的 Autonomous Plan；"
                f"请先恢复 plan_id={unfinished.plan_id}，不能并发开启新对话轮次"
            )
        result = await self._prompt_impl(
            text,
            cancellation=cancellation,
            requester=requester,
            approval_role=approval_role,
            idempotency_key=idempotency_key,
            entity_id=entity_id,
            expected_entity_version=expected_entity_version,
            business_preconditions=business_preconditions,
        )
        await self._verify_writer_lease()
        await self._record_session_activity()
        return result

    async def _record_session_activity(self) -> None:
        if self.session_catalog is None or self.session_metadata is None:
            return
        writer_lease = self.session_writer_lease
        if writer_lease is None:
            raise RuntimeError(
                "记录 Session Activity 前缺少 Writer Lease"
            )
        context = await self.context_projection.project(self.session_id)
        self.session_metadata = await self.session_catalog.record_activity(
            self.session_id,
            last_activity_sequence=(
                context.last_sequence if context.last_sequence >= 0 else None
            ),
            fenced_claim=writer_lease.claim_lease,
            fenced_claim_lease_seconds=writer_lease.lease_seconds,
        )

    async def _prompt_impl(
        self,
        text: str,
        *,
        cancellation: CancellationToken | None,
        requester: VerifiedIdentity | None,
        approval_role: str,
        idempotency_key: str | None,
        entity_id: str | None,
        expected_entity_version: int | None,
        business_preconditions: dict[str, Any] | None,
    ) -> DurableHostPromptResult:
        if cancellation is not None:
            # 预取消请求不能创建 Router/Provider/Tool 侧效果。
            cancellation.throw_if_cancelled()
        if (
            self.tool_identity is not None
            and requester is not None
            and requester != self.tool_identity
        ):
            raise PermissionError("Prompt Requester 与 Session Tool Identity 不一致")
        self.agent.tool_dispatch_context = ToolDispatchContext(
            identity=self.tool_identity or requester,
            tenant_id=self.agent.tenant_id,
            fencing_token=(
                self.session_writer_lease.fencing_token
                if self.session_writer_lease is not None
                else None
            ),
            fencing_scope=(
                f"conversation_session_writer:{self.session_id}"
                if self.session_writer_lease is not None
                else None
            ),
        )
        if self.routed_agent is None:
            unsafe_tools = sorted(
                tool.name
                for tool in self.agent.state.tools
                if tool.requires_approval
            )
            if unsafe_tools:
                raise PermissionError(
                    "无 Router 的 Host 禁止执行需要 Approval 的工具："
                    + ", ".join(unsafe_tools)
                )
            await self.agent.prompt(text, cancellation=cancellation)
            return DurableHostPromptResult(
                result=None,
                operation_id=self.operation_recorder.last_operation_id,
            )

        route_admission: tuple[str, Any] | None = None
        route_accounted = False
        route_admission_retained = False
        route_router = self.routed_agent.router
        plan_runner = self.autonomous_plan_runner
        autonomous_run_store = self.autonomous_run_store
        if (
            plan_runner is not None
            and plan_runner.pre_route_budget_enabled
        ):
            if (
                plan_runner.hard_model_budget_enabled
                and not isinstance(route_router, RuntimeBoundRouter)
            ):
                raise RuntimeError(
                    "硬 model/token/cost 预算禁止使用未纳入 Durable Admission "
                    "的自定义 Router"
                )
            route_admission = (
                await plan_runner.open_pre_route_admission(text)
            )

        async def close_route_admission(reason: str) -> None:
            if route_admission is None:
                return
            if plan_runner is None:
                raise RuntimeError(
                    "Pre-route Admission 存在但 Autonomous Runner 缺失"
                )
            await plan_runner.close_pre_route_admission(
                route_admission[0],
                reason=reason,
            )

        async def finish_route_admission(
            *,
            decision: Any | None,
            close_reason: str,
        ) -> None:
            """Settle exactly the Router call before any ordinary Agent call.

            ``RoutedAgent.prompt`` can continue into a multi-turn Agent loop after
            routing.  Keeping the Router ticket open around that whole method
            would make one reservation appear to cover several Provider calls.
            The route callback is therefore the exact metering boundary.  Only
            a Plan decision keeps the provisional Run open for ``prepare``.
            """

            nonlocal route_accounted, route_admission_retained
            if route_admission is None or route_accounted:
                return
            if plan_runner is None:
                raise RuntimeError(
                    "Pre-route Admission 存在但 Autonomous Runner 缺失"
                )
            route_run_id, route_ticket = route_admission
            try:
                if route_ticket is None:
                    pass
                else:
                    await plan_runner.settle_pre_route_admission(route_run_id, route_ticket)
            except BaseException:
                await plan_runner.close_pre_route_admission(
                    route_run_id,
                    reason="router_usage_unknown",
                )
                route_accounted = True
                raise
            route_admission_retained = bool(
                isinstance(decision, RequestDecision)
                and decision.status == "in_scope_plan_required"
            )
            if not route_admission_retained:
                await close_route_admission(close_reason)
        try:
            if route_admission is None:
                result = await self.routed_agent.prompt(
                    text,
                    cancellation=cancellation,
                )
            else:
                async def dispatch_router(callback):
                    if plan_runner is None:
                        raise RuntimeError(
                            "Pre-route Admission 存在但 Autonomous Runner 缺失"
                        )
                    try:
                        decision = await (
                            plan_runner.dispatch_pre_route_admission(
                                route_admission[0],
                                route_admission[1],
                                callback,
                            )
                        )
                    except BaseException:
                        await finish_route_admission(
                            decision=None,
                            close_reason="routing_error",
                        )
                        raise
                    await finish_route_admission(
                        decision=decision,
                        close_reason="routing_completed_non_plan",
                    )
                    return decision

                result = await self.routed_agent.prompt(
                    text,
                    route_dispatcher=dispatch_router,
                    cancellation=cancellation,
                )
        except BaseException:
            if route_admission is not None and not route_accounted:
                await finish_route_admission(
                    decision=None,
                    close_reason="routing_error",
                )
            elif (
                route_admission is not None
                and route_admission_retained
            ):
                await close_route_admission("routing_error")
            raise
        if (
            route_admission is not None
            and route_admission_retained
            and result.decision.status != "in_scope_plan_required"
        ):
            await close_route_admission("routing_completed_non_plan")
            route_admission_retained = False
        if result.decision.status == "in_scope_plan_required":
            task_decision = result.decision.task_decision
            if task_decision is None and not self.general_task_mode:
                message = "Router 没有提供可校验的结构化任务决策，已拒绝自动执行。"
                blocked_decision = replace(
                    result.decision,
                    status="in_scope_need_clarification",
                    reason=message,
                    message=message,
                )
                invalid = replace(
                    result,
                    decision=blocked_decision,
                    response_text=message,
                    error_code="task_decision_missing",
                )
                operation_id = await self._record_autonomous_exchange(
                    text,
                    message,
                )
                if route_admission is not None:
                    await close_route_admission("router_plan_input_missing")
                return DurableHostPromptResult(
                    result=invalid,
                    operation_id=operation_id,
                )
            if task_decision is not None and not task_decision.ready_for_planner:
                blocked_decision = replace(
                    result.decision,
                    status="in_scope_need_clarification",
                    reason="结构化任务仍有缺失参数或能力，不能进入执行计划",
                )
                incomplete = replace(
                    result,
                    decision=blocked_decision,
                    error_code="plan_input_incomplete",
                )
                operation_id = await self._record_autonomous_exchange(
                    text,
                    incomplete.response_text,
                )
                if route_admission is not None:
                    await close_route_admission(
                        "router_plan_input_incomplete"
                    )
                return DurableHostPromptResult(
                    result=incomplete,
                    operation_id=operation_id,
                )
            if plan_runner is None:
                message = (
                    "该请求需要拆解为多步骤任务，但 Host 尚未配置 Planner、"
                    "Plan Step Executor 和 Journal Plan Store。"
                )
                blocked_decision = replace(
                    result.decision,
                    status="in_scope_need_clarification",
                    reason=message,
                    message=message,
                )
                unavailable = replace(
                    result,
                    decision=blocked_decision,
                    response_text=message,
                    error_code="plan_workflow_unavailable",
                )
                operation_id = await self._record_autonomous_exchange(
                    text,
                    message,
                )
                return DurableHostPromptResult(
                    result=unavailable,
                    operation_id=operation_id,
                )
            try:
                prepared = await plan_runner.prepare(
                    text,
                    task_decision=task_decision,
                    run_id=(
                        None if route_admission is None else route_admission[0]
                    ),
                    defer_persistence_for_conversation=True,
                    cancellation=cancellation,
                )
            except BaseException:
                if route_admission is not None:
                    if autonomous_run_store is None:
                        raise RuntimeError(
                            "Autonomous Admission 缺少 Durable Run Store"
                        )
                    durable_admission = await autonomous_run_store.load(
                        route_admission[0]
                    )
                    if (
                        not durable_admission.initial_plan_bound
                        and not durable_admission.admission_closed
                    ):
                        await close_route_admission("plan_prepare_failed")
                raise
            if autonomous_run_store is None or not isinstance(
                self.plan_store,
                JournalPlanStore,
            ):
                raise RuntimeError(
                    "Autonomous Plan 需要支持 Plan/Run/Conversation 原子绑定的 "
                    "Session Journal Store"
                )
            link = await self.autonomous_conversation_projector.bootstrap(
                text,
                run_id=prepared.run_id,
                plan=prepared.plan,
                budget=plan_runner.budget,
                initial_messages=list(self.agent.state.messages),
                run_store=autonomous_run_store,
                plan_store=self.plan_store,
            )
            durable_user = (
                await self.autonomous_conversation_projector.load_user_message(
                    link
                )
            )
            self._append_autonomous_message_once(durable_user)
            # The user message and its plan/run linkage are durable before this
            # call can dispatch a tool or any other side effect.
            autonomous = await plan_runner.run_prepared(
                prepared,
                cancellation=cancellation,
            )
            routed = replace(
                result,
                response_text=autonomous.response_text,
                error_code=(
                    None
                    if autonomous.status in {"completed", "waiting_approval"}
                    else autonomous.status
                ),
            )
            if autonomous.status != "waiting_approval":
                assistant = await self.autonomous_conversation_projector.finalize(
                    link,
                    response_text=autonomous.response_text,
                    status=autonomous.status,
                )
                self._append_autonomous_message_once(assistant)
            else:
                completion = await self.plan_store.load(autonomous.plan_id)
                waiting = await self.autonomous_conversation_projector.project_waiting(
                    link,
                    response_text=autonomous.response_text,
                    projection_key=(
                        completion.completion_envelope.delivery_id
                        if completion.completion_envelope is not None
                        else str(completion.completion_generation)
                    ),
                )
                self._append_autonomous_message_once(waiting)
            if autonomous.closed_loop_run_id is None:
                raise RuntimeError("Autonomous Result 缺少 Closed-loop Run ID")
            await plan_runner.acknowledge_run_completions(
                autonomous.closed_loop_run_id
            )
            operation_id = link.operation_id
            return DurableHostPromptResult(
                result=routed,
                operation_id=operation_id,
                autonomous_result=autonomous,
                plan_id=autonomous.plan_id,
                pending_approval_ids=autonomous.pending_approval_ids,
            )
        if route_admission is not None and route_admission_retained:
            await close_route_admission(
                f"router_non_plan:{result.decision.status}"
            )
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
            entity_id=entity_id,
            expected_entity_version=expected_entity_version,
            business_preconditions=business_preconditions,
        )
        return DurableHostPromptResult(
            result=result,
            operation_id=self.operation_recorder.operation_id,
            approval_id=approval_id,
        )

    def _append_autonomous_message_once(self, message: dict[str, Any]) -> None:
        """Mirror an already durable autonomous message without duplicating it."""

        marker = message.get("autonomousRun")
        if not isinstance(marker, dict):
            raise ValueError("Autonomous Message 缺少 durable linkage")
        run_id = marker.get("runId")
        projection = marker.get("projection")
        projection_key = marker.get("projectionKey")
        role = message.get("role")
        if any(
            existing.get("role") == role
            and isinstance(existing.get("autonomousRun"), dict)
            and existing["autonomousRun"].get("runId") == run_id
            and existing["autonomousRun"].get("projection") == projection
            and existing["autonomousRun"].get("projectionKey") == projection_key
            for existing in self.agent.state.messages
        ):
            return
        self.agent.state.messages.append(message)

    async def _record_autonomous_exchange(
        self,
        text: str | None,
        response_text: str,
    ) -> str:
        """Persist the plan-level answer as normal Session conversation context.

        Routing owns its own short Operation and finishes it before autonomous
        execution begins.  The durable Plan stream records execution control,
        while this second Operation records only the user-visible exchange so a
        reopened Session receives the same context as an ordinary Agent turn.
        """

        await self._verify_writer_lease()
        initial_messages = list(self.agent.state.messages)
        operation_id = await self.operation_recorder.start_operation(
            initial_messages=initial_messages,
        )
        user = user_message(text) if text is not None else None
        assistant = assistant_message(
            model=self.model,
            content=[{"type": "text", "text": response_text}],
        )
        try:
            if user is not None:
                await self.operation_recorder.record_external(
                    "message_appended",
                    {"message": user},
                )
            await self.operation_recorder.record_external(
                "message_appended",
                {"message": assistant},
            )
            await self.operation_recorder.finish_operation("completed")
        except BaseException:
            # Never expose an in-memory conversation that was not durably
            # committed. Startup recovery will inspect the unfinished Operation.
            raise
        if user is not None:
            self.agent.state.messages.append(user)
        self.agent.state.messages.append(assistant)
        return operation_id

    async def approve_and_resume(
        self,
        approval_id: str,
        *,
        approver: VerifiedIdentity,
        consumer: VerifiedIdentity,
        idempotency_key: str,
        write_handler: Callable[..., Any] | None = None,
        context_handler: Callable[..., Any] | None = None,
    ) -> Any:
        return await self.lifecycle.run(
            lambda: self._run_with_writer_lease(
                lambda: self.approval_workflow.approve_and_resume(
                    approval_id,
                    approver=approver,
                    consumer=consumer,
                    idempotency_key=idempotency_key,
                    write_handler=write_handler,
                    context_handler=context_handler,
                )
            )
        )

    async def request_approval_batch(
        self,
        *,
        text: str,
        actions: tuple[DurableApprovalAction, ...],
        requester: VerifiedIdentity,
    ) -> DurableApprovalBatch:
        """原子创建一批审批动作，并受 Host 生命周期和租约保护。"""

        return await self.lifecycle.run(
            lambda: self._run_with_writer_lease(
                lambda: self.approval_workflow._request_many_impl(
                    text=text,
                    actions=actions,
                    requester=requester,
                )
            )
        )

    async def approve_approval_batch(
        self,
        batch: DurableApprovalBatch,
        executions: tuple[DurableApprovalExecution, ...],
    ) -> DurableApprovalBatchResult:
        """仅在全部确认齐备后，按顺序执行一批受审批保护的写动作。"""

        return await self.lifecycle.run(
            lambda: self._run_with_writer_lease(
                lambda: self.approval_workflow._approve_many_impl(
                    batch,
                    executions,
                )
            )
        )

    async def reject_approval_and_finish(
        self,
        approval_id: str,
        *,
        rejector: VerifiedIdentity,
        reason: str = "用户拒绝执行",
    ) -> Any:
        return await self.lifecycle.run(
            lambda: self._run_with_writer_lease(
                lambda: self.approval_workflow.reject_and_finish(
                    approval_id,
                    rejector=rejector,
                    reason=reason,
                )
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
        return await self.lifecycle.run(
            lambda: self._run_with_writer_lease(
                self._recover_and_refresh_context,
                allow_recovery_blocked=True,
            )
        )

    async def resolve_startup_recovery(
        self,
        resolver: Callable[[StartupRecoveryReport], Any],
    ) -> StartupRecoveryReport:
        """Resolve startup blockers without providing an unsafe acknowledge switch.

        ``resolver`` must append authoritative reconciliation/repair facts (for
        example by calling ``host.writes.reconcile``).  The Host then replays
        startup recovery and only unlocks normal prompts when the durable log
        proves that every blocker has gone away.
        """

        return await self.lifecycle.run(
            lambda: self._run_with_writer_lease(
                lambda: self._resolve_startup_recovery(resolver),
                allow_recovery_blocked=True,
            )
        )

    async def _resolve_startup_recovery(
        self,
        resolver: Callable[[StartupRecoveryReport], Any],
    ) -> StartupRecoveryReport:
        report = self.startup_recovery_report
        if report is None:
            report = await self._recover_and_refresh_context()
        if report.blocked:
            value = resolver(report)
            if inspect.isawaitable(value):
                await value
        report = await self._recover_and_refresh_context()
        if report.blocked:
            raise StartupRecoveryBlockedError(report)
        return report

    async def _recover_and_refresh_context(self) -> StartupRecoveryReport:
        inspection = await self.startup_recovery.inspect_all()
        report = (
            inspection
            if inspection.blocked
            else await self.startup_recovery.recover_all()
        )
        self.startup_recovery_report = report
        context = await self.context_projection.project(self.session_id)
        self.agent.state.messages = context.copy_messages()
        return report

    def _ensure_startup_recovery_unblocked(self) -> None:
        report = self.startup_recovery_report
        if report is not None and report.blocked:
            raise StartupRecoveryBlockedError(report)

    async def _verify_writer_lease(self) -> None:
        lease = self.session_writer_lease
        if lease is not None:
            await lease.verify_owned()

    async def _run_with_writer_lease(
        self,
        operation: Callable[[], Any],
        *,
        allow_recovery_blocked: bool = False,
    ) -> Any:
        """Fence every public durable write boundary with Store-backed checks."""

        await self._verify_writer_lease()
        if not allow_recovery_blocked:
            self._ensure_startup_recovery_unblocked()
        value = operation()
        result = await value if inspect.isawaitable(value) else value
        await self._verify_writer_lease()
        return result

    async def close(self) -> None:
        await self.lifecycle.close()
