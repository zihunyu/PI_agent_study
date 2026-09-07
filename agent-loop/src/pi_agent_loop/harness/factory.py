"""DurableAgentHost 的对象图装配。

Host 本身只保留稳定的 Facade API；Provider、统一 Journal、模型/工具 Runtime、
Recovery 与业务协调器都在这里完成一次性装配，避免不同执行路径各造一套 Runtime。
"""

from __future__ import annotations

from ..routing.routed_agent import RuntimeBoundRouter
from types import MethodType

from ..planning.store_protocol import JournalPlanStore
from ..session.journal import validate_session_event_journal

from ..execution_policy import ExecutionPolicy, guard_tool_output

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..agent import Agent
from ..approval import ApprovalService
from ..planning import (
    ApprovalBarrier,
    ClosedLoopBudget,
    DurablePlanStore,
    DurablePlanStoreConfigurationError,
    HybridRequestPlanner,
    IntentPlanPolicy,
    SessionJournalPlanStore,
    validate_durable_plan_store,
)
from ..retry.circuit_breaker import CircuitBreaker
from ..retry.compaction import CompactionRetryPolicy, ContextReplacement
from ..retry.types import ModelRetryPolicy
from ..routing.capabilities import CapabilityRegistry
from ..routing.routed_agent import RoutedAgent
from ..runtime import RuntimeStateTracker, Telemetry
from ..session import (
    ConversationSessionNotFoundError,
    DurableOperationRecorder,
    InMemoryOperationEventStore,
    JournalKeyProvider,
    JournalPrincipal,
    RuntimeRecoveryManager,
    SessionConfigurationMismatchError,
    SessionContextProjection,
    WorkspaceSessionCatalog,
)
from ..session.operation_state import replay_operation
from ..session.operation_store import ClaimLease
from ..tool_runtime import (
    ResourceLockBackend,
    ResourceLockBackendCapabilities,
    ToolDispatchRuntime,
)
from ..security import VerifiedIdentity, VerifiedIdentityValidator
from ..types import AgentTool, Model, StreamFn, ToolAuthorization, ToolDispatchContext
from ..writes import WriteOperationService
from .approval import DurableApprovalWorkflow
from .autonomous import AutonomousPlanRunner
from .autonomous_durability import (
    AutonomousConversationProjector,
    SessionJournalAutonomousRunStore,
)
from .approval_gateway import ApprovalResumeCoordinator, invoke_fenced_callback
from .lifecycle import DurableHostLifecycle
from .model_runtime_adapter import RecoverableModelRuntime, TokenPricing
from .plans import DurablePlanWorkflow
from .plan_tool_runtime import ToolRuntimePlanStepExecutor
from .recovery import build_recovery_callbacks
from .resources import (
    DurableHostResourceFactory,
    DurableHostResources,
    DurableResourceRequest,
)
from .session_runtime import SessionWriterLease, agent_configuration_hash
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
    router_policy_version: str = "1"
    approval_policy_version: str = "1"
    plan_policy_version: str = "1"
    security_policy_version: str = "1"
    execution_policy: ExecutionPolicy | None = None
    before_tool_call: Callable[..., Any] | None = None
    after_tool_call: Callable[..., Any] | None = None
    reconcile_tool: Callable[..., Any] | None = None
    reconcile_write: Callable[..., Any] | None = None
    authorize_never_replay: Callable[..., Any] | None = None
    tool_identity: VerifiedIdentity | None = None
    approval_identity_validator: VerifiedIdentityValidator | None = None
    tool_authorization: ToolAuthorization | None = None
    require_tool_identity: bool = False
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
    approval_resume_handler: Callable[..., Any] | None = None
    approval_consumer_resolver: Callable[[Any], Any] | None = None
    store_backend: Literal["journal", "sqlite", "jsonl"] = "journal"
    journal_key_provider: JournalKeyProvider | None = None
    journal_principal: JournalPrincipal | None = None
    tenant_id: str = "local"
    owned_resources: tuple[Any, ...] = field(default_factory=tuple)
    resource_factory: DurableHostResourceFactory | None = None
    planner: HybridRequestPlanner | None = None
    plan_policies: Mapping[str, IntentPlanPolicy] | None = None
    plan_step_executor: Callable[..., Any] | None = None
    plan_tool_bindings: Mapping[str, str] | None = None
    plan_write_metadata_provider: Callable[..., Any] | None = None
    plan_approval_barrier: ApprovalBarrier | None = None
    max_parallel_plan_steps: int = 4
    plan_lease_seconds: float = 30
    auto_plan_complex_requests: bool = True
    plan_result_validator: Any | None = None
    plan_replanner: Any | None = None
    plan_result_synthesizer: Any | None = None
    plan_correction_budget: ClosedLoopBudget | None = None
    plan_usage_meter: Any | None = None
    project_id: str | None = None
    workspace_path: str | Path | None = None
    session_title: str | None = None
    agent_profile: str = "default"
    resume_history: bool = True
    managed_session: bool = False
    exclusive_session: bool = True
    session_writer_lease_seconds: float = 30
    allow_configuration_migration: bool = False
    # Caller-owned Store. The Host validates but never closes it.
    plan_store: DurablePlanStore | None = None
    # Strict multi-host Plan execution. Local SQLite deliberately fails this.
    distributed_execution: bool = False


def _approval_required_tool_names(settings: DurableHostSettings) -> tuple[str, ...]:
    """Return every configured tool that cannot safely run without a Router."""

    unsafe_tools = {tool.name for tool in settings.tools if tool.requires_approval}
    if settings.capabilities is not None:
        unsafe_tools.update(
            entry.tool.name
            for entry in settings.capabilities.all_entries()
            if entry.tool.requires_approval
            or entry.requires_approval
            or entry.operation.casefold() != "read"
        )
    return tuple(sorted(unsafe_tools))


def _effective_plan_policies(
    settings: DurableHostSettings,
) -> dict[str, IntentPlanPolicy]:
    """Resolve the one trusted Plan policy catalogue used by every boundary.

    ``DurablePlanWorkflow`` historically fell back to ``planner.policies`` while
    Factory validation and the managed-Session configuration hash only inspected
    ``settings.plan_policies``.  That split allowed a dangerous effective policy
    to bypass assembly checks and to be omitted from the configuration identity.
    """

    configured = (
        None if settings.plan_policies is None else dict(settings.plan_policies)
    )
    planned = (
        None if settings.planner is None else dict(settings.planner.policies)
    )
    if configured is not None and planned is not None and configured != planned:
        raise ValueError(
            "plan_policies 与 planner.policies 不一致；禁止不同安全边界使用不同策略"
        )
    return configured if configured is not None else (planned or {})


def _validate_distributed_plan_configuration(
    settings: DurableHostSettings,
) -> None:
    """Fail closed when a Host claims a topology its adapters do not provide."""

    if type(settings.distributed_execution) is not bool:
        raise TypeError("distributed_execution 必须是布尔值")
    if settings.plan_store is not None:
        validate_durable_plan_store(
            settings.plan_store,
            tenant_id=settings.tenant_id,
            session_id=settings.session_id,
            require_multi_host=settings.distributed_execution,
        )
    elif settings.distributed_execution:
        raise DurablePlanStoreConfigurationError(
            "distributed_execution=True 必须显式注入共享 "
            "plan_store；Host 默认 SQLite 只支持单机多进程"
        )

    policies = _effective_plan_policies(settings)
    dangerous_intents = {
        intent
        for intent, policy in policies.items()
        if policy.write
        or policy.requires_approval
        or policy.replay_policy == "never"
    }
    if dangerous_intents and settings.plan_tool_bindings is None:
        raise ValueError(
            "危险 Durable Plan 必须通过 plan_tool_bindings 进入 "
            "ToolDispatchRuntime，禁止直接 Step Callback"
        )

    tool_by_name = {tool.name: tool for tool in settings.tools}
    dangerous_tool_names: set[str] = set()
    # Strict distributed mode also protects dangerous calls outside a Plan.
    # The ordinary single-host mode only enables multi-worker guarantees for
    # tools that a durable dangerous Plan can actually dispatch.
    if settings.distributed_execution:
        dangerous_tool_names.update(
            tool.name
            for tool in settings.tools
            if tool.requires_approval or tool.replay_policy == "never"
        )
    if settings.distributed_execution and settings.capabilities is not None:
        dangerous_tool_names.update(
            entry.tool.name
            for entry in settings.capabilities.all_entries()
            if entry.approval_required or entry.has_side_effect
        )
    if settings.plan_tool_bindings is not None:
        for intent in dangerous_intents:
            tool_name = settings.plan_tool_bindings.get(intent)
            if tool_name is None:
                raise ValueError(
                    f"危险 Durable Plan 缺少 Tool Binding：{intent}"
                )
            dangerous_tool_names.add(tool_name)

    if not dangerous_tool_names:
        return
    backend = settings.resource_lock_backend
    capabilities = getattr(backend, "capabilities", None)
    if not isinstance(capabilities, ResourceLockBackendCapabilities):
        raise ValueError(
            "危险 Durable Plan Tool 必须注入声明能力的 "
            "resource_lock_backend"
        )
    if not (
        capabilities.supports_cross_process
        and capabilities.atomic_multi_resource_acquire
        and capabilities.supports_lease_renewal
        and capabilities.supports_fencing_tokens
        and (
            not settings.distributed_execution
            or capabilities.supports_multi_host
        )
    ):
        raise ValueError(
            "危险 Durable Plan Tool 需要跨进程、原子多资源获取、可续租且"
            "能产生单调 fencing token 的 resource_lock_backend；分布式模式"
            "还必须支持多机"
        )
    for tool_name in sorted(dangerous_tool_names):
        tool = tool_by_name.get(tool_name)
        if tool is None:
            raise ValueError(f"危险 Plan 绑定了未注册 Tool：{tool_name}")
        if tool.execution_mode != "resource_locked":
            raise ValueError(
                f"危险 Durable Plan Tool {tool_name} 必须使用 "
                "execution_mode='resource_locked' 和稳定 Resource Key"
            )
        if not tool.supports_resource_fencing:
            raise ValueError(
                f"危险 Durable Plan Tool {tool_name} 必须显式声明并实现 "
                "supports_resource_fencing=True；下游必须按资源 token/scope "
                "拒绝旧 Worker"
            )


class DurableHostFactory:
    """构建一个共享 Model/Tool Runtime 的完整 Host。"""

    async def create(self, host_type: type[Any], settings: DurableHostSettings) -> Any:
        if not settings.session_id:
            raise ValueError("session_id 不能为空")
        if settings.execution_policy is not None:
            if not isinstance(settings.execution_policy, ExecutionPolicy):
                raise TypeError("execution_policy must be ExecutionPolicy")
            settings.after_tool_call = guard_tool_output(settings.execution_policy.content_safety, settings.after_tool_call, tenant_id=settings.tenant_id, session_id=settings.session_id)
        # Resolve this before opening stores.  It also rejects conflicting
        # catalogues before either one can influence a security decision.
        _effective_plan_policies(settings)
        _validate_distributed_plan_configuration(settings)
        # This is a pure configuration invariant, so reject it before opening
        # stores or binding a managed Session configuration hash.  Keeping the
        # same check in ``_assemble`` protects against future assembly changes.
        if settings.router is None:
            unsafe_tools = _approval_required_tool_names(settings)
            if unsafe_tools:
                raise ValueError(
                    "无 Router 的 Host 不能装配需要 Approval 的工具："
                    + ", ".join(unsafe_tools)
                )
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
                    operation_store=InMemoryOperationEventStore(),
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
        effective_plan_policies = _effective_plan_policies(settings)
        if not settings.exclusive_session:
            raise ValueError(
                "DurableAgentHost 禁止关闭 Session 单写者保护；"
                "无持久 Session 的并行实验请直接使用 Agent"
            )
        host.model = settings.model
        host.tool_identity = settings.tool_identity
        host.telemetry = settings.telemetry or Telemetry()
        host.resources = await _create_resources(settings)
        if host.resources.journal is not None:
            validate_session_event_journal(host.resources.journal, require_multi_host=settings.distributed_execution and settings.auto_plan_complex_requests and settings.planner is not None)
            for store in (host.resources.operation_store, host.resources.runtime_store, host.resources.retry_store):
                if getattr(store, "journal", None) is not host.resources.journal or getattr(store, "principal", None) != host.resources.journal_principal:
                    raise ValueError("Host logical stores must share the same Journal and Principal")
                if getattr(store, "session_id", settings.session_id) != settings.session_id:
                    raise ValueError("Host logical store session scope mismatch")
        for resource in _managed_resources(settings):
            host.resources.own(resource)
        # ``plan_store=`` is an explicit caller-owned injection boundary.  Do
        # not close it even if a generic resource bundle/owned_resources list
        # happened to contain the same object.
        if settings.plan_store is not None:
            host.resources.disown(settings.plan_store)

        runtime_store = host.resources.runtime_store
        host.operation_store = host.resources.operation_store
        retry_store = host.resources.retry_store
        host.project_id = settings.project_id
        host.session_catalog = (
            WorkspaceSessionCatalog(
                host.resources.journal,
                host.resources.journal_principal,
            )
            if host.resources.journal is not None
            and host.resources.journal_principal is not None
            else None
        )
        host.context_projection = SessionContextProjection(
            host.operation_store,
            catalog=host.session_catalog if settings.managed_session else None,
        )
        host.session_writer_lease = None
        host.session_writer_lease = await SessionWriterLease.acquire(
            host.operation_store,
            settings.session_id,
            lease_seconds=settings.session_writer_lease_seconds,
        )
        host.resources.own(host.session_writer_lease)

        host.session_metadata = await self._prepare_session(host, settings)
        initial_messages: list[dict[str, Any]] = []
        if settings.resume_history:
            context = await host.context_projection.project(settings.session_id)
            initial_messages = context.copy_messages()
        default_plan_store = (
            SessionJournalPlanStore(
                host.resources.journal,
                host.resources.journal_principal,
                session_id=settings.session_id,
            )
            if host.resources.journal is not None
            and host.resources.journal_principal is not None
            else None
        )
        host.plan_store = (
            settings.plan_store
            if settings.plan_store is not None
            else default_plan_store
        )
        if host.plan_store is not None:
            host.plan_store = validate_durable_plan_store(
                host.plan_store,
                tenant_id=settings.tenant_id,
                session_id=settings.session_id,
                require_multi_host=settings.distributed_execution,
            )
        host.distributed_execution = settings.distributed_execution
        host.autonomous_run_store = (
            SessionJournalAutonomousRunStore(
                host.resources.journal,
                host.resources.journal_principal,
                session_id=settings.session_id,
            )
            if host.resources.journal is not None
            and host.resources.journal_principal is not None
            else None
        )
        host.autonomous_conversation_projector = AutonomousConversationProjector(
            host.operation_store,
            session_id=settings.session_id,
            model=settings.model,
            fenced_lease=host.session_writer_lease.claim_lease,
            fenced_lease_seconds=settings.session_writer_lease_seconds,
        )
        effective_plan_executor = settings.plan_step_executor
        if settings.plan_tool_bindings is not None:
            if settings.plan_step_executor is not None:
                raise ValueError(
                    "plan_tool_bindings 与 plan_step_executor 不能同时配置"
                )
            missing_bindings = set(effective_plan_policies) - set(
                settings.plan_tool_bindings
            )
            if missing_bindings:
                raise ValueError(
                    "以下 Plan Intent 缺少可信 Tool Binding："
                    + ", ".join(sorted(missing_bindings))
                )
            dangerous_plan = any(
                policy.write
                or policy.requires_approval
                or policy.replay_policy == "never"
                for policy in effective_plan_policies.values()
            )
            if dangerous_plan and settings.tool_identity is None:
                raise ValueError(
                    "危险 Durable Plan 必须配置可恢复的 tool_identity"
                )
            effective_plan_executor = ToolRuntimePlanStepExecutor(
                runtime_provider=lambda: host.tool_dispatch_runtime,
                tools=settings.tools,
                intent_tools=settings.plan_tool_bindings,
                model=settings.model,
                session_id=settings.session_id,
                dispatch_context_provider=lambda: (
                    host.agent.tool_dispatch_context
                    if hasattr(host, "agent")
                    else ToolDispatchContext(
                        identity=settings.tool_identity,
                        tenant_id=settings.tenant_id,
                    )
                ),
                policies=effective_plan_policies,
                capabilities=settings.capabilities,
                write_service_provider=lambda: host.writes,
                write_metadata_provider=settings.plan_write_metadata_provider,
            )
        elif settings.plan_step_executor is not None and any(
            policy.write
            or policy.requires_approval
            or policy.replay_policy == "never"
            for policy in effective_plan_policies.values()
        ):
            raise ValueError(
                "危险 Plan 禁止直接使用 plan_step_executor；"
                "请通过 plan_tool_bindings 接入统一 ToolDispatchRuntime"
            )
        host.plan_workflow = DurablePlanWorkflow(
            store=host.plan_store,
            planner=settings.planner,
            policies=effective_plan_policies,
            step_executor=effective_plan_executor,
            approval_barrier=settings.plan_approval_barrier,
            max_parallel_steps=settings.max_parallel_plan_steps,
            lease_seconds=settings.plan_lease_seconds,
            distributed_execution=settings.distributed_execution,
            dispatch_context_provider=lambda: (
                host.agent.tool_dispatch_context
                if hasattr(host, "agent")
                else ToolDispatchContext(
                    identity=settings.tool_identity,
                    tenant_id=settings.tenant_id,
                )
            ),
        )
        autonomous_requested = (
            settings.auto_plan_complex_requests
            and settings.planner is not None
            and effective_plan_executor is not None
            and host.plan_store is not None
        )
        if autonomous_requested and (
            not isinstance(host.plan_store, JournalPlanStore)
            or host.autonomous_run_store is None
            or host.plan_store.journal is not host.autonomous_run_store.journal
            or host.plan_store.principal != host.autonomous_run_store.principal
            or host.plan_store.session_id != host.autonomous_run_store.session_id
        ):
            raise ValueError(
                "Autonomous Plan 需要 SessionJournalPlanStore 与 Run/"
                "Conversation 在同一事务中原子发布；自定义 "
                "DurablePlanStore 尚未接入该三流事务边界"
            )
        host.autonomous_plan_runner = (
            AutonomousPlanRunner(
                host.plan_workflow,
                result_validator=settings.plan_result_validator,
                replanner=settings.plan_replanner,
                result_synthesizer=settings.plan_result_synthesizer,
                budget=settings.plan_correction_budget,
                run_store=host.autonomous_run_store,
                usage_meter=settings.plan_usage_meter,
            )
            if autonomous_requested
            else None
        )
        configured_plan_budget = settings.plan_correction_budget
        hard_model_budget = configured_plan_budget is not None and any(
            value is not None
            for value in (
                configured_plan_budget.max_model_calls,
                configured_plan_budget.max_tokens,
                configured_plan_budget.max_cost,
            )
        )
        if hard_model_budget and settings.router is not None:
            if host.autonomous_plan_runner is None:
                raise ValueError(
                    "Router 硬 model/token/cost 预算需要完整 Autonomous "
                    "Plan Runner 和 Durable Run Store"
                )
            if not isinstance(settings.router, RuntimeBoundRouter):
                raise ValueError(
                    "自定义 Router 尚未接入 pre-route durable usage admission；"
                    "启用硬 model/token/cost 预算时拒绝装配"
                )
        await RuntimeRecoveryManager(
            runtime_store,
            claim_store=host.operation_store,
            claim_resource_id=settings.session_id,
            claim_lease_seconds=settings.session_writer_lease_seconds,
        ).recover()
        host.runtime_tracker = await RuntimeStateTracker.create(
            runtime_store,
            fenced_claim=host.session_writer_lease.claim_lease,
            fenced_claim_lease_seconds=settings.session_writer_lease_seconds,
        )
        host.operation_recorder = DurableOperationRecorder(
            host.operation_store,
            session_id=settings.session_id,
            tools=settings.tools,
            configuration={
                "provider": settings.model.provider,
                "model": settings.model.id,
            },
            run_id_provider=lambda: host.runtime_tracker.state.run_id,
            fenced_lease=host.session_writer_lease.claim_lease,
            fenced_lease_seconds=settings.session_writer_lease_seconds,
        )

        def durable_metadata_provider() -> dict[str, str]:
            """从可信 Host 状态生成模型请求关联标识。"""

            return host.operation_recorder.current_durable_metadata()

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
            execution_policy=settings.execution_policy,
            tenant_id=settings.tenant_id,
            session_id=settings.session_id,
        )
        # Close/drain the Runtime before the bound HTTP Provider (resources are
        # released in reverse ownership order).
        host.resources.own(host.model_runtime)
        def stage_stream(stage: str) -> StreamFn:
            def stream(runtime: Any, model: Model, context: dict[str, Any], options: dict[str, Any]) -> Any:
                return runtime.stream(model, context, {**options, "model_request_source": stage})
            return MethodType(stream, host.model_runtime)

        if settings.planner is not None and callable(getattr(settings.planner, "bind_runtime", None)):
            host.plan_workflow.planner = getattr(settings.planner, "bind_runtime")(stream_fn=stage_stream("planner"))
        if host.autonomous_plan_runner is not None:
            for name in ("result_validator", "replanner", "result_synthesizer"):
                component = getattr(host.autonomous_plan_runner, name)
                binder = getattr(component, "bind_runtime", None)
                if callable(binder):
                    bound = binder(stream_fn=stage_stream(name))
                    if bound is component or not callable(bound):
                        raise ValueError("Model component bind_runtime must return an independent callable")
                    setattr(host.autonomous_plan_runner, name, bound)
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
            suppress_unreviewed_updates=settings.execution_policy is not None and settings.execution_policy.content_safety is not None,
            telemetry=host.telemetry,
            default_tenant_id=settings.tenant_id,
            authorization=settings.tool_authorization,
            require_identity=settings.require_tool_identity,
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
            tool_identity=settings.tool_identity,
            tool_authorization=settings.tool_authorization,
            require_tool_identity=settings.require_tool_identity,
            messages=initial_messages,
            durable_metadata_provider=durable_metadata_provider,
        )
        if host.session_writer_lease is not None:
            host.session_writer_lease.set_loss_callback(
                lambda: host.agent.abort("Session Writer Lease 已丢失")
            )

        if settings.router is not None:
            if settings.capabilities is None:
                raise ValueError("使用 Router 时必须提供 CapabilityRegistry")
            host_router = settings.router
            if isinstance(settings.router, RuntimeBoundRouter):
                host_router = settings.router.bind_runtime(
                    stream_fn=stage_stream("router"),
                    retry_event_sink=retry_store.append,
                    durable_metadata_provider=durable_metadata_provider,
                )
                if host_router is settings.router or not isinstance(host_router, RuntimeBoundRouter):
                    raise ValueError("Router bind_runtime must return an independent RuntimeBoundRouter instance")
            host.routed_agent = RoutedAgent(
                host.agent,
                host_router,
                settings.capabilities,
                runtime_tracker=host.runtime_tracker,
                operation_recorder=host.operation_recorder,
                suspend_on_approval=True,
            )
        else:
            unsafe_tools = _approval_required_tool_names(settings)
            if unsafe_tools:
                raise ValueError(
                    "无 Router 的 Host 不能装配需要 Approval 的工具："
                    + ", ".join(unsafe_tools)
                )
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
        host.approvals = ApprovalService(
            host.operation_store,
            session_id=settings.session_id,
            identity_validator=settings.approval_identity_validator,
        )
        if settings.tool_identity is not None:
            host.approvals.assert_trusted_identity(
                settings.tool_identity,
                purpose="Durable Host tool identity",
            )
        host.approval_resume = ApprovalResumeCoordinator(
            host.operation_store,
            host.approvals,
            session_id=settings.session_id,
        )
        host.writes = WriteOperationService(host.operation_store, host.approvals)
        callbacks = build_recovery_callbacks(
            model_runtime=host.model_runtime,
            tool_runtime=host.tool_runtime,
            reconcile_tool=settings.reconcile_tool,
            write_service=host.writes,
            reconcile_write=settings.reconcile_write,
        )

        async def resume_autonomous(
            plan_id: str,
            expected_run_id: str,
        ) -> str:
            if host.autonomous_plan_runner is None:
                raise RuntimeError("未配置 AutonomousPlanRunner，无法恢复")
            result = await host.autonomous_plan_runner.resume(plan_id)
            if result.closed_loop_run_id != expected_run_id:
                raise RuntimeError("Autonomous Run 恢复身份不匹配")
            link = await host.autonomous_conversation_projector.find(
                expected_run_id
            )
            if link is None:
                raise RuntimeError("Autonomous Run 缺少 Conversation Link")
            if result.status != "waiting_approval":
                await host.autonomous_conversation_projector.finalize(
                    link,
                    response_text=result.response_text,
                    status=result.status,
                )
            else:
                if host.plan_store is None:
                    raise RuntimeError("Waiting Autonomous Plan 缺少 Plan Store")
                completion = await host.plan_store.load(result.plan_id)
                await host.autonomous_conversation_projector.project_waiting(
                    link,
                    response_text=result.response_text,
                    projection_key=(
                        completion.completion_envelope.delivery_id
                        if completion.completion_envelope is not None
                        else str(completion.completion_generation)
                    ),
                )
            await host.autonomous_plan_runner.acknowledge_run_completions(
                expected_run_id
            )
            return result.status

        host.startup_recovery = StartupRecoveryCoordinator(
            host.operation_store,
            callbacks,
            session_id=settings.session_id,
            telemetry=host.telemetry,
            autonomous_resume=resume_autonomous,
        )
        host.approval_workflow = DurableApprovalWorkflow(host)
        inspection = (
            await host.startup_recovery.inspect_all()
            if settings.auto_recover
            else None
        )
        # Never run a recovery callback or resume an Approval side effect
        # until the complete Session has been replayed and inspected.  A
        # blocker in a later Operation must fence callbacks for earlier ones.
        host.startup_recovery_report = inspection
        await self._recover_approvals(
            host,
            settings,
            allow_resume=(
                inspection is not None and not inspection.hard_blocked
            ),
        )
        if inspection is not None and not inspection.hard_blocked:
            host.startup_recovery_report = (
                await host.startup_recovery.recover_all()
            )
        if settings.resume_history:
            # Startup recovery can append a model/tool terminal fact.  Reload
            # once more so the in-memory Agent starts from the recovered view.
            context = await host.context_projection.project(settings.session_id)
            host.agent.state.messages = context.copy_messages()
        host.lifecycle = DurableHostLifecycle(host.agent, host.resources)
        return host

    async def _prepare_session(
        self,
        host: Any,
        settings: DurableHostSettings,
    ) -> Any | None:
        if not settings.managed_session:
            return None
        if host.session_catalog is None:
            raise ValueError("受管 Project/Session 只支持 journal Store Backend")
        workspace_path = Path(settings.workspace_path or Path.cwd()).resolve(strict=True)
        if not workspace_path.is_dir():
            raise ValueError(f"workspace_path 不是目录：{workspace_path}")
        digest = agent_configuration_hash(
            model=settings.model,
            system_prompt=settings.system_prompt,
            tools=settings.tools,
            router=settings.router,
            capabilities=settings.capabilities,
            plan_policies=_effective_plan_policies(settings),
            plan_tool_bindings=settings.plan_tool_bindings,
            plan_budget=settings.plan_correction_budget,
            router_policy_version=settings.router_policy_version,
            approval_policy_version=settings.approval_policy_version,
            plan_policy_version=settings.plan_policy_version,
            security_policy_version=settings.security_policy_version,
            execution_policy_version=None if settings.execution_policy is None else settings.execution_policy.version,
        )
        try:
            session = await host.session_catalog.get_session(settings.session_id)
        except ConversationSessionNotFoundError:
            session = await host.session_catalog.create_session(
                project_id=settings.project_id,
                title=settings.session_title or settings.session_id,
                cwd=workspace_path,
                session_id=settings.session_id,
                agent_profile=settings.agent_profile,
                configuration_hash=digest,
                fenced_claim=host.session_writer_lease.claim_lease,
                fenced_claim_lease_seconds=settings.session_writer_lease_seconds,
            )
        if session.status != "active":
            raise ValueError(f"Session 当前不可打开：{session.status}")
        if (
            settings.allow_configuration_migration
            and session.configuration_hash is not None
            and session.configuration_hash != digest
        ):
            await self._assert_configuration_migration_safe(host, settings.session_id)
        session = await host.session_catalog.bind_session_configuration(
            settings.session_id,
            project_id=settings.project_id,
            cwd=workspace_path,
            agent_profile=settings.agent_profile,
            configuration_hash=digest,
            allow_migration=settings.allow_configuration_migration,
            fenced_claim=host.session_writer_lease.claim_lease,
            fenced_claim_lease_seconds=settings.session_writer_lease_seconds,
        )
        host.project_id = session.project_id
        return session

    @staticmethod
    async def _assert_configuration_migration_safe(host: Any, session_id: str) -> None:
        """Only terminal, valid operations may cross a configuration boundary."""

        events = await host.operation_store.load(session_id=session_id)
        grouped: dict[str, list[Any]] = {}
        for event in events:
            grouped.setdefault(event.operation_id, []).append(event)
        for operation_id, operation_events in grouped.items():
            try:
                state = replay_operation(operation_events)
            except Exception as error:
                raise SessionConfigurationMismatchError(
                    "Session Operation Log 无法验证，禁止迁移配置"
                ) from error
            if state.phase not in {"completed", "failed", "cancelled"}:
                raise SessionConfigurationMismatchError(
                    f"Session 存在未完成 Operation {operation_id}，禁止迁移配置"
                )

    async def _recover_approvals(
        self,
        host: Any,
        settings: DurableHostSettings,
        *,
        allow_resume: bool,
    ) -> None:
        host.recovered_approval_resumes = ()
        handler = settings.approval_resume_handler
        if settings.auto_recover and allow_resume and handler is not None:
            async def resume_approval(
                payload: dict[str, Any],
                *,
                fencing_token: int,
                fenced_claim: ClaimLease | None = None,
            ) -> Any:
                return await invoke_fenced_callback(
                    handler,
                    payload,
                    fencing_token=fencing_token,
                    fenced_claim=fenced_claim,
                )

            recovered = await host.approval_resume.recover_incomplete(
                resume_approval,
                consumer_resolver=settings.approval_consumer_resolver,
            )
            host.recovered_approval_resumes = tuple(recovered)
        host.pending_approval_resumes = tuple(
            await host.approval_resume.pending_recovery_ids()
        )


def _managed_resources(settings: DurableHostSettings) -> tuple[Any, ...]:
    """Return explicit resources plus an opt-in provider behind StreamFn.

    Provider implementations remain independent from the Harness. A bound
    provider is Host-owned only when it explicitly declares
    ``manage_with_host=True`` and exposes ``aclose``; arbitrary callbacks are
    never captured implicitly.
    """

    resources = [
        item
        for item in settings.owned_resources
        if item is not settings.plan_store
    ]
    provider = getattr(settings.stream_fn, "__self__", None)
    if (
        provider is not None
        and getattr(provider, "manage_with_host", False) is True
        and callable(getattr(provider, "aclose", None))
        and all(
        item is not provider for item in resources
        )
    ):
        resources.append(provider)
    return tuple(resources)


async def _create_resources(settings: DurableHostSettings) -> DurableHostResources:
    if settings.resource_factory is None:
        return DurableHostResources.create(
            settings.state_dir,
            session_id=settings.session_id,
            store_backend=settings.store_backend,
            journal_key_provider=settings.journal_key_provider,
            journal_principal=settings.journal_principal,
            tenant_id=settings.tenant_id,
        )
    value = settings.resource_factory(
        DurableResourceRequest(
            state_dir=Path(settings.state_dir),
            session_id=settings.session_id,
            tenant_id=settings.tenant_id,
            journal_key_provider=settings.journal_key_provider,
            journal_principal=settings.journal_principal,
        )
    )
    if hasattr(value, "__await__"):
        value = await value
    if not isinstance(value, DurableHostResources):
        raise TypeError("resource_factory 必须返回 DurableHostResources")
    if value.operation_store is None or value.runtime_store is None or value.retry_store is None:
        raise ValueError("自定义 DurableHostResources 必须提供 operation/runtime/retry store")
    return value


__all__ = ["DurableHostFactory", "DurableHostSettings"]
