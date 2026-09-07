# 对话复习记录

> 创建日期：2026-08-25  
> 用途：保存用户问题和助手最终回答，方便后续复习。  
> 记录规则：只记录“用户问题”和“助手回答”，不记录分析过程、思考过程、工具调用和中间输出。  
> 后续规则：从本文件创建后开始，每轮对话都继续追加到本文件末尾。

---

## 对话 1：DeepSeek Harness 全部源码研究

> 说明：本轮发生在会话压缩前，以下内容根据当前会话中保留的摘要恢复，不是逐字转录。

### 用户问题

研究 `https://github.com/deepseek-ai/deepseek-harness` 全部源码，将技术分析持续写入 Markdown 文档，并在技术版后追加一份通俗易懂的源码讲解。

### 助手回答

已完成 DeepSeek Harness 全仓包级静态研究。

研究结果保存至：

`D:/0715/git_codex/20260825/deepseek-harness-source-study.md`

研究版本：

- Tag：`dsh-v0.1.1-rc.2`
- Commit：`b150a551b8d465e31e418e1b2eaf5e79bbb7d28e`

文档覆盖：

- 全部 227 个工作区包；
- CLI/Profile、Cordis、Agent Loop、Session、Tools、LLM；
- DeepSeek Adapter、Persistence、Compaction、Sandbox；
- Web、Subagent、Workflow、SDK、ACP、MCP、Hooks；
- Terminal、UI、Native、Python、构建、发布和测试体系；
- 技术版、逐包清单、通俗版、完整任务示例和术语表。

源码仓库保持干净。研究属于静态分析，尚未安装依赖、运行构建和测试。

---

## 对话 2：Pi 全部源码研究

### 用户问题

沿用此前解析 DeepSeek Harness 源码并写入 Markdown 的方式，解析 `https://github.com/earendil-works/pi` 全部源码，并保存到新的 Markdown 文档中。

### 助手回答

已完成 Pi 全仓源码静态研究，文档保存至：

`D:/0715/git_codex/20260825/pi-source-study.md`

文档包含：

- 研究版本与规模统计
- 整体架构及关键调用链
- AI Provider、Agent Loop、工具、会话、扩展、TUI
- Protocol、Client、Server、SQLite、Telemetry、Evals
- 540 个生产 TypeScript 文件及 SQL migration 全量清单
- 467 个测试文件及示例、脚本、原生代码清单
- 技术版与白话版讲解
- 术语表和动态验证建议
- 对“成熟旧主链与未完成的新 AgentHarness 主链并存”的专项分析

文档约 3,080 行、173 KB，Markdown 围栏检查正常。`pi/` 源码仓库保持干净。由于尚未安装依赖，构建和测试未动态执行。

---

## 对话 3：比较 DeepSeek Harness 与 Pi

### 用户问题

分析这两个源码的优缺点，并且假如我要写一个 agent，有哪些是可以值得借鉴的，写到一个新的 md 文件里。

### 助手回答

已完成对 DeepSeek Harness 与 Pi 的优缺点比较及 Agent 设计借鉴分析，保存至：

`D:/0715/git_codex/20260825/deepseek-harness-vs-pi-agent-design-study.md`

文档约 2,098 行，主要包括：

- 两个项目的架构定位和优缺点
- Agent Loop、Provider、工具、持久化、安全、扩展、UI、远程协议等维度对比
- 值得优先借鉴与不应过早照搬的设计
- 推荐的混合 Agent 架构
- TypeScript 核心接口草图与 Agent Loop 伪代码
- 会话恢复、CAS、原子写、审批、沙箱和重试建议
- 分阶段实现路线
- 个人 CLI、企业平台、多租户 Web、SDK 等场景的选型建议
- 开发前最终检查清单

核心建议是：**以 Pi 的简洁 Agent Loop、Provider 层和工具体验为骨架，补充 DeepSeek Harness 的持久 Inbox、事件溯源、CAS、审批、安全策略和多客户端恢复能力。**

两个源码仓库均保持干净。

---

## 对话 4：追加专业名词和英文解释

### 用户问题

文档内容中有很多专业名词和英文看不懂，能追加到文档专业名词和英文的解释吗？

### 助手回答

已追加到原对比文档：

`D:/0715/git_codex/20260825/deepseek-harness-vs-pi-agent-design-study.md`

新增“第 23 节：专业名词与英文解释”，约 560 行，覆盖：

- Agent、Provider、Tool、Session 等核心概念
- 模型、流式传输、认证和网络术语
- CAS、原子写、沙箱、审批等安全术语
- Cordis、插件、生命周期和架构术语
- TUI、终端、远程协议与数据库术语
- 测试和常见开发缩写
- 文中常见英文短语的整句白话翻译
- 推荐的术语学习顺序

文档现约 2,665 行、94 KB，Markdown 围栏检查正常。

---

## 对话 5：分析 Pi Agent Loop

### 用户问题

帮我分析 Pi 的 Agent Loop。

### 助手回答

已完成 Pi Agent Loop 专项分析，并追加到：

`D:/0715/git_codex/20260825/pi-source-study.md`

新增“第 29 节：Agent Loop 专项深读”，主要结论如下。

### 核心分为三层

1. `agent-loop.ts`：模型请求、工具执行和消息队列。
2. `agent.ts`：运行状态、取消、监听器、steering/follow-up。
3. `agent-session.ts`：持久化、扩展、重试、压缩和最终结算。

### 核心循环

```text
读取待处理消息
→ 请求模型
→ 流式接收文本/思考/工具调用
→ 执行工具
→ 把工具结果交回模型
→ 检查 steering
→ 检查 follow-up
→ 结束
```

### 重要设计

- 多工具默认并行执行，但结果按模型原始顺序回灌。
- 被 token 上限截断的工具参数绝不执行。
- steering 用于当前任务中途纠偏。
- follow-up 在原任务结束后执行。
- API key 每次请求前重新获取，支持 OAuth 刷新。
- `agent_end` 监听器全部完成后才真正 idle。
- 自动重试和压缩放在低层循环之外，由 `AgentSession` 编排。

### 优点

- 主循环短小、可读。
- Provider 和产品 UI 解耦。
- 工具事件与顺序定义清楚。
- 支持自定义消息和动态工具。
- 取消与运行结算边界明确。

### 不足

- 没有最大轮次、工具数、时间或费用预算。
- 工具取消依赖工具主动响应 `AbortSignal`。
- 并行工具没有并发数量限制。
- 高频工具 update 可能积累大量 Promise。
- EventStream 没有容量限制和背压。
- listener 异常缺少隔离。
- steering/follow-up 队列没有持久化。
- 当前循环本身不支持崩溃后恢复运行。

文档现约 3,741 行、193 KB，Markdown 检查正常。

---

## 对话 6：未完成的简短输入

### 用户问题

接

### 助手回答

请继续说明要接着分析或追加哪一部分。

---

## 对话 7：追加 Agent Loop 通俗逐句对照

### 用户问题

再加一个通俗易懂的对照组，在每个句子结束的下面加。

### 助手回答

已在 `pi-source-study.md` 中追加：

## 第 30 节：Agent Loop 技术句与白话句逐句对照

采用以下格式：

```markdown
**技术句：** 并行工具按 completion order 发出结束事件。

> **白话句：** 哪个工具先做完，界面就先显示哪个工具完成。
```

共增加 121 组逐句对照，覆盖：

- Agent Loop 三层结构
- `prompt()` 与 `continue()`
- 双层循环
- Turn 和 Step
- 模型流式响应
- 工具预检、串行和并行执行
- Steering 与 Follow-up
- 事件和状态更新
- 自动重试与压缩
- 主要优点、不足和改进建议

文档路径：

`D:/0715/git_codex/20260825/pi-source-study.md`

现约 4,282 行、219 KB，Markdown 检查正常。

---

## 对话 8：创建 Python Agent Loop 教学项目

### 用户问题

创建 agent loop 文件夹，按照 Pi 的 Agent Loop 将其改造成 Python 代码，并且给出详细的注释以及 README.md 说明，要求注释和 README.md 是中文。

### 助手回答

已创建 Python 版 Agent Loop：

`D:/0715/git_codex/20260825/agent-loop`

主要文件：

- `README.md`：中文使用与架构说明
- `src/pi_agent_loop/loop.py`：Pi Agent Loop 核心 Python 改写
- `src/pi_agent_loop/agent.py`：有状态 Agent 封装
- `event_stream.py`：异步事件流
- `cancellation.py`：合作式取消
- `types.py`：模型、工具、Hook 等类型
- `testing.py`：确定性 `ScriptedProvider`
- `examples/basic_usage.py`：并行工具完整示例
- `tests/test_agent_loop.py`：6 项核心行为测试

已实现：

- `prompt()` 和 `continue_run()`
- Turn 双层循环
- Steering 与 Follow-up
- 模型流式事件
- 工具参数校验
- 串行及并行工具
- 完成顺序事件与来源顺序结果
- Before/After Tool Hook
- 工具流式更新
- Abort 与 `wait_for_idle()`
- 截断工具参数拒绝执行
- Fake Provider 测试

验证结果：

```text
Ran 6 tests
OK
```

运行方式：

```bash
cd agent-loop
python examples/basic_usage.py
python -m unittest discover -s tests -v
```

代码注释和 `README.md` 均为中文，项目仅使用 Python 标准库，要求 Python 3.11 以上。

---

## 对话 9：看不懂 Agent Loop 示例输出

### 用户问题

运行之后没看懂：

```text
事件：agent_start
事件：turn_start
事件：message_start
事件：message_end
...
最终回答：加法结果是 5，乘法结果是 20。
模型调用次数：2
```

测试运行后六项都是 `ok`，这些输出是什么意思？

### 助手回答

你看到的输出其实包含两部分：

### 为什么模型调用了两次？

```text
第 1 次调用模型
→ 模型要求执行 add 和 multiply

执行两个工具
→ add 返回 5
→ multiply 返回 20

第 2 次调用模型
→ 模型读取工具结果
→ 生成最终回答
```

多次出现 `message_start/message_end`，是因为用户输入、模型回复、两个工具结果、最终回答都分别属于一条消息。

### 测试输出是什么意思？

```text
... ok
Ran 6 tests
OK
```

表示六项自动测试全部通过，并不是又运行了六次 Agent。

我已经改进示例：

1. 新增最简单的纯文本示例：

```bat
python examples\minimal_text.py
```

2. 原示例现在会把每个事件直接翻译成中文：

```bat
python examples\basic_usage.py
```

新输出类似：

```text
[01] Agent 开始处理用户任务。
[02] 开始第 1 轮：准备请求一次模型。
[06] 模型要求调用工具：add、multiply。
[10] 工具 multiply 执行成功，结果为 20。
[11] 工具 add 执行成功，结果为 5。
[17] 开始第 2 轮：准备请求一次模型。
[19] 模型回复完成：加法结果是 5，乘法结果是 20。
[21] 没有更多工作，Agent 结束本次运行。
```

`README.md` 也增加了运行结果和所有事件名称的中文解释。测试仍全部通过。

---

## 对话 10：Agent Loop 之后学习什么

### 用户问题

目前已经完成了 agent-loop 的开发教学并且测试通过了，我下一步该学习什么呢？

### 助手回答

下一步最适合学习 **模型 Provider 层（模型适配器）**。

你现在的 Agent Loop 已经会：

```text
请求模型 → 执行工具 → 返回工具结果 → 再次请求模型
```

但目前使用的是 `ScriptedProvider` 假模型。下一步应让它真正连接 DeepSeek 或 OpenAI。

## 推荐学习顺序

```text
1. Agent Loop                 已完成
2. 模型 Provider              下一步
3. 内置工具系统
4. Session 持久化
5. 重试、预算和上下文压缩
6. CLI 交互程序
7. 扩展系统
8. TUI/Web/远程服务
```

---

## 第二课：模型 Provider

建议先实现一个 `DeepSeekProvider`，因为 DeepSeek API 与 OpenAI Chat Completions 格式接近。

需要学习：

1. 将内部消息转换成 DeepSeek 请求格式；
2. 将 Python 工具转换成模型可识别的工具定义；
3. 接收流式文字；
4. 拼接分段到达的工具参数；
5. 转换停止原因；
6. 统计 Token 用量；
7. 处理网络错误、超时和取消；
8. 将结果转换成 `AssistantMessageEventStream`。

建议目录：

```text
src/pi_agent_loop/providers/
├─ __init__.py
├─ base.py
├─ deepseek.py
├─ message_converter.py
├─ stream_parser.py
└─ errors.py
```

### 完成标准

至少通过以下场景：

```text
用户：计算 12 × 8
→ DeepSeek 返回 multiply 工具调用
→ Agent Loop 执行工具，得到 96
→ 工具结果交回 DeepSeek
→ DeepSeek 最终回答“结果是 96”
```

测试还应覆盖：

- 普通文字回答；
- 一个工具调用；
- 多个并行工具；
- 工具参数分段到达；
- API 返回错误；
- 网络中途断开；
- 用户取消；
- Token 用量解析。

单元测试继续使用假 HTTP 或 `ScriptedProvider`，真实网络测试应单独运行。

---

## 第三课：真实工具系统

Provider 接通后，再实现：

1. `read`：读取文件并限制行数和大小；
2. `write`：创建或完整覆盖文件；
3. `edit`：精确替换文本；
4. `shell`：运行命令、超时、取消和输出截断。

安全上必须增加：

- 工作目录边界；
- 路径规范化；
- 同一文件修改队列；
- 文件版本检查；
- 原子写入；
- Shell 进程树清理；
- 大输出保存到临时文件。

---

## 第四课：Session 持久化

不要直接把 Agent 的内存对象序列化。建议学习 append-only JSONL：

```text
session_created
user_message
assistant_message
tool_started
tool_result
turn_finished
```

目标是程序重启后能够：

- 恢复历史消息；
- 恢复模型和工具设置；
- 找到未处理的用户消息；
- 判断上一次是否执行到一半；
- 避免重复执行有副作用的工具。

---

## 第五课：可靠性

然后补充：

- 最大 Turn 数；
- 最大工具调用数；
- 最长运行时间；
- Token 和费用上限；
- Provider 网络重试；
- Agent 整轮重试；
- Context Overflow；
- Compaction 上下文压缩；
- 崩溃恢复。

---

## 暂时不要优先学习

目前不建议马上做：

- TUI；
- Web；
- 插件热重载；
- Subagent；
- MCP；
- 多租户；
- 分布式执行。

这些会掩盖 Agent 最核心的模型、工具和持久化问题。

## 最终建议

你的下一项开发任务可以命名为：

> **第二课：将 Agent Loop 接入真实 DeepSeek 模型**

完成后，你将第一次拥有真正可用的：

```text
真实模型 + Agent Loop + Python 工具
```

这是从教学 Demo 走向完整 Agent 的关键一步。

---

## 对话 11：创建持续对话复习文档

### 用户问题

以后将每次我们俩的对话都写到 md 文档里面，方便我后面进行复习。只要我的问题、你的回答，不要思考过程。先将之前的对话写到 md 文档。

### 助手回答

已创建持续对话复习文档：

`D:/0715/git_codex/20260825/conversation-review.md`

已经将当前能够恢复的历史对话整理进去，只包含：

- 用户问题；
- 助手最终回答。

没有写入：

- 分析过程；
- 思考过程；
- 工具调用；
- 中间输出。

会话压缩前无法逐字恢复的早期 DeepSeek Harness 对话，已经明确标注为“根据会话摘要恢复”。

从下一轮开始，每次对话都会继续追加到这个文件末尾。

---

## 对话 12：内置工具系统架构设计

### 用户问题

我要先写内置工具系统，先给我架构设计，后续根据我的要求进行代码编写。

### 助手回答

已完成内置工具系统架构设计，目前只编写了设计文档，没有开始实现工具代码。

设计文档：

`D:/0715/git_codex/20260825/agent-loop/BUILTIN_TOOLS_DESIGN.md`

推荐架构：

```text
Agent Loop
   ↓ 只认识 AgentTool
Tool Factory / Registry
   ↓ 创建并注册工具
Built-in Tools
   ↓ 调用共享能力
Tool Services
   ├─ WorkspacePathPolicy     工作区路径安全
   ├─ FileObservationStore   文件版本观察
   ├─ FileMutationQueue      同文件修改排队
   ├─ AtomicFileWriter       原子写入
   ├─ ProcessRunner          Shell 进程管理
   └─ OutputAccumulator      输出截断与完整输出保存
```

第一批推荐工具：

- `read`
- `list_dir`
- `write`
- `edit`
- `shell`

第二批工具：

- `find`
- `grep`

推荐开发顺序：

1. 公共基础设施：错误、路径策略、文件观察、修改队列、原子写；
2. 只读工具：`read`、`list_dir`；
3. 修改工具：`write`、`edit`；
4. 输出系统和 `shell`；
5. 搜索工具：`find`、`grep`；
6. Registry、工具 Profile、README 和完整集成测试。

推荐默认安全规则：

- 文件工具只能访问 workspace；
- `write/edit` 修改已有文件前必须先 `read`；
- 同一个文件的修改必须串行；
- 写入使用临时文件原子替换；
- `shell` 默认关闭；
- 输出限制为 2,000 行或 50 KiB；
- 超大完整输出保存到临时文件。

正式编写代码前，需要根据你的要求确认：

1. 是否允许访问 workspace 外文件；
2. `shell` 是否默认启用；
3. 是否接受“先 read、后修改”的严格规则；
4. 是否允许使用 Pydantic 或 `jsonschema`；
5. 是否第一版就完整支持 Linux/macOS；
6. 是否按推荐工具顺序开发。

后续我会根据你的具体要求分阶段编写代码，每个阶段先写测试，再写实现，测试通过后再进入下一阶段。

---

## 对话 13：编写加法、乘法工具并讲解注册

### 用户问题

目前我的要求是按照之前的测试去改。之前的测试要求同时计算 `2+3`、`4×5`，我就写加法工具、乘法工具。

前五个事件保持一样：

```text
[01] Agent 开始处理用户任务。
[02] 开始第 1 轮：准备请求一次模型。
[03] 开始接收用户消息：请同时计算 2+3 和 4×5
[04] 用户消息已经加入本次模型上下文。
[05] 模型开始生成一条回复。
```

后面调用我写的工具：

1. 创建 `tools` 编写工具；
2. 讲解如何注册工具。

### 助手回答

已按照要求完成第一组内置教学工具，并保持示例前五个事件不变。

新增目录：

```text
agent-loop/src/pi_agent_loop/tools/
├─ __init__.py
├─ validators.py
├─ add.py
├─ multiply.py
└─ registry.py
```

实现内容：

- `create_add_tool()`：加法工具，执行 `a + b`；
- `create_multiply_tool()`：乘法工具，执行 `a * b`；
- `validate_two_numbers()`：统一校验 `a`、`b`；
- `ToolRegistry`：注册工具、查询工具和防止工具重名；
- `create_calculator_registry()`：快速创建已注册加法、乘法工具的注册表。

工具注册分三步：

```python
registry = ToolRegistry()
registry.register(create_add_tool())
registry.register(create_multiply_tool())

agent = Agent(
    model=model,
    stream_fn=provider.stream,
    tools=registry.all(),
)
```

完整调用关系：

```text
创建工具
→ 注册到 ToolRegistry
→ registry.all() 交给 Agent
→ 假模型返回 name=add/multiply 的 toolCall
→ Agent Loop 按名称找到工具
→ 执行真正的 Python 加法、乘法函数
→ 结果 5、20 写回模型上下文
→ 第二次模型调用生成最终回答
```

已修改：

- `examples/basic_usage.py`：不再在示例内部定义工具，改为从 `tools` 目录创建并注册；
- `README.md`：增加“加法工具、乘法工具和注册流程教学”；
- `BUILTIN_TOOLS_DESIGN.md`：更新当前实现进度；
- `tests/test_calculator_tools.py`：增加 5 项工具与注册测试。

现在共有 11 项测试，验证结果：

```text
Ran 11 tests
OK
```

运行示例：

```bat
cd D:\0715\git_codex\20260825\agent-loop
python examples\basic_usage.py
```

需要特别说明：当前仍使用 `ScriptedProvider` 假模型，所以第一次返回的 `add`、`multiply` 调用是预先写好的。后续接入真实 DeepSeek Provider 后，才会由真实模型根据用户问题和工具说明自行决定调用哪个工具。

---

## 对话 14：Agent Loop 当前完成度评估

### 用户问题

目前 Agent Loop 除了不做接入真正的 AI 模型功能之外，完成度如何，还需要添加什么吗？

### 助手回答

## 结论

如果目标是“学习 Agent Loop 原理并运行工具教学示例”，当前完成度约为 **85%～90%**，已经可以进入下一阶段。

如果目标是“可复用的低层 Agent Loop 库”，当前完成度约为 **70%**。

如果目标是“长期稳定运行的生产 Agent”，当前整体完成度约为 **40%**，因为还缺少运行预算、超时、持久化、重试和崩溃恢复等能力。

这些百分比是按照不同目标做的主观工程评估，不是测试覆盖率。

## 已经完成的核心能力

- `prompt()` 和 `continue_run()`；
- Agent、Turn、Message、Tool 生命周期事件；
- 模型流式事件消费；
- 串行和并行工具执行；
- 工具参数准备与校验；
- `before_tool_call` 和 `after_tool_call`；
- Steering 和 Follow-up；
- CancellationToken；
- `wait_for_idle()` 结算语义；
- length 截断工具参数拒绝执行；
- ToolRegistry；
- 加法和乘法真实 Python 工具；
- ScriptedProvider 假模型；
- 11 项自动测试；
- 中文 README、源码注释和事件说明。

因此核心的“模型提出工具调用 → Python 执行工具 → 结果回灌模型 → 模型继续回答”已经打通。

## 接入真实模型前建议补齐的 P0 能力

### 1. 运行预算

增加：

- `max_turns`：最多模型轮数；
- `max_tool_calls`：最多工具调用数；
- `max_run_seconds`：一次任务最长时间。

否则假如模型不断调用工具，Agent 可能一直循环。

### 2. Provider 和工具超时

增加：

- 单次模型请求超时；
- 单个工具执行超时；
- 超时后的统一错误事件。

CancellationToken 是合作式取消，工具不主动检查时不能保证停止。

### 3. 工具并发上限

当前并行模式会一次启动模型给出的全部工具。

建议增加：

```text
max_parallel_tools = 4
```

避免模型一次生成大量工具调用时耗尽文件句柄、进程或内存。

### 4. 并行任务异常收尾

当前某个并行工具的 listener 或事件发送发生异常时，其他工具任务可能仍在后台运行。

需要保证：

- 所有已启动任务都被等待；
- 或者明确取消剩余任务；
- 不留下无人管理的 asyncio Task。

### 5. Listener 错误策略

当前 listener 抛异常可能中断 Agent 事件链。

建议区分：

- persistence listener：失败可以终止运行；
- UI/log listener：失败只记录，不应拖垮 Agent；
- security listener：失败时默认拒绝工具。

### 6. EventStream 背压和容量

当前 EventStream 是无界队列。

至少需要：

- 最大排队事件数；
- 高频 `message_update/tool_update` 合并；
- 慢消费者处理策略。

教学示例不会有问题，但生产长输出可能积累内存。

### 7. Provider 事件和消息严格校验

需要验证：

- `start` 是否先出现；
- terminal event 是否只有一个；
- `done/error` 是否一定包含最终消息；
- toolCall 是否包含唯一 id、name 和 arguments；
- 重复 toolCallId 是否拒绝；
- Provider 提前结束但没有 final result 时不能永久等待。

### 8. 补充边界测试

当前测试还应增加：

1. 未知工具；
2. 参数校验失败；
3. before hook 阻止工具；
4. after hook 修改工具结果；
5. sequential 模式；
6. Steering 注入时机；
7. 用户取消模型流；
8. 用户取消工具；
9. Provider error/aborted；
10. Provider 没有 terminal event；
11. 最大 Turn 和工具预算；
12. listener 抛异常；
13. 大量工具 update；
14. 并行工具中一个失败、其他工具仍需正确收尾。

## 之后需要增加，但不应塞进 `loop.py` 的能力

这些功能属于更高层 Session/Host：

- JSONL 或 SQLite 会话持久化；
- Durable Inbox；
- Provider 网络重试；
- Agent 整轮重试；
- Context Window 统计；
- Compaction 上下文压缩；
- 工具执行意图和结果日志；
- 崩溃恢复；
- Token 和费用预算；
- 日志、Telemetry 和审计。

应保持：

```text
loop.py              只负责模型、工具、消息和队列
Session/Host          负责持久化、重试、压缩和恢复
Provider              负责真实模型协议
Tools                 负责具体外部能力
```

## 推荐下一步

在接入真实 DeepSeek Provider 前，建议先做一次“Agent Loop 加固阶段”：

1. 运行预算；
2. Provider/Tool timeout；
3. 最大并行工具数；
4. 并行任务异常清理；
5. Listener 错误隔离；
6. Provider 事件严格校验；
7. 补充上述边界测试。

完成这些以后，低层 Agent Loop 可以认为达到 **教学完成、基础库可用** 的状态，然后再接入真实模型。

当前不需要继续给 `loop.py` 增加持久化、压缩、TUI 或 Web 功能，否则低层循环会逐渐变成难以维护的大类。

---

## 对话 15：不接真实模型时是否需要 Provider，以及 Tool Timeout 和事件校验

### 用户问题

我目前不接入任何大模型，有必要写 Provider 吗？不应该写一个 Tool Timeout 吗？Provider 事件严格校验又是什么？

### 助手回答

你的判断是正确的。

## 1. 目前不需要再写真实 Provider

你现在不接入任何大模型，就不需要编写：

- DeepSeekProvider；
- OpenAIProvider；
- HTTP/SSE 客户端；
- API Key；
- OAuth；
- 模型错误重试。

当前已经存在两样足够教学使用的东西：

```text
stream_fn 接口          Provider 的抽象边界
ScriptedProvider        可控的假 Provider
```

所以 Provider 这一层并不是完全没有，而是已经有“接口 + 假实现”。现在没有必要继续创建复杂的 `providers/deepseek.py`。

## 2. 下一步确实应该优先实现 Tool Timeout

Tool Timeout 的意思是：

> 某个工具执行时间超过限制后，自动停止等待，并把“工具超时”作为错误结果交回 Agent。

例如：

```text
add 工具限制 2 秒
shell 工具限制 30 秒
工具超过时间仍未完成
→ 标记 timeout
→ 产生 isError=true 的 ToolResultMessage
→ Agent Loop 可以继续处理其他结果
```

### 推荐配置方式

全局默认超时：

```python
Agent(
    ...,
    default_tool_timeout_seconds=30,
)
```

单个工具可以覆盖：

```python
AgentTool(
    name="slow_tool",
    timeout_seconds=5,
    ...,
)
```

优先级：

```text
工具自己的 timeout
→ Agent 默认 timeout
→ 都没有则不限制
```

### 并行工具超时语义

假设同时执行：

```text
工具 A：1 秒完成
工具 B：超过 5 秒超时
工具 C：2 秒完成
```

推荐结果：

```text
A 成功
B 返回 timeout 错误
C 成功
```

工具 B 超时不应该自动取消 A、C，也不应该直接让整个 Agent 崩溃。

### 取消令牌注意事项

当前所有并行工具共享 Agent 的 CancellationToken。

实现 Tool Timeout 时不能直接取消这个总令牌，否则一个工具超时会把整个 Agent 和其他并行工具都取消。

应该给每个工具创建独立的子取消令牌：

```text
Agent 总取消令牌
  ├─ 工具 A 子令牌
  ├─ 工具 B 子令牌
  └─ 工具 C 子令牌
```

规则：

- 用户取消 Agent：所有子工具都取消；
- 工具 B 自己超时：只取消 B；
- A、C 继续执行。

### Tool Timeout 必须测试

至少增加：

1. 快速工具在限制内成功；
2. 慢工具超过时间返回 timeout；
3. 超时结果 `isError=true`；
4. 并行工具中一个超时不影响其他工具；
5. 用户取消仍能取消全部工具；
6. 工具超时后迟到的 update 被忽略；
7. timeout timer 不留下后台任务。

## 3. Provider 事件严格校验是什么

它不是检查模型回答得对不对，而是检查“事件格式和顺序是否合法”。

当前 ScriptedProvider 会发送：

```text
start
text_start
text_delta
text_end
done
```

工具调用可能发送：

```text
start
toolcall_start
toolcall_delta
toolcall_end
done
```

严格校验就是检查：

- `start` 必须最先出现；
- `text_delta` 前必须有 `text_start`；
- `toolcall_delta` 前必须有 `toolcall_start`；
- 每个 start 必须有对应 end；
- `done` 和 `error` 只能出现一个；
- terminal event 后不能继续发送事件；
- `done` 必须包含最终 assistant message；
- toolCall 必须有非空 id 和 name；
- arguments 必须是字典；
- 同一条 assistant message 中 toolCallId 不能重复；
- 事件流结束时 `.result()` 必须能完成，不能永久等待。

通俗解释：

> Provider 事件校验就像检查快递流程：必须先揽收，再运输，最后签收；不能没有揽收就直接运输，也不能签收两次。

### 现在是否必须实现

目前只有自己控制的 ScriptedProvider，因此它不是最高优先级。

建议优先级：

```text
P0  Tool Timeout
P0  Tool Timeout 测试
P1  最大 Turn/Tool Call 预算
P1  最大并行工具数
P1  并行任务异常清理
P2  Provider 事件严格校验
```

等以后准备接入真实模型 SDK、HTTP 流或第三方 Provider 时，再把 Provider 事件严格校验提升为 P0。

## 4. 修正后的下一步顺序

既然当前明确不接真实模型，建议按下面顺序继续：

1. 实现每个工具独立的 Tool Timeout；
2. 增加子 CancellationToken；
3. 编写超时和并行超时测试；
4. 增加最大 Turn 数；
5. 增加最大 Tool Call 数；
6. 增加最大并行工具数；
7. 修复并行任务异常收尾；
8. 最后再考虑 Provider 事件严格校验。

所以你现在最合适的下一课是：

> **Tool Timeout：让单个工具超时，但不误伤其他并行工具和整个 Agent。**

当前阶段不需要编写真实 Provider。

---

## 对话 16：实现每个工具独立的 Tool Timeout

### 用户问题

开始实现每个工具独立的 Tool Timeout，加法 2 秒，乘法 5 秒。

### 助手回答

已完成每个工具独立的 Tool Timeout：

```text
add.timeout_seconds = 2
multiply.timeout_seconds = 5
```

主要改动：

- `CancellationToken` 增加父子令牌；
- `AgentTool` 增加 `timeout_seconds`；
- `Agent` 增加 `default_tool_timeout_seconds`；
- Agent Loop 为每个工具创建独立子令牌和 timeout timer；
- 加法工具配置为 2 秒；
- 乘法工具配置为 5 秒；
- 超时结果转换成 `isError=true` 的 ToolResultMessage；
- 增加 `tests/test_tool_timeout.py`。

取消关系：

```text
Agent 总取消令牌
  ├─ add 子令牌，2 秒
  └─ multiply 子令牌，5 秒
```

行为规则：

- 用户取消 Agent：全部工具取消；
- add 超过 2 秒：只取消 add；
- multiply 超过 5 秒：只取消 multiply；
- 一个工具超时不会取消其他并行工具；
- 超时后迟到的 update 会被忽略；
- 工具完成后 timeout timer 和父子关系都会清理。

超时结果示例：

```python
{
    "role": "toolResult",
    "toolName": "add",
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

同时支持 Agent 默认超时：

```python
agent = Agent(
    ...,
    default_tool_timeout_seconds=30,
)
```

优先级：

```text
工具自己的 timeout_seconds
→ Agent 默认 timeout
→ 都没有则不限制
```

需要注意：2 秒和 5 秒表示“最长允许时间”，不是让正常计算故意等待。普通加法、乘法会立即完成。

新增 5 项测试：

1. 快速工具在自己的限制内成功；
2. 慢工具超过限制返回 `tool_timeout`；
3. 并行工具中一个超时不影响另一个；
4. 未单独配置的工具使用 Agent 默认 timeout；
5. 用户取消全部工具，并忽略取消后迟到的 update。

当前测试总数为 16，验证结果：

```text
Ran 16 tests
OK
```

相关中文说明已经更新到：

- `agent-loop/README.md`；
- `agent-loop/BUILTIN_TOOLS_DESIGN.md`。

运行验证：

```bat
cd D:\0715\git_codex\20260825\agent-loop
python -m unittest discover -s tests -v
python examples\basic_usage.py
```

当前仍未接入任何真实大模型。

---

## 对话 17：什么是子 CancellationToken

### 用户问题

增加子 CancellationToken 是干什么的？什么是子 CancellationToken？

### 助手回答

## 1. CancellationToken 是什么

`CancellationToken` 可以理解为一个“停止通知器”。

工具执行时会不断检查：

```python
cancellation.throw_if_cancelled()
```

如果用户已经要求取消，它就抛出取消异常，让工具停止继续工作。

它本身不是线程，也不是进程，只是一个保存“是否取消”和“为什么取消”的对象。

## 2. 父 CancellationToken 是什么

Agent 每次处理一个用户任务时，会创建一个总 CancellationToken：

```text
Agent 总 CancellationToken
```

它代表：

> 整个 Agent 任务是否被用户取消。

调用：

```python
agent.abort("用户取消")
```

实际上就是取消这个总令牌。

## 3. 子 CancellationToken 是什么

每个工具开始执行时，从 Agent 总令牌创建一个自己的子令牌：

```text
Agent 总令牌（父）
  ├─ add 工具令牌（子）
  └─ multiply 工具令牌（子）
```

子令牌代表：

> 只控制当前这个工具是否取消。

## 4. 为什么不能让所有工具直接共用总令牌

假设 add 和 multiply 并行执行：

```text
add        最多 2 秒
multiply   最多 5 秒
```

如果两个工具直接共用 Agent 总令牌，add 超过 2 秒时执行：

```python
总令牌.cancel()
```

结果会变成：

```text
add 被取消
multiply 也被取消
整个 Agent 也被取消
```

这不符合“每个工具独立超时”的要求。

## 5. 子令牌解决了什么问题

现在 add 和 multiply 各自使用一个子令牌。

add 超过 2 秒时：

```text
只取消 add 子令牌
multiply 子令牌不受影响
Agent 总令牌也不受影响
```

结果：

```text
add        timeout 错误
multiply   继续执行，最多允许 5 秒
Agent      继续处理两个工具的结果
```

## 6. 父子取消的传播方向

传播是单向的。

### 父令牌取消

```text
用户取消 Agent
  -> 父令牌取消
  -> add 子令牌取消
  -> multiply 子令牌取消
```

也就是：

> 取消整个 Agent，所有工具都必须停止。

### 子令牌取消

```text
add 超时
  -> add 子令牌取消
  -> 不取消父令牌
  -> 不取消 multiply 子令牌
```

也就是：

> 单个工具超时，只停止自己。

## 7. 最通俗的比喻

可以把它理解成家里的总电闸和房间电闸：

```text
总电闸                  Agent 父令牌
厨房电闸                add 子令牌
卧室电闸                multiply 子令牌
```

关闭总电闸：

```text
厨房和卧室全部断电
```

只关闭厨房电闸：

```text
厨房断电
卧室继续有电
总电闸不受影响
```

## 8. 当前代码中的实现位置

父子令牌定义在：

`agent-loop/src/pi_agent_loop/cancellation.py`

创建子令牌：

```python
tool_cancellation = cancellation.create_child()
```

工具执行时收到的不是 Agent 总令牌，而是自己的子令牌：

```python
await tool.execute(
    tool_call_id,
    arguments,
    tool_cancellation,
    on_update,
)
```

工具结束后解除父子关系：

```python
tool_cancellation.detach()
```

`detach()` 是为了防止 Agent 总令牌一直保存已经结束的工具令牌。

## 9. Timeout 时还会取消 asyncio Task

子 CancellationToken 负责：

- 告诉工具取消原因；
- 让工具主动检查取消；
- 把用户取消从父令牌传给工具。

`asyncio.Task.cancel()` 负责：

- 打断正在 `await asyncio.sleep()` 或等待异步 I/O 的工具协程。

Tool Timeout 会同时做：

```text
取消工具子令牌
+ 取消工具 asyncio Task
+ 等待工具清理完成
+ 返回 tool_timeout 结果
```

只用 Token 可能停得不及时，只用 `Task.cancel()` 又不方便工具知道取消原因，所以两者配合使用。

## 10. 在当前加法、乘法中的作用

正常情况下：

```text
add 很快算出 5
multiply 很快算出 20
```

不会真的触发 2 秒和 5 秒限制。

如果未来把 add 改成故意等待 3 秒：

```text
add 的 2 秒 timer 到期
  -> add 子令牌取消
  -> add Task 取消
  -> 返回 tool_timeout
```

multiply 仍然继续执行。

## 11. 一句话总结

> **子 CancellationToken 就是每个工具自己的停止开关。整个 Agent 的父开关可以关闭全部工具，但某个工具自己的开关只关闭自己。**

它是实现“加法超时 2 秒、乘法超时 5 秒，并且互不误伤”的关键。

---

## 对话 18：完成子 CancellationToken，并设置模拟延时触发 Timeout

### 用户问题

完成子 CancellationToken 的开发。执行 `python examples/basic_usage.py` 时，在哪里设置延时来触发工具超时限制？

### 助手回答

子 CancellationToken 已完成开发，并新增了专门测试。

## 1. 在哪里设置模拟延时

打开：

`agent-loop/examples/basic_usage.py`

在文件顶部找到：

```python
ADD_TOOL_DELAY_SECONDS = 0.0
MULTIPLY_TOOL_DELAY_SECONDS = 0.0
```

这两个值是教学用模拟执行时间。

## 2. 正常运行，不触发超时

```python
ADD_TOOL_DELAY_SECONDS = 0.0
MULTIPLY_TOOL_DELAY_SECONDS = 0.0
```

结果：

```text
add 立即返回 5
multiply 立即返回 20
```

## 3. 只触发加法超时

```python
ADD_TOOL_DELAY_SECONDS = 3.0       # 大于加法限制 2 秒
MULTIPLY_TOOL_DELAY_SECONDS = 0.0
```

运行后会看到：

```text
multiply 成功：20
add 失败：工具 add 执行超时（限制 2 秒）
```

这也证明 add 超时不会误伤 multiply。

## 4. 只触发乘法超时

```python
ADD_TOOL_DELAY_SECONDS = 0.0
MULTIPLY_TOOL_DELAY_SECONDS = 6.0  # 大于乘法限制 5 秒
```

运行后会看到：

```text
add 成功：5
multiply 失败：工具 multiply 执行超时（限制 5 秒）
```

## 5. 两个工具都触发超时

```python
ADD_TOOL_DELAY_SECONDS = 3.0
MULTIPLY_TOOL_DELAY_SECONDS = 6.0
```

由于两个工具并行执行，总等待时间大约是 5 秒，不是 9 秒。

## 6. Timeout 限制在哪里配置

工具真正的最长时间配置在：

- `src/pi_agent_loop/tools/add.py`：`timeout_seconds=2`；
- `src/pi_agent_loop/tools/multiply.py`：`timeout_seconds=5`。

区别是：

```text
ADD_TOOL_DELAY_SECONDS          模拟工具实际执行多久
AgentTool.timeout_seconds       工具最多允许执行多久
```

只有：

```text
模拟执行时间 > 最长允许时间
```

才会触发 Timeout。

## 7. 子 CancellationToken 已增加的测试

新增：

`tests/test_cancellation.py`

验证：

1. 父令牌取消会取消全部子令牌；
2. add 子令牌取消不会取消父令牌；
3. add 子令牌取消不会影响 multiply 子令牌；
4. 工具结束并 `detach()` 后，父令牌不再保存它。

加上 Tool Timeout 测试后，当前共有 19 项测试：

```text
Ran 19 tests
OK
```

## 8. 运行命令

正常运行：

```bat
cd D:\0715\git_codex\20260825\agent-loop
python examples\basic_usage.py
```

修改延时变量后，再执行同一命令即可观察 Timeout。

运行全部测试：

```bat
python -m unittest discover -s tests -v
```

相关中文说明已更新到 `agent-loop/README.md`。

---

## 对话 19：什么是最大 Turn 和 Tool Call 预算

### 用户问题

最大 Turn/Tool Call 预算是什么？

### 助手回答

## 1. 预算是什么意思

这里的“预算”不是只指钱，而是：

> 给 Agent 规定最多可以循环多少轮、最多可以调用多少次工具。

它的作用是防止 Agent 失控后无限循环。

例如模型不断重复：

```text
调用工具
→ 读取结果
→ 又调用工具
→ 读取结果
→ 又调用工具
...
```

如果没有限制，真实模型可能一直运行，直到用户取消、程序崩溃或费用耗尽。

## 2. 最大 Turn 预算

一个 Turn 可以简单理解为：

```text
模型生成一条 assistant 回复
+ 执行这条回复要求的全部工具
```

配置示例：

```python
Agent(
    ...,
    max_turns=10,
)
```

表示：

> 一次 Agent 运行最多允许模型回答 10 轮。

达到限制后不再发起第 11 次模型请求。

## 3. 最大 Tool Call 预算

Tool Call 预算限制一次运行中工具调用的总数量。

配置示例：

```python
Agent(
    ...,
    max_tool_calls=20,
)
```

表示：

> 一次 Agent 运行最多允许模型请求 20 个工具调用。

第 21 个工具调用不会执行。

## 4. 当前计算示例用了多少预算

当前任务：

```text
请同时计算 2+3 和 4×5
```

第 1 Turn：

```text
模型返回 add 和 multiply
执行 2 个 Tool Call
```

第 2 Turn：

```text
模型读取工具结果
生成最终回答
没有工具调用
```

总计：

```text
Turn 使用量       2
Tool Call 使用量  2
```

因此下面配置可以正常完成：

```python
Agent(
    ...,
    max_turns=2,
    max_tool_calls=2,
)
```

如果 `max_turns=1`，第一轮工具做完后，Agent 没有预算进行第二次模型请求，也就无法生成最终中文回答。

如果 `max_tool_calls=1`，模型一次要求 add 和 multiply 两个工具时就超过限制。

## 5. 并行工具怎样计数

并行只代表同时执行，不代表只算一次。

```text
add 和 multiply 同时执行
```

仍然计算为：

```text
2 个 Tool Call
```

例如一次模型回复同时要求 8 个工具，就消耗 8 个 Tool Call 预算。

## 6. Tool Call 预算应该统计哪些调用

推荐统计模型请求的每一个 toolCall，包括：

- 正常工具；
- 未知工具；
- 参数错误工具；
- 被 before hook 阻止的工具；
- 并行工具。

原因是如果错误工具不计数，模型可能不断生成无效调用，绕过预算限制。

## 7. 超出 Tool Call 预算时怎样处理

假设剩余预算只有 1，但模型一次请求：

```text
add
multiply
```

不建议只执行 add、拒绝 multiply，因为这会产生“执行了一半”的副作用。

推荐整批都不执行：

```text
add       未执行：工具调用预算不足
multiply  未执行：工具调用预算不足
```

然后结束当前运行，并产生结构化错误：

```text
code=tool_call_budget_exceeded
limit=1
requested=2
```

## 8. 超出 Turn 预算时怎样处理

开始下一次模型请求前检查：

```text
已经使用的 Turn 数 >= max_turns
```

如果达到限制：

- 不再请求模型；
- 发出 budget exceeded 事件；
- 生成明确错误状态；
- 正常执行 `agent_end`；
- 不把它当成程序崩溃。

## 9. 预算和 Timeout 的区别

| 功能 | 限制什么 |
|---|---|
| Tool Timeout | 单个工具最多运行多少秒 |
| Run Timeout | 整个任务最多运行多少秒 |
| Max Turns | 最多请求模型多少轮 |
| Max Tool Calls | 最多请求多少个工具 |
| Max Parallel Tools | 最多同时运行多少个工具 |

例子：

```text
max_tool_calls=20       总共最多调用 20 个工具
max_parallel_tools=4    同一时刻最多运行 4 个工具
```

这两个不能互相替代。

## 10. 为什么当前没有真实模型也值得实现

当前 ScriptedProvider 的响应数量有限，通常不会无限循环，所以教学示例暂时安全。

但预算仍然值得学习和测试，因为它能验证：

- Loop 是否能在正确边界停止；
- 是否会多请求一次模型；
- 并行工具如何统一计数；
- 超预算时是否会错误地执行半批工具。

以后接入真实模型时，这些保护已经存在。

## 11. 推荐默认值

教学阶段建议：

```python
max_turns = 10
max_tool_calls = 20
```

当前计算示例只使用：

```text
2 Turns
2 Tool Calls
```

不会触发限制。

## 12. 必须增加的测试

1. 两轮、两个工具在预算内正常完成；
2. `max_turns=1` 时禁止第二次模型请求；
3. `max_tool_calls=1` 时整批拒绝两个工具；
4. 并行的两个工具计数为 2；
5. 未知工具和参数错误也消耗预算；
6. 达到预算后产生明确错误事件；
7. 新的一次 `agent.prompt()` 重新获得新的运行预算。

## 13. 一句话总结

> **最大 Turn 预算限制 Agent 最多思考几轮；最大 Tool Call 预算限制 Agent 最多安排多少次工具。它们是防止 Agent 无限循环和无限执行工具的安全保险丝。**

---

## 对话 20：最大 Turn、最大 Tool Call 和最大并行工具数的关系

### 用户问题

我的理解是，最大 Turn 和最大 Tool Call 是相互关联的，最大 Tool Call 不能大于最大 Turn，对吗？

假如最大 Turn 是 20，需要调用 10 个工具，但是最大 Tool Call 设置为 6，会怎样运行？是第一轮运行 6 个工具、第二轮运行 4 个工具，还是直接停止？

### 助手回答

## 1. 最大 Tool Call 可以大于最大 Turn

这两个数有关联，但不是大小限制关系。

```text
max_turns       限制最多请求模型多少轮
max_tool_calls  限制整个运行最多接受多少个工具调用
```

一个 Turn 可以包含：

- 0 个工具；
- 1 个工具；
- 多个并行工具。

所以完全可以这样配置：

```python
max_turns = 20
max_tool_calls = 100
```

意思是最多允许模型回答 20 轮，整个任务最多调用 100 个工具。

## 2. 一个 Turn 可以调用多个工具

例如当前计算示例：

```text
第 1 Turn
  -> 模型同时调用 add 和 multiply
  -> 消耗 2 个 Tool Call

第 2 Turn
  -> 模型生成最终回答
  -> 消耗 0 个 Tool Call
```

总计：

```text
Turn = 2
Tool Call = 2
```

因此 Tool Call 数不需要小于或等于 Turn 数。

## 3. 你的配置代表什么

```python
max_turns = 20
max_tool_calls = 6
```

它表示：

```text
模型最多可以回答 20 轮
但整个运行最多只能请求 6 个工具
```

可能出现很多轮纯文本回答，但工具调用累计不能超过 6 个。

## 4. 模型一次请求 10 个工具时会怎样

假设当前已经使用 0 个工具，剩余预算为 6，但模型在同一条 assistant message 中一次请求 10 个工具。

按照之前推荐的“硬预算 + 整批拒绝”策略：

```text
requested = 10
remaining = 6
10 > 6
```

结果是：

```text
10 个工具全部不执行
不会执行前 6 个
不会自动把后 4 个放到下一 Turn
产生 tool_call_budget_exceeded
结束当前 Agent 运行
```

所以推荐答案是：**直接停止这一批工具，不执行 6+4。**

## 5. 为什么不执行前 6 个、再执行后 4 个

### 原因一：这会违反最大 Tool Call 总预算

`max_tool_calls=6` 表示整个运行最多只能执行 6 个工具。

把后 4 个放到下一轮继续执行，最后仍然执行了 10 个，等于绕过预算。

### 原因二：Agent 不应该擅自拆模型的一批工具

这 10 个工具来自同一条 assistant message，可能存在业务关系。

只执行一半可能造成：

- 文件只修改一半；
- 一组操作处于不完整状态；
- 后续工具依赖前面结果；
- 模型收到残缺的一批结果。

### 原因三：不能产生部分副作用

例如模型一次请求：

```text
创建 5 个文件
修改 5 个文件
```

只执行前 6 个后停止，项目会处于“做了一半”的状态。

因此超出总预算时，执行前就应该整批拒绝。

## 6. 什么配置才会按照 6+4 分批执行

你想到的“先运行 6 个，再运行 4 个”对应的不是 `max_tool_calls`，而是：

```python
max_parallel_tools = 6
```

假设：

```python
max_tool_calls = 20
max_parallel_tools = 6
```

模型一次请求 10 个工具时，可以这样调度：

```text
总工具预算 20，10 个工具没有超预算

第 1 批并发执行 6 个
第 2 批并发执行 4 个
```

但这两批仍属于同一个 Turn，因为中间没有再次请求模型，只是工具并发调度被分成两批。

## 7. 三个概念要分开

| 配置 | 作用 |
|---|---|
| `max_turns=20` | 最多调用模型 20 轮 |
| `max_tool_calls=6` | 整个运行最多接受 6 个工具调用 |
| `max_parallel_tools=6` | 同一时刻最多并行执行 6 个工具 |

### 情况 A

```python
max_turns = 20
max_tool_calls = 6
max_parallel_tools = 6
```

模型一次请求 10 个工具：

```text
超过总工具预算
→ 10 个全部拒绝
→ 直接停止
```

### 情况 B

```python
max_turns = 20
max_tool_calls = 20
max_parallel_tools = 6
```

模型一次请求 10 个工具：

```text
没有超过总工具预算
→ 先并发 6 个
→ 再并发 4 个
→ 全部完成后进入下一模型 Turn
```

### 情况 C

```python
max_turns = 2
max_tool_calls = 20
max_parallel_tools = 6
```

第一 Turn 请求 10 个工具：

```text
工具分成 6+4 执行
```

第二 Turn 生成最终回答：

```text
任务完成
```

这个配置可以正常工作。

## 8. 工具分散在多个 Turn 时怎样累计

假设：

```python
max_turns = 20
max_tool_calls = 6
```

运行过程：

```text
第 1 Turn 请求 4 个工具
→ 已使用 4，剩余 2

第 2 Turn 请求 2 个工具
→ 已使用 6，剩余 0

第 3 Turn 又请求 1 个工具
→ 超出预算
→ 这一批拒绝并停止
```

Tool Call 预算是整个运行累计的，不会每个 Turn 重新变成 6。

## 9. 未知工具和参数错误也应该计数

例如模型连续请求不存在的工具，如果不计数，它可以无限重试错误工具。

所以推荐在模型返回 toolCall 后立即计入 requested 数量，然后才做：

- 查找工具；
- 参数校验；
- before hook；
- 真正执行。

## 10. 推荐实现策略

在执行一批工具前先整体检查：

```text
used_tool_calls + len(current_batch) <= max_tool_calls
```

如果成立：

```text
整批进入正常预检和执行
```

如果不成立：

```text
整批不执行
为每个 toolCall 生成预算不足错误结果
标记 terminate=true
发出 budget exceeded 事件
结束当前运行
```

这样不会出现执行一半的问题。

## 11. 当前计算示例推荐配置

当前示例只需要：

```text
2 Turns
2 Tool Calls
最多同时 2 个工具
```

可以配置：

```python
max_turns = 4
max_tool_calls = 6
max_parallel_tools = 2
```

这里保留了一些余量，示例不会触发预算。

## 12. 一句话总结

> **Turn 预算限制模型思考轮数，Tool Call 预算限制工具调用总数，Parallel Tools 限制同一时刻运行数量。只有 Parallel Tools 才决定 6+4 分批；Tool Call 总预算不足时应该整批拒绝并直接停止。**

---

## 对话 21：模型调用了不存在的工具怎么办

### 用户问题

目前只有加法、乘法两个工具。如果执行命令时需要除法运算，但没有除法工具，它会怎么办？应该怎样处理？有多少种处理方式？

### 助手回答

## 1. 先说当前代码会怎样处理

如果 Provider 返回：

```python
{
    "type": "toolCall",
    "id": "divide-call",
    "name": "divide",
    "arguments": {"a": 10, "b": 2},
}
```

但 Agent 只注册了：

```text
add
multiply
```

Agent Loop 会按名称查找 `divide`。

找不到后不会崩溃，也不会随便选择一个其他工具，而是生成错误 ToolResult：

```text
工具不存在：divide
```

结果等价于：

```python
{
    "role": "toolResult",
    "toolCallId": "divide-call",
    "toolName": "divide",
    "content": [
        {"type": "text", "text": "工具不存在：divide"}
    ],
    "isError": True,
}
```

然后这个错误结果会被放回上下文，进入下一 Turn。

## 2. 为什么不能自动用乘法代替除法

Agent Loop 只知道：

```text
模型请求 divide
但 Registry 中没有 divide
```

它不能安全猜测：

```text
divide(a, b) 是否应该变成 multiply(a, 1/b)
```

因为：

- 参数语义可能不同；
- 可能涉及整数除法；
- 可能涉及除零；
- 可能要求余数；
- 可能是某个业务系统中的“divide”操作，而不是数学除法。

所以默认必须 fail-closed：找不到就拒绝，不擅自替换。

## 3. 当前 ScriptedProvider 还有一个特殊情况

现在没有真实模型，使用的是 `ScriptedProvider` 假模型。

它不会真正理解用户输入。

即使把用户问题改成：

```text
请计算 10÷2
```

如果 ScriptedProvider 仍预设返回 add、multiply，它还是会调用 add、multiply。

要测试“未知 divide 工具”，必须在预设响应中手工写一个：

```text
name=divide
```

真实模型以后只能看到已经注册的 add、multiply，正常情况下不应该主动选择未提供的 divide；但模型仍可能产生幻觉，所以 Agent Loop 必须保留未知工具保护。

## 4. 常见的 8 种处理方式

### 方式一：返回未知工具错误，让模型自己修正

流程：

```text
模型请求 divide
→ Agent 返回“工具不存在：divide”
→ 下一 Turn 模型看到错误
→ 模型选择其他办法或向用户解释
```

优点：

- 最符合通用 Agent Loop；
- 不会执行错误工具；
- 模型有机会恢复。

缺点：

- 需要再消耗一个 Turn；
- 假模型必须预设第二次响应；
- 模型可能反复请求同一个不存在的工具。

这是最推荐的默认处理方式。

### 方式二：直接停止整个 Agent

流程：

```text
发现未知工具
→ 记录 unknown_tool
→ agent_end
```

适合：

- 非常严格的自动化系统；
- 不允许模型犯工具名错误；
- 工具调用失败后继续执行可能有风险。

缺点是模型没有机会自己修正。

### 方式三：模型不用工具，直接回答

如果模型本身会计算除法，可以直接回答：

```text
10÷2=5
```

不需要 Tool Call。

优点：简单。

缺点：

- 复杂计算可能不可靠；
- 无法得到工具级审计；
- 当前 ScriptedProvider 不会自行推理。

### 方式四：增加 `divide` 工具

编写：

```text
create_divide_tool()
```

然后注册：

```python
registry.register(create_divide_tool())
```

这是需求明确且经常使用时最直接的做法。

还必须处理：

- 除数为零；
- int/float；
- NaN/无穷；
- timeout；
- 参数校验。

### 方式五：使用一个通用 calculator 工具

不再分别注册 add、multiply、divide，而是注册：

```text
calculator
```

参数：

```python
{
    "operation": "divide",
    "a": 10,
    "b": 2,
}
```

`operation` 可以是：

```text
add
multiply
divide
```

优点：

- 工具数量少；
- 容易继续增加减法、取余；
- 模型只需要记住一个工具名。

缺点：

- 工具内部逻辑越来越大；
- 每种 operation 的参数可能不同；
- 权限和统计粒度变粗。

计算器场景很适合这种方式，但文件、Shell 等不同能力不适合全部塞进一个万能工具。

### 方式六：工具别名或兼容映射

例如把模型常见错误名称：

```text
div
division
```

映射到正式工具：

```text
divide
```

适合处理名称差异，但前提是语义完全一致。

不能把不确定的工具随便映射。

### 方式七：动态加载或动态注册工具

流程：

```text
模型请求 divide
→ Registry 发现当前未加载
→ 从可信工具目录加载 divide
→ 注册后重新执行
```

适合大型插件系统。

缺点：

- 实现复杂；
- 必须解决工具来源和信任；
- 不能自动加载不可信代码；
- 需要持久化工具版本和执行记录。

当前教学阶段不建议实现。

### 方式八：询问用户怎么办

流程：

```text
当前没有 divide 工具
→ 询问用户：是否增加除法工具，还是停止任务？
```

适合教学系统和需要人工审批的系统。

优点是行为透明，缺点是自动化程度降低。

## 5. 不推荐的处理方式

### 不推荐自动执行 Shell 或 Python 表达式

例如发现没有 divide 后自动执行：

```python
eval("10 / 2")
```

或自动调用 Shell。

原因：

- `eval` 可能执行恶意代码；
- Shell 权限远大于数学工具；
- 这相当于绕过 ToolRegistry 和权限控制。

### 不推荐静默忽略

如果什么都不返回，模型会发现自己发出了 toolCall，却永远收不到对应 ToolResult，消息协议会不完整。

### 不推荐自动选择名字相近的工具

`divide` 和 `dividend_report` 名字可能相似，但含义完全不同。

## 6. 推荐当前项目采用的策略

当前教学项目建议使用：

```text
方式一：返回 unknown_tool 错误，让下一 Turn 处理
```

同时增加保护：

```text
未知工具也消耗 Tool Call 预算
同一个未知工具重复次数受 max_tool_calls 限制
达到预算后停止
```

建议把错误 details 改成结构化信息：

```python
{
    "code": "unknown_tool",
    "requestedTool": "divide",
    "availableTools": ["add", "multiply"],
}
```

给模型看的文本：

```text
工具不存在：divide。当前可用工具：add、multiply。
```

这样模型更容易在下一 Turn 修正。

## 7. 如果模型下一 Turn 继续请求 divide

运行过程可能是：

```text
第 1 Turn 请求 divide
→ unknown_tool

第 2 Turn 又请求 divide
→ unknown_tool

第 3 Turn 又请求 divide
→ unknown_tool
```

所以还需要：

- 最大 Turn 预算；
- 最大 Tool Call 预算；
- 可选的同名未知工具重复限制。

否则真实模型可能进入错误循环。

## 8. 当前代码已经具备什么

当前 `_prepare_tool_call()` 已经实现：

```text
按 name 查找工具
→ 找不到
→ 生成错误 ToolResult
→ isError=true
```

还可以改进：

- details 增加 `unknown_tool` code；
- 返回 available tools；
- 增加未知工具测试；
- 增加重复未知工具预算测试。

## 9. 一句话总结

> **模型请求不存在的工具时，Agent 不应该猜测或静默忽略，而应该返回明确的 unknown_tool 结果。常见处理有返回错误后恢复、直接停止、模型自行回答、增加工具、通用工具、别名映射、动态加载和询问用户八种；当前最推荐“返回错误，让模型下一 Turn 修正，并用预算防止无限重复”。**

---

## 对话 22：工具不是每个问题一个，Pi 和 DeepSeek Harness 如何处理能力之外的请求

### 用户问题

加法、乘法、除法只是比喻，它们背后代表真正要实现的业务工具。我不可能为每个问题都设置工具；业务相关的可以写工具，业务之外无法写工具，Agent 就不处理了吗？请查看 Pi Agent 和 DeepSeek Harness 源码怎样处理请求中不包含的工具或能力。

### 助手回答

你的这个理解非常重要：

> **工具不是“每个问题对应一个工具”，工具应该代表可复用的业务能力。**

例如不要为这些问题分别写工具：

```text
查询订单 1001
查询订单 1002
查询昨天的订单
查询未支付订单
```

而应该写一个可复用能力：

```text
query_orders
```

通过参数表达不同问题：

```python
{
    "order_id": "1001",
    "date": None,
    "status": None,
}
```

## 1. Agent 并不是所有问题都必须调用工具

一个完整 Agent 有两种主要能力：

```text
模型自身的理解、推理和回答能力
+ 外部工具提供的真实数据和操作能力
```

以下问题通常不需要工具：

- 解释一个概念；
- 总结已经放在 Context 中的内容；
- 根据已有信息给建议；
- 改写文字；
- 进行一般推理；
- 回答系统提示词中已经提供的规则。

以下问题通常必须依赖工具：

- 查询实时订单；
- 修改数据库；
- 创建退款；
- 发送邮件；
- 读取本地文件；
- 调用企业内部系统；
- 获取模型上下文中不存在的真实业务数据。

所以不是“没有对应工具就完全不处理”，而是先判断任务是否真的需要外部能力。

## 2. 如果问题超出业务工具范围怎么办

应该区分三种情况。

### 情况一：不需要外部数据

模型可以直接回答，不调用工具。

```text
用户：什么是订单状态？
模型：直接根据已有知识或 Context 解释
```

### 情况二：需要真实业务数据，但缺少工具

Agent 不能假装完成。

正确回答应该是：

```text
当前没有查询该业务数据的能力，无法确认真实结果。
```

也可以：

- 询问用户补充信息；
- 请求人工处理；
- 建议管理员增加业务能力；
- 路由到其他有权限的系统。

### 情况三：完全超出产品业务范围

Agent 应明确说明范围：

```text
这个请求不属于当前 Agent 支持的业务范围。
```

而不是自动生成新工具、执行任意代码或编造结果。

## 3. 当前没有真实模型时要注意

你现在使用的是 `ScriptedProvider`。

它不是智能模型，不会真正理解用户问题，也不会自己判断是否需要工具。

当前结构实际上是：

```text
用户输入
→ ScriptedProvider 返回预先写好的 toolCall
→ Agent Loop 执行工具
```

因此如果永远不接大模型，又希望处理不同自然语言问题，就必须增加一个确定性的规划器或路由器：

```text
用户输入
→ RequestRouter / RuleBasedPlanner
   ├─ 识别为业务查询 → 调业务工具
   ├─ 识别为已有知识问题 → 固定知识处理器
   ├─ 信息不足 → 请求用户补充
   └─ 超出范围 → 返回不支持
```

这个 RuleBasedPlanner 在架构上承担了“决定回答还是调用工具”的角色。它可以继续实现 `stream_fn` 接口，但内部不是大模型，而是规则、状态机或工作流。

没有大模型也没有规则规划器时，Agent Loop 本身无法理解任意用户问题。Agent Loop 是执行框架，不是智能来源。

## 4. Pi 源码怎样处理

Pi 的模型请求只会携带当前真正激活的工具：

```text
Agent.state.tools
→ 序列化工具 name、description、parameters
→ 发给 Provider
```

相关源码：

- `pi/packages/agent/src/agent-loop.ts`
- `pi/packages/agent/src/agent.ts`
- `pi/packages/coding-agent/src/core/agent-session.ts`

### 正常情况

模型看到：

```text
当前可用工具只有 read、edit、bash
```

它可以：

- 直接回答，不使用工具；
- 选择某个已提供工具；
- 说明当前无法完成需要其他外部能力的请求。

Pi 不要求每条用户问题都有一个工具。

### 模型幻觉调用未知工具

即使 Provider 没有声明 `divide`，模型仍可能错误生成：

```text
toolCall(name="divide")
```

Pi 在 `prepareToolCall()` 中执行：

```ts
const tool = currentContext.tools?.find(t => t.name === toolCall.name)
```

找不到时返回：

```text
Tool divide not found
```

并生成 `isError=true` 的 ToolResultMessage。

Pi 不会：

- 自动创建工具；
- 自动执行 Python/JavaScript；
- 自动改用名字相近的工具；
- 因未知工具直接让进程崩溃。

错误结果交回模型，模型下一 Turn 可以：

- 改用已有工具；
- 不用工具直接回答；
- 向用户说明能力不足。

### Pi 的动态工具能力

Pi 扩展可以显式注册新工具，工具结果也可以声明新增工具名称。

但这些工具必须来自：

- 已加载的可信扩展；
- SDK 显式注册；
- 当前 Tool Registry。

Pi 不会因为用户提出一个新问题就临时自动生成并执行陌生工具。

### Pi 的孤立 Tool Call 修复

`pi-ai/src/api/transform-messages.ts` 会为历史中缺少结果的孤立 tool call 补一条：

```text
No result provided
```

这是为了保持跨 Provider 消息协议完整，不是为了补充缺失业务能力。

## 5. DeepSeek Harness 源码怎样处理

DeepSeek Harness 也只把当前 scope 内可见、允许呈现的工具交给模型。

相关源码：

- `deepseek-harness/packages/core/tools/src/index.ts`
- `deepseek-harness/packages/core/agent-loop/src/tool-calls.ts`
- `deepseek-harness/packages/core/agent-loop/src/agent.ts`

### 未知工具的标准错误

DeepSeek Harness 定义：

```text
ToolNotFoundError
code = UNKNOWN_TOOL
message = unknown tool "工具名"
```

如果模型请求未注册工具：

```text
resolveExecution() 找不到工具
→ 抛 ToolNotFoundError
→ 转成结构化 ToolExecutionResult
→ isError=true
→ 写入 tool/result
→ 结果进入下一 Step 的 Context
```

它同样不会自动创建工具，也不会静默忽略。

### 为什么 DeepSeek Harness 使用结构化错误码

它不仅返回文字：

```text
unknown tool "divide"
```

还保留：

```text
code = UNKNOWN_TOOL
```

这样：

- 策略层可以区分未知工具和工具内部异常；
- 持久化日志可以审计；
- 恢复和重放时能保持同一错误类别；
- UI 可以展示更准确的错误类型。

### Scope 同时也是能力边界

DeepSeek Harness 中一个工具即使全局存在，但不在当前 Agent scope，也会表现为不可用或 unknown。

这不是 Bug，而是安全设计：

> 当前 Agent 没被授予的能力，不能因为系统其他位置存在就自动调用。

### Code Mode 的特殊提示

在 Code Mode 下，有些工具对模型可见，但不能直接调用，必须通过 `run_code` 间接使用。

这种情况下 DeepSeek Harness 不只返回“unknown tool”，还会告诉模型正确路径：

```text
only run_code is callable directly
call <tool> from inside a run_code program instead
```

这说明一个好的未知工具错误不仅要说“不能用”，还应尽量告诉调用者“正确做法是什么”。

### 调度与持久化

DeepSeek Harness 会把未知工具失败也作为正式工具结果记录，保持：

```text
tool/call
→ tool/result(isError=true, code=UNKNOWN_TOOL)
```

因此崩溃恢复和历史重放时，工具调用与结果仍然成对，不会留下协议缺口。

## 6. Pi 和 DeepSeek Harness 的共同原则

两个项目的共同做法是：

```text
只向模型声明当前可用工具
模型可以不调用工具直接回答
未知工具不执行
未知工具产生错误 ToolResult
错误交给下一轮模型处理
不自动生成陌生可执行代码
```

它们都没有试图为世界上的每个问题提供工具。

工具系统解决的是“外部能力”，模型或规划器解决的是“理解问题和决定怎样做”。

## 7. 业务 Agent 应该怎样设计工具

不要按用户问题数量设计工具，而要按稳定业务能力设计。

例如订单业务可以设计：

```text
query_orders
get_order_detail
update_order_status
cancel_order
create_refund
```

大量自然语言问题可以组合到这些能力：

```text
“查一下订单 1001”
→ get_order_detail

“找出昨天所有未支付订单”
→ query_orders(date=昨天, status=未支付)

“把订单 1001 取消”
→ cancel_order(order_id=1001)
```

不需要为每种句子写一个新工具。

## 8. 业务知识不一定都做成工具

可以分为四层。

### System Prompt

保存稳定、简短的业务规则：

```text
订单状态有哪些
什么时候允许退款
哪些操作必须审批
```

### Knowledge Base / RAG

保存大量文档、制度和产品说明，通过通用工具检索：

```text
search_knowledge_base
get_document
```

### Business Tools

访问实时数据或执行真实动作：

```text
query_orders
create_refund
```

### Out-of-scope Handler

处理超出能力范围的请求：

```text
说明不支持
请求补充信息
转人工
```

所以不需要“一个问题一个工具”。

## 9. 如果完全不接大模型，推荐增加 RequestRouter

当前项目不接真实模型时，推荐架构变成：

```text
用户输入
  ↓
RequestRouter
  ├─ 规则匹配业务意图
  ├─ 参数提取
  ├─ 选择已注册工具
  ├─ 信息不足时追问
  └─ 超出范围时拒绝或转人工
  ↓
Agent Loop
  ↓
Tool Registry
```

RequestRouter 可以使用：

- 正则表达式；
- 命令格式；
- 菜单；
- 状态机；
- 业务工作流；
- 确定性的 Python 规则。

例如：

```text
/order get 1001
/refund create 1001
```

这种方式没有自然语言模型的灵活理解能力，但结果可控、可测试。

## 10. 当前项目推荐的能力缺失处理

建议采用下面的统一结果：

```python
{
    "code": "capability_unavailable",
    "requestedCapability": "divide",
    "availableTools": ["add", "multiply"],
    "recoverable": True,
}
```

模型或 Router 可以根据它决定：

- 改用已有工具；
- 直接回答；
- 询问用户；
- 转人工；
- 明确拒绝。

`unknown_tool` 更偏向“调用了错误名字”。

`capability_unavailable` 更偏向“系统确实没有完成该业务的能力”。

这两个概念最好分开。

## 11. 什么时候 Agent 必须拒绝

以下情况必须明确拒绝或转人工：

- 任务需要实时数据，但没有查询工具；
- 任务需要修改业务系统，但没有写操作工具；
- 当前 Agent 没有对应权限；
- 工具不在当前 scope；
- 用户要求的操作超出产品范围；
- 不能确认结果真实性。

绝不能为了“看起来什么都会”而编造已经执行成功。

## 12. 最终结论

> **Agent 不需要为每个问题准备工具。工具代表稳定、可复用的业务能力；模型或规则 Router 负责把很多不同问题映射到少量业务工具。普通知识问题可以直接回答，需要实时数据或真实动作却没有能力时必须说明无法完成、追问或转人工。Pi 和 DeepSeek Harness 都只执行已注册且当前可见的工具；未知工具会变成正式错误结果交回下一轮，它们都不会自动创造陌生工具。**

对当前不接大模型的 Python 项目而言，如果希望处理不同业务请求，下一层真正缺少的不是“无限增加工具”，而是：

> **一个确定性的 RequestRouter / RuleBasedPlanner，用来识别业务意图、选择已有工具，并处理超出范围的请求。**

---

## 对话 23：开发最大 Turn、Tool Call 和最大并行工具数配置

### 用户问题

开发最大 Turn/Tool Call 预算和最大并行工具数，并把 `max_tool_calls`、`max_turns`、`max_parallel_tools` 提取到配置文件中，增加中文配置解释。

当前配置：

```text
max_tool_calls=10
max_parallel_tools=5
max_turns=20
```

### 助手回答

已完成三项运行限制开发，并提取到 TOML 配置文件。

## 1. 配置文件

新增：

`agent-loop/config/agent.toml`

当前配置：

```toml
[limits]
# 限制一次 Agent 运行最多接受多少个工具调用
max_tool_calls = 10

# 同一时刻最多并行执行多少个工具
max_parallel_tools = 5

# 限制一次 Agent 运行最多请求模型多少轮
max_turns = 20
```

配置文件中已经加入详细中文注释，包括超预算行为和三项配置的区别。

## 2. 配置加载

新增：

`agent-loop/src/pi_agent_loop/config.py`

提供：

```python
limits = load_agent_limits("config/agent.toml")
```

返回：

```python
AgentLimits(
    max_tool_calls=10,
    max_parallel_tools=5,
    max_turns=20,
)
```

严格校验：

- 必须包含三个字段；
- 必须是大于 0 的整数；
- 拒绝 0、负数、小数和布尔值；
- 拒绝未知字段，防止配置拼写错误后静默失效；
- TOML 格式错误会给出明确提示。

## 3. 示例已接入配置

`examples/basic_usage.py` 会读取：

```python
config_path = Path(__file__).resolve().parents[1] / "config" / "agent.toml"
limits = load_agent_limits(config_path)
```

然后传给 Agent：

```python
agent = Agent(
    ...,
    max_tool_calls=limits.max_tool_calls,
    max_parallel_tools=limits.max_parallel_tools,
    max_turns=limits.max_turns,
)
```

运行时会显示：

```text
运行预算：max_turns=20，max_tool_calls=10，max_parallel_tools=5。
```

## 4. 最大 Turn 已实现

每次真正准备请求一次 assistant 时消耗一个 Turn。

达到 `max_turns` 后：

- 不会多请求一次 Provider；
- 发出 `budget_exceeded` 事件；
- 设置 Agent `error_message`；
- 正常发出 `agent_end`；
- 尚未领取的 Steering/Follow-up 保留在队列中。

当前示例使用两轮：

```text
第 1 Turn：模型请求 add、multiply
第 2 Turn：模型生成最终回答
```

小于配置的 20。

## 5. 最大 Tool Call 总预算已实现

执行一批工具前检查：

```text
已使用 Tool Call + 当前整批数量 <= max_tool_calls
```

如果超出：

- 当前整批工具全部不执行；
- 不会只执行预算内的前几个；
- 每个调用都生成 `tool_call_budget_exceeded` 错误结果；
- 发出结构化 `budget_exceeded` 事件；
- 当前 Turn 完整记录后结束 Agent 运行。

未知工具、参数错误和被 Hook 阻止的工具也会消耗预算，防止无效调用绕过限制。

## 6. 最大并行工具数已实现

通过 `asyncio.Semaphore` 限制真正进入 `execute()` 的工具数量。

当前配置：

```text
max_parallel_tools=5
```

例如模型一次请求 8 个工具，并且总 Tool Call 预算允许：

```text
同一时刻最多运行 5 个
有工具完成后，剩余 3 个陆续进入执行
```

这 8 个工具仍属于同一个 Turn。

同时修复了并行任务异常收尾：如果一个并行调度或 listener 发生异常，会取消并等待其他已启动任务，不遗留无人管理的 asyncio Task。

## 7. 新增预算事件

超预算事件格式：

```python
{
    "type": "budget_exceeded",
    "budget": "turns" 或 "tool_calls",
    "limit": 10,
    "used": 8,
    "requested": 3,
    "remaining": 2,
    "message": "...",
}
```

示例事件打印器已经增加中文说明。

## 8. 每次 Prompt 独立预算

```text
agent.prompt("任务一")  使用一份新预算
agent.prompt("任务二")  再使用一份新预算
```

前一个任务使用的 Turn 和 Tool Call 不会扣减后一个任务。

## 9. 新增测试

新增：

- `tests/test_config.py`：4 项；
- `tests/test_budgets.py`：7 项。

测试覆盖：

- 配置读取为 10、5、20；
- 缺字段、错误类型和未知字段；
- 两轮两个工具在预算内完成；
- `max_turns=1` 阻止第二次模型请求；
- Tool Call 超限整批拒绝；
- Tool Call 跨 Turn 累计；
- 最大实际并行数；
- 未知工具消耗预算；
- 新 Prompt 重置预算。

当前测试总数：

```text
Ran 30 tests
OK
```

## 10. 文档更新

已更新：

- `agent-loop/README.md`；
- `agent-loop/BUILTIN_TOOLS_DESIGN.md`；
- `conversation-review.md`。

运行：

```bat
cd D:\0715\git_codex\20260825\agent-loop
python examples\basic_usage.py
python -m unittest discover -s tests -v
```

当前仍使用 ScriptedProvider，没有接入任何真实大模型。

---

## 对话 24：并行任务异常清理是否已经完成

### 用户问题

目前是否已经完成并行任务异常清理功能？

### 助手回答

## 结论

**基础清理已经实现，但还不能说达到完整的生产级完成。**

对于当前加法、乘法这种会正常响应取消的异步工具，现有实现可以正确工作。

如果按完成度评估：

```text
教学和普通异步工具场景       已完成
生产级异常与恶意工具场景     仍需加固
```

## 已经完成的部分

### 1. 并行任务统一等待

`_execute_tools_parallel()` 不再逐个等待任务，而是使用 `asyncio.gather()` 等待全部已启动工具。

结果数组仍按模型原始工具顺序排列。

### 2. 一个并行任务异常时取消其余任务

外层捕获到异常后会：

```text
取消尚未结束的并行 run_one Task
→ 等待所有 Task 完成或取消
→ 再把原始异常向上传播
```

避免只抛出第一个错误，却完全不管其他并行任务。

### 3. Tool Timeout 的等待任务会清理

每个工具结束后会清理：

- timeout timer Task；
- cancellation wait Task；
- 工具子 CancellationToken 的父子关系。

### 4. 用户取消时并行工具会分别收尾

Agent 父 CancellationToken 会取消所有工具子令牌。

每个 `_execute_prepared_tool()` 会取消并等待自己的 execute Task，然后返回 `tool_cancelled`。

### 5. 已有测试

当前测试已经覆盖：

- 一个并行工具超时不影响另一个；
- 用户取消全部工具；
- 取消后的迟到 update 被忽略；
- `max_parallel_tools` 实际并发数量；
- 正常并行工具全部完成。

## 仍然存在的缺口

### 1. 外层取消 `run_one` 时，内部 execute Task 可能没有被清理

当前每个 `run_one` 内部还会创建一个独立的：

```text
execute_task
```

如果另一个并行工具因为 listener 或调度异常导致外层取消当前 `run_one`：

```text
run_one 被取消
```

但内部单独创建的 `execute_task` 不一定自动跟着取消。

这可能留下后台工具 Task。

### 2. update listener 抛异常时，detach 可能没有执行

当前清理顺序中会等待 update Task。

如果 update listener 自己抛出异常，可能在执行：

```python
tool_cancellation.detach()
```

之前中断，导致已经结束的子令牌仍被父令牌保存。

### 3. 工具吞掉 CancelledError 时可能一直不结束

不规范工具可能写成：

```python
try:
    await long_work()
except asyncio.CancelledError:
    while True:
        ...
```

这种工具故意忽略取消，`await execute_task` 可能永久等待。

纯 asyncio 无法强制杀死不合作的同步代码或恶意协程。此类工具最终需要：

- 子进程；
- worker；
- 进程组；
- 强制终止机制。

### 4. 缺少专门的异常清理测试

目前有 timeout 和用户取消测试，但还缺少：

- 一个工具的 listener 抛异常时，其他慢工具是否取消；
- 外层 `run_one` 被取消后，内部 execute Task 是否消失；
- update listener 抛异常后，子令牌是否 detach；
- 清理异常是否不会覆盖最初的主要异常；
- 运行结束后是否不存在遗留的命名工具 Task。

## 建议补齐的实现

### 第一项：外层取消时强制清理 execute Task

`_execute_prepared_tool()` 的最外层 `finally` 应增加：

```text
如果 execute_task 仍未完成
→ 取消工具子令牌
→ execute_task.cancel()
→ await execute_task
```

### 第二项：detach 必须放在最外层 finally

无论以下哪一步失败：

- execute；
- timeout；
- update listener；
- tool end listener；

都必须执行：

```python
tool_cancellation.detach()
```

### 第三项：清理等待增加上限

增加类似：

```text
cleanup_timeout_seconds
```

防止不合作的工具让整个 Agent 永远卡在清理阶段。

但超过清理时间后，协程仍可能存在，所以真正强制隔离仍要依赖子进程。

### 第四项：保留主要异常

如果主要错误是：

```text
工具 A listener 失败
```

清理工具 B 时又出错，不应把最初错误覆盖掉。

应该保留：

```text
primary_error
cleanup_errors[]
```

### 第五项：增加专门测试

建议新增：

`tests/test_parallel_cleanup.py`

完成上述异常场景测试后，才能说：

> 对合作式异步 Python 工具，并行异常清理已经完成。

## 当前准确状态

| 能力 | 状态 |
|---|---|
| 正常并行完成 | 已完成 |
| 并行数量限制 | 已完成 |
| 单工具独立 Timeout | 已完成 |
| 用户取消传播 | 已完成 |
| 超时后迟到 update 隔离 | 已完成 |
| 外层异常取消嵌套 execute Task | 尚需加固 |
| update listener 异常下保证 detach | 尚需加固 |
| 不合作工具强制终止 | 需要子进程方案 |
| 专门异常清理测试 | 尚未完成 |

## 一句话总结

> **目前已经完成正常场景、Timeout 和用户取消下的并行清理，但 listener/调度异常导致的嵌套 Task 清理还没有完全补齐，因此应称为“基础完成”，不能称为“生产级全部完成”。**

---

## 对话 25：如何区分是否需要工具、缺少业务能力和超出业务范围

### 用户问题

进行下一步探讨。完整 Agent 有“模型自身的理解、推理和回答能力 + 外部工具提供的真实数据和操作能力”。

1. 怎么区分哪些问题需要使用工具，哪些问题不需要工具？
2. 如何区分需要真实业务数据但缺少工具？
3. 哪些请求完全超出产品业务范围？

请结合 Pi Agent 和 DeepSeek Harness 源码进行解释和规划。

### 助手回答

## 核心结论

这三个问题不能只靠 ToolRegistry 判断，应该拆成三次独立判断：

```text
第一步：请求是否属于产品业务范围？
第二步：完成请求是否需要外部真实数据或产生真实操作？
第三步：当前 Agent 是否拥有所需能力和权限？
```

建议统一产生五种结果：

```text
in_scope_no_tool              业务范围内，不需要工具
in_scope_tool_ready           业务范围内，需要工具，而且工具可用
in_scope_need_clarification   业务范围内，但缺少用户参数
in_scope_capability_missing   业务范围内，但系统缺少能力
out_of_scope                  完全超出产品范围
prohibited                    属于相关业务，但安全或合规禁止
```

还需要修正一个概念：生产 Agent 通常不只有两种能力，而是三层：

```text
理解与规划层        模型或 RuleBasedPlanner
外部能力层          Tools
控制与安全层        Router、Scope、Policy、Session
```

你当前不接大模型，所以“理解与规划层”不能依靠模型，必须由确定性的 RequestRouter/RuleBasedPlanner 承担。

## 1. 怎样判断问题是否需要工具

最可靠的判断标准不是看用户句子中有没有“查询”“计算”等关键词，而是看答案的来源和是否会产生副作用。

### 不需要工具

满足以下任一情况通常不需要工具：

- 答案已经在当前用户输入中；
- 答案已经在 Session Context 中；
- 是对已有内容的总结、改写、分类或解释；
- 是稳定业务规则，而且规则已经加载到 System Prompt 或本地静态知识中；
- 不要求实时性；
- 不会修改任何外部系统。

例如：

```text
“总结我刚才粘贴的订单内容”
→ 数据已经在输入中
→ 不需要工具
```

```text
“解释什么叫已发货状态”
→ 规则已经在业务知识中
→ 不需要工具
```

### 必须使用工具

满足以下任一情况通常必须使用工具：

- 需要当前数据库中的真实数据；
- 需要实时状态；
- 需要读取模型 Context 中不存在的私有数据；
- 需要修改数据库或业务系统；
- 需要发送消息、邮件或通知；
- 需要创建、取消、退款、审批等真实动作；
- 需要访问文件、网络或其他外部服务。

例如：

```text
“订单 1001 现在是什么状态？”
→ 需要当前订单数据
→ 需要 get_order/query_orders 工具
```

```text
“取消订单 1001”
→ 会修改真实业务状态
→ 必须使用 cancel_order 工具
```

### 推荐判断表

| 数据来源或动作 | 是否需要工具 |
|---|---|
| 用户当前输入中已有的信息 | 通常不需要 |
| 当前 Context 中已有的信息 | 通常不需要 |
| 已加载的稳定业务规则 | 通常不需要 |
| 大量业务文档，需要检索 | 需要知识库检索工具 |
| 当前数据库、实时 API | 必须需要 |
| 创建、修改、删除真实数据 | 必须需要 |
| 发送邮件、通知、支付、退款 | 必须需要 |

## 2. 怎样区分“需要真实业务数据，但缺少工具”

不能先寻找某个工具名，再判断任务。应该先得到“请求需要什么业务能力”，然后再和当前能力目录比较。

推荐流程：

```text
用户请求
  ↓
识别业务 Intent
  ↓
生成 RequiredCapabilities
  ↓
从 CapabilityRegistry 查找能满足能力的工具
  ↓
找到      → in_scope_tool_ready
找不到    → in_scope_capability_missing
```

例如：

```text
用户：查询订单 1001 当前状态
```

先得到需求：

```python
required_capabilities = ["orders.read_current"]
```

再检查工具：

```text
get_order 提供 orders.read_current
→ 工具存在
→ 可以执行
```

如果当前只有：

```text
create_order
cancel_order
```

没有任何工具提供 `orders.read_current`，结果应为：

```text
in_scope_capability_missing
```

返回：

```python
{
    "status": "in_scope_capability_missing",
    "intent": "order.get_status",
    "requiredCapabilities": ["orders.read_current"],
    "availableCapabilities": ["orders.create", "orders.cancel"],
    "message": "请求属于订单业务，但当前没有查询实时订单状态的能力。",
}
```

### Unknown Tool 与 Capability Missing 不一样

```text
unknown_tool
```

表示规划器请求了一个没有注册的工具名称，可能是名称写错或模型幻觉。

```text
capability_missing
```

表示系统经过能力匹配后确认：当前确实没有任何工具能完成这项业务。

例如：

```text
请求 divide，但正式工具叫 calculate
→ 可能是 unknown_tool 或别名问题
```

```text
系统完全没有任何除法/计算能力
→ capability_missing
```

## 3. 怎样判断完全超出产品业务范围

产品范围必须由明确配置定义，不能让 Agent 每次临时猜测。

例如订单 Agent 的范围配置可以是：

```text
支持领域：
  orders
  refunds
  customers

支持 Intent：
  order.query
  order.cancel
  refund.query
  refund.create
  customer.query
```

请求：

```text
“查询订单 1001”
```

属于 `orders`，是业务范围内。

请求：

```text
“明天北京天气如何？”
```

不属于 orders/refunds/customers，结果是：

```text
out_of_scope
```

### 不要把下面情况误判为 Out of Scope

#### 缺少参数

```text
“帮我查一下那个订单”
```

这仍然属于订单业务，只是缺少 order_id。

应该返回：

```text
in_scope_need_clarification
```

并询问订单号。

#### 缺少工具

```text
“导出本月退款明细”
```

如果退款属于产品业务，但没有 export_refunds 工具，这是：

```text
in_scope_capability_missing
```

不是 out_of_scope。

#### 权限禁止

```text
“绕过审批直接退款”
```

它与退款业务相关，但违反安全规则，应归类为：

```text
prohibited
```

不是 out_of_scope，也不是 capability_missing。

## 4. 五种状态的完整例子

以订单 Agent 为例：

| 用户请求 | 分类 | 处理 |
|---|---|---|
| 解释“已发货”是什么意思 | `in_scope_no_tool` | 使用已加载业务规则直接回答 |
| 查询订单 1001 | `in_scope_tool_ready` | 调用 get_order |
| 查询“那个订单” | `in_scope_need_clarification` | 询问 order_id |
| 导出退款，但无导出能力 | `in_scope_capability_missing` | 明确能力缺失或转人工 |
| 查询天气 | `out_of_scope` | 说明超出订单 Agent 范围 |
| 绕过审批退款 | `prohibited` | 明确拒绝并记录安全原因 |

## 5. Pi 源码如何决定是否使用工具

Pi 当前成熟主链主要采用“模型主导选择、宿主强制校验”。

### Pi 给模型哪些信息

Pi 每次请求只发送当前激活工具：

```text
Agent.state.tools
→ name
→ description
→ parameters
→ Provider
```

相关源码：

- `pi/packages/agent/src/agent-loop.ts`
- `pi/packages/agent/src/agent.ts`
- `pi/packages/coding-agent/src/core/agent-session.ts`
- `pi/packages/coding-agent/src/core/system-prompt.ts`

模型根据：

- System Prompt；
- 用户消息；
- 历史 Context；
- 工具 description；
- 工具 parameters；

决定：

```text
直接回答
或
返回 toolCall
```

Agent Loop 本身不负责理解“这个问题是否应该使用工具”。它只执行模型已经生成的合法 toolCall。

### Pi 的产品边界从哪里来

Pi 没有一个通用的中央 BusinessScope 分类器，范围主要来自：

- System Prompt；
- 当前 active tools；
- Skills；
- 项目 Context 文件；
- Extensions 的 input/context hook；
- before_tool_call；
- Project Trust。

扩展的 `input` hook 可以在进入模型前：

- handled：直接处理；
- transform：修改输入；
- continue：继续给模型。

因此如果要在 Pi 中增加确定性业务 Router，最适合放在 input/Host 层，而不是改低层 Agent Loop。

### Pi 对缺失工具的处理

模型调用未注册工具时：

```text
Tool <name> not found
→ isError=true ToolResultMessage
→ 下一 Turn 交回模型修正
```

Pi 不自动创造能力，也不把缺失工具等同于整个产品范围之外。

## 6. DeepSeek Harness 如何决定是否使用工具

DeepSeek Harness 也是“模型负责规划，平台负责能力边界和执行安全”，但平台边界更强。

相关源码：

- `deepseek-harness/packages/core/system-prompt/src/index.ts`
- `deepseek-harness/packages/core/tools/src/index.ts`
- `deepseek-harness/packages/core/agent-loop/src/agent.ts`
- `deepseek-harness/packages/core/agent-loop/src/tool-calls.ts`

### 工具可见性

模型只看到当前：

- Agent Preset 允许的工具；
- Scope 内可见的工具；
- 当前 presentation mode 允许的入口；
- 通过系统提示词组装出的工具 Schema。

如果 assistant 没有返回 tool call，Agent Step 正常完成。

### Scope 是能力和安全边界

一个工具即使在系统其他位置存在，但没有进入当前 Agent Scope，也不能调用。

这意味着：

```text
系统拥有这个能力
不等于
当前 Agent 被授权使用这个能力
```

### 缺失工具

真正未注册的工具会变成：

```text
ToolNotFoundError
code=UNKNOWN_TOOL
isError=true
```

并作为正式 tool/result 写入 Session，再交给下一 Step。

### Code Mode 的路由提示

若工具并非真正缺失，只是必须通过 `run_code` 间接调用，DeepSeek Harness 会返回正确路径，而不是含糊地说 unknown：

```text
only run_code is callable directly
call <name> from inside run_code
```

这值得借鉴：能力不可直接调用时，应告诉规划器正确入口。

### Policy、Approval 和 Guard

即使能力存在，仍可能因为：

- 当前 Scope；
- Permission Policy；
- 用户审批；
- Sandbox；
- Guard；

而不能执行。

所以 DeepSeek Harness 明确区分：

```text
能力不存在
能力不可见
能力存在但需审批
能力被策略拒绝
工具内部执行失败
```

## 7. 两个源码给出的共同答案

Pi 和 DeepSeek Harness 都没有内置一个万能规则表，自动判断所有自然语言问题属于哪种业务。

它们共同采用：

```text
System Prompt/Context/Tools 告诉模型当前能力
→ 模型决定直接回答还是发 toolCall
→ Host/ToolRuntime 强制检查工具是否存在和是否允许
→ 缺失或拒绝变成 ToolResult
→ 下一轮模型修正或说明失败
```

区别是：

| 项目 | 特点 |
|---|---|
| Pi | 结构直接，模型选择工具，扩展 hook 可加入业务 Router |
| DeepSeek Harness | Scope、Policy、Approval、结构化错误和持久化边界更强 |

## 8. 当前不接大模型时的推荐架构

因为 ScriptedProvider 不理解自然语言，所以需要在 Agent Loop 前增加确定性 Router：

```text
用户请求
  ↓
RequestRouter
  ↓
BusinessScopeMatcher
  ↓
IntentParser
  ↓
CapabilityMatcher
  ├─ 不需要工具 → Static/Knowledge Handler
  ├─ 工具可用 → 生成 toolCall 计划
  ├─ 缺少参数 → Clarification
  ├─ 缺少能力 → Capability Missing
  ├─ 超出范围 → Out of Scope
  └─ 策略禁止 → Prohibited
  ↓
Agent Loop / ToolRegistry
```

### 推荐目录规划

```text
src/pi_agent_loop/routing/
├─ __init__.py
├─ types.py
├─ scope.py
├─ intents.py
├─ capabilities.py
├─ router.py
├─ planner.py
└─ responses.py

config/
├─ agent.toml
└─ business_scope.toml
```

## 9. RequestDecision 规划

建议 Router 输出统一结构：

```python
RequestDecision(
    status="in_scope_tool_ready",
    domain="orders",
    intent="order.get_status",
    required_capabilities=["orders.read_current"],
    missing_fields=[],
    selected_tools=["get_order"],
    reason="需要读取当前订单状态",
)
```

状态：

```python
RequestStatus = Literal[
    "in_scope_no_tool",
    "in_scope_tool_ready",
    "in_scope_need_clarification",
    "in_scope_capability_missing",
    "out_of_scope",
    "prohibited",
]
```

## 10. Capability 不能只使用工具名

工具名是实现名称，Capability 是业务能力。

例如：

```text
工具名：get_order_v2
能力：orders.read_current
```

将来可以替换工具实现，但 Router 仍然寻找：

```text
orders.read_current
```

建议工具目录额外维护：

```python
ToolCapability(
    tool_name="get_order_v2",
    capabilities={"orders.read_current"},
    domain="orders",
    operation="read",
    risk="low",
    requires_approval=False,
)
```

## 11. `business_scope.toml` 规划

示意：

```toml
[product]
name = "订单业务 Agent"
general_qa = false

[domains.orders]
description = "订单查询、状态和取消"

[domains.refunds]
description = "退款查询和创建"

[[intents]]
id = "order.get_status"
domain = "orders"
patterns = ["查询订单", "订单状态"]
required_fields = ["order_id"]
required_capabilities = ["orders.read_current"]

[[intents]]
id = "order.cancel"
domain = "orders"
patterns = ["取消订单"]
required_fields = ["order_id"]
required_capabilities = ["orders.cancel"]

[[prohibited]]
pattern = "绕过审批"
reason = "禁止绕过业务审批流程"
```

没有大模型时，`patterns` 可以使用：

- 命令前缀；
- 关键词；
- 正则表达式；
- 明确菜单选项。

必须承认：纯规则 Router 无法可靠理解无限变化的自然语言。要求高可靠时，最好使用结构化命令或表单。

## 12. 推荐的处理顺序

```text
1. 先检查 prohibited
2. 再判断 domain/scope
3. 识别 intent
4. 提取 required fields
5. 缺字段则 clarification
6. 判断是否需要外部能力
7. 匹配 capability
8. 检查权限和 approval
9. 生成工具计划或直接响应
10. 记录 routing decision
```

为什么先检查 prohibited：

```text
“绕过审批取消订单”
```

虽然能识别 `order.cancel`，但不能因为工具存在就执行。

## 13. 推荐分阶段学习和开发

### 阶段一：只做设计和固定命令 Router

支持：

```text
/order get <order_id>
/order cancel <order_id>
/refund create <order_id>
```

目标是先把六种状态和 CapabilityMatcher 测清楚。

### 阶段二：增加简单中文规则

支持：

```text
查询订单 1001
取消订单 1001
```

使用正则提取参数。

### 阶段三：增加知识处理器

区分：

```text
稳定规则问题 → 不使用工具
文档检索问题 → knowledge search
实时数据问题 → business tool
```

### 阶段四：增加审批、权限和审计

对 write 类能力增加：

- 用户确认；
- 角色权限；
- routing decision 记录；
- tool intent/result 记录。

## 14. 必须测试的场景

1. Context 已有答案，不调用工具；
2. 稳定业务规则，不调用工具；
3. 实时订单数据，选择 get_order；
4. 修改订单，选择写工具；
5. 缺少 order_id，进入 clarification；
6. 业务内但缺工具，返回 capability_missing；
7. 天气问题，返回 out_of_scope；
8. 绕过审批，返回 prohibited；
9. 工具存在但当前 Scope 不可见，拒绝；
10. Router 产生未知工具，转换成 unknown_tool；
11. 同一 Capability 有多个工具时按优先级选择；
12. 超出 Turn/Tool Call 预算时停止。

## 15. 最终建议

现在不建议继续增加大量业务工具，也不建议把 Router 逻辑塞进 `loop.py`。

正确分层是：

```text
RequestRouter           判断业务范围和 Intent
CapabilityMatcher       判断需要什么能力、能力是否存在
Policy                  判断是否允许执行
Agent Loop              执行已经形成的消息和工具计划
ToolRegistry            提供实际业务能力
```

对当前无大模型项目，下一步最值得先完成的不是新的算术工具，而是先定义：

1. 产品支持哪些 Domain；
2. 每个 Domain 支持哪些 Intent；
3. 每个 Intent 是否需要实时数据或副作用；
4. 对应哪些 Capability；
5. 缺参数、缺能力、超范围和禁止请求分别怎样响应。

> **工具不负责理解用户问题，Agent Loop 也不负责判断业务范围。Pi 和 DeepSeek Harness 都把语义规划交给模型，再由工具系统强制能力边界；你不接大模型时，就需要用 RequestRouter/RuleBasedPlanner 明确替代模型的这一部分。**

---

## 对话 26：第三方大模型 API 接入的目录和架构规划

### 用户问题

假如要编写一个接入大模型的功能，目前只使用第三方 API 登录，并写到配置文件中：

1. 支持第三方 API HTTP 访问，并支持指定模型；
2. 检查 Git 文件，禁止提交真实配置文件，但允许提交名为 `.example` 的配置示例；
3. 先规划架构，说明创建什么文件夹、叫什么名字、放在哪里。

### 助手回答

## 结论

建议第一版明确支持：

```text
OpenAI-compatible Chat Completions HTTP API
+ API Key/Bearer 鉴权
+ 配置中指定模型
+ 流式文本
+ Tool Call
```

不要一开始宣称支持“所有第三方 API”。不同厂商的请求结构、鉴权方式和流协议可能完全不同，应先通过 Provider Adapter 抽象支持 OpenAI 兼容协议，以后再增加 DeepSeek、Anthropic 等独立适配器。

本轮只做架构规划，不创建 Provider 代码，也不会要求用户在对话中提供真实 API Key。

## 1. 当前 Git 状态检查

`agent-loop` 本身是独立 Git 仓库：

```text
D:/0715/git_codex/20260825/agent-loop
```

当前 `.gitignore` 只忽略：

- `__pycache__`；
- 虚拟环境；
- 构建目录；
- IDE 文件等。

它目前没有忽略 `config/*.toml`。

当前 `config/agent.toml` 还是未跟踪文件，没有进入 Git 历史。因此现在增加忽略规则仍然来得及。

## 2. 配置文件策略

根据“真实配置禁止提交，`.example` 可以提交”的要求，建议：

```text
config/agent.toml                 本地真实运行预算配置，不提交
config/agent.toml.example         示例配置，允许提交

config/providers.toml             本地真实 API 配置，不提交
config/providers.toml.example     示例配置，允许提交
```

也就是说，不只 API Key 配置，所有实际运行配置都采用：

```text
真实文件：      *.toml
示例文件：      *.toml.example
```

项目首次运行前执行：

```bat
copy config\agent.toml.example config\agent.toml
copy config\providers.toml.example config\providers.toml
```

然后只在本地 `providers.toml` 中填写真实 API Key。

## 3. 推荐目录结构

全部放在现有项目：

```text
D:/0715/git_codex/20260825/agent-loop
```

建议结构：

```text
agent-loop/
├─ .gitignore
├─ pyproject.toml
├─ config/
│  ├─ README.md
│  ├─ agent.toml.example
│  └─ providers.toml.example
│
├─ examples/
│  ├─ minimal_text.py
│  ├─ basic_usage.py
│  └─ real_model_usage.py
│
├─ src/pi_agent_loop/
│  ├─ agent.py
│  ├─ loop.py
│  ├─ types.py
│  ├─ config.py
│  ├─ testing.py
│  │
│  └─ providers/
│     ├─ __init__.py
│     ├─ settings.py
│     ├─ factory.py
│     ├─ errors.py
│     ├─ serialize.py
│     ├─ sse.py
│     ├─ translate.py
│     └─ openai_compatible.py
│
└─ tests/
   ├─ test_provider_settings.py
   ├─ test_provider_factory.py
   ├─ test_provider_serialize.py
   ├─ test_provider_sse.py
   ├─ test_openai_compatible_provider.py
   └─ test_config_gitignore.py
```

真实的以下文件只存在于开发者本机，不进入 Git：

```text
config/agent.toml
config/providers.toml
```

## 4. 每个文件的职责

### `config/README.md`

说明：

- 怎样从 `.example` 复制真实配置；
- 哪个文件可以提交；
- 哪个文件禁止提交；
- 不要把 API Key 粘贴到 Issue、日志、测试或聊天中；
- API Key 泄露后的撤销和轮换办法。

### `config/providers.toml.example`

只保存安全占位值，不保存任何可用密钥：

```toml
[active]
profile = "third_party"

[profiles.third_party]
protocol = "openai_chat_completions"
base_url = "https://api.example.com/v1"
endpoint = "/chat/completions"
auth_type = "bearer"
api_key = "REPLACE_WITH_YOUR_API_KEY"
model = "REPLACE_WITH_PROVIDER_MODEL_ID"
request_timeout_seconds = 60
connect_timeout_seconds = 10
stream = true
allow_insecure_http = false
```

`model` 就是发送给第三方 API 的实际模型 ID，例如厂商文档中规定的名称。

### `providers/settings.py`

定义冻结配置对象，例如：

```python
ProviderSettings
ProviderProfile
```

负责：

- 使用 `tomllib` 加载 `providers.toml`；
- 严格校验字段；
- 拒绝未知字段；
- 拒绝空 API Key；
- 拒绝 `.example` 中的占位 API Key；
- 校验 URL、Timeout、模型名称和协议类型；
- 默认禁止远程明文 HTTP。

### `providers/factory.py`

根据配置创建：

```text
Model
StreamFn/Provider
```

例如：

```python
settings = load_provider_settings("config/providers.toml")
model, provider = create_provider(settings)

agent = Agent(
    model=model,
    stream_fn=provider.stream,
)
```

这样示例程序不需要知道具体 Provider 类。

### `providers/errors.py`

定义结构化错误：

```text
ProviderConfigError
ProviderAuthenticationError
ProviderRateLimitError
ProviderTimeoutError
ProviderHTTPError
ProviderProtocolError
ProviderModelNotFoundError
```

所有错误信息都必须隐藏：

- API Key；
- Authorization Header；
- 私密自定义 Header。

### `providers/serialize.py`

把 Agent 内部消息转换成 OpenAI-compatible 请求格式：

```text
systemPrompt
messages
tools
```

转换为：

```text
model
messages[]
tools[]
stream=true
```

同时负责把 ToolResultMessage 转换成 `role=tool` 消息。

### `providers/sse.py`

只负责解析 Server-Sent Events：

```text
data: {...}

data: [DONE]
```

必须支持：

- 一个 JSON 被拆成多个网络 chunk；
- 一个 chunk 中包含多个 SSE event；
- 空行和注释行；
- `[DONE]`；
- 无效 JSON；
- 连接中途断开。

### `providers/translate.py`

把第三方响应转换成当前 Agent Loop 已经认识的：

```text
AssistantMessageEventStream
```

需要产生：

```text
start
text_start/text_delta/text_end
toolcall_start/toolcall_delta/toolcall_end
done/error
```

同时处理：

- `finish_reason=stop`；
- `finish_reason=tool_calls`；
- `finish_reason=length`；
- usage；
- 某些兼容 API 返回的 `reasoning_content`。

### `providers/openai_compatible.py`

负责真正的 HTTP 请求：

```text
POST {base_url}{endpoint}
Authorization: Bearer <api_key>
Content-Type: application/json
```

建议使用异步 `httpx.AsyncClient`，因此 `pyproject.toml` 后续增加：

```toml
dependencies = ["httpx>=0.27,<1"]
```

它负责：

- 连接 Timeout；
- 请求 Timeout；
- CancellationToken；
- HTTP 状态码分类；
- 流式读取响应；
- 调用 SSE 和 Translate 层；
- 关闭连接和 Client。

它不应该负责 Agent Turn、Tool 执行或业务 Router。

### `examples/real_model_usage.py`

唯一会访问真实网络的示例：

```text
读取 providers.toml
→ 创建 Provider 和 Model
→ 创建 Agent
→ 用户输入
→ 第三方模型流式回答
```

第一版可以先演示纯文本，再加入计算工具，验证真实模型是否能产生 Tool Call。

## 5. 为什么这样拆分

这是结合 Pi 和 DeepSeek Harness 源码得出的结构。

### 借鉴 Pi

Pi 将低层 Agent Loop 和模型 Provider 分开：

```text
Agent Loop 依赖 StreamFn
具体 Provider 负责把协议转换成统一事件流
```

相关源码：

- `pi/packages/agent/src/stream-fn.ts`
- `pi/packages/ai/src/providers/openai.ts`
- `pi/packages/ai/src/providers/deepseek.ts`
- `pi/packages/ai/src/api/openai-responses.ts`
- `pi/packages/ai/src/api/transform-messages.ts`
- `pi/packages/ai/src/auth/credential-store.ts`

当前 Python 项目已经拥有 `StreamFn` 和 `AssistantMessageEventStream`，因此不需要改写 Agent Loop，只需要增加 Provider Adapter。

### 借鉴 DeepSeek Harness

DeepSeek Harness 将协议适配拆成：

```text
adapter.ts
serialize.ts
sse.ts
translate.ts
```

相关源码：

- `deepseek-harness/packages/llm/llm-deepseek/src/adapter.ts`
- `deepseek-harness/packages/llm/llm-deepseek/src/serialize.ts`
- `deepseek-harness/packages/llm/llm-deepseek/src/sse.ts`
- `deepseek-harness/packages/llm/llm-deepseek/src/translate.ts`

这比把 HTTP、SSE、消息转换全部写在一个 Python 文件中更容易测试。

## 6. 推荐调用链

```text
real_model_usage.py
  ↓
load_provider_settings()
  ↓
create_provider()
  ├─ Model(id=配置中的 model)
  └─ OpenAICompatibleProvider.stream
       ↓
     serialize_request()
       ↓
     HTTP POST
       ↓
     parse_sse()
       ↓
     translate_chunks()
       ↓
     AssistantMessageEventStream
       ↓
     现有 Agent Loop
       ↓
     Tool Call / Tool Result / 下一 Turn
```

## 7. Git 忽略规则规划

`.gitignore` 增加：

```gitignore
# 本地运行配置可能包含 API Key，禁止提交
/config/*.toml

# 允许提交脱敏后的示例配置
!/config/*.toml.example

# 其他常见本地密钥文件
.env
.env.*
!.env.example
```

虽然 `*.toml.example` 本身不会被 `*.toml` 命中，但保留 `!` 规则可以清楚表达安全意图。

### 重要限制

`.gitignore` 只能阻止未跟踪文件。

如果真实配置曾经被 Git 跟踪，必须执行：

```bash
git rm --cached config/providers.toml
git rm --cached config/agent.toml
```

如果真实 API Key 曾经提交，还必须：

```text
立即在第三方平台撤销旧 Key
生成新 Key
必要时清理 Git 历史
```

仅从最新提交删除文件不能消除历史中的秘密。

## 8. Git 安全测试

新增 `tests/test_config_gitignore.py`，验证：

```text
config/providers.toml 必须被忽略
config/agent.toml 必须被忽略
config/providers.toml.example 不得被忽略
config/agent.toml.example 不得被忽略
```

还要在开发完成后手动检查：

```bash
git check-ignore -v config/providers.toml
git check-ignore -v config/agent.toml
git status --short
```

提交前再检查：

```bash
git diff --cached --name-only
```

其中绝不能出现：

```text
config/providers.toml
config/agent.toml
```

## 9. HTTP 和鉴权边界

“第三方 API 登录”第一版建议明确解释为：

```text
API Key/Bearer Token 鉴权
```

不是浏览器账号密码登录，也不是 OAuth 网页授权。

第一版支持：

```text
auth_type = "bearer"
Authorization: Bearer <api_key>
```

以后可扩展：

```text
auth_type = "api_key_header"
X-API-Key: <api_key>
```

如果厂商必须使用 OAuth、请求签名或临时 Token，应增加独立认证适配器，不能把逻辑硬编码到 OpenAI Provider 中。

### HTTP 与 HTTPS

技术上都是 HTTP 客户端访问，但远程 API 默认必须使用：

```text
https://
```

只有本机测试服务才允许：

```text
http://127.0.0.1
http://localhost
```

远程明文 HTTP 可能泄露 API Key，因此必须由 `allow_insecure_http=true` 显式开启，而且应该打印醒目警告。

## 10. 测试规划

### 配置测试

- 正常加载 profile；
- 指定模型进入 `Model.id`；
- 缺少 API Key；
- 使用示例占位 Key；
- 缺少模型名；
- 未知配置字段；
- 非法 URL；
- 远程明文 HTTP；
- 非法 Timeout。

### 序列化测试

- System Prompt；
- User/Assistant 消息；
- Tool Schema；
- Assistant Tool Call；
- Tool Result；
- 中文和 Unicode。

### SSE 测试

- 文本分片；
- JSON 跨网络 chunk；
- 多 event 同 chunk；
- `[DONE]`；
- Tool Call 参数分片；
- 中途断开；
- 非法 JSON。

### HTTP 测试

使用 `httpx.MockTransport`，不连接真实网络：

- 请求 URL；
- Authorization Header；
- 配置的 model 是否进入 JSON；
- 401、404、429、500；
- Timeout；
- Cancellation；
- API Key 不出现在错误和日志中。

### Agent 集成测试

```text
模拟 OpenAI-compatible SSE
→ Provider 翻译事件
→ Agent Loop 收到 Tool Call
→ 执行 add
→ Tool Result 回灌
→ 第二次模型请求
→ 最终回答
```

真实 API 测试只做手动或显式 opt-in，不进入默认单元测试。

## 11. 分阶段开发顺序

### 第一阶段：Git 安全边界

1. 修改 `.gitignore`；
2. 将当前 `agent.toml` 迁移为 `agent.toml.example`；
3. 新增 `providers.toml.example`；
4. 新增 `config/README.md`；
5. 验证真实配置被 Git 忽略。

### 第二阶段：配置加载

1. 实现 `ProviderSettings`；
2. 严格校验配置；
3. 实现 Provider Factory；
4. 增加配置测试。

### 第三阶段：HTTP Provider

1. 消息和工具序列化；
2. HTTP 请求；
3. SSE 解析；
4. 事件翻译；
5. 错误分类；
6. 取消和资源关闭。

### 第四阶段：示例和集成测试

1. `real_model_usage.py`；
2. 纯文本真实调用；
3. 真实 Tool Call；
4. MockTransport 完整集成测试；
5. 更新中文 README。

## 12. 验收标准

完成后应满足：

- 可从配置指定 `base_url`；
- 可从配置指定模型 ID；
- API Key 通过 HTTP Header 发送；
- 支持流式文本；
- 支持 Tool Call；
- 支持 Provider Timeout 和 Agent Cancellation；
- 401、429、5xx 等错误结构化；
- API Key 不出现在事件、日志、异常或测试快照中；
- `config/*.toml` 无法被普通 Git 提交；
- `config/*.toml.example` 可以提交；
- 默认测试不访问真实网络；
- 现有 ScriptedProvider 和 30 项测试继续通过。

## 13. 实现前需要确认的第三方协议

真正编码前，只需要确认以下公开信息，不要提供真实 API Key：

1. 是否兼容 OpenAI `/chat/completions`；
2. Base URL；
3. Endpoint 路径；
4. 模型 ID；
5. 鉴权是 Bearer 还是 `X-API-Key`；
6. 是否支持 `stream=true`；
7. 是否支持 Tool Calling。

如果没有额外说明，建议默认按以下方案开发：

```text
OpenAI-compatible /chat/completions
Bearer API Key
SSE streaming
Tool Calling
模型从 providers.toml 指定
```

> **Provider 应当放在 `src/pi_agent_loop/providers/`，真实配置放在被 Git 忽略的 `config/*.toml`，仓库只提交 `config/*.toml.example`。不要把 HTTP、API Key、SSE 解析写进 `loop.py`。**

---

## 对话 27：确认第三方模型 API 协议参数

### 用户问题

确认第三方 API：

- 兼容 OpenAI `/chat/completions`；
- Base URL 写入配置文件；
- Endpoint 是 `/chat/completions`；
- 模型 ID 自行写入配置文件；
- 使用 Bearer 鉴权；
- 支持 `stream=true`；
- 支持 Tool Calling。

### 助手回答

协议参数已经确认，下一阶段不再存在接口协议方面的阻塞。

## 1. 最终协议定义

```text
协议：       OpenAI-compatible Chat Completions
请求方法：   POST
Base URL：   从 config/providers.toml 读取
Endpoint：   /chat/completions
模型 ID：    从 config/providers.toml 读取
鉴权：       Authorization: Bearer <api_key>
流式响应：   stream=true
工具调用：   支持 OpenAI Tool Calling
```

最终请求地址使用安全拼接：

```python
base_url.rstrip("/") + "/" + endpoint.lstrip("/")
```

例如：

```text
base_url = https://third-party.example.com/v1
endpoint = /chat/completions
```

最终得到：

```text
https://third-party.example.com/v1/chat/completions
```

这样可以避免出现双斜线。

## 2. 最终配置格式

仓库提交：

`config/providers.toml.example`

```toml
[active]
profile = "third_party"

[profiles.third_party]
protocol = "openai_chat_completions"

# 可以包含 /v1，但不要包含 /chat/completions。
base_url = "https://api.example.com/v1"
endpoint = "/chat/completions"

# Bearer Token 鉴权。
auth_type = "bearer"
api_key = "REPLACE_WITH_YOUR_API_KEY"

# 填写第三方平台真实模型 ID。
model = "REPLACE_WITH_PROVIDER_MODEL_ID"

stream = true
connect_timeout_seconds = 10
request_timeout_seconds = 60
allow_insecure_http = false
```

本地使用：

`config/providers.toml`

该文件由用户复制 `.example` 后自行填写：

```bat
copy config\providers.toml.example config\providers.toml
```

真实 API Key 只能写入被 Git 忽略的 `providers.toml`，不能写入 `.example`。

## 3. HTTP 请求格式

Provider 将发送：

```http
POST /v1/chat/completions HTTP/1.1
Authorization: Bearer <真实 API Key>
Content-Type: application/json
Accept: text/event-stream
```

请求体：

```json
{
  "model": "配置中的模型ID",
  "messages": [],
  "tools": [],
  "tool_choice": "auto",
  "stream": true
}
```

当当前 Agent 没有工具时：

- 可以不发送 `tools`；
- 可以不发送 `tool_choice`。

这样兼容性更好。

## 4. Tool Calling 映射

发送给第三方 API 的工具：

```json
{
  "type": "function",
  "function": {
    "name": "add",
    "description": "计算两个数的和",
    "parameters": {
      "type": "object",
      "properties": {}
    }
  }
}
```

第三方流式响应中的：

```text
choices[0].delta.tool_calls[index]
```

将按 `index` 累积：

- `id`；
- `function.name`；
- `function.arguments` JSON 字符串片段。

结束后转换成当前 Agent Loop 使用的：

```python
{
    "type": "toolCall",
    "id": "call_xxx",
    "name": "add",
    "arguments": {"a": 2, "b": 3},
}
```

如果 `arguments` 最终不是合法 JSON，应返回结构化 `provider_protocol_error`，不能猜测或执行工具。

## 5. 流式响应映射

SSE：

```text
data: {"choices":[{"delta":{"content":"你"}}]}

data: {"choices":[{"delta":{"content":"好"}}]}

data: [DONE]
```

转换成：

```text
start
text_start
text_delta("你")
text_delta("好")
text_end
done
```

结束原因映射：

```text
stop          → stopReason="stop"
tool_calls    → stopReason="toolUse"
length        → stopReason="length"
content_filter→ stopReason="error"
```

## 6. API Key 安全规则

必须保证：

- 只进入 Authorization Header；
- 不写入 `Model`；
- 不写入 Agent Message；
- 不写入事件；
- 不打印到控制台；
- 不出现在 HTTP 错误信息；
- 不进入测试快照；
- 不提交 Git。

401 错误只能显示类似：

```text
第三方 API 鉴权失败，请检查本地 providers.toml 中的 API Key。
```

不能把 Header 或 Key 原文放进异常。

## 7. 已冻结的目录规划

```text
src/pi_agent_loop/providers/
├─ __init__.py
├─ settings.py
├─ factory.py
├─ errors.py
├─ serialize.py
├─ sse.py
├─ translate.py
└─ openai_compatible.py
```

Provider 继续实现现有 `StreamFn` 契约，不修改 Agent Loop 的核心职责。

## 8. 开发顺序

下一阶段可以直接按以下顺序实施：

```text
第一步：配置和 Git 安全
第二步：ProviderSettings 和 Factory
第三步：消息/工具序列化
第四步：SSE 解析
第五步：HTTP Provider
第六步：真实模型示例
第七步：MockTransport 和 Agent 集成测试
```

## 9. 当前尚不支持的协议

第一版明确不包含：

- OpenAI `/responses`；
- Anthropic `/messages`；
- OAuth 网页登录；
- AWS/Azure 请求签名；
- 非 OpenAI 格式 Tool Calling；
- 图片、音频和文件上传。

这些以后通过新的 Provider Adapter 扩展，不修改 Agent Loop。

## 10. 当前状态

```text
协议需求          已确认
目录规划          已确认
配置格式          已确认
Git 安全策略      已确认
HTTP 实现         尚未开始
SSE 实现          尚未开始
真实 API 调用     尚未开始
```

> **现在可以进入第一阶段开发：先建立 `.example` 配置和 Git 安全边界，再实现 ProviderSettings。**

---

## 对话 28：开始开发 OpenAI-compatible 第三方模型 Provider

### 用户问题

开始开发。

### 助手回答

已完成 OpenAI-compatible 第三方模型 Provider 第一版开发。

## 1. 已实现功能

当前支持：

```text
POST /chat/completions
Base URL 从 TOML 配置读取
模型 ID 从 TOML 配置读取
Authorization: Bearer <api_key>
stream=true
SSE 流式文本
OpenAI Tool Calling
Tool Result 回灌后的下一 Turn
Provider Timeout
Agent CancellationToken
```

## 2. 新增 Provider 目录

新增：

```text
agent-loop/src/pi_agent_loop/providers/
├─ __init__.py
├─ settings.py
├─ factory.py
├─ errors.py
├─ serialize.py
├─ sse.py
├─ translate.py
└─ openai_compatible.py
```

职责：

- `settings.py`：读取并严格校验 Provider TOML；
- `factory.py`：创建配置指定的 `Model` 和 Provider；
- `errors.py`：鉴权、限流、超时、HTTP、模型不存在和协议错误；
- `serialize.py`：内部 Message/Tool 转换为 Chat Completions JSON；
- `sse.py`：处理任意 UTF-8、CRLF 和网络 Chunk 边界；
- `translate.py`：把 OpenAI Delta 转换为 `AssistantMessageEventStream`；
- `openai_compatible.py`：异步 HTTP、Bearer、Timeout、取消和资源关闭。

Provider 继续实现已有 `StreamFn` 契约，没有把 HTTP 或 API Key 逻辑放进 `loop.py`。

## 3. 配置与 Git 安全

新增可提交文件：

```text
config/README.md
config/agent.toml.example
config/providers.toml.example
```

真实本地文件：

```text
config/agent.toml
config/providers.toml
```

`.gitignore` 已增加：

```gitignore
/config/*.toml
```

实际验证：

```text
config/agent.toml                 被忽略
config/providers.toml             被忽略
config/agent.toml.example         允许提交
config/providers.toml.example     允许提交
```

`git ls-files` 检查确认真实配置当前没有被 Git 跟踪。

## 4. Provider 配置

首先复制：

```bat
cd D:\0715\git_codex\20260825\agent-loop
copy config\agent.toml.example config\agent.toml
copy config\providers.toml.example config\providers.toml
```

然后编辑本地 `config/providers.toml`：

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

`base_url` 可以包含 `/v1`，但不能再包含 `/chat/completions`。

## 5. 配置安全校验

`ProviderSettings` 会拒绝：

- 缺失字段；
- 未知字段；
- 空 API Key；
- `.example` 占位 API Key 或模型 ID；
- 非 Bearer 鉴权；
- 非 `/chat/completions` Endpoint；
- `stream=false`；
- 非法 Timeout；
- 远程明文 HTTP；
- Base URL 重复包含 `/chat/completions`。

localhost 和 `127.0.0.1` 仍允许 HTTP，便于本地 Mock Server 测试。

API Key 字段设置了 `repr=False`，打印配置对象不会显示密钥。

## 6. 请求序列化

支持转换：

- System Prompt；
- User Message；
- Assistant Text；
- Assistant Tool Call；
- Tool Result；
- Tool JSON Schema。

没有工具时不会发送：

```text
tools
tool_choice
```

有工具时发送：

```json
{
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "add",
        "description": "...",
        "parameters": {}
      }
    }
  ],
  "tool_choice": "auto"
}
```

## 7. SSE 和 Tool Call

SSE 解析支持：

- 中文 UTF-8 被拆到多个 Chunk；
- CRLF 被拆到多个 Chunk；
- 一个 Chunk 包含多个 SSE Event；
- 多行 `data:`；
- `[DONE]`；
- 非法 JSON；
- `[DONE]` 前断开连接。

Tool Call 按 `index` 累积：

```text
id
function.name
function.arguments
```

`arguments` 只有在完整接收并成功解析为 JSON 对象后，才会交给 Agent Loop。

非法 JSON 不会猜测或执行工具。

## 8. 流式事件转换

支持转换：

```text
start
text_start/text_delta/text_end
thinking_start/thinking_delta/thinking_end
toolcall_start/toolcall_delta/toolcall_end
done/error
```

结束原因：

```text
stop           → stop
tool_calls     → toolUse
length         → length
content_filter → error
```

兼容部分第三方 API 返回的 `reasoning_content`。

## 9. HTTP 与错误处理

使用：

```toml
httpx>=0.27,<1
```

支持：

- Connection Timeout；
- Request Timeout；
- CancellationToken 主动中止 HTTP 流；
- 禁止自动 Redirect，避免 Bearer Header 被转发到其他主机；
- 401/403 → `ProviderAuthenticationError`；
- 404 → `ProviderModelNotFoundError`；
- 429 → `ProviderRateLimitError`；
- 其他非 2xx → `ProviderHTTPError`；
- 协议异常 → `ProviderProtocolError`。

错误消息不会包含 API Key、Authorization Header 或 HTTP Request 对象。

## 10. 真实模型示例

新增：

`examples/real_model_usage.py`

运行：

```bat
cd D:\0715\git_codex\20260825\agent-loop
python examples\real_model_usage.py
```

它会：

```text
读取本地 Provider 和预算配置
→ 创建配置指定的 Model
→ 注册 add/multiply
→ 访问真实第三方 API
→ 打印流式文本
→ 支持模型调用本地工具
```

缺少本地配置时会打印中文操作提示并正常退出，不输出长堆栈。

当前没有创建 `config/providers.toml`，也没有真实 API Key，因此没有进行真实收费 API 调用。

## 11. 新增测试

新增：

```text
tests/test_provider_settings.py
tests/test_provider_serialize.py
tests/test_provider_sse.py
tests/test_openai_compatible_provider.py
tests/test_config_gitignore.py
```

覆盖：

- Base URL、模型、Bearer 配置；
- `.example` 占位值；
- HTTP/HTTPS 安全；
- Factory 指定模型；
- Message/Tool 序列化；
- SSE 任意分片；
- Bearer Header；
- 文本流；
- Tool Call 参数分片；
- 401 密钥脱敏；
- CancellationToken 终止 HTTP；
- Provider → Agent → add → Tool Result → 下一 Turn；
- Git 忽略真实配置。

所有 HTTP 测试使用 `httpx.MockTransport`，没有访问真实网络。

## 12. 验证结果

```text
Ran 51 tests in 0.805s
OK
```

同时验证：

- `python -m compileall` 通过；
- `python -m pip install -e .` 通过；
- Provider 顶层导入通过；
- 原 `examples/basic_usage.py` 回归通过；
- `git diff --check` 没有内容错误；
- 真实配置没有被 Git 跟踪。

## 13. 当前明确未支持

第一版暂不支持：

- OpenAI `/responses`；
- Anthropic `/messages`；
- OAuth；
- Azure/AWS 请求签名；
- 图片、音频和文件上传；
- 非 OpenAI 格式 Tool Calling。

这些以后通过新增 Provider Adapter 实现，不修改 Agent Loop。

> **OpenAI-compatible Provider 的代码、配置安全、SSE、Tool Calling、取消、示例和 Mock 集成测试已经完成；现在只需要用户在本地被忽略的 `providers.toml` 中填写真实 Base URL、API Key 和模型 ID，即可进行首次真实 API 联调。**

---

## 对话 29：使用真实配置进行第三方模型 API 联调

### 用户问题

目前已经接入真实配置，进行真实调用测试。

### 助手回答

真实 API 联调已经完成，文本、单工具和并行双工具三条链路均成功。

## 1. 配置和 Git 安全检查

已验证本地配置：

```text
config/providers.toml 存在
配置严格校验通过
协议为 openai_chat_completions
Endpoint 为 /chat/completions
模型 ID 为 gpt-5.4
Bearer API Key 已配置，但测试过程没有显示其内容
stream=true
```

Git 检查结果：

```text
config/providers.toml 被 .gitignore 忽略
config/providers.toml 没有被 git ls-files 跟踪
```

因此真实 API Key 没有进入待提交文件列表。

## 2. 真实纯文本流测试

实际发送：

```text
Reply with exactly: REAL_API_OK
```

真实模型返回：

```text
REAL_API_OK
```

验证通过：

- Base URL 拼接正确；
- Bearer 鉴权成功；
- 配置的模型 ID 可用；
- `/chat/completions` 可用；
- `stream=true` SSE 可用；
- 文本 Delta 能正确转换并实时输出。

## 3. 真实单 Tool Call 测试

实际请求模型必须调用 `add` 计算 `2+3`。

运行记录：

```text
model = gpt-5.4
tool_events = [('add', False)]
error = None
final_role = assistant
final_content = 5
```

完整真实链路：

```text
用户请求
→ 第三方模型返回 add Tool Call
→ Provider 组装流式 arguments
→ Agent Loop 执行本地 add
→ 生成 Tool Result 5
→ 第二次真实模型请求
→ 模型最终回答 5
```

验证通过：

- Tool Schema 发送成功；
- 模型能生成 OpenAI Tool Call；
- Tool Call JSON 参数解析成功；
- 本地 add 工具执行成功；
- Tool Result 回灌成功；
- 第二个模型 Turn 成功；
- 无 Agent 错误。

## 4. 真实并行双 Tool Call 测试

实际请求模型在同一步调用：

```text
add(2, 3)
multiply(4, 5)
```

真实运行记录：

```text
model = gpt-5.4
tool_starts = ['add', 'multiply']
tool_ends = [('add', False), ('multiply', False)]
assistant_turns = 2
error = None
final_content = 2 + 3 = 5; 4 × 5 = 20.
```

验证通过：

- 模型在同一 Assistant Message 中返回两个 Tool Call；
- Provider 能按 `tool_calls[index]` 组装两个调用；
- Agent Loop 同批接受两个工具；
- add 和 multiply 都执行成功；
- 两个 Tool Result 都回灌给模型；
- 第二 Turn 正确生成综合回答。

## 5. 临时测试文件清理

真实单工具和并行工具测试使用的两个临时 Smoke Script 已删除：

```text
.tmp_real_tool_smoke.py        已删除
.tmp_real_parallel_smoke.py    已删除
```

没有把临时脚本或真实配置加入 Git。

## 6. 回归测试

真实联调后重新运行全部离线测试：

```text
Ran 51 tests
OK
```

因此真实配置没有破坏原有 ScriptedProvider、预算、Timeout、取消、SSE 和 Tool Call 测试。

## 7. 重要安全警告

当前真实 Base URL 使用的是：

```text
远程 http:// 地址
```

而不是：

```text
https://
```

配置之所以能够通过，是因为本地显式启用了：

```toml
allow_insecure_http = true
```

这意味着 Bearer API Key 和请求内容在传输层没有 TLS 加密。网络中间节点可能看到：

- Authorization Header；
- API Key；
- 用户 Prompt；
- Tool Schema；
- 模型回复。

建议立即采取以下措施：

1. 优先把 Base URL 改成厂商提供的 HTTPS 地址；
2. 如果服务只提供 HTTP，在服务前增加 Nginx/Caddy HTTPS 反向代理；
3. 或通过可信 VPN/SSH Tunnel，把本地连接地址变成 localhost；
4. 如果刚才的 HTTP 流量经过不可信网络，建议在第三方平台轮换 API Key；
5. 切换 HTTPS 后把 `allow_insecure_http` 改回 `false`。

## 8. 最终结果

| 测试项 | 结果 |
|---|---|
| 配置加载 | 通过 |
| Git 密钥隔离 | 通过 |
| Bearer 鉴权 | 通过 |
| 指定模型 | 通过 |
| 真实文本 SSE | 通过 |
| 真实单 Tool Call | 通过 |
| 真实并行 Tool Call | 通过 |
| Tool Result 回灌 | 通过 |
| 第二模型 Turn | 通过 |
| Agent 错误 | 无 |
| 51 项离线回归 | 通过 |

> **真实 API 功能已经成功接通，文本和工具闭环均可正常运行；当前唯一需要优先处理的是远程明文 HTTP 安全风险。**

---

## 对话 30：basic_usage 改为任意用户输入和真实模型，同时保留编号说明

### 用户问题

进行真实联调，删除以前在代码中预设的文本，例如固定的“请同时计算 2+3 和 4×5”。

1. 仍运行 `python examples/basic_usage.py`，但可以在命令后面添加自己的用户消息，由真实大模型分析并返回；
2. 保留 `01、02、03`、`最终回答`、`为什么调用` 等显示模式。

### 助手回答

已完成改造并通过真实 API 联调。

## 1. `basic_usage.py` 已移除预设模型

已删除：

- `ScriptedProvider`；
- 固定 `Model(id="demo-model")`；
- 预设 `first_response`；
- 预设 `final_response`；
- 固定的 `await agent.prompt("请同时计算 2+3 和 4×5")`；
- 固定“模型调用两次”的说明。

现在 `basic_usage.py` 会读取：

```text
config/agent.toml
config/providers.toml
```

然后通过真实 OpenAI-compatible Provider 调用配置中的模型。

## 2. 命令后面可以直接添加用户消息

使用引号：

```bat
python examples/basic_usage.py "请解释什么是 Agent Loop"
```

需要工具时：

```bat
python examples/basic_usage.py "请使用加法工具计算 8+9"
```

多个未加引号的参数也会合并成一条用户消息，但包含空格或特殊符号时仍建议使用引号。

## 3. 不传参数时支持交互输入

```bat
python examples/basic_usage.py
```

程序显示：

```text
请输入用户消息：
```

输入内容后再请求真实模型。

## 4. 真实模型自行决定是否使用工具

新的 System Prompt 是通用规则：

```text
先理解用户的真实请求
只有请求适合当前工具时才调用工具
工具执行后读取 Tool Result 再回答
没有合适工具时使用模型自身能力回答
不得编造已经执行了外部操作
```

目前可用工具仍然是：

```text
add
multiply
```

普通解释、写作或知识问题可以不调用工具；加法和乘法问题由真实模型决定是否调用对应工具。

## 5. 编号显示完整保留

保留：

```text
[01] Agent 开始处理本次用户任务。
[02] 开始第 1 轮：准备请求真实模型。
[03] 收到本次用户输入：<用户实际输入>
[04] 用户消息已经加入本次模型上下文。
[05] 真实模型开始生成一条回复。
...
```

还保留：

```text
最终回答：...
模型调用次数：...
为什么调用 ... 次：...
```

其中用户消息、工具名称、参数、工具结果和模型回答全部来自本次真实运行，不再是代码预设值。

## 6. “为什么调用”改成动态生成

如果模型不使用工具：

```text
模型调用次数：1
为什么调用 1 次：
  第 1 次：模型判断不需要工具，直接生成回答。
```

如果模型调用一次工具：

```text
模型调用次数：2
为什么调用 2 次：
  第 1 次：模型决定调用工具：add。
  第 2 次：模型读取此前消息和工具结果，生成回答。
```

如果模型规划更多轮，也会根据真实 assistant 历史逐轮显示，不再写死为两次。

为此给 `OpenAICompatibleProvider` 增加了只记录次数、不记录 Key 或正文的：

```python
provider.call_count
```

## 7. 真实无工具联调

实际运行：

```bat
python examples/basic_usage.py "Reply with exactly: DYNAMIC_INPUT_OK"
```

真实输出关键部分：

```text
[01] Agent 开始处理本次用户任务。
[02] 开始第 1 轮：准备请求真实模型。
[03] 收到本次用户输入：Reply with exactly: DYNAMIC_INPUT_OK
[05] 真实模型开始生成一条回复。
[06] 模型回复完成：DYNAMIC_INPUT_OK
[08] Agent 结束本次运行。

最终回答：DYNAMIC_INPUT_OK
模型调用次数：1
为什么调用 1 次：
  第 1 次：模型判断不需要工具，直接生成回答。
```

验证真实模型可以根据命令行动态输入直接回答。

## 8. 真实工具联调

实际运行：

```bat
python examples/basic_usage.py "Use add to calculate 8 plus 9, then answer with the result."
```

真实输出关键部分：

```text
[06] 模型要求调用工具：add。
[07] 开始执行工具 add，参数为 {'a': 8, 'b': 9}。
[08] 工具 add 报告中间进度：加法工具正在计算……
[09] 工具 add 执行成功，结果为 17。
[11] add 的结果已经写回模型上下文：17
[13] 开始第 2 轮：准备请求真实模型。
[15] 模型回复完成：17

最终回答：17
模型调用次数：2
为什么调用 2 次：
  第 1 次：模型决定调用工具：add。
  第 2 次：模型读取此前消息和工具结果，生成回答。
```

验证完成：

```text
命令行动态消息
→ 真实模型分析
→ 真实 Tool Call
→ 本地 add
→ Tool Result 回灌
→ 第二次真实模型调用
→ 最终回答
```

## 9. 保留配置和安全提示

运行开头仍显示：

- Provider 名称；
- 模型 ID；
- 本次用户消息；
- 已注册工具；
- Turn/Tool Call/并行预算。

如果检测到远程明文 HTTP，还会显示：

```text
安全警告：当前使用远程明文 HTTP，Bearer 和消息没有 TLS 保护。
```

不会显示 API Key。

## 10. 新增测试

新增：

`tests/test_basic_usage.py`

覆盖：

- 命令行多个词合并为一条消息；
- “为什么调用”根据真实 assistant 历史生成；
- `basic_usage.py` 不再包含 ScriptedProvider 和固定计算任务。

同时给 HTTP Provider 测试增加 `call_count` 断言。

当前结果：

```text
Ran 54 tests
OK
```

同时修复了一个旧测试使用墙钟耗时阈值导致的 Windows Timer 波动问题，改为直接断言 `agent_end` listener 运行时 Agent 仍处于忙碌状态，测试语义更准确。

## 11. 文档更新

已更新 `agent-loop/README.md`：

- `basic_usage.py` 改为真实模型示例；
- 增加命令行动态输入说明；
- 删除“当前仍是假模型”等过时内容；
- 更新动态模型调用原因；
- 测试数更新为 54。

> **现在可以用 `python examples/basic_usage.py "你的问题"` 把任意用户消息交给真实模型；原来的编号事件、最终回答、模型调用次数和调用原因说明全部保留。**

---

## 对话 31：用户是否需要自己指定调用工具

### 用户问题

直接问问题时，是否调用工具难道不是 Agent 自己分析的吗？为什么还需要用户指定工具？请说明原因。

### 助手回答

## 结论

**用户正常使用时不需要指定工具名称。是否调用工具应该由真实大模型根据用户问题、System Prompt 和工具 Schema 自己决定。**

之前测试中使用：

```text
Use add to calculate 8 plus 9
```

不是产品使用要求，而是一次“定向联调测试”。它的目的是强制真实模型走 Tool Calling 链路，确认以下基础设施可用：

```text
模型是否收到工具 Schema
→ 是否会返回 tool_calls
→ arguments 是否能正确组装
→ Agent Loop 是否执行 add
→ Tool Result 是否能回灌
→ 模型是否会生成最终回答
```

如果只问普通算术题，模型有可能直接心算并回答。那样虽然答案正确，却无法证明 Tool Calling 代码已经接通，所以联调时才明确要求使用 `add`。

## 1. 当前正常运行方式

用户可以直接说：

```bat
python examples/basic_usage.py "8 加 9 等于多少？"
```

不需要说：

```text
请调用 add
```

当前实际调用链是：

```text
用户自然语言问题
  ↓
真实模型读取问题
  ↓
同时读取 add/multiply 的名称、说明和参数 Schema
  ↓
模型自行判断
  ├─ 不需要工具 → 直接回答
  └─ 需要工具   → 返回 Tool Call
                    ↓
                  Agent Loop 执行工具
                    ↓
                  Tool Result 回灌模型
                    ↓
                  模型生成最终回答
```

## 2. 当前代码确实使用自动选择

`serialize.py` 在有工具时向 `/chat/completions` 发送：

```json
{
  "tools": [...],
  "tool_choice": "auto"
}
```

`tool_choice="auto"` 的意思是：

```text
模型可以直接回答
也可以选择一个或多个工具
```

不是用户选择，也不是 Python 示例提前选择。

`basic_usage.py` 的 System Prompt 也明确要求：

```text
先理解用户的真实请求
只有请求适合当前工具时才调用工具
没有合适工具时使用模型自身能力回答
```

## 3. 谁在“自己分析”

这里要区分 Model 和 Agent Loop：

```text
真实大模型
负责理解问题、推理、规划和决定是否生成 Tool Call
```

```text
Agent Loop
负责执行模型生成的 Tool Call、校验参数、控制预算、处理 Timeout、回灌结果
```

Agent Loop 本身不是另一个大脑，不会再次理解自然语言。

所以“Agent 自己分析”更准确地说是：

```text
Agent 内部的真实大模型负责分析
Agent Loop 负责可靠执行分析结果
```

## 4. 为什么简单算术可能不调用工具

大模型自身通常知道：

```text
8 + 9 = 17
```

在 `tool_choice="auto"` 下，它可能认为无需调用 add，直接回答 17。

这不是程序错误，而是模型的规划选择。

如果产品规则要求“凡是算术都必须经过工具”，就不能只写“可以使用 add”，而应设置更强规则：

```text
凡是 add/multiply 能完成的计算，必须调用相应工具，禁止直接心算。
```

还可以使用：

```text
tool_choice="required"
```

但不能全局永久设为 required，否则用户问“请解释 Agent Loop”时，模型也会被迫调用一个无关工具。

更合理的生产做法是：

```text
Router 先判断请求类型
计算请求       → 要求使用计算工具
普通知识请求   → tool_choice=auto 或不提供工具
真实业务数据   → 必须使用对应业务工具
```

## 5. 哪些情况可以让模型自行选择

适合 `tool_choice="auto"`：

- 普通知识问答；
- 总结、改写和解释；
- 可选的辅助计算；
- 失败后允许模型直接说明原因的任务。

例如：

```text
“解释什么是递归”
→ 不需要 add/multiply
→ 模型直接回答
```

```text
“计算 12345×67890”
→ 模型可以判断 multiply 更可靠
→ 返回 multiply Tool Call
```

## 6. 哪些情况不能只相信模型自由选择

以下情况应由产品策略强制使用工具：

- 查询当前订单状态；
- 查询账户余额；
- 查询实时库存；
- 查询天气或市场价格；
- 创建、修改、删除真实业务数据；
- 支付、退款、审批、发送消息。

例如用户问：

```text
“订单 1001 现在到哪里了？”
```

模型训练知识不可能知道当前订单状态。产品层必须规定：

```text
必须调用 orders.read_current
没有工具 → capability_missing
工具失败 → 明确失败
禁止根据语言模型知识猜测
```

这也是之前规划 `RequestRouter + CapabilityMatcher + Policy` 的原因。

## 7. Pi 和 DeepSeek Harness 也是这样

Pi 的做法：

```text
把当前 active tools 交给模型
→ 模型决定直接回答还是返回 Tool Call
→ Agent Loop 只负责执行和回灌
```

DeepSeek Harness 的做法：

```text
把当前 Scope 允许的工具交给模型
→ 模型负责语义规划
→ Tool Runtime、Policy、Approval 负责强制边界
```

二者都不是让用户必须说工具名。

工具名通常属于 Agent 内部实现细节，普通用户不应该需要知道。

## 8. “为什么调用”显示的含义

示例最后的：

```text
为什么调用 2 次
```

不是在要求用户选择工具，而是在运行结束后解释已经发生的事实：

```text
第 1 次模型请求产生了 add Tool Call
第 2 次模型请求读取 add 结果并回答
```

这是运行审计和教学说明，不是用户输入要求。

## 9. 最终结论

正常使用：

```bat
python examples/basic_usage.py "你的自然语言问题"
```

用户只需要描述目标，不需要知道 `add`、`multiply` 或其他工具名。

之前显式写 `Use add`，仅用于真实 API 的 Tool Calling Smoke Test，确保工具链一定被覆盖。

> **用户负责说“想完成什么”，模型负责判断“是否需要工具以及调用哪个工具”，Agent Loop 负责把模型的决定安全、可靠地执行。对于实时数据和有副作用的业务操作，还需要 Router/Policy 强制工具边界，不能只依赖模型自由选择。**

---

## 对话 32：自然语言算术没有调用工具的根本原因

### 用户问题

用户提供截图：直接运行 `python examples/basic_usage.py "请问答 8+9 等于多少"`，模型没有调用工具，而是直接回答 `8+9=17`。用户说明加法、除法只是自己业务能力的类比，认为直接测试没有通过，要求仔细检查原因。

### 助手回答

## 结论

截图中的现象不是 HTTP、SSE 或 Tool Calling 协议损坏，而是当前“工具选择策略”没有达到业务要求。

更准确地说：

```text
Tool Calling 基础设施已经接通
但“业务请求必须使用工具”的强制路由尚未实现
```

用户的测试结论是正确的：如果验收标准是“模型识别到这类请求后必须调用业务工具”，那么当前实现确实没有通过这个验收标准。

## 1. 从截图可以确认什么

截图显示：

```text
[05] 真实模型开始生成一条回复。
[06] 模型回复完成：8 + 9 = 17
[07] 第 1 轮结束。
[08] Agent 结束本次运行。

模型调用次数：1
```

没有出现：

```text
模型要求调用工具：add
开始执行工具 add
Tool Result 写回模型上下文
```

这证明第三方模型返回的是普通文本 Assistant Message，没有返回 `tool_calls`。

Agent Loop 收到普通文本后只能正常结束。它不会把文本中的 `8+9` 再解析一遍并自行创建 Tool Call。

## 2. 第一项根本原因：当前写死的是 `tool_choice="auto"`

文件：

`src/pi_agent_loop/providers/serialize.py`

当前代码：

```python
if raw_tools:
    payload["tools"] = [_serialize_tool(tool) for tool in raw_tools]
    payload["tool_choice"] = "auto"
```

`auto` 的准确含义是：

```text
允许模型调用工具
不要求模型必须调用工具
```

因此注册了 `add`，只表示模型拥有这个选项，并不代表每个加法问题都必须调用它。

## 3. 第二项根本原因：System Prompt 不是强制规则

文件：

`examples/basic_usage.py`

当前 Prompt 的关键语句是：

```text
只有在请求适合当前工具时才调用工具
没有合适工具时使用模型自身能力回答
```

“只有适合时才调用”表达的是：

```text
不合适时禁止调用
```

但它没有表达：

```text
只要适合，就必须调用
```

因此模型仍然拥有自由裁量权。

## 4. 第三项根本原因：8+9 对模型太简单

模型自身已经可以计算：

```text
8 + 9 = 17
```

它可能判断：

```text
我自己能可靠回答
没有必要增加 Tool Call 和下一轮模型请求
```

于是直接生成文本。

这是 `tool_choice=auto` 下完全合法的模型行为。

不同模型、不同第三方代理、不同温度甚至同一模型的不同运行，都可能做出不同选择。因此不能把 auto 当成确定性业务路由。

## 5. 第四项根本原因：ToolRegistry 不是 RequestRouter

当前 `ToolRegistry` 只负责：

```text
按名称注册工具
防止重名
向模型暴露工具 Schema
模型返回 toolCall 后查找并执行
```

它不负责：

```text
理解自然语言 Intent
判断是否需要实时数据
判断某业务能力是否必须调用
判断能力是否缺失
阻止模型绕过工具直接回答
```

所以：

```text
工具已注册
≠ 模型一定调用
≠ 业务路由已经实现
```

## 6. 第五项根本原因：当前没有强制结果检查

当前流程是：

```text
模型直接返回文本
→ Agent Loop 认为这一轮合法完成
→ agent_end
```

系统没有执行以下检查：

```text
这个 Intent 是否被标记为 requires_tool？
要求的 Capability 是什么？
Assistant 是否真的返回了对应 Tool Call？
如果没有，是否应该拒绝直接答案并重新规划？
```

因此模型可以绕过本应强制的业务工具。

## 7. 为什么之前定向 Tool Call 测试通过

此前真实联调使用：

```text
Use add to calculate 8 plus 9. Do not calculate it yourself.
```

真实结果确实产生：

```text
add Tool Call
→ add 执行
→ Tool Result 回灌
→ 第二次模型请求
```

这证明：

- 工具 Schema 已成功发送；
- 第三方 API 支持 Tool Calling；
- SSE Tool Call 参数组装正确；
- Agent Loop 工具执行正确；
- Tool Result 回灌正确。

但这个测试只能证明“模型被明确要求时能够调用工具”，不能证明“模型能按照产品业务规则稳定地自动路由”。

之前将这两种通过混为一谈是不够严谨的。

## 8. 加法和乘法只是业务能力类比时，问题更严重

假设未来工具不是 add，而是：

```text
orders.read_current
refunds.create
inventory.reserve
customer.update
```

如果仍只使用 `tool_choice="auto"`，模型可能对以下请求直接生成看似合理的文字：

```text
“订单 1001 当前到哪里了？”
“帮我退款。”
“库存还有多少？”
```

但模型自身没有真实业务数据，也没有真的修改系统。

这会产生业务幻觉：

```text
语言上回答成功
实际上没有查询或执行任何业务操作
```

因此真实业务不能只靠模型的 auto 自由选择。

## 9. 当前项目缺少的真正模块

需要补充：

```text
RequestRouter
CapabilityMatcher
ToolUsePolicy
RequiredToolCallGuard
```

建议流程：

```text
用户请求
  ↓
RequestRouter 识别 Domain/Intent
  ↓
CapabilityMatcher 得到 required_capabilities
  ↓
ToolUsePolicy 决定本次模式
  ├─ no_tool
  ├─ auto
  ├─ required
  └─ named_tool
  ↓
只向模型提供当前 Intent 需要的工具
  ↓
模型响应
  ↓
RequiredToolCallGuard 检查模型是否真的调用所需能力
  ├─ 已调用 → Agent Loop 执行
  └─ 未调用 → 拒绝直接答案、重新规划或返回 policy_violation
```

## 10. `tool_choice` 应支持四种策略

当前只有：

```text
auto
```

需要扩展为：

```text
none
```

本次禁止使用工具。

```text
auto
```

模型自由决定。

```text
required
```

模型必须至少调用一个当前可见工具，但由模型选择具体工具。

```json
{
  "type": "function",
  "function": {"name": "orders_read_current"}
}
```

强制调用指定工具。

注意：不能给所有请求全局设置 `required`，否则用户问普通知识问题时也会被迫调用无关工具。

正确做法是 Router 按 Intent 选择策略。

## 11. System Prompt 只能加强，不能作为唯一保障

可以把计算演示规则改成：

```text
凡是 add 或 multiply 能完成的计算，必须调用对应工具，禁止直接心算。
```

这会提高调用概率，但仍不是安全边界。模型可能不遵守 Prompt，第三方模型也可能对 Tool Choice 的实现不完全一致。

生产系统必须在模型响应后进行 Host 级检查。

## 12. 业务版本应该怎样分类

例如：

```text
“解释什么是退款”
→ in_scope_no_tool
→ 不需要业务工具
```

```text
“查询退款单 1001 当前状态”
→ required_capability=refunds.read_current
→ 必须调用对应工具
```

```text
“创建退款”
→ required_capability=refunds.create
→ 必须调用工具并经过 Approval
```

```text
“查询退款状态”，但没有查询工具
→ capability_missing
→ 禁止模型编造状态
```

```text
“查询天气”，产品是退款 Agent
→ out_of_scope
```

## 13. 与 Pi、DeepSeek Harness 的关系

Pi 的低层 Agent Loop 同样不会分析用户文本后强制工具。它把工具 Schema 交给模型，模型返回 Tool Call 后才执行。产品若要强制路由，应放在 Session/Host/Extension 层。

DeepSeek Harness 也由模型生成 Tool Call，但它通过 Scope、Policy、Approval、Guard 和可见工具集合强化业务边界。

当前 Python 项目已经实现的是接近二者的“低层执行循环”，但尚未实现完整产品层的业务 Router 和 Required Tool Guard。

## 14. 准确的故障判断

| 检查项 | 状态 |
|---|---|
| Base URL/Bearer/SSE | 正常 |
| 工具 Schema 发送 | 正常 |
| 定向 Tool Call | 正常 |
| 工具执行与回灌 | 正常 |
| `tool_choice=auto` | 正常工作，但不强制 |
| 自然语言自动业务路由 | 尚未实现 |
| 必须使用工具的 Host 校验 | 尚未实现 |
| 能力缺失与超范围处理 | 尚未实现 |

## 15. 下一步建议

不应该只把 System Prompt 改得更强，也不应该简单地全局设置 `tool_choice="required"`。

建议下一阶段正式开发：

1. `ToolChoicePolicy`：支持 none/auto/required/named；
2. `CapabilityRegistry`：工具名和业务能力分离；
3. `RequestRouter`：识别业务 Intent 和 Required Capability；
4. `RequiredToolCallGuard`：模型直接回答时强制拒绝或重试；
5. `capability_missing/out_of_scope/prohibited` 结构化结果；
6. 使用模拟业务工具测试，而不是继续把简单算术当作最终业务架构。

README 已补充警告：`tool_choice="auto"` 只代表允许调用，不保证一定调用。

> **截图证明的不是 Tool Calling 链路坏了，而是当前项目只有“模型可选工具”，还没有“业务规则强制工具”。对于真实业务，必须把“模型觉得要不要调用”升级为“Router/Policy 判断是否必须调用，并由 Guard 验证模型确实调用”。**

---

## 对话 33：开发 RequestRouter、CapabilityRegistry 和 Required Tool Guard

### 用户问题

开始下一阶段开发。

### 助手回答

下一阶段第一版已经完成：现在项目不仅支持 `tool_choice="auto"`，还增加了确定性业务路由、Capability 匹配和模型绕过工具时的强制 Guard。

## 1. 新增业务路由目录

```text
src/pi_agent_loop/routing/
├─ __init__.py
├─ types.py
├─ scope.py
├─ capabilities.py
├─ router.py
├─ guard.py
└─ routed_agent.py
```

职责：

```text
RequestRouter           判断 Scope、Domain、Intent 和必要字段
CapabilityRegistry      将业务能力映射到实际 AgentTool
ToolChoicePolicy        选择 none/auto/required/named
RequiredToolCallGuard   验证模型确实调用要求的能力和参数
RoutedAgent             把产品路由层和低层 Agent 组合起来
```

低层 `loop.py` 仍然不负责理解业务自然语言。

## 2. Tool Choice 已扩展为四种模式

原来只有：

```text
auto
```

现在支持：

```text
none       本 Turn 禁止工具
auto       模型自由选择
required   本 Turn 必须调用至少一个可见工具
named      强制指定 function name
```

Named 示例：

```python
ToolChoicePolicy("named", "get_order_status").to_openai()
```

转换成：

```json
{
  "type": "function",
  "function": {"name": "get_order_status"}
}
```

Provider 会在发送 HTTP 前校验：

- named 工具是否属于当前可见工具；
- 没有工具时不能使用 required/named；
- 策略对象结构是否合法。

## 3. CapabilityRegistry 已实现

工具名与业务能力分离：

```python
capabilities.register(
    get_order_status_tool,
    capabilities={"orders.read_current"},
    domain="orders",
    operation="read",
    risk="low",
)
```

Router 寻找：

```text
orders.read_current
```

而不是把：

```text
get_order_status
```

写死在业务 Intent 中。

同一 Capability 可以替换工具实现，也可以按 priority 选择实现。

## 4. Business Scope TOML 已实现

新增可提交示例：

`config/business_scope.toml.example`

本地文件：

`config/business_scope.toml`

该真实文件同样被：

```gitignore
/config/*.toml
```

忽略，不会进入 Git。

订单示例规则：

```text
解释订单状态
→ order.explain_status
→ no_tool

查询订单当前状态
→ order.get_status
→ required orders.read_current

取消订单
→ order.cancel
→ required orders.cancel

绕过审批/伪造订单
→ prohibited

其他领域
→ out_of_scope
```

订单规则只是业务架构示例，不是用户的正式业务定义。

## 5. Router 现在产生六种正式结果

```text
in_scope_no_tool
in_scope_tool_ready
in_scope_need_clarification
in_scope_capability_missing
out_of_scope
prohibited
```

处理顺序：

```text
Prohibited
→ Intent
→ Required Fields
→ Required Capabilities
→ Capability Match
→ Tool Choice Policy
```

例如：

```text
查询订单当前状态
```

缺少订单号时：

```text
in_scope_need_clarification
missing_fields=["order_id"]
```

不会让模型自己猜订单号。

## 6. RequiredToolCallGuard 已实现

当 Intent 规定必须使用工具，但模型直接回答时：

```text
required_tool_call_missing
```

模型调用当前 Intent 不允许的工具：

```text
tool_not_allowed_for_intent
```

模型没有调用满足 Required Capability 的工具：

```text
required_capability_not_called
```

Router 提取到：

```text
order_id=1001
```

但模型调用：

```text
order_id=9999
```

则返回：

```text
required_tool_arguments_mismatch
```

违规 Tool Call 会在进入 Agent Loop 前变成结构化 Assistant Error，因此不会执行错误工具。

## 7. Guard 不只依赖 Prompt

Required/None/Named 模式会先缓冲模型事件：

```text
模型输出
→ Guard 验证
→ 通过后才交给 Agent Loop/UI
```

如果模型违反策略，它生成的直接回答不会先被 UI 当成正式结果显示。

这比单纯写强 System Prompt 更可靠。

## 8. Required 第一轮后自动切回 Auto

业务查询流程：

```text
第 1 Turn：tool_choice=required
→ 必须调用查询工具

工具执行并回灌

第 2 Turn：tool_choice=auto
→ 允许模型读取 Tool Result 并生成最终文本
```

为此扩展了：

```python
AgentLoopTurnUpdate.stream_options
```

否则每一轮都保持 required，模型第二轮仍会被强制调用工具，无法正常回答。

## 9. RoutedAgent 已实现

`RoutedAgent.prompt()` 先路由再决定是否调用模型：

```text
in_scope_no_tool
→ tools=[]
→ tool_choice=none
→ 调用模型回答规则问题

in_scope_tool_ready
→ 只暴露 Capability 匹配的工具
→ required/auto
→ Guard 校验

need_clarification
capability_missing
out_of_scope
prohibited
→ 不调用模型
→ 直接返回结构化产品结果
```

因此 Capability Missing 不再交给模型编造答案，也不消耗模型调用额度。

## 10. 新增模拟业务工具和示例

新增：

```text
examples/mock_order_tools.py
examples/business_routing_usage.py
```

模拟订单数据：

```text
1001 → 已发货
1002 → 待支付
1003 → 已完成
```

运行：

```bat
copy config\business_scope.toml.example config\business_scope.toml
python examples\business_routing_usage.py "查询订单 1001 当前状态"
```

注意：这是本地 Mock 数据，不连接真实订单系统。

`basic_usage.py` 仍是通用 `auto` 示例；需要强制业务规则时，应使用 `RoutedAgent` 或把相同架构接入正式业务入口。

## 11. 真实 API Required Tool 联调

真实请求：

```text
查询订单 1002 当前状态
```

真实结果：

```text
工具开始：get_order_status，参数：{'order_id': '1002'}
工具结束：get_order_status，错误：False

路由状态：in_scope_tool_ready
Domain：orders
Intent：order.get_status
Required Capabilities：['orders.read_current']
Selected Tools：['get_order_status']
Tool Policy：required
是否请求模型：True
错误代码：None
最终结果：订单 1002 当前状态：待支付。
模型调用次数：2
```

这次用户没有指定工具名称。Router 根据 Intent 强制 `orders.read_current`，模型在 required 模式下选择匹配工具。

## 12. Capability Missing 联调

请求：

```text
取消订单 1001
```

配置要求：

```text
orders.cancel
```

但当前没有取消工具，结果：

```text
路由状态：in_scope_capability_missing
是否请求模型：False
错误代码：in_scope_capability_missing
最终结果：当前缺少能力 orders.cancel
模型调用次数：0
```

没有让模型声称“已经取消”。

## 13. No-tool 联调

请求：

```text
解释什么是订单状态
```

结果：

```text
路由状态：in_scope_no_tool
Tool Policy：none
Selected Tools：[]
是否请求模型：True
模型调用次数：1
```

业务工具没有暴露给模型，模型只解释稳定概念。

## 14. 新增和增强测试

新增：

```text
tests/test_routing.py
tests/test_routed_agent.py
```

并增强：

```text
tests/test_provider_serialize.py
tests/test_config_gitignore.py
```

覆盖：

- No-tool Intent；
- Required Capability；
- 缺少字段；
- Capability Missing；
- Out of Scope；
- Prohibited；
- Capability 工具重名；
- required/named 序列化；
- 不可见 named 工具；
- 模型直接回答被 Guard 拒绝；
- 模型篡改 Router 参数被拒绝；
- Required 工具完成后下一轮切回 auto；
- Capability Missing 零模型调用；
- No-tool Intent 零业务工具暴露。

当前结果：

```text
Ran 69 tests
OK
```

`python -m compileall` 和 `git diff --check` 同样通过。

## 15. 当前边界

当前 RuleBased Router 通过 TOML 正则规则识别 Intent，优点是确定、可测试；缺点是不能理解所有自然语言表达。

接入用户真实业务前，还需要用户提供：

1. 产品 Domain；
2. Intent 清单；
3. 每个 Intent 的必要参数；
4. Required Capability；
5. 对应真实业务工具；
6. 权限和 Approval 规则。

不能直接把模拟订单工具投入生产。

> **现在项目已经从“模型可以选择工具”升级为“产品 Router 决定是否必须使用能力，模型必须按策略调用，Guard 再验证工具名称、Capability 和关键参数”。**

---

## 对话 34：business_scope.toml 是否需要逐个配置

### 用户问题

`business_scope.toml` 配置文件是怎么回事？具体路由逻辑是什么？是否需要一个个写好配置？

### 助手回答

## 先说结论

**需要定义业务 Intent，但不是为每一句用户问题写一条配置，也不是每个问题写一个工具。**

正确粒度是：

```text
每一种稳定业务意图配置一次
```

例如订单业务可能只有：

```text
order.get_status
order.cancel
order.explain_status
refund.create
refund.get_status
```

用户对同一 Intent 的很多说法，都应该映射到同一个配置：

```text
“查询订单 1001”
“帮我看看 1001 到哪了”
“1001 订单当前什么状态”
```

都应该得到：

```text
intent=order.get_status
capability=orders.read_current
```

不应该为三句话分别创建三个工具或三个 Intent。

## 1. 当前文件是什么

仓库提交的是：

```text
config/business_scope.toml.example
```

本地实际文件是：

```text
config/business_scope.toml
```

真实文件被 Git 忽略。

目前内容是助手在不知道用户真实业务的情况下，为验证架构创建的“订单 Mock 示例”。它不是通用业务，也不是用户正式产品配置。

如果用户的业务不是订单，应替换，而不是继续扩充订单示例。

## 2. 为什么需要这个文件

大模型能理解语言，但它不天然知道产品规则：

- 产品支持哪些业务；
- 哪些请求需要实时数据；
- 哪些操作会修改系统；
- 哪些能力必须审批；
- 缺少工具时是否转人工；
- 哪些请求禁止处理。

这些不能让模型自己临时发明，必须有一个产品层的权威定义。

`business_scope.toml` 的作用是保存：

```text
产品边界
Intent
必要参数
Required Capability
Tool Choice Policy
禁止规则
优先级
```

它不是保存答案，也不是保存所有自然语言问题。

## 3. 一个 Intent 配置的含义

当前示例：

```toml
[[intents]]
id = "order.get_status"
domain = "orders"
patterns = ['查询.*订单', '订单.*(?:状态|到哪|进度)']
required_fields = ["order_id"]
field_patterns = { order_id = '(?P<order_id>[A-Za-z0-9_-]*[0-9][A-Za-z0-9_-]*)' }
required_capabilities = ["orders.read_current"]
tool_policy = "required"
priority = 80
clarification_message = "请提供要查询的订单号。"
```

逐项解释：

```text
id
系统内部稳定 Intent ID。
```

```text
domain
该 Intent 属于哪个业务领域。
```

```text
patterns
当前 RuleBased Router 用来识别自然语言的正则规则。
```

```text
required_fields
执行该 Intent 必须拥有的参数。
```

```text
field_patterns
从用户文本中提取参数。
```

```text
required_capabilities
完成请求需要什么业务能力，不是具体工具名。
```

```text
tool_policy
none、auto 或 required。
```

```text
priority
多条 Intent 同时匹配时，优先使用更高值。
```

```text
clarification_message
缺参数时怎样追问用户。
```

## 4. 当前路由逻辑

源码：

```text
src/pi_agent_loop/routing/router.py
```

处理顺序：

```text
1. 检查请求是否为空
2. 按 priority 检查 prohibited
3. 按 priority 匹配 intents.patterns
4. 提取 required_fields
5. 缺字段 → need_clarification
6. 判断 tool_policy
7. 用 required_capabilities 查询 CapabilityRegistry
8. 找不到能力 → capability_missing
9. 找到能力 → 选择工具和 Tool Choice
10. 模型响应后由 Guard 再验证
```

以“查询订单 1001 当前状态”为例：

```text
没有命中 prohibited
→ 命中 order.get_status
→ 提取 order_id=1001
→ 需要 orders.read_current
→ CapabilityRegistry 找到 get_order_status
→ tool_choice=required
→ 只向模型暴露 get_order_status
→ 模型必须返回 Tool Call
→ Guard 检查工具名、Capability 和 order_id
→ 通过后 Agent Loop 执行
```

## 5. 是否需要逐个写 patterns

如果继续使用当前纯 RuleBased Router：

```text
需要为每个 Intent 写若干 patterns
```

但不是每句话一条，而是用少量规则覆盖一组表达。

例如：

```toml
patterns = [
  '查询.*订单',
  '订单.*(?:状态|进度|到哪)',
  '(?:看看|查下).*订单'
]
```

它的优点：

- 确定；
- 可测试；
- 不额外调用模型；
- 写操作边界可靠。

缺点：

- 自然语言变化很多；
- 正则会越来越复杂；
- 容易漏掉表达；
- 中文参数提取较脆弱。

所以它适合：

- 命令式接口；
- 业务范围较小；
- 高风险写操作；
- 第一版确定性验收。

## 6. 不建议最终全部依靠人工正则

对于真实产品，更推荐“混合路由”：

```text
业务配置定义权威 Intent/Capability/Policy
            +
大模型负责把自然语言映射到已有 Intent
            +
Host/Guard 负责严格验证
```

流程：

```text
用户自然语言
→ LLM Intent Classifier
→ 只能从配置中的 Intent ID 枚举选择
→ 输出结构化 JSON
→ Host 校验 Intent、字段和 Capability
→ Policy 决定 none/required/named
→ Guard 验证实际 Tool Call
```

模型可以输出：

```json
{
  "intent": "order.get_status",
  "arguments": {"order_id": "1001"},
  "confidence": 0.96
}
```

模型不能：

- 自己创建新 Intent；
- 自己创造不存在的 Capability；
- 绕过 prohibited；
- 绕过权限；
- 把 required 改成 auto。

这样既能理解大量不同说法，又保留确定性安全边界。

## 7. 即使使用大模型 Router，哪些配置仍必须人工定义

以下内容不能完全省略：

```text
Domain
Intent ID
Required Fields
Required Capability
读/写风险等级
是否需要 Approval
Out-of-scope 边界
Prohibited 规则
```

因为这些是产品规则，不是语言理解问题。

可以让代码或 OpenAPI 自动生成初稿，但最终需要业务人员确认。

## 8. 哪些内容可以不再手写很多

混合路由实现后，可以减少：

```text
大量 patterns 正则
大量同义句
复杂自然语言字段提取规则
```

可以把 `patterns` 降级成：

- 测试样例；
- 高风险快速匹配；
- 模型不可用时的 fallback；
- 明确命令的 deterministic route。

## 9. 推荐用户提供的不是 TOML，而是一张业务表

用户不需要直接手写复杂 TOML。更合理的协作方式是先提供：

| Domain | Intent | 用户要做什么 | 必要参数 | 是否实时 | 是否写操作 | Capability | 工具/API | 是否审批 |
|---|---|---|---|---|---|---|---|---|
| orders | order.get_status | 查询订单状态 | order_id | 是 | 否 | orders.read_current | 订单查询 API | 否 |
| orders | order.cancel | 取消订单 | order_id | 是 | 是 | orders.cancel | 取消订单 API | 是 |

然后由开发工作生成：

- `business_scope.toml`；
- Capability 注册代码；
- Tool Schema；
- Router 测试；
- Guard 测试；
- Approval 策略。

## 10. 三种可选方案

### 方案 A：纯规则 Router

用户需要按 Intent 配置 patterns。

适合小型、固定和高确定性业务。

### 方案 B：混合 LLM Router，推荐

用户只定义业务 Intent、Capability 和 Policy；大模型负责自然语言分类和参数提取，规则负责禁止项和 fallback。

适合用户当前目标。

### 方案 C：完全让模型自由决定

配置最少，但不能保证实时数据和写操作一定使用工具，正是截图暴露的问题，不推荐用于正式业务。

## 11. 当前订单配置应该怎样看待

当前：

```text
business_scope.toml.example
mock_order_tools.py
business_routing_usage.py
```

只是用来证明以下架构有效：

```text
No Tool
Required Tool
Capability Missing
Out of Scope
Prohibited
Guard
```

不是要求用户照着订单例子一个个填写自己的所有问题。

## 12. 推荐下一步

建议下一阶段开发 HybridModelRouter：

1. 继续保留 `business_scope.toml` 作为权威业务合同；
2. 增加模型结构化 Intent 分类；
3. 模型只能选择配置中已有的 Intent；
4. 模型提取参数后由 Host 校验；
5. 低置信度时 Clarification；
6. Prohibited 和高风险写操作仍优先走确定性规则；
7. RuleBased Router 作为 fallback 和测试基线。

> **你需要定义“系统支持哪些稳定业务能力”，但不需要把所有用户问法逐句写进配置。当前纯规则版确实需要每个 Intent 配几条模式；更合适的最终方案是让大模型负责语言映射，让配置和 Guard 负责业务权威与安全边界。**

---

## 对话 35：将复杂 business_scope.toml 简化为易懂业务配置

### 用户问题

当前 `business_scope.toml` 太复杂，很难理解，是否可以重新设计得更简单易懂？

### 助手回答

## 结论

可以，而且应该简化。

当前配置把两类内容混在一起了：

```text
业务人员关心的产品规则
+
程序员关心的正则匹配和参数提取实现
```

所以才出现：

```text
patterns
field_patterns
priority
tool_policy
required_capabilities
```

这些字段一次性全部暴露给使用者，确实不够友好。

推荐改成：

> **配置文件只描述“系统支持什么业务”；自然语言识别和参数提取交给 HybridModelRouter。**

## 1. 建议把文件改名

从：

```text
business_scope.toml
```

改为更直接的：

```text
business.toml
```

仓库提交：

```text
config/business.toml.example
```

本地运行：

```text
config/business.toml
```

## 2. 推荐的简化配置

```toml
[product]
name = "订单助手"
description = "负责订单查询、取消和订单规则说明"
allow_general_questions = false

# 业务一：查询当前订单状态
[[intents]]
id = "order.get_status"
name = "查询订单状态"
description = "查询指定订单当前的真实处理状态"
examples = [
  "查询订单 1001",
  "帮我看看订单 1001 到哪了",
  "1001 当前是什么状态"
]
required_fields = ["order_id"]
capability = "orders.read_current"
must_use_tool = true
ask_when_missing = "请提供要查询的订单号。"

# 业务二：取消订单
[[intents]]
id = "order.cancel"
name = "取消订单"
description = "取消一个尚未完成的订单"
examples = [
  "取消订单 1001",
  "帮我撤销 1001"
]
required_fields = ["order_id"]
capability = "orders.cancel"
must_use_tool = true
requires_approval = true
ask_when_missing = "请提供要取消的订单号。"

# 业务三：解释稳定业务概念
[[intents]]
id = "order.explain_status"
name = "解释订单状态"
description = "解释订单状态的业务含义，不查询具体订单"
examples = [
  "什么是订单状态",
  "解释一下已发货是什么意思"
]
required_fields = []
must_use_tool = false

# 明确禁止的业务请求
[[denied]]
name = "绕过审批"
description = "禁止绕过取消、退款等审批流程"
examples = [
  "绕过审批取消订单",
  "不经过审核直接退款"
]
message = "不能绕过业务审批流程。"
```

## 3. 比旧配置少了什么

删除：

```text
patterns
field_patterns
priority
tool_policy
```

替换关系：

| 旧字段 | 新设计 |
|---|---|
| `patterns` | `examples`，只是自然语言示例 |
| `field_patterns` | 由模型结构化提取，工具 Schema 再校验 |
| `priority` | 由 HybridModelRouter 的置信度和歧义处理决定 |
| `tool_policy=required` | `must_use_tool=true` |
| `tool_policy=none` | `must_use_tool=false` |
| `required_capabilities=[...]` | 常见情况简化为单个 `capability` |
| `clarification_message` | `ask_when_missing` |
| `general_qa` | `allow_general_questions` |

## 4. 使用者真正需要理解的字段

每个 Intent 只需要回答几个业务问题：

```text
这项业务叫什么？
用户通常会怎样说？
需要哪些参数？
是否必须访问真实系统？
对应什么业务能力？
是否需要用户审批？
缺参数时怎样追问？
```

这比编写正则表达式容易理解。

## 5. 新路由逻辑

```text
用户自然语言
  ↓
先检查确定性安全规则
  ↓
HybridModelRouter 读取 intents 的 name/description/examples
  ↓
模型只能选择已有 Intent ID
  ↓
模型输出结构化结果
  ↓
Host 校验 Intent 和 required_fields
  ↓
must_use_tool=false
  → 不暴露业务工具，直接回答

must_use_tool=true
  → 查找 capability
  → 找不到：capability_missing
  → 找到：tool_choice=required/named
  → Guard 验证 Tool Call
```

模型路由输出：

```json
{
  "intent": "order.get_status",
  "arguments": {
    "order_id": "1001"
  },
  "confidence": 0.96
}
```

## 6. 配置简单后，复杂性放到哪里

配置简单不代表业务逻辑消失，而是把技术细节放到正确位置：

```text
business.toml
只保存业务人员能理解的业务合同

HybridModelRouter
负责理解用户不同说法

Tool Schema/validate_args
负责参数类型和格式校验

CapabilityRegistry
负责 Capability 到工具实现的映射

Policy/Approval
负责权限和高风险写操作

RequiredToolCallGuard
负责防止模型绕过工具或篡改关键参数
```

## 7. 是否仍然需要逐个配置 Intent

仍然需要按“稳定业务动作”配置一次，例如：

```text
查询订单
取消订单
创建退款
查询退款
修改地址
```

但不需要配置：

```text
用户所有可能问法
每一种同义句
每一个具体订单号
每一个具体问题
```

`examples` 只需要给 2～5 个典型表达，帮助模型理解 Intent。

## 8. 多 Capability 的高级情况

大部分 Intent 一个 Capability 即可：

```toml
capability = "orders.read_current"
```

如果一个 Intent 确实需要多个能力，可以保留可选高级字段：

```toml
capabilities = [
  "orders.read_current",
  "customers.read_contact"
]
```

普通用户不需要接触这个字段，只有复杂流程才使用。

## 9. 安全规则不能完全交给模型

简化配置后，`examples` 不是正则，也不是安全边界。

以下规则仍应由 Host 确定执行：

- Capability 是否存在；
- 当前用户是否有权限；
- 是否需要 Approval；
- 写操作是否允许；
- Tool Call 参数是否与 Router 结果一致；
- 模型是否绕过 Required Tool；
- Out-of-scope 和低置信度是否追问。

## 10. 推荐的开发迁移方案

下一阶段可以这样改：

1. 新增 `config/business.toml.example`；
2. 实现 `SimpleBusinessConfig`；
3. 实现 `HybridModelRouter`；
4. 使用 Tool Calling/JSON Schema 强制模型输出 Intent；
5. 保留现有 RuleBasedRouter 作为 fallback；
6. 保留 CapabilityRegistry 和 RequiredToolCallGuard；
7. 将旧 `business_scope.toml.example` 标记为高级规则示例；
8. 更新业务示例和测试。

## 11. 为什么这是更好的折中

```text
完全手写规则
确定，但复杂、难维护

完全交给模型
简单，但业务边界不可靠

简单业务配置 + LLM 路由 + Host Guard
容易理解，同时保留安全边界
```

## 最终建议

采用：

```text
business.toml
+ HybridModelRouter
+ CapabilityRegistry
+ RequiredToolCallGuard
```

用户只需要描述“系统支持哪些业务”，不需要写复杂正则。

> **建议把当前 `business_scope.toml` 降级成高级规则配置，新增一个面向普通使用者的简化 `business.toml`。其中只保留 Intent 名称、说明、示例、必要参数、Capability、是否必须用工具和是否审批。**

---

## 对话 36：开发简化 business.toml、HybridModelRouter 和 AI 业务需求 MD

### 用户问题

开始开发简化业务配置；同时定义一个让 AI 读取的 Markdown 文件，使后续实现真实功能和工具时，AI 可以根据该文件生成可直接使用的配置。

### 助手回答

开发已经完成：新增了简化 `business.toml`、HybridModelRouter，以及 AI 在后续业务开发前必须读取的 `BUSINESS_REQUIREMENTS.md`。

## 1. AI 必读入口

新增项目规则：

```text
agent-loop/AGENTS.md
```

其中明确要求：当任务涉及真实业务功能、AgentTool、Domain、Intent、Capability、业务配置、API、数据库、Approval 或 Guard 时，AI 必须先完整读取：

```text
agent-loop/BUSINESS_REQUIREMENTS.md
```

如果需求信息不足，AI 必须先向用户提问，不允许自行编造：

- API URL；
- 数据库字段；
- 认证方式；
- 权限；
- 幂等规则；
- 业务成功结果。

## 2. BUSINESS_REQUIREMENTS.md

新增：

```text
agent-loop/BUSINESS_REQUIREMENTS.md
```

该文件是后续业务能力的唯一需求入口，包含：

```text
产品信息
Intent 清单
工具与外部系统清单
写操作与审批
禁止和超范围规则
参数规则
预期对话验收用例
business.toml 生成规则
工具实现生成规则
AI 最终输出清单
当前未完成信息
```

业务人员以后只需要修改这一个文件中的表格和说明。

AI 读取后应生成或更新：

1. `config/business.toml.example`；
2. 本地 `config/business.toml`；
3. `src/pi_agent_loop/tools/` 中的真实工具；
4. Capability 注册代码；
5. Router/Guard/Approval 测试；
6. README 运行说明。

## 3. 简化业务配置

新增：

```text
config/business.toml.example
```

本地运行文件：

```text
config/business.toml
```

真实文件继续被 Git 忽略。

配置示例：

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

不再需要：

```text
patterns
field_patterns
priority
```

## 4. SimpleBusinessConfig Loader

新增：

```text
src/pi_agent_loop/routing/simple_config.py
```

公开接口：

```python
business = load_simple_business_config("config/business.toml")
```

严格拒绝：

- 未知字段；
- 重复 Intent ID；
- 空 examples；
- `must_use_tool=true` 却没有 Capability；
- No-tool Intent 配置 Capability；
- 不使用工具却要求 Approval；
- 有必要参数却没有追问消息。

## 5. HybridModelRouter

新增：

```text
src/pi_agent_loop/routing/hybrid_router.py
```

处理流程：

```text
用户自然语言
→ 模型调用内部 select_business_intent
→ 模型只能选择配置中的 Intent ID
→ 输出 arguments/confidence/reason
→ Host 校验
→ Capability Match
→ Approval
→ ToolChoice
→ RequiredToolCallGuard
```

模型结构化输出：

```json
{
  "decision": "order.get_status",
  "arguments": {"order_id": "1001"},
  "confidence": 0.96,
  "reason": "用户正在查询具体订单状态"
}
```

模型不能创建新 Intent，也不能修改配置中的 `must_use_tool` 和 `requires_approval`。

## 6. Hybrid 安全边界

已经实现：

- 明确 Denied 示例在调用模型前确定性阻止；
- 分类失败时 fail-closed；
- 低置信度进入 Clarification；
- 参数只能来自用户文本；
- 只保留 Intent 声明的 Required Fields；
- Capability Missing 不调用回答模型；
- 写操作即使有工具也先返回 `in_scope_approval_required`；
- Approval 前不会执行写工具；
- RequiredToolCallGuard 继续检查工具、Capability 和关键参数。

## 7. RoutedAgent 支持异步 Router

`RoutedAgent` 原来只支持同步 RuleBasedRouter，现在同时支持：

```text
同步 RequestRouter
异步 HybridModelRouter
```

所以产品入口可以先调用分类模型，再进入回答模型和业务工具循环。

## 8. 高级旧配置保留

```text
business.toml
推荐；简单；由 HybridModelRouter 理解自然语言

business_scope.toml
高级；包含正则；由 RuleBasedRouter 确定性匹配
```

旧配置没有删除，可以用于：

- 高风险快速规则；
- 明确命令；
- 模型不可用时 fallback；
- 确定性测试基线。

## 9. 业务示例已迁移

`examples/business_routing_usage.py` 已改为读取：

```text
config/business.toml
```

并使用：

```text
HybridModelRouter
CapabilityRegistry
RoutedAgent
RequiredToolCallGuard
```

## 10. 真实 API Hybrid 路由联调

用户自然语言：

```text
劳驾帮我看看编号 1003 的单子走到哪里了
```

该句没有原样写在 examples 中，真实模型仍然成功分类：

```text
路由状态：in_scope_tool_ready
Domain：order
Intent：order.get_status
Required Capabilities：['orders.read_current']
Selected Tools：['get_order_status']
Tool Policy：required
路由来源：model
路由置信度：0.96
是否需要审批：False
错误代码：None
最终结果：订单 1003 当前状态：已完成。
模型调用次数：3
```

三次模型调用分别是：

```text
第 1 次：Intent 分类和参数提取
第 2 次：required 模式生成业务 Tool Call
第 3 次：读取 Tool Result 并生成最终回答
```

## 11. Approval Gate

简化配置支持：

```toml
requires_approval = true
```

即使 Capability 和工具已经存在，RoutedAgent 也不会执行，而是返回：

```text
in_scope_approval_required
```

当前已完成“审批前阻止执行”，但尚未实现用户确认后恢复执行的持久 Approval 工作流。

## 12. 新增测试

新增：

```text
tests/test_simple_business_config.py
tests/test_hybrid_router.py
tests/test_business_requirements.py
```

覆盖：

- 简化配置不含正则；
- 配置矛盾严格拒绝；
- 自由自然语言映射到已有 Intent；
- No-tool Intent；
- Capability Missing；
- 缺必要字段；
- 低置信度；
- Denied 前置阻止；
- Approval Required；
- Hybrid Router → Required Tool → Final Answer；
- AGENTS.md 强制读取业务需求；
- 需求 MD 必要章节完整。

当前结果：

```text
Ran 82 tests
OK
```

同时通过：

- `python -m compileall`；
- `git diff --check`；
- 简化配置 Loader 导入；
- 真实配置 Git 隔离；
- 真实 API Hybrid 路由联调。

## 13. 当前需要用户后续填写的内容

`BUSINESS_REQUIREMENTS.md` 中订单内容仍是教学示例。正式业务前需要替换：

```text
真实产品名称和范围
真实 Intent 清单
真实 API/数据库文档
认证方式
参数格式
权限规则
Approval 流程
幂等和重试规则
审计要求
```

在这些信息补齐前，订单工具只能保持 Mock。

> **以后新增真实功能或工具时，只需先完善 `BUSINESS_REQUIREMENTS.md`；项目规则会要求 AI 读取它，再生成经过 Loader 校验、可直接使用的 business.toml、工具、Capability 注册和测试。**

---

## 对话 37：删除 business_scope.toml 和旧 RuleBasedRouter 冗余代码

### 用户问题

`business_scope.toml` 目前还能否使用？如果不能使用就删除，并在不影响其他功能的情况下删除对应代码；仍有需要的代码应搬到新位置，避免代码冗余。

### 助手回答

已完成检查和清理。

## 1. 检查结论

旧 `business_scope.toml` 在技术上仍可以被旧 `RuleBasedRouter` 单独加载，但当前真实业务入口已经完全迁移到：

```text
business.toml
→ SimpleBusinessConfig
→ HybridModelRouter
→ RoutedAgent
```

旧路由没有被 `business_routing_usage.py`、HybridModelRouter 或真实运行链路调用，也没有实现自动 fallback。

因此继续保留会造成：

- 两份业务配置；
- 两个 Loader；
- 两套路由语义；
- 两组 Intent 类型；
- 文档无法明确哪个是权威入口；
- 后续修改需要同步两套测试。

所以本次选择删除，而不是保留一个实际上没有接入的“备用方案”。

## 2. 已删除文件

```text
config/business_scope.toml.example
本地 config/business_scope.toml
src/pi_agent_loop/routing/scope.py
src/pi_agent_loop/routing/router.py
tests/test_routing.py
```

实际文件检查确认以上路径均已不存在。

## 3. 当前唯一业务配置

现在只保留：

```text
config/business.toml.example
本地 config/business.toml
```

业务需求来源：

```text
BUSINESS_REQUIREMENTS.md
```

运行链路：

```text
BUSINESS_REQUIREMENTS.md
→ business.toml
→ load_simple_business_config()
→ HybridModelRouter
→ CapabilityRegistry
→ RoutedAgent
→ RequiredToolCallGuard
```

不再存在两份配置之间的选择问题。

## 4. 必要错误类型已搬迁

旧 `scope.py` 中还被简化配置使用的错误类型已经搬到：

```text
src/pi_agent_loop/routing/errors.py
```

新名称：

```python
BusinessConfigError
```

`simple_config.py`、公开导出、业务示例和测试均已切换到新错误类型。

没有为了删除旧 Loader 而复制错误实现。

## 5. 公共类型已清理

从 `routing/types.py` 删除旧规则专用类型：

```text
BusinessScope
IntentDefinition
ProhibitedDefinition
```

继续保留当前链路需要的：

```text
RequestDecision
ToolChoicePolicy
ToolGuardViolation
RoutedPromptResult
```

## 6. 公共 API 已清理

从：

```text
src/pi_agent_loop/routing/__init__.py
src/pi_agent_loop/__init__.py
```

移除：

```text
RequestRouter
BusinessScope
BusinessScopeConfigError
load_business_scope
```

新增/保留：

```text
BusinessConfigError
HybridModelRouter
load_simple_business_config
SimpleBusinessConfig
CapabilityRegistry
RoutedAgent
RequiredToolCallGuard
```

实际导入检查：

```text
HybridModelRouter           存在
BusinessConfigError         存在
RequestRouter               不存在
load_business_scope         不存在
```

## 7. RoutedAgent 已解除旧 Router 依赖

`RoutedAgent` 原来类型注解直接引用 `RequestRouter`。

现在改为最小协议：

```python
class RouterLike(Protocol):
    def route(self, user_text: str) -> Any: ...
```

因此它继续支持：

- 当前异步 `HybridModelRouter`；
- 测试用 FixedRouter；
- 未来其他符合契约的 Router。

不再依赖已删除的旧实现。

## 8. Guard 测试没有丢失

旧 `test_routed_agent.py` 原本为了得到路由决定而依赖 RuleBasedRouter。

现在改用测试专用：

```text
FixedRouter
```

它只返回结构化 `RequestDecision`，从而更准确地隔离测试：

- Required Tool 缺失；
- 模型篡改参数；
- Required 后切回 Auto；
- Capability Missing；
- No-tool 工具隔离；
- CapabilityRegistry 重名保护。

CapabilityRegistry 的重名测试已从删除的旧测试文件迁移到该文件，没有丢失。

## 9. Git 安全测试已同步

`tests/test_config_gitignore.py` 已删除旧配置路径断言，只保留当前真实配置：

```text
config/business.toml      必须忽略
config/business.toml.example 允许提交
```

本地真实 `business.toml` 和 `providers.toml` 仍然没有被 Git 跟踪。

## 10. 文档已清理

已更新：

```text
README.md
config/README.md
```

删除：

- 高级正则配置说明；
- 两套 Loader 并存说明；
- RuleBasedRouter fallback 说明；
- 已删除文件的目录树；
- 已删除测试的清单。

当前文档明确：

```text
项目只有 business.toml 一份业务配置
```

历史对话文档仍保留此前设计过程，作为复习记录，不代表当前代码仍包含旧实现。

## 11. 当前目录

```text
src/pi_agent_loop/routing/
├─ __init__.py
├─ capabilities.py
├─ errors.py
├─ guard.py
├─ hybrid_router.py
├─ routed_agent.py
├─ simple_config.py
└─ types.py
```

配置目录：

```text
config/
├─ README.md
├─ agent.toml.example
├─ providers.toml.example
└─ business.toml.example
```

## 12. 验证结果

删除 7 项只属于旧 RuleBasedRouter 的测试，并把 1 项通用 CapabilityRegistry 测试迁移到当前测试文件后：

```text
Ran 76 tests
OK
```

同时通过：

- `python -m compileall`；
- `git diff --check`；
- 全项目旧标识搜索；
- 当前公开 API 导入检查；
- 真实配置 Git 隔离检查。

## 13. 真实 API 回归

清理后再次执行真实业务请求：

```text
查询订单 1001 当前状态
```

结果：

```text
路由状态：in_scope_tool_ready
Intent：order.get_status
Required Capability：orders.read_current
Selected Tool：get_order_status
Tool Policy：required
路由来源：model
路由置信度：0.99
工具执行：成功
最终结果：订单 1001 当前状态：已发货。
模型调用次数：3
```

说明删除旧配置和旧 Router 没有影响当前 Hybrid 路由、Required Tool、工具执行和最终回答。

> **当前只保留“BUSINESS_REQUIREMENTS.md → business.toml → HybridModelRouter”一条业务链路；旧 business_scope 配置、Loader、RuleBasedRouter、专用类型和测试已经完整删除，必要能力已迁移，没有影响现有功能。**

---

## 对话 38：当前是否支持自然语言多要求梳理后交给 Agent Loop

### 用户问题

目前是否已经支持使用自然语言，由 AI 梳理其中可能包含的多个要求，然后再交给 Agent Loop？

### 助手回答

## 结论

**目前只完成了“自然语言 → 单个 Intent”的 AI 梳理，还没有完成“自然语言 → 多任务计划 → 按依赖执行”的完整能力。**

准确状态：

| 能力 | 当前状态 |
|---|---|
| 读取自然语言 | 已完成 |
| 理解不同说法 | 已完成 |
| 映射一个 Intent | 已完成 |
| 提取一个 Intent 的参数 | 已完成 |
| 置信度和追问 | 已完成 |
| 一条请求拆成多个 Intent | 尚未完成 |
| 多任务依赖关系 | 尚未完成 |
| 条件任务 | 尚未完成 |
| 多写操作统一审批 | 尚未完成 |
| 多任务结果汇总 | 尚未完成 |

## 1. 当前已经有的自然语言 AI 梳理

当前：

```text
HybridModelRouter
```

确实会让真实模型读取用户自然语言。

例如配置 examples 中没有原句：

```text
劳驾帮我看看编号 1003 的单子走到哪里了
```

模型仍然可以输出：

```json
{
  "decision": "order.get_status",
  "arguments": {"order_id": "1003"},
  "confidence": 0.96,
  "reason": "用户正在查询具体订单状态"
}
```

然后 Host 再进行 Capability、Approval 和 Guard 校验。

所以当前不是关键词 Router，而是真实模型参与自然语言理解。

## 2. 当前为什么只支持一个要求

`hybrid_router.py` 中内部路由工具的输出结构是单数：

```json
{
  "decision": "一个 Intent",
  "arguments": {},
  "confidence": 0.96,
  "reason": "..."
}
```

不是：

```json
{
  "tasks": [
    {"intent": "..."},
    {"intent": "..."}
  ]
}
```

对应代码最终只生成一个：

```text
RequestDecision
```

`RoutedAgent.prompt()` 也只处理一次 Route Decision。

因此用户说：

```text
查询订单 1001，同时取消订单 1002
```

当前分类模型可能：

- 只选择查询；
- 只选择取消；
- 因为歧义降低置信度；
- 返回 Out of Scope/Clarification。

它不会稳定地产生两个可独立验证的任务。

## 3. Agent Loop 本身可以执行多个 Tool Call

低层 Agent Loop 已经支持：

```text
一条 Assistant Message
→ 多个 Tool Call
→ 按 max_parallel_tools 并发
→ 按原始顺序回灌多个 Tool Result
```

此前真实联调已经成功执行：

```text
add(2,3)
multiply(4,5)
```

因此瓶颈不是 Agent Loop，而是 Agent Loop 之前缺少安全的多任务 Planner。

## 4. basic_usage 和业务 Hybrid 路径的区别

### basic_usage

```text
tool_choice=auto
模型可以直接理解多个要求并生成多个 Tool Call
```

优点是简单。

缺点是：

- 没有明确 Intent 列表；
- 不能保证所有业务要求都被识别；
- 可能漏掉其中一项；
- 不能可靠处理 Capability Missing；
- 不能安全处理写操作和审批。

### HybridModelRouter

```text
自然语言 → 一个受配置约束的 Intent
```

优点是安全、可验证。

缺点是当前只支持单 Intent。

所以目前在“自由但不安全”和“安全但单任务”之间，还缺少一个多任务规划层。

## 5. 下一步需要的模块

建议增加：

```text
HybridRequestPlanner
PlanValidator
PlanExecutor
ResultSynthesizer
```

目录：

```text
src/pi_agent_loop/planning/
├─ __init__.py
├─ types.py
├─ hybrid_planner.py
├─ validator.py
├─ executor.py
└─ synthesizer.py
```

## 6. 多任务计划结构

模型不应直接生成任意工具名，而应输出配置中已有 Intent：

```json
{
  "tasks": [
    {
      "task_id": "task_1",
      "intent": "order.get_status",
      "arguments": {"order_id": "1001"},
      "depends_on": [],
      "condition": null,
      "confidence": 0.97
    },
    {
      "task_id": "task_2",
      "intent": "order.cancel",
      "arguments": {"order_id": "1002"},
      "depends_on": [],
      "condition": null,
      "confidence": 0.94
    }
  ],
  "summary": "查询一个订单，并取消另一个订单",
  "unresolved_questions": []
}
```

模型只能选择 `business.toml` 中存在的 Intent。

## 7. 更复杂的条件任务

例如：

```text
查询订单 1001；如果已经发货，就查询物流；同时取消订单 1002。
```

应拆成：

```text
task_1：查询订单 1001

task_2：查询订单 1001 的物流
依赖 task_1
条件：task_1.status == 已发货

task_3：取消订单 1002
需要 Approval
```

这需要：

- `depends_on`；
- 条件表达式白名单；
- 拓扑排序；
- 写操作审批；
- 失败传播；
- 最终汇总。

不能让模型输出任意 Python 条件代码。

## 8. 推荐执行策略

### 第一阶段：静态独立多任务

先支持：

```text
一个自然语言请求
→ 多个互不依赖 Intent
→ 全批预检
→ 只读任务并行
→ 写任务要求 Approval
→ 最终汇总
```

暂不支持“如果……就……”条件。

### 第二阶段：依赖任务

增加：

```text
depends_on
拓扑排序
前序失败传播
```

### 第三阶段：受限条件任务

条件只能使用受控结构：

```json
{
  "field": "status",
  "operator": "equals",
  "value": "已发货"
}
```

禁止模型生成 Python、SQL 或 Shell 条件。

## 9. 全批预检非常重要

执行多任务前必须先检查全部任务：

```text
Intent 是否存在
参数是否完整
Capability 是否存在
权限是否允许
哪些任务需要 Approval
总 Tool Call 是否超预算
任务之间是否冲突
```

例如：

```text
查询订单 1001，同时取消订单 1002
```

不能先查询成功，再发现取消能力不存在或没有审批。

对包含写操作的计划，默认应先完成整批预检和审批，再执行任何副作用。

## 10. 怎样交给 Agent Loop

推荐分两类：

### 无依赖只读任务

Planner 生成并验证多个任务后，可以让模型在一个 Assistant Turn 中生成多个 Tool Call，交给现有 Agent Loop 并行执行。

### 有依赖或写操作任务

由 `PlanExecutor` 分阶段驱动 Agent Loop：

```text
第一层无依赖任务
→ 等待结果
→ 判断下一层
→ Approval
→ 执行写任务
→ 最终汇总
```

不要把依赖、审批和工作流全部塞进 `loop.py`。

## 11. 必须防止的风险

多任务 Planner 必须防止：

- 模型创造不存在的 Intent；
- 模型漏掉用户明确要求；
- 重复写操作；
- 模型修改 Router 已确认参数；
- 未审批先执行写工具；
- 一部分写操作执行后整批才失败；
- 循环依赖；
- 任意条件代码执行；
- 任务数量绕过 `max_tool_calls`。

## 12. 当前准确结论

```text
自然语言 AI 理解：有
单 Intent 结构化路由：有
Agent Loop 多 Tool Call：有
安全的多 Intent 拆解：没有
多任务计划执行器：没有
```

> **当前 HybridModelRouter 能理解一句自然语言并整理成一个业务 Intent；Agent Loop 也能执行多个 Tool Call，但两者之间还缺少把“一句话中的多个要求”安全拆成任务列表、全批校验、审批、调度和汇总的 HybridRequestPlanner。**

---

## 对话 39：复合计算是否属于多 Intent 和多任务计划执行

### 用户问题

用户运行：

```text
python examples/basic_usage.py "帮我计算（2+3）x(5+6)"
```

真实模型先并行调用两个 add，再调用 multiply，最后回答 55。用户询问这种情况是否属于多 Intent 拆解和多任务计划执行器，并要求解释 Intent。

### 助手回答

## 结论

这个例子属于：

```text
单个 Intent
+
多个计算子任务
+
子任务依赖
+
模型跨 Turn 动态规划
+
Agent Loop 分批执行 Tool Call
```

它不属于严格意义上的：

```text
多个业务 Intent 拆解
+
显式结构化 PlanExecutor
```

之前说“当前没有多任务计划执行”过于绝对。更准确的说法应该是：

```text
当前已有隐式、动态的多步骤工具规划与执行
当前缺少显式、可审计、可预检的多 Intent 任务计划系统
```

## 1. 什么是 Intent

Intent 中文可以理解为：

```text
用户意图
用户最终想达成的目标
一种稳定业务动作
```

例如：

```text
用户：查询订单 1001 当前状态
Intent：order.get_status
```

```text
用户：取消订单 1002
Intent：order.cancel
```

```text
用户：帮我计算（2+3）×（5+6）
Intent：calculator.evaluate_expression
```

Intent 不是每一次工具调用。

## 2. 这个计算只有一个 Intent

用户最终目标只有一个：

```text
计算整个表达式的值
```

可以把它命名为：

```text
calculator.evaluate_expression
```

为了完成这个 Intent，模型拆出了三个子任务：

```text
subtask_1：2 + 3 = 5
subtask_2：5 + 6 = 11
subtask_3：5 × 11 = 55
```

其中：

```text
subtask_1 和 subtask_2 相互独立
subtask_3 依赖前两个结果
```

所以这是“一个 Intent 内部的多步骤执行”。

## 3. Intent、Task、Tool、Tool Call 的区别

| 层级 | 示例 | 含义 |
|---|---|---|
| Domain | calculator | 业务领域 |
| Intent | calculator.evaluate_expression | 用户最终目标 |
| Task/Subtask | 计算 2+3 | 为实现目标拆出的步骤 |
| Capability | calculator.add | 系统具备的稳定能力 |
| Tool | add | Capability 的 Python 实现 |
| Tool Call | add(a=2,b=3) | 某一次具体调用 |
| Turn | 第一次请求模型 | 一次模型请求及后续工具批次 |

因此：

```text
add(a=2,b=3)
```

不是一个新的用户 Intent，它只是完成主 Intent 的一个 Tool Call。

## 4. 输出中实际发生了什么

### 第 1 Turn

模型看到：

```text
（2+3）×（5+6）
```

发现两项加法互不依赖，于是在同一个 Assistant Message 中返回：

```text
add(2,3)
add(5,6)
```

Agent Loop 根据 `max_parallel_tools` 并行执行，得到：

```text
5
11
```

### 第 2 Turn

模型读取两个 Tool Result，才知道下一步应调用：

```text
multiply(5,11)
```

Agent Loop 执行后得到：

```text
55
```

### 第 3 Turn

模型读取最终 Tool Result，生成：

```text
结果是 55
```

## 5. 这算不算多任务执行

如果使用宽泛说法，它确实算：

```text
多个子任务被执行
```

而且已经包含：

- 并行任务；
- 顺序任务；
- 数据依赖；
- 多 Turn；
- 最终汇总。

所以从“实际行为”看，当前 Agent 已经具备动态多步骤执行能力。

## 6. 为什么还不能叫显式 PlanExecutor

因为程序在第 1 Turn 开始前，并没有拿到这样的完整计划：

```json
{
  "tasks": [
    {
      "id": "task_1",
      "tool": "add",
      "arguments": {"a": 2, "b": 3},
      "depends_on": []
    },
    {
      "id": "task_2",
      "tool": "add",
      "arguments": {"a": 5, "b": 6},
      "depends_on": []
    },
    {
      "id": "task_3",
      "tool": "multiply",
      "arguments_from": ["task_1", "task_2"],
      "depends_on": ["task_1", "task_2"]
    }
  ]
}
```

当前真实过程是边执行边规划：

```text
第 1 Turn 只知道两个 add
→ 执行后回到模型
→ 第 2 Turn 才得到 multiply
```

Host 在第 1 Turn 时并不知道后面一定会有 multiply。

## 7. 当前模型实际就是隐式 Planner

当前架构：

```text
真实模型 = 动态 Planner
Agent Loop = 当前批 Tool Call 执行器
对话 Context = 中间状态
Tool Result = 下一步规划输入
```

这正是 Pi 风格 Agent Loop 的核心方式：

```text
Think/Plan
→ Tool Call
→ Observe Tool Result
→ Re-plan
→ Tool Call
→ Final Answer
```

它不需要先生成完整计划，也能完成有依赖的多步骤任务。

## 8. 当前方式的优点

- 灵活；
- 代码简单；
- 能根据真实工具结果调整下一步；
- 天然支持失败后重新规划；
- 适合计算、搜索、文件读取和无副作用工具。

这个算式就是一个成功例子。

## 9. 当前方式的限制

Host 没有完整显式计划，因此无法在执行前检查：

- 模型是否漏掉某个用户要求；
- 后续总共需要多少 Tool Call；
- 全部 Capability 是否存在；
- 是否包含写操作；
- 是否需要统一 Approval；
- 多个写操作是否冲突；
- 整体是否可以原子执行；
- 任务之间是否存在循环依赖。

对于无副作用计算问题通常可以接受。

对于支付、退款、取消、删除等真实业务操作风险较高。

## 10. 什么才叫多个 Intent

例如：

```text
查询订单 1001，同时取消订单 1002
```

这里有两个独立业务目标：

```text
Intent 1：order.get_status
Intent 2：order.cancel
```

每个 Intent 都有自己的：

- 参数；
- Capability；
- Tool Policy；
- 权限；
- Approval；
- 成功/失败结果。

这才是多 Intent。

另一个例子：

```text
查询订单 1001；如果已发货，再查询物流。
```

可以视为：

```text
Intent 1：order.get_status
Intent 2：shipment.get_tracking
```

第二个 Intent 依赖第一个结果。

## 11. 两种规划能力的准确命名

### 当前已经有

```text
Implicit Dynamic Tool Planning
隐式动态工具规划
```

特征：

- 模型每 Turn 决定下一步；
- 没有完整任务列表；
- Agent Loop 执行当前批次；
- 通过 Tool Result 继续规划。

### 当前尚未有

```text
Explicit Multi-Intent Planning
显式多 Intent 规划
```

特征：

- 执行前生成完整任务列表；
- 每个 Task 有 ID；
- 明确 depends_on；
- 全批 Capability 预检；
- 全批预算检查；
- 写操作统一 Approval；
- 状态持久化；
- 最终结果统一汇总。

## 12. 对截图的准确分类

```text
Intent 数量：1
子任务数量：3
Tool Call 数量：3
第 1 批并行 Tool Call：2
第 2 批依赖 Tool Call：1
模型 Turn：3
是否存在动态规划：是
是否存在子任务依赖：是
是否存在显式任务计划对象：否
是否属于多业务 Intent：否
```

## 13. 最终结论

用户的这个算式测试非常有价值，它证明当前 Agent Loop 已经可以：

```text
理解复合目标
→ 发现可并行步骤
→ 并行调用工具
→ 读取中间结果
→ 规划依赖步骤
→ 再调用工具
→ 汇总最终结果
```

但这是模型通过多个 Turn 实现的隐式动态计划，不是 Host 预先生成和管理的显式多 Intent Plan。

> **“（2+3）×（5+6）”是一个 Intent 下的三个子任务；当前模型和 Agent Loop 已经能动态完成这种多步骤依赖执行。真正缺少的是面向多个独立业务目标的显式计划、全批预检、统一审批和持久状态管理。**

---

## 对话 40：business.toml 没有计算配置，为什么 basic_usage 仍会自动调用加法和乘法

### 用户问题

`business.toml` 没有指定使用加法和乘法工具，用户问题中也没有指定工具名称，为什么这次模型仍然自动调用了对应工具？

### 助手回答

## 结论

因为这次运行的是：

```text
examples/basic_usage.py
```

它没有读取 `business.toml`，也没有经过 `HybridModelRouter`。

它走的是另一条链路：

```text
代码直接注册 add/multiply
→ Provider 把工具 Schema 发给模型
→ tool_choice=auto
→ 模型自行判断是否调用
```

所以本次工具调用与 `business.toml` 无关。

## 1. basic_usage 读取哪些配置

`basic_usage.py` 读取：

```text
config/agent.toml
config/providers.toml
```

它没有调用：

```python
load_simple_business_config()
```

也没有创建：

```python
HybridModelRouter
RoutedAgent
```

因此 `business.toml` 中有没有 calculator Intent，不影响该示例。

## 2. 加法和乘法从哪里注册

`basic_usage.py` 直接执行：

```python
registry = ToolRegistry()
registry.register(create_add_tool(...))
registry.register(create_multiply_tool(...))
```

然后交给 Agent：

```python
agent = Agent(
    ...,
    tools=registry.all(),
)
```

终端中这行就是证据：

```text
已注册工具：add、multiply
```

工具不是从 `business.toml` 注册的，而是 Python 示例代码直接注册的。

## 3. 模型实际看到了什么

Provider 构造 `/chat/completions` 请求时，只要 Agent 中存在工具，就会发送类似：

```json
{
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "add",
        "description": "计算两个数字 a 与 b 的和",
        "parameters": {
          "type": "object",
          "properties": {
            "a": {"type": "number"},
            "b": {"type": "number"}
          },
          "required": ["a", "b"]
        }
      }
    },
    {
      "type": "function",
      "function": {
        "name": "multiply",
        "description": "计算两个数字 a 与 b 的积",
        "parameters": {}
      }
    }
  ],
  "tool_choice": "auto"
}
```

模型虽然没有从用户文本中看到工具名，但从 API 请求的 `tools` 字段中知道：

```text
我可以使用 add
我可以使用 multiply
它们分别需要 a、b 参数
```

## 4. 为什么模型知道应该先加后乘

大模型自身理解表达式：

```text
（2+3）×（5+6）
```

它推理得到：

```text
先算两个括号
→ 两个加法互不依赖
→ 可以同时调用 add
→ 得到 5 和 11
→ 再调用 multiply(5,11)
```

这部分规划来自模型自身的理解能力，不来自 `business.toml`。

## 5. 为什么之前 8+9 没调用工具

因为两次使用的都是：

```text
tool_choice=auto
```

Auto 只表示允许模型调用工具，不保证一定调用。

对于：

```text
8+9
```

模型认为自己可以直接可靠回答，于是不调用工具。

对于：

```text
（2+3）×（5+6）
```

模型判断分步骤调用工具更合适，于是自动调用。

这是模型在 Auto 模式下的动态决策，不是确定性规则。

## 6. business.toml 在什么时候生效

当前只有业务入口会读取它：

```text
examples/business_routing_usage.py
```

对应链路：

```text
business.toml
→ HybridModelRouter
→ Intent
→ Capability
→ Tool Policy
→ Selected Tools
→ RoutedAgent
→ Agent Loop
```

在这条链路中，只有配置 Intent 所需 Capability 匹配到的工具，才会暴露给回答模型。

## 7. 当前实际存在两种运行模式

### 通用 Auto Agent

入口：

```text
basic_usage.py
```

行为：

```text
Python 代码直接注册工具
模型自由决定是否调用
business.toml 不参与
```

适合：

- 教学；
- 计算；
- 搜索；
- 无副作用工具；
- 观察模型动态规划。

### 业务受控 Agent

入口：

```text
business_routing_usage.py
```

行为：

```text
business.toml 定义 Intent/Capability/Policy
HybridModelRouter 先分类
只暴露匹配工具
RequiredToolCallGuard 强制验证
```

适合：

- 实时业务数据；
- 订单、退款、库存；
- 写操作；
- 权限和审批。

## 8. 如果 business.toml 也要控制计算

需要增加：

```toml
[[intents]]
id = "calculator.evaluate_expression"
name = "计算表达式"
description = "使用计算能力求表达式结果"
examples = [
  "计算 8+9",
  "帮我算（2+3）×（5+6）"
]
required_fields = ["expression"]
capability = "calculator.evaluate"
must_use_tool = true
requires_approval = false
ask_when_missing = "请提供要计算的表达式。"
```

并注册对应 Capability 和工具。

但当前 add/multiply 是教学工具，所以 basic_usage 直接注册即可，不必写进订单业务配置。

## 9. 这里没有自动扫描 Python 工具

模型不会自动发现项目中的所有 Python 函数。

它只能看到本次 Agent 明确传入：

```python
Agent(tools=[...])
```

的工具。

如果 `basic_usage.py` 删除：

```python
registry.register(create_add_tool())
registry.register(create_multiply_tool())
```

模型就不会再收到 add/multiply Schema，也不能调用它们。

## 10. 最终调用链

本次实际流程：

```text
basic_usage.py
→ Python 注册 add/multiply
→ Agent.state.tools
→ OpenAI 请求 tools 字段
→ tool_choice=auto
→ 模型理解复合表达式
→ 返回 add、add
→ Agent Loop 并行执行
→ Tool Result 回灌
→ 模型返回 multiply
→ Agent Loop 执行
→ 模型最终回答
```

`business.toml` 完全没有参与这次运行。

> **工具之所以自动被调用，不是因为 business.toml，而是因为 basic_usage.py 已经把 add 和 multiply 的 Schema 直接交给了模型。用户只描述目标，模型在 tool_choice=auto 下根据工具说明自行选择。**

---

## 对话 41：新增除法工具和 AI 必读完整工具开发指南

### 用户问题

1. 在 `tools` 下新增除法工具；
2. 编写一个让 AI 读取的 Markdown，完整说明一个工具需要完成什么、如何注册等内容。

### 助手回答

除法工具和完整工具开发指南已经完成，并通过真实模型联调。

## 1. 新增除法工具

新增：

```text
src/pi_agent_loop/tools/divide.py
```

公开工厂：

```python
create_divide_tool()
```

工具定义：

```text
name：divide
label：除法
description：计算被除数 a 除以非零除数 b 的商
execution_mode：parallel
timeout_seconds：3
```

JSON Schema：

```python
{
    "type": "object",
    "properties": {
        "a": {
            "type": "number",
            "description": "被除数",
        },
        "b": {
            "type": "number",
            "description": "非零除数",
            "not": {"const": 0},
        },
    },
    "required": ["a", "b"],
    "additionalProperties": False,
}
```

## 2. 除法运行时校验

在：

```text
src/pi_agent_loop/tools/validators.py
```

新增：

```python
validate_division_args()
```

它先复用：

```python
validate_two_numbers()
```

继续拒绝：

- 缺少 a/b；
- bool；
- NaN；
- Infinity；
- 非数字。

然后额外拒绝：

```text
b = 0
b = -0.0
```

错误：

```text
除数 b 不能为 0
```

JSON Schema 是给模型看的，Python 校验才是最终执行边界。

## 3. 除法执行过程

执行时：

```text
检查 CancellationToken
→ 报告“除法工具正在计算……”
→ 等待可选教学延时
→ 再次检查 CancellationToken
→ 计算 a / b
→ 返回结构化结果
```

返回：

```python
AgentToolResult(
    content=[{"type": "text", "text": str(value)}],
    details={
        "operation": "divide",
        "a": a,
        "b": b,
        "value": value,
        "toolCallId": tool_call_id,
    },
)
```

## 4. 独立 Timeout

除法工具配置：

```python
timeout_seconds=3
```

`basic_usage.py` 新增教学延时：

```python
DIVIDE_TOOL_DELAY_SECONDS = 0.0
```

设置为：

```python
DIVIDE_TOOL_DELAY_SECONDS = 4.0
```

可触发除法独立超时，不会取消兄弟工具。

## 5. 工具注册已完成

更新：

```text
src/pi_agent_loop/tools/__init__.py
```

计算工具集合现在返回：

```python
[
    create_add_tool(),
    create_multiply_tool(),
    create_divide_tool(),
]
```

Registry 工厂：

```python
create_calculator_registry().names()
```

返回：

```python
["add", "multiply", "divide"]
```

更新包顶层：

```text
src/pi_agent_loop/__init__.py
```

现在可以：

```python
from pi_agent_loop import create_divide_tool
```

## 6. basic_usage 已注册除法

`examples/basic_usage.py` 现在直接注册：

```python
registry.register(create_add_tool(...))
registry.register(create_multiply_tool(...))
registry.register(create_divide_tool(...))
```

终端显示：

```text
已注册工具：add（2 秒）、multiply（5 秒）、divide（3 秒）
```

用户不需要说工具名，模型可以根据 description 和 Schema 在 `tool_choice=auto` 下选择 divide。

## 7. 真实模型除法联调

实际用户输入：

```text
帮我计算（10+2）÷（1+2）
```

用户没有指定 add 或 divide。

真实执行：

```text
第 1 Turn：
add(10,2)
add(1,2)
→ 并行得到 12 和 3

第 2 Turn：
divide(12,3)
→ 得到 4.0

第 3 Turn：
模型回答：结果是 4
```

关键输出：

```text
模型要求调用工具：add、add
工具 add 执行成功：12
工具 add 执行成功：3
模型要求调用工具：divide
工具 divide 执行成功：4.0
最终回答：结果是 4
模型调用次数：3
```

证明：

- 除法 Schema 已发送；
- 真实模型能自动选择 divide；
- 多 Turn 动态依赖正常；
- Tool Result 回灌正常。

## 8. AI 必读工具指南

新增：

```text
TOOLS_IMPLEMENTATION_GUIDE.md
```

该指南完整说明：

```text
完整工具的定义
Tool/Capability/Intent/Tool Call 区别
文件位置
命名规则
JSON Schema
运行时参数校验
Execute 标准签名
CancellationToken
进度 Update
独立 Timeout
结构化 Result
错误处理和脱敏
只读工具与写工具
ToolRegistry 注册
工具集合工厂
CapabilityRegistry 注册
包公开导出
单元测试
Agent 集成测试
文档要求
完整代码模板
AI 开发流程
Definition of Done
```

## 9. AGENTS.md 已强制引用

`AGENTS.md` 现在规定，AI 新增、修改或删除任何 AgentTool 前，必须完整阅读：

```text
BUSINESS_REQUIREMENTS.md
TOOLS_IMPLEMENTATION_GUIDE.md
```

如果真实 API、权限、幂等、审批等信息不足，AI 必须先提问，不能自行编造。

## 10. BUSINESS_REQUIREMENTS 已关联指南

`BUSINESS_REQUIREMENTS.md` 的工具生成章节已明确指向：

```text
TOOLS_IMPLEMENTATION_GUIDE.md
```

因此后续流程是：

```text
业务人员填写 BUSINESS_REQUIREMENTS.md
→ AI 读取完整工具指南
→ 生成 business.toml
→ 实现工具
→ 注册 ToolRegistry/CapabilityRegistry
→ 增加测试
→ 验证 Definition of Done
```

## 11. 工具指南契约测试

`tests/test_business_requirements.py` 新增检查：

- AGENTS.md 必须引用工具指南；
- 工具指南必须包含 Schema；
- 必须包含校验、取消、Timeout；
- 必须包含 ToolRegistry/CapabilityRegistry 注册；
- 必须包含公开导出；
- 必须包含 Agent 集成测试；
- 必须包含 Definition of Done。

防止后续重构误删关键文档或 AI 读取规则。

## 12. 除法测试

新增和更新测试覆盖：

- `10 / 4 = 2.5`；
- operation/details；
- Update；
- 默认 Timeout 为 3 秒；
- 除数正零；
- 除数负零；
- 实际除法工具 Timeout；
- Calculator Registry 包含 divide；
- Agent Loop 执行 divide 并回灌结果。

当前结果：

```text
Ran 80 tests
OK
```

同时通过：

- `python -m compileall`；
- `git diff --check`；
- 顶层 `create_divide_tool` 导入；
- Registry 顺序检查；
- 真实模型除法联调。

## 13. 文档更新

已更新：

```text
README.md
BUILTIN_TOOLS_DESIGN.md
AGENTS.md
BUSINESS_REQUIREMENTS.md
conversation-review.md
```

> **除法工具已经完成 Schema、校验、除零保护、取消、进度、Timeout、结构化结果、注册、导出、测试和真实模型联调；以后 AI 开发任何新工具前，必须读取 TOOLS_IMPLEMENTATION_GUIDE.md 并满足 Definition of Done。**

---

## 对话 42：模型轮次、并行工具和系统各层重试策略

### 用户问题

1. 每轮模型调用失败是否适合增加重试？
2. 一轮有多个工具，其中一个工具失败时怎样重试？
3. 重试还需要加在哪些位置？
4. 结合 Pi 和 DeepSeek Harness 源码回答。

### 助手回答

## 核心结论

适合增加重试，但不能实现成：

```text
任何失败都自动重试
```

正确原则是：

```text
瞬时基础设施错误        可以自动重试
确定性参数/权限/业务错误  不应自动重试
有副作用且结果不确定      禁止盲目重试
```

而且必须区分四种重试：

```text
Provider HTTP 重试
Assistant Turn 重试
单个 Tool Call 重试
业务 Task/Plan 重试
```

不能把它们都塞进 `loop.py`，也不能让多层重试相乘形成重试风暴。

## 1. 每轮模型调用失败是否适合重试

### 适合自动重试

模型请求遇到以下瞬时错误时适合：

```text
连接建立失败
连接被重置
请求 Timeout
HTTP 408
HTTP 409
HTTP 429
HTTP 500/502/503/504
第三方明确返回 x-should-retry=true
第三方提供合理 Retry-After
SSE 在终止事件前因瞬时网络故障中断
```

### 不适合自动重试

```text
HTTP 400 请求格式错误
HTTP 401 API Key 错误
HTTP 403 权限不足
HTTP 404 模型或 Endpoint 不存在
配额/余额/账单耗尽
Context Overflow
Tool Schema 无效
Content Filter
用户主动取消
达到 max_turns/max_tool_calls
Required Tool Guard 拒绝
Capability Missing
Out of Scope
Prohibited
```

Context Overflow 应先压缩或缩短上下文，而不是用完全相同请求反复重试。

## 2. 模型重试的安全边界

如果模型请求在生成完整 Assistant Message 之前失败，并且还没有执行任何工具，可以重试同一个逻辑 Turn。

```text
Turn 1 / Attempt 1 → 503
等待 Backoff
Turn 1 / Attempt 2 → 成功
```

它仍然是一个逻辑 Turn，只是两个请求 Attempt。

建议分别统计：

```text
turns_used
model_attempts_used
model_retries_used
```

Retry Attempt 不应伪装成新的业务 Turn，但必须拥有独立预算。

### 流式失败

如果流式响应已经展示部分文本或残缺 Tool Call：

- 不能执行残缺 Tool Call；
- Retry Context 必须排除失败的 Partial Assistant；
- UI 应收到 retry/reset 事件；
- 重试成功后只保留最终完整 Assistant Message。

### 已经执行工具后

如果模型已经成功生成 Tool Call，而且工具已经执行，不能因为其中一个工具失败就重试整个模型 Turn。

否则成功工具可能被重复执行。

## 3. 多个工具中一个失败怎样处理

例如：

```text
Tool A 成功
Tool B 瞬时失败
Tool C 成功
```

推荐：

```text
保留 A 和 C 的成功结果
只重试 B
禁止重跑整批
```

最终仍然只向模型写回每个逻辑 Tool Call 的一个最终 ToolResult：

```text
A → success
B → retry 2 后 success 或最终 error
C → success
```

结果顺序继续按模型原始 Tool Call 顺序排列。

## 4. 单工具重试的两种方式

### Host 自动重试

适合：

- GET/只读查询；
- 幂等计算；
- 明确的网络瞬时错误；
- 相同参数重试不会产生额外副作用。

同一个逻辑 `toolCallId` 保持不变，内部增加：

```text
attempt=1
attempt=2
retryId
```

模型只看到最终结果。

### 模型语义重试

工具先返回错误 ToolResult，下一 Turn 由模型决定：

- 修正参数；
- 先读取新状态；
- 换工具；
- 缩小查询范围；
- 放弃并解释。

适合：

```text
参数不正确
文件版本过期
需要先 read 再 edit
搜索范围过大
业务冲突
需要改变计划
```

此时模型产生新的 Tool Call ID，也会消耗新的 Tool Call 预算。

## 5. 哪些工具错误不应自动重试

```text
invalid_arguments
unknown_tool
capability_missing
permission_denied
approval_rejected
policy_denied
not_found（除非业务明确允许等待出现）
business_validation_error
除数为零
用户取消
预算耗尽
```

这些错误再次使用相同参数，结果不会改变。

## 6. 写工具为什么危险

对于：

```text
cancel_order
create_refund
send_payment
write_file
```

Timeout 或网络断开时，可能出现：

```text
服务端已经成功提交
客户端没有收到响应
```

此时自动重试可能产生重复退款、重复支付或重复写入。

写工具只有满足以下条件才允许自动重试：

```text
有稳定 Idempotency Key
服务端保证重复请求返回同一结果
可以查询操作最终状态
错误明确发生在提交之前
```

否则应返回：

```text
outcome_unknown
```

并先执行状态核对，不能直接重试。

## 7. Timeout 是否可以重试

不能统一回答。

```text
只读查询 Timeout
通常可以有限重试
```

```text
本地纯计算 Timeout
通常说明代码或输入有问题，重试意义很小
```

```text
写操作 Timeout
结果可能不确定，默认不重试
```

因此 `tool_timeout` 不能全局标记为 retryable，必须结合工具的 idempotency 和 operation 类型。

## 8. 并行工具重试时的调度

建议重试的是一个逻辑 Tool Call，而不是整批。

Backoff 期间不应占用并发执行槽：

```text
Attempt 执行时占用 Semaphore
失败后释放
Backoff 等待不占用
下一 Attempt 重新申请槽位
```

始终保证：

```text
同时运行的 Tool Attempt <= max_parallel_tools
```

Retry Attempt 不增加 `max_tool_calls` 的逻辑调用数量，但应增加独立的：

```text
max_tool_attempts
max_tool_retries
```

## 9. 重试事件

建议新增：

```text
model_retry_scheduled
model_retry_attempt_start
model_retry_finished

tool_retry_scheduled
tool_retry_attempt_start
tool_retry_finished
```

事件至少包含：

```text
retryId
logicalCallId
attempt
maxAttempts
delayMs
errorCode
retryable
```

API Key 和敏感参数不能进入事件。

## 10. Backoff 策略

推荐：

```text
指数退避
+
随机 Jitter
+
尊重 Retry-After
+
最大等待上限
+
CancellationToken 可中止等待
```

例如：

```text
第 1 次重试：约 0.5 秒
第 2 次重试：约 1 秒
第 3 次重试：约 2 秒
最大不超过 30～60 秒
```

禁止立即死循环重试。

## 11. Pi 的模型重试设计

Pi 的低层：

```text
packages/agent/src/agent-loop.ts
```

不负责产品级自动重试。失败 Assistant Message 由更高层 Session 处理。

### Provider 请求重试

源码：

```text
pi/packages/ai/src/utils/provider-retry.ts
```

`retryProviderRequest()`：

- 默认 `maxRetries=0`；
- 支持 408、409、429、5xx；
- status 未知的传输错误可重试；
- 尊重 `x-should-retry`；
- 读取 `retry-after-ms` 和 `retry-after`；
- 默认拒绝超过 60 秒的服务端等待；
- 指数退避加 Jitter；
- Backoff 可被 AbortSignal 中止；
- 要求 SDK 自带 Retry 关闭，避免双重重试。

### Assistant Error 分类

源码：

```text
pi/packages/ai/src/utils/retry.ts
```

`isRetryableAssistantError()` 识别：

- overloaded；
- rate limit；
- 429/5xx；
- 网络、DNS、Socket、WebSocket；
- Timeout；
- Stream 提前结束；
- Provider 要求 retry。

同时明确排除：

- quota；
- billing；
- monthly usage limit；
- insufficient_quota。

### Session 重试

源码：

```text
pi/packages/coding-agent/src/core/agent-session.ts
```

Pi：

- 在 Session 层处理 Auto Retry；
- 发送 `auto_retry_start/end`；
- 指数 Backoff；
- Backoff 可取消；
- 成功后重置 Retry Counter；
- 重试前从 Agent State 移除失败 Assistant；
- 失败记录仍保存在 Session 历史；
- Context Overflow 走 Compaction，不走普通重试。

这说明 Pi 的原则是：

```text
Agent Loop 保持低层
模型重试由 Session/Host 负责
```

### Pi 的工具失败

Pi 没有在低层 Agent Loop 中对所有工具错误做通用自动重试。

通常是：

```text
错误 ToolResult 回灌模型
→ 模型根据错误语义决定是否产生新 Tool Call
```

这可以避免对有副作用工具盲目重试。

## 12. DeepSeek Harness 的模型重试

核心 Agent：

```text
deepseek-harness/packages/core/agent-loop/src/agent.ts
```

模型 Step 内部存在 `while (true)`：

```text
请求失败
→ dispatch agent/request-error
→ 插件返回 {kind:"retry"}
→ 在同一 Turn/Step 中继续
```

没有 Retry 决定时，错误终止。

### llm-retry 插件

源码：

```text
deepseek-harness/packages/llm/llm-retry/src/index.ts
```

特点：

- Retry Policy 属于 Provider 配置；
- 支持 normal 有界模式；
- 支持 always 模式；
- normal 按 `retryableCodes` 判断；
- 指数退避；
- maxDelay；
- Jitter；
- Provider Retry-After；
- AbortSignal；
- 每条 Retry Chain 有 retryId；
- Provider、Turn、Step 和 Policy Key 都被记录。

### Durable Retry

DeepSeek Harness 会先写入：

```text
llm/retry
```

等待结束、真正发起请求前再写：

```text
llm/retry-started
```

因此进程恢复、审计和并发不变量都能知道：

```text
重试是否已计划
等待是否完成
第几次 Attempt
属于哪个 Turn/Step/Provider
```

这比仅在内存中 sleep 更适合生产系统。

## 13. DeepSeek Harness 的工具重试设计

工具 Runtime 暴露：

```text
tools/pre-execute
→ tools/execute
→ tools/post-execute
→ tools/result
```

其中：

```text
tools/execute
```

明确是 Timeout、Retry、Metrics 的 Around-dispatch 扩展点。

说明 Tool Retry 不需要污染工具实现或 Agent Loop，可以通过执行包装器实现。

但 DeepSeek Harness 也没有对全部工具错误统一自动重试。

它更强调结构化错误和工具专属恢复：

- `UNKNOWN_TOOL` 不重试；
- `INVALID_TOOL_OUTPUT` 不盲重试；
- `FS_STALE_VERSION` 要求先重新读取再重试；
- Sandbox 升级只允许同一操作按最小权限重试一次，并需要 Approval；
- Approval 拒绝后不应换方式绕过重试；
- 某些 Workflow 明确规定普通子任务失败即终止，不自动 Retry。

这说明：

```text
技术瞬时失败适合通用 Retry
语义和业务失败需要领域专属 Recovery
```

## 14. Pi 与 DeepSeek Harness 对比

| 方面 | Pi | DeepSeek Harness |
|---|---|---|
| 低层 Agent Loop | 不放产品级 Retry | 通过 request-error 扩展点恢复 |
| Provider Retry | 状态码、Header、Backoff、Jitter | Provider Policy、Failure Code、Backoff、Jitter |
| Session Retry | AgentSession 内存状态和 UI 事件 | Session Durable Event |
| Context Overflow | Compaction 后特殊重试 | 交给独立 Failure/Policy 路径 |
| Tool Retry | 主要由模型根据 ToolResult 决定 | tools/execute 提供包装扩展点 |
| 工具语义恢复 | 模型重新规划 | 结构化 Error Code + 专属 Remedy |
| 持久化 | Session 保存失败消息 | Retry 计划和开始事件均持久化 |

## 15. 当前 Python 项目的推荐分层

```text
OpenAICompatibleProvider
只负责一次 HTTP/SSE Attempt

ModelRetryPolicy / RetryingStreamFn
负责同一个 Assistant Turn 的模型重试

ToolRetryPolicy / ToolDispatchWrapper
只重试单个幂等 Tool Call

RoutedAgent / Future PlanExecutor
负责语义恢复、Capability、Approval 和任务重试

Session Store
保存 Retry Scheduled/Started/Finished
```

不要直接在 `loop.py` 中写大量 Backoff 和错误分类。

## 16. 建议新增文件

```text
src/pi_agent_loop/retry/
├─ __init__.py
├─ types.py
├─ classifier.py
├─ backoff.py
├─ model.py
├─ tool.py
└─ events.py
```

职责：

```text
types.py       ModelRetryPolicy、ToolRetryPolicy、RetryDecision
classifier.py  HTTP、Provider、Tool Error 分类
backoff.py     Retry-After、指数退避、Jitter、可取消等待
model.py       RetryingStreamFn
工具.py         单逻辑 Tool Call Attempt 包装
events.py      Retry 生命周期事件
```

文件名实际应为 `tool.py`。

## 17. 配置建议

Provider 专属模型重试：

```toml
[profiles.third_party.retry]
enabled = true
max_retries = 2
initial_delay_seconds = 0.5
max_delay_seconds = 30
jitter_ratio = 0.2
retryable_statuses = [408, 409, 429, 500, 502, 503, 504]
```

工具 Retry 不建议只有一个全局开关，应由工具声明：

```python
retry_policy=ToolRetryPolicy(
    max_retries=2,
    retryable_codes={"network_error", "rate_limited", "upstream_unavailable"},
    idempotent=True,
)
```

写工具默认：

```python
retry_policy=None
```

除非拥有服务端 Idempotency Key 和结果核对机制。

## 18. 当前项目在实现 Retry 前需要先补什么

### Provider Error 结构化

当前 Provider 最终主要返回文本错误。建议增加：

```python
providerError = {
    "code": "rate_limited",
    "statusCode": 429,
    "retryAfterMs": 2000,
    "retryable": True,
}
```

不能只靠中文错误文本做分类。

### Tool Error 结构化

当前通用工具异常大多归为：

```text
tool_execution_error
```

应允许工具抛出或返回：

```python
RetryableToolError(
    code="upstream_unavailable",
    retry_after_seconds=1,
)
```

并区分永久错误。

### 独立 Retry Budget

增加：

```text
max_model_attempts
max_model_retries
max_tool_attempts
max_tool_retries
max_retry_elapsed_seconds
```

不能让重试绕过现有 Turn 和 Tool Call 预算。

## 19. 推荐实现顺序

### 第一阶段：模型请求重试

先完成：

- Provider Error 结构化；
- Retryable 分类；
- 有界指数退避和 Jitter；
- Retry-After；
- CancellationToken；
- model retry 事件；
- 401/403/404/Quota 不重试；
- Context Overflow 不普通重试。

这是收益最高、风险最低的一层。

### 第二阶段：幂等只读工具重试

只支持：

```text
operation=read
idempotent=true
retryable error code
```

多个工具中只重试失败的逻辑 Tool Call。

### 第三阶段：持久化和 Circuit Breaker

增加：

- Durable Retry Event；
- Retry Chain ID；
- 进程恢复；
- Provider Circuit Breaker；
- Metrics；
- Retry Storm 防护。

### 第四阶段：业务 Task Retry

交给 RoutedAgent/PlanExecutor，处理：

- 先读后写；
- Stale Version；
- Approval Escalation；
- 结果不确定核对；
- 补偿事务。

## 20. 必须测试的场景

### 模型

```text
429 → Backoff → 成功
503 → 两次失败 → 第三次成功
401 → 不重试
Quota → 不重试
取消 Backoff → 立即结束
Retry-After 超上限 → 失败
SSE 中途断开 → 丢弃 Partial 后重试
成功后 Retry Counter 清零
```

### 多工具

```text
A 成功、B 瞬时失败、C 成功
→ 只重试 B
→ A/C 各执行一次
```

```text
B 参数错误
→ 不重试
```

```text
写工具 Timeout
→ 不自动重试
→ outcome_unknown
```

```text
用户取消
→ 停止当前 Attempt 和 Backoff
```

```text
重试耗尽
→ 只生成一个最终错误 ToolResult
→ details 包含 attempts 和 retryId
```

## 最终建议

当前项目适合增加重试，但建议严格按优先级：

```text
先做模型瞬时错误 Retry
→ 再做幂等只读 Tool Retry
→ 最后做持久 Task/Workflow Retry
```

多个并行工具中一个失败时：

```text
不要重试整批
保留成功兄弟结果
只重试失败 Tool Call
写操作必须先判断幂等性和结果是否确定
```

> **重试不是“再运行一次”，而是根据错误类型、幂等性、执行阶段和持久状态，决定能否安全地重做同一个逻辑操作。Pi 把模型 Retry 放在 Session/Host，DeepSeek Harness 用可持久化的 request-error/retry 扩展点；当前 Python 项目也应保持 Agent Loop 低层，把重试放在 Provider、Retry Wrapper、Tool Dispatch 和 PlanExecutor 四个边界。**

---

## 对话 43：实现模型瞬时错误 Retry 和幂等单工具 Retry

### 用户问题

开始开发重试功能。

### 助手回答

模型请求重试和幂等单工具重试第一版已经完成。

## 1. 新增 retry 包

```text
src/pi_agent_loop/retry/
├─ __init__.py
├─ types.py
├─ errors.py
├─ classifier.py
├─ backoff.py
├─ model.py
└─ tool.py
```

职责：

```text
types.py
ModelRetryPolicy、ToolRetryPolicy

errors.py
RetryableToolError

classifier.py
结构化 Provider Error 和保守文本 fallback 分类

backoff.py
指数退避、Jitter、Retry-After、CancellationToken

model.py
RetryingStreamFn，同一个逻辑 Turn 多次 Attempt

tool.py
只重试一个逻辑 Tool Call
```

## 2. 模型 Retry Policy

新增：

```python
ModelRetryPolicy(
    enabled=True,
    max_retries=2,
    initial_delay_seconds=0.5,
    max_delay_seconds=30,
    jitter_ratio=0.2,
    retryable_statuses=frozenset({
        408, 409, 429, 500, 502, 503, 504
    }),
)
```

严格校验：

- max_retries 必须是非负整数；
- enabled 时 max_retries 必须大于零；
- Delay 必须有限且合法；
- Initial 不能超过 Max；
- Jitter 必须在 0～1；
- Retryable Status 必须是合法 HTTP 状态码。

## 3. Provider 配置

`config/providers.toml.example` 新增：

```toml
[profiles.third_party.retry]
enabled = true
max_retries = 2
initial_delay_seconds = 0.5
max_delay_seconds = 30
jitter_ratio = 0.2
retryable_statuses = [408, 409, 429, 500, 502, 503, 504]
```

旧本地配置没有该分区时默认关闭，避免升级后突然产生额外收费调用。

当前本地被 Git 忽略的 `config/providers.toml` 已安全追加该分区并启用：

```text
enabled=True
max_retries=2
statuses=[408,409,429,500,502,503,504]
```

没有读取、显示或修改 API Key。

## 4. Provider Error 结构化

OpenAI-compatible Provider 错误消息现在增加：

```python
providerError = {
    "code": "provider_rate_limit_error",
    "statusCode": 429,
    "retryAfterMs": 2000,
    "retryable": True,
}
```

支持读取：

```text
retry-after-ms
retry-after 秒数
retry-after HTTP Date
```

401/403 和 404 显式标记为不可重试。

API Key 仍不会进入错误 Details。

## 5. 模型 Retry 执行方式

```text
OpenAICompatibleProvider.stream
→ RetryingStreamFn
→ Attempt 1
→ 若瞬时错误则 Backoff
→ Attempt 2
→ 成功或继续到上限
```

新增统计：

```text
provider.call_count
逻辑模型调用次数

provider.attempt_count
包括重试在内的真实 HTTP Attempt 次数
```

例如 429 后成功：

```text
call_count=1
attempt_count=2
```

Retry 不增加 `max_turns`，因为它仍属于同一个逻辑 Turn。

## 6. Partial Assistant 安全

启用模型 Retry 时，每个 Attempt 的 Assistant Event 会先缓冲。

```text
失败 Attempt
→ 整批丢弃
→ 不进入 Agent Context
→ 不执行残缺 Tool Call

成功 Attempt
→ 完整交给 Agent Loop
```

这样避免 SSE 中断后残缺 Tool Call 被执行。

代价是：启用 Retry 时，该次模型的 Token Delta 不再实时显示，而是在 Attempt 完成后一次性交给 Agent Loop。

## 7. 模型 Retry 分类

结构化字段优先。

可重试：

```text
provider_timeout_error
provider_http_error
provider_rate_limit_error
provider_transport_error
Retryable HTTP Status
```

不可重试：

```text
401/403/404
Quota/Billing
Context Overflow 文本
未分类错误
```

旧 Provider 没有结构化字段时，才使用保守文本 fallback。

## 8. 模型 Retry 事件

新增并由 Agent Loop 转发：

```text
model_retry_scheduled
model_retry_attempt_start
model_retry_finished
```

`basic_usage.py` 已增加中文事件说明。

## 9. Backoff

实现：

```text
指数退避
Jitter
Retry-After
最大等待
CancellationToken
```

服务端要求的 Retry-After 超过 Max Delay 时，不执行该次重试。

用户在 Backoff 期间取消 Agent，会立即取消 Timer 并返回 aborted。

## 10. Tool Retry Policy

新增：

```python
ToolRetryPolicy(
    max_retries=2,
    retryable_codes=frozenset({
        "network_error",
        "rate_limited",
        "upstream_unavailable",
    }),
    idempotent=True,
    initial_delay_seconds=0.2,
    max_delay_seconds=5,
    jitter_ratio=0.2,
)
```

只有：

```text
idempotent=True
```

才能创建自动 Tool Retry Policy。

非幂等工具会在配置阶段抛错，不能进入运行。

## 11. 工具怎样声明瞬时错误

工具执行函数使用：

```python
raise RetryableToolError(
    "上游暂时不可用",
    code="upstream_unavailable",
    retry_after_seconds=1,
)
```

只有 Code 同时存在于工具 `retryable_codes` 才会重试。

参数错误、除零、权限、Approval、业务错误使用普通 ValueError 或非 Retryable Error，不会重试。

## 12. 多个并行工具只重试失败项

已经验证：

```text
transient 工具第一次失败、第二次成功
sibling 工具第一次成功
```

结果：

```text
transient attempts=2
sibling attempts=1
```

成功兄弟工具不会重新执行，整批不会重跑。

## 13. Semaphore 与 Backoff

并发槽现在按 Tool Attempt 获取：

```text
Attempt 获取 Semaphore
→ 执行
→ 失败后释放
→ Backoff 不占并发槽
→ 新 Attempt 重新获取
```

因此所有正在执行的 Attempt 仍受：

```text
max_parallel_tools
```

限制。

初次等待并发槽不消耗工具 Timeout；第一次真正执行后开始计时。总 Timeout 包含后续 Attempt 和 Backoff。

## 14. Tool Retry 最终结果

同一个逻辑 Tool Call 最终只产生一个 ToolResult。

重试耗尽：

```python
{
    "code": "upstream_unavailable",
    "retryable": True,
    "attempts": 3,
    "retryId": "..."
}
```

不会向模型写入多个失败 ToolResult。

## 15. Tool Retry 事件

新增：

```text
tool_retry_scheduled
tool_retry_attempt_start
tool_retry_finished
```

事件包含 Retry ID、Tool Call ID、Attempt、Delay 和 Error Code，不包含 API Key 或完整敏感参数。

`basic_usage.py` 已增加中文说明。

## 16. 当前明确不自动重试

- 工具 Timeout；
- 用户取消；
- Invalid Arguments；
- Unknown Tool；
- Capability Missing；
- Permission/Policy/Approval Denied；
- 普通 ValueError；
- 非幂等工具；
- 写操作结果不确定。

写工具仍需要 Idempotency Key 和状态核对机制后才能单独设计。

## 17. 公开 API

新增顶层导出：

```python
from pi_agent_loop import (
    ModelRetryPolicy,
    ToolRetryPolicy,
    RetryableToolError,
    RetryingStreamFn,
    retry_model_stream,
)
```

`AgentTool` 新增：

```python
retry_policy: ToolRetryPolicy | None = None
```

## 18. 工具开发指南同步

`TOOLS_IMPLEMENTATION_GUIDE.md` 已增加：

- 幂等性评估；
- ToolRetryPolicy；
- RetryableToolError；
- Retryable/Permanent Error 区分；
- 写工具默认不重试；
- Definition of Done Retry 检查项。

## 19. 新增测试

新增：

```text
tests/test_retry.py
```

并增强：

```text
tests/test_provider_settings.py
tests/test_openai_compatible_provider.py
```

覆盖：

- 429 后同逻辑 Turn 重试成功；
- 401 不重试；
- Backoff 用户取消；
- Retry 生命周期事件；
- Provider Call 与 Attempt 分开计数；
- 并行工具只重试失败项；
- 非 Retryable Code 不重试；
- Tool Retry 耗尽；
- 非幂等工具拒绝策略；
- Provider Retry TOML 加载；
- 401 结构化错误和密钥脱敏。

当前结果：

```text
Ran 89 tests
OK
```

同时通过：

- `python -m compileall`；
- `git diff --check`；
- Retry 顶层导入；
- 当前真实配置 Git 隔离。

## 20. 真实 API 回归

启用本地模型 Retry 后运行：

```text
Reply with exactly: RETRY_ENABLED_OK
```

结果：

```text
模型重试：enabled=True，max_retries=2
最终回答：RETRY_ENABLED_OK
模型调用次数：1
```

本次真实 API 没有产生瞬时错误，所以没有触发实际 Retry；429 Retry 行为由 MockTransport 和 ScriptedProvider 确定性测试覆盖。

## 21. 当前未完成

- Durable Retry Event；
- 进程恢复 Retry Chain；
- Circuit Breaker；
- 全局 Max Retry Elapsed；
- 写操作 outcome_unknown；
- Task/Workflow Retry；
- Context Overflow Compaction Retry。

> **当前已完成有界模型瞬时错误重试和幂等单工具重试：模型失败只重试同一个逻辑 Turn，并行工具失败只重试失败项；非幂等写操作和确定性错误继续 Fail-closed。**

---

## 对话 44：实现 Durable Retry、恢复、Circuit、Task、Outcome 和 Compaction，并提供测试命令

### 用户问题

把上一轮仍未实现的 Retry 能力也实现，并说明如何使用当前工具测试全部重试功能。

### 助手回答

上一轮列出的高级 Retry 基础能力已经完成，并新增一个完全离线、可以直接运行的测试示例。

## 1. 新增高级 Retry 模块

```text
src/pi_agent_loop/retry/
├─ events.py
├─ circuit_breaker.py
├─ task.py
├─ outcome.py
└─ compaction.py
```

原有：

```text
model.py
tool.py
backoff.py
classifier.py
types.py
errors.py
```

继续保留。

## 2. Durable Retry Event

新增：

```python
JsonlRetryEventStore("state/retry-events.jsonl")
```

Agent 使用：

```python
store = JsonlRetryEventStore("state/retry-events.jsonl")
agent = Agent(
    ...,
    retry_event_sink=store.append,
)
```

已实现：

- Model/Tool/Task Retry Event 写入 JSONL；
- Scheduled、AttemptStart、Finished；
- 写入前脱敏；
- API Key、Authorization、Token、Cookie、Password、Idempotency Key 自动隐藏；
- 长文本截断；
- Flush 后再进入 Backoff/UI 发布；
- JSONL 格式错误明确报行号。

本地运行状态：

```text
state/
```

已加入 `.gitignore`，不会提交 Retry Journal。

## 3. 进程恢复 Retry Chain

新增：

```python
RetryRecoveryManager
```

启动时：

```python
store = JsonlRetryEventStore("state/retry-events.jsonl")
manager = RetryRecoveryManager(store)
await manager.recover(handler)
```

它会：

```text
重新加载 JSONL
→ 按 retryId 分组
→ 找出没有 Finished 的 Chain
→ 写 retry_recovery_started
→ 调用 Host 提供的恢复 Handler
→ 写 retry_recovery_finished
```

重要边界：它恢复 Retry 元数据和协调流程，不持久化 Prompt 或真实工具参数。完整模型请求恢复仍需要未来 Session Store 负责重建安全上下文。

## 4. Circuit Breaker

新增：

```python
CircuitBreakerPolicy(
    enabled=True,
    failure_threshold=3,
    recovery_timeout_seconds=30,
)
```

状态机：

```text
Closed
→ 连续瞬时失败达到阈值
→ Open
→ 后续调用快速失败，不访问 Provider
→ 到达恢复时间
→ Half-open，只允许一个探测
→ 探测成功后 Closed
→ 探测失败后重新 Open
```

Provider Retry 已自动使用配置中的 CircuitBreakerPolicy。

当前 Circuit 状态只在进程内，重启后重新 Closed。

## 5. Circuit 配置

`providers.toml.example` 新增：

```toml
[profiles.third_party.retry]
enabled = true
max_retries = 2
initial_delay_seconds = 0.5
max_delay_seconds = 30
jitter_ratio = 0.2
max_elapsed_seconds = 120
retryable_statuses = [408, 409, 429, 500, 502, 503, 504]

[profiles.third_party.retry.circuit_breaker]
enabled = true
failure_threshold = 3
recovery_timeout_seconds = 30
```

本地被 Git 忽略的真实配置也已加入并通过 Loader 校验：

```text
retry.enabled=True
retry.max_elapsed_seconds=120
circuit.enabled=True
circuit.failure_threshold=3
```

没有读取或显示 API Key。

## 6. 全局最大 Retry 耗时

新增：

```text
ModelRetryPolicy.max_elapsed_seconds
ToolRetryPolicy.max_elapsed_seconds
TaskRetryPolicy.max_elapsed_seconds
```

每次计划下一 Backoff 前检查：

```text
已经消耗时间 + 下一等待时间 <= max_elapsed_seconds
```

超出时立即停止，不再进入下一 Attempt。

这防止每次单独 Backoff 合法，但总运行时间无限增长。

## 7. outcome_unknown

新增：

```python
OutcomeUnknownToolError
OutcomeReconciliationRegistry
```

写工具在“服务端可能成功，但客户端没有收到确定结果”时：

```python
raise OutcomeUnknownToolError(
    "写操作结果不确定",
    operation_id="operation-1",
    idempotency_key="...",
    reconciliation_name="check_operation",
)
```

Agent ToolResult：

```python
{
    "code": "outcome_unknown",
    "operationId": "operation-1",
    "reconciliationName": "check_operation",
    "retryable": False,
}
```

Idempotency Key 不会进入 ToolResult。

然后注册状态核对器：

```python
registry = OutcomeReconciliationRegistry()
registry.register("check_operation", reconcile)
result = await registry.reconcile(error, cancellation)
```

核对器查询最终状态，不重放原写操作。

## 8. Task/Workflow Retry

新增：

```python
TaskRetryPolicy
RetryableTaskError
TaskRetryExecutor
```

使用：

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

该执行器支持：

- 幂等 Task；
- Retryable Code；
- Retry-After；
- Backoff/Jitter；
- CancellationToken；
- Max Elapsed；
- Durable Task Retry Events。

它不负责自然语言多 Intent 拆解，也不会允许非幂等 Workflow 自动重试。

## 9. Context Overflow Compaction Retry

新增：

```python
CompactionRetryPolicy
SlidingWindowCompactor
ContextOverflowCompactingStreamFn
compact_on_context_overflow()
```

使用：

```python
stream_fn = compact_on_context_overflow(
    provider.stream,
    CompactionRetryPolicy(
        max_retries=1,
        keep_recent_messages=20,
    ),
)
```

流程：

```text
模型返回 context_overflow/context_length_exceeded
→ 丢弃失败 Assistant
→ 压缩 messages
→ 同一个 Agent Turn 重试
→ 发送 context_compaction_started/finished
```

`basic_usage.py`、`real_model_usage.py` 和 `business_routing_usage.py` 已接入该包装器。

默认 SlidingWindowCompactor 是教学实现。生产系统需要替换成 token-aware 摘要器，并保证 Tool Call/Tool Result 配对。

## 10. 示例已启用 Durable Retry

以下真实模型示例现在都创建：

```python
JsonlRetryEventStore(ROOT / "state" / "retry-events.jsonl")
```

并传入：

```python
retry_event_sink=retry_store.append
```

包括：

```text
examples/basic_usage.py
examples/real_model_usage.py
examples/business_routing_usage.py
```

HybridModelRouter 的分类模型 Retry 也接入同一 Store。

## 11. 新增完整离线演示

新增：

```text
examples/retry_usage.py
```

该示例不访问真实网络，不消耗 API 额度。

全部测试：

```bat
python examples\retry_usage.py all
```

单独运行：

```bat
python examples\retry_usage.py model
python examples\retry_usage.py tool
python examples\retry_usage.py circuit
python examples\retry_usage.py task
python examples\retry_usage.py outcome
python examples\retry_usage.py compaction
python examples\retry_usage.py recovery
```

## 12. 使用当前 divide 工具测试 Tool Retry

```bat
python examples\retry_usage.py tool
```

该场景复用当前真实：

```python
create_divide_tool()
```

测试包装器只在第一次 Attempt 模拟：

```text
upstream_unavailable
```

第二次调用真实 divide：

```text
10 ÷ 4 = 2.5
```

实际输出：

```text
tool_retry_scheduled
tool_retry_attempt_start
tool_retry_finished
divide Attempts：2
divide 最终结果：2.5
```

模拟故障不是模型参数，不会污染 divide JSON Schema。

## 13. 其他场景输出

### 模型 Retry

```bat
python examples\retry_usage.py model
```

```text
429 → Retry → 成功
逻辑调用：1
实际 Attempt：2
```

### Circuit

```bat
python examples\retry_usage.py circuit
```

```text
连续失败达到阈值
→ state=open
→ 后续快速失败
```

### Task

```bat
python examples\retry_usage.py task
```

```text
Task Attempts：2
结果：task-ok
```

### outcome_unknown

```bat
python examples\retry_usage.py outcome
```

```text
不重放写操作
→ 调用状态核对器
→ 确认最终状态
```

### Compaction

```bat
python examples\retry_usage.py compaction
```

```text
压缩前消息数：6
压缩后消息数：2
最终回答：压缩后成功
```

### Recovery

```bat
python examples\retry_usage.py recovery
```

```text
发现未完成 Chain
→ 调用恢复 Handler
→ 写入恢复完成事件
```

## 14. 查看 Retry Journal

Windows：

```bat
type state\retry-demo.jsonl
```

Linux/macOS：

```bash
cat state/retry-demo.jsonl
```

Journal 中只包含 Retry 元数据，不包含 API Key 或完整 Prompt/参数。

## 15. 自动测试

新增：

```text
tests/test_retry_advanced.py
```

覆盖：

- JSONL 持久化和脱敏；
- 重启后识别未完成 Chain；
- Recovery Handler；
- Agent Retry Event 直接持久化；
- Circuit Open/Half-open/Closed；
- Circuit 阻止后续 Provider Attempt；
- 模型 Max Elapsed；
- Tool Max Elapsed；
- outcome_unknown 不重试；
- 状态核对器；
- Task Retry 和 Durable Event；
- Context Overflow 压缩重试。

当前结果：

```text
Ran 98 tests
OK
```

同时通过：

- `python -m compileall`；
- `git diff --check`；
- 高级 Retry 顶层导入；
- state/config Git 忽略检查；
- 完整 `retry_usage.py all` 演示。

## 16. 真实 API 回归

开启 Retry、Circuit、Journal 和 Compaction 包装后，真实模型返回：

```text
ADVANCED_RETRY_OK
```

```text
模型重试：enabled=True，max_retries=2
模型调用次数：1
```

本次没有真实瞬时错误，所以 Retry/Circuit 未触发；瞬时故障通过确定性 Mock 测试覆盖。

## 17. 当前边界

已经实现高级能力，但必须准确说明：

- Process Recovery 恢复 Retry Chain 元数据并调用 Host Handler；完整模型请求恢复仍依赖未来 Session Store；
- Circuit Breaker 当前是进程内状态，重启后归零；
- SlidingWindowCompactor 是教学实现，不是 token-aware 摘要；
- TaskRetryExecutor 是单个幂等 Task 执行器，不是多 Intent PlanExecutor；
- Outcome Reconciliation 必须由真实业务提供状态查询 API；
- 写工具仍默认禁止自动 Retry。

> **现在已形成完整的分层 Retry 基础：模型 Attempt、单工具 Attempt、Task Retry、Durable Journal、Recovery Coordinator、Circuit Breaker、Max Elapsed、outcome_unknown 核对和 Context Compaction Retry；同时保留了写操作安全边界。**

---

## 对话 45：提交当前 Retry 功能的 Git Commit 提示词

### 用户问题

准备提交代码。结合上一次已经推送到 GitHub 的代码和现在新增的内容，询问这次提交应该使用什么提示词或 Commit Message。

### 助手回答

当前 HEAD 是：

```text
abf879a 新增除法工具，并且写了给ai看的工具编写流程。
```

因此这次提交不需要重复描述除法工具，应该聚焦从该提交以后新增的完整 Retry/Reliability 能力。

推荐 Commit 标题：

```text
feat(retry): 实现模型、工具和任务的分层重试机制
```

推荐完整 Commit Message：

```text
feat(retry): 实现模型、工具和任务的分层重试机制

- 为 Provider 增加结构化错误、Retry-After、指数退避、Jitter 和总耗时限制
- 支持同一逻辑 Turn 的模型重试，区分 call_count 与 attempt_count
- 支持幂等 Tool Call 定向重试，保留并行成功工具结果
- 增加 JSONL Retry Journal、重试链发现和进程恢复协调
- 增加 Circuit Breaker、Task Retry 和 outcome_unknown 状态核对
- 增加 Context Overflow 压缩后重试
- 增加 Retry 生命周期事件、配置、离线演示和中文文档
- 新增基础与高级 Retry 测试，当前 98 项测试全部通过
```

如果需要交给 AI 执行提交，可以使用下面的提示词：

```text
请检查 agent-loop 仓库从当前 HEAD abf879a 之后的所有改动，只提交本次 Retry/Reliability 功能。

提交前必须执行：
1. python -m unittest discover -s tests -v
2. python -m compileall -q src examples tests
3. git diff --check
4. git diff --cached --name-only
5. 确认 config/agent.toml、config/providers.toml、config/business.toml 和 state/ 没有进入暂存区
6. 确认没有 API Key、Authorization、Token、Cookie、Password 或 Idempotency Key 被提交

提交标题：
feat(retry): 实现模型、工具和任务的分层重试机制

提交正文：
- 为 Provider 增加结构化错误、Retry-After、指数退避、Jitter 和总耗时限制
- 支持同一逻辑 Turn 的模型重试，区分 call_count 与 attempt_count
- 支持幂等 Tool Call 定向重试，保留并行成功工具结果
- 增加 JSONL Retry Journal、重试链发现和进程恢复协调
- 增加 Circuit Breaker、Task Retry 和 outcome_unknown 状态核对
- 增加 Context Overflow 压缩后重试
- 增加 Retry 生命周期事件、配置、离线演示和中文文档
- 新增基础与高级 Retry 测试，当前 98 项测试全部通过

不要使用 git add -f，不要提交任何真实 TOML 配置和 state 运行状态。提交完成后显示 commit hash 和 git status，但不要自动修改或清理用户本地真实配置。
```

PowerShell 手动提交命令：

```powershell
cd D:\0715\git_codex\20260825\agent-loop

python -m unittest discover -s tests -v
python -m compileall -q src examples tests
git diff --check

git add .
git diff --cached --name-only
git diff --cached --check

git commit -m "feat(retry): 实现模型、工具和任务的分层重试机制" `
  -m "为 Provider 增加结构化错误、Retry-After、指数退避、Jitter 和总耗时限制；支持同一逻辑 Turn 模型重试和幂等 Tool Call 定向重试；增加 JSONL Retry Journal、恢复协调、Circuit Breaker、Task Retry、outcome_unknown、Context Overflow Compaction、离线演示和 98 项测试。"

git status --short
git push origin main
```

当前安全检查结果：

```text
config/agent.toml      被忽略
config/providers.toml  被忽略
config/business.toml   被忽略
state/                 被忽略
```

当前没有文件进入暂存区，`git diff --check` 通过；只有 LF/CRLF 提示，不是内容错误。

> **这次建议使用一个聚焦 Retry 的 Feature Commit，不要把上一提交已经完成的除法工具再次写进标题。**

---

## 对话 46：什么是状态机，当前项目是否有状态机

### 用户问题

解释什么是状态机，以及当前 Agent Loop 项目是否存在状态机概念。

### 助手回答

## 1. 什么是状态机

状态机（State Machine）是一种描述系统行为的方法：

```text
系统当前处于某个状态
→ 收到一个事件
→ 检查是否允许转换
→ 进入下一个状态
→ 执行对应动作
```

一个完整状态机通常包含：

```text
State       当前状态
Event       触发事件
Transition  状态转换规则
Guard       是否允许转换的条件
Action      转换时执行的动作
```

简单例子：订单状态机。

```text
待支付
  ├─ 收到付款成功 → 待发货
  └─ 收到取消订单 → 已取消

待发货
  ├─ 收到发货事件 → 已发货
  └─ 收到取消申请 → 等待审批

已发货
  └─ 收到签收事件 → 已完成
```

它禁止非法转换，例如：

```text
已完成 → 待支付
```

除非业务明确允许重新打开订单。

## 2. 状态变量不一定等于状态机

只有一个变量：

```python
status = "running"
```

不一定算完整状态机。

还必须存在明确规则：

```text
running 收到 success 才能到 completed
running 收到 cancel 才能到 cancelled
completed 是终态，不能重新执行
```

所以状态机的重点不是“保存状态”，而是“限制状态如何变化”。

## 3. 当前项目有状态机概念吗

有，而且分为显式状态机和隐式状态机。

当前没有一个统一的：

```text
AgentStateMachine
```

类来管理所有状态，但多个模块已经按照状态机方式运行。

## 4. 最明确的显式状态机：Circuit Breaker

文件：

```text
src/pi_agent_loop/retry/circuit_breaker.py
```

状态：

```text
closed
open
half_open
```

转换：

```text
closed
→ 连续瞬时失败达到 failure_threshold
→ open

open
→ recovery_timeout_seconds 到期
→ half_open

half_open
→ 探测成功
→ closed

half_open
→ 探测失败
→ open
```

Guard：

```text
Open 且恢复时间未到
→ 拒绝 Provider 调用
```

这是当前项目中最标准的有限状态机（Finite State Machine）。

## 5. Retry Chain 也是状态机

文件：

```text
src/pi_agent_loop/retry/events.py
```

模型、工具和 Task Retry 都有类似状态：

```text
idle
→ retry_scheduled
→ retry_attempt_start
→ retry_finished(success/failure)
```

如果进程退出：

```text
retry_scheduled
→ 没有 retry_finished
```

`RetryRecoveryManager` 会把它识别成未完成 Chain：

```text
retry_recovery_started
→ Host Recovery Handler
→ retry_recovery_finished
```

由于状态来自 JSONL Event 重建，这也属于事件溯源状态机。

## 6. Agent 生命周期是隐式状态机

相关文件：

```text
src/pi_agent_loop/agent.py
src/pi_agent_loop/loop.py
```

可以抽象为：

```text
idle
→ routing/planning
→ requesting_model
→ executing_tools
→ requesting_model
→ completed
```

异常路径：

```text
requesting_model → retry_backoff
executing_tools  → tool_error/tool_timeout
auto             → budget_exceeded
任意运行状态      → cancelled
```

当前代码通过以下内容表达状态：

- `Agent.state.is_streaming`；
- `_active_token`；
- `has_more_tool_calls`；
- `first_turn`；
- `CancellationToken`；
- `agent_start/turn_start/turn_end/agent_end`；
- `message_start/update/end`；
- `tool_execution_start/update/end`。

但这些状态和转换分散在控制流、布尔变量和事件中，所以属于隐式状态机。

## 7. EventStream 也是小型状态机

文件：

```text
src/pi_agent_loop/event_stream.py
```

状态：

```text
open
→ push 普通事件
→ 继续 open

open
→ done/error/end
→ closed
```

终态后再次 push：

```text
直接忽略
```

`_done` 和 terminal event 共同维护这个状态机。

## 8. CancellationToken 也是两状态机

文件：

```text
src/pi_agent_loop/cancellation.py
```

```text
active
→ cancel(reason)
→ cancelled
```

特点：

- Cancelled 是终态；
- 重复 cancel 不改变第一次原因；
- 父取消向子传播；
- 子取消不反向影响父和兄弟；
- detach 解除父子关系。

## 9. 工具执行生命周期也是隐式状态机

```text
received
→ validating
→ prepared
→ waiting_for_semaphore
→ executing
→ retry_backoff
→ retry_attempt
→ success/error/timeout/cancelled
→ finalized
→ tool_result_written
```

并行时，每个 Tool Call 都有自己的状态机。

因此不能只用一个全局：

```text
agent_state=executing_tool
```

因为不同工具可能同时处于：

```text
A：success
B：retry_backoff
C：executing
D：timeout
```

## 10. Routing Status 是不是状态机

`RequestDecision.status` 包括：

```text
in_scope_no_tool
in_scope_tool_ready
in_scope_need_clarification
in_scope_capability_missing
in_scope_approval_required
out_of_scope
prohibited
```

它更接近一次分类结果，而不是完整动态状态机。

原因是它目前只生成一次决定，没有实现：

```text
approval_required
→ approved
→ executing
→ completed
```

所以它是状态枚举，但还不是完整 Approval 工作流状态机。

## 11. Outcome Unknown 也需要状态机

写操作可能经历：

```text
not_started
→ submitted
→ outcome_unknown
→ reconciling
→ succeeded/failed/not_found
```

当前已经实现：

```text
outcome_unknown
→ OutcomeReconciliationRegistry
→ 核对最终结果
```

但还没有统一持久化写操作状态机。

## 12. 状态机与 Workflow 的区别

状态机主要回答：

```text
现在处于什么状态？
收到事件后允许去哪一个状态？
```

Workflow 主要回答：

```text
整个任务需要按什么步骤执行？
步骤之间有什么依赖？
```

例如复合计算：

```text
add
add
multiply
```

是 Workflow/动态计划。

每个 Tool Call 的：

```text
waiting → executing → success
```

是状态机。

两者可以组合。

## 13. 与 Pi 和 DeepSeek Harness 的关系

Pi 的低层 Agent Loop 主要使用事件和控制流表达隐式状态机；AgentSession 在外层处理 Retry、Compaction、Session 和 UI 状态。

DeepSeek Harness 的状态边界更显式：

- Agent phase kind；
- Turn/Step 边界；
- Durable Session Event；
- `llm/retry` 和 `llm/retry-started`；
- 工具 Pre/Execute/Post/Result；
- Invariant 校验。

当前 Python 项目正在从 Pi 风格的简单事件循环，逐步吸收 DeepSeek Harness 的 Durable Event 和状态不变量思想。

## 14. 是否需要建立统一状态机

对于当前计算教学示例，没有必要立刻把所有代码改成一个庞大 StateMachine 类。

但未来增加以下能力时值得正式化：

- 持久 Session；
- Approval 后恢复；
- 多 Intent PlanExecutor；
- 写操作 Outcome Reconciliation；
- 崩溃恢复；
- 分布式 Worker；
- UI 恢复和审计。

建议分层：

```text
RunState
idle/routing/planning/requesting_model/executing_tools/
waiting_approval/retrying/compacting/completed/failed/cancelled

ToolCallState
received/validated/queued/executing/backoff/
succeeded/failed/timed_out/cancelled

TaskState
pending/ready/running/waiting_dependency/
waiting_approval/succeeded/failed/skipped
```

## 15. 推荐实现位置

不要把统一状态投影塞进 `loop.py`。

可以新增：

```text
src/pi_agent_loop/runtime/
├─ states.py
├─ events.py
├─ reducer.py
├─ invariants.py
└─ projection.py
```

设计：

```text
Agent Loop 继续发事件
→ Reducer 根据事件更新状态
→ Invariant 检查非法转换
→ Projection 给 CLI/TUI/Web 展示
→ Session Store 持久化 Event
```

## 16. 最终结论

当前项目：

```text
有显式 Circuit Breaker 状态机
有 Retry Chain 事件状态机
有 EventStream 和 CancellationToken 小状态机
有 Agent/Tool 生命周期隐式状态机
有 Routing 状态枚举
没有统一、持久化的完整 Agent Run 状态机
```

> **状态机就是“状态 + 事件 + 合法转换规则”。当前项目已经大量使用状态机思想，其中 Circuit Breaker 最显式；Agent Loop、工具、Retry、EventStream 和取消则主要通过事件与控制流形成隐式状态机。**

---

## 对话 47：用通俗语言重新解释状态机

### 用户问题

上一轮关于状态机的说明较难理解，要求使用通俗易懂的方式重新回答。

### 助手回答

## 一句话解释

状态机就是：

> **给程序规定一张“现在走到哪一步，以及下一步允许做什么”的流程表。**

## 1. 用红绿灯理解

红绿灯只有几种状态：

```text
红灯
黄灯
绿灯
```

它不能随便变化，而是有固定规则：

```text
红灯 → 绿灯
绿灯 → 黄灯
黄灯 → 红灯
```

当前是红灯时，不能莫名其妙直接变成另一个不存在的状态。

这就是最简单的状态机。

## 2. 用订单理解

订单可能处于：

```text
待付款
待发货
已发货
已完成
已取消
```

用户付款后：

```text
待付款 → 待发货
```

商家发货后：

```text
待发货 → 已发货
```

用户签收后：

```text
已发货 → 已完成
```

状态机负责阻止明显错误，例如：

```text
已完成的订单突然变成待付款
```

所以状态机可以理解为程序的“流程警察”。

## 3. 当前 Agent 项目有没有状态机

有。

只是当前没有一个名字叫：

```text
AgentStateMachine
```

的大文件统一管理，而是分散在 Agent、工具、重试和 Circuit Breaker 中。

## 4. Agent 自己的流程

用户提出问题后，Agent 大概这样变化：

```text
空闲
→ 正在请求模型
→ 模型要求调用工具
→ 正在执行工具
→ 工具完成
→ 再次请求模型
→ 最终回答
→ 回到空闲
```

例如计算：

```text
（2+3）×（5+6）
```

实际过程：

```text
Agent 空闲
→ 请求模型
→ 执行两个 add
→ 请求模型
→ 执行 multiply
→ 请求模型
→ 输出 55
→ 结束
```

虽然代码没有把这些状态集中写成一张表，但运行过程本身就是状态变化。

## 5. 每个工具也有自己的状态

一个 divide 工具可能经历：

```text
等待执行
→ 正在执行
→ 执行成功
```

失败时：

```text
等待执行
→ 正在执行
→ 瞬时失败
→ 等待重试
→ 再次执行
→ 成功
```

或者：

```text
正在执行
→ 超时
→ 失败结束
```

这也是状态机。

并行时，不同工具可以处于不同状态：

```text
add：已经成功
multiply：正在执行
divide：正在等待重试
```

## 6. Circuit Breaker 是最明显的状态机

Circuit Breaker 可以理解为“自动断电保护器”。

正常情况：

```text
Closed（正常工作）
```

服务连续失败：

```text
Closed → Open（暂时断开）
```

Open 时，新请求不会继续打到故障服务，而是快速失败。

等待一段时间后：

```text
Open → Half-open（试一次）
```

试成功：

```text
Half-open → Closed
```

试失败：

```text
Half-open → Open
```

这个模块就是一个写得非常明确的状态机。

## 7. Retry 也是状态机

重试过程：

```text
没有重试
→ 已安排重试
→ 正在等待
→ 开始下一次尝试
→ 重试成功或失败
```

项目中的事件：

```text
model_retry_scheduled
model_retry_attempt_start
model_retry_finished
```

就是在告诉程序和 UI：重试现在走到哪一步了。

## 8. CancellationToken 也可以看成状态机

它只有两个状态：

```text
正常
已取消
```

变化过程：

```text
正常 → cancel() → 已取消
```

一旦取消，不能再变回正常。

## 9. 状态机有什么用

### 防止重复操作

```text
已经成功的退款不能再次执行
```

### 防止错误顺序

```text
没有审批不能执行取消订单
```

### 让 UI 知道正在做什么

```text
正在请求模型
正在执行工具
正在等待重试
已经完成
```

### 崩溃后恢复

```text
上次停在等待重试
→ 重启后知道从哪里继续
```

### 方便排查问题

```text
到底是在请求模型时失败
还是在工具执行时失败
还是在等待重试时失败
```

## 10. 状态机和任务步骤的区别

任务步骤表示：

```text
先做什么，再做什么
```

例如：

```text
先 add
再 multiply
```

状态机表示：

```text
当前这一步正在什么状态
```

例如 add：

```text
等待 → 执行中 → 成功
```

可以把它理解为：

```text
任务步骤 = 路线图
状态机 = 每个位置上的交通规则
```

## 11. 当前项目的准确情况

```text
Agent 运行过程       有状态变化，但目前分散在代码中
工具执行过程        有状态变化
模型和工具重试      有状态变化和正式事件
CancellationToken   有两个明确状态
Circuit Breaker     有完整、明确的状态机
统一 Agent 状态机   目前没有
```

## 12. 现在是否必须增加统一状态机

当前加减乘除、模型调用和工具 Retry 已经能正常运行，不需要马上重写。

以后实现以下功能时，再增加统一状态机会更有价值：

- 用户审批后继续执行；
- 多业务任务；
- 崩溃恢复；
- 持久 Session；
- Web/TUI 显示当前进度；
- 分布式 Worker。

## 最终总结

状态机可以用一句大白话理解：

> **程序现在正在做什么，下一步允许做什么，不允许做什么。**

当前项目已经有很多小状态机，其中 Circuit Breaker 最明显；Agent 和工具本身也有状态变化，只是还没有集中成一个统一的大状态机。

---

## 对话 48：Pi 与 DeepSeek Harness 的状态机架构及当前项目分层建议

### 用户问题

1. 检查 Pi 和 DeepSeek Harness 的状态机架构；
2. 判断状态机是否应与 Agent Loop 放在不同文件夹；
3. 状态机是否要按不同项目定义，以及它如何知道项目当前处于什么状态。

### 助手回答

## 核心结论

Pi 和 DeepSeek Harness 都没有把所有状态塞进一个巨大 `StateMachine` 类。

它们共同采用的是分层方式：

```text
低层 Agent Loop
负责当前一次模型和工具执行

实时运行状态
负责 idle/running/cancel 等进程内控制

持久事件日志
记录已经发生的事实

Reducer/Projection
根据事件推导当前状态

业务状态机
由具体项目定义订单、退款、审批等规则
```

对于当前 Python 项目，推荐：

> **把通用、可恢复的状态机放在 Agent Loop 外层的独立目录；只把与循环互斥、取消、Turn/Step 强相关的极小运行 Phase 留在 Agent/Loop 附近。**

## 1. Pi 的简单 Agent 状态架构

相关源码：

```text
pi/packages/agent/src/agent.ts
pi/packages/agent/src/agent-loop.ts
```

Pi 的基础 `Agent` 没有显式 `Phase` 联合类型，而是通过：

```text
activeRun 是否存在
state.isStreaming
state.streamingMessage
state.pendingToolCalls
state.errorMessage
AbortController
```

表达实时状态。

大致流程：

```text
activeRun 不存在
→ idle

prompt()
→ 创建 activeRun
→ isStreaming=true
→ runAgentLoop()

message/tool 事件
→ processEvents() 更新 AgentState

agent_end listener 完成
→ finishRun()
→ isStreaming=false
→ activeRun=undefined
→ idle
```

这属于轻量隐式状态机。

Pi 的低层 `agent-loop.ts` 主要负责：

```text
Outer Follow-up Loop
Inner Tool/Steering Loop
模型请求
工具执行
消息回灌
生命周期事件
```

它没有承担完整 Session 恢复状态机。

## 2. Pi 新 Harness 的持久状态架构

Pi 的 Harness 已经比基础 Agent 更接近正式状态机：

```text
pi/packages/agent/src/harness/reducer.ts
pi/packages/agent/src/harness/session/types.ts
pi/packages/agent/src/harness/agent-harness.ts
```

它没有把状态直接保存成一份可随意修改的对象，而是先记录事实：

```text
operation_started
operation_finished
abort_requested
step_attempt
tool_started
queue_enqueued
queue_cancelled
write_deferred
usage
```

然后使用：

```python
reduceLaneState(...)
```

根据事件和记录推导 `LaneState`：

```text
当前是否有 Operation
Operation 是 run/compaction/navigation
是否 aborting
当前 Step 和 Attempt
当前 Tool Batch
哪些 Tool Result 还没完成
Steer/Follow-up 队列
Pending Write
Deferred Operation
Overflow Recovery 是否已使用
```

这叫：

```text
Event Sourcing + Reducer
事件溯源 + 状态归约
```

## 3. Pi 如何防止非法状态

`harness/reducer.ts` 中的 `validateRecordLog()` 会拒绝不可能出现的记录组合，例如：

```text
同时存在多个未结束 Operation
记录引用不存在的 Operation
Operation Finished 后还有新记录
Step Attempt 编号不连续
Tool Invocation 重复
Tool Call ID/Name 与 Assistant 不一致
Abort 后继续向队列写入
Deferred Handle 无效
```

发现这些情况不会“猜一个状态修复”，而是抛出：

```text
RecordLogCorruption
```

这说明 Pi Harness 的状态机核心不只是 enum，而是：

```text
持久记录
+ Reducer
+ Invariant
```

## 4. Pi 为什么把状态逻辑与 Agent Loop 分开

Pi 的目录已经体现分层：

```text
agent-loop.ts
低层模型和工具循环

agent.ts
进程内运行生命周期

harness/reducer.ts
持久状态推导

harness/session/*
事件、记录、JSONL/内存存储

agent-harness.ts
运行、恢复、队列、工具和配置编排
```

这样 Agent Loop 不需要知道：

- JSONL；
- Lane；
- Operation 恢复；
- 历史记录损坏；
- UI Projection；
- Compaction 持久状态。

## 5. DeepSeek Harness 的实时 Phase 状态机

相关源码：

```text
deepseek-harness/packages/core/agent-loop/src/agent.ts
```

DeepSeek Harness 在 Agent Loop 类中明确定义：

```typescript
type Phase =
  | { kind: 'idle'; lastTurn: number }
  | { kind: 'maintenance'; ... }
  | { kind: 'running'; turn: number; step: number; ... }
```

状态：

```text
idle
maintenance
running
```

`setPhase()` 是唯一提交 Phase 的入口，并在对外状态变化时发送：

```text
agent/status idle|running
```

为什么这一小部分放在 Agent Loop 文件里？

因为它直接控制：

- 同一 Agent 是否已有 Driver；
- 是否允许进入 Maintenance；
- Turn/Step 编号；
- AbortController；
- Wake 是否需要延后；
- whenIdle() 何时完成。

这些是循环自身的实时互斥状态，和 Loop 关系非常紧密。

## 6. DeepSeek Harness 的 Turn/Step 状态

运行时：

```text
idle
→ wakeDriver
→ running
→ turn/start
→ step/start
→ 模型请求
→ tool/call/tool/result
→ step/end
→ turn/end
→ idle
```

失败时：

```text
模型失败
→ agent/request-error
→ Retry Policy 决定 retry 或 terminal
```

取消时：

```text
AbortSignal
→ turn/end reason=aborted
→ Driver 收敛
→ idle
```

Maintenance 完成后如果有延迟 Wake，则重新启动 Driver。

## 7. DeepSeek Harness 的持久状态与实时状态分离

生命周期文档明确说明：

```text
持久回放事实保存在 session/event
实时控制与状态保存在 agent/*
```

Session 记录：

```text
turn/start
step/start
user/message
assistant/chunk
assistant/message
tool/call
tool/result
step/end
turn/end
llm/retry
llm/retry-started
```

运行状态可以丢失并重新创建，但持久事件可以重放。

这也是为什么 DeepSeek Harness 可以：

- 审计；
- 恢复；
- 重建模型 Context；
- 判断 Retry 进行到哪；
- 修复中断的 Turn/Tool 边界。

## 8. DeepSeek Harness 的 Projection

Session Log 是事实来源，但模型不直接读取全部 Log。

例如：

```text
packages/core/session/src/surface.ts
```

会把事件折叠成模型可见消息 Surface：

```text
user/message      → User Message
assistant/message → Assistant Message
tool/result       → Tool Result
其他边界事件       → 不进入模型消息
```

所以 DeepSeek Harness 也是：

```text
Event Log
→ Fold/Projection
→ 当前模型 Context
```

## 9. 两个项目的架构对比

| 方面 | Pi | DeepSeek Harness |
|---|---|---|
| 简单实时状态 | activeRun、isStreaming 等隐式状态 | Phase 联合类型显式状态 |
| 低层 Loop | 双层 Loop + Lifecycle Event | Driver + Turn + Step |
| 持久事实 | Harness Entry/LaneRecord | SessionEvent |
| 状态重建 | reduceLaneState | Session Fold/Projection |
| 非法状态检查 | validateRecordLog | Session/Event Invariant |
| 工具状态 | tool_started + resultExists | tool/call + tool/result |
| 恢复 | Operation/Lane Resume | Session Replay/Repair |
| UI 状态 | Projection/Snapshot | agent/status + session/event |

共同原则：

```text
实时小状态靠内存
持久事实靠事件日志
当前状态靠 Reducer/Projection 推导
业务规则放在产品层
```

## 10. 状态机是否应和 Agent Loop 放在不同文件夹

答案不是全部分开，也不是全部放一起，而是按职责拆分。

### 应留在 Agent/Loop 附近

只保留很小的实时 Phase：

```text
idle
running
maintenance
cancelled/aborting
```

以及：

- Active CancellationToken；
- 当前 Turn/Step；
- 是否已有运行任务；
- Wake/Steering 是否待处理。

这些状态直接控制 Loop 是否能继续运行。

### 应放在独立目录

以下应独立：

- Event 定义；
- 持久 Session；
- Reducer；
- Invariant；
- Snapshot/Projection；
- Approval 状态；
- Task/Workflow 状态；
- 业务实体状态；
- 崩溃恢复。

## 11. 当前 Python 项目推荐目录

```text
src/pi_agent_loop/
├─ loop.py                 低层模型和工具循环
├─ agent.py                轻量实时生命周期
├─ runtime/
│  ├─ states.py            Run/Tool/Task 状态定义
│  ├─ events.py            状态事件
│  ├─ reducer.py           Event → State
│  ├─ invariants.py        非法转换检查
│  └─ projection.py        CLI/TUI/Web 读取视图
├─ session/
│  ├─ store.py             持久存储接口
│  ├─ jsonl.py             JSONL 实现
│  ├─ replay.py            重放
│  └─ recovery.py          崩溃恢复
└─ domains/
   └─ <业务领域>/
      ├─ states.py         业务状态
      ├─ transitions.py    业务转换规则
      └─ policies.py       权限/审批/幂等
```

依赖方向：

```text
loop.py
→ 只发事实事件

runtime reducer
→ 消费事件并生成状态

session store
→ 保存事件

UI
→ 读取 projection

业务 domain
→ 定义业务转换和策略
```

不要让 `loop.py` 依赖订单、退款、数据库或 Web UI。

## 12. 状态机是否根据不同项目定义

需要分成两层。

### 通用 Agent 状态机

各项目基本相同：

```text
idle
routing
requesting_model
executing_tools
retrying
compacting
waiting_approval
completed
failed
cancelled
```

这些属于 Agent 框架，可以复用。

### 项目业务状态机

每个项目不同。

订单系统：

```text
pending_payment
paid
shipped
completed
cancelled
```

退款系统：

```text
requested
reviewing
approved
processing
refunded
rejected
```

文件编辑：

```text
unobserved
observed
modified
conflict
written
```

部署系统：

```text
pending
building
deploying
healthy
failed
rolled_back
```

所以不能用一个万能状态机描述所有项目业务。

## 13. 状态机如何知道项目当前状态

它不会通过模型“猜”，也不会自动扫描代码后知道。

状态来自可信事实源：

```text
Agent Loop 发出的运行事件
工具执行结果
真实业务 API/数据库
用户 Approval
持久 Session Event
Worker 回调
Retry/Circuit Event
```

核心公式：

```text
旧状态 + 新事件 = 新状态
```

例如订单：

```text
旧状态：paid
事件：shipment_created
新状态：shipped
```

Retry：

```text
旧状态：scheduled
事件：attempt_started
新状态：running
```

工具：

```text
旧状态：executing
事件：tool_result(success)
新状态：succeeded
```

## 14. 用户说的话不是业务事实

用户说：

```text
订单 1001 已经发货了
```

这只是用户输入，不应直接把订单状态改成 `shipped`。

正确流程：

```text
用户输入
→ Router 识别查询/操作 Intent
→ Tool 查询真实订单系统
→ Tool Result 返回 status=shipped
→ 写入业务事件
→ Reducer 更新状态
```

模型负责理解语言，工具和业务系统负责提供事实。

## 15. 重启后如何知道状态

有两种主要方式。

### 重放事件

```text
读取 Session/Event Log
→ 从第一条开始执行 Reducer
→ 重建当前 State
```

### 快照加增量事件

```text
读取最近 Snapshot
→ 读取 Snapshot 之后的 Event
→ Reducer 应用增量
→ 得到当前 State
```

Pi Harness 和 DeepSeek Harness 都大量使用第二种思路的变体。

## 16. 一个项目需要定义什么

建议在 `BUSINESS_REQUIREMENTS.md` 中增加状态表：

| 实体 | 当前状态 | 事件 | 允许的来源状态 | 目标状态 | 事实来源 | 是否审批 |
|---|---|---|---|---|---|---|
| order | paid | shipment_created | paid | shipped | 订单 API | 否 |
| order | paid | cancel_approved | paid | cancelled | 取消 API | 是 |

AI 根据该表生成：

- Enum/Dataclass；
- Transition Table；
- Reducer；
- Invariant；
- 测试；
- Projection。

但业务人员必须确认状态和转换，不能让 AI 自行发明。

## 17. 当前项目下一步建议

不要立刻开发一个覆盖所有业务的巨大 StateMachine。

建议按顺序：

### 第一阶段

增加通用只读状态投影：

```text
Agent Event
→ RunState Reducer
→ 当前 Run Snapshot
```

不改变现有 Agent Loop。

### 第二阶段

将 Agent/Tool/Retry 关键事件写入统一 Session Store，而不只是 Retry JSONL。

### 第三阶段

增加状态不变量：

- completed 后不能重新 executing；
- waiting_approval 时不能执行写工具；
- Circuit Open 时不能发 Provider 请求；
- Tool Finished 后不能再次 Update；
- Retry Finished 后不能再次 Attempt。

### 第四阶段

根据真实业务需求，增加 `domains/<domain>/` 状态机。

## 最终结论

Pi 给出的最佳参考是：

```text
Agent Loop 与持久 Reducer/Session 分离
```

DeepSeek Harness 给出的最佳参考是：

```text
Loop 内保留很小的实时 Phase
持久事实放 Session Event
当前状态通过 Fold/Projection 重建
```

当前 Python 项目应综合两者：

```text
Loop 附近保留 idle/running/cancel 等小状态
独立 runtime/session 目录保存正式状态机、Reducer、Invariant 和恢复
具体项目在 domains/ 下定义自己的业务状态转换
```

> **状态机不会自己知道项目状态；它根据 Agent 事件、工具结果、业务数据库/API、用户审批和持久日志这些可信事实，通过“旧状态 + 事件 = 新状态”计算出当前状态。**

---

## 对话 49：开发 Runtime/Domain 状态机、JSONL 重放与恢复

### 用户问题

根据 Pi 和 DeepSeek Harness 的分层状态机架构建议开始开发。

### 助手回答

状态机第一版已经完成，并按照“Loop 保持低层、状态和持久化放外层、业务状态按项目定义”的架构实现。

## 1. 新增 Runtime 状态目录

```text
src/pi_agent_loop/runtime/
├─ __init__.py
├─ states.py
├─ events.py
├─ reducer.py
├─ invariants.py
├─ projection.py
└─ tracker.py
```

职责：

```text
states.py
RunState、ToolCallState 和 Phase

events.py
稳定 Runtime Event

reducer.py
旧状态 + Event → 新状态

invariants.py
非法状态转换检查

projection.py
面向 CLI/TUI/Web 的只读视图

tracker.py
把现有 Agent Event 转为 Runtime Event，并先持久化再提交状态
```

没有把订单、退款、JSONL 或 UI 状态逻辑塞进 `loop.py`。

## 2. 通用 Run 状态

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

终态：

```text
completed
failed
cancelled
suspended
```

新 Run 可以从终态重新开始，但终态 Run 本身不能继续接收普通事件。

## 3. 每个 Tool Call 独立状态

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

并行工具不会挤进一个全局 Tool 状态。

例如：

```text
Tool A：succeeded
Tool B：retry_backoff
Tool C：executing
```

`RunState.active_tool_count` 会统计尚未结束的工具。

## 4. Runtime Event

新增事件：

```text
run_started
run_finished
run_interrupted
routing_started
routing_finished
turn_started
turn_finished
model_request_started
model_response_finished
model_retry_scheduled
model_retry_attempt_started
model_retry_finished
context_compaction_started
context_compaction_finished
tool_started
tool_retry_scheduled
tool_retry_attempt_started
tool_retry_finished
tool_finished
approval_required
approval_granted
approval_rejected
outcome_unknown
reconciliation_started
reconciliation_finished
budget_exceeded
```

事件包含：

```text
runId
sequence
timestamp
data
```

## 5. Reducer

公开接口：

```python
next_state = reduce_runtime_state(current_state, event)
```

Reducer 不修改旧对象，而是返回新快照。

已经支持：

- Run 开始/结束；
- Routing；
- Turn；
- Model Request/Response；
- Model Retry；
- Context Compaction；
- Tool Start/Retry/Finish；
- Approval；
- Outcome Unknown/Reconciliation；
- Budget Exceeded；
- Process Interrupted。

## 6. Invariant

已经拒绝：

```text
Event sequence 不连续
没有活动 Run 却写运行事件
Run ID 不匹配
Run 终态后继续写事件
未知 Tool Call 结束或重试
Tool 已结束后继续更新
同一个 Tool Call 重复开始
非 waiting_approval 状态批准或拒绝
Run completed 时仍有未结束工具
```

错误类型：

```python
RuntimeInvariantError(code, message)
```

系统发现非法记录后不会猜测修复。

## 7. Runtime State Tracker

```python
tracker = await RuntimeStateTracker.create(store)
agent.subscribe(tracker.listener)
```

流程：

```text
Agent Event
→ 转换为 Runtime Event
→ Reducer 预验证
→ Store 持久化
→ 提交新 RunState
```

持久化失败时，不会先更新内存状态。

支持外层事件：

```python
await tracker.record_external(
    "approval_required",
    {...},
)
```

用于 Router、Approval 和 Outcome 等 Agent Loop 外层状态。

## 8. 新增 Session 状态目录

```text
src/pi_agent_loop/session/
├─ __init__.py
├─ store.py
├─ jsonl.py
├─ replay.py
└─ recovery.py
```

提供：

```text
RuntimeEventStore Protocol
InMemoryRuntimeEventStore
JsonlRuntimeEventStore
replay_runtime_events
RuntimeRecoveryManager
```

## 9. JSONL 持久化与重放

```python
store = JsonlRuntimeEventStore(
    "state/runtime-events.jsonl"
)
```

重放：

```python
state = replay_runtime_events(
    await store.load()
)
```

所有事件按 sequence 重新进入同一个 Reducer，得到当前状态。

## 10. 崩溃恢复

```python
await RuntimeRecoveryManager(store).recover()
```

如果上次 Run 已经完成，保持终态。

如果进程停止时 Run 仍处于：

```text
routing
requesting_model
executing_tools
retrying
compacting
```

恢复时追加：

```text
run_interrupted
```

状态变为：

```text
suspended
```

当前不会擅自重新执行模型或写工具；完整自动恢复仍需要未来消息 Session Store。

## 11. Projection

```python
view = project_runtime_state(tracker.state)
```

返回：

```text
Run ID
phase
中文 phaseLabel
terminal
turn
modelRetryAttempt
activeToolCount
每个 Tool Call 状态
failureCode
routingStatus
lastEvent
sequence
```

UI 不需要自己解析所有事件。

## 12. Hybrid Router 进入同一个 Run

`RoutedAgent` 新增：

```python
RoutedAgent(
    agent,
    router,
    capabilities,
    runtime_tracker=tracker,
)
```

流程：

```text
run_started
→ routing_started
→ HybridModelRouter
→ routing_finished
→ Agent Loop
→ Model/Tool Events
→ run_finished
```

Capability Missing、Prohibited、Out of Scope、Approval Required 等不调用回答模型的结果，也会形成一个完整 Completed Run，并在 Projection 中保存 `routingStatus`。

## 13. 新增 Domain 状态机

```text
src/pi_agent_loop/domains/state_machine.py
```

公开类型：

```text
DomainStateMachine
DomainTransition
DomainEvent
EntityState
DomainTransitionError
```

示例：

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

应用事件：

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

## 14. Domain 状态安全

已经检查：

- Event Entity 与当前 Entity 一致；
- expected_version 与当前版本一致；
- 当前状态允许该 Event；
- Event Source 属于可信来源；
- 需要 Approval 时必须 approved；
- 同一来源状态和事件不能定义两条冲突转换。

用户或模型文本：

```text
订单已经发货
```

不能直接推进业务状态。

只有：

```text
order_api
payment_api
cancel_api
审批事件
```

等配置的可信来源可以推进。

## 15. 业务需求文件更新

`BUSINESS_REQUIREMENTS.md` 新增状态转换表：

| 实体 | 事件 | 允许来源状态 | 目标状态 | 事实来源 | 是否审批 |
|---|---|---|---|---|---|
| order | payment_succeeded | pending_payment | paid | 支付 API | 否 |
| order | shipment_created | paid | shipped | 订单 API | 否 |
| order | cancel_approved | paid | cancelled | 取消 API | 是 |

AI 不得在没有该表的情况下自行发明生产状态。

## 16. AGENTS.md 更新

项目规则现在要求：

- 通用 Reducer/Invariant 放在 `runtime/`；
-持久化放在 `session/`；
-业务状态放在 `domains/`；
-不得写入 `loop.py`；
-用户和模型文本不是状态事实；
-新增状态必须更新需求文件和非法转换测试。

## 17. 新增离线示例

```text
examples/state_machine_usage.py
```

运行：

```bat
python examples\state_machine_usage.py
```

实际输出：

```text
通用 Runtime 状态机
Run ID：...
最终阶段：已完成
模型 Turn：2
Tool divide：succeeded

订单业务状态机
pending_payment
→ payment_succeeded
→ paid
→ shipment_created
→ shipped
→ 非法转换被拒绝
```

事件保存在：

```text
state/state-machine-demo.jsonl
```

## 18. 现有真实示例接入

以下示例现在都会写：

```text
state/runtime-events.jsonl
```

包括：

```text
examples/basic_usage.py
examples/real_model_usage.py
examples/business_routing_usage.py
```

并显示：

```text
运行状态
Runtime Run ID
路由状态投影（业务示例）
```

## 19. 真实 API 回归

业务请求：

```text
查询订单 1002 当前状态
```

结果：

```text
路由状态：in_scope_tool_ready
Intent：order.get_status
Tool Policy：required
最终结果：订单 1002 当前状态：待支付。
模型调用次数：3
运行状态：已完成
路由状态投影：in_scope_tool_ready
Runtime Run ID：98d863c4-...
```

JSONL 中完整记录：

```text
run_started
routing_started
routing_finished
turn_started
model_request_started
model_response_finished
tool_started
tool_finished
turn_finished
turn_started
model_request_started
model_response_finished
turn_finished
run_finished
```

## 20. 新增测试

```text
tests/test_runtime_state_machine.py
```

共 10 项：

-完整 Run/Tool/Retry 生命周期；
-并行工具状态；
-未知 Tool Call Invariant；
-终态后禁止事件；
-JSONL 重放；
-进程中断恢复；
-Tracker 订阅 Agent；
-RoutedAgent Routing 同一 Run；
-Domain 可信来源；
-Approval 和乐观版本。

当前全部测试：

```text
Ran 108 tests
OK
```

同时通过：

- `python -m compileall`；
- `git diff --check`；
- Runtime/Domain 顶层导入；
- state/ Git 忽略检查；
-真实模型业务回归。

## 21. 当前边界

- Runtime Event Store 尚未与完整模型消息 Session Store 合并；
- Suspended Run 可以识别，但自动恢复模型 Context 尚未实现；
- DomainStateMachine 是通用基础，真实项目要提供状态转换表；
- 多 Intent Plan/Task 状态机尚未实现；
- 分布式多写者需要数据库事务、版本号或单写者协议。

> **当前已实现 Pi Harness 风格的“Event + Reducer + Invariant”，并结合 DeepSeek Harness 的“小型实时 Phase + 持久 Session Event”思想：低层 Agent Loop 保持独立，状态推导、持久化、恢复和业务转换全部在外层目录。**

---

## 对话 50：当前还有哪些状态机代码需要继续开发

### 用户问题

目前还有哪些状态机相关代码需要继续开发？

### 助手回答

## 结论

有，但要根据目标区分。

```text
如果目标是学习 Agent Loop、计算工具、Retry 和状态投影
→ 当前状态机已经够用

如果目标是接入真实只读业务
→ 还需要完整 Session Store 和恢复

如果目标是执行退款、取消、支付等写操作
→ 必须继续开发 Approval 和写操作状态机

如果目标是多 Intent、多任务工作流
→ 还需要 Plan/Task 状态机
```

不建议继续增加一个巨大万能状态机，而应按业务风险分层开发。

## 1. 当前已经完成什么

### Run 状态

```text
idle/running/routing/requesting_model/executing_tools/
retrying/compacting/waiting_approval/outcome_unknown/
completed/failed/cancelled/suspended
```

### Tool Call 状态

```text
queued/executing/retry_backoff/succeeded/failed/
timed_out/cancelled/outcome_unknown
```

### 已完成基础能力

- Runtime Event；
- Reducer；
- Invariant；
- Projection；
- JSONL 持久化；
- Replay；
- 崩溃后标记 Suspended；
- Agent Event Tracker；
- Hybrid Routing 同一 Run；
- DomainStateMachine 基础；
- Trusted Source；
- Approval 布尔检查；
- expected_version 乐观锁。

这些已经足够观察一次 Agent Run 当前走到哪里。

## 2. 最高优先级：完整 Session/Operation 恢复状态机

当前只能做到：

```text
发现上次 Run 没结束
→ 标记 suspended
```

还不能做到：

```text
恢复完整消息 Context
→ 判断最后一个模型 Attempt 是否完成
→ 判断工具是否已经执行
→ 安全恢复剩余步骤
```

建议新增：

```text
session/
├─ schema.py
├─ message_store.py
├─ operation_store.py
├─ snapshot.py
├─ migrations.py
└─ resume.py
```

Operation 状态：

```text
created
running
suspending
suspended
resuming
completed
failed
cancelled
```

需要记录：

- 用户消息；
- Assistant Message；
- Tool Call/Tool Result；
- Step Attempt；
- Operation Started/Finished；
-当前配置快照；
-活跃工具列表；
-模型和 Provider。

这是最接近 Pi Harness Lane/Operation 和 DeepSeek Session Replay 的下一步。

## 3. 第二优先级：Approval 状态机

当前 `requires_approval=true` 只会阻止执行并返回：

```text
in_scope_approval_required
```

还没有：

```text
用户批准
→ 恢复原操作
→ 保证只能使用一次批准
→ 过期或撤销
```

建议状态：

```text
requested
waiting
approved
rejected
expired
cancelled
consumed
```

必须增加：

- approvalId；
-请求人；
-审批人；
-操作摘要；
-参数摘要；
-过期时间；
-一次性消费；
-持久化；
-恢复执行；
-审计记录。

不能继续只依赖：

```python
approved=True
```

因为同进程调用者可以自己传入 True。正式系统要验证可信 Approval Record。

## 4. 第三优先级：写操作状态机

当前已有：

```text
outcome_unknown
→ reconciliation
```

但还没有完整写操作流程。

建议：

```text
prepared
waiting_approval
approved
submitting
submitted
succeeded
failed
outcome_unknown
reconciling
compensating
compensated
```

需要结合：

- Idempotency Key；
- Operation ID；
-业务版本号；
-提交前/提交后错误区分；
-结果核对；
-补偿操作；
-重复请求检测。

支付、退款、取消订单前必须完成这一层。

## 5. 第四优先级：Plan/Task 状态机

当前 Agent 可以由模型跨 Turn 动态调用多个工具，但没有显式多 Intent 计划。

建议 Task 状态：

```text
pending
ready
running
waiting_dependency
waiting_approval
retry_backoff
succeeded
failed
skipped
cancelled
```

Plan 状态：

```text
drafting
validating
ready
running
partially_completed
completed
failed
cancelled
```

需要实现：

- Task ID；
-依赖图；
-拓扑排序；
-循环依赖检测；
-整批 Capability 预检；
-整批预算；
-只读并行；
-写任务审批；
-失败传播；
-结果汇总。

## 6. 当前 Invariant 还可以增强

已有 Invariant 主要检查 Run ID、Event Sequence、Tool Terminal 等。

还可增加：

```text
Turn 必须从 1 连续递增
Model Request Start 必须匹配 Response Finish
Retry Attempt 必须连续递增
Retry Finished 后不能再 Attempt
Compaction Started 必须有 Finished
Tool Retry 只能发生在 executing/retry_backoff
Approval ID 只能消费一次
Run Outcome 必须与最后错误一致
Suspended 状态只能通过 Resume 开启新活动
```

Pi Harness 的 `validateRecordLog()` 就非常强调这些不变量。

## 7. Snapshot 和 Schema Migration

当前 JSONL 每次启动都从头重放。

数据较大后需要：

```text
Snapshot
+ Snapshot 之后的增量 Event
```

还应增加：

- event schema version；
-状态 schema version；
-迁移脚本；
-旧版本兼容测试；
-损坏日志检测；
-备份和校验和。

否则代码升级后旧 Session 可能无法读取。

## 8. 多进程和分布式并发

当前 JSONL 适合单进程教学，不适合多 Worker 同时写。

生产环境需要：

-数据库事务；
-单写者协议；
-全局 Sequence；
-乐观锁；
-租约/Lease；
-分布式锁或队列；
-重复事件去重；
-At-least-once 消费处理。

## 9. Domain 状态机仍需要按项目补充

当前 `DomainStateMachine` 只是通用引擎。

它不知道用户最终项目是：

-订单；
-退款；
-库存；
-部署；
-文件编辑；
-客服工单。

正式开发前需要在 `BUSINESS_REQUIREMENTS.md` 填写业务状态转换表。

然后才能生成：

```text
domains/<domain>/states.py
domains/<domain>/transitions.py
domains/<domain>/policies.py
tests/test_<domain>_state_machine.py
```

## 10. 当前一些状态还只是“已定义但未完整接通”

### queued

Tool Call State 中已有 `queued`，但当前 Tracker 在收到 `tool_execution_start` 时直接进入 `executing`，还没有独立的 `tool_queued` 事件。

### waiting_approval

Runtime 支持该状态，但 Hybrid Router 只返回 Approval Required，尚未实现批准后恢复。

### outcome_unknown

有 Error 和 Reconciliation Registry，但业务状态事件尚未统一写入 Runtime Store。

### suspended

崩溃后可以识别，但尚不能恢复模型和工具上下文。

### maintenance

DeepSeek Harness 有 Maintenance Phase，当前项目没有单独的 Maintenance 状态。

## 11. 建议下一步开发顺序

### 如果准备做真实业务，推荐：

```text
第 1 步：ApprovalStateMachine
第 2 步：WriteOperationStateMachine
第 3 步：完整 Session/Operation Resume
第 4 步：加强 Invariant
第 5 步：Snapshot 和 Migration
第 6 步：Plan/Task StateMachine
```

但从底层依赖关系看，完整 Session/Operation Store 是它们的共同基础，所以也可以先完成 Session，再做 Approval。

推荐综合顺序：

```text
Session Schema/Store
→ Approval StateMachine
→ WriteOperation StateMachine
→ Resume
→ Plan/Task StateMachine
→ Distributed Store
```

## 12. 是否现在必须继续

### 不需要继续的情况

```text
只学习 Agent Loop
只运行加法/乘法/除法
只测试模型和工具 Retry
只需要显示当前运行状态
```

当前 108 项测试和状态投影已经够用。

### 必须继续的情况

```text
真实退款/支付/订单取消
用户审批后继续执行
程序崩溃后自动恢复
一个请求包含多个业务任务
多个 Worker 并发执行
Web UI 需要恢复历史任务
```

## 最终建议

当前最值得继续开发的是：

```text
持久 Approval 状态机
+
写操作状态机
+
完整 Session/Operation Resume
```

因为这三项决定真实业务写操作是否安全。

> **当前已经有可用的状态机骨架；下一步不是继续增加更多状态名称，而是把 Approval、写操作和 Session 恢复真正接到持久事件、可信身份、幂等和恢复执行上。**

---

## 对话 51：开发可扩展业务状态骨架、完整 Context 恢复、可信 Approval 和幂等写操作

### 用户问题

不实现具体真实业务状态机，而是继续完成可供后续业务扩展的骨架：

- 恢复完整消息 Context；
- 判断最后一个模型请求是否完成；
- 判断工具是否已经执行；
- 安全继续剩余步骤；
- 把 Approval、写操作和 Session 恢复连接到持久事件、可信身份、幂等和恢复执行。

### 助手回答

可扩展骨架已经完成，没有写死真实订单、退款或支付业务规则。

## 1. 新增完整 Operation Session 事件

```text
src/pi_agent_loop/session/
├─ operation_events.py
├─ operation_store.py
├─ operation_state.py
├─ recorder.py
└─ resume.py
```

与只保存运行投影的 Runtime Event 不同，Operation Event 会保存安全恢复所需的完整事实：

```text
operation_started
operation_finished
routing_started/routing_finished
message_appended
model_request_started
model_request_completed
model_request_failed
tool_intent_recorded
tool_dispatch_started
tool_completed
tool_outcome_unknown
tool_reconciled
approval_*
write_*
```

## 2. 完整消息 Context 持久化

`DurableOperationRecorder` 会记录：

- User Message；
- Assistant Message；
- ToolResult Message；
-模型 Request Started/Completed；
- Tool Call Name/Arguments；
- Tool Dispatch Attempt；
- Tool Final Result；
- Provider/Model 配置快照；
-工具 Replay Policy；
- Operation Outcome。

真实运行后的 Operation State 可以重建：

```text
[user, assistant, toolResult, assistant]
```

因此不再只有 Run Phase，而是拥有恢复模型调用所需的完整 Message Context。

## 3. DurableOperationRecorder

```python
recorder = DurableOperationRecorder(
    JsonlOperationEventStore("state/operation-events.jsonl"),
    session_id="my-session",
    tools=tools,
    configuration={
        "provider": model.provider,
        "model": model.id,
    },
)
agent.subscribe(recorder.listener)
```

Recorder 在工具真正进入 Python Execute 之前持久化：

```text
tool_dispatch_started
```

这使恢复器可以区分：

```text
只有 Tool Intent，还没执行
已经进入 Tool Body，但没有结果
已经得到结果，但 ToolResult Message 未落盘
```

## 4. 工具 Replay Policy

`AgentTool` 新增：

```python
replay_policy: Literal["never", "safe"] = "never"
```

规则：

```text
safe
只读或严格幂等工具，Dispatch 后崩溃可以安全重放

never
写操作或结果不确定，Dispatch 后不能重放，必须 Reconcile
```

当前教学工具：

```text
add
multiply
divide
get_order_status Mock
```

均明确设置：

```python
replay_policy="safe"
```

写工具默认仍是 `never`。

## 5. Operation Reducer

`operation_state.py` 可以从 Operation Event 重建：

```text
Operation Phase
完整 messages
所有 Model Request 状态
所有 Tool Invocation 状态
Tool Arguments
Replay Policy
Dispatch Attempts
Tool Result
Configuration Snapshot
```

并拒绝：

-第一条不是 operation_started；
- Session/Operation ID 不一致；
- Sequence 倒退；
-终态后继续追加；
- Model Completed 没有 Started；
- Tool Dispatch 没有 Intent；
- Tool Result 重复；
-非法 Write/Tool 状态转换。

## 6. Recovery Planner

```python
plan = await DurableSessionRecovery(store).plan(
    session_id="...",
    operation_id="...",
)
```

支持动作：

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
Model Started 但没有 Completed
→ retry_model_request

Assistant Tool Call 已持久化但没有 Tool Intent
→ execute_tool

Tool Intent 已记录但没有 Dispatch
→ execute_tool

Tool Dispatch 后无结果 + replay_policy=safe
→ replay_safe_tool

Tool Dispatch 后无结果 + replay_policy=never
→ reconcile_tool

Tool Result 已持久化但 ToolResult Message 缺失
→ materialize_tool_result

最后消息是 User/ToolResult
→ continue_model

最后 Assistant 已完成且无未解决 Tool Call
→ finish_operation
```

## 7. Recovery Executor

Host 注入可信 Callback：

```python
callbacks = RecoveryCallbacks(
    request_model=request_model,
    execute_tool=execute_tool,
    reconcile_tool=reconcile_tool,
)

result = await DurableSessionRecovery(store).resume(
    session_id="...",
    operation_id="...",
    callbacks=callbacks,
)
```

恢复器会：

```text
加载完整 Context
→ 生成恢复计划
→ 先持久化 Recovery Attempt
→ 执行 Safe Tool 或 Reconcile
→ 补写 ToolResult Message
→ 使用完整 Context 继续模型
→ 重复规划
→ Operation Completed 或 Manual Intervention
```

未完成的旧 Model Request 会先写为：

```text
model_request_failed: process_interrupted
```

然后才发起新的 Request，防止一直把旧请求识别为 Pending。

## 8. Operation Store

提供：

```text
OperationEventStore Protocol
InMemoryOperationEventStore
JsonlOperationEventStore
```

JSONL 实现：

-全局递增 Sequence；
-单进程 Async Lock；
-每次 Append Flush；
-调用 `os.fsync()`；
-格式和 Sequence 校验；
-按 Session/Operation 查询。

本地文件：

```text
state/operation-events.jsonl
```

已被 Git 忽略。

## 9. 可信身份骨架

新增：

```text
src/pi_agent_loop/security/identity.py
```

类型：

```text
IdentityClaim
VerifiedIdentity
IdentityVerifier Protocol
StaticIdentityVerifier
IdentityVerificationError
```

流程：

```text
外部 Claim + Credential
→ IdentityVerifier
→ VerifiedIdentity
→ Approval/Write Service
```

Credential 不会进入持久事件。

`StaticIdentityVerifier` 只用于开发和测试；生产必须替换为 OAuth、IAM、企业 SSO 等真实认证。

## 10. 持久 Approval 状态机

新增：

```text
src/pi_agent_loop/approval/state_machine.py
```

状态：

```text
waiting
→ approved/rejected/expired
approved
→ consumed
```

已实现：

- approvalId；
- Requester 身份；
- Approver 身份；
- Required Role；
-默认禁止自审；
-操作摘要；
-精确 Action SHA-256 Hash；
-过期时间；
-一次性消费；
-操作变更后拒绝消费；
-持久 Approval Event。

不再只依赖可伪造的：

```python
approved=True
```

## 11. 幂等写操作状态机

新增：

```text
src/pi_agent_loop/writes/state_machine.py
```

状态：

```text
prepared
waiting_approval
approved
submitting
succeeded
failed
outcome_unknown
reconciling
```

已实现：

- VerifiedIdentity；
- Approval 一次消费；
- Idempotency Key SHA-256 Hash；
-相同 Key + 相同 Action 返回原 Write ID；
-相同 Key + 不同 Action 返回 idempotency_conflict；
-重复成功请求不再次调用 Handler；
-提交前/后持久事件；
-outcome_unknown 不自动重放；
-状态核对后进入 succeeded/failed。

原始 Idempotency Key 不写入日志，但执行时仍传给真实业务 Handler。

## 12. RoutedAgent 集成

`RoutedAgent` 现在可以同时接收：

```python
runtime_tracker=runtime_tracker
operation_recorder=operation_recorder
```

Routing、零模型 Capability Missing、Prohibited、Approval Required 以及正常模型/工具流程都在同一个 Durable Operation 中。

## 13. 当前示例接入

以下示例现在都会保存：

```text
state/runtime-events.jsonl
state/operation-events.jsonl
```

包括：

```text
basic_usage.py
real_model_usage.py
business_routing_usage.py
```

并显示：

```text
Runtime Run ID
Durable Operation ID
```

## 14. 新增离线演示

```text
examples/durable_session_usage.py
```

运行：

```bat
python examples\durable_session_usage.py
```

演示：

```text
可信 Operator 提交写操作
→ waiting_approval
→ 可信 Approver 批准
→ Approval 一次消费
→ 写操作 succeeded
→ 相同 Idempotency Key 重复请求不重复执行

进程崩溃在 safe divide Dispatch 后
→ Recovery Planner 识别 replay_safe_tool
→ 补写 ToolResult
→ 恢复完整 [user, assistant, toolResult] Context
→ 继续模型
→ Operation completed
```

## 15. 文档规范更新

`TOOLS_IMPLEMENTATION_GUIDE.md` 新增：

- replay_policy；
- Safe/Never 判断；
- DurableOperationRecorder；
-写工具 Approval/Idempotency/outcome_unknown；
- Definition of Done 检查项。

`BUSINESS_REQUIREMENTS.md` 新增：

```text
Session、Approval 和恢复要求
```

要求填写：

- replay_policy；
- Approval Role；
- Idempotency Key 来源；
- Outcome Unknown 核对 API；
-恢复允许动作。

## 16. 新增测试

```text
tests/test_durable_session.py
```

共 10 项：

- Recorder 完整 Context 和 Tool Fact；
-零模型 Routing Durable Operation；
-未完成 Model Request 识别；
-Safe Replay 与 Reconcile 区分；
-Safe Tool Replay 后继续模型；
-错误身份凭证拒绝；
- Approval Role、自审、Action Binding、一次消费；
-写操作 Approval/Idempotency/重复去重；
-outcome_unknown 核对；
-JSONL 重启恢复完整 Context。

当前全部测试：

```text
Ran 118 tests
OK
```

## 17. 真实 API 回归

请求：

```text
查询订单 1003 当前状态
```

结果：

```text
Intent：order.get_status
Tool：get_order_status
最终结果：订单 1003 当前状态：已完成。
运行状态：已完成
Runtime Run ID：...
Durable Operation ID：...
```

重放 Operation 后得到：

```text
phase=completed
messages=[user, assistant, toolResult, assistant]
tool=get_order_status
phase=completed
replay_policy=safe
```

## 18. 当前准确边界

这是供真实业务扩展的安全骨架，不包含具体生产业务状态机。

仍需真实项目提供：

- OAuth/IAM/SSO IdentityVerifier；
-加密数据库 Operation Store；
-真实 Approval UI/API；
-真实写 Handler；
-真实 Outcome Reconciliation API；
-业务 State Transition 表；
-多 Worker 事务/单写者协议。

JSONL 当前是单进程单写者教学实现；完整 Context 和 Tool Arguments 可能含敏感数据，生产存储必须加密、鉴权和设置保留周期。

> **现在已经把“完整 Context → 判断 Model/Tool 是否完成 → Safe Replay 或 Reconcile → 继续模型”连接到持久 Operation Event；同时把 Approval 和写操作连接到 VerifiedIdentity、Action Hash、Idempotency Key Hash、一次性消费和 Outcome Reconciliation。**

---

## 对话 52：完成 Durable Session 后还有哪些状态机代码值得继续开发

### 用户问题

在完成完整 Context、Recovery、Approval 和幂等写操作骨架后，目前还有哪些状态机代码需要继续开发？

### 助手回答

## 结论

当前已经达到“可供后续真实业务扩展的通用状态机骨架”目标。

如果暂时不接真实业务，可以停止继续增加状态机代码。下一步最缺的不是更多 State 名称，而是把现有状态机进一步产品化和生产化。

准确判断：

```text
教学和单进程骨架
→ 已经足够

真实只读业务
→ 主要缺生产 Store 和自动恢复 Host

真实写业务
→ 主要缺 Approval API/UI、真实 Identity 和 Tool Runtime 接线

多 Intent 工作流
→ 仍缺 Plan/Task StateMachine

多 Worker 生产部署
→ 仍缺事务数据库和分布式协调
```

## 1. 当前已经完成的骨架

### Runtime

```text
RunState
ToolCallState
RuntimeEvent
Reducer
Invariant
Projection
Tracker
JSONL Replay
Suspended Recovery
```

### Durable Operation

```text
完整 User/Assistant/ToolResult Context
Model Request Started/Completed/Failed
Tool Intent/Dispatch/Completed/OutcomeUnknown
Recovery Planner
Recovery Executor
Safe Replay
Reconcile
继续模型
```

### Security/Write

```text
VerifiedIdentity
Approval StateMachine
Action Hash Binding
Approval 一次消费
Idempotency Key Hash
WriteOperation StateMachine
outcome_unknown
reconciliation
```

### Domain

```text
DomainStateMachine 通用引擎
Trusted Source
Approval Check
Expected Version
非法转换检查
```

所以通用骨架并不缺少主要模块。

## 2. 现在最缺的是 DurableAgentHost

当前已经有：

```text
Event
Reducer
Store
Recovery Planner
Recovery Executor
Approval Service
Write Service
```

但这些仍由示例手工组装。

Pi Harness 中对应的是 `AgentHarness`，DeepSeek Harness 中对应的是 Agent Factory/Session/Resume Driver。

当前项目还缺一个统一 Host：

```text
src/pi_agent_loop/harness/
├─ durable_agent_host.py
├─ model_runtime_adapter.py
├─ tool_runtime_adapter.py
├─ approval_gateway.py
└─ startup_recovery.py
```

它应负责：

```text
创建 Session
→ 创建 Runtime/Operation Recorder
→ 注入 Model/Tool Recovery Callback
→ 扫描 Suspended Operation
→ 恢复工具和模型配置
→ 执行 Safe Replay/Reconcile
→ 继续 Agent
→ 关闭前 Flush
```

这是最值得继续开发的一项。

## 3. Recovery Callback 还需要正式 Runtime Adapter

当前：

```python
RecoveryCallbacks(
    request_model=...,
    execute_tool=...,
    reconcile_tool=...,
)
```

由 Host 注入。

这很适合作为骨架，但生产接入前应增加：

```text
RecoverableModelRuntime
RecoverableToolRuntime
```

确保恢复调用仍经过：

- Provider Retry；
- Circuit Breaker；
- Tool Schema；
- validate_args；
- Permission；
- Approval；
- Timeout；
- Tool Retry；
- RequiredToolCallGuard；
- Usage 和审计。

不能让 Recovery Callback 直接调用业务函数并绕过现有安全管线。

## 4. Approval 还缺“批准后恢复原 Agent Run”

当前 Approval 状态机已经支持：

```text
waiting
approved/rejected/expired
consumed
```

但业务入口尚未自动完成：

```text
RoutedAgent 返回 approval_required
→ 创建 Approval Request
→ 用户在 API/UI 批准
→ 找回原 Operation
→ 消费 Approval
→ 恢复 Write Operation
→ 继续 Agent 回答
```

还需要：

```text
ApprovalResumeCoordinator
Approval API/TUI/Web Gateway
Approval Notification
Approval Expiration Scheduler
```

这属于通用骨架产品化，不是具体业务状态定义。

## 5. Operation Recovery 状态还可以更显式

当前 Operation 主要是：

```text
running/completed/failed/cancelled
```

恢复动作由 Planner 表达。

如果要让 UI 和多 Worker 清晰观察，可以增加：

```text
suspending
suspended
resume_requested
resuming
manual_intervention
```

以及事件：

```text
operation_suspended
operation_resume_requested
operation_resume_started
operation_resume_finished
operation_manual_intervention
```

目前功能可以恢复，但恢复生命周期本身还没有完整投影成状态。

## 6. Invariant 还可以加强

建议继续增加：

```text
Turn 必须连续递增
Model Request Start/Finish 必须一一配对
Tool Dispatch Attempt 必须连续
Tool Completed 后不得再次 Dispatch
Approval Event 必须属于同一 Operation
Approval Consumed 后不能再次使用
Write Succeeded 后不能再变成 Failed
Operation Finished 时不能存在 Pending Model/Tool/Approval
Safe Replay 必须匹配 replay_policy
Reconcile 只能用于 never/outcome_unknown
```

Pi Harness 的 RecordLogCorruption 检查就是很好的参考。

## 7. Snapshot、Schema Version 和 Migration

当前 Runtime/Operation JSONL 会从头重放。

后续建议增加：

```text
session/schema.py
session/snapshot.py
session/migrations.py
session/checksum.py
```

需要：

- Event Schema Version；
- Snapshot Version；
- Snapshot + 增量 Event；
-旧版本迁移；
-日志校验和；
-损坏检测；
-备份和恢复测试。

否则事件越来越多后，启动会越来越慢，代码升级也可能无法读取旧数据。

## 8. 生产级 Store

当前两个 JSONL：

```text
runtime-events.jsonl
operation-events.jsonl
```

适合单进程教学。

生产环境需要统一事务 Store，例如 SQLite/PostgreSQL：

```text
append events
update operation version
consume approval
claim idempotency key
写业务 intent
```

最好在同一事务内完成。

还需要：

-单写者或 Lease；
-全局 Sequence；
-唯一 Idempotency Constraint；
-并发版本检查；
-加密；
-访问控制；
-数据保留和删除策略。

## 9. 可信身份仍需生产 Adapter

当前 `StaticIdentityVerifier` 只是测试实现。

真实系统需要：

```text
OAuth/OIDC
JWT 验签
IAM
企业 SSO
服务账号
角色和 Scope
```

还要确保重启后持久事件中的 Identity 可以被重新验证，而不是只相信一个字符串 ID。

## 10. 写操作状态机还缺补偿流程

当前写操作完成到：

```text
succeeded/failed/outcome_unknown/reconciling
```

复杂业务可能需要：

```text
compensation_requested
compensating
compensated
compensation_failed
```

例如：

```text
退款成功
但积分扣回失败
→ 启动补偿任务
```

是否需要这部分，要由真实业务决定，不应现在写死。

## 11. 多 Intent Plan/Task 状态机仍未实现

用户如果提出：

```text
查询订单 1001，同时取消订单 1002，并把结果发给客服
```

仍需要：

```text
PlanStateMachine
TaskStateMachine
Dependency Graph
Approval Barrier
Result Synthesis
```

这和当前单 Operation 恢复不同，属于下一大模块。

## 12. 当前有些状态仍可细化

### queued

定义了 Tool `queued`，但还没有独立 `tool_queued` 持久事件。

### maintenance

还没有 DeepSeek Harness 风格的 Maintenance Phase，用于 Compaction、Migration、Snapshot 等排他操作。

### Circuit

Circuit Breaker 状态仍是进程内，没有写入统一 Session Store。

### Retry Journal

Retry Journal 和 Operation Journal 尚未统一事务。

## 13. 推荐继续开发顺序

如果继续完善通用骨架，推荐：

```text
1. DurableAgentHost
2. RecoverableModelRuntime/ToolRuntime
3. ApprovalResumeCoordinator
4. Operation Resume 状态事件和更强 Invariant
5. SQLite Transaction Store
6. Snapshot/Migration
7. Plan/Task StateMachine
8. Distributed Lease/Worker
```

## 14. 当前是否可以先停止

可以。

如果现在的目标是：

```text
等真实业务需求到来
→ 根据 BUSINESS_REQUIREMENTS.md 增加 Domain 状态机
```

当前骨架已经足够。

以后业务接入时，只需要提供：

-真实 Identity Provider；
-实际工具和 Capability；
- Approval Role；
- Idempotency Key 生成规则；
- Outcome Reconciliation API；
- Domain Transition 表。

## 最终建议

不要继续为了“看起来完整”而堆更多状态。

当前下一步若继续，最有价值的是：

```text
DurableAgentHost
+
ApprovalResumeCoordinator
+
事务型 Session Store
```

它们负责把现有骨架真正组合成可启动、可暂停、可恢复的产品运行时。

> **当前状态机核心骨架已经完成；剩余工作主要是 Host 编排、生产存储、审批交互和多任务计划，而不是继续定义更多状态名称。**

---

## 对话 53：编写 AI 必读的真实业务状态机实现指南

### 用户问题

编写一个 Markdown，说明后续接入真实业务状态机时应该怎样写，让 AI 可以读取；同时在对话中用通俗语言解释。

### 助手回答

已经新增：

```text
agent-loop/STATE_MACHINE_IMPLEMENTATION_GUIDE.md
```

该文件约 920 行，是后续 AI 实现真实订单、退款、库存、工单、部署等业务状态机时的完整开发规范。

## 1. 文档之间的关系

为了避免重复配置：

```text
BUSINESS_REQUIREMENTS.md
保存“你的真实业务是什么”

STATE_MACHINE_IMPLEMENTATION_GUIDE.md
保存“AI 应该怎样安全实现”
```

真实状态、事件和转换只填写一份：

```text
BUSINESS_REQUIREMENTS.md
```

状态机指南不会再复制一份真实业务状态，避免两份文档不一致。

## 2. AGENTS.md 已强制读取

项目规则现在要求：新增或修改以下内容前，AI 必须完整阅读状态机指南：

```text
State
Event
Transition
Reducer
Approval
WriteOperation
Recovery
```

读取顺序：

```text
AGENTS.md
→ BUSINESS_REQUIREMENTS.md
→ TOOLS_IMPLEMENTATION_GUIDE.md（涉及工具时）
→ STATE_MACHINE_IMPLEMENTATION_GUIDE.md
→ 现有源码和测试
```

## 3. 通俗解释五个概念

### State

现在处于什么阶段：

```text
pending_payment
paid
shipped
```

### Command

用户希望做什么：

```text
CancelOrder
```

Command 只是请求，不代表成功。

### Event

真实发生了什么：

```text
order_cancelled
```

只有可信 Event 才能改变状态。

### Guard

允许改变之前要检查什么：

```text
当前必须是 paid
身份必须有 operator 角色
必须已经审批
版本号必须正确
```

### Action

真正调用什么外部系统：

```text
调用取消订单 API
```

Reducer 只计算状态，不能在里面调用 API。

## 4. 最重要的规则

错误做法：

```text
用户说“取消订单”
→ 直接 state=cancelled
```

正确做法：

```text
用户发出 CancelOrder Command
→ Router 理解 Intent
→ 权限检查
→ Approval
→ WriteOperationService
→ cancel_order Tool/API
→ API 确认真实成功
→ 产生 order_cancelled Event
→ Reducer 把状态改成 cancelled
```

模型只理解语言，不能宣布业务事实。

## 5. 业务人员要填写什么

`BUSINESS_REQUIREMENTS.md` 中填写：

| 实体 | Event | 来源状态 | 目标状态 | 事实来源 | 审批 | 幂等/核对 |
|---|---|---|---|---|---|---|
| order | shipment_created | paid | shipped | 订单 API | 否 | 按业务定义 |
| order | cancel_approved | paid | cancelled | 取消 API | 是 | Idempotency Key |

还要说明：

```text
实体 ID
初始状态
终态
状态保存位置
版本字段
可信事件来源
Approval Role
Outcome Unknown 核对 API
```

业务人员不需要写 Agent Loop。

## 6. AI 编码前必须询问

指南要求 AI 确认：

-业务实体是什么；
-唯一 ID；
-初始状态；
-终态；
-每个事件允许的来源状态；
-目标状态；
-事实来源；
-是否审批；
- Idempotency Key；
- Timeout 后如何核对；
-是否补偿；
- expected_version；
-存储和恢复方式。

信息缺失时只能实现接口或 Mock，不能编造生产流程。

## 7. 推荐生成目录

```text
src/pi_agent_loop/domains/<domain>/
├─ states.py
├─ events.py
├─ transitions.py
├─ reducer.py
├─ policies.py
├─ repository.py
├─ service.py
├─ projection.py
└─ recovery.py
```

测试：

```text
tests/domains/test_<domain>_transitions.py
tests/domains/test_<domain>_service.py
tests/domains/test_<domain>_recovery.py
```

## 8. 与当前骨架的连接

```text
Domain State Transition
→ DomainStateMachine

运行状态
→ RuntimeStateTracker

完整消息和工具事实
→ DurableOperationRecorder

崩溃恢复
→ DurableSessionRecovery

可信身份
→ IdentityVerifier/VerifiedIdentity

审批
→ ApprovalService

幂等写操作
→ WriteOperationService

工具恢复
→ replay_policy safe/never
```

真实业务只提供状态表、Adapter、Tool 和 Policy，不需要重新实现 Agent Loop。

## 9. Command 与 Event 必须分离

```text
Command：请求取消订单
Event：订单已经真实取消
```

如果 API 失败，不能产生成功 Event。

如果 API Timeout：

```text
Write Operation → outcome_unknown
→ Reconciliation API
→ 确认成功后才产生 Event
```

## 10. 可信事实来源

允许：

```text
payment_api
order_api
inventory_worker
approval_service
reconciliation_service
```

不允许直接驱动状态：

```text
user_message
assistant_text
model_reasoning
```

生产 Source 还要绑定可信 Adapter 和 VerifiedIdentity，不能只相信一个字符串。

## 11. Approval、幂等和恢复

指南规定：

- Approval 必须绑定 Action Hash；
-禁止默认自审；
-只能消费一次；
-写操作使用 Idempotency Key；
-日志只保存 Key Hash；
-同 Key 不得用于不同 Action；
- safe 只用于只读/严格幂等 Tool；
-写工具默认 replay_policy=never；
- Dispatch 后崩溃的 Never Tool 必须 Reconcile；
- Recovery Callback 不能绕过 Guard、权限和 Approval。

## 12. 并发控制

业务事件携带：

```text
entity_id
expected_version
```

版本不一致：

```text
stale_version
→ 重新读取
→ 重新判断是否还能执行
```

防止多个 Worker 相互覆盖状态。

## 13. 纯 Reducer

```python
next_state = reduce(current_state, event)
```

Reducer 可以：

-验证转换；
-返回新状态；
-增加版本；
-记录最近事件。

Reducer 不能：

-请求 HTTP；
-写数据库；
-调用模型；
-发送消息；
-执行随机副作用。

外部 Action 成功后再产生 Event。

## 14. 状态持久化要求

业务事件建议包含：

```text
event_id
entity_id
entity_type
event_type
sequence/version
timestamp
source
actor_id
correlation_id
causation_id
operation_id
payload
schema_version
```

禁止存储：

```text
API Key
Password
Authorization
Cookie
明文 Idempotency Key
不必要隐私数据
```

## 15. 必须测试的情况

-正常转换；
-非法来源状态；
-不可信 Source；
-无 Approval；
-重复 Approval 消费；
- Action Hash 不匹配；
- expected_version 过期；
-同 Idempotency Key 重复请求；
-Safe Tool 崩溃恢复；
-Never Tool Reconcile；
-outcome_unknown；
-终态后非法事件；
-完整 Context 恢复。

## 16. 常见错误设计

指南明确禁止：

```text
模型文本直接修改业务状态
把 Command 当成功 Event
Reducer 中调用 API
所有状态互相转换
写工具随意设置 safe
只用 approved=True
明文保存 Idempotency Key
Timeout 后直接重试写操作
没有 expected_version
为了自动恢复而猜结果
```

## 17. Definition of Done

指南最后提供完整检查表，包括：

-业务状态表已确认；
-初始/终态明确；
-Command/Event 分离；
-Transition 完整；
-Trusted Source；
-纯 Reducer；
-VerifiedIdentity；
-Approval Action Hash；
-Idempotency；
-replay_policy；
-outcome_unknown/Reconcile；
-expected_version；
-Durable Event；
-完整 Context Recovery；
-Projection；
-正常和非法测试；
-敏感信息安全；
-全部测试通过。

## 18. 测试和文档更新

`tests/test_business_requirements.py` 新增指南契约测试，确保：

- AGENTS.md 必须引用状态机指南；
-指南必须包含 Command/Event、可信来源、Approval、幂等、Replay、Session、并发、纯 Reducer、测试和完成标准。

当前全部测试：

```text
Ran 119 tests
OK
```

`README.md` 已增加状态机指南章节。

## 19. 后续让 AI 开发时的推荐提示词

```text
请先完整阅读：
1. AGENTS.md
2. BUSINESS_REQUIREMENTS.md
3. TOOLS_IMPLEMENTATION_GUIDE.md（涉及工具时）
4. STATE_MACHINE_IMPLEMENTATION_GUIDE.md

根据 BUSINESS_REQUIREMENTS.md 中已经确认的业务状态转换表，为 <domain> 生成可直接运行的状态机。

要求：
- 不得自行发明 State、Event、API、权限、审批或补偿规则；
- 先列出缺失信息并向我确认；
- Command 与 Event 分离；
- 只允许可信事实来源推进状态；
- 接入 VerifiedIdentity、ApprovalService、WriteOperationService；
- 定义 replay_policy、Idempotency 和 outcome_unknown Reconciliation；
- 使用 expected_version；
- 接入 DurableOperationRecorder 和 DurableSessionRecovery；
- 生成 states/events/transitions/reducer/policies/repository/service/projection/recovery；
- 增加正常、非法、并发、审批、幂等和恢复测试；
- 运行全部测试并汇报尚未提供的真实业务信息。
```

## 最通俗总结

以后你不需要告诉 AI 怎样写代码，只需要告诉它：

```text
有哪些业务状态
什么真实事件能让状态变化
哪些状态允许变化
谁有权限
是否审批
失败后怎样核对
```

AI 根据指南把这些内容生成状态机代码，并接入当前已经完成的身份、审批、幂等、持久 Context 和恢复骨架。

> **`BUSINESS_REQUIREMENTS.md` 负责写真实业务规则，`STATE_MACHINE_IMPLEMENTATION_GUIDE.md` 负责告诉 AI 怎样把规则安全地变成代码。**

---

## 对话 54：当前串行和并行工具调度如何判断

### 用户问题

目前串行和并行功能怎么样？系统怎样判断哪些工具可以并行、哪些必须串行？

### 助手回答

## 核心结论

当前串行和并行功能已经可用，但它不是由模型自动判断安全性，而是由代码配置决定：

```text
Agent 全局 tool_execution
+
每个 AgentTool.execution_mode
+
max_parallel_tools
```

当前判断规则：

```text
Agent 全局设置 sequential
→ 整批串行

当前批次中任何一个工具声明 sequential
→ 整批串行

否则
→ 整批并行，但受 max_parallel_tools 限制
```

系统不会分析函数源码后自动知道是否安全，也不会仅凭模型说“可以并行”就并行。

## 1. 通俗理解

可以把工具执行想象成饭店厨房。

### 可以并行

```text
同时烤面包
同时煮咖啡
```

两个任务互不影响，可以由两个厨师同时做。

### 必须串行

```text
先切菜
再下锅
```

第二步依赖第一步，必须按顺序。

另一个串行例子：

```text
两个操作同时修改同一个订单
```

即使它们没有数据依赖，也可能发生冲突，所以应该串行或使用事务/版本锁。

## 2. 当前全局配置

```python
agent = Agent(
    ...,
    tool_execution="parallel",
    max_parallel_tools=5,
)
```

含义：

```text
默认允许同一批工具并行
同一时刻最多真正执行 5 个 Attempt
```

如果设置：

```python
tool_execution="sequential"
```

该 Agent 每一批 Tool Call 都按模型顺序一个个执行。

## 3. 每个工具自己的配置

```python
AgentTool(
    ...,
    execution_mode="parallel",
)
```

或：

```python
AgentTool(
    ...,
    execution_mode="sequential",
)
```

当前：

```text
add       parallel
multiply  parallel
divide    parallel
```

它们都是纯计算，互不修改外部数据，因此适合并行。

`execution_mode=None` 表示继承 Agent 全局设置。全局为 parallel 时，None 工具也会并行。

## 4. 当前核心判断代码

`loop.py` 中：

```text
检查当前 Assistant Message 中全部 Tool Call
→ 查找对应 AgentTool
→ 是否存在 execution_mode=sequential
```

逻辑等价于：

```python
if agent.tool_execution == "sequential":
    execute_batch_sequentially()
elif any(tool.execution_mode == "sequential" for tool in current_batch):
    execute_batch_sequentially()
else:
    execute_batch_in_parallel()
```

## 5. 当前串行怎样执行

```text
Tool Call 1
→ 参数校验
→ Before Hook
→ Execute
→ After Hook
→ Tool Result

Tool Call 2
→ 参数校验
→ Execute
→ Tool Result
```

特点：

-严格按模型 Tool Call 顺序；
-前一个完成后才开始下一个；
-用户取消后停止后续调用；
-一个工具失败后通常仍会产生错误结果，再继续后续工具，除非 terminate/cancel。

## 6. 当前并行怎样执行

并行模式并不是无上限启动。

```text
顺序完成预检
→ 为合法工具创建异步 Task
→ Semaphore 控制实际进入 Execute 的数量
→ 工具并发执行
→ 完成事件按真实完成顺序
→ ToolResult 按模型原始顺序写回
```

例如：

```text
模型顺序：slow、fast
完成顺序：fast、slow
写回顺序：slow、fast
```

这样既保留真实 UI 进度，也保持模型 Context 稳定。

## 7. max_parallel_tools

```toml
max_parallel_tools = 5
```

模型一次返回 8 个并行 Tool Call：

```text
最多 5 个实际执行
某个结束后，下一个获取 Semaphore
最终 8 个全部完成
```

Retry Backoff 不占并发槽。每个 Retry Attempt 开始时重新申请槽位。

## 8. 复合计算为什么会自动分阶段

用户请求：

```text
（2+3）×（5+6）
```

第 1 Turn 模型只返回：

```text
add(2,3)
add(5,6)
```

两个 add 同批且都是 parallel，所以并行。

得到 5 和 11 后，第 2 Turn 模型才返回：

```text
multiply(5,11)
```

multiply 与 add 之间的依赖不是调度器自动计算出来的，而是模型通过不同 Turn 表达的。

也就是说：

```text
模型负责发现步骤依赖
Scheduler 负责执行当前这一批
```

## 9. 如果同一批有一个 sequential 工具

例如模型返回：

```text
read_order
cancel_order（sequential）
read_customer
```

当前实现会保守地执行：

```text
read_order
→ cancel_order
→ read_customer
```

整批全部串行。

这很安全，但可能损失一部分并行效率。

## 10. Pi 的做法

Pi：

```text
packages/agent/src/agent-loop.ts
```

使用与当前 Python 项目相同的保守规则：

```text
全局 toolExecution=sequential
或者批次中包含 executionMode=sequential
→ 整批串行

否则
→ 整批并行
```

当前 Python 实现基本继承了 Pi 这一语义。

## 11. DeepSeek Harness 的做法

DeepSeek Harness 更精细：

```text
packages/core/agent-loop/src/tool-calls.ts
```

它把工具分为：

```text
parallel pool
exclusive barrier
```

执行方式：

```text
并行工具进入有界 Rolling Pool
→ 遇到 Exclusive 工具
→ 等当前并行池排空
→ 单独执行 Exclusive
→ 再继续后续并行工具
```

例如：

```text
read A
read B
write C（exclusive）
read D
read E
```

可以执行为：

```text
read A + read B 并行
→ write C 独占
→ read D + read E 并行
```

比当前“出现一个 sequential 就整批串行”更高效。

DeepSeek Harness 还会在工具真正启动前重新读取 Execution Mode，因此运行时工具注册或策略变化可以形成新的 Barrier。

## 12. 当前功能完成度

已经完成：

-全局串行/并行；
-单工具 Sequential Override；
-并发上限；
-并行预检；
-完成顺序事件；
-模型顺序 ToolResult；
-独立 Timeout；
-取消隔离；
-单工具 Retry；
-Retry Backoff 释放并发槽；
-工具状态投影和持久 Dispatch Event。

尚未完成：

- DeepSeek 风格 Parallel/Exclusive Barrier 分组；
-按资源 ID 加锁；
-同订单/同文件冲突检测；
-读写锁；
-显式 Task 依赖图；
-业务事务批次；
-动态并发策略。

## 13. 当前风险

`execution_mode=None` 会继承全局 parallel。

因此新增写工具时，如果忘记配置：

```python
execution_mode="sequential"
```

它可能与其他工具并行执行。

所以真实写工具至少应该声明：

```python
execution_mode="sequential"
replay_policy="never"
```

同时通过 Approval、Idempotency 和 expected_version。

## 14. 后续推荐改进

建议把两种模式扩展为：

```text
parallel
exclusive
resource_locked
```

工具元数据：

```python
execution_policy=ToolExecutionPolicy(
    mode="resource_locked",
    operation="write",
    resource_keys=["order:{order_id}"],
)
```

调度规则：

```text
独立只读工具并行
Exclusive 形成 Barrier
同 Resource Key 串行
不同 Resource Key 可以并行
写工具需要 Approval
依赖任务按 depends_on 分层
```

## 15. 最终判断

当前串行和并行功能适合：

-教学计算；
-多个独立只读工具；
-保守处理 Sequential 工具；
-单进程 Agent。

如果接入真实写业务，建议下一步借鉴 DeepSeek Harness，开发：

```text
Parallel Pool
+
Exclusive Barrier
+
Resource Lock
```

> **当前系统不是靠 AI 猜并发安全，而是靠 Agent 全局配置和工具 execution_mode 决定。模型负责把有依赖的步骤放到不同 Turn，调度器只负责安全执行当前批次。**

---

## 对话 55：开发 Parallel、Exclusive 和 Resource-Locked 调度器

### 用户问题

继续开发，把工具执行策略扩展为：

```text
parallel
exclusive
resource_locked
```

### 助手回答

三种执行策略已经完成，并把原来“批次中出现一个串行工具就整批串行”的保守调度升级为 DeepSeek Harness 风格的 Parallel Pool + Exclusive Barrier，同时增加 Resource Lock。

## 1. AgentTool 执行策略

现在支持：

```python
execution_mode="parallel"
execution_mode="exclusive"
execution_mode="resource_locked"
```

旧值：

```python
execution_mode="sequential"
```

继续兼容，但调度时等价于 `exclusive`。新工具应使用新的三种模式。

全局：

```python
tool_execution="sequential"
```

仍然拥有最高优先级，会让整批工具按模型顺序串行执行。

## 2. Parallel Pool

```python
AgentTool(
    ...,
    execution_mode="parallel",
)
```

同一调度段中的 Parallel 工具进入共享 Semaphore：

```text
同时执行 Attempt 数量 <= max_parallel_tools
```

当前：

```text
add
multiply
divide
get_order_status Mock
```

均明确声明为 parallel。

## 3. Exclusive Barrier

```python
AgentTool(
    ...,
    execution_mode="exclusive",
)
```

模型顺序：

```text
parallel A
parallel B
exclusive C
parallel D
parallel E
```

实际执行：

```text
A + B 并行
→ 等 A/B 全部完成
→ C 单独执行
→ 等 C 完成
→ D + E 并行
```

Exclusive 不会与同批前后工具重叠。

如果 Exclusive 自身发生 Retry，它在整个逻辑 Tool Call 完成前继续保持 Barrier，后面的组不会越过它。

## 4. Resource Lock

```python
AgentTool(
    ...,
    execution_mode="resource_locked",
    resolve_resource_keys=lambda args: f"order:{args['order_id']}",
)
```

调度结果：

```text
order:1001 与 order:1001
→ 串行

order:1001 与 order:2002
→ 并行
```

适合：

-同一订单写入；
-同一文件修改；
-同一用户账户操作；
-同一部署环境更新。

## 5. Resource Key 安全

资源键从已经通过 `validate_args` 的参数生成。

支持：

```python
"order:1001"
```

或多个键：

```python
[
    "account:A",
    "account:B",
]
```

多个键会：

```text
去重
→ 排序
→ 按稳定顺序加锁
```

避免两个任务使用不同加锁顺序产生死锁。

Resource Key 不会进入普通调度事件，只记录 Key 数量，避免暴露敏感资源信息。

## 6. 配置严格校验

以下配置会在 AgentTool 创建阶段报错：

```text
resource_locked 但没有 resolve_resource_keys
parallel/exclusive 却提供 resolve_resource_keys
未知 execution_mode
```

`resolve_resource_keys` 运行后如果返回空值、非字符串或空字符串，也会生成：

```text
invalid_execution_policy
```

错误 ToolResult，不执行工具。

## 7. Retry 与锁

每一个真实 Attempt：

```text
申请 Resource Lock
→ 申请 Semaphore
→ tool_execution_dispatch_start
→ 执行工具函数
→ 释放 Semaphore
→ 释放 Resource Lock
```

失败后：

```text
释放锁和槽位
→ Retry Backoff
→ 下一 Attempt 重新获取
```

因此：

- Backoff 不占并发槽；
- Backoff 不长期锁住资源；
-同资源的其他任务可以在 Backoff 期间执行；
-下一 Attempt 仍会重新参与资源冲突检查。

## 8. 新调度事件

新增：

```text
tool_execution_queued
```

字段：

```text
Tool Call ID
Tool Name
executionMode
resourceKeyCount
```

已有：

```text
tool_execution_dispatch_start
```

现在完整区分：

```text
Tool Intent
→ Queued
→ Dispatch Attempt
→ Update
→ Result
```

RuntimeStateTracker 会先把 Tool 标记为 queued，在 Dispatch Event 后再改成 executing。

DurableOperationRecorder 继续记录真正进入工具函数的 Dispatch Attempt。

## 9. 调度实现

新的 `_execute_tools_scheduled()`：

```text
顺序 Preflight
→ 解析每个工具执行策略
→ 构建 Parallel/Resource 分段
→ Exclusive 形成 Barrier
→ 每段使用有界并行池
→ Resource Key Lock
→ Tool End 按真实完成顺序
→ ToolResult 按模型原始顺序
```

未知工具、参数错误、Hook 阻止和无效 Resource Policy 在 Preflight 阶段转为正式 ToolResult。

## 10. 与 Pi 的变化

Pi 使用：

```text
批次中存在 sequential
→ 整批串行
```

原 Python 项目此前保持该语义。

当前已经升级为：

```text
Parallel Group
→ Exclusive Barrier
→ Parallel Group
```

这部分更接近 DeepSeek Harness 的 `tool-calls.ts` 调度方式。

## 11. Resource Lock 是新增能力

DeepSeek Harness 参考实现主要提供 Parallel Pool 和 Exclusive Barrier。

当前 Python 项目在此基础上额外增加：

```text
resource_locked
```

用于同业务实体冲突隔离。

Resource Lock 只是进程内调度保护；多进程/多 Worker 仍需数据库事务、Lease 或分布式锁。

## 12. 全局 Sequential

```python
Agent(
    ...,
    tool_execution="sequential",
)
```

会忽略每个工具的 Parallel/Resource 策略，全部按模型顺序执行。

这适合调试、严格审计或临时关闭并发。

## 13. 新增离线示例

```text
examples/tool_scheduling_usage.py
```

运行：

```bat
python examples\tool_scheduling_usage.py
```

实际时间线：

```text
read-a/read-b 同时开始
→ read-a/read-b 结束
→ exclusive 单独执行
→ exclusive 结束
→ order:1001 与 order:2002 同时执行
→ 第二个 order:1001 等待第一个结束
```

## 14. 真实模型回归

用户自然语言：

```text
帮我计算（10+2）÷（1+2）
```

真实模型和新调度器执行：

```text
add/add → parallel
→ 得到 12、3
→ divide → parallel（下一 Turn）
→ 最终结果 4
```

终端新增显示：

```text
工具 add 已进入调度队列，策略为 parallel
工具 add 第 1 次实际进入执行函数
工具 divide 已进入调度队列，策略为 parallel
```

最终：

```text
运行状态：已完成
Runtime Run ID：...
Durable Operation ID：...
```

## 15. 新增测试

```text
tests/test_tool_scheduling.py
```

共 6 项：

- Exclusive 在前后 Parallel Pool 间形成 Barrier；
-同 Resource Key 串行；
-不同 Resource Key 并行；
-Retry Backoff 释放 Resource Lock；
-全局 Sequential 覆盖 Parallel；
-Sequential 旧值兼容；
-Resource Resolver 必填和模式配置校验。

当前全部测试：

```text
Ran 125 tests
OK
```

## 16. 文档规范更新

`TOOLS_IMPLEMENTATION_GUIDE.md` 已要求每个工具选择：

```text
parallel
exclusive
resource_locked
```

`BUSINESS_REQUIREMENTS.md` 的 Session/Recovery 表已增加：

```text
execution_mode
Resource Key
```

`AGENTS.md` 要求 AI 不得让模型猜并发安全，写工具必须显式声明策略。

## 17. 当前边界

- Resource Lock 是当前 Python 进程内锁；
-多 Worker 必须使用数据库事务、Lease 或分布式锁；
-调度器不自动推导跨 Turn 依赖，仍由模型或未来 PlanExecutor 表达；
-业务写工具仍需 Approval、Idempotency、expected_version 和 outcome_unknown 核对；
-尚未实现读写锁的共享读/独占写模式。

> **当前工具调度已经从“全批并行或全批串行”升级为“有界 Parallel Pool + Exclusive Barrier + 按业务资源加锁”，同时保持 ToolResult 模型顺序、Retry 释放锁和 Durable Dispatch 语义。**

---

## 对话 56：当前项目是否算合格的 Agent Loop

### 用户问题

目前这个项目是否可以算一个合格的 Agent Loop？

### 助手回答

## 结论

可以分三种标准回答：

```text
作为教学和源码研究用 Agent Loop
→ 合格，而且功能已经明显超过最小 Agent Loop

作为单进程、受控环境的可嵌入 Agent Runtime
→ 基本合格

作为可以直接部署到生产的完整 Agent 平台
→ 目前还不合格
```

最准确的一句话是：

> **它已经是一个合格的 Pi 风格低层 Agent Loop 和较完整的单进程教学 Harness，但还不能宣传为 Production-ready Agent Platform。**

## 1. 一个最小合格 Agent Loop 需要什么

最核心循环：

```text
接收用户消息
→ 请求模型
→ 模型直接回答：结束
→ 模型返回 Tool Call：执行工具
→ Tool Result 写回 Context
→ 再请求模型
→ 直到最终回答
```

当前项目已经完整实现这条链路，并通过真实模型验证。

## 2. 当前核心 Agent Loop 已完成

-真实 OpenAI-compatible Provider；
-SSE 流式事件；
-指定模型和 Bearer；
-User/Assistant/ToolResult Message；
-多 Turn；
- Tool Call 参数分片；
-Tool Result 回灌；
-模型直接回答；
-错误和取消终止；
-Steering；
-Follow-up；
-生命周期事件。

所以从 Agent Loop 定义看，已经合格。

## 3. 工具执行能力已经较完整

当前支持：

```text
ToolRegistry
JSON Schema
Python 运行时校验
Before/After Hook
独立 Timeout
CancellationToken
父子取消
工具 Update
结构化 Result
Unknown Tool
Budget
```

调度支持：

```text
Parallel Pool
Exclusive Barrier
Resource Lock
max_parallel_tools
模型顺序 ToolResult
完成顺序 UI Event
```

重试支持：

```text
模型 Retry
幂等单工具 Retry
Task Retry
Backoff/Jitter/Retry-After
Circuit Breaker
```

这已经超过普通最小 Agent Loop。

## 4. 可靠性骨架已经存在

- max_turns；
-max_tool_calls；
-max_parallel_tools；
-整批预算拒绝；
-Durable Retry Journal；
-Runtime Event；
-Reducer；
-Invariant；
-Projection；
-JSONL Replay；
-Process Interrupted；
-完整 Operation Context；
-Recovery Planner/Executor；
-replay_policy；
-outcome_unknown；
-Context Compaction Retry。

## 5. 产品安全骨架已经存在

```text
business.toml
HybridModelRouter
CapabilityRegistry
RequiredToolCallGuard
VerifiedIdentity
ApprovalService
WriteOperationService
Idempotency Key Hash
DomainStateMachine
```

所以它不再只是一个几十行的演示循环，而是开始具备 Harness/Runtime 架构。

## 6. 测试和真实验证

当前：

```text
Ran 125 tests
OK
```

已经真实验证：

-第三方模型文本回答；
-单 Tool Call；
-并行 Tool Call；
-跨 Turn 工具依赖；
-除法工具；
-Hybrid Intent Router；
-Required Tool；
-Durable Operation ID；
-Runtime State；
-Parallel/Exclusive/Resource Lock 调度。

因此不能说它只是“纸上设计”。

## 7. 为什么还不能叫生产级平台

### 并行异常清理仍有加固缺口

如果一个并行 `run_one` 因 Listener/调度异常被外层取消，内部单独创建的 `execute_task` 仍可能没有被最外层明确取消并等待。

Update Listener 异常也可能在 `detach()` 前中断清理。

正常、Timeout、用户取消已经覆盖，但专门的异常清理测试仍需补齐。

这是当前最需要先修复的低层可靠性问题。

### 持久化仍是单进程 JSONL

```text
runtime-events.jsonl
operation-events.jsonl
retry-events.jsonl
```

适合教学，不适合多个 Worker 同时写。

还缺：

-数据库事务；
-Schema Version；
-Snapshot；
-Migration；
-加密；
-访问控制；
-数据保留策略。

### Recovery 仍依赖 Host Callback

完整 Context 和恢复计划已经存在，但：

```text
request_model
execute_tool
reconcile_tool
```

仍由 Host 注入。

还缺统一 `RecoverableModelRuntime/ToolRuntime`，确保恢复过程不会绕过 Guard、Timeout、权限和 Approval。

### 真实身份尚未接入

`StaticIdentityVerifier` 只是测试实现。

生产需要：

```text
OAuth/OIDC/JWT/IAM/SSO
```

### Approval 没有产品界面

状态机已完成，但没有：

- Web/TUI Approval UI；
-通知；
-批准后自动恢复入口；
-过期任务调度器。

### Compaction 是教学实现

`SlidingWindowCompactor` 只是保留最近消息，不是真正 Token-aware Summary，也没有完整处理复杂 Tool Pair 压缩。

### 业务工具仍是 Mock/教学

真实订单、退款、支付 API 尚未提供。

当前不能宣称完成真实业务操作。

### 多 Intent PlanExecutor 未实现

当前支持模型跨 Turn 隐式动态规划，但没有：

-显式 Task List；
-Dependency Graph；
-整批预检；
-统一 Approval Barrier；
-任务级持久恢复。

### Backpressure 和可观测性不足

EventStream 仍是无界队列，还缺：

-有界队列；
-慢消费者策略；
-Metrics；
-Trace；
-结构化生产日志；
-告警。

## 8. 当前分级

| 使用目标 | 评价 |
|---|---|
| 学习 Agent Loop 原理 | 完全合格 |
| 研究 Pi/DeepSeek Harness | 完全合格 |
| 单进程真实模型与只读工具 | 基本合格 |
| 内部受控业务原型 | 基本合格，但需安全审查 |
| 真实退款/支付/高风险写操作 | 暂不合格 |
| 多 Worker 生产平台 | 不合格 |

## 9. Agent Loop 和完整 Agent 平台不要混淆

Agent Loop 只需要负责：

```text
模型
消息
工具
多 Turn
停止条件
```

OAuth、数据库、Approval UI、分布式 Worker 和长期 Session 属于 Agent Platform/Harness。

当前项目的 Loop 已经合格；不足主要集中在生产 Harness 和产品层。

## 10. 推荐继续顺序

### P0

```text
并行异常清理加固
Update Listener 失败下保证 detach
专门 test_parallel_cleanup.py
```

### P1

```text
DurableAgentHost
RecoverableModelRuntime/ToolRuntime
Approval Resume
```

### P2

```text
SQLite Transaction Store
Schema Version/Snapshot/Migration
Token-aware Compaction
```

### P3

```text
Plan/Task StateMachine
Backpressure
Telemetry
Distributed Worker
```

## 最终评价

> **如果问题是“它是不是一个真正能跑模型、调用工具、读取结果并继续思考的 Agent Loop”，答案是肯定的。**

> **如果问题是“它能不能不做改造就承载支付、退款和多 Worker 生产流量”，答案是否定的。**

当前最准确定位：

```text
合格的低层 Agent Loop
+
功能丰富的单进程教学 Harness
+
尚在建设中的生产 Agent Runtime
```

---

## 对话 57：通俗解释后续开发优先级 P0/P1/P2/P3

### 用户问题

将推荐开发顺序中的 P0、P1、P2、P3 用通俗易懂的语言解释。

### 助手回答

## 先理解 P0/P1/P2/P3

它们表示优先级：

```text
P0：必须最先处理的安全问题
P1：让零散模块真正组合成可用产品
P2：让数据长期可靠、可升级
P3：增加复杂任务和大规模运行能力
```

可以把项目比作开饭店：

```text
P0：先确保煤气不会泄漏、员工离开后炉火会关闭
P1：安排一个店长统一管理点单、厨房和审批
P2：建立可靠账本、仓库记录和历史档案
P3：开连锁店、增加监控系统和复杂套餐调度
```

## 1. P0：先把可能遗留的后台任务彻底清理

当前多个工具并行时：

```text
Tool A
Tool B
Tool C
```

如果某个 Listener 或调度代码突然异常，外层会取消其他任务。

但内部还有单独创建的：

```text
execute_task
update_task
timeout_task
```

某些特殊异常路径下，内部 Task 可能没有被明确取消和等待。

通俗理解：

```text
饭店已经关门
但厨房里可能还有一个炉子没有关闭
```

可能造成：

-工具在 Agent 结束后继续执行；
-后台 Task 泄漏；
-迟到 Update；
-文件或数据库连接没有释放；
-Python 输出 Task exception was never retrieved；
-写操作在用户认为取消后仍继续。

### P0 要完成什么

```text
外层任务被取消
→ 内部 execute_task 必须取消
→ 等待 execute_task 真正结束
→ 清理 timeout/cancellation/update task
→ 无论 Listener 是否异常都 detach 子令牌
```

增加：

```text
tests/test_parallel_cleanup.py
```

专门模拟：

-一个 Listener 抛异常；
-兄弟工具被取消；
-内部 Execute Task 不残留；
-Update Listener 异常；
-detach 一定执行；
-主要异常不被清理异常覆盖。

### P0 完成后的效果

```text
一次 Agent Run 结束
→ 所有属于它的异步 Task 都真正结束
```

这是最基础的安全保障，所以优先级最高。

## 2. P1：增加一个统一“店长”管理现有模块

当前已经有很多零件：

```text
Agent
Provider
Tool Runtime
RuntimeStateTracker
DurableOperationRecorder
Recovery Planner
ApprovalService
WriteOperationService
```

但不少代码需要示例手工组装。

通俗理解：

```text
厨房、收银、仓库和审批人员都有了
但还没有店长统一协调
```

### DurableAgentHost

它相当于店长，负责：

```text
创建 Agent
加载 Provider
注册工具
创建 Session
连接 Runtime/Operation Recorder
启动时扫描未完成任务
关闭前 Flush
```

用户不需要每个项目重复手工拼装。

### RecoverableModelRuntime

恢复模型请求时，不能直接随便调用 HTTP。

应该继续经过：

-Provider Retry；
-Circuit Breaker；
-Context Compaction；
-模型配置和鉴权；
-Usage 记录；
-持久事件。

通俗理解：

```text
恢复订单仍然必须走正式收银台
不能从后门直接拿钱
```

### RecoverableToolRuntime

恢复工具时，也必须继续经过：

-参数校验；
-Before/After Hook；
-权限；
-Approval；
-Timeout；
-Retry；
-Resource Lock；
-RequiredToolCallGuard；
-Durable Dispatch Event。

不能因为“这是恢复流程”就直接调用 Python 函数。

### Approval Resume

当前可以创建和批准 Approval，但还缺完整产品流程：

```text
用户请求取消订单
→ 返回等待审批
→ 审批人在 UI/API 批准
→ 系统找到原 Operation
→ 消费 Approval
→ 恢复写操作
→ 继续模型生成最终答复
```

通俗理解：

```text
现在已经有审批单
但还需要有人把批准后的审批单送回原来的工作台继续处理
```

### P1 完成后的效果

```text
项目启动
→ 自动装配全部模块
→ 发现未完成任务
→ 使用正式安全管线恢复
→ 等待审批的任务可以在批准后继续
```

完成 P1 后，项目才更像一个可直接使用的 Agent 产品骨架。

## 3. P2：把教学账本升级成生产账本

当前主要使用 JSONL：

```text
runtime-events.jsonl
operation-events.jsonl
retry-events.jsonl
```

它适合教学和单进程，但不适合多个 Worker、长期数据和复杂升级。

通俗理解：

```text
现在用的是一本纸质流水账
一个人记账没问题
多人同时记就容易冲突
```

### SQLite Transaction Store

升级为 SQLite 后可以：

```text
在一个事务中：
写 Operation Event
消费 Approval
占用 Idempotency Key
更新版本号
提交状态
```

要么全部成功，要么全部失败。

避免：

```text
Approval 已消费
但写操作事件没有保存
```

### Schema Version 和 Migration

代码会不断升级，旧事件结构可能和新代码不同。

需要：

```text
Event Schema Version
Snapshot Version
Migration
旧版本兼容测试
```

通俗理解：

```text
以前的账本格式和新账本格式不同
需要转换规则，不能直接扔掉旧数据
```

### Snapshot

如果有几十万条 Event，每次启动都从第一条重放会很慢。

Snapshot 类似存档点：

```text
读取最近 Snapshot
→ 只重放之后的新 Event
```

### Token-aware Compaction

当前只保留最近 N 条消息，比较粗糙。

真正的压缩应：

```text
计算 Token
→ 保留关键 User/Assistant/Tool 配对
→ 总结早期内容
→ 验证摘要
→ 持久化摘要
```

通俗理解：

```text
不是简单撕掉旧账本
而是把旧账整理成准确摘要后归档
```

### P2 完成后的效果

```text
数据可靠
程序升级能读旧数据
大量历史启动仍然快
多个状态可以事务提交
```

## 4. P3：处理复杂任务和大规模运行

P3 不是基础安全修复，而是高级能力。

### Plan/Task StateMachine

用户可能说：

```text
查询订单 1001
取消订单 1002
把结果发送给客服
```

需要拆成：

```text
Task 1：查询订单
Task 2：取消订单，需要审批
Task 3：发送结果，依赖前两个
```

Plan/Task 状态机负责：

-任务列表；
-依赖图；
-并行任务；
-Approval Barrier；
-失败传播；
-任务级恢复；
-最终汇总。

通俗理解：

```text
从“处理一张订单”升级成“管理一个包含多个步骤的项目”
```

### Backpressure

如果工具一秒产生一万条 Update，而 UI 处理不过来，内存会不断增长。

Backpressure 负责：

```text
限制队列大小
合并重复进度
降低生产速度
丢弃不重要 Update
保护关键终止事件
```

通俗理解：

```text
入口一次涌入一万人
需要排队和限流，不能全部挤进大厅
```

### Telemetry

Telemetry 是运行监控：

```text
一次模型请求多久
哪个工具最慢
Retry 多少次
Token 和费用多少
失败率多少
Circuit 是否打开
```

需要：

- Metrics；
-Trace；
-结构化日志；
-告警；
-Dashboard。

通俗理解：

```text
给系统安装仪表盘、监控摄像头和报警器
```

### Distributed Worker

一个进程处理能力有限，未来可能多个 Worker：

```text
Worker A 处理订单查询
Worker B 处理退款
Worker C 处理通知
```

需要：

-任务队列；
-Lease；
-心跳；
-重复任务去重；
-Worker 故障接管；
-分布式 Resource Lock；
-At-least-once 处理。

通俗理解：

```text
从一家店升级成多家分店，需要总部统一派单和防止重复处理
```

### P3 完成后的效果

```text
可以管理复杂多任务
高流量时不会把内存撑爆
可以监控性能和费用
可以扩展到多个 Worker
```

## 5. 四个阶段的关系

```text
P0：确保不会留下危险后台任务
P1：让现有模块自动组装并安全恢复
P2：让数据可靠、可升级、可长期运行
P3：支持复杂任务、高流量和分布式扩展
```

## 6. 是否都必须开发

### 当前学习和单进程测试

```text
P0 建议完成
P1/P2/P3 可以等待
```

### 内部只读业务原型

```text
P0 必须
P1 建议
P2 视数据重要性
P3 可等待
```

### 真实退款、支付、订单取消

```text
P0 必须
P1 必须
P2 基本必须
P3 根据复杂度
```

### 多 Worker 生产平台

```text
P0/P1/P2/P3 都需要
```

## 最通俗总结

```text
P0：关好炉火，不能留下后台危险任务
P1：增加店长，把现有模块真正串起来
P2：把纸质账本升级成可靠数据库和档案系统
P3：建设项目管理、限流、监控和多 Worker 平台
```

> **开发顺序不能反过来。应先解决 P0 安全清理，再做 P1 产品编排，然后做 P2 数据可靠性，最后才是 P3 高级规划和大规模运行。**

---

## 对话 58：完成 P0 并行异常清理加固

### 用户问题

完成 P0 的开发。

### 助手回答

P0 并行异常清理已经完成，并通过专门的故障注入测试。

## 1. 修复的核心问题

旧实现中：

```text
Scheduler run_one 被外层取消
```

不会自动级联取消内部单独创建的：

```text
execute_task
```

因此可能出现：

```text
Agent 已经结束
→ 工具协程仍在后台运行
```

现在 `_execute_prepared_tool()` 最外层会显式取消并等待嵌套 Execute Task。

## 2. 当前完整清理顺序

无论工具正常、超时、用户取消、Listener 异常还是 Scheduler 异常，都会执行：

```text
accepting_updates=False
→ 取消工具子 CancellationToken
→ execute_task.cancel()
→ await execute_task
→ 取消并等待 timeout_task
→ 取消并等待 cancellation_wait_task
→ 等待全部 update_task
→ 收集 Update Listener 错误
→ finally 中强制 tool_cancellation.detach()
```

只有全部清理完成后，Agent Run 才能结算。

## 3. 外层异常会清理内部 Execute Task

例如：

```text
fast 工具完成
→ tool_execution_end Listener 抛异常
→ Scheduler 取消 slow 的 run_one
→ slow 内部 execute_task 被显式取消
→ slow 收到 CancelledError
→ 等待 slow 清理完成
```

不会再遗留后台 slow 工具。

## 4. Update Listener 异常仍保证 Detach

旧实现：

```text
await update_tasks
→ Listener 抛异常
→ 可能跳过 detach
```

新实现：

```text
await gather(update_tasks, return_exceptions=True)
→ 记录错误
→ finally detach
→ 清理完成后再传播 Listener 错误
```

因此父 CancellationToken 不会长期保存已经结束的工具子令牌。

## 5. 主要异常不会被清理异常覆盖

示例：

```text
主要错误：tool_execution_end Listener 失败
清理错误：slow 工具在取消收尾时又抛异常
```

最终 Agent Error 保留：

```text
primary-listener-error
```

而不是：

```text
secondary-cleanup-error
```

规则：

```text
有主要错误
→ 清理错误只负责结算，不覆盖主要错误

没有主要错误
→ Update Listener 错误在清理后正常向上传播
```

## 6. 内部 Task 统一命名

新增 Task 名称：

```text
pi-tool-run:<tool>:<callId>
pi-tool-execute:<tool>:<callId>
pi-tool-update:<tool>:<index>
pi-tool-timeout:<tool>:<callId>
pi-tool-cancel-wait:<tool>:<callId>
```

这样可以使用：

```python
asyncio.all_tasks()
```

检查 Agent 结束后是否仍有工具 Task。

## 7. CancellationToken 诊断

新增：

```python
CancellationToken.child_count
```

用于检查当前父令牌仍挂接多少子工具令牌。

正常结算后：

```text
child_count == 0
```

## 8. 新增专门测试

```text
tests/test_parallel_cleanup.py
```

共 4 项：

### Tool End Listener 异常

```text
fast Listener 失败
→ slow 内部 Execute 被取消
→ 无后台 Task
```

### Update Listener 异常

```text
Update Listener 失败
→ 子令牌仍 Detach
→ child_count=0
```

### 主要异常优先

```text
主要 Listener Error
+ 次要工具清理 Error
→ 最终保留主要 Error
```

### 用户取消

```text
Agent.abort()
→ Execute/Timer/Waiter/Update 全部结束
→ asyncio.all_tasks 中无 pi-tool-* 残留
```

## 9. 测试结果

```text
python -m unittest tests.test_parallel_cleanup -v
```

结果：

```text
Ran 4 tests
OK
```

全部项目：

```text
Ran 129 tests
OK
```

同时通过：

- `python -m compileall`；
- `git diff --check`；
-无 `Task exception was never retrieved`；
-无未处理的 asyncio Shutdown Exception。

## 10. 文档和规则更新

已更新：

```text
README.md
TOOLS_IMPLEMENTATION_GUIDE.md
AGENTS.md
BUILTIN_TOOLS_DESIGN.md
```

AI 以后修改并行调度时，必须验证：

-外层取消清理嵌套任务；
-Update Listener 失败仍 Detach；
-清理错误不覆盖主要错误；
-没有后台 Task 残留。

## 11. P0 当前边界

asyncio 只能可靠取消会响应 Cancellation 的异步协程。

以下代码仍无法由 Task Cancel 强制杀死：

-完全阻塞事件循环；
-同步死循环；
-故意吞掉 CancelledError；
-卡死的原生库调用。

这类工具必须放入：

```text
子进程
Worker Process
进程组
```

并使用进程级终止。

> **P0 已完成：一次 Agent Run 无论正常、超时、取消或 Listener 异常结束，所有合作式异步 Tool Task、Timer、Waiter、Update 和子 CancellationToken 都会完成清理后再结算。**

---

## 对话 59：完成 P1 DurableAgentHost、Recovery Runtime 和 Approval Resume

### 用户问题

完成 P1 的开发。

### 助手回答

P1 已完成：现有 Agent、Provider、Tool、Runtime State、Durable Operation、Recovery、Approval 和 WriteOperation 已经通过统一 DurableAgentHost 自动装配。

## 1. 新增 Harness 目录

```text
src/pi_agent_loop/harness/
├─ __init__.py
├─ durable_agent_host.py
├─ model_runtime_adapter.py
├─ tool_runtime_adapter.py
├─ approval_gateway.py
└─ startup_recovery.py
```

## 2. DurableAgentHost

统一入口：

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

自动创建和连接：

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

普通 Agent 和 RoutedAgent 两种模式都支持。

## 3. 启动恢复

```python
auto_recover=True
```

是默认值。

Host 创建时自动扫描 Operation Store：

```text
已完成 Operation
→ 跳过

未完成 Operation
→ DurableSessionRecovery
→ Model/Tool/Reconcile Callback
→ completed/manual_intervention/failed
```

结果保存在：

```python
host.startup_recovery_report
```

也可以手动：

```python
await host.recover_on_startup()
```

## 4. RecoverableModelRuntime

新增：

```text
harness/model_runtime_adapter.py
```

恢复模型请求不再由用户随意直接请求 HTTP，而是继续使用现有 StreamFn：

```python
message = await host.model_runtime.request(messages)
```

因此继续经过：

- OpenAI-compatible Provider；
-模型 Retry；
-Circuit Breaker；
-Context Compaction；
-CancellationToken；
- Tool Schema；
- Retry Event Store。

错误/Aborted Assistant 不会被当作成功恢复结果。

## 5. RecoverableToolRuntime

新增：

```text
harness/tool_runtime_adapter.py
```

恢复 Tool Action 时复用当前 Agent Tool 管线：

```text
AgentTool 查找
→ validate_args
→ Before Hook
→ Parallel/Exclusive/Resource 调度
→ Timeout
→ Tool Retry
→ Cancellation
→ After Hook
→ 标准 ToolResult
```

使用：

```python
result = await host.tool_runtime.execute(recovery_action)
```

安全规则：

```text
replay_safe_tool
→ 必须匹配 replay_policy=safe

replay_policy=never
→ 默认拒绝
→ 必须由 authorize_never_replay 明确授权
```

真实写操作优先走 WriteOperationService，不通过普通 Safe Replay。

外部 CancellationToken 通过桥接 Task 实时向内部恢复 Agent 传播取消。

## 6. StartupRecoveryCoordinator

新增：

```text
harness/startup_recovery.py
```

它扫描所有 Session/Operation：

```text
重放 Operation State
→ 跳过终态
→ 对非终态执行 Recovery Planner/Executor
→ 汇总报告
```

报告：

```python
StartupRecoveryReport(
    completed=(...),
    manual_intervention=(...),
    failed=(...),
)
```

损坏或无法安全恢复的 Operation 不会猜测成功。

## 7. ApprovalResumeCoordinator

新增：

```text
harness/approval_gateway.py
```

Approval Request 会额外持久化：

```text
approval_resume_registered
```

内容包括：

- approvalId；
-精确 Action；
-恢复 Payload；
-所属 Session/Operation。

批准后：

```text
验证 Approver Role
→ 禁止默认自审
→ 消费 Approval
→ approval_resume_started
→ 调用 Resume Callback
→ approval_resume_completed/failed
```

如果 Approval 已消费、Resume Started 后进程崩溃：

```python
await coordinator.recover_incomplete(resume_callback)
```

可以读取持久 Payload 继续恢复，不会再次消费 Approval。

## 8. RoutedAgent Approval 挂起

`RoutedAgent` 新增：

```python
suspend_on_approval=True
```

DurableAgentHost 自动启用。

当路由得到：

```text
in_scope_approval_required
```

不会再把 Operation 当普通 Completed 结束，而是：

```text
Runtime → waiting_approval
Durable Operation 保持活动
持久化 approval_pending
等待 Approval Resume
```

Capability Missing、Out of Scope 等其他零模型结果仍正常完成 Operation。

## 9. Host Prompt Approval

```python
pending = await host.prompt(
    "取消订单 1001",
    requester=verified_operator,
)
```

Host 会：

```text
Hybrid Router 得到 Approval Required
→ 持久化 User Message
→ 持久化 Host 确认的 Assistant Tool Call
→ 创建 Action Hash Approval
→ 返回 approval_id 和 operation_id
```

没有 VerifiedIdentity 时：

```text
Runtime/Operation 安全结束为 Failed
→ 不留下悬空 waiting_approval
→ 抛 PermissionError
```

## 10. 批准后恢复写操作

```python
final = await host.approve_and_resume(
    pending.approval_id,
    approver=verified_approver,
    consumer=verified_operator,
    idempotency_key="cancel-order-1001",
    write_handler=cancel_order_handler,
)
```

完整流程：

```text
Approval Grant
→ Action Hash 校验
→ Approval Consume 一次
→ Runtime approval_granted
→ WriteOperation Prepare
→ Idempotency Key 去重
→ Tool Intent/Dispatch 持久化
→ Write Handler
→ Write Succeeded/OutcomeUnknown
→ ToolResult 写入完整 Context
→ RecoverableModelRuntime 请求最终回答
→ approval_resume_completed
→ Operation Finished
→ Run Completed
```

写 Handler 失败时会持久化 Failed，Runtime/Operation 安全终止，不留下活动指针。

## 11. DurableHostPromptResult

Host Prompt 返回：

```python
DurableHostPromptResult(
    result=...,          # 普通或 RoutedPromptResult
    operation_id="...",
    approval_id="...",  # 不需要审批时为 None
)
```

## 12. P1 离线演示

新增：

```text
examples/durable_host_usage.py
```

运行：

```bat
python examples\durable_host_usage.py
```

输出：

```text
普通 Host
→ 自动装配
→ 回答成功
→ Runtime completed
→ Operation ID
→ Startup Recovery Report

Approval Host
→ waiting_approval
→ 返回 Approval ID
→ Approver 批准
→ Write Handler 执行 1 次
→ 模型最终回答
→ Runtime completed
```

演示每次运行前清理自己的 Demo State，保证输出确定。

## 13. 新增测试

```text
tests/test_durable_agent_host.py
```

共 9 项：

- RecoverableModelRuntime 使用正式 StreamFn；
-RecoverableToolRuntime 复用校验/Timeout/Retry；
-Never Tool 未授权拒绝；
-Startup Recovery 完成未结束 Operation；
-Approval Resume Payload；
-已消费 Approval 的崩溃恢复；
-DurableAgentHost 自动装配；
-缺少可信申请人安全失败；
-Approval 后幂等写并继续模型。

全部项目：

```text
Ran 138 tests
OK
```

## 14. 顶层公开 API

新增：

```python
from pi_agent_loop import (
    DurableAgentHost,
    DurableHostPromptResult,
    RecoverableModelRuntime,
    RecoverableToolRuntime,
    ApprovalResumeCoordinator,
    StartupRecoveryCoordinator,
    StartupRecoveryReport,
)
```

## 15. 当前准确边界

P1 通用编排骨架已经完成，但生产接入仍需：

-真实 OAuth/OIDC/IAM IdentityVerifier；
-真实 Approval API/TUI/Web；
-真实 Write Handler 和 Reconciliation；
-P2 事务型 SQLite/PostgreSQL Store；
-多 Worker Lease；
-P3 多 Intent Plan/Task。

`RecoverableToolRuntime` 使用现有 Agent 管线执行安全恢复，但 Never 写工具仍优先走 WriteOperationService 和 Approval Resume。

> **P1 已完成：现有零散模块已经由 DurableAgentHost 自动装配，启动时可以扫描恢复 Operation，模型和工具恢复继续走正式管线，Approval 批准后可以执行幂等写操作并继续原模型 Context。**

---

## 对话 60：当前是否已有 Harness，以及 Agent Loop 与 Harness 的区别

### 用户问题

询问当前项目是否真的已经有 Harness 框架，并要求详细解释 Agent Loop 和 Harness 的区别。

### 助手回答

## 结论

当前项目确实已经有 Harness，但必须准确命名：

```text
已经有：单进程、可扩展、可恢复的 Harness 骨架
还没有：多 Worker、事务数据库、完整 UI 的生产级 Harness 平台
```

所以“已经有 Harness 框架”是真的；“已经达到 DeepSeek Harness 全部生产能力”不是真的。

## 1. 先区分四个概念

### Model

大模型是大脑：

```text
理解问题
推理
决定是否调用工具
组织最终回答
```

### Agent Loop

Agent Loop 是反复执行的思考—行动循环：

```text
请求模型
→ 得到回答或 Tool Call
→ 执行工具
→ Tool Result 写回模型
→ 再请求模型
→ 直到结束
```

### Agent

Agent 是一个具体运行实例：

```text
模型配置
System Prompt
Message History
工具列表
队列
CancellationToken
当前状态
```

### Harness

Harness 是围绕 Agent/Loop 的运行外壳和管理系统：

```text
创建和装配 Agent
Provider/Tool Runtime
Session 持久化
崩溃恢复
身份和权限
Approval
幂等写操作
Retry/Circuit
状态机
配置
监控和 UI 接口
```

## 2. 用汽车理解

```text
Model = 驾驶员的大脑
Agent Loop = 发动机反复做功
Agent = 一辆正在运行的具体汽车
Harness = 底盘、安全带、仪表盘、油路、刹车、维修和车队管理系统
```

发动机可以转，不代表整辆车已经适合上高速。

同样，Agent Loop 能调用模型和工具，不代表已经拥有恢复、审批、持久化和安全运行环境。

## 3. Agent Loop 只负责什么

当前：

```text
src/pi_agent_loop/loop.py
```

主要负责：

-组装模型 Context；
-请求 Assistant；
-消费流式响应；
-识别 Tool Call；
-执行工具；
-写回 Tool Result；
-多 Turn；
-Steering/Follow-up；
-停止条件；
-生命周期事件。

最核心伪代码：

```python
while True:
    assistant = await model(messages, tools)

    if assistant 没有 Tool Call:
        return assistant

    results = await execute_tools(assistant.tool_calls)
    messages.extend(results)
```

## 4. Agent Loop 不应该负责什么

不应该把以下全部塞入 `loop.py`：

-数据库；
- OAuth/JWT；
-Approval UI；
-完整 Session；
-崩溃恢复；
-业务订单状态；
-多 Worker；
-Web/TUI；
-长期日志；
-Snapshot/Migration。

否则 Loop 会变成无法测试和复用的巨型模块。

## 5. Harness 负责什么

Harness 回答的是：

```text
Agent 怎样被创建？
使用哪个模型？
可以看到哪些工具？
消息保存在哪里？
程序崩溃后如何恢复？
写操作是否需要审批？
谁有权限？
如何防止重复支付？
如何关闭并清理所有任务？
```

Harness 不负责大模型推理本身，而是负责让推理和工具执行安全、可靠、可恢复。

## 6. 当前项目的 Harness 入口

```text
src/pi_agent_loop/harness/durable_agent_host.py
```

公开入口：

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

使用：

```python
result = await host.prompt("用户问题")
```

这已经属于 Harness，而不是单纯 Agent Loop。

## 7. DurableAgentHost 自动装配什么

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

如果不使用 Harness，应用需要手工创建和连接这些对象。

## 8. 当前 Harness 目录

```text
src/pi_agent_loop/harness/
├─ durable_agent_host.py
├─ model_runtime_adapter.py
├─ tool_runtime_adapter.py
├─ approval_gateway.py
└─ startup_recovery.py
```

周边支撑目录：

```text
providers/   模型协议
 tools/       外部能力
 routing/     Intent/Capability
 runtime/     状态 Reducer/Projection
 session/     Context、Event、Replay、Recovery
 retry/       Retry/Circuit/Compaction
 security/    VerifiedIdentity
 approval/    Approval StateMachine
 writes/      Idempotency/OutcomeUnknown
 domains/     项目业务状态机
```

Harness 是这些模块的组合层，不只是一个文件。

## 9. 不使用 Harness 时的调用链

```python
agent = Agent(
    model=model,
    stream_fn=provider.stream,
    tools=tools,
)
await agent.prompt("问题")
```

调用链：

```text
User
→ Agent
→ Agent Loop
→ Model/Tool
→ Answer
```

优点：简单。

缺少：

-持久 Operation；
-启动恢复；
-Approval Resume；
-统一状态投影；
-幂等写操作协调。

## 10. 使用 Harness 时的调用链

```text
User/API/UI
→ DurableAgentHost
→ HybridModelRouter
→ Agent
→ Agent Loop
→ Provider/Tool Runtime
→ Answer
```

同时旁路：

```text
Agent Event
→ RuntimeStateTracker
→ runtime-events

完整 Message/Tool Fact
→ DurableOperationRecorder
→ operation-events

Retry
→ retry-events

Approval Required
→ ApprovalResumeCoordinator
→ WriteOperationService
→ 恢复原 Context
```

## 11. 一个实际例子

用户：

```text
取消订单 1001
```

仅有 Agent Loop：

```text
模型可能调用 cancel_order
→ Tool 执行
→ 模型回答
```

Harness：

```text
Hybrid Router 判断是写操作
→ VerifiedIdentity
→ Approval Required
→ 持久化原 Operation
→ 返回 Approval ID
→ 审批人批准
→ Action Hash 校验
→ Approval 一次消费
→ Idempotency 去重
→ Write Handler
→ Tool Result 写回完整 Context
→ 模型继续回答
→ Operation/Run Completed
```

这些额外步骤就是 Harness 的价值。

## 12. Pi 中的区别

Pi 的基础层：

```text
packages/agent/src/agent-loop.ts
packages/agent/src/agent.ts
```

负责模型、工具、消息和轻量状态。

Pi Harness：

```text
packages/agent/src/harness/
```

增加：

- Durable Session；
-Lane/Operation；
-Reducer/Invariant；
-Tool Started/Result；
-Queue；
-Resume；
-Compaction/Navigation；
-配置快照。

当前 Python 项目采用了类似分层。

## 13. DeepSeek Harness 中的区别

DeepSeek Agent Loop：

```text
packages/core/agent-loop/
```

负责 Driver、Turn、Step、Model Request、Tool Schedule。

外围 Harness/Platform：

```text
Session Event
Tool Runtime
Scope
Policy
Approval
Provider
Persistence
Sandbox
Workflow
UI/SDK
```

DeepSeek Harness 的生产能力比当前 Python 项目更多，特别是插件系统、Scope、Sandbox、多子系统和持久化边界。

## 14. 当前 Python 项目映射

| Harness 能力 | 当前模块 |
|---|---|
| 创建 Agent | DurableAgentHost |
| 模型 Runtime | OpenAICompatibleProvider/RecoverableModelRuntime |
| 工具 Runtime | Agent Loop/RecoverableToolRuntime |
| Intent Router | HybridModelRouter |
| Capability | CapabilityRegistry |
| 状态 | RuntimeStateTracker |
| 完整 Context | DurableOperationRecorder |
| 恢复 | DurableSessionRecovery/StartupRecoveryCoordinator |
| Retry | retry/ |
| Approval | ApprovalService/ApprovalResumeCoordinator |
| 幂等写 | WriteOperationService |
|身份 | IdentityVerifier |
|业务状态 | DomainStateMachine |

这张表说明当前确实已经形成 Harness 骨架。

## 15. 为什么只能叫 Harness 骨架

当前仍然是：

```text
单 Python 进程
JSONL Store
StaticIdentityVerifier
无正式 Approval Web/TUI
无真实业务 Adapter
```

还缺：

- SQLite/PostgreSQL 事务 Store；
- OAuth/OIDC/IAM；
-生产 Approval UI/API；
-Snapshot/Migration；
-Telemetry/Backpressure；
-多 Worker；
-多 Intent PlanExecutor。

所以准确名称是：

```text
Single-process Durable Agent Harness Skeleton
单进程可持久、可恢复 Agent Harness 骨架
```

不是：

```text
Production Distributed Agent Platform
```

## 16. 判断一个项目是否有 Harness

可以问：

```text
是否有统一创建入口？
是否管理 Provider 和 Tools？
是否保存 Session？
是否能恢复？
是否处理身份和 Approval？
是否处理幂等写操作？
是否提供状态和生命周期？
```

当前项目对这些问题大部分回答是“有”，所以可以说已经有 Harness。

## 最终总结

```text
Agent Loop
= 模型与工具反复交互的发动机

Agent
= 一辆具体运行的车

Harness
= 把 Agent 组装、安全运行、持久化、恢复和管理起来的整套底盘
```

> **当前项目的 Agent Loop 已经合格，DurableAgentHost 也已经构成真实 Harness 骨架；只是它目前定位为单进程教学/可嵌入 Harness，还不是完整生产级分布式平台。**

---

## 对话 61：当前 Harness 骨架基于哪个开源项目

### 用户问题

当前 Harness 骨架是基于哪个开源项目完成的？

### 助手回答

## 结论

当前项目不是单独照搬某一个开源项目，而是：

```text
核心 Agent Loop：主要基于 Pi
Durable Harness：主要参考 Pi Harness
生产可靠性和调度边界：大量借鉴 DeepSeek Harness
业务 Router、Resource Lock、Approval/Write 组合：当前 Python 项目自己的实现
```

最准确的项目描述是：

> **以 Pi Agent Loop 语义为核心，融合 Pi Harness 的 Event/Reducer/Session 设计与 DeepSeek Harness 的 Durable Event、Tool Runtime、Retry、Approval 和调度思想，独立编写的纯 Python 教学实现。**

它不是 Pi 或 DeepSeek Harness 的官方 Python SDK，也不是一比一翻译。

## 1. 最初骨架主要来自 Pi

项目名和包名：

```text
pi-agent-loop-python
pi_agent_loop
```

最初参考：

```text
pi/packages/agent/src/agent-loop.ts
pi/packages/agent/src/agent.ts
pi/packages/ai/src/utils/event-stream.ts
```

继承的核心语义：

```text
双层 Agent Loop
User/Assistant/ToolResult Message
Tool Call → Tool Result → 下一 Turn
Steering
Follow-up
Lifecycle Event
Cancellation
Agent 有状态封装
StreamFn Provider 抽象
```

因此如果只问“Agent Loop 核心基于谁”，答案是：

```text
主要基于 Pi
```

## 2. Durable Harness 主要参考 Pi Harness

Pi 新 Harness 源码：

```text
pi/packages/agent/src/harness/agent-harness.ts
pi/packages/agent/src/harness/reducer.ts
pi/packages/agent/src/harness/session/types.ts
pi/packages/agent/src/harness/session/*
```

当前 Python 项目借鉴：

```text
Operation Started/Finished
Model Step Attempt
Tool Intent/Dispatch/Result
完整 Message Context
Event Log
Reducer
Invariant
Projection
JSONL/内存 Store
Crash Recovery
Replay Policy
```

对应当前模块：

```text
runtime/
session/
harness/
```

例如：

```text
Pi reduceLaneState
→ Python reduce_runtime_state/replay_operation

Pi LaneRecord/Entry
→ Python RuntimeEvent/OperationEvent

Pi AgentHarness
→ Python DurableAgentHost
```

所以当前 Harness 的“持久状态和恢复骨架”与 Pi Harness 的结构关系最直接。

## 3. DeepSeek Harness 影响最大的部分

参考源码：

```text
deepseek-harness/packages/core/agent-loop/
deepseek-harness/packages/core/tools/
deepseek-harness/packages/core/session/
deepseek-harness/packages/llm/llm-retry/
deepseek-harness/packages/interaction/user-approval/
```

主要借鉴：

### Durable Event 与实时状态分离

```text
实时 Agent 状态
≠
持久 Session 事实
```

### Turn/Step/Tool 边界

```text
turn/start
step/start
tool/call
tool/result
step/end
turn/end
```

### Retry Chain

```text
retry scheduled
retry started
retry finished
Retry ID
Backoff/Jitter/Retry-After
```

### Tool Runtime 管线

```text
Pre Execute
Execute
Post Execute
Result
```

### Approval 与策略边界

```text
工具存在
≠
当前 Agent 有权限执行
```

### Parallel Pool + Exclusive Barrier

当前 Python 调度器从 Pi 的“含 Sequential 则整批串行”升级为：

```text
Parallel Pool
→ Exclusive Barrier
→ Parallel Pool
```

这部分更接近 DeepSeek Harness 的 `tool-calls.ts`。

## 4. 当前项目自己新增的设计

不是直接来自任一项目的一比一实现：

```text
resource_locked
```

按 `order:{order_id}` 等 Resource Key 进行同资源串行、不同资源并行。

```text
business.toml + HybridModelRouter
```

使用简单业务配置和结构化模型分类。

```text
RequiredToolCallGuard
```

强制模型调用 Required Capability，并验证关键参数。

```text
VerifiedIdentity + ApprovalResumeCoordinator
```

将可信身份、Action Hash、一次消费和恢复 Payload 连接起来。

```text
WriteOperationService
```

连接 Idempotency Key Hash、Approval、Outcome Unknown 和 Reconciliation。

```text
STATE_MACHINE_IMPLEMENTATION_GUIDE.md
TOOLS_IMPLEMENTATION_GUIDE.md
BUSINESS_REQUIREMENTS.md
```

这些是为当前教学和未来业务扩展编写的中文工程规范。

## 5. 架构来源映射

| 当前 Python 模块 | 主要参考 |
|---|---|
| `loop.py` | Pi Agent Loop |
| `agent.py` | Pi Agent |
| `event_stream.py` | Pi EventStream |
| `providers/` | Pi Provider 抽象 + DeepSeek Adapter 分层 |
| `runtime/` | Pi Harness Reducer + DeepSeek 实时/持久分离 |
| `session/` | Pi Harness Session + DeepSeek Session Event |
| `retry/` | Pi Retry Utils/AgentSession + DeepSeek llm-retry |
| `harness/` | Pi AgentHarness + DeepSeek Agent Factory/Driver |
| `approval/` | DeepSeek Approval/Policy 思想 + 自有实现 |
| `writes/` | 两项目幂等/恢复思想 + 自有实现 |
| Parallel/Exclusive | DeepSeek Tool Scheduler |
| Resource Lock | 当前项目自有扩展 |
| `routing/` | DeepSeek Scope/Capability 思想 + 自有 Hybrid Router |

## 6. 为什么不是直接复制

原项目语言和架构不同：

```text
Pi：TypeScript，多包 Monorepo
DeepSeek Harness：TypeScript + Cordis 插件系统
当前项目：纯 Python 教学实现
```

当前工作采用：

```text
阅读原始源码
→ 提取稳定行为和边界
→ 设计 Python 类型和异步语义
→ 使用 unittest/ScriptedProvider 验证
```

所以是“参考架构后独立改写”，不是机械逐行翻译。

## 7. 当前更接近谁

### 核心循环

```text
更接近 Pi
```

因为：

-双层 Loop；
-Steering/Follow-up；
-事件名和 ToolResult 语义；
-Agent 状态封装。

### Durable Harness

```text
Pi Harness 与 DeepSeek Harness 的混合
```

因为：

-Event/Reducer/Operation 更接近 Pi Harness；
-实时/持久分离、Retry、Tool Pipeline、Approval、Barrier 更接近 DeepSeek Harness。

### 业务扩展

```text
当前 Python 项目自己的设计更多
```

包括 Resource Lock、简化 business.toml、AI 必读中文规范和 Approval Resume 组合。

## 8. 推荐在 README 中怎样描述

```text
本项目是一个纯 Python 教学型 Agent Loop/Harness。
核心循环参考 Pi Agent Loop；持久 Session、Reducer 和恢复参考 Pi Harness；
Retry、Tool Runtime、Approval 和并发调度结合 DeepSeek Harness 的设计思想；
并在此基础上增加了 Python 原生的 Hybrid Router、Resource Lock、
Verified Identity、Approval Resume 和 Idempotent Write 骨架。
```

## 最终总结

如果必须只回答一个项目名称：

```text
核心基础是 Pi
```

如果回答完整 Harness 架构来源：

```text
Pi Harness + DeepSeek Harness 融合设计
```

如果回答代码性质：

```text
根据两套源码研究后独立编写的 Python 教学实现
```

> **当前 Harness 不是照搬 DeepSeek Harness，也不再只是 Pi Agent Loop；它以 Pi 为核心骨架，融合两者的 Harness 设计，并加入当前 Python 项目的自有扩展。**

---

## 对话 62：当前 Harness 还需要优化哪些地方

### 用户问题

目前 Harness 还需要在哪些方面继续优化？

### 助手回答

## 结论

当前 Harness 的功能骨架已经比较完整，下一步不应继续堆功能，而应优先优化：

```text
统一事务存储
恢复管线复用
Approval 产品化
安全和隐私
性能与可观测性
复杂任务和多 Worker
```

最重要的问题是：

> **当前很多能力已经“有了”，但部分仍是单进程 JSONL、开发身份和示例 Adapter，需要升级成生产实现。**

## 1. 最高优先级：统一事务型 Store

当前存在：

```text
runtime-events.jsonl
operation-events.jsonl
retry-events.jsonl
```

它们分别写入，无法保证跨文件原子性。

例如：

```text
Approval 已消费
→ 进程崩溃
→ Write Event 还没有写入
```

可能产生不一致。

建议升级为：

```text
SQLite（单机）
PostgreSQL（多机）
```

同一个事务中完成：

```text
追加 Operation Event
消费 Approval
占用 Idempotency Key
更新 Entity Version
写入 Tool Intent/Result
```

这是 P2 最重要的优化。

## 2. 合并三个 Journal

当前 Runtime、Operation、Retry 分开，便于教学，但生产恢复需要关联三个时间线。

建议统一事件表：

```text
session_events
```

字段：

```text
session_id
operation_id
run_id
event_type
sequence
schema_version
payload
timestamp
```

通过 Projection 分别生成：

```text
Runtime View
Operation View
Retry View
Approval View
Write View
```

这样更接近 Pi Harness 和 DeepSeek Harness 的统一 Session Event Log。

## 3. RecoverableToolRuntime 需要去掉“嵌套 Agent”过渡实现

当前 RecoverableToolRuntime 为了复用工具校验、Timeout 和 Retry，会创建一个临时 Agent，并使用 ScriptedProvider 注入持久 Tool Call。

这在骨架阶段有效，但生产架构更适合抽出统一：

```text
ToolDispatchRuntime
```

调用方式：

```python
result = await tool_runtime.dispatch(
    tool_call,
    identity,
    approval,
    cancellation,
)
```

普通 Agent Loop 和 Recovery 都调用同一个 Runtime。

这样可以：

-避免嵌套 Agent；
-减少额外 Message；
-统一 Hook/Timeout/Retry/Resource Lock；
-更容易审计；
-更容易测试和持久恢复。

这是当前最重要的代码结构优化之一。

## 4. RecoverableModelRuntime 也应成为统一模型调用边界

目前普通 Agent Loop 和 Recovery 都能使用 StreamFn，但状态事件和 Usage 记录仍有差异。

建议抽出：

```text
ModelCallRuntime
```

统一负责：

```text
Provider
Retry
Circuit
Compaction
Cancellation
Usage
Cost
Model Request Started/Finished
Durable Event
```

普通调用、Router 分类、恢复调用都走同一入口。

## 5. DurableAgentHost 需要继续拆分

`DurableAgentHost` 现在已经负责很多事：

-创建 Agent；
-连接 Store；
-恢复；
-Approval；
-Write；
-模型和工具 Adapter；
-状态 Tracker。

继续增加功能会变成巨型类。

建议拆分：

```text
harness/factory.py
harness/lifecycle.py
harness/recovery.py
harness/approval.py
harness/resources.py
```

`DurableAgentHost` 只作为 Facade，对外提供简单 API。

## 6. Approval 需要真实产品入口

当前 Approval 状态机和 Resume 已经有，但仍缺：

```text
Approval REST API
Web/TUI 审批页面
通知
审批列表
过期调度器
撤销
审计查询
```

还要防止：

-Approval Payload 被篡改；
-审批人身份过期；
-同一个 Approval 多端重复消费；
-审批后业务状态已经变化。

消费前应再次检查：

```text
Action Hash
Entity Version
权限
业务状态
```

## 7. StaticIdentityVerifier 必须替换

当前静态身份只适合测试。

生产需要：

```text
OAuth/OIDC
JWT 验签
IAM
企业 SSO
服务账号
Role/Scope
Token Expiration
Key Rotation
```

持久事件中的 Principal ID 还要能够在恢复时重新验证。

## 8. Operation Store 需要加密和数据治理

完整 Context 中可能包含：

-用户输入；
-客户信息；
-Tool Arguments；
-业务结果；
-模型回复。

生产需要：

```text
Encryption at Rest
字段脱敏
访问控制
租户隔离
数据保留时间
删除和导出 API
审计日志
```

当前 `state/` 被 Git 忽略，只解决了不提交代码仓库的问题，不等于生产数据安全。

## 9. Snapshot 和 Migration

事件数量变大后，从第一条重放会越来越慢。

需要：

```text
Snapshot
+ Snapshot 之后的增量 Event
```

并增加：

```text
Event Schema Version
State Version
Migration
Checksum
损坏检测
旧版本兼容测试
```

## 10. Context Compaction 需要生产化

当前 SlidingWindowCompactor 只是保留最近 N 条消息。

风险：

-丢掉重要业务事实；
-丢掉 Tool Call/Result 配对；
-丢掉 Approval/约束信息；
-摘要不可验证。

需要 Token-aware Compactor：

```text
Token 计算
→ 保留最近关键消息
→ 对早期消息做结构化摘要
→ 保留 Tool/Approval/业务事实
→ 验证摘要
→ 持久化 Replacement Event
```

## 11. Provider HTTP Client 应复用连接池

当前 Provider 每个请求创建新的 `httpx.AsyncClient`。

生产优化：

```text
Host 生命周期创建共享 AsyncClient
→ Provider 请求复用连接池
→ Host.close() 统一关闭
```

收益：

-减少 TLS 握手；
-降低延迟；
-减少 Socket；
-更容易统一 Proxy、证书和连接限制。

## 12. 远程 HTTP 必须改为 HTTPS

当前真实 Provider 仍使用远程明文 HTTP。

这是比性能更优先的安全问题：

```text
Bearer API Key
Prompt
Tool Schema
模型结果
```

都可能被网络中间节点看到。

建议：

1. 改用 HTTPS；
2. 或增加 Nginx/Caddy TLS 反向代理；
3. 或使用 VPN/SSH Tunnel；
4. 轮换已经通过 HTTP 使用过的 API Key；
5. 设置 `allow_insecure_http=false`。

## 13. Backpressure

当前 EventStream 仍然是无界队列。

如果模型或工具高速产生 Update，UI 消费很慢，内存会增长。

需要：

```text
有界 Queue
Update 合并
每 Tool 限速
慢消费者策略
终止事件优先
最大内存
```

## 14. Telemetry

建议增加：

```text
Metrics
Tracing
Structured Logging
Alert
```

至少监控：

-模型延迟和 Token；
-费用；
-Tool 延迟；
-Retry 次数；
-Circuit Open；
-Approval 等待时间；
-Recovery 成功率；
-Resource Lock 等待时间；
-队列长度。

## 15. Router 质量评估

HybridModelRouter 使用模型自报 Confidence，不一定可靠。

需要：

- Intent 测试数据集；
-混淆矩阵；
-低置信度阈值校准；
-不同模型版本回归；
-恶意 Prompt 测试；
-漏掉 Required Tool 的统计；
-分类成本和延迟。

## 16. Tool Scheduler 继续优化

已经有：

```text
Parallel Pool
Exclusive Barrier
Resource Lock
```

后续可以增加：

-读写锁；
-公平排队；
-优先级；
-Resource Lock 清理；
-锁等待 Timeout；
-跨进程锁；
-死锁监控；
-同租户限流。

## 17. 多 Intent PlanExecutor

当前仍是一个 Hybrid Intent。

复杂请求：

```text
查询订单
取消另一个订单
发送结果
```

需要：

```text
HybridRequestPlanner
PlanValidator
TaskStateMachine
Dependency Graph
Approval Barrier
PlanExecutor
ResultSynthesizer
```

## 18. 多 Worker 和分布式

需要：

```text
任务 Queue
Worker Lease
Heartbeat
故障接管
At-least-once
Idempotency
分布式 Resource Lock
事务 Outbox
```

这属于后期 P3。

## 19. 测试体系继续增强

当前已有 138 项测试，但生产还应增加：

```text
压力测试
长时间稳定性测试
随机取消测试
Property-based Test
Fuzz Tool Arguments
Chaos Provider/DB
多进程并发测试
旧版本 Migration Test
安全渗透测试
```

## 20. 推荐优化顺序

### 立即处理

```text
1. 远程 HTTP 改 HTTPS，并轮换 Key
2. ToolDispatchRuntime 去除嵌套 Agent
3. 统一 SQLite Transaction Event Store
```

### 随后处理

```text
4. ModelCallRuntime
5. Approval API/UI 和重新校验
6. Snapshot/Migration
7. Token-aware Compaction
```

### 后期处理

```text
8. Backpressure/Telemetry
9. Plan/Task StateMachine
10. Distributed Worker
```

## 最终建议

当前 Harness 已经具备：

```text
统一 Host
持久 Context
恢复
Approval
幂等写
状态机
三种调度
Retry
```

下一步优化重点应从“增加功能”转向：

```text
减少重复执行路径
统一事务和事件来源
加强真实身份与数据安全
提高可观测性和扩展性
```

> **最优先的三个优化是：HTTPS 安全、统一 Tool/Model Runtime、事务型 Event Store。完成后，当前 Harness 才会从功能丰富的单进程骨架进一步接近生产级运行时。**

---

## 对话 63：取消或模型异常是否会留下缺少 Tool Result 的历史

### 用户问题

用户指出：模型 Error/Aborted 或全局串行工具执行中途取消时，Assistant Message 可能包含 A、B、C 三个 Tool Call，但历史中只生成 A 的 Tool Result，B、C 没有结果。下次发送给 OpenAI-compatible API 可能因为 Tool Call 缺少对应 Tool Result 而报协议错误。询问当前是否存在该问题，以及此前优化建议是否提到。

### 助手回答

## 结论

这个问题当前确实存在，而且用户判断准确。

它不是“可能完全不存在”的理论问题，已经用当前代码复现：

```text
Assistant Tool Calls：A、B、C
全局 sequential
执行 A 时用户取消
```

当前实际历史：

```text
Tool Calls：['a', 'b', 'c']
Tool Results：['a']
```

B、C 没有 Tool Result。

如果继续把这段历史发送给 OpenAI-compatible `/chat/completions`，确实可能收到：

```text
Assistant tool_calls 后缺少对应 role=tool 消息
```

或类似 HTTP 400 协议错误。

## 1. 为什么会发生

当前全局串行执行：

```text
for 每个 Tool Call
→ 执行当前工具
→ 生成 Tool Result
→ 如果 cancellation.cancelled
→ break
```

所以 A 被取消并生成结果后直接退出循环，B、C 没有生成任何结果。

但是 Assistant Message 已经完整写入 Context，其中仍然包含 A、B、C。

形成：

```text
assistant:
  tool_calls=[A,B,C]

tool:
  result=A

B/C 缺失
```

## 2. Parallel/Exclusive/Resource 调度也存在部分边界

当前新调度器通常会先完成整批 Preflight，再让所有已计划调用获得成功、取消或错误结果。

但以下情况仍可能留下未闭合调用：

- Preflight 期间用户取消并提前 break；
- Listener/Scheduler 在所有 ToolResult Message 写入前失败；
-模型 Error/Aborted Message 自身携带 Tool Call；
-Durable Operation 已标终态，但 Context 中仍有未匹配 Tool Call。

所以不能只修串行 `break`，需要增加统一 Tool Call Closure Invariant。

## 3. Model Error/Aborted 的实际情况

当前真实 OpenAI Provider 在普通 HTTP/SSE 错误时通常生成：

```text
content=[]
stopReason=error/aborted
```

这种常见路径不会携带 Tool Call，因此通常没有配对问题。

模型 Retry 还会缓冲并丢弃失败 Attempt 的 Partial Tool Call，这也降低了风险。

但以下 Provider 仍可能返回 Error/Aborted + Tool Call：

-自定义 StreamFn；
-ScriptedProvider；
-第三方不完全兼容实现；
-已经组装部分 Tool Call 后才报终止的 Adapter。

当前 Loop 遇到 Error/Aborted 会在执行工具前直接结束，不会补 Tool Result。

所以架构上仍存在缺口。

## 4. Durable Recovery 目前没有完全解决

当前 `OperationRecoveryPlanner` 可以检测非终态 Operation 中缺少 Tool Result 的 Assistant Tool Call，并生成：

```text
execute_tool
replay_safe_tool
reconcile_tool
materialize_tool_result
```

但是：

-用户主动取消的 Operation 通常已经记录为 cancelled 终态；
-Startup Recovery 会跳过终态 Operation；
-终态历史仍可能缺 Tool Result；
-普通 Agent 下一次 Prompt 不会自动调用 Operation Repair；
-用户取消后的 B/C 不应该恢复执行，而应该写入“取消前未 Dispatch”的合成错误结果。

所以 Recovery Planner 只能解决部分崩溃恢复情况，不能代替 Transcript Repair。

## 5. 之前的优化是否提到

没有明确提出和完整覆盖这个问题。

### P0 已解决的是

```text
后台 execute_task 泄漏
Timer/Waiter/Update Task 清理
CancellationToken detach
主要错误不被清理错误覆盖
```

P0 保证“任务真的停下来”，但不保证“Assistant 的每个 Tool Call 都有一个协议配对 Result”。

### P1 已解决的是

```text
完整 Context 持久化
Tool Intent/Dispatch/Result 判断
Safe Replay/Reconcile
```

P1 能发现部分未完成 Tool，但终态取消历史仍不会自动补齐。

因此用户指出的是一个新的、独立的 P0 级协议一致性问题。

## 6. DeepSeek Harness 怎样处理

源码：

```text
deepseek-harness/packages/core/agent-loop/src/tool-calls.ts
```

DeepSeek Harness 在取消时：

```text
停止启动新工具
→ 等待已经启动的工具完成/取消
→ 为所有未启动 Tool Call 写入合成错误 Result
```

关键函数：

```text
appendSkippedToolCall()
```

它会写入：

```text
tool/call
→ tool/result: Error: tool call aborted before dispatch
```

错误码：

```text
ABORTED_BEFORE_DISPATCH
```

因此每个模型 Tool Call 都有闭合结果。

DeepSeek Harness 的做法更适合持久 Session 和 OpenAI Tool 协议。

## 7. Pi 基础 Agent Loop 的情况

Pi 基础 `agent-loop.ts` 的 Sequential 实现同样在：

```text
signal.aborted → break
```

所以这个问题与当前 Python 版本最初继承的 Pi 简单语义有关。

但 Pi 新 Harness 会记录：

```text
tool_started
resultExists
ToolBatchState.unresolved
```

并通过 Reducer/Recovery 检测未解决 Tool Invocation。

当前 Python 项目需要把 DeepSeek 的 Synthetic Skipped Result 与 Pi Harness 的 Unresolved Invariant 结合起来。

## 8. 正确修复原则

必须建立全局不变量：

> **只要一个 Assistant Message 被提交到历史，其中每个 Tool Call ID 在进入下一条 User/Assistant Message 前都必须存在且只存在一个 Tool Result。**

## 9. 在线执行阶段修复

执行工具批次时维护：

```text
all_tool_call_ids
resolved_tool_call_ids
unresolved_tool_call_ids
```

无论因何结束：

-正常；
-用户取消；
-Listener 异常；
-Scheduler 异常；
-Preflight 失败；
-Budget；

都在 Batch 结算边界为未解决调用生成合成 ToolResult：

```python
{
    "isError": True,
    "details": {
        "code": "tool_aborted_before_dispatch"
    },
    "content": [
        {
            "type": "text",
            "text": "工具调用在执行前因取消而跳过"
        }
    ]
}
```

不能执行 B/C，只能补“未执行”的正式结果。

## 10. Model Error/Aborted 修复

如果 Error/Aborted Assistant 中包含 Tool Call：

### 参数可能不完整

优先做法：

```text
不要把这些 Tool Call 作为可执行调用提交
→ 从最终错误 Message 的 content 中移除 Tool Call
```

### 已经作为 Durable Assistant 提交

必须补：

```text
model_response_incomplete
或 tool_not_executed_due_model_error
```

Synthetic ToolResult，不能执行。

## 11. 发送模型前的 Transcript Validator

即使在线阶段修复，也应在序列化前再检查：

```text
Assistant Tool Call 是否都有 Result
是否存在重复 Tool Result
是否存在 Orphan Tool Result
Tool Call ID/Name 是否匹配
```

处理方式：

```text
可安全补齐的取消/跳过
→ 生成 Synthetic ToolResult

无法判断的损坏
→ 拒绝请求并返回 transcript_corrupted
```

不能把非法历史直接发送给 Provider。

## 12. Durable Session Invariant

Operation Reducer 还应增加：

```text
Operation Finished 时不能有 unresolved Tool Call
Cancelled/Failed 终态前必须补齐 Synthetic Result
Tool Result 每个 Tool Call 最多一个
Tool Result 不能引用不存在的 Tool Call
下一条 User/Assistant 前必须闭合前一个 Tool Batch
```

恢复器遇到用户取消的未 Dispatch Tool 时：

```text
生成 cancelled_before_dispatch Result
```

而不是 `execute_tool`。

## 13. 需要增加的测试

```text
tests/test_tool_call_closure.py
```

至少覆盖：

1. Sequential A/B/C，执行 A 时取消，得到 A/B/C 三个 Result；
2. 执行第一个工具前取消，全部得到 skipped Result；
3. Parallel Preflight 中途取消，所有 Tool Call 闭合；
4. Exclusive Barrier 前取消后续调用；
5. Listener/Scheduler 异常后 Transcript 不残缺；
6. Error Assistant 含 Tool Call 时不执行并补齐/移除；
7. Duplicate/Orphan Tool Result 被拒绝；
8. OpenAI Serializer 不接收未闭合 Transcript；
9. Durable Cancelled Operation 重放后没有 unresolved Tool Call。

## 14. 严重程度

```text
级别：P0
```

原因：

-会破坏 Provider 协议；
-会让下一次 Prompt 直接 HTTP 400；
-会让持久 Session 无法安全重放；
-可能错误恢复未执行写操作；
-影响 Sequential、Cancel、Listener Failure 和 Custom Provider。

## 最终结论

```text
P0 Task Cleanup
保证工具真的停止

Tool Call Closure
保证历史协议完整
```

两者是不同问题，必须分别解决。

> **当前确实存在取消后部分 Tool Call 没有对应 Tool Result 的缺口；此前 P0/P1 只间接触及，没有完整修复。最合理的下一步是增加 Synthetic Skipped ToolResult、Transcript Validator 和 Durable Closure Invariant。**

---

## 对话 64：修复取消或异常导致 Tool Call 缺少 Tool Result 的 P0 问题

### 用户问题

修复 Tool Call/Tool Result 未闭合的 P0 问题。

### 助手回答

Tool Call Closure P0 已完成。现在只要 Assistant Tool Call 被写入历史，无论正常、取消、跳过还是 Listener/Scheduler 异常，都会保证每个 Tool Call 有且只有一个对应 ToolResult。

## 1. 新增 Transcript 协议模块

```text
src/pi_agent_loop/transcript.py
```

公开能力：

```python
analyze_tool_call_transcript(messages)
repair_unresolved_tool_calls(messages)
validate_closed_tool_call_transcript(messages)
sanitize_terminal_assistant_tool_calls(message)
```

错误类型：

```python
TranscriptIntegrityError(code, message)
```

## 2. 全局 Tool Call Closure 不变量

```text
一个 Assistant Message 中的每个 Tool Call
→ 在下一条 User/Assistant Message 前
→ 必须存在且只存在一个 ToolResult
```

同时检查：

- Duplicate Tool Call ID；
-Duplicate ToolResult；
-Orphan ToolResult；
-Tool Call/Result Name Mismatch；
-Missing ToolResult。

## 3. Sequential 取消修复

旧行为：

```text
Assistant：A、B、C
执行 A 时取消
→ 只生成 A Result
→ break
```

新行为：

```text
A → tool_cancelled
B → tool_aborted_before_dispatch
C → tool_aborted_before_dispatch
```

B/C 不会执行，只补 Synthetic Error Result。

复现结果已经从：

```text
calls=['a','b','c']
results=['a']
```

修复为：

```text
calls=['a','b','c']
results=[
  ('a','tool_cancelled'),
  ('b','tool_aborted_before_dispatch'),
  ('c','tool_aborted_before_dispatch')
]
```

## 4. Parallel/Exclusive Preflight 取消修复

Parallel Scheduler 不再在 Preflight 检测到取消后直接 break。

它会继续遍历剩余模型 Tool Call，但 `_prepare_tool_call()` 对每个未执行调用返回：

```python
{
    "code": "tool_aborted_before_dispatch",
    "synthetic": True,
}
```

因此 Parallel、Exclusive Barrier 和 Resource 调度中的全部 Tool Call 都会闭合。

## 5. Synthetic Skipped ToolResult

新增统一辅助逻辑：

```text
_append_skipped_tool_results()
```

为每个未 Dispatch 调用发出：

```text
tool_execution_start
tool_execution_end
message_start ToolResult
message_end ToolResult
```

内容明确说明：

```text
工具调用在执行前因 Agent 取消而跳过
```

不会假装工具执行过。

## 6. Error/Aborted Assistant 修复

模型最终状态是：

```text
stopReason=error/aborted
```

时，最终 Assistant 中的 Tool Call 会被移除：

```python
sanitize_terminal_assistant_tool_calls()
```

原因：Error/Aborted 下的 Tool Call 可能是残缺参数，不能执行，也不能作为正常 Tool Call 写入历史。

普通 Text/Thinking 内容可以保留。

## 7. Listener/Scheduler 异常修复

如果异常发生在部分 ToolResult 尚未写入时：

```text
Agent._handle_run_failure()
→ 扫描当前 Agent State
→ 按模型 Tool Call 顺序补齐 Synthetic ToolResult
→ 再追加失败 Assistant
```

错误码：

```text
tool_not_executed_due_run_error
```

新增修复通知：

```text
transcript_repaired
```

通知所有 Listener，但修复 Listener 的二次失败不会覆盖原始运行错误。

## 8. 下一次 Prompt 前自动修复旧历史

`Agent._run_prompt_messages()` 和 `continue_run()` 在加入新 User Message 前调用：

```python
repair_unresolved_tool_calls()
```

明确缺失的结果补为：

```text
tool_result_missing_repaired
```

顺序：

```text
旧 Assistant Tool Call
→ Synthetic ToolResult
→ 新 User Message
```

Duplicate/Orphan/Name Mismatch 属于无法安全自动修复的损坏，抛出 TranscriptIntegrityError，不调用模型。

## 9. 低层 Loop 防线

直接使用：

```python
run_agent_loop()
```

而不经过 Agent 时，`_stream_assistant_response()` 也会：

```text
修复明确 Missing Result
→ Transform Context
→ 验证闭合
→ 请求 Provider
```

所以低层 API 同样受到保护。

## 10. OpenAI Serializer 最后防线

`serialize_chat_request()` 在生成 Payload 前执行：

```python
validate_closed_tool_call_transcript(raw_messages)
```

未闭合历史会转换成：

```text
ProviderProtocolError:
模型消息历史违反 Tool Call 协议
```

不会发送给 `/chat/completions` 后再等待 HTTP 400。

## 11. DurableOperationRecorder

收到：

```text
transcript_repaired
```

后会把 Synthetic ToolResult 持久化为：

```text
message_appended
syntheticRepair=true
```

因此 Agent State 和 Durable Operation Context 保持一致。

## 12. Operation Finished Invariant

`reduce_operation_event()` 在 `operation_finished` 前检查：

```text
不存在 Started 但未完成的 Model Request
Tool Call Transcript 完全闭合
不存在 Duplicate/Orphan/Name Mismatch
```

Cancelled 和 Failed Operation 也不能带着未解决 Tool Call 进入终态。

## 13. Durable Recovery 语义

正常取消现在会在终态前生成 Skipped Result，因此 Startup Recovery 不需要恢复执行 B/C。

崩溃导致的非终态调用仍按原规则：

```text
未 Dispatch → execute_tool
Safe Dispatch → replay_safe_tool
Never Dispatch → reconcile_tool
Result 已持久化 → materialize_tool_result
```

取消和崩溃恢复的语义已经分开。

## 14. 新增测试

```text
tests/test_tool_call_closure.py
```

共 10 项：

-Sequential A/B/C 中途取消；
-首个 Tool Preflight 前取消；
-Parallel Preflight 取消；
-Exclusive Barrier 取消；
-Listener 异常自动修复；
-Error Assistant 移除 Tool Call；
-Duplicate/Orphan Result 拒绝；
-OpenAI Serializer 拒绝未闭合历史；
-Durable Cancelled Operation 无 unresolved；
-下一 Prompt 前修复旧历史。

## 15. 测试结果

```text
python -m unittest tests.test_tool_call_closure -v
```

结果：

```text
Ran 10 tests
OK
```

全部项目：

```text
Ran 148 tests
OK
```

同时通过：

- `python -m compileall`；
- `git diff --check`；
-原问题确定性复现验证；
-Transcript API 顶层导入检查。

## 16. 文档和项目规则

已更新：

```text
README.md
TOOLS_IMPLEMENTATION_GUIDE.md
AGENTS.md
BUILTIN_TOOLS_DESIGN.md
conversation-review.md
```

AI 以后修改 Tool Batch/Cancel/Provider 序列化时，必须保持 Tool Call Closure 不变量。

> **P0 Tool Call Closure 已完成：工具可能成功、失败、取消或根本没有执行，但只要 Tool Call 进入历史，就一定有一个正式 ToolResult；未闭合、重复或孤立历史不会发送给 Provider。**

---

## 对话 65：Approval Grant 后、Resume Started 前是否存在永久卡死窗口

### 用户问题

用户指出：如果进程在 `approval_granted` 已持久化之后、`approval_resume_started` 写入之前崩溃，Approval 已不再是 waiting，重新批准会失败，而 `recover_incomplete()` 又只扫描 Started，可能导致退款等写操作永久无法继续。询问当前是否存在该问题。

### 助手回答

## 结论

这个问题当前确实存在，而且不只是理论风险，已经使用当前代码复现。

复现状态：

```text
approval_state = approved
approval_resume_registered 已存在
approval_resume_started 不存在
recover_incomplete() 返回 []
```

因此经理已经批准，但恢复器看不到这条待恢复操作，确实可能永久卡住。

严重程度：

```text
P0 Durable State Transition Gap
```

## 1. 当前代码顺序

当前 `approve_and_resume()` 大致顺序：

```text
ApprovalService.grant()
→ approval_granted 持久化

get_pending()

ApprovalService.consume()
→ approval_consumed 持久化

写 approval_resume_started

调用 Resume Callback

写 approval_resume_completed/failed
```

这里存在两个危险窗口。

## 2. 窗口一：Granted 后、Consumed 前

```text
approval_granted
→ 进程崩溃
→ approval_consumed 尚未写
→ approval_resume_started 尚未写
```

重启后：

```text
Approval.state = approved
```

重新调用 `approve_and_resume()` 时，第一步仍然调用：

```text
grant()
```

但 `grant()` 只接受：

```text
waiting
```

因此返回：

```text
approval_not_waiting
```

## 3. 窗口二：Consumed 后、Started 前

```text
approval_consumed
→ 进程崩溃
→ approval_resume_started 尚未写
```

重启后：

```text
Approval.state = consumed
```

同样不能再次 Grant 或 Consume。

而当前：

```python
recover_incomplete()
```

只扫描：

```text
approval_resume_started
```

没有 Started，就完全发现不了这条 Operation。

## 4. 为什么 Registered 没有发挥作用

Approval 请求阶段已经持久化：

```text
approval_resume_registered
```

其中包含：

```text
approvalId
Action
Resume Payload
Session ID
Operation ID
```

这其实已经是恢复所需的 Durable Intent。

但当前恢复器没有从 Registered 开始扫描，而是从 Started 扫描，所以漏掉了 Granted/Consumed 到 Started 之间的窗口。

## 5. 当前测试为什么没有发现

已有“已消费 Approval 的崩溃恢复”测试手工写入了：

```text
approval_resume_started
```

然后再调用 `recover_incomplete()`。

它只验证了：

```text
Started 后崩溃可以恢复
```

没有覆盖：

```text
Granted 后、Started 前
Consumed 后、Started 前
```

所以这是 P1 测试中的真实盲区。

## 6. 正确状态机原则

持久流程必须遵守：

> **先持久化“我要恢复这个操作”的 Intent，再修改不可逆状态。**

当前已经有：

```text
approval_resume_registered
```

因此恢复器应该以 Registered 为起点，而不是 Started。

## 7. approve_and_resume 应改成幂等状态驱动

不应每次固定执行：

```text
grant → consume → started
```

而应读取当前状态：

```text
waiting
→ Grant
→ Consume
→ 确保 Started
→ Resume

approved
→ 不再 Grant
→ Consume
→ 确保 Started
→ Resume

consumed
→ 不再 Grant/Consume
→ 确保 Started
→ Resume

Started 未完成
→ 直接 Resume

Completed
→ 返回已完成，不重复执行
```

每一步都必须幂等。

## 8. recover_incomplete 应扫描 Registered

推荐算法：

```text
扫描 approval_resume_registered
→ 排除 approval_resume_completed
→ 排除 rejected/expired
→ 读取 Approval 当前状态
```

然后：

```text
waiting
→ 继续等待人工批准

approved
→ Consume
→ 写 Started
→ Resume

consumed
→ 写 Started（如果缺失）
→ Resume

Started
→ Resume

Completed
→ 跳过

Rejected/Expired
→ 写 Resume Cancelled/Failed
```

## 9. 还需要防止重复 Resume

如果两个进程同时恢复：

```text
两个进程都看到 consumed
→ 都写 started
→ 都执行退款
```

JSONL 单进程锁只能保护单进程，无法保护多进程。

生产环境需要：

```text
SQLite/PostgreSQL Transaction
唯一约束 approvalId + resume generation
Lease/Claim
Compare-And-Set State
```

同一个 Approval Resume 同时只能有一个 Worker 获得执行权。

## 10. 写操作还必须依赖 Idempotency

即使 Resume Started 已经持久化，也存在：

```text
Write Handler 已成功
→ 进程在 Completed Event 前崩溃
```

恢复时可能再次调用 Handler。

因此：

```text
Approval Resume 幂等
+
WriteOperation Idempotency Key
+
Outcome Reconciliation
```

必须同时存在。

只修 Approval 状态机仍不能完全避免重复退款。

## 11. 需要增加的测试

```text
tests/test_approval_resume_windows.py
```

至少覆盖：

1. Registered + Waiting：不自动执行；
2. Granted、未 Consumed、未 Started：恢复 Consume 并执行；
3. Consumed、未 Started：恢复 Started 并执行；
4. Started、未 Completed：继续执行；
5. Completed：不重复执行；
6. Rejected/Expired：不执行；
7. 同一 approve_and_resume 重复调用：结果幂等；
8. Write Handler 成功后崩溃：Idempotency 防止重复副作用；
9. 两个恢复 Worker：只有一个 Claim 成功（事务 Store 阶段）。

## 12. 当前准确判断

```text
Tool Call Closure P0
已修复

Task Cleanup P0
已修复

Approval Grant→Started Crash Window
尚未修复，当前真实存在
```

## 最终结论

> **当前 Approval Resume 存在永久卡死窗口。根因是恢复扫描从 `approval_resume_started` 开始，而不是从已经持久化的 `approval_resume_registered` 开始，并且 `approve_and_resume()` 不是按 waiting/approved/consumed 状态幂等推进。**

最合理的下一步是：

```text
以 Registered 为恢复锚点
→ 幂等推进 Approval 状态
→ 缺失时补 Started
→ 使用事务 Claim 防并发恢复
→ 依赖 Write Idempotency 防重复副作用
```

---

## 对话 66：修复 Approval Grant/Consume 到 Resume Started 的 P0 崩溃窗口

### 用户问题

修复 Approval Resume 永久卡死的 P0 问题。

### 助手回答

Approval Resume 崩溃窗口已经修复。恢复逻辑现在从 `approval_resume_registered` 开始扫描，并按照 waiting/approved/consumed/started/completed 当前状态幂等推进。

## 1. Durable 恢复锚点改为 Registered

旧逻辑：

```text
只扫描 approval_resume_started
```

新逻辑：

```text
扫描 approval_resume_registered
→ 排除 Completed/Failed/Cancelled
→ 读取 Approval 当前状态
→ 决定下一步
```

Registered 在 Grant 和 Consume 之前持久化，所以不会再漏掉 Granted→Started 或 Consumed→Started 窗口。

## 2. approve_and_resume 改为状态驱动

不再固定执行：

```text
grant → consume → started
```

现在：

```text
waiting
→ Grant
→ Consume
→ 确保 Started
→ Resume

approved
→ 跳过 Grant
→ Consume
→ 确保 Started
→ Resume

consumed
→ 跳过 Grant/Consume
→ 确保 Started
→ Resume

started 未完成
→ 直接 Resume

completed
→ 返回持久结果
→ 不重复 Resume
```

因此重新调用不会因为 `approval_not_waiting` 卡死。

## 3. Granted 未 Consumed/Started 恢复

重启后：

```text
Approval.state=approved
approval_resume_registered 存在
approval_resume_started 不存在
```

恢复器：

```text
解析 Verified Consumer
→ Consume Approval
→ 补写 approval_resume_started
→ 执行 Resume
→ 写 approval_resume_completed
```

确定性复现结果已经从：

```text
approval_state=approved
recovered=[]
resume_events=[registered]
```

修复为：

```text
approval_state=consumed
recovered=[approvalId]
resume_events=[
  approval_resume_registered,
  approval_resume_started,
  approval_resume_completed
]
```

## 4. Consumed 未 Started 恢复

```text
Approval.state=consumed
Started 缺失
```

恢复器不再 Grant/Consume，直接：

```text
补写 Started
→ Resume
→ Completed
```

## 5. Approved 缺少可信 Consumer

恢复已批准 Approval 需要可信 Consumer 才能一次性消费。

如果没有：

```python
consumer_resolver
```

系统不会伪造身份，也不会把任务标记失败。

它保持可恢复，并通过：

```python
pending_recovery_ids()
```

暴露给 Host，等待真实 Identity Adapter。

## 6. Rejected/Expired

Registered Approval 如果已经：

```text
rejected
expired
```

不会执行 Resume，而是补写：

```text
approval_resume_cancelled
```

重复扫描不会重复写 Cancelled。

## 7. Completed 幂等返回

`approval_resume_completed` 现在保存 JSON 可序列化结果：

```python
{
    "approvalId": "...",
    "result": ...,
    "resultStored": True,
}
```

重复调用 `approve_and_resume()`：

```text
读取 Completed
→ 返回原结果
→ 不再 Grant
→ 不再 Consume
→ 不再执行 Resume Callback
```

如果 Callback 返回值不可 JSON 序列化，仍保存 Completed 标记和 `resultStored=False`，避免为了返回值重复副作用。

## 8. 同进程并发恢复去重

Coordinator 新增：

```text
asyncio.Lock
_active[approvalId] = shared Task
```

两个协程同时恢复同一 Approval：

```text
只创建一个 Resume Task
→ 两个调用等待同一个 Task
→ Callback 只执行一次
```

Task 名称：

```text
approval-resume:<approvalId>
```

## 9. Startup Recovery 与 Approval 分流

普通 StartupRecovery 现在识别：

```text
approval_pending
approval_resume_registered
```

不会再把等待审批的 Never 写工具误判为普通 `execute_tool`。

Approval 恢复由 ApprovalResumeCoordinator 专属处理。

## 10. DurableAgentHost 启动支持

Host 新增：

```text
pending_approval_resumes
recovered_approval_resumes
```

创建时可注入：

```python
approval_resume_handler=...
approval_consumer_resolver=...
```

有 Handler/Consumer 时自动恢复。

没有时明确列出 Pending ID，不会永久隐藏。

## 11. Write Idempotency 仍是第二道防线

还存在窗口：

```text
Resume Callback 中写操作成功
→ Completed Event 前崩溃
```

恢复会再次进入 Callback。

因此测试使用 Idempotency Key 模拟：

```text
第二次进入 Callback
→ 检查 Key 已应用
→ 不重复产生副作用
```

Approval Resume 幂等负责流程不丢失，Write Idempotency 负责业务副作用不重复。

## 12. 新增测试

```text
tests/test_approval_resume_windows.py
```

共 9 项：

- Registered+Waiting 不执行；
-Granted 未 Consumed/Started 恢复；
-Consumed 未 Started 恢复；
-Started 未 Completed 恢复；
-Completed 重复调用不执行；
-Rejected 写 Cancelled；
-同进程并发 Resume 只执行一次；
-Approved 缺 Consumer 保持可恢复；
-Started 后副作用通过 Idempotency 去重。

全部项目：

```text
Ran 157 tests
OK
```

## 13. 当前边界

单进程 P0 卡死窗口已经修复。

多进程同时 Claim 同一 Approval 仍需要 P2：

```text
SQLite/PostgreSQL Transaction
Unique Constraint
Compare-And-Set
Lease
```

JSONL Lock 只能保护当前进程，不能提供跨进程 Exactly-once。

> **Approval Resume 现在以 Registered 为 Durable Intent，waiting/approved/consumed/started/completed 均可幂等推进；Granted 或 Consumed 后即使在 Started 前崩溃，也能被恢复器发现并继续。**

---

## 对话 67：启动恢复是否真正理解等待审批状态

### 用户问题

用户指出：Operation Reducer 当前忽略 Approval/Write Event，Recovery Planner 看到 Assistant 中未完成的退款 Tool Call 时可能直接规划 `execute_tool`。经理审批前重启后，如果恢复授权过宽，可能绕过审批执行退款；没有授权则可能失败，而不是继续等待审批。询问当前是否存在。

### 助手回答

## 结论

这个问题目前是“默认 DurableAgentHost 路径已有防护，但底层 Operation Reducer/Recovery Planner 仍然不真正理解 Approval 状态”。

所以：

```text
默认 Host 启动流程
→ 当前不会直接绕过审批执行退款

直接调用 DurableSessionRecovery
或授权回调配置过宽
→ 仍存在绕过审批的架构风险
```

它属于尚未彻底消除的 P0/P1 状态建模缺口。

## 1. 当前 Operation Reducer 的确忽略 Approval/Write

`operation_state.py` 当前对：

```text
routing_*
approval_*
write_*
```

只执行：

```text
return base
```

因此 OperationState 只知道：

```text
phase=running
messages 中有 Assistant Tool Call
该 Tool Call 没有 ToolResult
```

它不知道：

```text
Approval 正在 waiting
Approval 已 approved
Approval 已 consumed
WriteOperation 当前状态
```

## 2. Recovery Planner 确实会错误规划 execute_tool

已经用当前代码复现以下事件：

```text
operation_started
message user
model_request_started/completed
Assistant Tool Call refund
approval_pending
approval_requested
approval_resume_registered
```

Approval 仍然 waiting。

直接调用：

```python
DurableSessionRecovery(store).plan(...)
```

当前结果：

```text
operation_phase=running
actions=[('execute_tool','refund')]
```

所以用户指出的底层问题真实存在。

## 3. 为什么默认 DurableAgentHost 暂时不会直接执行

上一轮修复中，`StartupRecoveryCoordinator` 增加了特殊检查：

```text
存在 approval_pending
或 approval_resume_registered
并且没有 Resume Completed/Cancelled
→ 不进入普通 Tool Recovery
→ 返回 manual_intervention
```

另外 `RecoverableToolRuntime` 对：

```text
replay_policy=never
```

默认拒绝，除非提供：

```python
authorize_never_replay
```

所以默认 Host 有两层保护：

```text
Startup 特判 Approval Pending
+
Never Tool 默认拒绝恢复执行
```

## 4. 当前实际行为不是“直接失败”而是 Manual Intervention

在当前版本中，普通 StartupRecovery 遇到 Approval Pending 会加入：

```text
manual_intervention
```

不会直接标记 Operation Failed，也不会执行工具。

ApprovalResumeCoordinator：

```text
waiting → 继续等待
approved/consumed/started → 专属恢复
```

因此用户描述的“没有授权就标记失败”更接近旧行为；当前默认路径已经改为等待/人工介入。

## 5. 为什么仍然不能认为彻底解决

当前安全依赖：

```text
StartupRecoveryCoordinator 的事件名特殊判断
```

而不是：

```text
OperationState.phase=waiting_approval
Recovery Planner 原生理解 ApprovalRecord
```

这比较脆弱：

-其他调用方可以绕过 StartupCoordinator，直接调用 DurableSessionRecovery；
-未来新增另一种 Approval Event 时可能忘记更新特判；
-authorize_never_replay 如果写得过宽，可能执行未审批写工具；
-Operation Projection 无法准确显示 Approval/Write 状态；
-Invariant 无法检查 Tool Execution 是否拥有已消费 Approval。

所以它是“外围挡住了”，不是“状态机从根上理解了”。

## 6. authorize_never_replay 是危险边界

如果应用写成：

```python
authorize_never_replay=lambda action: True
```

那么直接 Recovery Planner 得到：

```text
execute_tool refund
```

后，RecoverableToolRuntime 可能允许 Never Tool 进入执行。

这会绕过：

-Approval Action Hash；
-Approval 一次消费；
-WriteOperation Idempotency；
-outcome_unknown Reconciliation。

因此 Never Tool 不应该使用一个简单布尔授权回调，而应该要求正式：

```text
Consumed Approval Record
+
WriteOperation ID
+
Action Hash
+
Idempotency Key
```

## 7. 正确修复方式

### OperationState 应正式包含 Approval

```text
approvals: dict[approvalId, ApprovalSnapshot]
```

Approval 状态：

```text
waiting
approved
consumed
rejected
expired
```

### OperationState 应正式包含 WriteOperation

```text
writes: dict[writeId, WriteSnapshot]
```

Write 状态：

```text
prepared
waiting_approval
approved
submitting
succeeded
failed
outcome_unknown
reconciling
```

### Operation Phase 应能投影

```text
waiting_approval
ready_to_resume
executing_write
outcome_unknown
```

## 8. Recovery Planner 的优先级应修改

正确顺序：

```text
先检查 Approval/Write 状态
→ 再检查未完成 Model/Tool Call
```

规则：

```text
Approval waiting
→ wait_for_approval
→ 绝不能 execute_tool

Approval approved
→ consume_approval 或等待可信 Consumer

Approval consumed
→ resume_approved_write

Approval rejected/expired
→ synthetic denied ToolResult
→ 结束或继续模型说明拒绝

Write outcome_unknown
→ reconcile_write

只有没有 Approval/Write 约束的 Tool Call
→ execute_tool/replay_safe_tool
```

## 9. RecoveryAction 应增加

```text
wait_for_approval
consume_approval
resume_approved_write
reconcile_write
finalize_rejected_approval
```

而不是把所有未完成 Tool Call 都归成：

```text
execute_tool
```

## 10. 必须增加 Invariant

```text
waiting Approval 时不能 tool_dispatch_started
Never Tool Dispatch 必须引用 consumed approvalId
Approval Action Hash 必须匹配 Tool Name/Arguments
Write ID 必须关联 Tool Call ID
Rejected/Expired Approval 不得执行
Operation Finished 前 Approval/Write 必须进入允许终态
```

## 11. 需要增加测试

```text
tests/test_approval_aware_recovery.py
```

覆盖：

1. Waiting Approval → wait_for_approval；
2. Waiting Approval 永不 execute_tool；
3. Approved 缺 Consumer → 等待，不失败；
4. Consumed → resume_approved_write；
5. Rejected/Expired → Synthetic Denied Result；
6. OutcomeUnknown → reconcile_write；
7. Never Tool 无 Approval → Invariant 拒绝；
8. Action Hash 不匹配 → 拒绝；
9. Direct DurableSessionRecovery 也不能绕过审批；
10. authorize_never_replay=True 不能替代 Consumed Approval。

## 12. 当前准确状态

```text
Approval Grant→Started Crash Window
→ 已修复

Startup 默认绕过审批
→ 当前有特殊防护，不会执行

Operation Reducer 原生理解 Approval/Write
→ 尚未实现

Direct Recovery Planner 绕过风险
→ 当前存在
```

## 最终结论

> **当前默认 DurableAgentHost 启动恢复不会在经理审批前直接执行退款，因为 StartupCoordinator 会把 Approval Pending 分流到 Manual Intervention，Never Tool 也默认拒绝。但 Operation Reducer 和 Recovery Planner 仍不理解 Approval/Write 状态，直接调用 Recovery Planner 仍会错误产生 execute_tool；所以问题只被外围防住，尚未从状态机根部解决。**

---

## 对话 68：修复 Operation Reducer/Recovery Planner 不理解 Approval/Write 的 P0 问题

### 用户问题

修复启动恢复只靠外围特判、底层 Reducer/Planner 可能绕过等待审批的问题。

### 助手回答

Approval/Write 感知恢复已经从状态机根部完成。Operation Reducer 现在正式归约 Approval/Write Event，Recovery Planner 会先检查审批和写操作状态，再考虑普通 Tool Call。

## 1. OperationState 新增 ApprovalSnapshot

```python
OperationState.approvals: dict[str, ApprovalSnapshot]
```

字段：

```text
approval_id
state
action_hash
requester_id
required_role
action
tool_call_id
consumer_id
resume_state
```

状态：

```text
waiting
approved
consumed
resume_started
resume_completed
resume_failed
rejected
expired
resume_cancelled
```

## 2. OperationState 新增 WriteSnapshot

```python
OperationState.writes: dict[str, WriteSnapshot]
```

字段：

```text
write_id
state
tool_name
arguments
action_hash
idempotency_key_hash
tool_call_id
approval_id
result
```

状态：

```text
prepared
waiting_approval
approved
submitting
succeeded
failed
outcome_unknown
reconciling
```

## 3. Operation Phase 现在反映审批和写操作

```text
approval_requested
→ waiting_approval

approval_granted/consumed
→ ready_to_resume

approval_resume_started/write_submitting
→ executing_write

write_outcome_unknown/write_reconciling
→ outcome_unknown

resume/write 完成
→ running
```

不再始终显示 `phase=running`。

## 4. Approval Event 正式归约

Reducer 现在处理：

```text
approval_pending
approval_requested
approval_resume_registered
approval_granted
approval_consumed
approval_rejected
approval_expired
approval_resume_started
approval_resume_completed
approval_resume_failed
approval_resume_cancelled
```

不再直接忽略 `approval_*`。

## 5. Write Event 正式归约

Reducer 现在处理：

```text
write_prepared
write_waiting_approval
write_approved
write_submitting
write_succeeded
write_failed
write_outcome_unknown
write_reconciling
```

不再直接忽略 `write_*`。

## 6. Approval Action Hash 绑定 Tool Call

`approval_resume_registered` 载入时检查：

```text
SHA-256(Action)
==
approval_requested.actionHash
```

Tool Intent/Dispatch 时再次检查：

```text
Tool Name
Tool Arguments
==
Approval Action
```

参数或工具被修改会抛：

```text
Approval Action Hash 与 Tool Call 不匹配
```

## 7. Waiting Approval 禁止 Dispatch

`tool_dispatch_started` 时：

```text
找到关联 Approval
→ 状态必须 consumed 或 resume_started
```

如果仍是：

```text
waiting
approved
```

Reducer 抛出：

```text
Tool 仍在等待 Approval，禁止 Dispatch
```

这条 Invariant 位于 Operation Reducer，不依赖外部布尔授权回调。

## 8. Write ID 与 Tool Call ID 关联

`WriteOperationService.prepare()` 新增：

```python
tool_call_id: str | None
```

Host Approval Resume 会把当前持久 Tool Call ID 写入 Write Prepared Event。

Recovery 可以明确知道哪个 Write 属于哪个模型 Tool Call。

## 9. Recovery Planner 改为 Approval/Write 优先

新顺序：

```text
Write State
→ Approval State
→ Pending Model Request
→ 普通 Tool Call
```

因此 Assistant 即使存在未完成 refund Tool Call，只要 Approval waiting，就不会产生 `execute_tool`。

## 10. 新增 Recovery Action

```text
wait_for_approval
consume_approval
resume_approved_write
reconcile_write
finalize_rejected_approval
```

处理规则：

```text
Approval waiting
→ wait_for_approval

Approval approved
→ consume_approval

Approval consumed/resume_started
→ resume_approved_write

Approval rejected/expired
→ finalize_rejected_approval

Write submitting/outcome_unknown/reconciling
→ reconcile_write
```

## 11. Direct DurableSessionRecovery 不再绕过

原复现：

```text
operation_phase=running
actions=[execute_tool refund]
```

修复后：

```text
operation_phase=waiting_approval
approval_state=waiting
resume_state=registered
actions=[wait_for_approval refund]
```

这在直接调用 `DurableSessionRecovery.plan()` 时也成立，不依赖 StartupCoordinator 特判。

## 12. Recovery Executor 新状态

没有对应可信 Callback 时返回：

```text
waiting_approval
approval_consumer_required
approved_write_runtime_required
write_reconciliation_required
```

不会调用普通 `execute_tool`，也不会把 Operation 错误标记 Failed。

`RecoveryCallbacks` 新增可选：

```python
consume_approval
resume_write
reconcile_write
```

## 13. Rejected/Expired 自动闭合

恢复时：

```text
Approval rejected/expired
→ 生成 Synthetic ToolResult
   code=approval_not_granted
→ 写 approval_resume_cancelled
→ 使用完整 Context 继续模型
→ Operation Completed
```

写工具不会执行，Tool Call 协议仍保持闭合。

## 14. Outcome Unknown 优先 Reconcile

Write 为：

```text
submitting
outcome_unknown
reconciling
```

Planner 返回：

```text
reconcile_write
```

不会因为 Assistant 缺 ToolResult 就重放写工具。

## 15. StartupRecoveryReport

新增：

```python
waiting_approval: tuple[str, ...]
```

等待审批、缺少可信 Consumer 或缺少 Write Runtime 的 Operation 会进入 Waiting 分类。

不再使用脆弱的 Event Name 特判，也不会标记 Failed。

## 16. Operation Finished Invariant

Completed 前检查：

```text
没有 waiting/approved/consumed/resume_started Approval Resume
没有 prepared/waiting/approved/submitting/outcome_unknown/reconciling Write
Model Request 全部结束
Tool Call 全部闭合
```

普通 Write Approval 已消费且没有独立 Resume Workflow 时不会被误判为阻塞。

## 17. 新增测试

```text
tests/test_approval_aware_recovery.py
```

共 10 项：

- Waiting Approval → wait_for_approval；
-Direct Recovery 不调用工具；
-Approved 缺 Consumer → 明确等待；
-Consumed → resume_approved_write；
-Rejected → Synthetic Denied Result；
-OutcomeUnknown → reconcile_write；
-Waiting Approval 禁止 Dispatch；
-Action Hash 不匹配拒绝；
-Startup Report Waiting Approval；
-Never Tool 宽授权无法绕过 Planner。

全部项目：

```text
Ran 167 tests
OK
```

## 18. 文档和规则更新

已更新：

```text
README.md
AGENTS.md
STATE_MACHINE_IMPLEMENTATION_GUIDE.md
conversation-review.md
```

AI 以后实现 Recovery 时必须从 Operation Approval/Write Snapshot 驱动，不能只扫描未完成 Tool Call。

## 19. 当前边界

单进程 Approval/Write 感知恢复已经完成。

多进程仍需 P2 事务 Store，确保：

```text
Approval Consume
Write Claim
Tool Dispatch
Entity Version
```

在一个事务中 Compare-And-Set。

> **现在 Approval/Write 不再只是外围特殊判断，而是 OperationState 的正式组成部分；Recovery Planner 在任何入口下都会先尊重 waiting/approved/consumed/outcome_unknown，再决定是否允许工具恢复。**

---

## 对话 69：审查 Model Recovery 策略丢失与 JSONL 并发问题

### 用户问题

审查以下问题是否存在：

1. `RecoverableModelRuntime` 恢复时固定暴露全部工具并使用 `tool_choice="auto"`，可能丢失 Router/Guard 约束；Approval 完成后的单次模型请求如果再次返回 Tool Call，Host 不执行该调用却直接完成 Operation。
2. JSONL Store 的 read-check-append 不是原子状态转换，并发 Approval Grant 或 Write Execute 可能产生重复事件和重复外部写操作。
3. 第二项是否就是统一事务型 Store 问题，并计划先使用 SQLite 单机实现。

### 助手回答

两个问题均确认存在。

审查发现：

```text
RecoverableModelRuntime
→ 使用 Host 全部工具
→ 固定 tool_choice=auto
→ RecoveryCallbacks 只传 messages
→ Operation 没有持久化 Tool Policy/Expected Arguments
```

Approval 完成后的模型请求如果返回新 Tool Call：

```text
新 Tool Call 不执行
ToolResult 缺失
operation_finished(completed) 已追加
下次 replay_operation() 因 Transcript 未闭合失败
```

JSONL Store 的 `asyncio.Lock` 只保护单次 `load()`/`append()`，不能保护完整状态转换。确定性并发复现得到：

```text
两个 Grant
→ 两条 approval_granted
→ 日志非法

两个 Write Execute
→ 两条 write_submitting
→ 外部 Handler 调用两次
→ 两条 write_succeeded
```

结论：SQLite 单机事务 Store 就是统一事务型 Store 的第一阶段，但必须提供 CAS、唯一约束、批量事务和 Lease Claim，不能只是把 JSONL 行搬进 SQLite。

---

## 对话 70：实现 Model Policy Recovery、Approval 闭合与 SQLite Transaction Store

### 用户问题

先修复请求策略持久化和 Approval 后模型闭合问题，测试通过后直接实现 SQLite 单机事务型 Store，并完成测试。

### 助手回答

请求策略恢复、Approval 最终响应闭合和 SQLite 单机事务 Store 已完成。

## 1. ModelRequestPolicy

新增：

```text
src/pi_agent_loop/model_policy.py
```

每次 Model Request 独立持久化：

```text
visibleToolNames
toolChoice
requiredCapabilities
allowedToolNames
expectedToolArguments
continuationPolicy
```

策略按 Request/Turn 保存，而不是按 Operation 保存。

## 2. Recovery 不再扩大工具权限

`RecoveryAction` 携带原始 `request_policy`，`RecoveryCallbacks.request_model` 接收：

```python
request_model(messages, request_policy)
```

`RecoverableModelRuntime`：

```text
只装配 visibleToolNames
恢复原始 tool_choice
恢复 requiredCapabilities
恢复 allowedToolNames
恢复 expectedToolArguments
再次校验模型 Tool Call
```

策略缺失时：

```text
manual_intervention
```

禁止回退到全部工具和 `auto`。

## 3. Continuation Policy

Required/Named Tool 调用前同时保存 Tool 完成后的下一轮策略。

例如：

```text
初始：
visible=[get_order_status]
tool_choice=required
expected={order_id: 1001}

完成后：
visible=[get_order_status]
tool_choice=auto
expected={}
```

崩溃发生在 Tool Dispatch/Result 窗口时也不会猜测下一轮策略。

## 4. Approval 最终模型请求闭合

Approval Write 完成后的模型请求固定为：

```text
tools=[]
tool_choice=none
```

如果模型仍返回 Tool Call 或没有正常 Stop：

```text
model_request_failed
approval_resume_failed
operation_finished(failed)
```

不会执行新工具，也不会产生带未闭合 Tool Call 的 Completed Operation。

## 5. Finished Event 预验证

Recorder、Host 和 Recovery 写 `operation_finished` 前都会：

```text
重放当前 Operation
→ 应用候选 Finished Event
→ 检查 Model Request
→ 检查 Transcript Closure
→ 检查 Approval/Write 终态
→ expected_last_sequence CAS 提交
```

## 6. SQLite 单机事务 Store

新增：

```text
src/pi_agent_loop/session/sqlite.py
SQLiteOperationEventStore
SQLiteRuntimeEventStore
```

`DurableAgentHost` 默认：

```python
store_backend="sqlite"
```

状态文件：

```text
<state_dir>/agent-state.sqlite3
```

JSONL 通过 `store_backend="jsonl"` 保留为单实例兼容模式。

## 7. SQLite 事务能力

实现：

```text
WAL
synchronous=FULL
busy_timeout
BEGIN IMMEDIATE
append_batch()
expected_last_sequence CAS
唯一 Idempotency Key Hash
持久 Lease Claim
```

唯一约束覆盖：

```text
Approval Request ID
Write ID
Write Idempotency Key Hash
Event Sequence
Claim Type + Resource ID
```

## 8. Approval/Write 原子转换

Approval Grant/Reject/Consume、Write Prepare/Claim/Finalize 全部使用重新读取 + CAS。

以下事件可以同一事务提交：

```text
approval_consumed
write_approved
tool_dispatch_started
write_submitting
```

`approval_requested` 与 `approval_resume_registered` 也在同一批事务提交。

外部 Handler 不放入长 SQLite 事务：

```text
先提交 Write Claim
→ 唯一 Worker 调用外部 Handler
→ 使用 Idempotency Key
→ 写 succeeded/failed/outcome_unknown
→ 必要时 Reconciliation
```

## 9. 跨进程 Claim

SQLite Lease 防止同机多个 Worker 同时执行：

```text
approval_resume
operation_recovery
```

同一 Approval Resume 的两个 Coordinator 只有一个可以执行 Callback。

## 10. 安全文件规则

新增 Git Ignore：

```text
*.sqlite3
*.sqlite3-wal
*.sqlite3-shm
```

避免本地业务状态和 WAL 被提交。

## 11. 测试

新增/扩展测试覆盖：

```text
Model Request Policy 每轮持久化
Required → Continuation 策略切换
缺少 Policy → Manual Intervention
Approval 后 Tool Call 拒绝且日志可重放
SQLite Operation/Runtime 重启持久化
两个 Store 并发 Grant 只有一个成功
两个 Store 并发 Write 只调用一次外部 Handler
跨 Operation Idempotency 唯一去重
Approval Consume/Write Claim/Tool Dispatch 批量事务
两个 Coordinator Approval Resume 互斥
跨 Store Lease Claim
SQLite/WAL Git Ignore
```

全部测试：

```text
Ran 178 tests
OK
```

同时通过：

```text
python -m compileall

git diff --check
```

## 12. 当前边界

SQLite 当前保证单机多进程事务和 Claim。跨机器部署仍需要 PostgreSQL 等网络事务数据库；外部副作用仍必须由业务系统提供幂等键和 Outcome Reconciliation，SQLite 不能把外部 HTTP/API 写操作变成真正的分布式 Exactly Once。

> **恢复现在会严格使用原始 Model Request Policy；Approval 后不会留下未闭合 Tool Call；SQLite Store 则通过 CAS、唯一约束、事务批次和 Lease Claim 阻止同机并发 Grant、Consume、Write Claim 和 Recovery 重复执行。**

---

## 对话 71：一次性修复 Durable P0 崩溃窗口

### 用户问题

在前一轮修复后继续发现 8 个 P0 和 3 个高风险点：

```text
Approval 创建前 Tool Call 已落盘
Approval Action 与 Resume Payload 未绑定
外部 Task.cancel() 不收尾
Approved Write 仍规划 Waiting
Resume 重复 Tool Intent
Reconciliation 异常后永久 reconciling
缺 Policy 继承旧 Active Policy
Approval 转换可追加到终态且 TTL 非事务
Listener 在副作用后失败会伪造未执行
普通 Recovery 接受 length
Expected Arguments 可跨调用拼凑并允许额外参数
```

要求一次性修复全部 P0。

### 助手回答

全部列出问题已按统一持久命令和事务边界完成修复。

## 1. DurableActionEnvelope

新增：

```text
src/pi_agent_loop/durable_action.py
```

统一绑定：

```text
operationId
toolCallId
toolName
exact arguments
writeId
```

同一 Envelope 用于 Approval Action Hash、Resume Payload、Operation Reducer、Write Action Hash、Tool Intent 和实际执行校验。

## 2. Approval 初始原子事务

Host 不再先写 Tool Call、后创建 Approval。初始 Prompt 必须提供 Idempotency Key，然后在一个 Store Batch 中提交：

```text
User Message
Model Request Started/Completed
Continuation Policy
Approval Requested/Resume Registered
Write Prepared/Waiting Approval
Tool Intent Recorded
```

因此崩溃时只能看到全部事实或完全看不到 Tool Call。`waiting_approval` 却没有 Approval/Write 的旧损坏状态会进入 Manual Intervention。

JSONL 不保证崩溃批次原子性，因此 Approval 写请求只允许使用支持原子事务的 Store；默认 SQLite。

## 3. Resume Payload 与执行绑定

`ApprovalResumeCoordinator` 会强制解析并比较：

```text
Approval Action Envelope
==
Resume Payload Envelope
```

Host 再比较：

```text
Envelope
==
Write ID/Tool Call ID/Tool Name/Arguments/Action Hash/Approval ID
```

审批 `read_order` 后不能通过独立 Payload 执行 `refund_order`。

## 4. 外部 Task Cancel 收尾

`Agent._run_with_lifecycle()` 现在捕获外部 `asyncio.CancelledError`，先完成：

```text
嵌套 Tool Task 取消
Synthetic ToolResult
Transcript Closure
Durable Operation Cancelled
```

然后重新向调用方抛出 CancelledError。

## 5. Approval/Write 联合 Planner

Write 为 `waiting_approval` 时联合检查关联 Approval：

```text
waiting  → wait_for_approval
approved → consume_approval
consumed → resume_approved_write
rejected/expired → finalize_rejected_approval
missing → manual_intervention
```

Startup Report 新增 `ready_to_resume`，已批准操作不再显示为等待经理审批。

## 6. Tool Intent 和结果幂等

Tool Intent 已在初始事务中创建，Resume 不再重复追加。

Write 结果物化使用 CAS：

```text
Tool Completed 已存在 → 复用
ToolResult Message 已存在 → 复用
缺少其中一个 → 只补缺失项
```

Recorder 在 ToolResult Message Commit 时还会补齐瞬时失败的 Tool Completed Event。

## 7. Reconciliation 可重入

新增 `write_reconcile` Lease Claim 和事件：

```text
write_reconcile_failed
```

规则：

```text
Handler Exception
→ reconciling → outcome_unknown
→ 下次可以重新核对

进程在 reconciling 崩溃
→ Lease 获胜 Worker 可重入
```

## 8. Request Policy 不继承

`model_request_started` 缺少 `requestPolicy` 时：

```text
request.policy=None
active_model_policy=None
Planner → manual_intervention
```

不会继承旧的全部工具/Auto 策略。

## 9. Approval 完整 Reducer 与事务 TTL

Approval Request/Grant/Reject/Consume/Expire 全部先通过完整 Operation Reducer。

终态 Operation 后追加 Grant 会在落盘前拒绝。

Grant/Reject 使用 `deadline_ms`，SQLite 在 `BEGIN IMMEDIATE` 事务中最终检查 TTL；过期后只能写 Expired，不能写 Granted。

## 10. Tool Commit Boundary

`tool_execution_end` 现在是副作用 Commit Boundary。Listener 在副作用完成后抛异常：

```text
只记录 listener_errors
不取消已提交兄弟工具
不触发“工具未执行”Synthetic Result
继续写真实 ToolResult
```

如果 Durable Recorder 的 Tool End 写入瞬时失败，ToolResult Message 阶段会补齐 Tool Event。

## 11. Length 和 Expected Arguments

普通 Recoverable Model Runtime 现在拒绝：

```text
stopReason=length
```

不会把截断回答完成化。

Expected Arguments 现在要求：

```text
恰好一个 Tool Call
Arguments 与快照完整相等
拒绝额外参数
拒绝多个 Tool Call 拼凑字段
```

Router Guard 和 Recovery Runtime 使用相同约束。

## 12. 测试

新增：

```text
tests/test_p0_durable_boundaries.py
```

覆盖全部 11 类问题，包括原子初始事务、Envelope 篡改、外部 Task Cancel、Approved Write Recovery、Reconciliation 重试、旧 Policy 污染、终态 Approval、事务 TTL、Listener Commit Boundary、Length 和精确参数。

全部项目：

```text
Ran 189 tests
OK
```

同时通过：

```text
python -m compileall
git diff --check
examples/durable_host_usage.py
```

> **Durable 写请求现在由统一 Envelope 驱动，并从初始 Tool Call 到 Approval、Write Claim、实际副作用、Tool Result 和 Recovery 使用同一持久身份；所有列出的崩溃窗口都有对应的拒绝规则或确定性测试。**
