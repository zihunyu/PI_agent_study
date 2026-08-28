"""DurableAgentHost 的对象图装配。

Host 本身只保留稳定的 Facade API；Provider、统一 Journal、模型/工具 Runtime、
Recovery 与业务协调器都在这里完成一次性装配，避免不同执行路径各造一套 Runtime。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..agent import Agent
from ..approval import ApprovalService
from ..planning import (
    ApprovalBarrier,
    HybridRequestPlanner,
    IntentPlanPolicy,
    SessionJournalPlanStore,
)
from ..providers.openai_compatible import OpenAICompatibleProvider
from ..retry.circuit_breaker import CircuitBreaker
from ..retry.compaction import CompactionRetryPolicy, ContextReplacement
from ..retry.types import ModelRetryPolicy
from ..routing.capabilities import CapabilityRegistry
from ..routing.hybrid_router import HybridModelRouter
from ..routing.routed_agent import RoutedAgent
from ..runtime import RuntimeStateTracker, Telemetry
from ..session import (
    DurableOperationRecorder,
    JournalKeyProvider,
    JournalPrincipal,
    RuntimeRecoveryManager,
)
from ..tool_runtime import ResourceLockBackend, ToolDispatchRuntime
from ..types import AgentTool, Model, StreamFn
from ..writes import WriteOperationService
from .approval import DurableApprovalWorkflow
from .approval_gateway import ApprovalResumeCoordinator
from .lifecycle import DurableHostLifecycle
from .model_runtime_adapter import RecoverableModelRuntime, TokenPricing
from .plans import DurablePlanWorkflow
from .recovery import build_recovery_callbacks
from .resources import DurableHostResources
from .startup_recovery import StartupRecoveryCoordinator
from .tool_runtime_adapter import RecoverableToolRuntime


Compactor = Callable[
    [list[dict[str, Any]]],
    Awaitable[list[dict[str, Any]] | ContextReplacement],
]


@dataclass(slots=True)
class DurableHostSettings:
    session_id: str
    state_dir: str | Path
    model: Model
    stream_fn: StreamFn
    system_prompt: str
    tools: list[AgentTool]
    router: Any | None = None
    capabilities: CapabilityRegistry | None = None
    before_tool_call: Callable[..., Any] | None = None
    after_tool_call: Callable[..., Any] | None = None
    reconcile_tool: Callable[..., Any] | None = None
    authorize_never_replay: Callable[..., Any] | None = None
    max_turns: int | None = None
    max_tool_calls: int | None = None
    max_parallel_tools: int | None = None
    default_tool_timeout_seconds: float | None = None
    default_lock_timeout_seconds: float = 30
    tenant_tool_limits: dict[str, int] | None = None
    resource_lock_backend: ResourceLockBackend | None = None
    max_tool_update_tasks: int = 64
    model_retry_policy: ModelRetryPolicy | None = None
    model_circuit_breaker: CircuitBreaker | None = None
    compaction_policy: CompactionRetryPolicy | None = None
    compactor: Compactor | None = None
    telemetry: Telemetry | None = None
    pricing: TokenPricing | Mapping[str, TokenPricing] | None = None
    model_event_buffer_size: int = 256
    model_event_buffer_bytes: int = 4 * 1024 * 1024
    auto_recover: bool = True
    approval_resume_handler: Callable[[dict[str, Any]], Any] | None = None
    approval_consumer_resolver: Callable[[Any], Any] | None = None
    store_backend: Literal["journal", "sqlite", "jsonl"] = "journal"
    journal_key_provider: JournalKeyProvider | None = None
    journal_principal: JournalPrincipal | None = None
    tenant_id: str = "local"
    owned_resources: tuple[Any, ...] = field(default_factory=tuple)
    planner: HybridRequestPlanner | None = None
    plan_policies: Mapping[str, IntentPlanPolicy] | None = None
    plan_step_executor: Callable[..., Any] | None = None
    plan_approval_barrier: ApprovalBarrier | None = None
    max_parallel_plan_steps: int = 4
    plan_lease_seconds: float = 30


class DurableHostFactory:
    """构建一个共享 Model/Tool Runtime 的完整 Host。"""

    async def create(self, host_type: type[Any], settings: DurableHostSettings) -> Any:
        if not settings.session_id:
            raise ValueError("session_id 不能为空")
        host: Any | None = None
        try:
            # Host construction belongs to the same ownership boundary as the
            # rest of assembly. A custom facade constructor can fail before
            # ``host.resources`` exists, but caller-supplied owned resources
            # must still be released.
            host = host_type()
            return await self._assemble(host, settings)
        except BaseException as error:
            resources = getattr(host, "resources", None) if host is not None else None
            managed_resources = _managed_resources(settings)
            if resources is None and managed_resources:
                resources = DurableHostResources(
                    root=Path(settings.state_dir),
                    operation_store=None,
                    runtime_store=None,
                    retry_store=None,
                )
                for resource in managed_resources:
                    resources.own(resource)
            if resources is not None:
                try:
                    await resources.close()
                except BaseException as cleanup_error:
                    raise BaseExceptionGroup(
                        "DurableAgentHost 创建和资源清理均失败",
                        [error, cleanup_error],
                    )
            raise

    async def _assemble(
        self,
        host: Any,
        settings: DurableHostSettings,
    ) -> Any:
        host.session_id = settings.session_id
        host.model = settings.model
        host.telemetry = settings.telemetry or Telemetry()
        host.resources = DurableHostResources.create(
            settings.state_dir,
            session_id=settings.session_id,
            store_backend=settings.store_backend,
            journal_key_provider=settings.journal_key_provider,
            journal_principal=settings.journal_principal,
            tenant_id=settings.tenant_id,
        )
        for resource in _managed_resources(settings):
            host.resources.own(resource)

        runtime_store = host.resources.runtime_store
        host.operation_store = host.resources.operation_store
        retry_store = host.resources.retry_store
        host.plan_store = (
            SessionJournalPlanStore(
                host.resources.journal,
                host.resources.journal_principal,
                session_id=settings.session_id,
            )
            if host.resources.journal is not None
            and host.resources.journal_principal is not None
            else None
        )
        host.plan_workflow = DurablePlanWorkflow(
            store=host.plan_store,
            planner=settings.planner,
            policies=settings.plan_policies,
            step_executor=settings.plan_step_executor,
            approval_barrier=settings.plan_approval_barrier,
            max_parallel_steps=settings.max_parallel_plan_steps,
            lease_seconds=settings.plan_lease_seconds,
        )
        await RuntimeRecoveryManager(runtime_store).recover()
        host.runtime_tracker = await RuntimeStateTracker.create(runtime_store)
        host.operation_recorder = DurableOperationRecorder(
            host.operation_store,
            session_id=settings.session_id,
            tools=settings.tools,
            configuration={
                "provider": settings.model.provider,
                "model": settings.model.id,
            },
            run_id_provider=lambda: host.runtime_tracker.state.run_id,
        )

        policy = settings.compaction_policy or CompactionRetryPolicy(
            max_retries=1,
            keep_recent_messages=20,
            target_context_tokens=32_000,
            reserved_tokens=4_000,
        )
        host.model_runtime = RecoverableModelRuntime(
            model=settings.model,
            stream_fn=settings.stream_fn,
            system_prompt=settings.system_prompt,
            tools=settings.tools,
            retry_policy=settings.model_retry_policy,
            circuit_breaker=settings.model_circuit_breaker,
            compaction_policy=policy,
            compactor=settings.compactor,
            retry_event_sink=retry_store.append,
            # Model boundary events use the same encrypted Session Journal as
            # retry events. They intentionally carry no retryId, so the Retry
            # projection ignores them while the unified timeline retains every
            # Agent/Router/Recovery request start and terminal outcome.
            durable_event_sink=retry_store.append,
            telemetry=host.telemetry,
            pricing=settings.pricing,
            max_buffer_size=settings.model_event_buffer_size,
            max_buffer_bytes=settings.model_event_buffer_bytes,
        )
        # Close/drain the Runtime before the bound HTTP Provider (resources are
        # released in reverse ownership order).
        host.resources.own(host.model_runtime)
        host.tool_dispatch_runtime = ToolDispatchRuntime(
            settings.tools,
            before_tool_call=settings.before_tool_call,
            after_tool_call=settings.after_tool_call,
            retry_event_sink=retry_store.append,
            default_tool_timeout_seconds=settings.default_tool_timeout_seconds,
            max_parallel_tools=settings.max_parallel_tools or 64,
            default_lock_timeout_seconds=settings.default_lock_timeout_seconds,
            tenant_limits=settings.tenant_tool_limits,
            distributed_lock_backend=settings.resource_lock_backend,
            max_update_tasks=settings.max_tool_update_tasks,
            telemetry=host.telemetry,
            default_tenant_id=settings.tenant_id,
        )
        host.agent = Agent(
            model=settings.model,
            stream_fn=host.model_runtime.stream,
            system_prompt=settings.system_prompt,
            tools=settings.tools,
            before_tool_call=settings.before_tool_call,
            after_tool_call=settings.after_tool_call,
            max_turns=settings.max_turns,
            max_tool_calls=settings.max_tool_calls,
            max_parallel_tools=settings.max_parallel_tools,
            default_tool_timeout_seconds=settings.default_tool_timeout_seconds,
            retry_event_sink=retry_store.append,
            tool_runtime=host.tool_dispatch_runtime,
            tenant_id=settings.tenant_id,
        )

        if settings.router is not None:
            if settings.capabilities is None:
                raise ValueError("使用 Router 时必须提供 CapabilityRegistry")
            host_router = settings.router
            if isinstance(settings.router, HybridModelRouter):
                def router_durable_metadata() -> dict[str, str]:
                    values = {
                        "sessionId": settings.session_id,
                        "operationId": host.operation_recorder.operation_id,
                        "runId": host.runtime_tracker.state.run_id,
                    }
                    return {
                        key: value
                        for key, value in values.items()
                        if isinstance(value, str) and value
                    }

                host_router = settings.router.bind_runtime(
                    stream_fn=host.model_runtime.stream,
                    retry_event_sink=retry_store.append,
                    durable_metadata_provider=router_durable_metadata,
                )
            host.routed_agent = RoutedAgent(
                host.agent,
                host_router,
                settings.capabilities,
                runtime_tracker=host.runtime_tracker,
                operation_recorder=host.operation_recorder,
                suspend_on_approval=True,
            )
        else:
            host.routed_agent = None
            host.agent.subscribe(host.operation_recorder.listener)
            host.agent.subscribe(host.runtime_tracker.listener)

        host.tool_runtime = RecoverableToolRuntime(
            model=settings.model,
            tools=settings.tools,
            before_tool_call=settings.before_tool_call,
            after_tool_call=settings.after_tool_call,
            authorize_never=settings.authorize_never_replay,
            retry_event_sink=retry_store.append,
            default_tool_timeout_seconds=settings.default_tool_timeout_seconds,
            runtime=host.tool_dispatch_runtime,
        )
        callbacks = build_recovery_callbacks(
            model_runtime=host.model_runtime,
            tool_runtime=host.tool_runtime,
            reconcile_tool=settings.reconcile_tool,
        )
        host.startup_recovery = StartupRecoveryCoordinator(
            host.operation_store,
            callbacks,
            session_id=settings.session_id,
            telemetry=host.telemetry,
        )
        host.approvals = ApprovalService(
            host.operation_store,
            session_id=settings.session_id,
        )
        host.approval_resume = ApprovalResumeCoordinator(
            host.operation_store,
            host.approvals,
            session_id=settings.session_id,
        )
        host.writes = WriteOperationService(host.operation_store, host.approvals)
        host.approval_workflow = DurableApprovalWorkflow(host)
        await self._recover_approvals(host, settings)
        host.startup_recovery_report = (
            await host.startup_recovery.recover_all()
            if settings.auto_recover
            else None
        )
        host.lifecycle = DurableHostLifecycle(host.agent, host.resources)
        return host

    async def _recover_approvals(
        self,
        host: Any,
        settings: DurableHostSettings,
    ) -> None:
        host.recovered_approval_resumes = ()
        handler = settings.approval_resume_handler
        if settings.auto_recover and handler is not None:
            async def resume_approval(payload: dict[str, Any]) -> Any:
                value = handler(payload)
                if hasattr(value, "__await__"):
                    return await value
                return value

            recovered = await host.approval_resume.recover_incomplete(
                resume_approval,
                consumer_resolver=settings.approval_consumer_resolver,
            )
            host.recovered_approval_resumes = tuple(recovered)
        host.pending_approval_resumes = tuple(
            await host.approval_resume.pending_recovery_ids()
        )


def _managed_resources(settings: DurableHostSettings) -> tuple[Any, ...]:
    """Return explicit resources plus the pooled HTTP provider behind StreamFn.

    ``create_provider`` returns a bound ``OpenAICompatibleProvider.stream``.
    Detecting that exact provider type lets the Host own and close its shared
    connection pool by default without taking ownership of arbitrary user
    callbacks or their enclosing objects.
    """

    resources = list(settings.owned_resources)
    provider = getattr(settings.stream_fn, "__self__", None)
    if isinstance(provider, OpenAICompatibleProvider) and all(
        item is not provider for item in resources
    ):
        resources.append(provider)
    return tuple(resources)


__all__ = ["DurableHostFactory", "DurableHostSettings"]
