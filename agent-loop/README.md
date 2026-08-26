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
├─ AGENTS.md                         要求 AI 先读业务需求文件
├─ BUSINESS_REQUIREMENTS.md          业务功能与工具的唯一需求入口
├─ README.md                         中文说明
├─ LICENSE                           Pi 原项目 MIT 许可证
├─ pyproject.toml                    Python 包配置
├─ config/
│  ├─ README.md                      本地配置和密钥安全说明
│  ├─ agent.toml.example             运行预算配置示例
│  ├─ providers.toml.example         第三方模型配置示例
│  └─ business.toml.example          唯一的简化业务配置
├─ examples/
│  ├─ minimal_text.py                最简单的纯文本示例
│  ├─ basic_usage.py                 任意输入、真实模型和编号事件示例
│  ├─ real_model_usage.py            真实 OpenAI-compatible 模型示例
│  ├─ mock_order_tools.py            模拟订单业务工具
│  └─ business_routing_usage.py      强制业务工具路由示例
├─ tests/
│  ├─ test_agent_loop.py             Agent Loop 核心语义测试
│  ├─ test_calculator_tools.py       加法、乘法与注册表测试
│  ├─ test_tool_timeout.py           独立超时与取消隔离测试
│  ├─ test_cancellation.py           父子取消令牌传播测试
│  ├─ test_config.py                 TOML 配置校验测试
│  ├─ test_budgets.py                Turn/Tool/并行预算测试
│  ├─ test_basic_usage.py            动态命令行和调用原因测试
│  ├─ test_provider_settings.py      Provider 配置测试
│  ├─ test_provider_serialize.py     OpenAI 请求序列化测试
│  ├─ test_provider_sse.py           SSE 分片测试
│  ├─ test_openai_compatible_provider.py  HTTP 与 Agent 集成测试
│  ├─ test_config_gitignore.py       真实配置 Git 隔离测试
│  ├─ test_business_requirements.py  AI 业务需求入口契约测试
│  ├─ test_simple_business_config.py 简化业务配置测试
│  ├─ test_hybrid_router.py          模型 Intent 路由测试
│  └─ test_routed_agent.py           Required Tool Guard 集成测试
└─ src/pi_agent_loop/
   ├─ __init__.py                    公开导出
   ├─ cancellation.py                合作式取消令牌
   ├─ config.py                      TOML 配置加载
   ├─ event_stream.py                异步事件流和最终结果
   ├─ messages.py                    消息构造与复制
   ├─ types.py                       Model、Tool、Config 等类型
   ├─ loop.py                        低层 Agent Loop
   ├─ agent.py                       有状态 Agent 封装
   ├─ testing.py                     ScriptedProvider
   ├─ providers/                     第三方大模型 Provider
   │  ├─ settings.py                 Provider TOML 配置
   │  ├─ factory.py                  Model/Provider 工厂
   │  ├─ errors.py                   结构化且脱敏的错误
   │  ├─ serialize.py                OpenAI 请求序列化
   │  ├─ sse.py                      SSE 任意分片解析
   │  ├─ translate.py                流事件转换
   │  └─ openai_compatible.py        异步 HTTP Provider
   ├─ routing/
   │  ├─ types.py                    路由决定与 ToolChoicePolicy
   │  ├─ errors.py                   业务配置错误
   │  ├─ simple_config.py            简化 business.toml 加载
   │  ├─ capabilities.py             CapabilityRegistry
   │  ├─ hybrid_router.py            大模型结构化 Intent 分类
   │  ├─ guard.py                    RequiredToolCallGuard
   │  └─ routed_agent.py             产品层 RoutedAgent
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
- Agent Loop 核心仍只使用 Python 标准库；
- 真实 HTTP Provider 使用 `httpx>=0.27,<1`；
- 不需要安装厂商模型 SDK；
- 默认测试不访问真实网络。

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

首次运行先创建本地预算和 Provider 配置：

```bat
copy config\agent.toml.example config\agent.toml
copy config\providers.toml.example config\providers.toml
```

在 `providers.toml` 填写真实配置后，可以把用户消息直接放在命令后面：

```bash
python examples/basic_usage.py "请同时计算 2+3 和 4×5"
```

也可以不传消息，进入交互输入：

```bash
python examples/basic_usage.py
```

这个示例不再包含预设用户问题和预设模型回复。真实模型会根据当前消息决定：

```text
不需要工具 → 一次模型调用后直接回答
需要工具   → 返回 Tool Call → 本地执行 → 回灌结果 → 再次请求模型
```

注意：当前发送的是 `tool_choice="auto"`。注册工具只表示模型“可以调用”，
不保证它一定调用。简单计算可能被模型直接回答；需要实时业务数据或真实操作时，
还必须增加 Router/CapabilityMatcher/Policy 来强制工具边界。

原来的显示方式仍然保留：

```text
[01] ...
[02] ...
[03] ...
最终回答：...
模型调用次数：...
为什么调用 ... 次：...
```

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
Ran 76 tests
OK
```

表示七十六个自动测试全部通过，并不是 Agent 又执行了七十六个用户任务。

七十六个测试分别检查：

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
11. 注册后的真实工具函数是否能被 Agent Loop 调用；
12. 快速工具能否在自己的 timeout 内完成；
13. 慢工具是否返回 `tool_timeout`；
14. 一个并行工具超时是否不影响其他工具；
15. Agent 默认 timeout 是否作用于未单独配置的工具；
16. 用户取消是否取消全部工具并忽略迟到 update；
17. 父令牌取消是否传播给全部子令牌；
18. 子令牌取消是否不会误伤父令牌和兄弟令牌；
19. `detach()` 后父令牌是否不再保存已结束工具；
20. TOML 配置是否读取为 10、5、20；
21. 配置缺少字段时是否明确报错；
22. 配置中的 0、负数、小数和布尔值是否被拒绝；
23. 配置拼写错误是否通过未知字段检测；
24. 两轮、两个工具是否能在预算内完成；
25. `max_turns=1` 是否阻止第二次模型请求；
26. Tool Call 总预算不足时是否整批拒绝；
27. Tool Call 预算是否跨 Turn 累计；
28. `max_parallel_tools` 是否限制实际并发数；
29. 未知工具是否也消耗 Tool Call 预算；
30. 新 prompt 是否重新获得独立预算；
31. Provider Base URL、模型和 Bearer 配置；
32. Factory 是否使用指定模型；
33. `.example` 占位值是否被拒绝；
34. 远程明文 HTTP 是否默认被拒绝；
35. localhost HTTP 是否可用于测试；
36. Provider 未知字段是否被拒绝；
37. `stream=false` 是否被拒绝；
38. System/User/Tool Call/Tool Result 序列化；
39. 没有工具时是否省略 tools/tool_choice；
40. 未知消息角色是否 fail-closed；
41. UTF-8、CRLF 和网络分片 SSE；
42. 一个 Chunk 中多个 SSE Event；
43. SSE EOF 收尾；
44. Bearer、指定模型和文本流完整转换；
45. 流式 Tool Call 参数组装；
46. 401 错误是否隐藏 API Key；
47. CancellationToken 是否终止真实 HTTP 流；
48. HTTP Provider → Agent → Tool → 下一 Turn 闭环；
49. 真实 TOML 是否被 Git 忽略；
50. `.toml.example` 是否允许提交；
51. 真实配置是否未被 Git 跟踪；
52. 命令行多个参数是否合并为一条用户消息；
53. “为什么调用”是否根据真实 assistant 历史生成；
54. `basic_usage.py` 是否已经移除 ScriptedProvider 和固定任务；
55. `required` 和 named Tool Choice 序列化；
56. 强制不可见工具是否被拒绝；
57. 没有工具时 `required` 是否被拒绝；
58. 模型绕过 Required Tool 时 Guard 是否拒绝；
59. 模型篡改 Router 参数时 Guard 是否拒绝；
60. Required Tool 成功后下一轮是否切回 auto；
61. Capability Missing 是否完全不请求模型；
62. No-tool Intent 是否不向模型暴露业务工具；
63. CapabilityRegistry 是否拒绝工具重名；
64. 简化配置是否不包含正则且可直接加载；
65. must_use_tool 缺少 Capability 是否被拒绝；
66. No-tool Intent 偷配 Capability 是否被拒绝；
67. 模型是否把自由表达映射到已有 Intent；
68. 低置信度是否进入 Clarification；
69. 缺少字段是否使用配置追问；
70. 明确 Denied 示例是否在模型前阻止；
71. 写操作有能力时是否仍先要求 Approval；
72. Hybrid Router → Required Tool → Final Answer 完整闭环；
73. Hybrid No-tool Intent 是否不选择业务工具；
74. Hybrid Intent 已识别但能力缺失是否结构化返回；
75. AGENTS.md 是否强制 AI 先读取业务需求；
76. 业务需求 MD 是否包含配置和工具生成所需章节。

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
- `tests/test_calculator_tools.py`；
- `tests/test_tool_timeout.py`；
- `tests/test_cancellation.py`；
- `tests/test_config.py`；
- `tests/test_budgets.py`。

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
10. Registry → Agent → Agent Loop → ToolResult 完整链路；
11. 单工具独立 timeout；
12. 并行工具 timeout 隔离；
13. Agent 默认 timeout；
14. 父子 CancellationToken；
15. 取消后的迟到 update 隔离；
16. 父取消向下传播；
17. 子取消不向上或横向传播；
18. 子令牌 detach 清理；
19. TOML 配置读取和严格校验；
20. 最大 Turn 预算；
21. 最大 Tool Call 总预算；
22. Tool Call 跨 Turn 累计；
23. 超预算整批拒绝；
24. 最大并行工具数；
25. 并行任务异常清理；
26. 未知工具消耗预算；
27. 每次 prompt 独立预算。

运行：

```bash
python -m unittest discover -s tests -v
```

测试完全使用 `ScriptedProvider`，不会调用真实网络。

---

## 17. 当前没有实现的生产能力

如果要把本项目用于生产，至少还应增加：

### 17.1 其余运行预算

当前已完成最大 Turn 数和最大 Tool Call 数，还缺少：

- 整个 Run 的最大总时间；
- 最大 token；
- 最大费用。

### 17.2 更高级的并发调度

当前已使用 Semaphore 实现 `max_parallel_tools`，后续生产系统还可增加：

- 工具优先级；
- 公平排队；
- 不同工具类型分别限流；
- 只读工具与有副作用工具的执行屏障。

### 17.3 子进程级强制 Timeout

当前已实现异步工具 timeout 和子 CancellationToken，但完全阻塞事件循环的
同步代码仍无法被 asyncio timer 打断。Shell、外部程序等需要：

- 子进程；
- 进程组；
- timeout 后终止整个进程树。

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
    system_prompt=(
        "先理解用户请求，只有适合当前工具时才调用；"
        "没有合适工具时使用模型自身能力回答。"
    ),
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

真实模型判断需要加法时可能返回：

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

### 20.10 为什么模型调用次数不再写死

如果用户只是要求解释概念，真实模型可以直接回答：

```text
模型调用次数：1
为什么调用 1 次：模型判断不需要工具，直接生成回答。
```

如果模型先调用 `add`：

```text
第 1 次：模型决定调用工具 add
执行 add 并回灌 Tool Result
第 2 次：模型读取工具结果并生成回答
```

如果模型连续规划多轮，程序会根据真实 assistant 历史逐轮解释，不再固定显示“两次”。

### 20.11 编号事件显示仍然保留

```text
[01] Agent 开始处理本次用户任务。
[02] 开始第 1 轮：准备请求真实模型。
[03] 收到本次用户输入：<命令行或交互输入的真实消息>
[04] 用户消息已经加入本次模型上下文。
[05] 真实模型开始生成一条回复。
```

这些属于 Agent Loop 的通用流程。后续是否出现工具事件，由真实模型决定。

### 20.12 当前已经使用真实 Provider

`basic_usage.py` 读取本地 `providers.toml`，通过 OpenAI-compatible Provider 访问真实模型。模型根据：

- 当前用户消息；
- System Prompt；
- 工具 description；
- 工具 parameters；
- 历史 Tool Result；

自行决定直接回答，还是调用 `add`/`multiply`。

`ScriptedProvider` 仍保留在单元测试和 `minimal_text.py` 中，以保证离线测试稳定。

### 20.13 运行示例

命令后直接添加用户消息：

```bash
python examples/basic_usage.py "8 加 9 等于多少？"
```

或进入交互输入：

```bash
python examples/basic_usage.py
```

如果真实模型调用工具，编号事件会显示：

```text
模型要求调用工具：add
开始执行工具 add
工具 add 返回结果
结果写回模型上下文
再次请求真实模型
```

### 20.14 运行测试

```bash
python -m unittest discover -s tests -v
```

当前共有 76 项离线测试，覆盖 Agent Loop、Provider、简化业务配置、Hybrid Router、AI 需求入口、Capability、Approval 和 Guard。

只有看到：

```text
Ran 76 tests
OK
```

才表示本阶段全部通过。

### 20.15 加法 2 秒、乘法 5 秒的独立 Timeout

当前工具配置：

```text
add.timeout_seconds = 2
multiply.timeout_seconds = 5
```

Timeout 是“最多允许执行多久”，不是故意等待多久。正常加法和乘法会立即完成，所以普通示例不会等待 2 秒或 5 秒。

#### 在 `basic_usage.py` 哪里设置模拟延时

打开：

`examples/basic_usage.py`

在文件顶部找到：

```python
ADD_TOOL_DELAY_SECONDS = 0.0
MULTIPLY_TOOL_DELAY_SECONDS = 0.0
```

正常成功：

```python
ADD_TOOL_DELAY_SECONDS = 0.0
MULTIPLY_TOOL_DELAY_SECONDS = 0.0
```

只触发加法超时：

```python
ADD_TOOL_DELAY_SECONDS = 3.0       # 大于加法限制 2 秒
MULTIPLY_TOOL_DELAY_SECONDS = 0.0
```

只触发乘法超时：

```python
ADD_TOOL_DELAY_SECONDS = 0.0
MULTIPLY_TOOL_DELAY_SECONDS = 6.0  # 大于乘法限制 5 秒
```

两个工具都触发超时：

```python
ADD_TOOL_DELAY_SECONDS = 3.0
MULTIPLY_TOOL_DELAY_SECONDS = 6.0
```

两个工具是并行执行的，因此同时超时时总等待大约 5 秒，而不是 3+6=9 秒。

然后运行：

```bat
python examples\basic_usage.py
```

事件中会看到：

```text
工具 add 执行失败，结果为 工具 add 执行超时（限制 2 秒）
```

或者：

```text
工具 multiply 执行失败，结果为 工具 multiply 执行超时（限制 5 秒）
```

工具自己的 timeout 配置位置：

- `src/pi_agent_loop/tools/add.py`；
- `src/pi_agent_loop/tools/multiply.py`。

每个工具执行时会从 Agent 总 CancellationToken 创建自己的子令牌：

```text
Agent 总令牌
  ├─ add 子令牌，2 秒
  └─ multiply 子令牌，5 秒
```

行为：

- 用户取消 Agent：两个工具全部取消；
- add 超过 2 秒：只取消 add；
- multiply 超过 5 秒：只取消 multiply；
- 一个工具超时不会误伤另一个工具；
- 超时后的迟到 update 会被忽略；
- timeout timer 和子令牌在工具结束后清理。

工具没有单独 timeout 时，也可以使用 Agent 默认值：

```python
agent = Agent(
    ...,
    default_tool_timeout_seconds=30,
)
```

优先级：

```text
AgentTool.timeout_seconds
  -> Agent.default_tool_timeout_seconds
  -> None，表示不限制
```

超时后的 ToolResultMessage：

```python
{
    "isError": True,
    "content": [
        {"type": "text", "text": "工具 add 执行超时（限制 2 秒）"}
    ],
    "details": {
        "code": "tool_timeout",
        "toolName": "add",
        "timeoutSeconds": 2,
    },
}
```

注意：asyncio timeout 只能打断会让出事件循环的异步工具。完全阻塞 Python 事件循环的同步死循环仍需要放入子进程，才能强制终止。

### 20.16 本阶段完成标准

- [x] 创建 `tools` 目录；
- [x] 加法工具独立文件；
- [x] 乘法工具独立文件；
- [x] 公共参数校验；
- [x] ToolRegistry；
- [x] 防止工具重名；
- [x] 示例通过 Registry 注册工具；
- [x] Agent Loop 调用真实 Python 工具函数；
- [x] 工具单元测试；
- [x] Agent 集成测试；
- [x] 每个工具独立 timeout；
- [x] 加法 timeout 为 2 秒；
- [x] 乘法 timeout 为 5 秒；
- [x] 父子 CancellationToken；
- [x] 并行 timeout 隔离；
- [x] timeout 和取消测试。

最大 Turn/Tool Call 预算和最大并行工具数已经完成，详细配置见下一节。

---

## 21. TOML 运行预算配置

### 21.1 配置文件位置

`config/agent.toml`

当前内容：

```toml
[limits]
max_tool_calls = 10
max_parallel_tools = 5
max_turns = 20
```

配置文件内已经为三个字段加入中文注释。修改后重新运行程序即可生效。

### 21.2 三个字段的含义

```text
max_tool_calls       限制整个运行最多接受多少个工具调用
max_parallel_tools   同一时刻最多并行执行多少个工具
max_turns            限制最多请求模型多少轮
```

它们必须是大于 0 的整数。以下值都会被拒绝：

```text
0
-1
1.5
true
```

未知字段也会被拒绝，防止把 `max_tool_calls` 错写成 `max_tool_call` 后误以为已经生效。

### 21.3 示例怎样读取配置

`examples/basic_usage.py`：

```python
limits = load_agent_limits(ROOT / "config" / "agent.toml")
provider_settings = load_provider_settings(
    ROOT / "config" / "providers.toml"
)
model, provider = create_provider(provider_settings)
```

然后交给 Agent：

```python
agent = Agent(
    ...,
    max_tool_calls=limits.max_tool_calls,
    max_parallel_tools=limits.max_parallel_tools,
    max_turns=limits.max_turns,
)
```

运行示例时会打印：

```text
运行预算：max_turns=20，max_tool_calls=10，max_parallel_tools=5。
```

### 21.4 Turn 预算

每次真正准备请求一次 assistant 时消耗 1 Turn。

如果真实模型决定调用工具，一次典型运行是：

```text
第 1 Turn：模型请求 add、multiply
第 2 Turn：模型读取工具结果并生成最终回答
```

如果不需要工具，则可能只有 1 个 Turn。实际数量由真实模型响应决定。

达到 `max_turns` 后：

- 不再多请求一次 Provider；
- 发出 `budget_exceeded`；
- 设置 Agent error_message；
- 正常发出 `agent_end`。

### 21.5 Tool Call 总预算

模型一条 assistant message 中的全部 toolCall 会先按整批检查：

```text
已使用数量 + 本批请求数量 <= max_tool_calls
```

成立：整批进入预检和执行。

不成立：整批都不执行。

例如：

```text
max_tool_calls=6
模型一次请求10个工具
```

结果：

```text
10个全部拒绝
不会先执行6个
每个工具获得 tool_call_budget_exceeded 结果
当前 Agent 运行结束
```

未知工具、参数错误和 before hook 阻止的工具，只要整批预算允许，也会消耗 Tool Call 预算，防止错误调用无限重试。

### 21.6 最大并行工具数

`max_parallel_tools` 通过 `asyncio.Semaphore` 限制真正进入 execute 的工具数量。

例如：

```text
max_tool_calls=20
max_parallel_tools=5
模型一次请求8个工具
```

结果：

```text
总预算允许8个
同一时刻最多执行5个
有工具完成后，剩余3个陆续进入执行
```

这8个工具仍属于同一个 Turn。

### 21.7 预算事件

超出预算时发出：

```python
{
    "type": "budget_exceeded",
    "budget": "tool_calls" 或 "turns",
    "limit": 10,
    "used": 8,
    "requested": 3,
    "remaining": 2,
    "message": "...",
}
```

UI、日志和测试可以根据结构化字段展示原因。

### 21.8 每次 prompt 的预算独立

预算按一次 `run_agent_loop` 计算。

```text
agent.prompt("任务一")  获得一份新预算
agent.prompt("任务二")  再获得一份新预算
```

任务一使用过的 Turn 和 Tool Call 不会扣减任务二的预算。

### 21.9 测试

相关测试包括：

- `tests/test_config.py`：4 项配置测试；
- `tests/test_budgets.py`：7 项预算与并行测试；
- `tests/test_basic_usage.py`：3 项动态示例测试；
- `tests/test_routed_agent.py`：6 项强制工具与 Capability 测试；
- `tests/test_simple_business_config.py`：3 项简化配置测试；
- `tests/test_hybrid_router.py`：8 项混合路由测试；
- `tests/test_business_requirements.py`：2 项 AI 需求入口契约测试。

全部测试：

```text
Ran 76 tests
OK
```

测试覆盖：

- 当前配置读取为 10、5、20；
- 缺字段和错误类型；
- 预算内正常运行；
- Turn 达限不多请求模型；
- Tool Call 超限整批拒绝；
- 跨 Turn 累计；
- 并行数量不超过限制；
- 未知工具消耗预算；
- 新 prompt 重置预算。

---

## 22. 接入真实 OpenAI-compatible 模型

### 22.1 安装

```bash
python -m pip install -e .
```

真实 HTTP Provider 使用异步 `httpx`，不需要安装第三方厂商 SDK。

### 22.2 创建本地配置

Windows：

```bat
copy config\agent.toml.example config\agent.toml
copy config\providers.toml.example config\providers.toml
```

Linux/macOS：

```bash
cp config/agent.toml.example config/agent.toml
cp config/providers.toml.example config/providers.toml
```

编辑本地 `config/providers.toml`：

```toml
[active]
profile = "third_party"

[profiles.third_party]
protocol = "openai_chat_completions"
base_url = "https://你的第三方服务地址/v1"
endpoint = "/chat/completions"
auth_type = "bearer"
api_key = "你的真实API Key"
model = "第三方平台规定的模型ID"
stream = true
connect_timeout_seconds = 10
request_timeout_seconds = 60
allow_insecure_http = false
```

`base_url` 可以包含 `/v1`，但不要再次包含 `/chat/completions`。

### 22.3 Git 安全

`.gitignore` 已禁止提交：

```text
config/agent.toml
config/providers.toml
```

允许提交：

```text
config/agent.toml.example
config/providers.toml.example
```

提交前检查：

```bash
git check-ignore -v config/providers.toml
git diff --cached --name-only
```

API Key 不会写入 Model、Agent Message、事件或错误消息。若 Key 曾进入 Git
历史，必须立即在第三方平台撤销并轮换。

### 22.4 运行真实模型

```bash
python examples/real_model_usage.py
```

程序将读取本地配置并打印 Provider 和模型 ID，但绝不会打印 API Key。

示例同时注册 `add` 和 `multiply`，因此可以测试：

```text
用户问题
→ 第三方模型 stream=true
→ OpenAI Tool Calling
→ 本地 Python 工具
→ Tool Result 回灌
→ 第二次模型请求
→ 最终回答
```

### 22.5 Provider 分层

```text
settings.py             配置和安全校验
factory.py              创建 Model 与 Provider
serialize.py            内部消息转 Chat Completions JSON
sse.py                  任意 HTTP Chunk 边界的 SSE 解析
translate.py            OpenAI Delta 转 AssistantMessageEventStream
openai_compatible.py    Bearer HTTP、Timeout、取消和状态码
errors.py               结构化且脱敏的错误
```

Agent Loop 继续只依赖 `StreamFn`，没有混入 HTTP 和 API Key 逻辑。

### 22.6 当前协议范围

已支持：

- `POST /chat/completions`；
- 配置 Base URL；
- 配置模型 ID；
- Bearer API Key；
- `stream=true`；
- 流式文本；
- OpenAI Tool Calling；
- Tool Call JSON 参数分片；
- `stop/tool_calls/length/content_filter`；
- 401、404、429、5xx 分类；
- Provider Timeout；
- Agent CancellationToken；
- `reasoning_content` 兼容事件；
- API Key 脱敏。

暂未支持：

- `/responses`；
- Anthropic `/messages`；
- OAuth；
- Azure/AWS 签名；
- 图片、音频和文件上传；
- 非 OpenAI 格式 Tool Calling。

### 22.7 测试不访问真实网络

Provider 测试使用 `httpx.MockTransport` 模拟：

- 文本 SSE；
- Tool Call SSE；
- 401；
- 用户取消；
- 完整 Agent 工具闭环。

因此默认执行：

```bash
python -m unittest discover -s tests -v
```

不会消耗真实 API 额度。

---

## 23. 业务 Router 与 Required Tool Guard

### 23.1 为什么需要产品层

`tool_choice="auto"` 只代表模型可以调用工具，不能保证实时数据和写操作一定经过工具。新路由层把职责拆为：

```text
HybridModelRouter       将自然语言映射到已配置 Intent
CapabilityRegistry      将业务能力映射到实际工具
ToolChoicePolicy        选择 none/auto/required/named
RequiredToolCallGuard   验证模型确实调用了要求的能力和参数
RoutedAgent             组合以上模块并驱动低层 Agent
```

低层 `loop.py` 仍然只负责模型 Turn 和工具执行。

### 23.2 唯一业务配置

项目现在只保留 `business.toml`。业务人员填写名称、说明、示例、必要参数、Capability 和 Approval，不再维护正则版配置。

### 23.3 Capability 与工具名分离

```python
capabilities = CapabilityRegistry()
capabilities.register(
    get_order_status_tool,
    capabilities={"orders.read_current"},
    domain="orders",
)
```

Hybrid Router 寻找的是稳定业务能力 `orders.read_current`，而不是把实现名称 `get_order_status` 写死到每个 Intent。

### 23.4 Tool Choice 策略

Provider 现在支持：

```text
none       禁止工具
auto       模型自由选择
required   当前 Turn 必须调用至少一个可见工具
named      强制指定 function name
```

Named 格式：

```python
ToolChoicePolicy("named", "get_order_status").to_openai()
```

得到：

```json
{
  "type": "function",
  "function": {"name": "get_order_status"}
}
```

不可见工具和无工具时的 required 会在发送 HTTP 前被拒绝。

### 23.5 Guard 防止模型绕过业务工具

Required Intent 中，如果模型直接输出文本：

```text
required_tool_call_missing
```

如果模型调用了不属于当前 Intent 的工具：

```text
tool_not_allowed_for_intent
```

如果调用的工具不提供要求的 Capability：

```text
required_capability_not_called
```

如果 Router 从用户输入确认 `order_id=1001`，但模型改成调用 `order_id=9999`：

```text
required_tool_arguments_mismatch
```

违规回答会在进入 Agent Loop 前转换成结构化 Assistant Error，不会执行工具。

### 23.6 Required 只约束规划 Turn

第一 Turn：

```text
tool_choice=required
→ 模型必须返回工具调用
```

工具执行成功后，`AgentLoopTurnUpdate.stream_options` 会让下一 Turn 自动切换为：

```text
tool_choice=auto
```

否则第二 Turn 也被强制调用工具，模型将无法生成最终文本。

### 23.7 RoutedAgent 状态处理

```text
in_scope_no_tool
→ 不向模型暴露业务工具
→ tool_choice=none
→ 调用模型回答稳定规则

in_scope_tool_ready
→ 只暴露匹配 Capability 的工具
→ required/auto
→ Guard 校验

in_scope_need_clarification
→ 不调用模型
→ 返回缺少字段

in_scope_capability_missing
→ 不调用模型
→ 返回缺少能力

out_of_scope/prohibited
→ 不调用模型
→ 直接返回产品策略结果
```

### 23.8 运行模拟订单业务

先复制推荐的简化配置：

```bat
copy config\business.toml.example config\business.toml
python examples\business_routing_usage.py "查询订单 1001 当前状态"
```

模拟数据库包含：

```text
1001 → 已发货
1002 → 待支付
1003 → 已完成
```

这是本地 Mock 数据，不是真实订单系统。

### 23.9 真实 API 验证

真实运行：

```text
查询订单 1002 当前状态
```

结果：

```text
路由状态：in_scope_tool_ready
Intent：order.get_status
Required Capabilities：orders.read_current
Selected Tools：get_order_status
Tool Policy：required
路由来源：model
工具参数：order_id=1002
工具执行：成功
最终结果：订单 1002 当前状态：待支付。
模型调用次数：3
```

Capability Missing 验证：

```text
取消订单 1001
→ 需要 orders.cancel
→ 当前没有取消工具
→ 不请求模型
→ 返回 in_scope_capability_missing
```

No-tool 验证：

```text
解释什么是订单状态
→ 不暴露订单工具
→ tool_choice=none
→ 模型调用一次并解释概念
```

### 23.10 当前边界

高级规则 Router 仍使用正则；推荐的 HybridModelRouter 则使用大模型理解自然语言，但会额外消耗一次分类模型调用。

正式接入用户业务前还需要提供：

- 产品 Domain；
- Intent 清单；
- 每个 Intent 的参数；
- Required Capability；
- 实际业务工具；
- 权限和 Approval 规则。

不要直接把订单 Mock 示例用于生产。

---

## 24. 简化 business.toml 与 HybridModelRouter

### 24.1 AI 业务需求入口

项目根目录新增：

```text
AGENTS.md
BUSINESS_REQUIREMENTS.md
```

`AGENTS.md` 要求后续 AI 在实现业务功能、工具、Capability、Policy 或配置前，必须先完整读取 `BUSINESS_REQUIREMENTS.md`。

业务人员只需在 `BUSINESS_REQUIREMENTS.md` 填写：

- 产品范围；
- Intent 表；
- Capability 与真实 API；
- 参数；
- 写操作和审批；
- 禁止规则；
- 验收对话。

AI 应根据该文件生成可加载配置、工具、注册代码和测试。信息不足时必须先提问，禁止猜测真实接口。

### 24.2 创建简化配置

```bat
copy config\business.toml.example config\business.toml
```

示例：

```toml
[product]
name = "订单助手"
description = "负责订单查询、取消和订单规则说明"
allow_general_questions = false

[[intents]]
id = "order.get_status"
name = "查询订单状态"
description = "查询指定订单当前的真实处理状态"
examples = [
  "查询订单 1001",
  "帮我看看订单 1001 到哪了"
]
required_fields = ["order_id"]
capability = "orders.read_current"
must_use_tool = true
requires_approval = false
ask_when_missing = "请提供要查询的订单号。"
```

没有 `patterns`、`field_patterns` 和 `priority`。

### 24.3 SimpleBusinessConfig

```python
business = load_simple_business_config("config/business.toml")
```

Loader 会拒绝：

- 未知字段；
- 重复 Intent ID；
- 空 examples；
- `must_use_tool=true` 却没有 Capability；
- No-tool Intent 偷配 Capability；
- 不使用工具却要求 Approval；
- 缺必要参数却没有追问消息。

### 24.4 HybridModelRouter

```text
用户自然语言
→ 模型只调用 select_business_intent
→ 只能选择配置中的 Intent ID
→ 输出 arguments/confidence/reason
→ Host 严格校验
→ Capability/Approval/ToolChoice/Guard
```

结构化分类结果：

```json
{
  "decision": "order.get_status",
  "arguments": {"order_id": "1001"},
  "confidence": 0.96,
  "reason": "用户正在查询具体订单状态"
}
```

模型不能新增 Intent，也不能修改配置中的 `must_use_tool` 和 Approval。

### 24.5 路由安全

- 明确命中 `denied.examples` 时，在模型调用前直接阻止；
- 分类失败时 fail-closed，返回 Clarification；
- 低于置信度阈值时不执行工具；
- 参数只接受配置声明的 Required Fields；
- Capability Missing 不调用回答模型；
- 写操作有能力时仍返回 `in_scope_approval_required`；
- Approval 完成前不会执行写工具；
- RequiredToolCallGuard 继续验证工具、Capability 和参数。

### 24.6 模型调用次数

Hybrid 路由会增加一次分类调用。

读取真实数据的典型流程：

```text
第 1 次：分类模型选择 Intent 并提取参数
第 2 次：回答模型在 required 模式下生成业务 Tool Call
执行工具并回灌
第 3 次：回答模型读取 Tool Result 并生成最终文本
```

这是用额外一次模型调用换取自然语言灵活性和业务边界。

### 24.7 真实联调结果

用户表达：

```text
劳驾帮我看看编号 1003 的单子走到哪里了
```

该说法没有写在配置 examples 中，真实模型仍成功路由：

```text
路由状态：in_scope_tool_ready
Intent：order.get_status
路由来源：model
路由置信度：0.96
Required Capability：orders.read_current
Selected Tool：get_order_status
Tool Policy：required
工具参数：order_id=1003
最终结果：订单 1003 当前状态：已完成。
模型调用次数：3
```

### 24.8 测试

新增：

```text
tests/test_simple_business_config.py
tests/test_hybrid_router.py
```

覆盖简化配置矛盾、自由表达分类、低置信度、缺字段、Denied、Approval 和完整业务工具闭环。

```text
Ran 76 tests
OK
```

### 24.9 配置唯一性

项目只保留：

```text
BUSINESS_REQUIREMENTS.md
→ config/business.toml.example
→ 本地 config/business.toml
→ HybridModelRouter
```

旧正则配置和 RuleBasedRouter 已删除，避免两套路由逻辑并存造成维护歧义。
