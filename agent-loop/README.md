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
│  ├─ test_agent_loop.py             Agent Loop 核心语义测试
│  └─ test_calculator_tools.py       加法、乘法与注册表测试
└─ src/pi_agent_loop/
   ├─ __init__.py                    公开导出
   ├─ cancellation.py                合作式取消令牌
   ├─ event_stream.py                异步事件流和最终结果
   ├─ messages.py                    消息构造与复制
   ├─ types.py                       Model、Tool、Config 等类型
   ├─ loop.py                        低层 Agent Loop
   ├─ agent.py                       有状态 Agent 封装
   ├─ testing.py                     ScriptedProvider
   └─ tools/
      ├─ __init__.py                 计算工具公开导出
      ├─ validators.py               a、b 参数校验
      ├─ add.py                      加法工具
      ├─ multiply.py                 乘法工具
      └─ registry.py                 工具注册表
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
Ran 11 tests
OK
```

表示十一个自动测试全部通过，并不是 Agent 又执行了十一个用户任务。

十一个测试分别检查：

1. 最终回答能否进入 Agent 状态；
2. 工具结果能否交回模型并触发第二次模型请求；
3. 并行工具的完成顺序与结果顺序是否正确；
4. 被长度上限截断的工具参数是否会被安全拒绝；
5. follow-up 是否等原任务结束后才处理；
6. `agent_end` 的异步监听器结束前，Agent 是否仍保持忙碌；
7. 加法工具是否正确返回 `2+3=5`；
8. 乘法工具是否正确返回 `4×5=20`；
9. 参数校验是否拒绝缺少字段和布尔值；
10. 注册表是否拒绝重名工具；
11. 注册后的真实工具函数是否能被 Agent Loop 调用。

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

测试文件：

- `tests/test_agent_loop.py`；
- `tests/test_calculator_tools.py`。

覆盖：

1. 纯文本流式响应；
2. 工具调用结果回灌模型；
3. 并行工具完成顺序与结果顺序；
4. length 截断工具不执行；
5. follow-up 的处理时机；
6. `agent_end` listener settlement；
7. 加法和乘法结果；
8. 参数校验；
9. 工具重名保护；
10. Registry → Agent → Agent Loop → ToolResult 完整链路。

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

---

## 20. 加法工具、乘法工具和注册流程教学

本阶段只实现两个工具：

```text
add          加法工具
multiply     乘法工具
```

目标不是建设完整文件工具系统，而是先学懂一条最小工具链：

```text
编写工具
  -> 注册工具
  -> 把工具交给 Agent
  -> 假模型返回 toolCall
  -> Agent Loop 找到同名工具
  -> 执行 Python 函数
  -> ToolResult 写回模型上下文
  -> 模型生成最终回答
```

### 20.1 工具文件在哪里

```text
src/pi_agent_loop/tools/
├─ validators.py       加法、乘法共用参数校验
├─ add.py              加法工具
├─ multiply.py         乘法工具
├─ registry.py         工具注册表
└─ __init__.py         对外导出
```

### 20.2 加法工具由哪些部分组成

打开：

`src/pi_agent_loop/tools/add.py`

工具工厂：

```python
create_add_tool()
```

它返回 `AgentTool`，其中包含：

```text
name                 模型调用时使用的名字 add
label                给人看的中文名称“加法”
description          告诉模型这个工具做什么
parameters           给模型看的参数 JSON Schema
validate_args        Python 运行时参数校验
execute              真正执行 a + b
execution_mode       允许与其他工具并行
```

真正计算发生在：

```python
value = a + b
```

最终返回：

```python
AgentToolResult(
    content=[{"type": "text", "text": str(value)}],
    details={
        "operation": "add",
        "a": a,
        "b": b,
        "value": value,
    },
)
```

`content` 会交给模型，`details` 主要给测试、日志和以后 UI 使用。

### 20.3 乘法工具

打开：

`src/pi_agent_loop/tools/multiply.py`

工具工厂：

```python
create_multiply_tool()
```

核心计算：

```python
value = a * b
```

它与加法工具使用相同参数：

```python
{"a": 4, "b": 5}
```

返回文本：

```text
20
```

### 20.4 参数校验为什么独立

两个工具都需要 `a` 和 `b`，所以共享：

`src/pi_agent_loop/tools/validators.py`

校验规则：

- 参数必须是字典；
- 必须同时包含 `a` 和 `b`；
- 两者必须是 `int` 或 `float`；
- 拒绝布尔值；
- 拒绝 NaN；
- 拒绝正负无穷。

工具给模型看的 JSON Schema 和 Python 真正运行的校验是两件事：

```text
parameters       告诉模型应该怎样填写参数
validate_args    防止错误参数真正进入 Python 计算
```

不能只相信模型会按照 Schema 正确输出。

### 20.5 什么叫“注册工具”

工具写好后只是一个 Python 对象，Agent 还不知道它存在。

注册就是把它加入 `ToolRegistry`：

```python
registry = ToolRegistry()
registry.register(create_add_tool())
registry.register(create_multiply_tool())
```

此时：

```python
registry.names()
```

返回：

```python
["add", "multiply"]
```

### 20.6 为什么需要 ToolRegistry

也可以直接写：

```python
tools = [create_add_tool(), create_multiply_tool()]
```

但 Registry 可以：

- 集中管理工具；
- 检查工具名称不能为空；
- 防止同名工具被静默覆盖；
- 按名称查找工具；
- 保持注册顺序；
- 以后方便增加工具 Profile。

重复注册：

```python
registry.register(create_add_tool())
registry.register(create_add_tool())
```

会明确报错：

```text
工具已经注册：add
```

### 20.7 注册表怎样交给 Agent

注册完成后，把 Registry 导出的列表交给 Agent：

```python
agent = Agent(
    model=model,
    stream_fn=provider.stream,
    tools=registry.all(),
)
```

这一步完成两个连接：

1. Provider 请求时可以看到 `add`、`multiply` 的名称、描述和参数 Schema；
2. 模型返回 `toolCall(name="add")` 时，Agent Loop 能找到真正的 Python 加法函数。

### 20.8 完整注册代码

`examples/basic_usage.py` 中现在使用：

```python
registry = ToolRegistry()
registry.register(create_add_tool())
registry.register(create_multiply_tool())

agent = Agent(
    model=model,
    stream_fn=provider.stream,
    system_prompt="你是一个只使用给定工具完成计算的助手。",
    tools=registry.all(),
    tool_execution="parallel",
)
```

也可以使用快捷工厂：

```python
from pi_agent_loop import create_calculator_registry

registry = create_calculator_registry()
agent = Agent(
    model=model,
    stream_fn=provider.stream,
    tools=registry.all(),
)
```

### 20.9 注册以后是怎样调用到工具的

假模型第一次返回：

```python
{
    "type": "toolCall",
    "id": "call-add",
    "name": "add",
    "arguments": {"a": 2, "b": 3},
}
```

Agent Loop 执行：

```text
读取 name=add
  -> 在 Agent tools 中找到 add
  -> 调用 add.validate_args({a:2,b:3})
  -> 调用 add.execute(...)
  -> 得到文本结果 5
  -> 创建 ToolResultMessage
```

乘法同理：

```text
name=multiply
  -> 调用乘法工具
  -> 4×5
  -> 得到 20
```

### 20.10 为什么模型调用两次

第 1 次：

```text
用户：请同时计算 2+3 和 4×5
模型：我要调用 add 和 multiply
```

工具执行：

```text
add(2,3)          -> 5
multiply(4,5)     -> 20
```

第 2 次：

```text
模型读取结果 5 和 20
  -> 生成“加法结果是 5，乘法结果是 20。”
```

因此模型调用次数是 2，但 Python 工具各执行一次。

### 20.11 为什么前五个事件保持不变

```text
[01] Agent 开始处理用户任务。
[02] 开始第 1 轮：准备请求一次模型。
[03] 开始接收用户消息：请同时计算 2+3 和 4×5
[04] 用户消息已经加入本次模型上下文。
[05] 模型开始生成一条回复。
```

这五步属于 Agent Loop 的通用流程，与工具写在示例内部还是独立 tools 目录无关。

从第六步开始，模型返回 `add` 和 `multiply`，Agent Loop 才进入我们编写的工具。

### 20.12 当前仍是假模型

`ScriptedProvider` 已经预先写好：

```text
第一次返回 add/multiply toolCall
第二次返回最终中文回答
```

所以当前重点是学习“工具编写和注册”，不是学习真实模型怎样理解用户问题。

以后接入 DeepSeekProvider 后，将由真实模型根据：

- 用户问题；
- 工具 description；
- 工具 parameters；

自行决定是否调用 `add` 或 `multiply`。

### 20.13 运行示例

```bash
python examples/basic_usage.py
```

前五步保持原样，后面会明确显示：

```text
模型要求调用工具：add、multiply
开始执行工具 add
开始执行工具 multiply
加法工具正在计算
乘法工具正在计算
工具 add 返回 5
工具 multiply 返回 20
```

### 20.14 运行测试

```bash
python -m unittest discover -s tests -v
```

现在共有 11 项测试：

- 原 Agent Loop 6 项；
- 加法、乘法和 Registry 5 项。

只有看到：

```text
Ran 11 tests
OK
```

才表示本阶段全部通过。

### 20.15 本阶段完成标准

- [x] 创建 `tools` 目录；
- [x] 加法工具独立文件；
- [x] 乘法工具独立文件；
- [x] 公共参数校验；
- [x] ToolRegistry；
- [x] 防止工具重名；
- [x] 示例通过 Registry 注册工具；
- [x] Agent Loop 调用真实 Python 工具函数；
- [x] 工具单元测试；
- [x] Agent 集成测试。

下一阶段可以继续学习真实 Provider，或者根据要求继续增加新的内置工具。
