"""有状态 Agent 封装。

低层 ``loop.py`` 每次都接收 Context 快照；本模块提供更方便的长期对象：

- 保存 system prompt、model、tools 和 transcript；
- 防止同一 Agent 同时运行两个 prompt；
- 管理 steering/follow-up 内存队列；
- 传播取消；
- 在更新公开状态后按订阅顺序等待 listener；
- 提供可靠的 ``wait_for_idle()``。

自动重试、持久化和压缩仍应由更高层 Session 类负责。
"""

from __future__ import annotations

import asyncio
import copy
import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

from .cancellation import CancellationToken, OperationCancelledError
from .loop import run_agent_loop, run_agent_loop_continue
from .messages import (
    DEFAULT_MESSAGE_INPUT_LIMITS,
    MessageInputLimits,
    empty_usage,
    normalize_user_message,
    now_ms,
    public_error_message,
    user_message,
)
from .safety import ContentSafetyPipeline
from .security import VerifiedIdentity
from .tool_runtime import ToolDispatchRuntime
from .transcript import repair_unresolved_tool_calls
from .types import (
    AfterToolCallContext,
    AfterToolCallResult,
    AgentContext,
    AgentEvent,
    AgentLoopConfig,
    AgentLoopTurnUpdate,
    AgentMessage,
    AgentState,
    AgentTool,
    AgentToolResult,
    BeforeToolCallContext,
    Model,
    QueueMode,
    StreamFn,
    ThinkingLevel,
    ToolAuthorization,
    ToolDispatchContext,
    ToolExecutionMode,
    TurnCompletedContext,
    UNSET,
)

Listener = Callable[[AgentEvent, CancellationToken], Any]


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await cast(Awaitable[Any], value)
    return value


def _default_convert_to_llm(messages: list[AgentMessage]) -> list[AgentMessage]:
    """默认只把三种标准消息发给模型，自定义 UI 消息会被过滤。"""

    return [
        message
        for message in messages
        if message.get("role") in {"user", "assistant", "toolResult"}
    ]


def _apply_after_tool_override(
    result: AgentToolResult,
    is_error: bool,
    override: Any,
) -> tuple[AgentToolResult, bool]:
    """Apply the public after-hook contract before content-safety inspection."""

    if not isinstance(override, AfterToolCallResult):
        return result, is_error
    updated = AgentToolResult(
        content=(
            result.content
            if override.content is UNSET
            else list(override.content)
        ),
        details=(
            result.details if override.details is UNSET else override.details
        ),
        usage=result.usage if override.usage is UNSET else override.usage,
        added_tool_names=result.added_tool_names,
        terminate=(
            result.terminate
            if override.terminate is UNSET
            else override.terminate
        ),
    )
    if override.is_error is not UNSET:
        is_error = bool(override.is_error)
    return updated, is_error


def _validate_guarded_tool_output(value: Any) -> dict[str, Any]:
    """Reject malformed policy rewrites before they re-enter the transcript."""

    if not isinstance(value, Mapping):
        raise TypeError("内容安全策略返回了无效的工具输出")
    required = {"content", "details", "usage", "terminate", "isError"}
    if set(value) != required:
        raise ValueError("内容安全工具输出字段不完整或包含未知字段")
    content = value.get("content")
    usage = value.get("usage")
    terminate = value.get("terminate")
    is_error = value.get("isError")
    if not isinstance(content, list) or any(
        not isinstance(block, dict) for block in content
    ):
        raise TypeError("内容安全工具输出 content 必须是对象列表")
    if usage is not None and not isinstance(usage, dict):
        raise TypeError("内容安全工具输出 usage 必须是对象或 None")
    if terminate is not None and not isinstance(terminate, bool):
        raise TypeError("内容安全工具输出 terminate 必须是布尔值或 None")
    if not isinstance(is_error, bool):
        raise TypeError("内容安全工具输出 isError 必须是布尔值")
    return {
        "content": copy.deepcopy(content),
        "details": copy.deepcopy(value.get("details")),
        "usage": copy.deepcopy(usage),
        "terminate": terminate,
        "isError": is_error,
    }


def _materialize_staged_tool_results(
    messages: list[AgentMessage],
    staged: dict[str, AgentMessage],
) -> list[AgentMessage]:
    """把 Commit Boundary 已产生、但尚未 Message End 的真实结果放回原位。"""

    if not staged:
        return list(messages)
    output: list[AgentMessage] = []
    pending: dict[str, str] = {}

    def close_pending() -> None:
        for tool_call_id, tool_name in list(pending.items()):
            message = staged.get(tool_call_id)
            if (
                message is not None
                and str(message.get("toolName", "")) == tool_name
            ):
                output.append(copy.deepcopy(message))
        pending.clear()

    for original in messages:
        message = copy.deepcopy(original)
        role = message.get("role")
        if role != "toolResult" and pending:
            close_pending()
        output.append(message)
        if role == "assistant":
            for block in message.get("content", []):
                if isinstance(block, dict) and block.get("type") == "toolCall":
                    pending[str(block.get("id", ""))] = str(block.get("name", ""))
        elif role == "toolResult":
            pending.pop(str(message.get("toolCallId", "")), None)
    if pending:
        close_pending()
    return output


class _PendingMessageQueue:
    """支持 all 与 one-at-a-time 两种领取方式的内存队列。"""

    def __init__(self, mode: QueueMode) -> None:
        self.mode = mode
        self._messages: list[AgentMessage] = []

    def enqueue(self, message: AgentMessage) -> None:
        self._messages.append(message)

    def has_items(self) -> bool:
        return bool(self._messages)

    def drain(self) -> list[AgentMessage]:
        if self.mode == "all":
            messages = self._messages
            self._messages = []
            return messages
        if not self._messages:
            return []
        return [self._messages.pop(0)]

    def clear(self) -> None:
        self._messages.clear()


class Agent:
    """Pi 风格的有状态 Agent。

    参数很多是因为它是低层可嵌入对象。实际应用可再写一个 Session/Host 工厂，
    集中注入 Provider、认证、持久化和扩展 hook。
    """

    def __init__(
        self,
        *,
        model: Model,
        stream_fn: StreamFn,
        system_prompt: str = "",
        thinking_level: ThinkingLevel = "off",
        tools: list[AgentTool] | None = None,
        messages: list[AgentMessage] | None = None,
        convert_to_llm: Callable[[list[AgentMessage]], Any] | None = None,
        transform_context: Callable[
            [list[AgentMessage], CancellationToken], Any
        ] | None = None,
        get_api_key: Callable[[str], Any] | None = None,
        before_tool_call: Callable[
            [BeforeToolCallContext, CancellationToken], Any
        ] | None = None,
        after_tool_call: Callable[
            [AfterToolCallContext, CancellationToken], Any
        ] | None = None,
        should_stop_after_turn: Callable[
            [TurnCompletedContext, CancellationToken], Any
        ] | None = None,
        prepare_next_turn: Callable[
            [TurnCompletedContext, CancellationToken], Any
        ] | None = None,
        steering_mode: QueueMode = "one-at-a-time",
        follow_up_mode: QueueMode = "one-at-a-time",
        tool_execution: ToolExecutionMode = "parallel",
        default_tool_timeout_seconds: float | None = None,
        max_tool_calls: int | None = None,
        max_parallel_tools: int | None = None,
        max_turns: int | None = None,
        stream_options: dict[str, Any] | None = None,
        retry_event_sink: Callable[[AgentEvent], Any] | None = None,
        tool_runtime: ToolDispatchRuntime | None = None,
        tenant_id: str | None = None,
        tool_identity: VerifiedIdentity | None = None,
        tool_approval: Any = None,
        tool_authorization: ToolAuthorization | None = None,
        require_tool_identity: bool = False,
        durable_metadata_provider: Callable[[], Any] | None = None,
        message_input_limits: MessageInputLimits | None = None,
        content_safety: ContentSafetyPipeline | None = None,
    ) -> None:
        if (
            default_tool_timeout_seconds is not None
            and default_tool_timeout_seconds <= 0
        ):
            raise ValueError("default_tool_timeout_seconds 必须大于 0")
        for name, value in (
            ("max_tool_calls", max_tool_calls),
            ("max_parallel_tools", max_parallel_tools),
            ("max_turns", max_turns),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise ValueError(f"{name} 必须是大于 0 的整数或 None")

        self.state = AgentState(
            system_prompt=system_prompt,
            model=model,
            thinking_level=thinking_level,
            tools=tools,
            messages=messages,
        )
        self.stream_fn = stream_fn
        self.convert_to_llm = convert_to_llm or _default_convert_to_llm
        self.transform_context = transform_context
        self.get_api_key = get_api_key
        self.before_tool_call = before_tool_call
        self.after_tool_call = after_tool_call
        self.should_stop_after_turn = should_stop_after_turn
        self.prepare_next_turn = prepare_next_turn
        self.tool_execution = tool_execution
        self.default_tool_timeout_seconds = default_tool_timeout_seconds
        self.max_tool_calls = max_tool_calls
        self.max_parallel_tools = max_parallel_tools
        self.max_turns = max_turns
        self.stream_options = dict(stream_options or {})
        self.retry_event_sink = retry_event_sink
        self.tenant_id = tenant_id
        self.tool_dispatch_context = ToolDispatchContext(
            identity=tool_identity,
            approval=tool_approval,
            tenant_id=tenant_id,
        )
        self.tool_authorization = tool_authorization
        self.require_tool_identity = require_tool_identity
        self.durable_metadata_provider = durable_metadata_provider
        if message_input_limits is not None and not isinstance(
            message_input_limits,
            MessageInputLimits,
        ):
            raise TypeError(
                "message_input_limits 必须是 MessageInputLimits 或 None"
            )
        self.message_input_limits = (
            message_input_limits or DEFAULT_MESSAGE_INPUT_LIMITS
        )
        if content_safety is not None and not isinstance(
            content_safety,
            ContentSafetyPipeline,
        ):
            raise TypeError(
                "content_safety 必须是 ContentSafetyPipeline 或 None"
            )
        self.content_safety = content_safety
        self.tool_runtime = tool_runtime or ToolDispatchRuntime(
            self.state.tools,
            before_tool_call=before_tool_call,
            after_tool_call=after_tool_call,
            retry_event_sink=retry_event_sink,
            default_tool_timeout_seconds=default_tool_timeout_seconds,
            max_parallel_tools=max_parallel_tools or 64,
            default_tenant_id=tenant_id,
            authorization=tool_authorization,
            require_identity=require_tool_identity,
        )
        if tool_runtime is not None:
            if tool_authorization is not None:
                self.tool_runtime.authorization = tool_authorization
            if require_tool_identity:
                self.tool_runtime.require_identity = True

        self._steering_queue = _PendingMessageQueue(steering_mode)
        self._follow_up_queue = _PendingMessageQueue(follow_up_mode)
        self._listeners: list[Listener] = []
        self.listener_errors: list[str] = []
        self._staged_tool_results: dict[str, AgentMessage] = {}
        self._active_token: CancellationToken | None = None
        self._idle_event = asyncio.Event()
        self._idle_event.set()

    @property
    def steering_mode(self) -> QueueMode:
        return self._steering_queue.mode

    @steering_mode.setter
    def steering_mode(self, mode: QueueMode) -> None:
        self._steering_queue.mode = mode

    @property
    def follow_up_mode(self) -> QueueMode:
        return self._follow_up_queue.mode

    @follow_up_mode.setter
    def follow_up_mode(self, mode: QueueMode) -> None:
        self._follow_up_queue.mode = mode

    @property
    def cancellation(self) -> CancellationToken | None:
        """当前运行的取消令牌；空闲时为 None。"""

        return self._active_token

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """按注册顺序订阅 Agent 事件，并返回取消订阅函数。"""

        self._listeners.append(listener)
        active = True

        def unsubscribe() -> None:
            nonlocal active
            if not active:
                return
            active = False
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

        return unsubscribe

    def steer(self, message: AgentMessage | str) -> None:
        """把消息排到当前 Turn 之后、下一模型请求之前。"""

        self._steering_queue.enqueue(
            user_message(message, limits=self.message_input_limits)
            if isinstance(message, str)
            else self._normalize_prompt_message(message)
        )

    def follow_up(self, message: AgentMessage | str) -> None:
        """把消息排到 Agent 原本准备结束的位置。"""

        self._follow_up_queue.enqueue(
            user_message(message, limits=self.message_input_limits)
            if isinstance(message, str)
            else self._normalize_prompt_message(message)
        )

    def clear_steering_queue(self) -> None:
        self._steering_queue.clear()

    def clear_follow_up_queue(self) -> None:
        self._follow_up_queue.clear()

    def clear_all_queues(self) -> None:
        self.clear_steering_queue()
        self.clear_follow_up_queue()

    def has_queued_messages(self) -> bool:
        return self._steering_queue.has_items() or self._follow_up_queue.has_items()

    def abort(self, reason: str = "用户取消了 Agent 运行") -> None:
        """请求取消当前运行。工具和 Provider 必须合作检查令牌。"""

        if self._active_token is not None:
            self._active_token.cancel(reason)

    async def wait_for_idle(self) -> None:
        """等待低层 Loop 和所有已 await 的 listener 完成。"""

        await self._idle_event.wait()

    def reset(self) -> None:
        """仅在空闲时清空 transcript、队列和运行状态。"""

        if self._active_token is not None:
            raise RuntimeError("Agent 正在运行，不能 reset")
        self.state.messages = []
        self.state.streaming_message = None
        self.state.pending_tool_calls = set()
        self.state.error_message = None
        self.clear_all_queues()

    async def prompt(
        self,
        value: str | AgentMessage | list[AgentMessage],
        images: list[dict] | None = None,
        *,
        cancellation: CancellationToken | None = None,
    ) -> None:
        """提交一条或多条新消息，并等待本次低层运行结算。"""

        if self._active_token is not None:
            raise RuntimeError(
                "Agent 正在处理 prompt；请使用 steer/follow_up，或等待空闲"
            )
        if isinstance(value, str):
            prompts = [
                user_message(
                    value,
                    images,
                    limits=self.message_input_limits,
                )
            ]
        elif isinstance(value, list):
            if images is not None:
                raise ValueError("images 只能与字符串 prompt 一起使用")
            prompts = [self._normalize_prompt_message(message) for message in value]
        else:
            if images is not None:
                raise ValueError("images 只能与字符串 prompt 一起使用")
            prompts = [self._normalize_prompt_message(value)]
        if cancellation is not None:
            cancellation.throw_if_cancelled()
        await self._run_prompt_messages(
            prompts,
            cancellation=cancellation,
        )

    def _normalize_prompt_message(self, message: AgentMessage) -> AgentMessage:
        if not isinstance(message, dict):
            raise TypeError("prompt 消息必须是字典")
        if message.get("role") != "user":
            return message
        return normalize_user_message(
            message,
            limits=self.message_input_limits,
        )

    async def continue_run(self) -> None:
        """从当前 transcript 继续；名称避开 Python 关键字 ``continue``。"""

        if self._active_token is not None:
            raise RuntimeError("Agent 正在运行，不能 continue")
        if not self.state.messages:
            raise RuntimeError("没有可继续的消息")

        last = self.state.messages[-1]
        if last.get("role") == "assistant":
            steering = self._steering_queue.drain()
            if steering:
                await self._run_prompt_messages(
                    steering,
                    skip_initial_steering_poll=True,
                )
                return
            follow_ups = self._follow_up_queue.drain()
            if follow_ups:
                await self._run_prompt_messages(follow_ups)
                return
            raise RuntimeError("无法从 assistant 消息继续")

        await self._run_continuation()

    async def _run_prompt_messages(
        self,
        messages: list[AgentMessage],
        *,
        skip_initial_steering_poll: bool = False,
        cancellation: CancellationToken | None = None,
    ) -> None:
        self._repair_transcript_before_run()

        async def execute(token: CancellationToken) -> None:
            await run_agent_loop(
                messages,
                self._context_snapshot(),
                self._create_loop_config(
                    token,
                    skip_initial_steering_poll=skip_initial_steering_poll,
                ),
                self._process_event,
                token,
                self.stream_fn,
            )

        await self._run_with_lifecycle(execute, cancellation=cancellation)

    async def _run_continuation(self) -> None:
        self._repair_transcript_before_run()

        async def execute(token: CancellationToken) -> None:
            await run_agent_loop_continue(
                self._context_snapshot(),
                self._create_loop_config(token),
                self._process_event,
                token,
                self.stream_fn,
            )

        await self._run_with_lifecycle(execute)

    def _repair_transcript_before_run(self) -> None:
        """在加入新 User Message 前补齐旧历史中缺失的 ToolResult。"""

        repaired, _inserted = repair_unresolved_tool_calls(
            self.state.messages,
            code="tool_result_missing_repaired",
            text="此前工具调用没有结果，系统已补写错误结果以恢复协议完整性。",
        )
        self.state.messages = repaired

    def _context_snapshot(self) -> AgentContext:
        """深拷贝运行 Context，隔离 Loop、Hook 与公开状态。"""

        return AgentContext(
            system_prompt=self.state.system_prompt,
            messages=copy.deepcopy(self.state.messages),
            tools=list(self.state.tools),
        )

    def _create_loop_config(
        self,
        token: CancellationToken,
        *,
        skip_initial_steering_poll: bool = False,
    ) -> AgentLoopConfig:
        skip_poll = skip_initial_steering_poll

        async def get_steering() -> list[AgentMessage]:
            nonlocal skip_poll
            if skip_poll:
                skip_poll = False
                return []
            return self._steering_queue.drain()

        async def get_follow_up() -> list[AgentMessage]:
            return self._follow_up_queue.drain()

        async def should_stop(context: TurnCompletedContext) -> bool:
            if self.should_stop_after_turn is None:
                return False
            return bool(await _maybe_await(self.should_stop_after_turn(context, token)))

        async def prepare(
            context: TurnCompletedContext,
        ) -> AgentLoopTurnUpdate | None:
            if self.prepare_next_turn is None:
                return None
            return await _maybe_await(self.prepare_next_turn(context, token))

        async def inspect_model_input(
            messages: list[AgentMessage],
            cancellation: CancellationToken,
        ) -> list[AgentMessage]:
            if self.content_safety is None:
                return messages
            inspected = await self.content_safety.inspect(
                "model_input",
                messages,
                cancellation,
                tenant_id=self.tenant_id,
            )
            if not isinstance(inspected, list) or any(
                not isinstance(message, dict) for message in inspected
            ):
                raise TypeError(
                    "内容安全策略返回了无效的模型输入消息"
                )
            return copy.deepcopy(inspected)

        async def inspect_model_output(
            message: AgentMessage,
            cancellation: CancellationToken,
        ) -> AgentMessage:
            if self.content_safety is None:
                return message
            inspected = await self.content_safety.inspect(
                "model_output",
                message,
                cancellation,
                tenant_id=self.tenant_id,
            )
            if not isinstance(inspected, dict):
                raise TypeError(
                    "内容安全策略返回了无效的模型输出消息"
                )
            return copy.deepcopy(inspected)

        guarded_after_tool_call = self._guarded_after_tool_call()

        # 工具可以在运行时动态加入；每轮同步到普通/恢复共用的 Runtime。
        self.tool_runtime.register_tools(self.state.tools)
        self.tool_runtime.before_tool_call = self.before_tool_call
        self.tool_runtime.after_tool_call = guarded_after_tool_call
        self.tool_runtime.retry_event_sink = self.retry_event_sink
        self.tool_runtime.default_tool_timeout_seconds = self.default_tool_timeout_seconds
        return AgentLoopConfig(
            model=self.state.model,
            thinking_level=self.state.thinking_level,
            convert_to_llm=self.convert_to_llm,
            transform_context=self.transform_context,
            get_api_key=self.get_api_key,
            before_tool_call=self.before_tool_call,
            after_tool_call=guarded_after_tool_call,
            should_stop_after_turn=should_stop
            if self.should_stop_after_turn is not None
            else None,
            prepare_next_turn=prepare if self.prepare_next_turn is not None else None,
            get_steering_messages=get_steering,
            get_follow_up_messages=get_follow_up,
            tool_execution=self.tool_execution,
            default_tool_timeout_seconds=self.default_tool_timeout_seconds,
            max_tool_calls=self.max_tool_calls,
            max_parallel_tools=self.max_parallel_tools,
            max_turns=self.max_turns,
            stream_options=dict(self.stream_options),
            retry_event_sink=self.retry_event_sink,
            tool_runtime=self.tool_runtime,
            tenant_id=self.tenant_id,
            tool_dispatch_context=self.tool_dispatch_context,
            durable_metadata_provider=self.durable_metadata_provider,
            inspect_model_input=(
                inspect_model_input if self.content_safety is not None else None
            ),
            inspect_model_output=(
                inspect_model_output if self.content_safety is not None else None
            ),
        )

    def _guarded_after_tool_call(
        self,
    ) -> Callable[[AfterToolCallContext, CancellationToken], Any] | None:
        """Expose only inspected Tool output to hooks and the transcript."""

        pipeline = self.content_safety
        if pipeline is None:
            return self.after_tool_call

        async def inspect_output(
            result: AgentToolResult,
            is_error: bool,
            cancellation: CancellationToken,
            *,
            tool_name: str | None,
            tool_call_id: str,
        ) -> dict[str, Any]:
            inspected = await pipeline.inspect(
                "tool_output",
                {
                    "content": copy.deepcopy(result.content),
                    "details": copy.deepcopy(result.details),
                    "usage": copy.deepcopy(result.usage),
                    "terminate": result.terminate,
                    "isError": is_error,
                },
                cancellation,
                tenant_id=self.tenant_id,
                tool_name=tool_name,
                metadata=(
                    {"toolCallId": tool_call_id} if tool_call_id else None
                ),
            )
            return _validate_guarded_tool_output(inspected)

        async def guarded(
            context: AfterToolCallContext,
            cancellation: CancellationToken,
        ) -> AfterToolCallResult:
            original_result = copy.deepcopy(context.result)
            original_is_error = context.is_error
            tool_name = str(context.tool_call.get("name", "")) or None
            tool_call_id = str(context.tool_call.get("id", ""))
            safe = await inspect_output(
                original_result,
                original_is_error,
                cancellation,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
            if self.after_tool_call is not None:
                # The caller hook is an observer/rewriter, not a trusted bypass:
                # it receives the inspected snapshot and any override is applied
                # to the original result, then inspected again before release.
                safe_context = AfterToolCallContext(
                    assistant_message=context.assistant_message,
                    tool_call=context.tool_call,
                    args=context.args,
                    result=AgentToolResult(
                        content=safe["content"],
                        details=safe["details"],
                        usage=safe["usage"],
                        added_tool_names=copy.deepcopy(
                            original_result.added_tool_names
                        ),
                        terminate=safe["terminate"],
                    ),
                    is_error=safe["isError"],
                    context=context.context,
                )
                override = await _maybe_await(
                    self.after_tool_call(safe_context, cancellation)
                )
                if isinstance(override, AfterToolCallResult):
                    overridden_result, overridden_is_error = (
                        _apply_after_tool_override(
                            original_result,
                            original_is_error,
                            override,
                        )
                    )
                    safe = await inspect_output(
                        overridden_result,
                        overridden_is_error,
                        cancellation,
                        tool_name=tool_name,
                        tool_call_id=tool_call_id,
                    )
            return AfterToolCallResult(
                content=safe["content"],
                details=safe["details"],
                usage=safe["usage"],
                terminate=safe["terminate"],
                is_error=safe["isError"],
            )

        return guarded

    async def _run_with_lifecycle(
        self,
        executor: Callable[[CancellationToken], Awaitable[None]],
        *,
        cancellation: CancellationToken | None = None,
    ) -> None:
        if self._active_token is not None:
            raise RuntimeError("Agent 已经在运行")

        token = cancellation or CancellationToken()
        # 外部预取消必须在发出 agent_start、持久化 Operation 或调用 Provider
        # 之前失败；同一 token 随后会继续传给模型、Hook 和工具 Runtime。
        token.throw_if_cancelled()
        self._active_token = token
        self._idle_event.clear()
        self.state.is_streaming = True
        self.state.streaming_message = None
        self.state.error_message = None
        self._staged_tool_results = {}

        try:
            await executor(token)
        except asyncio.CancelledError:
            # 外部 prompt_task.cancel() 不会经过 Agent.abort()。必须先完成
            # Transcript/Durable Operation 收尾，再把取消继续抛给调用方。
            token.cancel("外部 Prompt Task 被取消")
            cleanup = asyncio.create_task(
                self._handle_run_failure(
                    OperationCancelledError(token.reason),
                    True,
                ),
                name="pi-agent-external-cancel-cleanup",
            )
            try:
                await asyncio.shield(cleanup)
            except BaseException:
                # 外部取消是主要结果；清理失败不能替换 CancelledError。
                if not cleanup.done():
                    await asyncio.gather(cleanup, return_exceptions=True)
            raise
        except Exception as error:
            await self._handle_run_failure(error, token.cancelled)
        finally:
            self.state.is_streaming = False
            self.state.streaming_message = None
            self.state.pending_tool_calls = set()
            self._staged_tool_results = {}
            self._active_token = None
            self._idle_event.set()

    async def _handle_run_failure(self, error: Exception, aborted: bool) -> None:
        """补齐未闭合 Tool Call，再规范成失败生命周期事件。"""

        try:
            staged_ids = set(self._staged_tool_results)
            materialized = _materialize_staged_tool_results(
                self.state.messages,
                self._staged_tool_results,
            )
            repaired, inserted = repair_unresolved_tool_calls(
                materialized,
                code="tool_not_executed_due_run_error",
                text="工具调用因 Agent 运行异常而未执行。",
            )
            self.state.messages = repaired
            notify_ids = staged_ids | {
                str(message.get("toolCallId", "")) for message in inserted
            }
            notifications = [
                message
                for message in repaired
                if message.get("role") == "toolResult"
                and str(message.get("toolCallId", "")) in notify_ids
            ]
            for message in notifications:
                await self._notify_repair_event(
                    {
                        "type": "transcript_repaired",
                        "message": copy.deepcopy(message),
                        "contextMessages": copy.deepcopy(repaired),
                        "reason": "run_error",
                    }
                )
            self._staged_tool_results.clear()
        except Exception:
            # 原始运行错误优先；无法安全修复的历史由下一次运行前校验正式拒绝。
            pass

        failure: AgentMessage = {
            "role": "assistant",
            "content": [{"type": "text", "text": ""}],
            "api": self.state.model.api,
            "provider": self.state.model.provider,
            "model": self.state.model.id,
            "usage": empty_usage(),
            "stopReason": "aborted" if aborted else "error",
            "errorMessage": public_error_message(
                error,
                fallback="Agent 运行失败",
            ),
            "timestamp": now_ms(),
        }
        await self._process_event({"type": "message_start", "message": failure})
        await self._process_event({"type": "message_end", "message": failure})
        await self._process_event(
            {"type": "turn_end", "message": failure, "toolResults": []}
        )
        await self._process_event({"type": "agent_end", "messages": [failure]})

    async def _notify_repair_event(self, event: AgentEvent) -> None:
        """通知所有 Listener，但修复通知失败不能覆盖原始运行错误。"""

        token = self._active_token
        if token is None:
            return
        for listener in list(self._listeners):
            try:
                await _maybe_await(listener(copy.deepcopy(event), token))
            except Exception:
                continue

    async def _process_event(self, event: AgentEvent) -> None:
        """先更新 AgentState，再按订阅顺序等待 listener。"""

        event_type = event.get("type")
        if event_type == "agent_start":
            event = dict(event)
            event["contextMessages"] = copy.deepcopy(self.state.messages)
        elif event_type == "transcript_repaired":
            context_messages = event.get("contextMessages")
            if isinstance(context_messages, list):
                self.state.messages = copy.deepcopy(context_messages)
        elif event_type in {"message_start", "message_update"}:
            message = event.get("message")
            self.state.streaming_message = (
                copy.deepcopy(message) if isinstance(message, dict) else None
            )
        elif event_type == "message_end":
            self.state.streaming_message = None
            self.state.messages.append(copy.deepcopy(event["message"]))
        elif event_type == "tool_execution_start":
            pending = set(self.state.pending_tool_calls)
            pending.add(str(event.get("toolCallId", "")))
            self.state.pending_tool_calls = pending
        elif event_type == "tool_execution_end":
            committed = event.get("toolResultMessage")
            if isinstance(committed, dict):
                tool_call_id = str(committed.get("toolCallId", ""))
                if tool_call_id:
                    self._staged_tool_results[tool_call_id] = copy.deepcopy(committed)
            pending = set(self.state.pending_tool_calls)
            pending.discard(str(event.get("toolCallId", "")))
            self.state.pending_tool_calls = pending
        elif event_type == "turn_end":
            message = event.get("message", {})
            if message.get("role") == "assistant" and message.get("errorMessage"):
                self.state.error_message = str(message["errorMessage"])
        elif event_type == "budget_exceeded":
            self.state.error_message = str(event.get("message", "运行预算不足"))
        elif event_type == "agent_end":
            self.state.streaming_message = None

        token = self._active_token
        if token is None:
            raise RuntimeError("Agent listener 在 active run 之外被调用")
        # Tool 已产生结果后进入 Commit Boundary。此时 Observer 失败不能把
        # 已发生的副作用改写成“工具未执行”，也不能取消同批兄弟工具。
        deferred_cancel: asyncio.CancelledError | None = None
        for listener in list(self._listeners):
            # Listener 是观察边界：每个订阅者得到独立快照，不能通过修改
            # Message/Tool Call 反向篡改 AgentState 或低层 Loop 的执行输入。
            listener_event = copy.deepcopy(event)
            try:
                # Listener 永远只是 Observer。即使它修改自己的事件副本，也
                # 不允许任何字段（包括审计关联 ID）回写 Agent/Loop。
                await _maybe_await(listener(listener_event, token))
            except asyncio.CancelledError as error:
                if event_type == "tool_execution_end":
                    self.listener_errors.append(
                        f"[{type(error).__name__}] "
                        + public_error_message(
                            error,
                            fallback=(
                                "tool_execution_end listener failed/cancelled"
                            ),
                        )
                    )
                    deferred_cancel = deferred_cancel or error
                    continue
                raise
            except Exception as error:
                if event_type == "tool_execution_end":
                    message = (
                        f"[{type(error).__name__}] "
                        + public_error_message(
                            error,
                            fallback=(
                                "tool_execution_end listener failed/cancelled"
                            ),
                        )
                    )
                    self.listener_errors.append(message)
                    self.state.error_message = message
                    continue
                raise
        if event_type == "message_end":
            message = event.get("message", {})
            if message.get("role") == "toolResult":
                self._staged_tool_results.pop(
                    str(message.get("toolCallId", "")),
                    None,
                )
        if deferred_cancel is not None:
            raise deferred_cancel
