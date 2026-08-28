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

### 1.8 Project 与持久会话

`DurableAgentWorkspace` 提供类似 Coding Agent 产品的两层结构：

```text
Project（本地工作目录）
  └─ Session（一个可持续打开的对话）
       └─ Operation Events（模型、工具、审批和完整消息事实）
```

- 用同一个 `session_id` 重新打开时，会从加密 Journal 重建之前的完整对话；
- Project/Session 列表由事件投影产生，支持重命名、归档、排序、移动和软删除；
- `fork_session()` 在固定 Journal Sequence 处分叉，后续主线消息不会泄漏到分支；
- 打开受管 Session 时会核对工作目录和 Agent 配置摘要，并持有可续租的单写者 Lease；
- 项目与会话元数据不会混入模型 Prompt，但与对话事件一样受加密、校验和租户隔离保护。

最小使用方式：

```python
workspace = DurableAgentWorkspace.open("state")
project = await workspace.ensure_project(".", title="my-project")
session = await workspace.create_session(project.project_id, title="订单调试")

host = await workspace.open_session(
    session.session_id,
    model=model,
    stream_fn=provider.stream,
    system_prompt="你是一个中文助手",
    tools=tools,
)
await host.prompt("第一个问题")
await host.close()

# 重新运行程序后，使用同一 session_id 即可继续对话。
host = await workspace.open_session(
    session.session_id,
    model=model,
    stream_fn=provider.stream,
    system_prompt="你是一个中文助手",
    tools=tools,
)
```

`examples/basic_usage.py` 默认持续使用 `basic-usage` Session；可用
`--session-id` 选择或新建另一个对话：

```powershell
python examples/basic_usage.py --session-id order-debug "我上个问题是什么"
```

首次使用新 Journal 时，该示例会把同 Session 的旧
`state/operation-events.jsonl` 对话一次性合并导入；原 JSONL 保留为可恢复备份。

---

## 2. 目录结构

```text
agent-loop/
├─ AGENTS.md                         要求 AI 先读业务需求文件
├─ BUSINESS_REQUIREMENTS.md          业务功能与工具的唯一需求入口
├─ TOOLS_IMPLEMENTATION_GUIDE.md     AI 必读的完整工具开发规范
├─ STATE_MACHINE_IMPLEMENTATION_GUIDE.md  AI 必读的业务状态机规范
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
│  ├─ retry_usage.py                 全部 Retry 能力离线演示
│  ├─ state_machine_usage.py         Runtime/业务状态机离线演示
│  ├─ tool_scheduling_usage.py       三种工具执行策略离线演示
│  ├─ durable_session_usage.py       Approval/写操作/恢复离线演示
│  ├─ durable_host_usage.py          P1 Host/Approval Resume 离线演示
│  ├─ mock_order_tools.py            模拟订单业务工具
│  └─ business_routing_usage.py      强制业务工具路由示例
├─ tests/
│  ├─ test_agent_loop.py             Agent Loop 核心语义测试
│  ├─ test_calculator_tools.py       加法、乘法、除法与注册表测试
│  ├─ test_tool_timeout.py           独立超时与取消隔离测试
│  ├─ test_cancellation.py           父子取消令牌传播测试
│  ├─ test_config.py                 TOML 配置校验测试
│  ├─ test_budgets.py                Turn/Tool/并行预算测试
│  ├─ test_basic_usage.py            动态命令行和调用原因测试
│  ├─ test_provider_settings.py      Provider 配置测试
│  ├─ test_retry.py                  模型和单工具重试测试
│  ├─ test_retry_advanced.py         持久化/Circuit/Task/Compaction 测试
│  ├─ test_runtime_state_machine.py  Runtime/Domain 状态机测试
│  ├─ test_tool_scheduling.py        Parallel/Exclusive/Resource Lock 测试
│  ├─ test_parallel_cleanup.py       Listener 异常与嵌套 Task 清理测试
│  ├─ test_tool_call_closure.py      Tool Call/Result 协议闭合测试
│  ├─ test_approval_resume_windows.py Approval Resume 崩溃窗口测试
│  ├─ test_approval_aware_recovery.py Approval/Write 感知恢复测试
│  ├─ test_durable_agent_host.py     P1 Host/Runtime/Approval Resume 测试
│  ├─ test_durable_session.py        Context/Approval/写操作/恢复测试
│  ├─ test_sqlite_transactional_store.py SQLite/CAS/Claim 并发测试
│  ├─ test_p0_durable_boundaries.py Action/Crash/Cancel P0 测试
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
   ├─ durable_action.py              统一 DurableActionEnvelope
   ├─ event_stream.py                异步事件流和最终结果
   ├─ messages.py                    消息构造与复制
   ├─ model_policy.py                Model Request/Continuation Policy
   ├─ types.py                       Model、Tool、Config 等类型
   ├─ loop.py                        低层 Agent Loop
   ├─ agent.py                       有状态 Agent 封装
   ├─ testing.py                     ScriptedProvider
   ├─ retry/
   │  ├─ types.py                    Model/Tool Retry Policy
   │  ├─ classifier.py               瞬时错误分类
   │  ├─ backoff.py                  Backoff/Jitter/可取消等待
   │  ├─ model.py                    RetryingStreamFn
   │  ├─ tool.py                     单逻辑 Tool Call 重试
   │  ├─ task.py                     Task/Workflow Retry
   │  ├─ events.py                   JSONL Retry Journal/Recovery
   │  ├─ circuit_breaker.py          Circuit Breaker
   │  ├─ compaction.py               Context Overflow 压缩重试
   │  ├─ outcome.py                  outcome_unknown 状态核对
   │  └─ errors.py                   Retryable/OutcomeUnknown Error
   ├─ runtime/
   │  ├─ states.py                   Run/Tool 状态
   │  ├─ events.py                   Runtime Event
   │  ├─ reducer.py                  Event → State
   │  ├─ invariants.py               非法转换检查
   │  ├─ projection.py               UI 状态视图
   │  └─ tracker.py                  Agent Event 适配与持久化
   ├─ session/
   │  ├─ store.py                    Runtime Store 接口
   │  ├─ jsonl.py                    Runtime Event JSONL
   │  ├─ replay.py                   Runtime 状态重放
   │  ├─ recovery.py                 崩溃恢复为 Suspended
   │  ├─ operation_events.py         完整 Operation Event
   │  ├─ operation_store.py          内存/JSONL Store 与 CAS 接口
   │  ├─ sqlite.py                   SQLite Transaction Store
   │  ├─ operation_state.py          Context/Model/Tool Reducer
   │  ├─ recorder.py                 Agent 持久事件 Recorder
   │  └─ resume.py                   安全恢复计划与执行协调
   ├─ security/
   │  └─ identity.py                 可信身份验证边界
   ├─ approval/
   │  └─ state_machine.py            持久 Approval 状态机
   ├─ writes/
   │  └─ state_machine.py            幂等写操作状态机
   ├─ domains/
   │  └─ state_machine.py            项目业务实体状态机
   ├─ harness/
   │  ├─ durable_agent_host.py       P1 统一编排入口
   │  ├─ model_runtime_adapter.py    可恢复模型 Runtime
   │  ├─ tool_runtime_adapter.py     可恢复工具 Runtime
   │  ├─ approval_gateway.py         Approval Resume 协调
   │  └─ startup_recovery.py         启动恢复扫描
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
      ├─ divide.py                   除法工具与除零保护
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
Ran 167 tests
OK
```

表示一百六十七个自动测试全部通过，并不是 Agent 又执行了一百六十七个用户任务。

一百六十七个测试分别检查：

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
76. 业务需求 MD 是否包含配置和工具生成所需章节；
77. 工具指南是否包含实现、注册、测试和完成标准；
78. 除法工具是否返回正确商和结构化 Details；
79. 除法工具是否拒绝正零和负零；
80. 除法工具是否使用独立 Timeout；
81. Provider 模型重试 TOML 是否正确加载；
82. HTTP 429 Retry 是否保持一个逻辑模型调用；
83. 模型 429 后是否重试同一个逻辑 Turn；
84. 模型 401 是否明确不重试；
85. 模型 Backoff 是否可被用户取消；
86. 并行工具是否只重试失败的那个；
87. 非 Retryable Tool Code 是否不重试；
88. Tool Retry 耗尽是否只返回一个最终错误；
89. 非幂等工具是否禁止配置自动重试；
90. Retry JSONL 是否脱敏并支持进程恢复；
91. Agent Retry 事件是否直接持久化；
92. Circuit Breaker 是否支持 Open/Half-open/Closed；
93. Circuit Open 是否阻止后续 Provider Attempt；
94. 模型 Retry 是否受最大总耗时限制；
95. Tool Retry 是否受最大总耗时限制；
96. outcome_unknown 是否不重试并支持状态核对；
97. TaskRetryExecutor 是否持久化并最终成功；
98. Context Overflow 是否压缩后重试同一 Turn；
99. Run/Tool/Retry 完整状态生命周期；
100. 并行工具是否在全部结束前保持 executing_tools；
101. 未知 Tool Call 终止事件是否被 Invariant 拒绝；
102. Run 终态后是否禁止继续写事件；
103. Runtime JSONL 重放是否得到相同状态；
104. 进程中断是否恢复为 suspended；
105. Runtime Tracker 是否可直接订阅现有 Agent Event；
106. Hybrid 路由阶段是否进入同一个 Run；
107. Domain 状态是否只能由可信事实来源推进；
108. Domain 状态是否执行 Approval 和乐观版本检查；
109. Recorder 是否持久化完整消息和工具事实；
110. 无模型路由结果是否形成完整 Durable Operation；
111. Recovery 是否识别未完成模型请求；
112. Recovery 是否区分 Safe Replay 和 Reconcile；
113. Safe Tool Replay 后是否继续模型并完成；
114. 可信身份是否拒绝错误凭证；
115. Approval 是否检查角色、自审、操作绑定和一次消费；
116. 写操作是否执行审批、幂等和重复请求去重；
117. outcome_unknown 写操作是否通过核对完成；
118. JSONL 重启后是否恢复完整消息 Context；
119. 状态机指南是否包含业务实现和恢复安全契约；
120. Exclusive 是否在前后 Parallel Pool 之间形成屏障；
121. Resource Lock 是否实现同资源串行、不同资源并行；
122. Resource Lock 是否在 Retry Backoff 期间释放；
123. 全局 Sequential 是否覆盖工具 Parallel；
124. 旧 Sequential 值是否兼容为 Exclusive；
125. Resource-locked 工具是否强制提供资源解析器；
126. Tool End Listener 异常是否取消兄弟嵌套 Execute Task；
127. Update Listener 异常后是否仍 Detach 子令牌；
128. 清理阶段次要异常是否不覆盖主要 Listener 异常；
129. 用户取消后是否没有 Tool Timer/Waiter/Update Task 残留；
130. RecoverableModelRuntime 是否复用正式 StreamFn；
131. RecoverableToolRuntime 是否复用参数/Timeout/Retry 管线；
132. RecoverableToolRuntime 是否拒绝未授权 Never Tool；
133. Startup Recovery 是否扫描并完成未结束 Operation；
134. Approval Resume 是否批准后恢复 Payload；
135. 进程中断后是否继续已消费 Approval 的 Resume；
136. DurableAgentHost 是否自动装配普通 Agent；
137. Approval 缺少可信申请人时是否安全结束 Operation；
138. Host Approval 是否执行幂等写并继续模型；
139. Sequential 中途取消是否补齐全部 Tool Result；
140. 首个 Tool Preflight 前取消是否补齐整批结果；
141. Parallel Preflight 取消是否闭合全部 Tool Call；
142. Exclusive Barrier 取消后续调用是否闭合；
143. Listener 异常后 Agent State 是否自动修复；
144. Error/Aborted Assistant 中 Tool Call 是否移除；
145. Duplicate/Orphan Tool Result 是否拒绝；
146. OpenAI Serializer 是否拒绝未闭合历史；
147. Durable Cancelled Operation 是否没有 Unresolved Tool Call；
148. 下一次 Prompt 前是否自动修复旧的未闭合历史；
149. Registered+Waiting 是否保持等待且不自动执行；
150. Granted 未 Consumed/Started 是否可恢复；
151. Consumed 未 Started 是否可恢复；
152. Started 未 Completed 是否可恢复；
153. Completed 重复调用是否不重复执行；
154. Rejected 是否不恢复并写 Cancelled；
155. 同进程并发 Resume 是否只执行一次；
156. Approved 缺少 Consumer 时是否保持可恢复；
157. Started 后重复副作用是否由 Idempotency 去重；
158. Waiting Approval 是否规划等待而不是 Execute Tool；
159. Direct Recovery 是否不会绕过 Waiting Approval；
160. Approved 缺 Consumer 是否返回明确等待状态；
161. Consumed Approval 是否规划 Resume Approved Write；
162. Rejected Approval 是否生成 Denied ToolResult；
163. Write Outcome Unknown 是否优先 Reconcile Write；
164. Waiting Approval 是否禁止 Tool Dispatch；
165. Approval Action Hash 与 Tool Call 不匹配是否拒绝；
166. Startup Recovery 是否报告 Waiting Approval 而非 Failed；
167. Never Tool 即使宽授权回调也不会绕过 Planner。

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

### 9.3 Parallel 工具

```python
tool = AgentTool(
    ...,
    execution_mode="parallel",
)
```

同一段中的 Parallel 工具进入有界并行池。

### 9.4 Exclusive Barrier

```python
tool = AgentTool(
    ...,
    execution_mode="exclusive",
)
```

Exclusive 会等待前面的并行池排空，单独执行完成后，后面的并行工具才开始。

```text
parallel A + parallel B
→ exclusive C
→ parallel D + parallel E
```

### 9.5 Resource Lock

```python
tool = AgentTool(
    ...,
    execution_mode="resource_locked",
    resolve_resource_keys=lambda args: f"order:{args['order_id']}",
)
```

相同 Resource Key 串行，不同 Key 可以并行。资源锁只在实际 Attempt 期间持有，Retry Backoff 会释放锁。

### 9.6 Sequential 兼容

旧值：

```python
execution_mode="sequential"
```

仍可使用，但内部等价于 `exclusive`。新工具应使用新的三种策略。

### 9.7 为什么工具结果不按完成顺序交给模型

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

EventStream 和工具 update 已支持：

- 有界队列；
- update 合并；
- 慢消费者策略；
- 最大内存限制；
- 终止事件优先投递。

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

## 20. 加法、乘法、除法工具和注册流程教学

当前实现三个计算工具：

```text
add          加法工具
multiply     乘法工具
divide       除法工具（包含除零保护）
```

目标不是建设完整文件工具系统，而是先学懂一条最小工具链：

```text
编写工具
  -> 注册工具
  -> 把工具交给 Agent
  -> 模型返回 toolCall
  -> Agent Loop 找到同名工具
  -> 执行 Python 函数
  -> ToolResult 写回模型上下文
  -> 模型生成最终回答
```

### 20.1 工具文件在哪里

```text
src/pi_agent_loop/tools/
├─ validators.py       二元数字与除法参数校验
├─ add.py              加法工具
├─ multiply.py         乘法工具
├─ divide.py           除法工具
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

### 20.3.1 除法工具

打开：

`src/pi_agent_loop/tools/divide.py`

工厂：

```python
create_divide_tool()
```

核心计算：

```python
value = a / b
```

除法额外使用 `validate_division_args()` 拒绝：

```text
b = 0
b = -0.0
```

默认独立 Timeout 为 3 秒，成功结果会返回 operation、a、b、value 和 toolCallId。

### 20.4 参数校验为什么独立

三个工具都需要 `a` 和 `b`，所以共享：

`src/pi_agent_loop/tools/validators.py`

校验规则：

- 参数必须是字典；
- 必须同时包含 `a` 和 `b`；
- 两者必须是 `int` 或 `float`；
- 拒绝布尔值；
- 拒绝 NaN；
- 拒绝正负无穷；
- 除法额外拒绝正零和负零。

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
registry.register(create_divide_tool())
```

此时：

```python
registry.names()
```

返回：

```python
["add", "multiply", "divide"]
```

### 20.6 为什么需要 ToolRegistry

也可以直接写：

```python
tools = [create_add_tool(), create_multiply_tool(), create_divide_tool()]
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

1. Provider 请求时可以看到 `add`、`multiply`、`divide` 的名称、描述和参数 Schema；
2. 模型返回 `toolCall(name="add")` 时，Agent Loop 能找到真正的 Python 加法函数。

### 20.8 完整注册代码

`examples/basic_usage.py` 中现在使用：

```python
registry = ToolRegistry()
registry.register(create_add_tool())
registry.register(create_multiply_tool())
registry.register(create_divide_tool())

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

自行决定直接回答，还是调用 `add`/`multiply`/`divide`。

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

当前共有 167 项离线测试，覆盖 Agent Loop、Tool Closure、P0 清理、Approval/Write 感知恢复、P1 Host、Durable Session 和状态机。

只有看到：

```text
Ran 167 tests
OK
```

才表示本阶段全部通过。

### 20.15 加法 2 秒、乘法 5 秒、除法 3 秒的独立 Timeout

当前工具配置：

```text
add.timeout_seconds = 2
multiply.timeout_seconds = 5
divide.timeout_seconds = 3
```

Timeout 是“最多允许执行多久”，不是故意等待多久。正常计算会立即完成。

#### 在 `basic_usage.py` 哪里设置模拟延时

打开：

`examples/basic_usage.py`

在文件顶部找到：

```python
ADD_TOOL_DELAY_SECONDS = 0.0
MULTIPLY_TOOL_DELAY_SECONDS = 0.0
DIVIDE_TOOL_DELAY_SECONDS = 0.0
```

正常成功时三个值都保持 `0.0`。

只触发加法超时：

```python
ADD_TOOL_DELAY_SECONDS = 3.0       # 大于加法限制 2 秒
MULTIPLY_TOOL_DELAY_SECONDS = 0.0
```

只触发乘法超时：

```python
MULTIPLY_TOOL_DELAY_SECONDS = 6.0  # 大于乘法限制 5 秒
```

只触发除法超时：

```python
DIVIDE_TOOL_DELAY_SECONDS = 4.0    # 大于除法限制 3 秒
```

多个工具同时触发超时：

```python
ADD_TOOL_DELAY_SECONDS = 3.0
MULTIPLY_TOOL_DELAY_SECONDS = 6.0
DIVIDE_TOOL_DELAY_SECONDS = 4.0
```

同一批工具会受最大并行数控制，单个工具超时不会取消兄弟工具。

然后运行：

```bat
python examples\basic_usage.py
```

事件中会看到：

```text
工具 add 执行失败，结果为 工具 add 执行超时（限制 2 秒）
```

还可能看到 multiply（5 秒）或 divide（3 秒）的独立超时错误。

工具自己的 timeout 配置位置：

- `src/pi_agent_loop/tools/add.py`；
- `src/pi_agent_loop/tools/multiply.py`；
- `src/pi_agent_loop/tools/divide.py`。

每个工具执行时会从 Agent 总 CancellationToken 创建自己的子令牌：

```text
Agent 总令牌
  ├─ add 子令牌，2 秒
  ├─ multiply 子令牌，5 秒
  └─ divide 子令牌，3 秒
```

行为：

- 用户取消 Agent：全部工具取消；
- add 超过 2 秒：只取消 add；
- multiply 超过 5 秒：只取消 multiply；
- divide 超过 3 秒：只取消 divide；
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
- [x] 除法工具独立文件和除零保护；
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
- [x] 除法 timeout 为 3 秒；
- [x] 父子 CancellationToken；
- [x] 并行 timeout 隔离；
- [x] timeout 和取消测试；
- [x] `TOOLS_IMPLEMENTATION_GUIDE.md` 完整工具规范；
- [x] AGENTS.md 强制 AI 开发工具前读取指南。

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
- `tests/test_business_requirements.py`：4 项 AI 需求、工具与状态机指南契约测试；
- `tests/test_retry.py`：7 项模型和单工具重试测试；
- `tests/test_retry_advanced.py`：9 项持久化、Circuit、Task、Outcome 和 Compaction 测试；
- `tests/test_runtime_state_machine.py`：10 项 Runtime/Domain 状态机测试；
- `tests/test_durable_session.py`：10 项 Context、Recovery、Identity、Approval 和 Write 测试；
- `tests/test_tool_scheduling.py`：6 项 Parallel/Exclusive/Resource Lock 测试；
- `tests/test_parallel_cleanup.py`：4 项 Listener 异常和嵌套 Task 清理测试；
- `tests/test_durable_agent_host.py`：9 项 P1 Host、Recovery Runtime 和 Approval Resume 测试；
- `tests/test_tool_call_closure.py`：10 项 Tool Call/Result 协议闭合测试；
- `tests/test_approval_resume_windows.py`：9 项 Approval Resume 崩溃窗口测试；
- `tests/test_approval_aware_recovery.py`：10 项 Approval/Write 感知恢复测试。

全部测试：

```text
Ran 167 tests
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

示例同时注册 `add`、`multiply` 和 `divide`，因此可以测试：

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

覆盖 Hybrid 路由、Retry、状态机、Durable Session、P0 清理、Tool Closure、Approval/Write 感知恢复、P1 Host 和调度。

```text
Ran 167 tests
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

---

## 25. AI 工具开发指南

新增：

```text
TOOLS_IMPLEMENTATION_GUIDE.md
```

后续 AI 新增或修改任何 AgentTool 前，必须同时阅读：

```text
AGENTS.md
BUSINESS_REQUIREMENTS.md
TOOLS_IMPLEMENTATION_GUIDE.md
```

指南完整定义：

- Tool、Capability、Intent 和 Tool Call 的区别；
- 工具文件位置和命名；
- JSON Schema；
- Python 运行时校验；
- Execute 标准签名；
- CancellationToken；
- Update；
- 独立 Timeout；
- 结构化 Result；
- 错误脱敏；
- 只读和写工具差异；
- ToolRegistry 注册；
- CapabilityRegistry 注册；
- 包公开导出；
- 单元测试和 Agent 集成测试；
- 完整工具模板；
- AI 开发步骤；
- Definition of Done。

`AGENTS.md` 已加入强制规则，契约测试会防止该指南或引用被误删。

---

## 26. 模型和单工具重试

### 26.1 Provider 配置

`providers.toml.example` 增加：

```toml
[profiles.third_party.retry]
enabled = true
max_retries = 2
initial_delay_seconds = 0.5
max_delay_seconds = 30
jitter_ratio = 0.2
retryable_statuses = [408, 409, 429, 500, 502, 503, 504]
```

旧本地配置没有 `[retry]` 时默认关闭，避免升级后突然产生额外收费请求。

### 26.2 模型 Retry

```text
OpenAICompatibleProvider.stream
→ RetryingStreamFn
→ 一个逻辑模型 Turn
→ 多个 HTTP/SSE Attempt
```

`provider.call_count` 统计逻辑模型调用，`provider.attempt_count` 统计包含重试的实际 HTTP Attempt。

支持：

- 结构化 Provider Error；
- 408/409/429/5xx；
- Timeout 和连接错误；
- Retry-After；
- 指数 Backoff；
- Jitter；
- CancellationToken；
- 重试生命周期事件；
- 失败 Partial Assistant 不进入 Agent Context。

401/403/404、配额、业务错误和用户取消不重试。

启用 Retry 时，每次 Attempt 的流事件会先缓冲；失败 Attempt 被丢弃，最终成功或最终失败 Attempt 才交给 Agent Loop。这样更安全，但会牺牲该次模型响应的实时 Token 展示。

### 26.3 模型 Retry 事件

```text
model_retry_scheduled
model_retry_attempt_start
model_retry_finished
```

`basic_usage.py` 已提供中文解释。

### 26.4 单工具 Retry

工具显式声明：

```python
retry_policy=ToolRetryPolicy(
    max_retries=2,
    retryable_codes=frozenset({
        "network_error",
        "rate_limited",
        "upstream_unavailable",
    }),
    idempotent=True,
)
```

工具遇到瞬时错误时抛出：

```python
raise RetryableToolError(
    "上游暂时不可用",
    code="upstream_unavailable",
    retry_after_seconds=1,
)
```

非幂等工具无法创建自动 Retry Policy。

### 26.5 并行工具

```text
A 成功
B 瞬时失败
C 成功
```

只重试 B，A/C 不会重复执行。Retry Backoff 不占 `max_parallel_tools` 槽位，每个新 Attempt 重新申请 Semaphore。

同一个逻辑 Tool Call 最终只生成一个 ToolResult，重试耗尽时 details 包含：

```text
code
retryable
attempts
retryId
```

### 26.6 Tool Retry 事件

```text
tool_retry_scheduled
tool_retry_attempt_start
tool_retry_finished
```

事件不会包含 API Key 或完整敏感参数。

### 26.7 预算语义

```text
模型 Retry Attempt
不增加 max_turns，但受 max_retries 限制

工具 Retry Attempt
不增加 max_tool_calls，但受 ToolRetryPolicy.max_retries 限制
```

工具总 Timeout 从第一次真正获得执行槽开始计算，包含后续 Attempt 和 Backoff；初次排队等待并发槽不消耗工具 Timeout。

### 26.8 Durable Retry Journal

```python
store = JsonlRetryEventStore("state/retry-events.jsonl")
agent = Agent(..., retry_event_sink=store.append)
```

模型和工具 Retry 的 Scheduled/AttemptStart/Finished 会在 Backoff 或 UI 发布前写入 JSONL。Journal 只允许脱敏元数据，不保存 Prompt、完整工具参数或 API Key。

`state/` 已加入 `.gitignore`。

### 26.9 进程恢复

```python
manager = RetryRecoveryManager(store)
await manager.recover(handler)
```

启动时扫描没有 Finished 的 Retry Chain，并把每条 Chain 交给 Host 提供的恢复处理器。恢复开始和结束也会写入 Journal。

恢复管理器不自行持久化或重放 Prompt；Host 必须根据 Session/业务状态安全重建操作。

### 26.10 Circuit Breaker

Provider Retry Policy 支持：

```toml
[profiles.third_party.retry.circuit_breaker]
enabled = true
failure_threshold = 3
recovery_timeout_seconds = 30
```

状态：

```text
Closed
→ 连续瞬时失败达到阈值
→ Open 快速失败
→ 等待恢复窗口
→ Half-open 只允许一个探测
→ 成功后 Closed
```

Circuit 当前是进程内状态，重启后重置。

### 26.11 最大 Retry 总耗时

模型：

```toml
max_elapsed_seconds = 120
```

工具和 Task Policy 也拥有 `max_elapsed_seconds`。如果下一次 Backoff 会超过总耗时，立即停止，不再等待。

### 26.12 outcome_unknown

写工具在“服务端可能成功、客户端未收到结果”时抛出：

```python
OutcomeUnknownToolError(
    "操作结果不确定",
    operation_id="operation-1",
    idempotency_key="...",
    reconciliation_name="check_operation",
)
```

Agent 返回：

```text
code=outcome_unknown
retryable=false
```

幂等键不会进入 ToolResult。使用 `OutcomeReconciliationRegistry` 注册状态核对器；核对器查询最终状态，不重放原写操作。

### 26.13 Task/Workflow Retry

```python
executor = TaskRetryExecutor(
    TaskRetryPolicy(
        max_retries=2,
        retryable_codes=frozenset({"worker_unavailable"}),
        idempotent=True,
    ),
    event_store=store,
)
```

该执行器适用于 Host 已经定义好的一个幂等 Task。它不负责把自然语言拆成多 Intent Plan，也不允许非幂等 Workflow 自动重试。

### 26.14 Context Overflow Compaction Retry

```python
stream_fn = compact_on_context_overflow(
    provider.stream,
    CompactionRetryPolicy(
        max_retries=1,
        keep_recent_messages=20,
    ),
)
```

检测到 `context_overflow/context_length_exceeded` 后：

```text
丢弃失败 Assistant
→ 压缩消息
→ 同一逻辑 Turn 重试一次
```

默认 `SlidingWindowCompactor` 只是教学实现。生产系统应替换为 token-aware 摘要器，并维护 Tool Call/Tool Result 配对。

### 26.15 使用当前工具测试全部 Retry

全部离线演示：

```bat
python examples\retry_usage.py all
```

单独测试：

```bat
python examples\retry_usage.py model
python examples\retry_usage.py tool
python examples\retry_usage.py circuit
python examples\retry_usage.py task
python examples\retry_usage.py outcome
python examples\retry_usage.py compaction
python examples\retry_usage.py recovery
```

其中 `tool` 场景使用当前真实 `divide` 工具：第一次模拟上游瞬时失败，第二次执行 `10÷4` 成功。该模拟故障是工厂外层测试包装，不会污染 divide 的模型参数 Schema。

查看持久 Journal：

```bat
type state\retry-demo.jsonl
```

运行自动测试：

```bat
python -m unittest tests.test_retry -v
python -m unittest tests.test_retry_advanced -v
python -m unittest discover -s tests -v
```

### 26.16 当前边界

- Process Recovery 已实现 Chain 发现和 Handler 协调，但完整模型请求恢复仍依赖未来 Session Store；
- Circuit 状态尚未跨进程持久化；
- 兼容用 Sliding Window Compactor 仍保留；生产入口默认使用 Token-aware 结构化摘要器；
- TaskRetryExecutor 仍只负责单任务重试；多 Intent 请求使用独立的 `HybridRequestPlanner` 和 `PlanExecutor`；
- Outcome Reconciliation 需要真实业务提供查询 API；
- 写工具默认不配置自动 Retry。

---

## 27. Runtime 与项目业务状态机

### 27.1 分层

```text
loop.py
只负责模型、工具和消息执行，并继续发出事实事件

runtime/
定义 Run/Tool 状态、Runtime Event、Reducer、Invariant 和 Projection

session/
保存 Runtime Event、重放和崩溃恢复

domains/
定义具体项目业务实体的状态转换
```

没有把订单、退款、JSONL 或 UI 状态逻辑塞入低层 Agent Loop。

### 27.2 通用 Run 状态

```text
idle
running
routing
requesting_model
executing_tools
retrying
compacting
waiting_approval
outcome_unknown
completed
failed
cancelled
suspended
```

每个并行 Tool Call 另有独立状态：

```text
queued
executing
retry_backoff
succeeded
failed
timed_out
cancelled
outcome_unknown
```

### 27.3 Runtime Event

```text
run_started/run_finished/run_interrupted
routing_started/routing_finished
turn_started/turn_finished
model_request_started/model_response_finished
model_retry_*
context_compaction_*
tool_started/tool_retry_*/tool_finished
approval_*
outcome_unknown/reconciliation_*
budget_exceeded
```

`RuntimeStateTracker` 可以直接传给：

```python
agent.subscribe(runtime_tracker.listener)
```

它先验证状态转换，再写 Store，最后提交内存快照。

### 27.4 Reducer 和 Invariant

```python
next_state = reduce_runtime_state(current_state, event)
```

已经拒绝：

- Event sequence 不连续；
- 没有活动 Run 却写运行事件；
- Run ID 不匹配；
- 终态后继续写事件；
- 未知 Tool Call 完成或重试；
- Tool 终态后继续更新；
- 重复 Tool Start；
- 非 waiting_approval 状态批准/拒绝；
- Run Completed 时仍有未完成工具。

### 27.5 Session Store 与恢复

```python
store = JsonlRuntimeEventStore("state/runtime-events.jsonl")
await RuntimeRecoveryManager(store).recover()
tracker = await RuntimeStateTracker.create(store)
```

正常启动会重放 JSONL 得到当前状态。若上次进程停在非终态，恢复管理器追加：

```text
run_interrupted
```

并把状态变为：

```text
suspended
```

状态文件位于已忽略的 `state/`。

### 27.6 Projection

```python
view = project_runtime_state(tracker.state)
```

返回：

```text
Run ID
phase/中文 phaseLabel
terminal
turn
modelRetryAttempt
activeToolCount
每个工具状态
failureCode
routingStatus
lastEvent
sequence
```

CLI、TUI 和 Web 应读取 Projection，而不是各自猜测状态。

### 27.7 Hybrid Router 同一个 Run

`RoutedAgent` 可接收：

```python
runtime_tracker=tracker
```

流程：

```text
run_started
→ routing_started
→ HybridModelRouter
→ routing_finished
→ Agent Loop 模型/工具事件
→ run_finished
```

Capability Missing、Out of Scope、Prohibited 等不调用回答模型的结果也会形成一个完整 Run。

### 27.8 业务 Domain 状态机

```python
machine = DomainStateMachine(
    initial_state="pending_payment",
    transitions=[
        DomainTransition(
            event_type="payment_succeeded",
            from_states=frozenset({"pending_payment"}),
            to_state="paid",
            allowed_sources=frozenset({"payment_api"}),
        )
    ],
)
```

应用可信事件：

```python
state = machine.apply(
    state,
    DomainEvent(
        entity_id="order-1001",
        type="payment_succeeded",
        source="payment_api",
        expected_version=0,
    ),
)
```

状态机检查：

- Entity ID；
- 当前状态是否允许该事件；
- 事件来源是否可信；
- 是否完成 Approval；
- expected_version 是否与当前版本一致。

用户或模型说“订单已发货”不能直接改变订单状态；必须由业务 API/工具产生可信事件。

### 27.9 运行演示

```bat
python examples\state_machine_usage.py
```

演示内容：

```text
ScriptedProvider → divide → Tool Result
→ Runtime Event JSONL
→ RunState completed

订单 pending_payment
→ payment_api
→ paid
→ order_api
→ shipped
→ 非法转换被拒绝
```

查看状态事件：

```bat
type state\state-machine-demo.jsonl
```

### 27.10 当前示例已接入

```text
examples/basic_usage.py
examples/real_model_usage.py
examples/business_routing_usage.py
```

会写入：

```text
state/runtime-events.jsonl
```

并显示最终运行状态和 Run ID。

### 27.11 业务需求文件

`BUSINESS_REQUIREMENTS.md` 已增加业务状态转换表，要求填写：

```text
实体
事件
允许来源状态
目标状态
可信事实来源
是否审批
```

AI 不得自行发明生产业务状态和转换。

### 27.12 测试

```bat
python -m unittest tests.test_runtime_state_machine -v
python -m unittest discover -s tests -v
```

当前：

```text
Ran 227 tests
OK
```

### 27.13 当前边界

- DurableAgentHost 的 Runtime/Operation/Retry Event 默认共用加密的 SQLite Session Journal；
- JSONL Store 只保留给单实例示例和兼容入口；
- 完整 Context 和恢复计划已实现，但真实 Provider/工具恢复要由 Host 注入 Callback；
- DomainStateMachine 是基础框架，真实项目必须提供自己的状态表；
- 多 Intent Plan、依赖图、审批屏障、Task 状态机和结果合成器已经提供，真实项目仍需注册 Intent 策略与执行 Handler；
- 分布式多写者需要数据库事务或单写者协议。

---

## 28. Durable Session、可信 Approval 和幂等写操作骨架

### 28.1 完整 Operation Event

新增持久事实：

```text
operation_started/operation_finished
message_appended
model_request_started/completed/failed
tool_intent_recorded
tool_dispatch_started
tool_completed/tool_outcome_unknown/tool_reconciled
routing_started/routing_finished
approval_*
write_*
```

与 Runtime Projection 不同，Operation Store 会保存恢复所需的完整 User/Assistant/ToolResult Message、Tool Arguments 和结果。文件位于已忽略的 `state/operation-events.jsonl`，生产系统应加密并执行数据保留策略。

### 28.2 DurableOperationRecorder

```python
recorder = DurableOperationRecorder(
    JsonlOperationEventStore("state/operation-events.jsonl"),
    session_id="my-session",
    tools=tools,
    configuration={"provider": model.provider, "model": model.id},
)
agent.subscribe(recorder.listener)
```

Recorder 在实际工具函数前记录 `tool_dispatch_started`。工具声明：

```python
replay_policy="safe"   # 只读/幂等，崩溃后可重放
replay_policy="never"  # 写操作，崩溃后必须核对
```

### 28.3 Recovery Planner

```python
plan = await DurableSessionRecovery(store).plan(
    session_id="my-session",
    operation_id="...",
)
```

可能动作：

```text
retry_model_request
continue_model
execute_tool
replay_safe_tool
reconcile_tool
materialize_tool_result
finish_operation
manual_intervention
```

判断规则：

```text
Model Started 无 Completed
→ 重试模型请求

Assistant Tool Call 尚未 Dispatch
→ 可以执行

Tool 已 Dispatch + replay_policy=safe
→ 可以安全重放

Tool 已 Dispatch + replay_policy=never
→ 必须核对，不能重放

Tool Result 已持久化但消息未写入
→ 补写 ToolResult Message

最后消息是 User/ToolResult
→ 继续模型
```

### 28.4 Recovery Executor

Host 提供可信 Callback；模型回调必须接收持久化策略：

```python
async def request_model(messages, request_policy):
    return await model_runtime.request(
        messages,
        policy=request_policy,
    )

callbacks = RecoveryCallbacks(
    request_model=request_model,
    execute_tool=execute_tool,
    reconcile_tool=reconcile_tool,
)

result = await recovery.resume(
    session_id="...",
    operation_id="...",
    callbacks=callbacks,
)
```

恢复器会循环规划、持久化 Attempt、执行安全动作、补齐消息，再继续模型，直到 Operation 完成或需要人工介入。

### 28.5 可信身份

```python
identity = await verifier.verify(
    IdentityClaim("operator", credential)
)
```

后续 Approval 和写操作只接受 `VerifiedIdentity`。项目提供的 `StaticIdentityVerifier` 仅用于开发测试；生产必须替换为 OAuth、IAM、企业 SSO 或其他可信认证系统。

持久事件只保存 Principal ID、Role、Issuer/Verification ID，不保存 Credential。

### 28.6 Approval 状态机

```text
waiting
→ approved/rejected/expired
approved
→ consumed
```

检查：

- 所需角色；
-禁止默认自审；
- Approval 与操作 Hash 绑定；
-过期时间；
-一次性消费；
-可信审批人身份。

```python
approval = await approvals.request(...)
await approvals.grant(approval.approval_id, approver)
await approvals.consume(
    approval.approval_id,
    action=exact_action,
    consumer=operator,
)
```

参数或工具发生变化后，旧 Approval 无法消费。

### 28.7 写操作状态机

```text
prepared
waiting_approval
approved
submitting
succeeded/failed/outcome_unknown
outcome_unknown
→ reconciling
→ succeeded/failed
```

写操作使用 Idempotency Key，但持久化只保存 SHA-256 Hash。

同一个 Idempotency Key 和相同 Action 返回原 Write ID；同一个 Key 用于不同 Action 会返回 `idempotency_conflict`。

写操作 `outcome_unknown` 不会自动重放，而是进入状态核对。

### 28.8 离线演示

```bat
python examples\durable_session_usage.py
```

输出包括：

```text
写操作 waiting_approval
→ 可信 approver 批准
→ succeeded
→ 同 Idempotency Key 重复请求不重复执行

崩溃在 safe divide Dispatch 后
→ Recovery Planner 选择 replay_safe_tool
→ 补写 ToolResult
→ 继续模型
→ Operation completed
```

Journal：

```bat
type state\durable-session-demo.jsonl
```

### 28.9 当前真实示例

`basic_usage.py`、`real_model_usage.py`、`business_routing_usage.py` 已接入 `DurableOperationRecorder`，会显示 Durable Operation ID 并保存完整 Context。

业务入口的 Routing 也在同一个 Operation 中，Capability Missing、Prohibited 等零模型结果同样会正常结束 Operation。

### 28.10 测试

```bat
python -m unittest tests.test_durable_session -v
python -m unittest discover -s tests -v
```

当前：

```text
Ran 189 tests
OK
```

### 28.11 骨架边界

- SQLite 支持单机多进程事务；跨机器 Worker 仍需 PostgreSQL Store；
- JSONL 仅适用于单 Store 实例兼容模式；
- 完整消息和工具参数可能包含敏感业务数据，生产存储必须加密、控制权限和设置保留周期；
- Recovery Callback 是 Host 信任边界，必须调用真实 Provider/Tool Runtime，不能绕过 Guard；
- StaticIdentityVerifier 只能用于开发测试；
- Approval 尚未提供 Web/TUI 交互界面；
- 真实业务状态机继续由 `BUSINESS_REQUIREMENTS.md` 状态表生成。

---

## 29. AI 必读真实业务状态机指南

新增：

```text
STATE_MACHINE_IMPLEMENTATION_GUIDE.md
```

以后新增或修改真实业务 State、Event、Transition、Reducer、Approval、WriteOperation 或 Recovery 时，AI 必须依次阅读：

```text
AGENTS.md
BUSINESS_REQUIREMENTS.md
TOOLS_IMPLEMENTATION_GUIDE.md（涉及工具时）
STATE_MACHINE_IMPLEMENTATION_GUIDE.md
```

指南完整规定：

- State、Command、Event、Guard、Action 的区别；
- Runtime 状态与业务 Domain 状态分离；
- 业务人员需要填写的状态转换表；
- AI 编码前必须确认的问题；
- 推荐 Domain 目录；
- DomainStateMachine 使用方法；
- Command 不能直接修改状态；
- 可信事实来源；
- VerifiedIdentity 和权限；
- Approval 与 Action Hash；
- WriteOperation 和 Idempotency；
- replay_policy；
-完整 Context 和 Recovery；
-expected_version 和并发控制；
-纯 Reducer；
-事件持久化；
-Snapshot/Migration；
-终态、可恢复状态和 Manual Intervention；
-错误、Reconcile 和 Compensation；
-Projection；
-测试矩阵；
-AI 实现步骤；
-常见错误设计；
-Definition of Done。

`BUSINESS_REQUIREMENTS.md` 仍是唯一真实业务需求来源；状态机指南只定义实现方法，不复制具体业务状态，避免两份配置冲突。

`AGENTS.md` 和契约测试会防止 AI 忘记阅读该指南。

---

## 30. Parallel、Exclusive 和 Resource-Locked 调度

### 30.1 三种策略

```text
parallel
互不影响的纯计算或只读操作进入有界并行池

exclusive
形成全局屏障，等待前一并行池排空后单独执行

resource_locked
只锁定具体资源；相同 Key 串行，不同 Key 并行
```

全局 `tool_execution="sequential"` 仍会覆盖所有工具并强制串行。

### 30.2 Exclusive Barrier

模型顺序：

```text
parallel A
parallel B
exclusive C
parallel D
parallel E
```

执行：

```text
A + B 并行
→ C 独占
→ D + E 并行
```

`sequential` 作为旧值继续兼容，但等价于 `exclusive`。

### 30.3 Resource Lock

```python
AgentTool(
    ...,
    execution_mode="resource_locked",
    resolve_resource_keys=lambda args: f"order:{args['order_id']}",
)
```

```text
order:1001 与 order:1001
→ 串行

order:1001 与 order:2002
→ 可以并行
```

支持一个工具返回多个 Key；调度器排序和去重后加锁，避免不同加锁顺序造成死锁。

### 30.4 与 Retry 的关系

Resource Lock 和 Semaphore 只在真实 Attempt 执行期间持有：

```text
Attempt 失败
→ 释放 Resource Lock 和并发槽
→ Retry Backoff
→ 下一 Attempt 重新获取
```

所以等待 Backoff 不会阻塞同资源后续操作，也不会浪费并发槽。

Exclusive 则在整个逻辑调用（包括 Retry）完成前保持屏障，防止后续分段越过独占操作。

### 30.5 调度事件

新增：

```text
tool_execution_queued
```

包含：

```text
Tool Call ID
Tool Name
executionMode
Resource Key Count
```

真实 Key 不进入普通调度事件。

现有：

```text
tool_execution_dispatch_start
```

表示某个 Attempt 真正进入工具函数。Runtime 和 DurableOperationRecorder 可以区分 Queued、Intent、Dispatch 和 Result。

### 30.6 运行演示

```bat
python examples\tool_scheduling_usage.py
```

预期时间线：

```text
read-a/read-b 同时开始
→ 两者结束
→ exclusive 单独执行
→ exclusive 结束
→ order:1001 和 order:2002 并行
→ 第二个 order:1001 等第一个结束后执行
```

### 30.7 写工具建议

全局独占写操作：

```python
execution_mode="exclusive"
replay_policy="never"
```

按实体锁定的写操作：

```python
execution_mode="resource_locked"
resolve_resource_keys=lambda args: f"order:{args['order_id']}"
replay_policy="never"
```

同时仍需 Approval、Idempotency Key、expected_version 和 outcome_unknown 核对。

### 30.8 测试

```bat
python -m unittest tests.test_tool_scheduling -v
python -m unittest discover -s tests -v
```

当前：

```text
Ran 167 tests
OK
```

覆盖 Exclusive Barrier、资源冲突、不同资源并行、Retry 释放锁、全局串行覆盖、Sequential 兼容和无资源解析器拒绝。

---

## 31. P0 并行异常清理

### 31.1 问题

一个并行 `run_one` 因 Listener 或 Scheduler 异常被取消时，内部独立创建的 `execute_task` 不会由 asyncio 自动级联取消。如果不显式清理，Agent 结束后工具可能继续运行。

Update Listener 异常也可能在旧实现中阻止 `CancellationToken.detach()`。

### 31.2 当前清理顺序

`_execute_prepared_tool()` 最外层现在执行：

```text
禁止后续 Update
→ 取消工具子 CancellationToken
→ 取消并等待嵌套 execute_task
→ 取消并等待 timeout/cancellation waiter
→ 等待全部 update task，收集 Listener 错误
→ finally 中强制 detach 子令牌
→ 保留最早的主要异常
```

### 31.3 主要异常优先

例如：

```text
主要错误：tool_execution_end Listener 失败
清理错误：慢工具取消收尾又抛异常
```

最终 Agent Error 保留主要 Listener 错误，次要清理错误不会覆盖根因。

如果没有更早错误，Update Listener 错误仍会在所有清理完成后按严格 Listener 语义向上传播。

### 31.4 Task 命名和诊断

内部 Task 现在使用可识别名称：

```text
pi-tool-run:...
pi-tool-execute:...
pi-tool-update:...
pi-tool-timeout:...
pi-tool-cancel-wait:...
```

测试和诊断可以通过 `asyncio.all_tasks()` 检查 Agent 结束后是否仍有 Tool Task。

`CancellationToken.child_count` 可以检查是否仍挂接子令牌。

### 31.5 测试

```bat
python -m unittest tests.test_parallel_cleanup -v
python -m unittest discover -s tests -v
```

覆盖：

- Tool End Listener 异常取消兄弟 Execute Task；
- Update Listener 异常后仍 Detach；
-次要清理异常不覆盖主要异常；
-用户取消后没有 Timer/Waiter/Update Task 残留。

```text
Ran 167 tests
OK
```

### 31.6 边界

asyncio 只能可靠清理会响应 Cancellation 的协程。完全阻塞事件循环、故意吞掉 CancelledError 或卡死的第三方同步代码，仍必须放入子进程并使用进程级终止。

---

## 32. P1 DurableAgentHost 与 Approval Resume

### 32.1 统一 Host

```python
host = await DurableAgentHost.create(
    session_id="my-session",
    state_dir="state/my-session",
    model=model,
    stream_fn=provider.stream,
    system_prompt="...",
    tools=tools,
    router=router,
    capabilities=capabilities,
)
```

Host 自动装配：

```text
Agent
RuntimeStateTracker
DurableOperationRecorder
Retry Journal
Context Compaction
RecoverableModelRuntime
RecoverableToolRuntime
StartupRecoveryCoordinator
ApprovalService
ApprovalResumeCoordinator
WriteOperationService
```

`auto_recover=True` 时创建 Host 会扫描未完成 Operation。

### 32.2 RecoverableModelRuntime

恢复模型请求仍通过正式 StreamFn，因此继续使用 Provider Retry、Circuit Breaker、Compaction、鉴权和 Retry Event Store。

```python
message = await host.model_runtime.request(messages)
```

### 32.3 RecoverableToolRuntime

```python
result = await host.tool_runtime.execute(recovery_action)
```

恢复工具继续经过：

```text
Agent Tool 参数校验
Before/After Hook
Timeout
Tool Retry
Cancellation
Parallel/Exclusive/Resource Policy
Tool Result 标准化
```

`replay_safe_tool` 必须匹配 `replay_policy="safe"`。

`replay_policy="never"` 默认拒绝，只有注入 `authorize_never_replay` 后才允许进入执行管线；真实写操作优先使用 WriteOperationService。

### 32.4 Startup Recovery

```python
report = await host.recover_on_startup()
```

报告：

```text
completed
waiting_approval
ready_to_resume
manual_intervention
failed
```

扫描每个非终态 Operation，使用 Model/Tool/Reconcile Runtime Callback 恢复。

### 32.5 Approval Resume

Host 使用 Router 得到 `in_scope_approval_required` 时，在一个事务中持久化：

```text
User Message
Host 确认的 Assistant Tool Call
DurableActionEnvelope
Approval Request + Resume Registered
Write Prepared + Waiting Approval
Tool Intent
```

Envelope 统一绑定：

```text
operationId
toolCallId
toolName
exact arguments
writeId
```

使用时必须在初始 Prompt 提供 Idempotency Key：

```python
pending = await host.prompt(
    "取消订单 1001",
    requester=verified_operator,
    idempotency_key="cancel-order-1001",
)
```

批准并继续：

```python
final = await host.approve_and_resume(
    pending.approval_id,
    approver=verified_approver,
    consumer=verified_operator,
    idempotency_key="cancel-order-1001",
    write_handler=cancel_order_handler,
)
```

流程：

```text
验证 Approver Role
→ 禁止默认自审
→ 校验 Action/Resume/Write Envelope
→ 消费 Approval 一次
→ 复用已持久 Write/Tool Intent
→ CAS Claim + Tool Dispatch
→ Idempotency 去重
→ 执行写 Handler
→ Tool Result 写入完整 Context
→ 正式 Model Runtime 生成最终回答
→ Operation/Run Completed
```

缺少 VerifiedIdentity 时，Host 会安全结束当前 Operation 为 Failed，不留下 waiting 状态。

### 32.6 Approval Resume 崩溃恢复

如果 Approval 已消费、Resume 已开始但进程崩溃：

```python
await host.approval_resume.recover_incomplete(resume_callback)
```

会读取持久 Resume Payload，继续未完成恢复，并写入 Completed/Failed。

Write Handler 仍必须使用 Idempotency Key，防止崩溃恢复重复写入。

### 32.7 Recovery 信任边界

`RecoveryCallbacks`、`write_handler` 和 `reconcile_tool` 属于 Host 信任边界。

生产实现必须：

- 使用正式 Provider/Tool Adapter；
-不能绕过 Guard/Permission/Approval；
-不能把 Never Tool 当 Safe Tool；
-使用 Idempotency 和 expected_version；
-持久化结果后再对外确认成功。

### 32.8 运行演示

```bat
python examples\durable_host_usage.py
```

输出：

```text
普通 Host 自动装配并完成回答
→ Runtime completed
→ Durable Operation ID

写操作 waiting_approval
→ 可信 Approver 批准
→ 写 Handler 只执行一次
→ 模型继续回答
→ Runtime completed
```

### 32.9 测试

```bat
python -m unittest tests.test_durable_agent_host -v
python -m unittest discover -s tests -v
```

当前：

```text
Ran 167 tests
OK
```

覆盖正式 Model Runtime、Tool Runtime、Never Tool 拒绝、Startup Recovery、Approval Resume、崩溃恢复、Host 自动装配、缺失身份安全失败和幂等写后继续模型。

### 32.10 当前边界

- StaticIdentityVerifier 仍只用于开发测试；
- Approval UI/API 和通知尚未实现；
- DurableAgentHost 默认使用加密、租户隔离的 SQLite Session Journal；
- JSONL 只保留为 `store_backend="jsonl"` 单实例兼容模式；
- 跨机器多 Worker 需要后续 PostgreSQL Store；
- 真实业务 Tool/Identity/Reconciliation 需要后续 Adapter；
- 多 Intent Plan/Task 执行框架已实现；跨机器 Worker Queue 和故障接管仍属于后续分布式能力。

---

## 33. Tool Call/Tool Result 协议闭合

### 33.1 全局不变量

```text
一个 Assistant Message 中的每个 Tool Call
→ 在下一条 User/Assistant Message 前
→ 必须有且只有一个对应 ToolResult
```

缺失结果会导致 OpenAI-compatible API 返回协议错误。

### 33.2 取消时补齐未执行调用

Sequential A/B/C 在 A 执行时取消：

```text
A → tool_cancelled
B → tool_aborted_before_dispatch
C → tool_aborted_before_dispatch
```

B/C 不会执行，只生成 Synthetic Error ToolResult。

Parallel Preflight、Exclusive Barrier 和 Resource 调度同样保证整批 Tool Call 闭合。

### 33.3 Error/Aborted Assistant

Error/Aborted Assistant 中的 Tool Call 可能不完整，最终提交历史前会移除这些 Tool Call，不执行它们。

模型 Retry 的失败 Partial Attempt 继续整体丢弃。

### 33.4 Agent 异常修复

Listener/Scheduler 异常导致部分 ToolResult 尚未写入时：

```text
扫描 Agent State
→ 按 Tool Call 原始顺序补 Synthetic ToolResult
→ 再追加失败 Assistant
```

修复通知：

```text
transcript_repaired
```

DurableOperationRecorder 会把修复结果写入 Operation Context。

### 33.5 下一次 Prompt 前修复

Agent 在加入新 User Message 前自动检查旧历史：

```text
明确缺失 Result
→ 补 tool_result_missing_repaired

Duplicate/Orphan/Name Mismatch
→ TranscriptIntegrityError
→ 禁止继续请求模型
```

### 33.6 Provider 最后防线

`serialize_chat_request()` 在生成 OpenAI Payload 前再次调用：

```python
validate_closed_tool_call_transcript(messages)
```

未闭合、重复、孤立或 Name 不匹配的历史不会发送给 Provider。

### 33.7 Durable Operation Invariant

`operation_finished` 前检查：

- 不存在 Started 未完成的 Model Request；
-每个 Assistant Tool Call 都有 ToolResult；
-Duplicate/Orphan Result 不存在。

Cancelled/Failed Operation 也必须先闭合 Tool Batch。

### 33.8 公开工具

```python
analyze_tool_call_transcript(messages)
repair_unresolved_tool_calls(messages)
validate_closed_tool_call_transcript(messages)
```

错误：

```python
TranscriptIntegrityError
```

### 33.9 测试

```bat
python -m unittest tests.test_tool_call_closure -v
python -m unittest discover -s tests -v
```

覆盖 Sequential/Parallel/Exclusive 取消、Listener 异常、Error Assistant、Duplicate/Orphan、Serializer 防线、Durable Cancelled Operation 和下一 Prompt 自动修复。

```text
Ran 167 tests
OK
```

---

## 34. Approval Resume 崩溃窗口修复

### 34.1 Durable 锚点

恢复扫描现在从 `approval_resume_registered` 开始，而不是从 `approval_resume_started` 开始。Registered 在 Grant/Consume 之前已经持久化，包含 Approval ID、Action 和 Resume Payload。

### 34.2 幂等状态推进

```text
waiting  → Grant → Consume → Started → Resume
approved → Consume → Started → Resume
consumed → Started → Resume
started 未完成 → Resume
completed → 返回持久结果，不重复 Resume
```

`approve_and_resume()` 不再每次固定从 `grant()` 开始。

### 34.3 恢复扫描

```python
await coordinator.recover_incomplete(
    resume_callback,
    consumer_resolver=resolve_verified_consumer,
)
```

```text
Registered + waiting  → 等待人工审批
Registered + approved → 解析可信 Consumer、Consume、补 Started、Resume
Registered + consumed → 补 Started、Resume
Started               → Resume
Rejected/Expired      → approval_resume_cancelled
Completed/Failed      → 跳过
```

Approved 状态缺少可信 Consumer Resolver 时不会猜测身份，也不会丢失；它保留在 `pending_recovery_ids()` 中等待 Adapter。

### 34.4 并发 Claim

同一 Coordinator 中，两个协程恢复同一 Approval ID 时只创建一个共享 Resume Task。

SQLite Store 进一步使用持久 Lease Claim，让同一台机器上的多个进程/Coordinator 不能同时恢复同一 Approval 或 Operation。跨机器部署仍需 PostgreSQL 等网络数据库。

### 34.5 Completed 幂等返回

Completed Event 保存可 JSON 序列化结果。重复调用直接读取原结果，不再次执行 Resume Callback。

不可序列化结果只保存完成标记，防止为了返回值重复副作用。

### 34.6 Host 启动检查

`DurableAgentHost` 新增：

```text
pending_approval_resumes
recovered_approval_resumes
```

创建时可以注入：

```python
approval_resume_handler=...
approval_consumer_resolver=...
```

有 Adapter 时自动恢复；没有可信 Consumer/Handler 时明确暴露 Pending ID，不会永久隐身。

普通 StartupRecovery 不会把 Approval Pending 写工具误当成普通 `execute_tool`。

### 34.7 Write Idempotency

如果写 Handler 已成功但 Completed Event 前崩溃，Resume 可能再次进入 Handler。因此仍必须结合：

```text
Idempotency Key
WriteOperationService
Outcome Reconciliation
```

Approval Resume 幂等解决流程卡死，业务 Idempotency 解决重复副作用。

### 34.8 测试

```bat
python -m unittest tests.test_approval_resume_windows -v
python -m unittest discover -s tests -v
```

覆盖 Waiting、Granted、Consumed、Started、Completed、Rejected、并发 Resume、缺少 Consumer 和副作用 Idempotency。

```text
Ran 167 tests
OK
```

---

## 35. Approval/Write 感知的 Operation Recovery

### 35.1 OperationState 正式投影

```python
OperationState.approvals: dict[str, ApprovalSnapshot]
OperationState.writes: dict[str, WriteSnapshot]
```

Approval 状态：

```text
waiting/approved/consumed/resume_started/resume_completed/
resume_failed/rejected/expired/resume_cancelled
```

Write 状态：

```text
prepared/waiting_approval/approved/submitting/
succeeded/failed/outcome_unknown/reconciling
```

Operation Phase 会投影为 `waiting_approval`、`ready_to_resume`、`executing_write` 或 `outcome_unknown`。

### 35.2 Recovery Planner 优先级

```text
先检查 Write/Approval
→ 再检查 Pending Model Request
→ 最后检查普通 Tool Call
```

新增：

```text
wait_for_approval
consume_approval
resume_approved_write
reconcile_write
finalize_rejected_approval
```

Waiting Approval 不再产生 `execute_tool`。

### 35.3 Approval/Tool Invariant

- Waiting/Approved Approval 禁止 Tool Dispatch；
- Registered Action Hash 必须匹配 Approval Request；
- Tool Name/Arguments 必须匹配 Approval Action；
- Approval 未 Consumed 不能 Resume Started；
- Operation Completed 前不得存在未完成 Approval Resume；
- Operation Completed 前 Write 必须进入 Succeeded/Failed。

### 35.4 Rejected/Expired

```text
生成 approval_not_granted Synthetic ToolResult
→ 写 approval_resume_cancelled
→ 使用完整 Context 继续模型说明未执行
→ Operation Completed
```

不会执行写工具。

### 35.5 Outcome Unknown

Write 为 `submitting/outcome_unknown/reconciling` 时，Planner 优先返回 `reconcile_write`，不会因为缺少 ToolResult 而重放写工具。

### 35.6 Startup Report

```python
StartupRecoveryReport.waiting_approval
```

等待审批或缺少可信 Consumer/Write Runtime 的 Operation 会进入该分类，不会被标记 Failed，也不会进入普通 Tool Recovery。

### 35.7 测试

```bat
python -m unittest tests.test_approval_aware_recovery -v
python -m unittest discover -s tests -v
```

覆盖 Waiting、Approved、Consumed、Rejected、OutcomeUnknown、Action Hash、Dispatch Invariant、Startup Report 和 Direct Recovery 防绕过。

```text
Ran 189 tests
OK
```

---

## 36. Model Request Policy 持久化与 Approval 最终响应闭合

### 36.1 每次请求独立保存策略

新增：

```python
ModelRequestPolicy
```

每个 `model_request_started` 保存：

```text
visibleToolNames
toolChoice
requiredCapabilities
allowedToolNames
expectedToolArguments
continuationPolicy
```

策略按 Model Request/Turn 保存，不能只在 Operation 级保存，因为 Required Tool 完成后的下一轮通常会切换为 Auto 或 None。

### 36.2 Recovery 禁止扩大权限

`RecoveryAction.request_policy` 会把持久策略传给 `RecoverableModelRuntime`。Runtime 只装配 `visibleToolNames` 中的工具，并恢复原始 Tool Choice、Capability、Allowed Tool 和 Expected Arguments。

```text
策略缺失
→ manual_intervention
→ 禁止“全部工具 + tool_choice=auto”兜底

当前 Runtime 缺少策略要求的工具
→ ModelRequestPolicyError
→ 禁止静默缩减或替换工具
```

### 36.3 Continuation Policy

Router 在初始 Required/Named 请求前同时持久化工具完成后的 Continuation Policy。这样即使崩溃发生在 Tool Dispatch/Result 窗口，Recovery 也知道下一次模型请求应使用什么策略。

```text
初始请求：
visible=[get_order_status]
tool_choice=required
expected={order_id: 1001}

工具完成后：
visible=[get_order_status]
tool_choice=auto
expected={}
```

### 36.4 Approval 后只允许最终文本

Approval Write 成功后的说明请求固定使用：

```text
tools=[]
tool_choice=none
```

如果模型仍返回 Tool Call 或没有正常 `stop`：

```text
model_request_failed
approval_resume_failed
operation_finished(failed)
```

不会执行新 Tool Call，也不会写入带未闭合 Tool Call 的 Completed Operation。

### 36.5 完成前预验证

`DurableOperationRecorder.finish_operation()`、Host 和 Recovery 在写 `operation_finished` 前都会：

```text
重放当前 Operation
→ 纯函数应用候选 Finished Event
→ Transcript Closure/Approval/Write Invariant
→ expected_last_sequence CAS 追加
```

---

## 37. SQLite 单机事务 Store

### 37.1 默认后端

`DurableAgentHost.create()` 默认：

```python
store_backend="journal"
```

状态文件：

```text
<state_dir>/agent-state.sqlite3
```

兼容旧 JSONL（仅普通 Agent/只读或离线示例）：

```python
store_backend="jsonl"
```

`journal` 后端把 Runtime、Operation、Retry、Approval 和 Write 事实写进同一条加密时间线。旧 `sqlite` 和 `jsonl` 只作为兼容入口；JSONL 不保证崩溃时批次原子性，因此 DurableAgentHost 会拒绝在 JSONL Backend 上启动 Approval 写请求。

### 37.2 Store 类型

```python
SQLiteOperationEventStore
SQLiteRuntimeEventStore
```

两者共用一个 SQLite 文件。Operation Store 使用：

```text
WAL
synchronous=FULL
busy_timeout
BEGIN IMMEDIATE
Event Batch Transaction
Operation expected_last_sequence CAS
```

### 37.3 唯一约束

SQLite Schema 强制：

```text
Approval Request approval_id 唯一
Write Prepared write_id 唯一
Write idempotency_key_hash 唯一
Event sequence 单调唯一
Claim (claim_type, resource_id) 唯一
```

### 37.4 原子状态转换

Approval Request 与 `approval_resume_registered` 也在同一批事务中提交，避免只留下不可恢复的 Waiting Approval。

Approval 的 Grant/Reject/Consume 和 Write 的 Prepare/Claim/Finalize 都采用：

```text
读取当前 Operation Version
→ 纯函数验证转换
→ BEGIN IMMEDIATE
→ 检查 expected_last_sequence
→ 批量追加 Event
→ COMMIT
```

冲突方必须重新读取状态，不能使用旧的 Waiting/Approved 快照继续执行。

### 37.5 Write Claim 边界

对于带 Approval 和 Tool Call 的 Write，下列事件在同一事务提交：

```text
approval_consumed
write_approved
tool_dispatch_started
write_submitting
```

事务提交后只有获胜 Worker 调用外部 Handler。外部调用不放进长 SQLite 事务；它继续依赖 Idempotency Key 和 Outcome Reconciliation。

### 37.6 跨进程 Lease

SQLite `operation_claims` 防止同机多个进程同时执行：

```text
approval_resume
operation_recovery
```

进程崩溃后 Lease 到期可由新 Worker 接管。

### 37.7 测试

新增 `tests/test_sqlite_transactional_store.py`，覆盖：

1. Operation/Runtime 重启持久化；
2. 两个 Store 并发 Grant 只有一个获胜；
3. 两个 Store 并发 Execute 外部 Handler 只执行一次；
4. 跨 Operation Idempotency Key 唯一去重；
5. Approval Consume/Write Claim/Tool Dispatch 批量事务；
6. 两个 Coordinator 的 Approval Resume 互斥；
7. 跨 Store Lease Claim 互斥。

全部测试：

```text
Ran 189 tests
OK
```

---

## 38. Durable P0 崩溃窗口统一修复

### 38.1 初始安全事务

Approval 写请求不再先写 Assistant Tool Call、后建 Approval。Host 会先生成：

```python
DurableActionEnvelope(
    operation_id=...,
    tool_call_id=...,
    tool_name=...,
    arguments=...,
    write_id=...,
)
```

然后把以下事实作为一个 `append_batch()` 事务提交：

```text
User Message
Model Request Started/Completed
Continuation Policy
Approval Requested/Resume Registered
Write Prepared/Waiting Approval
Tool Intent Recorded
```

因此只能观察到“全部不存在”或“全部存在”。`waiting_approval` 却没有 Approval/Write 的旧损坏状态会进入 Manual Intervention，绝不执行 Tool。

### 38.2 Envelope 端到端绑定

同一个 Envelope 同时用于：

```text
Approval Action Hash
Resume Payload
Operation Reducer
Write Action Hash
Tool Intent
Write Execute
```

Reducer 和 Host 都校验 Operation ID、Tool Call ID、Tool Name、精确 Arguments 和 Write ID。`read_order` Approval 不能再执行 `refund_order` Payload。

### 38.3 Intent 与结果物化幂等

Tool Intent 在初始事务中创建，Resume 只复用。Write 成功后的 `tool_completed + ToolResult Message` 使用 CAS 幂等物化：

```text
Intent 已存在 → 不重复写
Tool Completed 已存在 → 不重复写
ToolResult Message 已存在 → 不重复写
```

### 38.4 Cancellation 与 Commit Boundary

外部：

```python
prompt_task.cancel()
```

现在会先：

```text
取消嵌套工具 Task
补齐 Synthetic ToolResult
闭合 Transcript
写 Operation Cancelled
```

完成后才向调用方重新抛出 `CancelledError`。

`tool_execution_end` 被定义为副作用 Commit Boundary。Listener 在该事件失败时只记录 `listener_errors`，不会取消已经提交的兄弟工具，也不会把真实成功伪造成“工具未执行”。Recorder 会在 ToolResult Message 阶段补齐缺失的 Tool Completed Event。

### 38.5 Approval/Write 联合恢复

`write.state=waiting_approval` 不再单独决定结果：

```text
Approval waiting  → wait_for_approval
Approval approved → consume_approval / ready_to_resume
Approval consumed → resume_approved_write
Approval rejected/expired → finalize_rejected_approval
Approval missing → manual_intervention
```

Startup Report 新增 `ready_to_resume`，已批准操作不会继续显示为等待经理审批。

### 38.6 Reconciliation 可重入

Reconciliation 使用 `write_reconcile` Lease Claim：

```text
outcome_unknown → reconciling → succeeded/failed
Handler 异常 → write_reconcile_failed → outcome_unknown
进程在 reconciling 崩溃 → Lease 获胜者可重入
```

### 38.7 Policy、Length 与精确参数

```text
model_request_started 缺 requestPolicy
→ policy=None
→ active policy 清空
→ manual_intervention
```

不会继承旧的全部工具/Auto 策略。

普通 Recovery 遇到 `stopReason=length` 会失败，不会完成 Operation。

存在 Expected Arguments 时必须满足：

```text
恰好一个 Tool Call
Arguments 与持久快照完整相等
没有额外参数
不能跨多个调用拼凑字段
```

### 38.8 Approval 完整事务校验

Approval Request、Grant、Reject、Consume、Expire 都先通过完整 Operation Reducer。终态 Operation 不能继续追加 Approval Event。

Grant/Reject 的 TTL 使用 Store `deadline_ms` 在 `BEGIN IMMEDIATE` 事务内最终检查，不能跨过过期时间后再批准。

### 38.9 P0 测试

新增 `tests/test_p0_durable_boundaries.py`，覆盖 11 类确定性场景：

1. Approval 缺失窗口禁止 Execute Tool；
2. Host 原子持久化 Envelope/Approval/Write/Intent；
3. Resume Payload 篡改拒绝；
4. 外部 Task Cancel 后 Transcript/Operation 闭合；
5. Approved Write 不再规划 Waiting；
6. Reconciliation 异常后可重试；
7. 缺失 Request Policy 不继承旧策略；
8. 终态 Approval 与事务 TTL；
9. Listener 失败不伪造副作用结果；
10. 普通 Recovery 拒绝 Length；
11. Expected Arguments 单调用精确匹配。

全部项目：

```text
Ran 227 tests
OK
```

---

## 39. P0 完成审计补强

本轮在第 38 节基础上继续关闭了重入和绕过窗口：

- Public `ApprovalResumeCoordinator` 必须在一个事务中创建 Assistant Call、Write、Approval、Resume 和 Tool Intent，并拒绝非原子 Store；
- Operation Reducer 逐步校验 Approval、Write、Tool Call、Tool Intent 和唯一 ToolResult，不能靠伪造 Event 绕过；
- Durable Action 与 Expected Arguments 按 JSON 类型递归精确比较，`true`、`1`、`1.0` 不再互相等价；
- Approval Resume、Operation Recovery 和 Write Reconciliation 的长回调均持续续租，并在提交前再次确认 Claim；
- Write Handler 被取消会进入 `outcome_unknown`，只能通过 Reconciliation 确认结果；
- Approval 后最终 Model Request 被取消时复用同一个 Pending Request；若 Completed 已落盘则直接复用响应；
- Recovery Tool Callback 必须返回当前计划的 Tool Call ID、Tool Name 和合法 ToolResult，不能替换目标调用；
- 完整历史 Context、低层 Transcript Repair 和 Tool Commit Boundary 均同步到调用方与 Durable Journal。

专项测试位于：

```text
tests/test_p0_approval_atomicity.py
tests/test_p0_invariant_bindings.py
tests/test_p0_recovery_policy.py
tests/test_p0_session_recovery_lease.py
tests/test_p0_transcript_boundaries.py
```

---

## 40. 生产化 Harness 能力

本轮把原先分散或仅用于教学的能力收敛到统一运行边界：

- `SQLiteSessionEventJournal`：Runtime、Operation、Retry、Approval、Write 共用加密事件表，提供租户隔离、角色访问控制、脱敏读取、保留策略、导出、删除、审计、完整性校验、Snapshot 和版本迁移；
- `ToolDispatchRuntime`：普通 Agent 与 Recovery 共用工具校验、Hook、Timeout、Retry、取消、读写资源锁、公平/优先级队列、租户限流、跨进程 Lease 和 Telemetry；
- `ModelCallRuntime`：普通调用、Router 与 Recovery 共用 Provider、Retry、Circuit、Compaction、取消、Usage/Cost、Durable Event 和 Telemetry；
- `DurableAgentHost`：保留 Facade，对象创建、资源生命周期、恢复和审批分别由 `harness/factory.py`、`resources.py`、`lifecycle.py`、`recovery.py`、`approval.py` 负责；
- `TokenAwareStructuredCompactor`：按 Token 预算保留近期消息，把早期 Tool/Approval/业务事实生成可校验的结构化摘要，并记录 Replacement Event；
- `RouterEvaluator`：提供 Intent 数据集、Status + Intent 联合混淆矩阵、精确到工具名的 Required Tool 漏检、置信度校准、恶意输入、真实 Hybrid Router Usage/Cost/延迟和不可用 NaN 绕过的版本回归门禁；
- `HybridRequestPlanner` 与 `PlanExecutor`：提供 Plan 校验、依赖图、Task 状态机、审批屏障、并行执行、恢复策略和结果合成；`DurableAgentHost.plan()`、`execute_plan()`、`resume_plan()` 已把它们接到统一 Session Journal，Plan/Event 绑定 Session + Plan ID，追加使用 CAS，同 Plan 跨 Worker 执行使用可续租 Lease，重启后从事件流重放。

生产边界也已补齐：

- Journal 完整性清单同时覆盖 Event、Snapshot 和元数据；旧版无完整性清单的数据库默认拒绝打开，只能由受控迁移程序显式设置 `allow_legacy_integrity_bootstrap=True` 完成一次性升级；
- SQLite 等同步持久化调用使用可排空的线程边界，调用方即使连续取消，Host 也会等待正在提交的事务结束，避免后台线程在资源关闭后继续写库；
- `ModelCallRuntime` 和 `OpenAICompatibleProvider` 都会停止接收新请求、取消并排空在途请求，再由 Host 统一关闭共享 `httpx.AsyncClient` 连接池；
- Host 可通过 `model_retry_policy` 和 `model_circuit_breaker` 为普通 Agent、Router 和 Recovery 配置同一套模型重试与熔断策略；
- EventStream 使用条数和字节双重有界队列，更新事件可合并，终止事件受保护，慢消费者不会让内存无限增长；
- Telemetry 提供脱敏的 Metrics、Trace、结构化日志和 Alert，覆盖模型/工具延迟、Token、费用、重试、熔断、审批等待、恢复、资源锁和队列压力。

专项测试：

```bat
python -m unittest tests.test_unified_session_journal -v
python -m unittest tests.test_tool_dispatch_runtime -v
python -m unittest tests.test_p2_runtime_features -v
python -m unittest tests.test_p2_host_integration -v
python -m unittest tests.test_p2_context_compaction -v
python -m unittest tests.test_p2_router_evaluation -v
python -m unittest tests.test_p2_multi_intent_planning -v
python -m unittest tests.test_p2_durable_planning -v
python -m unittest tests.test_durable_to_thread -v
```
