# Pi Agent Loop 的 Python 改写

本目录是对 Pi Agent Loop 核心语义的纯 Python 改写，适合用于：

- 学习 Agent Loop 的工作原理；
- 编写自己的命令行 Agent；
- 测试模型调用和工具调用流程；
- 作为后续加入持久化、重试、压缩和 UI 的基础。

本实现不是 Pi 官方 Python 包，也没有复制 Pi 的全部 Coding Agent 产品功能。它重点保留 Pi 低层 Agent Loop 最有价值的控制语义，并使用中文注释解释关键代码。

参考源码：

- `pi/packages/agent/src/agent-loop.ts`
- `pi/packages/agent/src/agent.ts`
- `pi/packages/agent/src/types.ts`
- `pi/packages/agent/src/stream-fn.ts`

Pi 原项目采用 MIT License。本目录保留了原许可证文件；若继续分发或改造，请同时遵守许可证要求。

---

## 1. 已实现的能力

### 1.1 Agent 循环

核心流程：

```text
用户消息
  -> 请求模型
  -> 接收流式 assistant 消息
  -> 提取工具调用
  -> 校验并执行工具
  -> 把工具结果加入上下文
  -> 再次请求模型
  -> 没有工具时结束
```

### 1.2 两种队列

- `steer()`：当前 assistant 的工具批次结束后、下一次模型请求前插入；
- `follow_up()`：Agent 原本准备结束时才插入。

两种队列都支持：

- `all`：一次取出全部消息；
- `one-at-a-time`：每次只取最早一条。

### 1.3 工具执行

支持：

- 参数预处理；
- 参数校验；
- `before_tool_call`；
- `after_tool_call`；
- 串行执行；
- 并行执行；
- 工具流式进度；
- 工具错误转换成 ToolResultMessage；
- `terminate` 提示。

并行模式保留 Pi 的重要顺序规则：

```text
预检：按模型原始顺序
执行：并发
完成事件：哪个先完成就先发哪个
工具结果回灌：仍按模型原始顺序
```

### 1.4 安全处理

当模型因为输出长度上限而停止，并且消息中包含工具调用时，本实现不会执行任何工具。

原因是工具参数可能只生成了一半。即使残缺 JSON 能被修复，也不能证明参数语义完整。

### 1.5 生命周期事件

支持以下事件：

- `agent_start`
- `agent_end`
- `turn_start`
- `turn_end`
- `message_start`
- `message_update`
- `message_end`
- `tool_execution_start`
- `tool_execution_update`
- `tool_execution_end`

### 1.6 取消与结算

- 每次运行创建独立 `CancellationToken`；
- Provider、工具和钩子可以检查取消；
- `wait_for_idle()` 会等待 Agent Loop 和异步 listener 完成；
- 同一 Agent 同时只允许一个主 prompt/continuation。

### 1.7 确定性测试 Provider

`ScriptedProvider` 可以：

- 按顺序返回预设模型消息；
- 模拟 text/thinking/tool call 流式事件；
- 模拟 error、aborted、length；
- 记录调用次数和每次模型上下文；
- 控制 chunk 大小和发送延迟。

因此测试不需要真实 API key，也不依赖在线大模型。

---

## 2. 目录结构

```text
agent-loop/
├─ README.md                         中文说明
├─ LICENSE                           Pi 原项目 MIT 许可证
├─ pyproject.toml                    Python 包配置
├─ examples/
│  ├─ minimal_text.py                最简单的纯文本示例
│  └─ basic_usage.py                 带逐步中文说明的并行工具示例
├─ tests/
│  └─ test_agent_loop.py             核心语义测试
└─ src/pi_agent_loop/
   ├─ __init__.py                    公开导出
   ├─ cancellation.py                合作式取消令牌
   ├─ event_stream.py                异步事件流和最终结果
   ├─ messages.py                    消息构造与复制
   ├─ types.py                       Model、Tool、Config 等类型
   ├─ loop.py                        低层 Agent Loop
   ├─ agent.py                       有状态 Agent 封装
   └─ testing.py                     ScriptedProvider
```

---

## 3. 环境要求

- Python 3.11 或更高版本；
- 仅使用 Python 标准库；
- 不需要安装模型 SDK；
- 运行示例和测试不需要网络。

当前环境已使用 Python 3.12 验证。

---

## 4. 快速运行

进入目录：

```bash
cd agent-loop
```

### 4.1 第一次运行：先看最简单的纯文本示例

```bash
python examples/minimal_text.py
```

输出大致如下：

```text
用户输入： 你好，请介绍你自己。
Agent 正在把用户消息交给假模型……
模型回答： 你好！这是一条由假模型返回的固定回答。

说明：这个例子没有工具调用，也没有连接真实网络。
```

这个例子只有一件事：

```text
用户消息 -> 假模型 -> 固定文本回答
```

### 4.2 第二次运行：再看带工具的完整示例

```bash
python examples/basic_usage.py
```

这个示例会逐步打印中文故事线。实际过程是：

```text
第一次调用假模型
  -> 假模型不直接回答
  -> 它要求执行 add 和 multiply
  -> 两个工具并行计算
  -> 结果 5 和 20 写回对话
  -> 第二次调用假模型
  -> 假模型把工具结果整理成最终回答
```

所以最后显示：

```text
最终回答：加法结果是 5，乘法结果是 20。
模型调用次数：2
```

模型调用两次不是重复执行：

1. 第一次让模型决定“需要调用哪些工具”；
2. 第二次让模型阅读工具结果并组织最终回答。

### 4.3 如何理解事件名称

| 事件 | 最简单的含义 |
|---|---|
| `agent_start` | 整个 Agent 任务开始 |
| `turn_start` | 准备调用一次模型 |
| `message_start` | 开始处理一条用户、模型或工具消息 |
| `message_update` | 模型流式增加了一小段内容 |
| `message_end` | 一条消息已经完整结束 |
| `tool_execution_start` | 某个工具开始执行 |
| `tool_execution_update` | 工具报告中间进度 |
| `tool_execution_end` | 某个工具执行结束 |
| `turn_end` | 这一轮模型回复和工具执行全部完成 |
| `agent_end` | 没有更多工作，低层 Agent Loop 结束 |

原始输出中多次出现 `message_start/message_end` 是正常的，因为以下内容都属于消息：

- 用户输入是一条消息；
- 模型要求调用工具是一条消息；
- 每个工具结果各是一条消息；
- 模型最终回答又是一条消息。

### 4.4 运行全部测试

```bash
python -m unittest discover -s tests -v
```

测试输出中的：

```text
... ok
Ran 6 tests
OK
```

表示六个自动测试全部通过，并不是 Agent 又执行了六个用户任务。

六个测试分别检查：

1. 最终回答能否进入 Agent 状态；
2. 工具结果能否交回模型并触发第二次模型请求；
3. 并行工具的完成顺序与结果顺序是否正确；
4. 被长度上限截断的工具参数是否会被安全拒绝；
5. follow-up 是否等原任务结束后才处理；
6. `agent_end` 的异步监听器结束前，Agent 是否仍保持忙碌。

### 4.5 可选安装

以 editable 模式安装：

```bash
python -m pip install -e .
```

安装后可以在任意 Python 程序中：

```python
from pi_agent_loop import Agent, AgentTool, Model
```

---

## 5. 最小示例

```python
import asyncio

from pi_agent_loop import (
    Agent,
    Model,
    ScriptedProvider,
    assistant_message,
)


async def main():
    model = Model(id="demo", provider="fake", api="fake")

    provider = ScriptedProvider([
        assistant_message(
            model=model,
            content=[{"type": "text", "text": "你好，我已经完成任务。"}],
        )
    ])

    agent = Agent(
        model=model,
        stream_fn=provider.stream,
        system_prompt="你是一个简洁的助手。",
    )

    await agent.prompt("你好")
    print(agent.state.messages[-1]["content"][0]["text"])


asyncio.run(main())
```

---

## 6. 消息格式

为了保持零依赖和容易 JSON 序列化，消息使用普通 Python 字典。

### 6.1 用户消息

```python
{
    "role": "user",
    "content": [
        {"type": "text", "text": "请读取配置"}
    ],
    "timestamp": 1234567890,
}
```

### 6.2 Assistant 文本消息

```python
{
    "role": "assistant",
    "content": [
        {"type": "text", "text": "配置内容如下"}
    ],
    "api": "fake",
    "provider": "fake",
    "model": "demo",
    "usage": {...},
    "stopReason": "stop",
    "timestamp": 1234567890,
}
```

### 6.3 Assistant 工具调用

```python
{
    "role": "assistant",
    "content": [
        {
            "type": "toolCall",
            "id": "call-1",
            "name": "read",
            "arguments": {"path": "README.md"},
        }
    ],
    "stopReason": "toolUse",
    ...
}
```

### 6.4 工具结果消息

```python
{
    "role": "toolResult",
    "toolCallId": "call-1",
    "toolName": "read",
    "content": [
        {"type": "text", "text": "文件内容"}
    ],
    "details": {},
    "isError": False,
    "timestamp": 1234567890,
}
```

应用可以增加自定义消息 role，但必须通过 `convert_to_llm` 转成模型 Provider 能理解的格式，或者在该函数中把它过滤掉。

---

## 7. Provider 接口

`stream_fn` 接收三个参数：

```python
stream_fn(model, context, options)
```

### 7.1 `model`

`Model` 对象，至少包含：

- `id`
- `provider`
- `api`

### 7.2 `context`

```python
{
    "systemPrompt": "...",
    "messages": [...],
    "tools": [...],
}
```

其中 `tools` 已转换成可序列化定义：

```python
{
    "name": "read",
    "description": "读取文件",
    "parameters": {"type": "object", ...},
}
```

### 7.3 `options`

包含调用方提供的 `stream_options`，以及循环补充的：

- `api_key`
- `cancellation_token`
- `reasoning`

### 7.4 返回值

必须返回 `AssistantMessageEventStream`，并遵守：

```text
start
  -> text/thinking/toolcall start/delta/end
  -> done 或 error
```

`done` 和 `error` 必须只有一个。

真实 Provider 可以参考 `ScriptedProvider` 的写法，再接入自己的 OpenAI、Anthropic 或 DeepSeek SDK。

---

## 8. 自定义工具

### 8.1 定义工具

```python
from pi_agent_loop import AgentTool, AgentToolResult


def validate(arguments):
    if not isinstance(arguments, dict):
        raise ValueError("参数必须是对象")
    if not isinstance(arguments.get("value"), int):
        raise ValueError("value 必须是整数")
    return arguments


async def execute(tool_call_id, arguments, cancellation, on_update):
    cancellation.throw_if_cancelled()

    on_update(
        AgentToolResult(
            content=[{"type": "text", "text": "正在处理"}],
            details={"phase": "working"},
        )
    )

    result = arguments["value"] * 2
    return AgentToolResult(
        content=[{"type": "text", "text": str(result)}],
        details={"value": result},
    )


tool = AgentTool(
    name="double",
    label="翻倍",
    description="把整数翻倍",
    parameters={
        "type": "object",
        "properties": {"value": {"type": "integer"}},
        "required": ["value"],
    },
    validate_args=validate,
    execute=execute,
)
```

### 8.2 `parameters` 与 `validate_args` 的区别

- `parameters`：给模型看的 JSON Schema；
- `validate_args`：Python 运行时真正执行的验证函数。

本项目没有引入 JSON Schema 第三方验证器，因此两者不会自动关联。生产实现可以接入：

- `jsonschema`
- Pydantic
- msgspec
- 自己的严格校验函数

### 8.3 工具错误

工具执行函数应在失败时抛异常：

```python
raise RuntimeError("文件不存在")
```

Agent Loop 会把异常变成：

```python
{
    "role": "toolResult",
    "isError": True,
    "content": [{"type": "text", "text": "文件不存在"}],
}
```

这样模型可以读取错误并决定如何修正。

---

## 9. 串行与并行工具

### 9.1 默认并行

```python
agent = Agent(
    ...,
    tool_execution="parallel",
)
```

### 9.2 全局串行

```python
agent = Agent(
    ...,
    tool_execution="sequential",
)
```

### 9.3 单工具要求串行

```python
tool = AgentTool(
    ...,
    execution_mode="sequential",
)
```

只要一批调用中有一个工具要求串行，整批都会串行。这与 Pi 原实现保持一致。

### 9.4 为什么工具结果不按完成顺序交给模型

假设模型按以下顺序调用：

```text
1. read A
2. read B
```

B 可能先读完，但最终上下文仍按：

```text
result A
result B
```

这样同一任务重复运行时，模型看到的历史不会因为操作系统调度差异而随机变化。

---

## 10. Steering 与 Follow-up

### 10.1 Steering

```python
agent.steer("重点检查安全配置")
```

当前工具批次结束后、下一次模型请求前注入。

适合：

- 纠正方向；
- 补充当前任务信息；
- 告诉 Agent 暂时不要走某种方案。

### 10.2 Follow-up

```python
agent.follow_up("完成以后再生成迁移说明")
```

只有 Agent 原本准备停止时才处理。

适合：

- 当前任务完成后继续下一项；
- 排队多个独立要求；
- 不希望干扰当前工具链的任务。

### 10.3 运行中不能再次 prompt

错误示例：

```python
first = asyncio.create_task(agent.prompt("任务一"))
await agent.prompt("任务二")  # 会抛 RuntimeError
```

正确做法：

```python
first = asyncio.create_task(agent.prompt("任务一"))
agent.steer("这是任务一的补充")
agent.follow_up("任务一完成后做任务二")
await first
```

---

## 11. 事件订阅

```python
async def listener(event, cancellation):
    print(event["type"])

unsubscribe = agent.subscribe(listener)
await agent.prompt("你好")
unsubscribe()
```

Agent 会先更新 `AgentState`，再调用 listener。

同一事件的 listener 按注册顺序逐个等待。某个 listener 抛异常时，异常会向上传播；这是接近 Pi 的严格行为。

生产项目建议在 listener 内部捕获自己的诊断错误：

```python
async def safe_listener(event, cancellation):
    try:
        await save_event(event)
    except Exception as error:
        logger.exception("保存事件失败", exc_info=error)
```

如果希望 UI listener 失败不影响 Agent，可在宿主层实现统一 observer error policy。

---

## 12. 取消

```python
task = asyncio.create_task(agent.prompt("长任务"))
await asyncio.sleep(1)
agent.abort("用户按下取消")
await task
```

Provider 应检查：

```python
token = options["cancellation_token"]
token.throw_if_cancelled()
```

工具应检查：

```python
async def execute(_id, arguments, cancellation, on_update):
    cancellation.throw_if_cancelled()
    ...
```

### 12.1 重要限制

CancellationToken 是合作式取消。

以下同步函数若完全不检查 token，Agent Loop 无法强制停止它：

```python
def blocking_forever():
    while True:
        pass
```

生产工具应使用：

- 子进程；
- timeout；
- 进程组清理；
- 独立 worker；
- 可取消的异步 I/O。

---

## 13. Hook

### 13.1 执行前阻止工具

```python
from pi_agent_loop import BeforeToolCallResult


async def before_tool(context, cancellation):
    if context.tool_call["name"] == "delete_all":
        return BeforeToolCallResult(
            block=True,
            reason="禁止危险操作",
            terminate=True,
        )
    return None
```

### 13.2 执行后修改结果

```python
from pi_agent_loop import AfterToolCallResult


async def after_tool(context, cancellation):
    return AfterToolCallResult(
        content=[{"type": "text", "text": "结果已脱敏"}],
        is_error=False,
    )
```

绑定：

```python
agent = Agent(
    ...,
    before_tool_call=before_tool,
    after_tool_call=after_tool,
)
```

### 13.3 注意事项

未知工具、参数校验失败和被 `before_tool_call` 阻止的调用属于“立即结果”，不会再进入 `after_tool_call`。

如果需要所有结果统一审计，可在 `tool_execution_end` listener 中处理。

---

## 14. 与 Pi TypeScript 版本的对应关系

| Pi TypeScript | Python 改写 |
|---|---|
| `AbortController/AbortSignal` | `CancellationToken` |
| `EventStream<T, R>` | `EventStream[TEvent, TResult]` |
| `AssistantMessageEventStream` | 同名 Python 类 |
| `AgentMessage` 联合类型 | 可序列化 `dict` |
| `AgentTool` interface | `AgentTool` dataclass |
| TypeBox validation | `validate_args` 回调 |
| `agentLoop()` | `agent_loop()` |
| `agentLoopContinue()` | `agent_loop_continue()` |
| `runAgentLoop()` | `run_agent_loop()` |
| `runAgentLoopContinue()` | `run_agent_loop_continue()` |
| `Agent.prompt()` | `Agent.prompt()` |
| `Agent.continue()` | `Agent.continue_run()` |
| `Agent.waitForIdle()` | `Agent.wait_for_idle()` |
| `steer()` | `steer()` |
| `followUp()` | `follow_up()` |

Python 不能把方法命名为 `continue`，因为它是语言关键字，所以使用 `continue_run()`。

---

## 15. 与原实现的有意差异

### 15.1 EventStream 增加 `fail()`

Pi 低层 stream wrapper 对契约外后台异常缺少统一 reject 通道。本 Python 实现增加 `fail()`，使 `await stream.result()` 能得到明确异常。

### 15.2 消息使用字典

TypeScript 通过联合类型提供编译期检查。Python 版本为零依赖和方便扩展，使用 `dict`。

生产项目可改成：

- TypedDict；
- dataclass；
- Pydantic model；
- msgspec Struct。

### 15.3 参数验证由调用方注入

本项目不依赖 JSON Schema 验证包，因此 `parameters` 只负责描述，`validate_args` 负责运行时检查。

### 15.4 取消对象使用自定义 Token

它在语义上接近 AbortSignal，但不是浏览器或 Node 的同一 API。

### 15.5 不是完整 Coding Agent

本目录没有搬入 Pi 的：

- `AgentSession`；
- 自动 Provider retry；
- 自动 compaction；
- SessionManager JSONL；
- 扩展系统；
- TUI；
- OAuth；
- 模型目录；
- 文件和 shell 工具实现。

这些应作为上层模块逐步加入，而不是全部塞进低层循环。

---

## 16. 测试说明

测试文件：`tests/test_agent_loop.py`。

覆盖：

1. 纯文本流式响应；
2. 工具调用结果回灌模型；
3. 并行工具完成顺序与结果顺序；
4. length 截断工具不执行；
5. follow-up 的处理时机；
6. `agent_end` listener settlement。

运行：

```bash
python -m unittest discover -s tests -v
```

测试完全使用 `ScriptedProvider`，不会调用真实网络。

---

## 17. 当前没有实现的生产能力

如果要把本项目用于生产，至少还应增加：

### 17.1 运行预算

- 最大 Turn 数；
- 最大工具调用数；
- 最大总时间；
- 最大 token；
- 最大费用。

### 17.2 并发限制

目前一批并行工具会全部启动。应增加 semaphore，例如：

```python
semaphore = asyncio.Semaphore(4)
```

### 17.3 工具 timeout

CancellationToken 只能合作取消。每个工具还应有明确 timeout。

### 17.4 持久 Inbox

当前 steering/follow-up 只在内存。崩溃恢复需要把 enqueue/dequeue 写入 Session Store。

### 17.5 Operation Log

工具执行前保存 intent，执行后保存 result，避免崩溃恢复时误重复执行有副作用工具。

### 17.6 Listener 隔离

需要明确：

- persistence listener 失败是否终止 Agent；
- UI listener 失败是否只记录日志；
- extension listener 是否 fail-open 或 fail-closed。

### 17.7 Backpressure

EventStream 和工具 update 应支持：

- 有界队列；
- update 合并；
- 慢消费者策略；
- 最大内存限制。

### 17.8 Retry 与 Compaction

建议像 Pi 一样放在更高层 Session/Host，不要继续扩大 `loop.py`。

---

## 18. 推荐的下一步目录

若继续开发完整 Agent，建议在当前目录上增加：

```text
src/pi_agent_loop/
├─ providers/
│  ├─ openai.py
│  └─ deepseek.py
├─ tools/
│  ├─ read.py
│  ├─ write.py
│  ├─ edit.py
│  └─ shell.py
├─ session/
│  ├─ events.py
│  ├─ store.py
│  ├─ jsonl.py
│  └─ projection.py
├─ policy/
│  ├─ approval.py
│  └─ filesystem.py
└─ host/
   ├─ retry.py
   ├─ compaction.py
   └─ coordinator.py
```

保持依赖方向：

```text
Provider + Tool
      ↓
  Agent Loop
      ↓
Session/Host
      ↓
 CLI/TUI/Web
```

不要让低层 Agent Loop 反向依赖 UI 或具体数据库。

---

## 19. 一句话总结

这份 Python 代码保留了 Pi Agent Loop 最重要的思想：

> **低层循环只负责模型、消息、工具和队列；重试、压缩、持久化和 UI 由外层负责。**

如果只是学习 Agent 原理，可以直接阅读 `loop.py` 和 `agent.py`；如果准备做生产系统，请先运行测试，再按第 17 节补齐可靠性和安全能力。
