"""Agent Loop 的核心类型。

这些 dataclass 对应 Pi TypeScript 实现中的 Model、AgentContext、AgentTool、
AgentLoopConfig 和各种 hook 上下文。消息与事件使用 dict，以便直接序列化为
JSON，并允许应用自定义消息角色。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

from .cancellation import CancellationToken
from .retry.types import ToolRetryPolicy
from .security import VerifiedIdentity

ToolExecutionMode: TypeAlias = Literal["sequential", "parallel"]
ToolExecutionPolicyMode: TypeAlias = Literal[
    "parallel",
    "exclusive",
    "resource_locked",
    "sequential",  # 向后兼容别名，调度时等价于 exclusive。
]
ToolReplayPolicy: TypeAlias = Literal["never", "safe"]
ToolResourceAccess: TypeAlias = Literal["read", "write"]
QueueMode: TypeAlias = Literal["all", "one-at-a-time"]
ThinkingLevel: TypeAlias = Literal[
    "off", "minimal", "low", "medium", "high", "xhigh", "max"
]
AgentMessage: TypeAlias = dict[str, Any]
AgentEvent: TypeAlias = dict[str, Any]

# 回调可以同步返回，也可以返回 Awaitable；loop.py 会统一 await。
MaybeAwaitable: TypeAlias = Any | Awaitable[Any]
EventSink: TypeAlias = Callable[[AgentEvent], MaybeAwaitable]
ToolUpdateCallback: TypeAlias = Callable[["AgentToolResult"], None]


@dataclass(slots=True)
class Model:
    """Agent Loop 真正需要的最小模型信息。"""

    id: str
    provider: str
    api: str = "custom"
    name: str | None = None
    context_window: int = 0
    max_tokens: int = 0
    reasoning: bool = False


@dataclass(slots=True)
class AgentToolResult:
    """工具最终或部分执行结果。"""

    content: list[dict]
    details: Any = None
    usage: dict | None = None
    added_tool_names: list[str] | None = None
    terminate: bool | None = None


@dataclass(frozen=True, slots=True)
class AgentTool:
    """Agent 可执行的工具定义。

    ``validate_args`` 相当于 TypeScript 版本的 schema validation。为了保持
    标准库零依赖，这里让调用方注入验证函数。验证失败时直接抛 ValueError。
    """

    name: str
    label: str
    description: str
    execute: Callable[
        [str, Any, CancellationToken, ToolUpdateCallback],
        Awaitable[AgentToolResult],
    ] | None
    # parameters 保存给模型看的 JSON Schema；validate_args 负责真正运行时校验。
    parameters: dict[str, Any] = field(default_factory=dict)
    validate_args: Callable[[Any], Any] = lambda value: value
    prepare_arguments: Callable[[Any], Any] | None = None
    execution_mode: ToolExecutionPolicyMode | None = None
    # resource_locked 工具根据已校验参数返回一个或多个资源键。
    resolve_resource_keys: Callable[[Any], str | list[str] | tuple[str, ...]] | None = None
    # 单工具超时优先于 Agent 的默认超时。None 表示使用 Agent 默认值。
    timeout_seconds: float | None = None
    # 只有显式幂等并声明可重试错误码的工具才能自动重试。
    retry_policy: ToolRetryPolicy | None = None
    # 崩溃发生在 Dispatch 后时，safe 才允许 Recovery 重放。
    replay_policy: ToolReplayPolicy = "never"
    # 调度优先级，数值越大越先获得并发槽；相同优先级保持 FIFO。
    priority: int = 0
    # resource_locked 工具可声明共享读或独占写，默认保持旧版独占语义。
    resource_access: ToolResourceAccess = "write"
    # 等待资源锁的最长时间；None 表示由 Runtime 使用默认值。
    lock_timeout_seconds: float | None = None
    # 可选租户解析器，用于同租户并发限制。
    resolve_tenant_id: Callable[[Any], str | None] | None = None
    # Tool 自身的审批声明属于可信装配策略。Router/Capability 只能增加审批，
    # 不能把该要求关闭。以下新字段放在末尾，保留旧版位置参数兼容性。
    requires_approval: bool = False
    # Python callable 本身无法得到跨部署稳定且不泄密的摘要；工具作者必须在
    # 实现或安全边界变化时显式升级这些版本，使受管 Session 拒绝静默混用。
    implementation_version: str = "1"
    security_policy_version: str = "1"
    # 新 Tool 可选择该一等身份入口。Runtime 会优先调用它，并传入可信
    # ToolDispatchContext；旧 execute 四参签名继续完整兼容。
    execute_with_context: Callable[
        [
            str,
            Any,
            "ToolDispatchContext",
            CancellationToken,
            ToolUpdateCallback,
        ],
        Awaitable[AgentToolResult],
    ] | None = None
    # 分布式副作用 Tool 必须显式声明它会把 Runtime 提供的资源级 fencing
    # token/scope 交给下游并由下游执行 compare-and-reject。仅仅接收取消信号
    # 或 workflow/plan generation 不能阻止已经丢锁的旧 Worker 迟到提交。
    supports_resource_fencing: bool = False

    def __post_init__(self) -> None:
        if self.execute is None and self.execute_with_context is None:
            raise ValueError("工具必须提供 execute 或 execute_with_context")
        if self.execute is not None and not callable(self.execute):
            raise TypeError("工具 execute 必须可调用或为 None")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("工具 timeout_seconds 必须大于 0")
        if self.replay_policy not in {"never", "safe"}:
            raise ValueError("工具 replay_policy 必须是 never 或 safe")
        if not isinstance(self.requires_approval, bool):
            raise ValueError("工具 requires_approval 必须是布尔值")
        if self.execute_with_context is not None and not callable(
            self.execute_with_context
        ):
            raise TypeError("工具 execute_with_context 必须可调用或为 None")
        if type(self.supports_resource_fencing) is not bool:
            raise TypeError("工具 supports_resource_fencing 必须是布尔值")
        if self.supports_resource_fencing and self.execute_with_context is None:
            raise ValueError(
                "支持 Resource Fencing 的工具必须使用 execute_with_context"
            )
        if self.supports_resource_fencing and (
            self.execution_mode != "resource_locked"
            or self.resource_access != "write"
        ):
            raise ValueError(
                "支持 Resource Fencing 的工具必须使用 resource_locked 写锁"
            )
        for name, value in (
            ("implementation_version", self.implementation_version),
            ("security_policy_version", self.security_policy_version),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"工具 {name} 必须是非空字符串")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise ValueError("工具 priority 必须是整数")
        if self.resource_access not in {"read", "write"}:
            raise ValueError("工具 resource_access 必须是 read 或 write")
        if self.lock_timeout_seconds is not None and self.lock_timeout_seconds <= 0:
            raise ValueError("工具 lock_timeout_seconds 必须大于 0")
        if self.execution_mode not in {
            None,
            "parallel",
            "exclusive",
            "resource_locked",
            "sequential",
        }:
            raise ValueError("工具 execution_mode 无效")
        if self.execution_mode == "resource_locked" and self.resolve_resource_keys is None:
            raise ValueError("resource_locked 工具必须提供 resolve_resource_keys")
        if self.execution_mode != "resource_locked" and self.resolve_resource_keys is not None:
            raise ValueError("只有 resource_locked 工具可以提供 resolve_resource_keys")
        if self.execution_mode != "resource_locked" and self.resource_access != "write":
            raise ValueError("只有 resource_locked 工具可以声明 read 资源访问")


@dataclass(frozen=True, slots=True)
class ToolDispatchContext:
    """由 Host/认证边界提供、模型参数无法伪造的 Tool 调用身份上下文。"""

    identity: VerifiedIdentity | None = None
    approval: Any = None
    tenant_id: str | None = None
    # Durable Worker 的单调所有权代际。业务 Tool 可以把它传给支持
    # fencing 的下游存储，从而拒绝已经失去 Lease 的旧 Worker。
    fencing_token: int | None = None
    # Token 只在对应 Claim 资源内单调；下游必须同时保存/比较这个作用域，
    # 不能把不同 Session、Plan 或恢复任务的相同整数混为一个全局版本。
    fencing_scope: str | None = None
    # Resource Lock 的 token 与 workflow/plan/recovery token 是两套不同的
    # 单调序列。危险 Tool 必须使用这一对资源级字段保护真实业务实体；不能
    # 拿不同 Plan 恰好相同的 generation 代替资源 fencing。
    resource_fencing_token: int | None = None
    resource_fencing_scope: str | None = None

    def __post_init__(self) -> None:
        if self.identity is not None and not isinstance(self.identity, VerifiedIdentity):
            raise TypeError("Tool Dispatch identity 必须是 VerifiedIdentity 或 None")
        if self.tenant_id is not None and (
            not isinstance(self.tenant_id, str) or not self.tenant_id.strip()
        ):
            raise ValueError("Tool Dispatch tenant_id 必须是非空字符串或 None")
        if self.tenant_id is not None:
            object.__setattr__(self, "tenant_id", self.tenant_id.strip())
        if self.fencing_token is not None and (
            isinstance(self.fencing_token, bool)
            or not isinstance(self.fencing_token, int)
            or self.fencing_token < 1
        ):
            raise ValueError("Tool Dispatch fencing_token 必须是正整数或 None")
        if self.fencing_scope is not None and (
            not isinstance(self.fencing_scope, str)
            or not self.fencing_scope.strip()
        ):
            raise ValueError("Tool Dispatch fencing_scope 必须是非空字符串或 None")
        if (self.fencing_token is None) != (self.fencing_scope is None):
            raise ValueError(
                "Tool Dispatch fencing_token 与 fencing_scope 必须同时提供"
            )
        if self.resource_fencing_token is not None and (
            isinstance(self.resource_fencing_token, bool)
            or not isinstance(self.resource_fencing_token, int)
            or self.resource_fencing_token < 1
        ):
            raise ValueError(
                "Tool Dispatch resource_fencing_token 必须是正整数或 None"
            )
        if self.resource_fencing_scope is not None and (
            not isinstance(self.resource_fencing_scope, str)
            or not self.resource_fencing_scope.strip()
        ):
            raise ValueError(
                "Tool Dispatch resource_fencing_scope 必须是非空字符串或 None"
            )
        if (self.resource_fencing_token is None) != (
            self.resource_fencing_scope is None
        ):
            raise ValueError(
                "Tool Dispatch resource_fencing_token 与 resource_fencing_scope "
                "必须同时提供"
            )


ToolAuthorization: TypeAlias = Callable[
    [AgentTool, Any, ToolDispatchContext],
    MaybeAwaitable,
]


@dataclass(slots=True)
class AgentContext:
    """一次 Agent 运行所看到的上下文快照。"""

    system_prompt: str
    messages: list[AgentMessage]
    tools: list[AgentTool] = field(default_factory=list)


@dataclass(slots=True)
class BeforeToolCallContext:
    assistant_message: AgentMessage
    tool_call: dict
    args: Any
    context: AgentContext


@dataclass(slots=True)
class BeforeToolCallResult:
    block: bool = False
    reason: str | None = None
    terminate: bool = False


@dataclass(slots=True)
class AfterToolCallContext:
    assistant_message: AgentMessage
    tool_call: dict
    args: Any
    result: AgentToolResult
    is_error: bool
    context: AgentContext


# 唯一哨兵用于区分“hook 没有提供这个字段”和“明确提供 None”。
UNSET = object()


@dataclass(slots=True)
class AfterToolCallResult:
    """after hook 对工具结果的逐字段覆盖。"""

    content: Any = UNSET
    details: Any = UNSET
    is_error: Any = UNSET
    usage: Any = UNSET
    terminate: Any = UNSET


@dataclass(slots=True)
class TurnCompletedContext:
    """一轮 assistant + tools 完成后传给策略 hook 的上下文。"""

    message: AgentMessage
    tool_results: list[AgentMessage]
    context: AgentContext
    new_messages: list[AgentMessage]


@dataclass(slots=True)
class AgentLoopTurnUpdate:
    """下一轮可替换的运行时状态。"""

    context: AgentContext | None = None
    model: Model | None = None
    thinking_level: ThinkingLevel | None = None
    # 例如第一轮强制 Tool Call 成功后，下一轮切回 auto 以允许最终文本。
    stream_options: dict[str, Any] | None = None


StreamFn: TypeAlias = Callable[
    [Model, dict[str, Any], dict[str, Any]],
    MaybeAwaitable,
]


@dataclass(slots=True)
class AgentLoopConfig:
    """低层 Agent Loop 配置和扩展钩子。"""

    model: Model
    convert_to_llm: Callable[[list[AgentMessage]], MaybeAwaitable]
    stream_options: dict[str, Any] = field(default_factory=dict)
    # Retry 元数据持久化 Hook；不得记录 Prompt、工具参数或密钥。
    retry_event_sink: Callable[[AgentEvent], MaybeAwaitable] | None = None
    thinking_level: ThinkingLevel = "off"
    tool_execution: ToolExecutionMode = "parallel"
    # 当工具没有自己的 timeout_seconds 时使用这个默认值。
    default_tool_timeout_seconds: float | None = None
    # 三项预算均按“一次 run_agent_loop 调用”计算；None 表示不限制。
    max_tool_calls: int | None = None
    max_parallel_tools: int | None = None
    max_turns: int | None = None
    # 普通 Loop 与 Recovery 可共享同一个 ToolDispatchRuntime。
    tool_runtime: Any | None = None
    # 由 Host/认证边界注入；不得从模型 Tool Arguments 推导租户身份。
    tenant_id: str | None = None
    # Tool 身份、审批凭据与 Tenant 只由可信装配边界提供。Runtime 配置了
    # authorization 时，缺少 VerifiedIdentity 会 fail-closed。
    tool_dispatch_context: ToolDispatchContext = field(
        default_factory=ToolDispatchContext
    )
    # 由 Agent/Host 的可信装配边界提供模型请求关联标识。普通 Listener 只是
    # Observer，其返回值和对事件副本的修改绝不能进入模型调用元数据。
    durable_metadata_provider: Callable[[], MaybeAwaitable] | None = None
    # Optional fail-closed content-safety boundaries.  They run immediately
    # before Provider ingress and after the terminal Provider message, before
    # any model-generated Tool Call can be dispatched.  Configuring the output
    # boundary buffers Provider partials until the terminal inspection passes.
    inspect_model_input: Callable[
        [list[AgentMessage], CancellationToken], MaybeAwaitable
    ] | None = None
    inspect_model_output: Callable[
        [AgentMessage, CancellationToken], MaybeAwaitable
    ] | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("max_tool_calls", self.max_tool_calls),
            ("max_parallel_tools", self.max_parallel_tools),
            ("max_turns", self.max_turns),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise ValueError(f"{name} 必须是大于 0 的整数或 None")
        if (
            self.default_tool_timeout_seconds is not None
            and self.default_tool_timeout_seconds <= 0
        ):
            raise ValueError("default_tool_timeout_seconds 必须大于 0 或为 None")
        if self.tenant_id is not None and (
            not isinstance(self.tenant_id, str) or not self.tenant_id.strip()
        ):
            raise ValueError("tenant_id 必须是非空字符串或 None")
        if not isinstance(self.tool_dispatch_context, ToolDispatchContext):
            raise TypeError("tool_dispatch_context 必须是 ToolDispatchContext")

    transform_context: Callable[
        [list[AgentMessage], CancellationToken], MaybeAwaitable
    ] | None = None
    get_api_key: Callable[[str], MaybeAwaitable] | None = None
    should_stop_after_turn: Callable[[TurnCompletedContext], MaybeAwaitable] | None = None
    prepare_next_turn: Callable[[TurnCompletedContext], MaybeAwaitable] | None = None
    get_steering_messages: Callable[[], MaybeAwaitable] | None = None
    get_follow_up_messages: Callable[[], MaybeAwaitable] | None = None
    before_tool_call: Callable[
        [BeforeToolCallContext, CancellationToken], MaybeAwaitable
    ] | None = None
    after_tool_call: Callable[
        [AfterToolCallContext, CancellationToken], MaybeAwaitable
    ] | None = None


class AgentState:
    """Agent 对外可读写状态。

    tools 和 messages 的赋值会复制顶层 list，防止调用者随后增删原 list 时
    悄悄改变 Agent 状态。消息对象本身没有深复制，这是与 Pi 类似的性能取舍。
    """

    def __init__(
        self,
        *,
        system_prompt: str,
        model: Model,
        thinking_level: ThinkingLevel = "off",
        tools: list[AgentTool] | None = None,
        messages: list[AgentMessage] | None = None,
    ) -> None:
        self.system_prompt = system_prompt
        self.model = model
        self.thinking_level = thinking_level
        self._tools = list(tools or [])
        self._messages = list(messages or [])
        self.is_streaming = False
        self.streaming_message: AgentMessage | None = None
        self.pending_tool_calls: set[str] = set()
        self.error_message: str | None = None

    @property
    def tools(self) -> list[AgentTool]:
        return self._tools

    @tools.setter
    def tools(self, value: list[AgentTool]) -> None:
        self._tools = list(value)

    @property
    def messages(self) -> list[AgentMessage]:
        return self._messages

    @messages.setter
    def messages(self, value: list[AgentMessage]) -> None:
        self._messages = list(value)
