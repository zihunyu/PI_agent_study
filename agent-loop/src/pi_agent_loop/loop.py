"""Pi Agent Loop 的 Python 改写。

本模块刻意只处理低层机制：

- 组装模型上下文并消费流式 assistant 事件；
- 执行串行或并行工具；
- 处理 steering 与 follow-up 队列；
- 发出 Agent/Turn/Message/Tool 生命周期事件。

模型重试策略、上下文压缩、JSONL/SQLite 持久化和 UI 不属于低层循环，
应该由更高层 Session/Host 编排。低层只调用独立 Tool Retry 包装器。
"""

from __future__ import annotations

import asyncio
import copy
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, cast

from .cancellation import (
    CancellationToken,
    OperationCancelledError,
)
from .event_stream import (
    AgentEventStream,
    AssistantMessageEventStream,
)
from .messages import clone_message, error_tool_result, now_ms
from .retry.errors import OutcomeUnknownToolError, RetryableToolError
from .retry.tool import execute_tool_with_retry
from .types import (
    AfterToolCallContext,
    AfterToolCallResult,
    AgentContext,
    AgentEvent,
    AgentLoopConfig,
    AgentLoopTurnUpdate,
    AgentMessage,
    AgentTool,
    AgentToolResult,
    BeforeToolCallContext,
    BeforeToolCallResult,
    EventSink,
    Model,
    StreamFn,
    TurnCompletedContext,
    UNSET,
)

# Provider 流更新 assistant partial 时可能出现的事件。
_MODEL_RETRY_EVENT_TYPES = {
    "model_retry_scheduled",
    "model_retry_attempt_start",
    "model_retry_finished",
    "context_compaction_started",
    "context_compaction_finished",
}

_ASSISTANT_UPDATE_TYPES = {
    "text_start",
    "text_delta",
    "text_end",
    "thinking_start",
    "thinking_delta",
    "thinking_end",
    "toolcall_start",
    "toolcall_delta",
    "toolcall_end",
}


@dataclass(slots=True)
class _RunBudgetState:
    """一次低层 Agent 运行的预算使用量；每次 prompt/continue 都重新创建。"""

    turns_used: int = 0
    tool_calls_used: int = 0


async def _maybe_await(value: Any) -> Any:
    """统一处理同步回调和异步回调的返回值。"""

    if inspect.isawaitable(value):
        return await cast(Awaitable[Any], value)
    return value


async def _emit(sink: EventSink, event: AgentEvent) -> None:
    """发出事件，并等待可能存在的异步 listener。"""

    await _maybe_await(sink(event))


def _assistant_tool_calls(message: AgentMessage) -> list[dict]:
    """从 assistant content 中按原始顺序提取工具调用。"""

    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") == "toolCall"
    ]


def _serialize_tools(tools: list[AgentTool]) -> list[dict[str, Any]]:
    """把 Python 工具对象变成模型 Provider 可以序列化的工具定义。"""

    return [
        {
            "name": tool.name,
            "description": tool.description,
            "parameters": copy.deepcopy(tool.parameters),
        }
        for tool in tools
    ]


def _tool_result_payload(result: AgentToolResult) -> dict[str, Any]:
    """把 dataclass 工具结果转换成事件中的普通字典。"""

    payload: dict[str, Any] = {
        "content": copy.deepcopy(result.content),
        "details": copy.deepcopy(result.details),
    }
    if result.usage is not None:
        payload["usage"] = copy.deepcopy(result.usage)
    if result.added_tool_names:
        payload["addedToolNames"] = list(result.added_tool_names)
    if result.terminate is not None:
        payload["terminate"] = result.terminate
    return payload


def agent_loop(
    prompts: list[AgentMessage],
    context: AgentContext,
    config: AgentLoopConfig,
    cancellation: CancellationToken,
    stream_fn: StreamFn,
) -> AgentEventStream:
    """启动带新 prompt 的低层循环，并立即返回可消费事件流。

    后台任务若发生契约外异常，``stream.result()`` 会抛出该异常。普通模型失败
    则应由 Provider 编码成 assistant ``error`` 事件，而不是抛异常。
    """

    stream = AgentEventStream()

    async def runner() -> None:
        try:
            messages = await run_agent_loop(
                prompts,
                context,
                config,
                stream.push,
                cancellation,
                stream_fn,
            )
            stream.end(messages)
        except BaseException as error:
            stream.fail(error)

    asyncio.create_task(runner())
    return stream


def agent_loop_continue(
    context: AgentContext,
    config: AgentLoopConfig,
    cancellation: CancellationToken,
    stream_fn: StreamFn,
) -> AgentEventStream:
    """从现有 user/toolResult 结尾继续循环。"""

    _validate_continuation_context(context)
    stream = AgentEventStream()

    async def runner() -> None:
        try:
            messages = await run_agent_loop_continue(
                context,
                config,
                stream.push,
                cancellation,
                stream_fn,
            )
            stream.end(messages)
        except BaseException as error:
            stream.fail(error)

    asyncio.create_task(runner())
    return stream


def _validate_continuation_context(context: AgentContext) -> None:
    if not context.messages:
        raise ValueError("无法继续：上下文中没有消息")
    if context.messages[-1].get("role") == "assistant":
        raise ValueError("无法从 assistant 消息继续；最后一条必须是 user 或 toolResult")


async def run_agent_loop(
    prompts: list[AgentMessage],
    context: AgentContext,
    config: AgentLoopConfig,
    emit: EventSink,
    cancellation: CancellationToken,
    stream_fn: StreamFn,
) -> list[AgentMessage]:
    """运行带 prompt 的循环，返回本次运行新产生的消息。

    返回值不是完整 transcript：它包含传入 prompts，以及本次运行新增的
    assistant、toolResult、steering 和 follow-up 消息。
    """

    new_messages = list(prompts)
    current_context = AgentContext(
        system_prompt=context.system_prompt,
        messages=[*context.messages, *prompts],
        tools=list(context.tools),
    )

    await _emit(emit, {"type": "agent_start"})
    await _emit(emit, {"type": "turn_start"})
    for prompt in prompts:
        await _emit(emit, {"type": "message_start", "message": clone_message(prompt)})
        await _emit(emit, {"type": "message_end", "message": prompt})

    await _run_loop(
        current_context,
        new_messages,
        config,
        cancellation,
        emit,
        stream_fn,
    )
    return new_messages


async def run_agent_loop_continue(
    context: AgentContext,
    config: AgentLoopConfig,
    emit: EventSink,
    cancellation: CancellationToken,
    stream_fn: StreamFn,
) -> list[AgentMessage]:
    """运行 continuation；不会把旧 Context 消息放进返回值。"""

    _validate_continuation_context(context)
    new_messages: list[AgentMessage] = []
    current_context = AgentContext(
        system_prompt=context.system_prompt,
        messages=list(context.messages),
        tools=list(context.tools),
    )

    await _emit(emit, {"type": "agent_start"})
    await _emit(emit, {"type": "turn_start"})
    await _run_loop(
        current_context,
        new_messages,
        config,
        cancellation,
        emit,
        stream_fn,
    )
    return new_messages


async def _run_loop(
    initial_context: AgentContext,
    new_messages: list[AgentMessage],
    initial_config: AgentLoopConfig,
    cancellation: CancellationToken,
    emit: EventSink,
    stream_fn: StreamFn,
) -> None:
    """共享的双层循环。

    内层处理工具和 steering；外层在 Agent 本来结束时处理 follow-up。
    ``has_more_tool_calls`` 初始为 True，确保一定发生第一次模型请求。
    """

    current_context = initial_context
    config = initial_config
    first_turn = True
    budget = _RunBudgetState()
    pending_messages = await _get_messages(config.get_steering_messages)

    while True:
        has_more_tool_calls = True

        while has_more_tool_calls or pending_messages:
            if not first_turn:
                # 在发出 turn_start 和请求 Provider 前检查，确保不会多请求一轮。
                if (
                    config.max_turns is not None
                    and budget.turns_used >= config.max_turns
                ):
                    await _emit_budget_exceeded(
                        emit,
                        budget="turns",
                        limit=config.max_turns,
                        used=budget.turns_used,
                        requested=1,
                    )
                    await _emit(
                        emit,
                        {"type": "agent_end", "messages": list(new_messages)},
                    )
                    return
                await _emit(emit, {"type": "turn_start"})
            else:
                first_turn = False

            # 每次真正准备请求 assistant 时消耗一个 Turn。
            budget.turns_used += 1

            # Steering/follow-up 在新 assistant 请求前作为正常消息注入。
            if pending_messages:
                for pending in pending_messages:
                    await _emit(
                        emit,
                        {"type": "message_start", "message": clone_message(pending)},
                    )
                    await _emit(emit, {"type": "message_end", "message": pending})
                    current_context.messages.append(pending)
                    new_messages.append(pending)
                pending_messages = []

            message = await _stream_assistant_response(
                current_context,
                config,
                cancellation,
                emit,
                stream_fn,
            )
            new_messages.append(message)

            # Provider/模型失败是本次低层 run 的 terminal 状态。
            if message.get("stopReason") in {"error", "aborted"}:
                await _emit(
                    emit,
                    {"type": "turn_end", "message": message, "toolResults": []},
                )
                await _emit(
                    emit,
                    {"type": "agent_end", "messages": list(new_messages)},
                )
                return

            tool_calls = _assistant_tool_calls(message)
            tool_results: list[AgentMessage] = []
            has_more_tool_calls = False
            hard_budget_stop = False

            if tool_calls:
                requested_calls = len(tool_calls)
                if (
                    config.max_tool_calls is not None
                    and budget.tool_calls_used + requested_calls
                    > config.max_tool_calls
                ):
                    # 总预算不足时整批拒绝，绝不执行“前几个成功、后几个失败”的
                    # 半批副作用。被拒绝调用不计入 used，但运行会在本 Turn 后结束。
                    await _emit_budget_exceeded(
                        emit,
                        budget="tool_calls",
                        limit=config.max_tool_calls,
                        used=budget.tool_calls_used,
                        requested=requested_calls,
                    )
                    batch = await _fail_tool_call_budget(
                        tool_calls,
                        emit,
                        limit=config.max_tool_calls,
                        used=budget.tool_calls_used,
                    )
                    hard_budget_stop = True
                else:
                    # 一整批先计入预算，再做未知工具、参数和 before hook 检查，
                    # 因此无效/被阻止调用也不能绕过工具总预算。
                    budget.tool_calls_used += requested_calls
                    if message.get("stopReason") == "length":
                        batch = await _fail_truncated_tool_calls(tool_calls, emit)
                    else:
                        batch = await _execute_tool_calls(
                            current_context,
                            message,
                            config,
                            cancellation,
                            emit,
                        )
                tool_results.extend(batch.messages)
                has_more_tool_calls = not batch.terminate
                for result_message in tool_results:
                    current_context.messages.append(result_message)
                    new_messages.append(result_message)

            await _emit(
                emit,
                {
                    "type": "turn_end",
                    "message": message,
                    "toolResults": tool_results,
                },
            )

            # Tool Call 硬预算超限时，错误结果已完整记录；现在直接结束，
            # 不再调用 prepareNextTurn，也不让队列消息绕过本次预算。
            if hard_budget_stop:
                await _emit(
                    emit,
                    {"type": "agent_end", "messages": list(new_messages)},
                )
                return

            turn_context = TurnCompletedContext(
                message=message,
                tool_results=tool_results,
                context=current_context,
                new_messages=new_messages,
            )

            # 允许产品层在下一次 Provider 请求前刷新模型、工具和 prompt。
            if config.prepare_next_turn is not None:
                update = await _maybe_await(config.prepare_next_turn(turn_context))
                if update is not None:
                    update = cast(AgentLoopTurnUpdate, update)
                    current_context = update.context or current_context
                    config = replace(
                        config,
                        model=update.model or config.model,
                        thinking_level=update.thinking_level
                        if update.thinking_level is not None
                        else config.thinking_level,
                        stream_options=dict(update.stream_options)
                        if update.stream_options is not None
                        else config.stream_options,
                    )

            if config.should_stop_after_turn is not None:
                should_stop = bool(
                    await _maybe_await(config.should_stop_after_turn(turn_context))
                )
                if should_stop:
                    await _emit(
                        emit,
                        {"type": "agent_end", "messages": list(new_messages)},
                    )
                    return

            # 当前 Turn 已经用尽预算时，不再领取 steering/follow-up，避免消息
            # 从内存队列取出后尚未处理就丢失。若工具要求继续，额外发预算事件。
            if (
                config.max_turns is not None
                and budget.turns_used >= config.max_turns
            ):
                if has_more_tool_calls:
                    await _emit_budget_exceeded(
                        emit,
                        budget="turns",
                        limit=config.max_turns,
                        used=budget.turns_used,
                        requested=1,
                    )
                await _emit(
                    emit,
                    {"type": "agent_end", "messages": list(new_messages)},
                )
                return

            pending_messages = await _get_messages(config.get_steering_messages)

        # 没有工具和 steering，Agent 本应停止；此时才检查 follow-up。
        follow_ups = await _get_messages(config.get_follow_up_messages)
        if follow_ups:
            pending_messages = follow_ups
            continue
        break

    await _emit(emit, {"type": "agent_end", "messages": list(new_messages)})


async def _get_messages(callback: Callable[[], Any] | None) -> list[AgentMessage]:
    """读取队列 callback；None 或返回 None 都视为空队列。"""

    if callback is None:
        return []
    result = await _maybe_await(callback())
    return list(result or [])


async def _stream_assistant_response(
    context: AgentContext,
    config: AgentLoopConfig,
    cancellation: CancellationToken,
    emit: EventSink,
    stream_fn: StreamFn,
) -> AgentMessage:
    """请求并消费一次 assistant 流。"""

    messages = list(context.messages)
    if config.transform_context is not None:
        transformed = await _maybe_await(
            config.transform_context(messages, cancellation)
        )
        messages = list(transformed)

    llm_messages = list(await _maybe_await(config.convert_to_llm(messages)))
    llm_context = {
        "systemPrompt": context.system_prompt,
        "messages": llm_messages,
        "tools": _serialize_tools(context.tools),
    }

    api_key = config.stream_options.get("api_key")
    if config.get_api_key is not None:
        dynamic_key = await _maybe_await(config.get_api_key(config.model.provider))
        api_key = dynamic_key or api_key

    options = dict(config.stream_options)
    options.update(
        {
            "api_key": api_key,
            "cancellation_token": cancellation,
            "reasoning": None
            if config.thinking_level == "off"
            else config.thinking_level,
            "retry_event_sink": config.retry_event_sink,
        }
    )

    response = await _maybe_await(stream_fn(config.model, llm_context, options))
    if not hasattr(response, "__aiter__") or not hasattr(response, "result"):
        raise TypeError("stream_fn 必须返回 AssistantMessageEventStream")
    response = cast(AssistantMessageEventStream, response)

    partial_message: AgentMessage | None = None
    added_partial = False

    async for event in response:
        event_type = event.get("type")
        if event_type == "start":
            partial_message = event["partial"]
            context.messages.append(partial_message)
            added_partial = True
            await _emit(
                emit,
                {
                    "type": "message_start",
                    "message": clone_message(partial_message),
                },
            )
        elif event_type in _MODEL_RETRY_EVENT_TYPES:
            # Retry 实现在 StreamFn/Host；低层循环只把结构化事件转发给 UI。
            await _emit(emit, dict(event))
        elif event_type in _ASSISTANT_UPDATE_TYPES:
            if partial_message is None:
                continue
            partial_message = event["partial"]
            context.messages[-1] = partial_message
            await _emit(
                emit,
                {
                    "type": "message_update",
                    "message": clone_message(partial_message),
                    "assistantMessageEvent": event,
                },
            )
        elif event_type in {"done", "error"}:
            final_message = await response.result()
            if added_partial:
                context.messages[-1] = final_message
            else:
                context.messages.append(final_message)
                await _emit(
                    emit,
                    {
                        "type": "message_start",
                        "message": clone_message(final_message),
                    },
                )
            await _emit(
                emit,
                {"type": "message_end", "message": final_message},
            )
            return final_message

    # 防御性 fallback：规范 Provider 应先发 done/error，再结束迭代。
    final_message = await response.result()
    if added_partial:
        context.messages[-1] = final_message
    else:
        context.messages.append(final_message)
        await _emit(
            emit,
            {"type": "message_start", "message": clone_message(final_message)},
        )
    await _emit(emit, {"type": "message_end", "message": final_message})
    return final_message


@dataclass(slots=True)
class _ExecutedToolBatch:
    messages: list[AgentMessage]
    terminate: bool


@dataclass(slots=True)
class _PreparedToolCall:
    tool_call: dict
    tool: AgentTool
    args: Any


@dataclass(slots=True)
class _FinalizedToolCall:
    tool_call: dict
    result: AgentToolResult
    is_error: bool


@dataclass(slots=True)
class _ImmediateToolCall:
    result: AgentToolResult
    is_error: bool


async def _emit_budget_exceeded(
    emit: EventSink,
    *,
    budget: str,
    limit: int,
    used: int,
    requested: int,
) -> None:
    """发出结构化预算耗尽事件，供 UI、日志和测试读取。"""

    label = "Turn" if budget == "turns" else "Tool Call"
    remaining = max(0, limit - used)
    await _emit(
        emit,
        {
            "type": "budget_exceeded",
            "budget": budget,
            "limit": limit,
            "used": used,
            "requested": requested,
            "remaining": remaining,
            "message": (
                f"{label} 预算不足：限制 {limit}，已使用 {used}，"
                f"本次请求 {requested}，剩余 {remaining}"
            ),
        },
    )


async def _fail_tool_call_budget(
    tool_calls: list[dict],
    emit: EventSink,
    *,
    limit: int,
    used: int,
) -> _ExecutedToolBatch:
    """总工具预算不足时整批拒绝，避免执行部分副作用。"""

    requested = len(tool_calls)
    remaining = max(0, limit - used)
    messages: list[AgentMessage] = []
    for tool_call in tool_calls:
        await _emit_tool_start(tool_call, emit)
        tool_name = str(tool_call.get("name", ""))
        finalized = _FinalizedToolCall(
            tool_call=tool_call,
            result=error_tool_result(
                f"工具 {tool_name} 未执行：Tool Call 预算不足。"
                f"限制 {limit}，已使用 {used}，本批请求 {requested}，"
                f"剩余 {remaining}。",
                terminate=True,
                details={
                    "code": "tool_call_budget_exceeded",
                    "limit": limit,
                    "used": used,
                    "requested": requested,
                    "remaining": remaining,
                },
            ),
            is_error=True,
        )
        await _emit_tool_end(finalized, emit)
        message = _create_tool_result_message(finalized)
        await _emit_tool_result_message(message, emit)
        messages.append(message)
    return _ExecutedToolBatch(messages=messages, terminate=True)


async def _fail_truncated_tool_calls(
    tool_calls: list[dict],
    emit: EventSink,
) -> _ExecutedToolBatch:
    """拒绝执行来自 length 截断 assistant 的全部工具调用。"""

    messages: list[AgentMessage] = []
    for tool_call in tool_calls:
        await _emit_tool_start(tool_call, emit)
        finalized = _FinalizedToolCall(
            tool_call=tool_call,
            result=error_tool_result(
                f'工具 "{tool_call.get("name", "")}" 未执行：模型输出达到长度上限，'
                "参数可能被截断。请使用完整参数重新发起工具调用。"
            ),
            is_error=True,
        )
        await _emit_tool_end(finalized, emit)
        message = _create_tool_result_message(finalized)
        await _emit_tool_result_message(message, emit)
        messages.append(message)
    return _ExecutedToolBatch(messages=messages, terminate=False)


async def _execute_tool_calls(
    context: AgentContext,
    assistant_message: AgentMessage,
    config: AgentLoopConfig,
    cancellation: CancellationToken,
    emit: EventSink,
) -> _ExecutedToolBatch:
    tool_calls = _assistant_tool_calls(assistant_message)
    tools_by_name = {tool.name: tool for tool in context.tools}
    contains_sequential_tool = any(
        tools_by_name.get(str(call.get("name"))) is not None
        and tools_by_name[str(call.get("name"))].execution_mode == "sequential"
        for call in tool_calls
    )
    if config.tool_execution == "sequential" or contains_sequential_tool:
        return await _execute_tools_sequential(
            context,
            assistant_message,
            tool_calls,
            config,
            cancellation,
            emit,
        )
    return await _execute_tools_parallel(
        context,
        assistant_message,
        tool_calls,
        config,
        cancellation,
        emit,
    )


async def _execute_tools_sequential(
    context: AgentContext,
    assistant_message: AgentMessage,
    tool_calls: list[dict],
    config: AgentLoopConfig,
    cancellation: CancellationToken,
    emit: EventSink,
) -> _ExecutedToolBatch:
    finalized_calls: list[_FinalizedToolCall] = []
    messages: list[AgentMessage] = []

    for tool_call in tool_calls:
        await _emit_tool_start(tool_call, emit)
        preparation = await _prepare_tool_call(
            context,
            assistant_message,
            tool_call,
            config,
            cancellation,
        )
        if isinstance(preparation, _ImmediateToolCall):
            finalized = _FinalizedToolCall(
                tool_call=tool_call,
                result=preparation.result,
                is_error=preparation.is_error,
            )
        else:
            executed_result, is_error = await _execute_prepared_tool(
                preparation,
                cancellation,
                emit,
                config.default_tool_timeout_seconds,
                retry_event_sink=config.retry_event_sink,
            )
            finalized = await _finalize_executed_tool(
                context,
                assistant_message,
                preparation,
                executed_result,
                is_error,
                config,
                cancellation,
            )

        await _emit_tool_end(finalized, emit)
        result_message = _create_tool_result_message(finalized)
        await _emit_tool_result_message(result_message, emit)
        finalized_calls.append(finalized)
        messages.append(result_message)
        if cancellation.cancelled:
            break

    return _ExecutedToolBatch(
        messages=messages,
        terminate=_should_terminate_batch(finalized_calls),
    )


async def _execute_tools_parallel(
    context: AgentContext,
    assistant_message: AgentMessage,
    tool_calls: list[dict],
    config: AgentLoopConfig,
    cancellation: CancellationToken,
    emit: EventSink,
) -> _ExecutedToolBatch:
    """顺序预检、并发执行、按 source order 生成 ToolResultMessage。"""

    # Semaphore 只限制真正进入工具 execute 的数量。所有工具仍按模型顺序
    # 完成预检；等待槽位的工具不会占用并发执行名额。
    parallel_limit = config.max_parallel_tools or max(1, len(tool_calls))
    semaphore = asyncio.Semaphore(parallel_limit)

    # entries 保持 assistant 原始调用顺序。立即失败存 finalized；通过预检则存
    # 一个异步工厂，等预检阶段完成后再统一 create_task。
    entries: list[
        _FinalizedToolCall | Callable[[], Awaitable[_FinalizedToolCall]]
    ] = []

    for tool_call in tool_calls:
        await _emit_tool_start(tool_call, emit)
        preparation = await _prepare_tool_call(
            context,
            assistant_message,
            tool_call,
            config,
            cancellation,
        )
        if isinstance(preparation, _ImmediateToolCall):
            finalized = _FinalizedToolCall(
                tool_call=tool_call,
                result=preparation.result,
                is_error=preparation.is_error,
            )
            # Pi 语义：立即失败在预检阶段就发 execution_end。
            await _emit_tool_end(finalized, emit)
            entries.append(finalized)
        else:

            async def run_one(
                prepared: _PreparedToolCall = preparation,
            ) -> _FinalizedToolCall:
                result, is_error = await _execute_prepared_tool(
                    prepared,
                    cancellation,
                    emit,
                    config.default_tool_timeout_seconds,
                    execution_semaphore=semaphore,
                    retry_event_sink=config.retry_event_sink,
                )
                finalized_result = await _finalize_executed_tool(
                    context,
                    assistant_message,
                    prepared,
                    result,
                    is_error,
                    config,
                    cancellation,
                )
                # end 在每个任务中发，因此谁先完成谁先发。
                await _emit_tool_end(finalized_result, emit)
                return finalized_result

            entries.append(run_one)

        if cancellation.cancelled:
            break

    tasks: list[asyncio.Task[_FinalizedToolCall] | None] = []
    for entry in entries:
        if isinstance(entry, _FinalizedToolCall):
            tasks.append(None)
        else:
            tasks.append(asyncio.create_task(entry()))

    # 一次 gather 等待所有已启动任务，并保持结果数组与 source order 一致。
    # 任一任务出现调度/listener 异常时，明确取消并等待剩余任务，避免后台泄漏。
    running_tasks = [task for task in tasks if task is not None]
    try:
        running_results = (
            await asyncio.gather(*running_tasks) if running_tasks else []
        )
    except BaseException:
        for task in running_tasks:
            if not task.done():
                task.cancel()
        if running_tasks:
            await asyncio.gather(*running_tasks, return_exceptions=True)
        raise

    finalized_calls: list[_FinalizedToolCall] = []
    running_index = 0
    for entry, task in zip(entries, tasks, strict=True):
        if task is None:
            finalized_calls.append(cast(_FinalizedToolCall, entry))
        else:
            finalized_calls.append(running_results[running_index])
            running_index += 1

    messages: list[AgentMessage] = []
    for finalized in finalized_calls:
        result_message = _create_tool_result_message(finalized)
        await _emit_tool_result_message(result_message, emit)
        messages.append(result_message)

    return _ExecutedToolBatch(
        messages=messages,
        terminate=_should_terminate_batch(finalized_calls),
    )


async def _prepare_tool_call(
    context: AgentContext,
    assistant_message: AgentMessage,
    tool_call: dict,
    config: AgentLoopConfig,
    cancellation: CancellationToken,
) -> _PreparedToolCall | _ImmediateToolCall:
    tool_name = str(tool_call.get("name", ""))
    tool = next((item for item in context.tools if item.name == tool_name), None)
    if tool is None:
        return _ImmediateToolCall(
            result=error_tool_result(f"工具不存在：{tool_name}"),
            is_error=True,
        )

    try:
        raw_args = tool_call.get("arguments", {})
        if tool.prepare_arguments is not None:
            raw_args = tool.prepare_arguments(raw_args)
        validated_args = tool.validate_args(raw_args)

        if config.before_tool_call is not None:
            before_result = await _maybe_await(
                config.before_tool_call(
                    BeforeToolCallContext(
                        assistant_message=assistant_message,
                        tool_call=tool_call,
                        args=validated_args,
                        context=context,
                    ),
                    cancellation,
                )
            )
            if cancellation.cancelled:
                return _ImmediateToolCall(
                    result=error_tool_result("操作已取消"),
                    is_error=True,
                )
            if isinstance(before_result, BeforeToolCallResult) and before_result.block:
                return _ImmediateToolCall(
                    result=error_tool_result(
                        before_result.reason or "工具执行已被阻止",
                        terminate=before_result.terminate,
                    ),
                    is_error=True,
                )

        if cancellation.cancelled:
            return _ImmediateToolCall(
                result=error_tool_result("操作已取消"),
                is_error=True,
            )
        return _PreparedToolCall(tool_call=tool_call, tool=tool, args=validated_args)
    except Exception as error:
        return _ImmediateToolCall(
            result=error_tool_result(str(error)),
            is_error=True,
        )


async def _execute_prepared_tool(
    prepared: _PreparedToolCall,
    cancellation: CancellationToken,
    emit: EventSink,
    default_timeout_seconds: float | None,
    *,
    execution_semaphore: asyncio.Semaphore | None = None,
    retry_event_sink: Callable[[AgentEvent], Any] | None = None,
) -> tuple[AgentToolResult, bool]:
    """执行一个已验证工具，并实现单工具独立超时。

    每个工具获得 Agent 总取消令牌的子令牌：

    - 用户取消 Agent 时，父令牌会取消所有子令牌；
    - 当前工具超时时，只取消自己的子令牌；
    - 其他并行工具继续运行。

    超时只对能让出 asyncio 事件循环的异步工具有效。完全阻塞事件循环的同步
    死循环无法被 asyncio 定时器打断，生产系统应把这种工作放入子进程。
    """

    update_tasks: list[asyncio.Task[None]] = []
    accepting_updates = True
    tool_name = str(prepared.tool_call.get("name", ""))
    timeout_seconds = (
        prepared.tool.timeout_seconds
        if prepared.tool.timeout_seconds is not None
        else default_timeout_seconds
    )

    # 子令牌只控制当前工具。它会继承父令牌的用户取消，但自己的 timeout
    # 不会反向传播给 Agent 或其他并行工具。
    tool_cancellation = cancellation.create_child()

    def on_update(partial_result: AgentToolResult) -> None:
        nonlocal accepting_updates
        if not accepting_updates:
            return
        update_tasks.append(
            asyncio.create_task(
                _emit(
                    emit,
                    {
                        "type": "tool_execution_update",
                        "toolCallId": str(prepared.tool_call.get("id", "")),
                        "toolName": tool_name,
                        "args": copy.deepcopy(prepared.tool_call.get("arguments", {})),
                        "partialResult": _tool_result_payload(partial_result),
                    },
                )
            )
        )

    tool_call_id = str(prepared.tool_call.get("id", ""))
    execution_started = asyncio.Event()

    async def execute_once() -> AgentToolResult:
        if execution_semaphore is None:
            execution_started.set()
            return await prepared.tool.execute(
                tool_call_id,
                prepared.args,
                tool_cancellation,
                on_update,
            )
        # Retry Backoff 不占并发槽；每个新 Attempt 重新申请。
        async with execution_semaphore:
            execution_started.set()
            return await prepared.tool.execute(
                tool_call_id,
                prepared.args,
                tool_cancellation,
                on_update,
            )

    async def emit_retry_event(event: AgentEvent) -> None:
        # 持久化 Hook 先完成，再向 UI 发布，接近 DeepSeek Harness 的 Durable 语义。
        if retry_event_sink is not None:
            await _maybe_await(retry_event_sink(dict(event)))
        await _emit(emit, event)

    execute_task = asyncio.create_task(
        execute_tool_with_retry(
            execute=execute_once,
            policy=prepared.tool.retry_policy,
            cancellation=tool_cancellation,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            emit=emit_retry_event,
        )
    )
    cancellation_wait_task = asyncio.create_task(tool_cancellation.wait())

    async def wait_for_timeout() -> None:
        # 初次排队等待并发槽不消耗工具 Timeout；第一次真正执行后开始计时。
        await execution_started.wait()
        await asyncio.sleep(cast(float, timeout_seconds))

    timeout_task = (
        asyncio.create_task(wait_for_timeout())
        if timeout_seconds is not None
        else None
    )

    async def stop_execute_task() -> None:
        """取消并等待工具协程收尾，避免遗留无人管理的 asyncio Task。"""

        if not execute_task.done():
            execute_task.cancel()
        try:
            await execute_task
        except asyncio.CancelledError:
            pass
        except Exception:
            # 超时/用户取消是当前工具的最终分类。工具在取消收尾中抛出的
            # 次要异常不应覆盖这个更早、更明确的原因。
            pass

    result: AgentToolResult
    is_error: bool

    try:
        waiters: set[asyncio.Task[Any]] = {execute_task, cancellation_wait_task}
        if timeout_task is not None:
            waiters.add(timeout_task)
        done, _pending = await asyncio.wait(
            waiters,
            return_when=asyncio.FIRST_COMPLETED,
        )

        # 工具与 timeout 同时完成时优先接受已经完成的工具结果。
        if execute_task in done:
            try:
                result = await execute_task
                is_error = False
            except (asyncio.CancelledError, OperationCancelledError):
                result = error_tool_result(
                    f"工具 {tool_name} 已取消：{tool_cancellation.reason}",
                    details={"code": "tool_cancelled"},
                )
                is_error = True
            except OutcomeUnknownToolError as error:
                result = error_tool_result(
                    str(error),
                    details={
                        "code": "outcome_unknown",
                        "operationId": error.operation_id,
                        "reconciliationName": error.reconciliation_name,
                        "retryable": False,
                    },
                )
                is_error = True
            except RetryableToolError as error:
                result = error_tool_result(
                    str(error),
                    details={
                        "code": error.code,
                        "retryable": True,
                        "attempts": error.attempts,
                        **(
                            {"retryId": error.retry_id}
                            if error.retry_id is not None
                            else {}
                        ),
                    },
                )
                is_error = True
            except Exception as error:
                result = error_tool_result(
                    str(error),
                    details={"code": "tool_execution_error"},
                )
                is_error = True
        elif timeout_task is not None and timeout_task in done:
            accepting_updates = False
            timeout_text = f"{timeout_seconds:g}"
            tool_cancellation.cancel(
                f"工具 {tool_name} 执行超过 {timeout_text} 秒"
            )
            await stop_execute_task()
            result = error_tool_result(
                f"工具 {tool_name} 执行超时（限制 {timeout_text} 秒）",
                details={
                    "code": "tool_timeout",
                    "toolName": tool_name,
                    "timeoutSeconds": timeout_seconds,
                },
            )
            is_error = True
        else:
            # cancellation_wait_task 完成，说明用户取消 Agent，或上层主动取消
            # 当前工具子令牌。
            accepting_updates = False
            await stop_execute_task()
            result = error_tool_result(
                f"工具 {tool_name} 已取消：{tool_cancellation.reason}",
                details={
                    "code": "tool_cancelled",
                    "toolName": tool_name,
                    "reason": tool_cancellation.reason,
                },
            )
            is_error = True
    finally:
        accepting_updates = False

        # 清理 timeout/cancellation 等待任务，确保工具结束后没有 timer 残留。
        waiter_tasks = [cancellation_wait_task]
        if timeout_task is not None:
            waiter_tasks.append(timeout_task)
        for waiter in waiter_tasks:
            if not waiter.done():
                waiter.cancel()
        if waiter_tasks:
            await asyncio.gather(*waiter_tasks, return_exceptions=True)

        # 当前工具在超时或取消前已经发出的 update 必须先结算，再允许发送
        # tool_execution_end。超时之后到来的 update 会因 accepting_updates=False
        # 被忽略。
        if update_tasks:
            await asyncio.gather(*update_tasks)
        tool_cancellation.detach()

    return result, is_error


async def _finalize_executed_tool(
    context: AgentContext,
    assistant_message: AgentMessage,
    prepared: _PreparedToolCall,
    result: AgentToolResult,
    is_error: bool,
    config: AgentLoopConfig,
    cancellation: CancellationToken,
) -> _FinalizedToolCall:
    """运行 after hook，并应用逐字段覆盖。"""

    if config.after_tool_call is not None:
        try:
            override = await _maybe_await(
                config.after_tool_call(
                    AfterToolCallContext(
                        assistant_message=assistant_message,
                        tool_call=prepared.tool_call,
                        args=prepared.args,
                        result=result,
                        is_error=is_error,
                        context=context,
                    ),
                    cancellation,
                )
            )
            if isinstance(override, AfterToolCallResult):
                result = AgentToolResult(
                    content=result.content
                    if override.content is UNSET
                    else list(override.content),
                    details=result.details
                    if override.details is UNSET
                    else override.details,
                    usage=result.usage if override.usage is UNSET else override.usage,
                    added_tool_names=result.added_tool_names,
                    terminate=result.terminate
                    if override.terminate is UNSET
                    else override.terminate,
                )
                if override.is_error is not UNSET:
                    is_error = bool(override.is_error)
        except Exception as error:
            result = error_tool_result(str(error))
            is_error = True

    return _FinalizedToolCall(
        tool_call=prepared.tool_call,
        result=result,
        is_error=is_error,
    )


def _should_terminate_batch(finalized_calls: list[_FinalizedToolCall]) -> bool:
    return bool(finalized_calls) and all(
        finalized.result.terminate is True for finalized in finalized_calls
    )


async def _emit_tool_start(tool_call: dict, emit: EventSink) -> None:
    await _emit(
        emit,
        {
            "type": "tool_execution_start",
            "toolCallId": str(tool_call.get("id", "")),
            "toolName": str(tool_call.get("name", "")),
            "args": copy.deepcopy(tool_call.get("arguments", {})),
        },
    )


async def _emit_tool_end(finalized: _FinalizedToolCall, emit: EventSink) -> None:
    await _emit(
        emit,
        {
            "type": "tool_execution_end",
            "toolCallId": str(finalized.tool_call.get("id", "")),
            "toolName": str(finalized.tool_call.get("name", "")),
            "result": _tool_result_payload(finalized.result),
            "isError": finalized.is_error,
        },
    )


def _create_tool_result_message(finalized: _FinalizedToolCall) -> AgentMessage:
    message: AgentMessage = {
        "role": "toolResult",
        "toolCallId": str(finalized.tool_call.get("id", "")),
        "toolName": str(finalized.tool_call.get("name", "")),
        "content": copy.deepcopy(finalized.result.content or []),
        "details": copy.deepcopy(finalized.result.details),
        "isError": finalized.is_error,
        "timestamp": now_ms(),
    }
    if finalized.result.usage is not None:
        message["usage"] = copy.deepcopy(finalized.result.usage)
    if finalized.result.added_tool_names:
        message["addedToolNames"] = list(finalized.result.added_tool_names)
    return message


async def _emit_tool_result_message(message: AgentMessage, emit: EventSink) -> None:
    await _emit(
        emit,
        {"type": "message_start", "message": clone_message(message)},
    )
    await _emit(emit, {"type": "message_end", "message": message})
