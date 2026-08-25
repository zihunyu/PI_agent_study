"""Pi Agent Loop 的 Python 改写。

本模块刻意只处理低层机制：

- 组装模型上下文并消费流式 assistant 事件；
- 执行串行或并行工具；
- 处理 steering 与 follow-up 队列；
- 发出 Agent/Turn/Message/Tool 生命周期事件。

自动重试、上下文压缩、JSONL/SQLite 持久化和 UI 不属于低层循环，应该由
更高层 Session/Host 编排。这正是 Pi 原设计最值得借鉴的分层之一。
"""

from __future__ import annotations

import asyncio
import copy
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, cast

from .cancellation import CancellationToken
from .event_stream import (
    AgentEventStream,
    AssistantMessageEventStream,
)
from .messages import clone_message, error_tool_result, now_ms
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
    pending_messages = await _get_messages(config.get_steering_messages)

    while True:
        has_more_tool_calls = True

        while has_more_tool_calls or pending_messages:
            if not first_turn:
                await _emit(emit, {"type": "turn_start"})
            else:
                first_turn = False

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

            if tool_calls:
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

    finalized_calls: list[_FinalizedToolCall] = []
    for entry, task in zip(entries, tasks, strict=True):
        finalized_calls.append(entry if task is None else await task)

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
) -> tuple[AgentToolResult, bool]:
    """执行一个已验证工具，并保证该工具的 update 在 end 前结算。"""

    update_tasks: list[asyncio.Task[None]] = []
    accepting_updates = True

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
                        "toolName": str(prepared.tool_call.get("name", "")),
                        "args": copy.deepcopy(prepared.tool_call.get("arguments", {})),
                        "partialResult": _tool_result_payload(partial_result),
                    },
                )
            )
        )

    try:
        result = await prepared.tool.execute(
            str(prepared.tool_call.get("id", "")),
            prepared.args,
            cancellation,
            on_update,
        )
        accepting_updates = False
        if update_tasks:
            await asyncio.gather(*update_tasks)
        return result, False
    except Exception as error:
        accepting_updates = False
        if update_tasks:
            await asyncio.gather(*update_tasks)
        return error_tool_result(str(error)), True
    finally:
        accepting_updates = False


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
