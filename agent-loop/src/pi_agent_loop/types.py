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

if False:  # 仅供静态类型工具理解，运行时不会导入，避免循环依赖。
    from .event_stream import AssistantMessageEventStream

ToolExecutionMode: TypeAlias = Literal["sequential", "parallel"]
ToolExecutionPolicyMode: TypeAlias = Literal[
    "parallel",
    "exclusive",
    "resource_locked",
    "sequential",  # 向后兼容别名，调度时等价于 exclusive。
]
ToolReplayPolicy: TypeAlias = Literal["never", "safe"]
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


@dataclass(slots=True)
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
    ]
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

    def __post_init__(self) -> None:
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("工具 timeout_seconds 必须大于 0")
        if self.replay_policy not in {"never", "safe"}:
            raise ValueError("工具 replay_policy 必须是 never 或 safe")
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
