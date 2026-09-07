# DeepSeek Harness 源码研究

> 仓库：<https://github.com/deepseek-ai/deepseek-harness>  
> 本地源码：`deepseek-harness/`  
> 研究版本：`dsh-v0.1.1-rc.2`  
> 提交：`b150a551b8d465e31e418e1b2eaf5e79bbb7d28e`  
> 提交时间：2026-08-21 20:03:37 +08:00  
> 研究方式：全仓包级源码静态研究；已覆盖 227 个包及核心实现链路，尚未安装依赖、构建或运行测试。

## 1. 项目定位

DeepSeek Harness（`dsh`）不是大模型实现，而是一个用于组装、运行和扩展编码 Agent 的 Harness。它负责：

- 选择并调用 LLM 提供方；
- 组装系统提示词和工具 schema；
- 驱动多轮 Agent Loop；
- 执行、审批和隔离工具；
- 保存并恢复会话；
- 提供 Web、Headless、ACP、JSON-RPC 和 SDK 等入口；
- 通过插件替换模型、工具、存储、沙箱、子 Agent 和 UI。

项目最核心的架构原则是：**一切皆插件（Everything is a plugin）**。

## 2. 仓库规模与技术栈

静态统计结果：

- 约 7,903 个文件；
- 227 个 `packages/*/*` workspace 包；
- 2 个应用包：CLI 与 Web；
- TypeScript 文件约 2,472 个，约 546,111 行；
- TSX 文件约 262 个，约 70,234 行；
- 测试文件约 1,012 个；
- Markdown 文件约 2,506 个。

主要技术：

- TypeScript 6、严格类型检查；
- Node.js `^22.19.0 || >=24.0.0`；
- ESM；
- pnpm workspace；
- Cordis 插件框架；
- React 18、Vite；
- Vitest、Playwright；
- JSON-RPC、SSE、ACP；
- Python SDK；
- JSONL/Zstandard 与 SQLite 持久化。

根配置：

- `deepseek-harness/package.json`
- `deepseek-harness/pnpm-workspace.yaml`
- `deepseek-harness/tsconfig.base.json`

## 3. 总体架构

### 3.1 Cordis

Cordis 是项目底层插件框架，源码以 vendor 方式放在 `deepseek-harness/vendor/cordis`。

五个关键概念：

1. **Plugin**：函数插件或 `Service` 子类；
2. **Context**：服务容器，例如 `ctx.llm`、`ctx.tools`；
3. **inject**：声明插件启动所需的服务；
4. **Event**：插件之间的类型化通信机制；
5. **Effect**：可逆副作用，插件卸载时自动撤销。

事件模式：

| 模式 | 作用 |
| --- | --- |
| `emit` | 同步通知监听者 |
| `waterfall` | 环绕中间件，可委托或短路 |
| `parallel` | 并行等待所有监听者 |
| `serial` | 按顺序等待监听者 |

`waterfall` 监听器必须调用 `next()` 才会继续执行下游；不调用即短路。

参考：

- `deepseek-harness/docs/cordis-primer.zh.md`
- `deepseek-harness/vendor/cordis/src/context.ts`
- `deepseek-harness/vendor/cordis/src/service.ts`
- `deepseek-harness/vendor/cordis/src/fiber.ts`

### 3.2 核心服务

核心主干由以下服务组成：

| 服务 | 包 | 职责 |
| --- | --- | --- |
| `ctx.sessions` | `dsh-session` | 内存事件日志与会话注册表 |
| `ctx.systemPrompt` | `dsh-system-prompt` | 提示词、运行时上下文、工具 schema 组装 |
| `ctx.tools` | `dsh-tools` | 工具注册、策略和执行流水线 |
| `ctx.agents` | `dsh-agent` | 活跃 Agent 注册与创建工厂入口 |
| `ctx.agentLoop` | `dsh-agent-loop` | 默认 Agent 驱动器 |
| `ctx.llm` | `dsh-llm` | LLM 适配器注册与流式调用 |

扩展插件依赖服务定义，而不应依赖具体提供方。例如工具依赖 `dsh-agent` 和 `dsh-tools`，不直接依赖 `dsh-agent-loop`。

## 4. CLI 与插件树启动

### 4.1 CLI 入口

入口文件：`deepseek-harness/apps/cli/src/bin.ts`

执行流程：

```text
process.argv
  -> parseDshArgs()
  -> profile / plugin / dump-config
  -> 动态导入对应运行器
```

关键位置：

- `apps/cli/src/bin.ts:27`：解析参数；
- `apps/cli/src/bin.ts:29`：按模式分发；
- `apps/cli/src/args.ts`：完整参数语法。

CLI 只解析自己拥有的参数。遇到第一个不认识的参数后，其余参数原样交给已启动的应用插件。因此：

```sh
dsh --profile web --port 8080
```

`--profile` 属于启动器，`--port` 属于 Web 应用。

### 4.2 Profile 与 Bundle

一个运行中的 `dsh` 是由多层 patch 叠加形成的 Cordis 插件树：

```text
空的 cordis.yml 根
  + profile 中声明的 bundle patch
  + profile/cordis.patch.yml
  + $DSH_HOME/cordis.patch.yml
  + --patch overlays
  + telemetry 禁用补丁
```

关键实现：

- `apps/cli/src/profile-boot.ts:142`：`composeProfile()`；
- `apps/cli/src/profile-boot.ts:207`：`runProfile()`；
- `apps/cli/src/profile-boot.ts:248`：调用 `boot()`。

主要组合包：

- `packages/bundle/base/cordis.patch.yml`：共享核心；
- `packages/bundle/web-app/cordis.patch.yml`：Web Host 与浏览器插件；
- `packages/bundle/headless/cordis.patch.yml`：一次性任务运行器。

因此 Web 与 Headless 不是两套核心实现，而是同一核心上的不同插件组合。

## 5. Agent 的创建与所有权

### 5.1 AgentRegistry

源码：`deepseek-harness/packages/core/agent/src/index.ts`

`AgentRegistry` 提供：

- 活跃 Agent 注册表；
- `create()` / `resume()`；
- `AgentFactory` 注册位；
- 运行时父子所有权；
- 基于 `AsyncLocalStorage` 的发起 Agent 传播。

具体 Agent 实现不由注册表创建。`dsh-agent-loop` 将自己注册为 `AgentFactory`，因此调用方只依赖 `ctx.agents`。

### 5.2 AgentHandle

编程式创建返回：

```ts
interface AgentHandle {
  agent: Agent
  dispose(): Promise<void>
}
```

`dispose()` 的含义不只是删除 Map 项，而是：

1. 中止 Agent；
2. 等待 Agent 完全空闲；
3. 释放 Agent 作用域；
4. 从 Agent 注册表移除；
5. 从 Session 注册表移除；
6. 撤销创建过程中安装的所有插件副作用。

Agent 与 Session 使用同一个 `SessionId`。

### 5.3 Agent 的公开操作

主要操作：

- `followup()`：进入下一普通轮次并唤醒；
- `steer()`：进入最近的下一步骤边界并唤醒；
- `inject()`：注入下一步骤上下文，但不唤醒空闲 Agent；
- `cancel()`：取消当前活动；
- `whenIdle()`：等待整个 Agent 活动收敛到空闲；
- `runMaintenance()`：在真实空闲阶段执行非轮次维护任务。

## 6. Inbox

源码：`deepseek-harness/packages/core/agent/src/inbox.ts`

Inbox 有两个有序队列：

```ts
type InboxTarget = 'next-turn' | 'next-step'
```

- `next-turn`：每条普通消息独占一个轮次；
- `next-step`：steering、注入上下文和工具产生的附加上下文。

Inbox 不是纯内存队列。每次插入、删除、替换和领取都会写入：

```text
agent/inbox/spliced
```

因此进程恢复后可以从 Session 日志重建待处理工作。

领取首步骤时，Loop 会取出：

```text
全部 next-step + 一条 next-turn
```

后续步骤只领取 `next-step`。

## 7. Agent Loop

核心实现：

- `packages/core/agent-loop/src/agent.ts`
- `packages/core/agent-loop/src/tool-calls.ts`
- `packages/core/agent-loop/src/index.ts`

### 7.1 驱动状态

`ReactLoopAgent` 内部状态：

```text
idle
maintenance
running(turn, step, abort, wakeRequested)
```

对外状态只有：

```text
idle | running
```

`maintenance` 对外仍显示为 `idle`，但会阻止另一项维护或轮次同时占用 Agent。

### 7.2 完整轮次

```text
Agent.followup(message)
  -> Inbox 持久插入
  -> wakeDriver()
  -> turn/start
  -> Inbox.claim()
  -> systemPrompt.assemble()
  -> agent/pre-step
  -> step/start
  -> user/message
  -> Session.deriveMessages()
  -> agent/request
  -> LLM stream
  -> assistant/chunk*
  -> assistant/message
  -> tool/call*
  -> 工具执行
  -> tool/result*
  -> 如有工具结果或 steering，进入下一 step
  -> agent/turn-stopping
  -> turn/end
```

关键位置：

- `agent.ts:64`：`ReactLoopAgent`；
- `agent.ts:246`：`turn()`；
- `agent.ts:332`：`step()`；
- `agent.ts:426`：`buildRequest()`。

### 7.3 Pre-step

`agent/pre-step` 是请求推导前的权威拦截点：

```ts
type PreStepDecision =
  | { kind: 'reject' }
  | { kind: 'enter'; messages: UserMessage[] }
```

监听器可以：

- 拒绝该步骤；
- 保留消息；
- 替换消息；
- 添加已记录的上下文。

首次步骤被拒绝或改写为空时，仍然会记录一个没有 step 的持久轮次。

### 7.4 请求重建

`buildRequest()` 不从某个可变消息数组构建请求，而是读取：

- Session 当前 surface 派生出的消息；
- 当前组装的系统提示词；
- 当前工具 schema；
- 最新 `request/header`；
- LLM 适配器对精确模型解析出的默认值。

最终生效的请求头会写入 `request/header`，路由容量写入 `request/context`。

## 8. Session 事件溯源

核心源码：

- `packages/core/session/src/index.ts`
- `packages/core/session/src/types.ts`
- `packages/core/session/src/surface.ts`

### 8.1 唯一真源

`Session` 是仅追加的 `SessionEvent[]`。项目不维护另一份权威聊天历史。

关键原则：

> 模型可见即已记录。任何进入模型请求的内容，都必须能够从 Session 日志重建。

核心事件包括：

- `turn/start` / `turn/end`；
- `step/start` / `step/end`；
- `user/message`；
- `assistant/chunk`；
- `assistant/message`；
- `tool/call` / `tool/result`；
- `request/header`；
- `request/context`；
- `session/end-seed`。

插件可以通过 TypeScript declaration merging 扩展 `SessionEventMap`。

### 8.2 append 边界

`Session.append()` 位于 `packages/core/session/src/index.ts:604`。

写入前会：

1. 将数据复制为无损 JSON；
2. 拒绝 `BigInt`、函数、循环对象、稀疏数组、Date/Map/Set 等；
3. 验证 surface 元数据；
4. 深冻结事件；
5. 校验不能重入 append；
6. 原子加入日志；
7. 同步通知观察者，但隔离观察者异常。

事件序号始终满足：

```text
seq === log.length（写入前）
```

### 8.3 Surface

并非每个事件都会进入模型历史。只有三类事件能进入 surface：

- `user/message`；
- `assistant/message`；
- `tool/result`。

Surface 操作：

```ts
type SurfaceOp =
  | 'append'
  | { op: 'replace'; start: number; end: number }
```

`replace` 不删除原始事件，而是在模型视图中遮蔽一段旧 surface。这样既能压缩模型上下文，也能保留完整审计和回放数据。

### 8.4 deriveMessages

`Session.deriveMessages()` 位于 `packages/core/session/src/index.ts:726`。

规则：

- `user/message` 原样成为 user message；
- 非空 `assistant/message` 成为 assistant message；
- `tool/result` 成为携带 tool-result block 的 user message；
- raw chunk、边界事件和日志控制事件不进入模型历史；
- surface 替换后重建缓存，否则只增量投影新节点。

## 9. System Prompt

源码：`deepseek-harness/packages/core/system-prompt/src/index.ts`

`SystemPrompt` 管理：

- 静态或动态 section；
- 动态 runtime context；
- 工具 schema provider；
- `{{variable}}` 插值；
- 全局与 Agent 作用域覆盖；
- `system-prompt/assemble` waterfall。

组装顺序：

1. 读取全局层；
2. 按作用域链合并；
3. 最近作用域覆盖同名 section/variable；
4. 按 `order` 排序；
5. 收集并排序工具 schema；
6. 执行 waterfall；
7. 若存在 `complete` section，恢复为唯一系统提示词。

默认包括 Harness 身份 section 与部署 persona。

## 10. 工具系统

核心源码：`deepseek-harness/packages/core/tools/src/index.ts`

### 10.1 ToolDefinition

工具同时定义：

- 模型可见 schema；
- 规范 JSON 输出 schema；
- `execute()`；
- 规范输出到模型内容的 `render()`；
- 可选并发分类；
- 可选超时；
- 可选 UI 展示投影。

推荐通过 `defineTool()` 创建，参数类型和返回类型由 schema 推导。

### 10.2 执行流水线

```text
tools/pre-execute
  -> approval ask/deny/allow
  -> 单调 ToolGuard
  -> tools/execute around waterfall
  -> ToolDefinition.execute()
  -> tools/post-execute
  -> ToolDefinition.finalizeContent
  -> tools/result 通知
  -> Session tool/result
```

特点：

- 参数在策略执行前复制并冻结；
- 调用具有不可伪造的 Symbol token；
- guard 只能拒绝，不能强制允许；
- 未知工具和工具异常被转换为结构化结果，不直接破坏整个 Loop；
- 结果在写入日志前再次物化为无损 JSON；
- UI 卡片数据与模型结果分离。

### 10.3 并发工具调度

源码：`packages/core/agent-loop/src/tool-calls.ts`

工具分类：

```text
parallel | exclusive
```

调度策略：

- exclusive 工具形成屏障；
- parallel 工具进入有界滚动池；
- 默认最大并发数由 `maxParallelToolCalls` 控制；
- 分发可以重叠，但 pre/post 策略、结果提交和附加上下文保持模型顺序；
- 取消后，未启动工具会获得合成错误结果，保证会话回放仍然配对完整。

### 10.4 Code Mode

在 `code` 模式下，模型直接可见的工具折叠为 `run_code`。原始工具被生成为代码 SDK：

```text
await tools.<toolName>(args)
```

SDK 子调用仍重新进入完整工具策略流水线，并不是绕过权限直接调用函数。

## 11. LLM 抽象层

源码：`deepseek-harness/packages/llm/llm/src/index.ts`

### 11.1 LlmRuntime

`LlmRuntime` 负责：

- 注册 provider route 到 adapter；
- 防止重复 route；
- 模型目录；
- 精确模型能力解析；
- 默认 maxTokens 和 reasoning effort；
- retry policy；
- `llm/stream` waterfall；
- 适配器异常到统一 `finish` chunk 的转换。

### 11.2 PreparedLlmCall

`prepareCall()` 位于 `packages/llm/llm/src/index.ts:824`。

它把以下内容绑定到同一个适配器注册代次：

- 解析后的请求配置；
- 上下文窗口；
- 模态能力；
- reasoning 能力；
- retry policy；
- 最终 stream 调用入口。

这防止 HMR 或动态配置变化造成“使用旧适配器能力组装请求，却交给新适配器发送”的竞态。

Prepared call 只能执行一次，且执行前会验证调用配置没有被修改。

### 11.3 StreamChunk

适配器输出统一流协议：

- `block-start`；
- 文本/reasoning/tool-call delta；
- `block-end`；
- `usage`；
- `finish`。

`BlockAssembler` 将其组装为最终 ContentBlock，同时处理：

- 多块交错；
- 中断时仅保留安全的文本和 reasoning；
- max-token 时丢弃不完整工具调用；
- replay state 与实际保留块同步裁剪。

## 12. DeepSeek 适配器

源码：

- `packages/llm/llm-deepseek/src/index.ts`
- `packages/llm/llm-deepseek/src/adapter.ts`
- `packages/llm/llm-deepseek/src/serialize.ts`
- `packages/llm/llm-deepseek/src/sse.ts`
- `packages/llm/llm-deepseek/src/translate.ts`

### 12.1 Provider

该插件注册：

```text
deepseek-official
```

当前默认模型目录：

- `deepseek-v4-flash`；
- `deepseek-v4-pro`；
- `deepseek-v4-flash-vision-exp`。

模型目录只是建议，不是路由白名单。未列出的模型 ID 仍会原样传给服务端，但默认按纯文本模型处理。

### 12.2 请求过程

```text
DeepSeekAdapter.stream()
  -> 冻结本次 connection/settings 快照
  -> 解析 API key
  -> 检查图片能力
  -> 准备图片附件
  -> Files API / Base64
  -> POST /chat/completions
  -> parseSse()
  -> translate()
  -> StreamChunk
```

### 12.3 Thinking

Harness reasoning effort：

```text
off | low | high | max
```

映射：

- `off` -> `thinking: { type: 'disabled' }`；
- `low/high/max` -> 启用 thinking，并设置 `reasoning_effort`。

`session-title` 辅助请求会强制关闭 thinking。

### 12.4 图片

视觉模型通常通过 DeepSeek Files API 获得图片引用；文件解析失败时，整个请求切换到 Base64，不混合两种表示。

图片预算控制：

- 总请求图片字节；
- Base64 回退字节；
- 图片数量；
- 单图像素；
- 单图编码字节。

超预算时优先移除最旧图片，并插入稳定的模型可见占位文本，避免每新增一张图片都改变大量缓存前缀。

### 12.5 SSE 与错误

`parseSse()` 要求 `[DONE]`，流在没有结束标记时返回 `STREAM_CLOSED`。

`translate()` 将 DeepSeek 协议转换为 Harness block，并保证：

- usage 位于 finish 之前；
- finish 之后无数据；
- 工具参数保持原始 JSON 字符串；
- 空成功响应转换为 `EMPTY_RESPONSE`；
- `length` 转换为 `max-tokens`。

HTTP 错误归一化为：

- `AUTH`；
- `QUOTA`；
- `RATE_LIMIT`；
- `CONTEXT_WINDOW_EXCEEDED`；
- `INVALID_REQUEST`；
- `SERVER`；
- `HTTP_<status>`。

## 13. 持久化

### 13.1 抽象 seam

服务定义：`packages/session/session-persistence/src/index.ts`

后端：

- JSONL/Zstandard；
- SQLite。

核心接口包括：

- `create()`；
- `append()`；
- `prepare()`；
- `load()`；
- `inspect()`；
- `readFrom()`；
- `list()`；
- `listSnapshots()`。

### 13.2 异步写入

`session/event` 是同步通知，但持久化不会阻塞 Agent 热路径。事件先进入逐会话写队列，然后按固定窗口批量写入。

显式 `flush()` 会：

- 取消等待窗口；
- 排空所有待写事件；
- 等待后端完全稳定；
- 暴露持久化错误。

### 13.3 崩溃恢复

如果日志末尾存在已经开始但没有结束的轮次，恢复不会删除已写事件，而会合成缺失边界：

```text
turn/end { kind: 'interrupted' }
```

撕裂的最后物理记录可以被丢弃，但完整写入的事件会保留。

### 13.4 JSONL

源码：`packages/session/session-persistence-jsonl/src/index.ts`

特点：

- 每个 Session 独立工件；
- 默认 Zstandard frame；
- header 使用独立 frame；
- chunk 连续段可打包；
- 首次写入采用临时文件、fsync 和原子发布；
- POSIX 使用 `link()` 避免覆盖已有同 ID 日志；
- append 失败会回滚文件长度；
- 支持原始工件导出。

## 14. Headless、Web 与 SDK

### 14.1 Headless

源码：`packages/bundle/headless/src/index.ts`

流程：

1. 等待 Loader 完成；
2. 读取默认模型；
3. 创建新 Agent；
4. 提交普通用户消息；
5. 等待 `whenIdle()`；
6. flush Session；
7. 找到最后一条非空 assistant 文本；
8. 输出 stdout；
9. 根据 `turn/end` 请求进程退出。

它不启动 Host、HTTP server 或浏览器运行时。

### 14.2 Web Host

主要入口：`packages/host/apiproxy/src/api-proxy.ts`

请求链：

```text
React Client
  -> Fetch/SSE RPC
  -> host-apiproxy
  -> Session/Agent Registry
  -> Agent Loop
```

`session.prompt` 会：

- 校验会话和模型可路由性；
- 校验时区；
- 持久化图片附件；
- 创建带 rpcId 的 UserMessage；
- 根据模式调用 `followup()` 或 `steer()`。

### 14.3 浏览器插件

Web UI 本身也是插件树。Host 扫描带 `dsh.client` 声明的包，产生：

```text
window.__DSH_BOOT__
```

浏览器再动态加载 `/plugins/<id>/client.js`，创建客户端 Cordis 树。

Web 应用入口极薄：

- `apps/web/src/main.ts`；
- `packages/client/web/src/boot.ts`；
- `packages/client/runtime`；
- `packages/client/ui-*`。

### 14.4 JSON-RPC 与 Python SDK

服务端：`packages/sdk/server/src/server.ts`

Python 客户端：`python/sdk/src/deepseek_harness/client.py`

SDK 使用 stdio JSON-RPC：

- `initialize`；
- `session/prompt`；
- `shutdown`；
- `session.event` 通知；
- `session.status` 通知；
- subagent 生命周期通知。

Prompt 调用立即返回 `MessageId`，不会把某条后续 assistant 输出强行归属于这条 prompt。调用方通过会话事件自行观察活动区间。

## 15. Agent Preset 与作用域

Web 模式下，模型可见能力从 Host plane 移到每 Session preset。

标准 preset：

`apps/cli/config/agent-presets/standard/agent.cordis.yml`

Preset 以 standing scope 的方式每进程挂载一次；每个 Session Agent 通过作用域父级关系加入该组合。这样：

- 工具、提示词和局部服务可按 Agent 隔离；
- 相同 preset 可被多个 Agent 复用；
- Host 级服务仍保持单例；
- Session 恢复时可以重新使用原来的 preset。

`SessionHeader.agentPreset` 会持久化 preset ID，因为使用不同工具集合恢复旧历史可能导致已记录的工具调用无法继续执行。

## 16. Subagent

服务：`ctx.subagents`

提供方：

- spawn in-process；
- fork in-process；
- ACP；
- Codex；
- Claude Code；
- DSH SDK。

两种模式：

1. **one-shot**：一次委派，一个最终结果；
2. **continuable**：持久子 Session，可在以后继续发送 FIFO 消息。

Continuable 子 Agent 的唯一任务队列仍然是 Agent Inbox，没有第二套队列。

持久信息包括：

- parent Session；
- origin；
- delegation depth；
- provider；
- persona/tool filter；
- continuable 描述符。

## 17. Compaction

Compaction 不是 Agent Loop 内核逻辑，而是监听 `agent/pre-step` 和 `agent/request-error` 的可选插件。

过程：

```text
compaction/start
  -> 选择安全 surface 区域
  -> 生成摘要
  -> compaction/summary
  -> user/message surface replace
  -> compaction/end
```

原始历史不删除。摘要 user message 只替换模型 surface，审计日志仍保留原始事件和摘要生成信息。

工具结果 pruner 可在摘要之前将超大工具结果替换为 head/tail 形式。

## 18. 沙箱与审批

### 18.1 Sandbox 模式

```text
read-only
workspace-write
danger-full-access
```

本地后端：

- Linux：bwrap / Landlock；
- macOS：Seatbelt；
- Windows：ACL 与受限令牌。

受限模式不能静默退化为直接执行。后端必须返回真正的约束 argv，或明确失败。

注意：当前 Sandbox 主要约束**文件系统效果**，不保证网络与进程可见性隔离。

### 18.2 Approval

工具在 `tools/pre-execute` 阶段可以返回 `ask`。只有审批服务明确返回 `allowed-once` 才执行；缺少服务、缺少回答通道、拒绝或取消都会 fail closed。

## 19. 类型设计

### 19.1 可扩展 Map

项目大量使用：

```ts
interface XxxMap { ... }
type Xxx = XxxMap[keyof XxxMap]
```

插件通过 declaration merging 添加事件、来源或结束原因。

主要示例：

- `SessionEventMap`；
- `ContentBlockMap`；
- `MessageSourceMap`；
- `FinishReasonMap`；
- `TurnEndReasonMap`。

### 19.2 Branded ID

跨包 ID 不是裸字符串，而是：

```ts
type Branded<B extends string> = string & Brand<B>
```

例如：

- `SessionId`；
- `MessageId`；
- `CallId`；
- `JobId`。

这可以在编译期阻止把不同类型 ID 传错位置。

## 20. 工程质量

项目具有较强的机械约束：

- strict TypeScript；
- 源码包逐文件 100% coverage 目标；
- 生成模块依赖图；
- 生成事件生产/消费矩阵；
- 生成工具和配置目录；
- 文档中类型签名与源码做等价校验；
- package invariant 插件；
- snapshot、e2e、Web、Python SDK 多层测试；
- 公共导出 JSDoc 检查；
- NodeNext consumer 检查；
- HMR 和 teardown 专项测试。

## 21. 优势与代价

### 优势

- Agent Loop 与具体能力解耦；
- Session 可重建、可审计、可恢复；
- 工具参数、输出和 UI 展示边界清晰；
- 动态配置与 HMR 考虑适配器代次一致性；
- 取消、并发、dispose 顺序设计深入；
- 同一核心可服务 Web、Headless 和 SDK；
- 扩展点丰富，通常无需修改 Loop。

### 代价与风险

- 227 个包导致理解和改动成本较高；
- Cordis fiber/effect/realm/scope 学习门槛高；
- 生命周期 teardown 顺序具有较多隐含约束；
- 当前是开发者预览版；
- `SESSION_FORMAT_VERSION` 仍为 0，不承诺兼容；
- Preset 和 `!!js` 配置属于受信代码，不能当作不可信数据执行；
- DeepSeek 适配器直接使用 `fetch`，尚无共享 HTTP/proxy 配置层；
- Sandbox 不覆盖网络隔离；
- Telemetry 默认关闭，但显式开启后会上报原始捕获副本。

## 22. 推荐阅读顺序

1. `deepseek-harness/docs/architecture.zh.md`
2. `deepseek-harness/docs/cordis-primer.zh.md`
3. `deepseek-harness/apps/cli/src/profile-boot.ts`
4. `deepseek-harness/packages/core/agent-loop/src/agent.ts`
5. `deepseek-harness/packages/core/session/src/index.ts`
6. `deepseek-harness/packages/core/session/src/surface.ts`
7. `deepseek-harness/packages/core/tools/src/index.ts`
8. `deepseek-harness/packages/llm/llm/src/index.ts`
9. `deepseek-harness/packages/llm/llm-deepseek/src/adapter.ts`
10. `deepseek-harness/packages/session/session-persistence/src/coordinator.ts`
11. `deepseek-harness/packages/host/apiproxy/src/api-proxy.ts`
12. `deepseek-harness/apps/cli/config/agent-presets/standard/agent.cordis.yml`

---

# 后续静态研究日志

后续每轮新增研究都追加在本节，并注明研究主题、源码路径、关键流程、结论和待验证项。

## 第二轮：生命周期、作用域、持久化协调、重试与压缩

### 研究范围

本轮进一步阅读：

- `deepseek-harness/vendor/cordis/src/context.ts`
- `deepseek-harness/vendor/cordis/src/reflect.ts`
- `deepseek-harness/vendor/cordis/src/fiber.ts`
- `deepseek-harness/vendor/cordis/src/events.ts`
- `deepseek-harness/packages/core/scope/src/index.ts`
- `deepseek-harness/packages/core/scope/src/store.ts`
- `deepseek-harness/packages/boot/app-boot/src/index.ts`
- `deepseek-harness/packages/session/session-persistence/src/coordinator.ts`
- `deepseek-harness/packages/session/session-persistence/src/preparations.ts`
- `deepseek-harness/packages/session/session-persistence/src/write-behind.ts`
- `deepseek-harness/packages/llm/llm-retry/src/index.ts`
- `deepseek-harness/packages/core/agent-loop/src/invariant.ts`
- `deepseek-harness/packages/compaction/compaction-basic/src/index.ts`
- `deepseek-harness/packages/compaction/compaction-basic/src/region.ts`
- `deepseek-harness/packages/compaction/compaction-basic/src/summarizer.ts`
- `deepseek-harness/packages/llm/token-meter/src/index.ts`

### 1. Cordis Fiber 是插件自动启停的核心

Fiber 状态机：

```text
PENDING
  -> LOADING
  -> ACTIVE
  -> UNLOADING
  -> PENDING / LOADING

启动异常：FAILED
最终销毁：DISPOSED
```

每个插件的 `inject` 被解析成依赖实现集合。Fiber 会把当前依赖实现的 fiber uid 拼成一个 **epoch**：

```text
:<service-a-fiber-uid>:<service-b-fiber-uid>
```

当依赖服务出现、消失或被新实现替换时，epoch 改变，Fiber 自动执行：

```text
卸载旧插件副作用
  -> 使用新依赖重新校验配置
  -> 重新执行插件 apply/constructor
```

因此项目的 HMR 和“服务出现后自动启动消费方”不是 Loader 手工排序，而是服务图驱动的 Fiber 重载。

关键实现：

- `vendor/cordis/src/fiber.ts:597`：检查服务实现；
- `fiber.ts:611`：重新计算依赖 epoch；
- `fiber.ts` 中 `_reload()` / `_unload()`：执行生命周期转换；
- `vendor/cordis/src/reflect.ts` 中 `notify()`：服务拓扑变化后刷新依赖 Fiber。

#### 新结论：启动顺序只是依赖图的结果

Bundle YAML 中的行顺序主要用于阅读和 patch 定位。真正的激活条件是：

```text
插件声明的所有 inject 服务是否都在相同 isolate realm 中可用
```

这解释了为什么 base bundle 注释反复强调“行顺序不承载加载语义”。

### 2. Effect 的精确 disposer 身份为何重要

`ctx.effect()` 支持：

- 单个 disposer；
- Promise disposer；
- 同步 generator 依次 yield 多个 disposer；
- async generator。

同一个 effect 内部收集的 disposer 会逆序执行：

```text
yield A
yield B
yield C

teardown: C -> B -> A
```

但 Fiber 顶层拥有的多个 effect 在 `_unload()` 中通过 `Promise.all()` 排空；它们是并行 sibling，不具有彼此的确定顺序。

因此源码中多处强调“返回 exact Cordis disposer，而不是包装函数”。如果把注册表 detach disposer 包一层再返回，它可能不再嵌套于原 composite effect 的正确位置，而变成并行 sibling，造成：

- Agent 尚在写最终 `turn/end`，Session 已被移除；
- Session publication hook 先释放，最后事件没有持久化观察者；
- `agent/disposed` 早于真实 driver quiescence。

这不是代码风格要求，而是生命周期正确性条件。

### 3. 服务注入、可选读取和 isolate realm

`Context` 是 Proxy。普通 `ctx.serviceName` 读取有严格约束：插件没有声明 `inject` 时，直接读取服务会抛出：

```text
cannot get property "..." without inject
```

可选能力使用：

```ts
ctx.get('serviceName')
```

它允许服务不存在，并默认只返回 ACTIVE 提供方。

`ctx.provide()` 将服务实现写入 ReflectService。实现由当前 Fiber 所有；Fiber 卸载时服务自动移除，并通知所有依赖该服务的 Fiber 重新计算 epoch。

#### Isolate 的实际表示

每个服务名在 Context 上对应一个 symbol realm：

```text
service name -> isolate symbol -> implementation
```

`ctx.isolate(name)` 为该服务创建新 symbol。两个 Context 使用相同 symbol 时共享 realm；不同 symbol 时可以同时提供同名服务而不冲突。

这解释了 Agent preset 中的规则：

- `planMode`、`compaction` 等按 preset 隔离的服务必须放在 `isolate` group；
- Host 单例注册表不能放进 preset 私有 realm，否则 Host RPC 无法解析它；
- 仅向已有注册表贡献条目的工具插件不需要 isolate，因为它们不提供同名服务。

### 4. DSH Scope 与 Cordis Isolate 是两套不同机制

本轮确认二者不能混为一谈：

- **Cordis isolate**：决定服务实现解析到哪个 realm；
- **dsh-scope**：决定注册表视图继承和事件路由。

`dsh-scope` 使用 WeakMap 保存：

```text
child ScopeKey -> parent ScopeKey
```

作用域链为：

```text
agent -> standing preset -> 更高层 scope
```

注册表读取方向：子 scope 能看到祖先贡献；越近的同名项越优先。

事件方向：事件从 descendant 向 ancestor 传播；祖先 preset 的 listener 能收到其所有 Agent 的事件，但一个 child listener 不会收到 parent 的事件。

`scopeTarget()` 创建的只是路由 carrier，不暴露真实 Agent/Session 属性。真实对象仍放在事件 payload 中。

#### Scope rebind

只有首次调用 `bindScopeParent()` 得到的私有 binding 才能 `rebind()`。外部代码不能任意移动 scope ancestry。

Agent preset 切换正是利用这个受控 rebind，并由调用方保证 Session 仍为空白。作用域机制本身无法知道历史里是否已出现旧工具调用，因此业务层必须先验证“尚未产生任何对话”。

### 5. Boot 的 fail-loud 行为

`app-boot.boot()` 的执行顺序：

```text
new Context()
  -> 提供 dshHomePath
  -> 挂载 Loader
  -> 执行 launcher prepare
  -> 挂载 root include + patches
  -> 等待 Loader settle
  -> 审计每个 enabled entry
```

最终审计会区分：

- 模块根本未解析：没有 Fiber；
- 插件启动失败：Fiber 为 FAILED，并保留原始异常栈；
- 依赖缺失：Fiber 为 PENDING，并列出等待的服务；
- 正常：ACTIVE。

任何启动阶段失败都会先 dispose 部分插件树，再抛出带阶段名称的错误。

进程级 `unhandledRejection` 保护会将晚到的插件加载失败打印到 stderr 并退出 1。如果应用持有终端，会先尝试释放终端；释放最多等待 2 秒，防止卡死 disposer 把失败退出无限阻塞。

### 6. 持久化协调器的并发模型

`PersistenceCoordinator` 不是简单的事件监听器，而是后端无关的事务协调层。

每个 Session ID 都有独立 Promise chain：

```text
chains: Map<SessionId, Promise>
```

同 ID 的 create、append、load、inspect、repair 和 flush 严格串行；不同 ID 可以并行。某次操作失败不会污染后续链，调用方看到真实 rejection，而链尾会吞掉失败并继续服务下一项操作。

#### 三类状态

```text
states       按 SessionId 保存 durable cursor、header 和 owner
live         按精确 Session 对象保存初始化与 write-behind
retirements  已 dispose Session 的最终 drain
```

使用“精确 Session 对象”而不只使用 ID，可以区分：

- 同一 ID 的旧生命周期尚在退役；
- 新生命周期试图过早接管；
- 两个无关 Session 错误复用同一 ID。

### 7. 写后缓存的固定窗口语义

`SessionWriteBehind` 默认窗口为 200ms。

状态包括：

```text
pending events
active durable write
timer
flush barrier
deadlineExpired
automaticPaused
```

事件到达时会先 `structuredClone()`，持久化队列不借用生产方对象。

窗口是**固定窗口**而不是 debounce：第一条事件启动计时器，后续事件不会重置截止时间。

如果截止时已有写入进行中：

- 标记 `deadlineExpired`；
- 当前写入结束后立即开始下一批。

如果后台写失败：

- 整批事件按原顺序放回 pending 头部；
- 自动重试暂停；
- 错误写日志；
- 新事件会重新打开窗口；
- 显式 flush 会立即重试。

因此后台失败不会丢事件，也不会形成失控重试循环。

#### Flush barrier

多个并发 `flush()` 共享同一个 barrier。它会：

1. 取消计时器；
2. 等待正在写入的批次；
3. 逐批排空 pending；
4. 在确认队列为空的同一 job 内关闭 barrier；
5. 再 resolve 调用者。

这样新事件不会被困在已经 settle 的 barrier 后面。

### 8. Lazy materialization 与 ID 冲突防护

新 Session 的 `create()` 只登记 metadata，不立即创建物理工件。第一次 append 才原子写入 header + 首批事件。

结果：创建后从未产生事件的空 Session 不会留下磁盘垃圾。

持久化接管 live Session 时会校验：

- ID；
- cwd；
- seed 是否完整覆盖磁盘 prefix；
- durable cursor；
- 是否已有另一个精确 Session owner。

同 ID 但 cwd 不同，或 seed 与存储前缀不同，会明确按 collision 拒绝，不会将两个会话拼接到同一个日志。

### 9. Cold prepare 的共享读取与独占发布

`SessionPreparations` 状态机：

```text
loading -> ready -> committing -> reserved
                    ^              |
                    |---- release--|
```

作用：

- 同 ID 的多个 inspect 共享一次冷读取；
- ready 项进入有界 LRU，默认容量 5；
- resume 前必须取得独占 reservation；
- committing/reserved 阶段拒绝 append；
- 发布时必须是 preparation 中的**精确 Session 对象**，别名对象不能冒充；
- revision 变化会使缓存失效并重新读取。

调用方取消只取消自己的等待观察，不取消共享底层读取。只有操作还没跨过开始边界时，队列观察者才会立即收到取消。

### 10. 冷恢复与 HMR 接管的关键区别

冷恢复：

- 读取完整持久 prefix；
- 删除撕裂的物理尾部；
- 对完整但未闭合的轮次生成 interrupted closers；
- 提交 repair；
- 构造可发布 Session。

HMR/live 接管：

- live Session 仍是权威；
- 只允许截断撕裂物理尾部；
- **不能**把 open turn 修复成 interrupted；
- 验证持久 prefix 是 live seed 的前缀；
- 写入 live Session 领先于磁盘的 suffix。

否则 HMR 时可能先伪造 `turn/end(interrupted)`，旧 driver 随后又写入真实 `step/end/turn/end`，造成日志结构冲突。

### 11. 有限 legacy 归一化不等于格式兼容承诺

虽然 `SESSION_FORMAT_VERSION` 仍为 0，协调器内部仍保留若干旧形状归一化：

- 旧 steering event 转换为 `user/message`；
- 旧消息补稳定 legacy MessageId；
- 旧 turn trigger 删除；
- 旧 aborted/disposed/error 结果转成当前 envelope。

同时它明确拒绝：

- `request/header-delta`；
- `mode/set`；
- `request/header` 的 `fallback` reason；
- 未知且没有 `ignorable: true` 的事件。

这些转换用于读取仓库演进过程中有限的历史形状，不构成公开迁移保证。

另一个值得注意的取舍：append 热路径只拒绝明确已淘汰的形状，但对当前构建未知的扩展事件不立即拒绝；未知 required event 会在下次 load 时拒绝。原因是 append 时拒绝会让 live Session 的持久化在中途永久停住。

### 12. LLM Retry 是持久恢复策略，不是适配器内部重试

源码：`packages/llm/llm-retry/src/index.ts`

Retry 插件监听：

```text
agent/request-error
```

Retry policy 归 provider 注册所有，而不是归 retry 插件配置所有。插件自身配置必须为空；把 `retryPolicy` 错写到它下面会直接报错。

#### Normal policy

- 只处理 `retryableCodes`；
- 受 `maxRetries` 限制；
- 达到上限后调用 `next()`，保留原始错误。

#### Always policy

- 先允许下游恢复插件处理；
- 下游已返回 retry 时直接采用；
- 下游抛错时记录 warning，然后继续自己的无限恢复策略；
- 不设 retry 次数上限。

#### Backoff

```text
min(initialDelay * 2^(retry-1), maxDelay)
  * [1-jitterRatio, 1+jitterRatio]
```

最终仍不超过 `maxDelayMs`。

有效的 provider `Retry-After` 优先：

- 小于等于 maxDelay：直接采用；
- 大于 maxDelay：normal 放弃重试；
- 大于 maxDelay：always 改用本地 backoff。

#### 重试先落账，再等待

每次重试顺序：

```text
append llm/retry
  -> cancellable delay
  -> append llm/retry-started
  -> return { kind: 'retry' }
```

`llm/retry` 保存：

- provider；
- policyKey；
- retry 序号；
- delay；
- 结构化失败；
- normal 模式的 maxRetries。

同一 turn/step/provider/policy 的连续重试共享 RetryId。UI 和恢复逻辑可以区分“已计划但尚未开始”和“等待完成、即将再次请求”。

插件卸载时会：

1. 先移除 listener；
2. abort 所有等待；
3. 等待 active recovery 全部 settle。

### 13. 请求可重建性有运行时不变量

`packages/core/agent-loop/src/invariant.ts` 在 `llm/stream` 前 prepend 一个全局检查。

对每个标记为 Agent Loop 构造的请求，它验证：

- 请求对象已冻结；
- messages 数组已冻结；
- 带 live SessionId；
- Session 已有 step/start；
- Session 已有 request/header；
- 请求 messages 与 `session.deriveMessages()` JSON 完全一致；
- model/system/temperature/maxTokens/stop/tools 与折叠后的 header 一致。

检查使用 `prepend: true`，防止下游 replay listener 短路 `llm/stream` 后跳过验证。

这说明“模型可见即已记录”不只是一条文档原则，而是可在运行时启用的断言。

### 14. Compaction 的自动接入点

`compaction-basic` 不修改 Agent Loop，而是注册：

```text
agent/pre-step       上下文压力检查
agent/request-error  CONTEXT_WINDOW_EXCEEDED 恢复
agent/status         清除 overflow retry 计数
session/event        成功 assistant message 后重置恢复序列
```

压力模式：

1. 从最新 durable request header 取得 provider/model；
2. 通过 LLM adapter 解析 context window；
3. TokenMeter 测量当前请求压力；
4. 超过阈值后先执行可选的 model-free 工具结果剪枝；
5. 重新测量；
6. 选择保留尾部之外的平衡范围；
7. 生成摘要并替换 surface；
8. 若仍超过阈值，在配置上限内继续压缩。

上下文溢出模式跳过普通阈值与保留策略，强制寻找一个有用的安全缩减范围。

### 15. Overflow Recovery 用 surface generation 证明进展

请求因 `CONTEXT_WINDOW_EXCEEDED` 失败时，恢复插件记录压缩前：

```text
session.surface.replaceGeneration
```

只有 generation 真正增加，才返回 `{ kind: 'retry' }`。

即使 model-free pruner 已完成替换、后续 LLM 摘要却失败，只要：

- signal 未取消；
- replaceGeneration 已前进；

仍会重试原请求，因为 durable surface 已经变小。

如果没有任何 surface 变化，则调用 `next()`，保留原始上下文溢出错误。这样避免“恢复插件声称已修复，但请求输入实际上没变化”的无限重试。

### 16. Compaction 事务与锁

Compaction 的持久事务：

```text
validate selection
  -> append compaction/start
  -> 生成摘要（异步）
  -> 再验证 surface 稳定
  -> append compaction/summary
  -> append replacement user/message
  -> append compaction/end
```

`compaction/start` 与初始验证同步相邻，在第一次 await 前成为持久锁。

后续失败会尝试写入恰好一个带 error 的 `compaction/end`。如果连 close append 都失败，则保留 unmatched start，下一次操作可以检测到未关闭锁，而不会误认为事务成功。

恢复后的 `session/end-seed` 如果晚于 unmatched start，说明旧锁属于已经结束的上一个进程生命周期，可以忽略。

#### 自动与手动稳定性不同

- 自动压缩要求整个 surface 在摘要期间不变化；
- 手动压缩作为 maintenance 运行，只要求选定 span 保持存在、连续、价格一致；span 外新增的独立上下文不会使操作失败。

手动压缩还会在 marker pair 闭合后执行 Session flush，向调用者提供持久性检查点。

### 17. 摘要请求专门为 KV Cache 复用设计

摘要请求不是使用全新的 summarizer system prompt。它重放被压缩范围对应的：

```text
原 system prompt
原 tool schemas
原 surface messages
```

然后只在尾部追加 compaction instruction。

这样请求前缀尽量与最近一次会话请求一致，提供方可以复用已有 KV cache。

摘要输出必须：

- 成功结束；
- 不能 max-token 截断；
- 不能包含图片；
- 至少包含非空文本；
- 加上 checkpoint framing 后，估算 token 必须小于被遮蔽内容。

否则不允许提交 surface replacement。

### 18. TokenMeter 的 usage 与估算混合策略

TokenMeter 按 Session 增量回放：

- request/header；
- surface append/replace；
- step start/end；
- assistant usage；
- compaction shadow price。

当 assistant message 有 provider usage 时，它会重组该消息引用的原始 chunk，估算 provider 实际输出所占 surface token，并建立 usage anchor。

只有 provider usage 总数不小于同一请求 envelope 的完整启发式估算时，才将 usage 作为 baseline；否则使用更保守的估算值。

后续 surface 新增/替换以 signed delta 叠加在 anchor 上。请求 header 改变时，如果不再匹配 anchor，则重新按完整 header + surface 估算。

这避免把一个比本地完整估算还小的 provider usage 当作压力基线，导致过晚触发压缩。

### 本轮综合结论

1. Cordis 的本质不是普通 DI 容器，而是**服务拓扑驱动的可重启 Fiber 系统**。
2. DSH 的大量复杂注释集中在生命周期代码，是因为 sibling effect 默认可并行回收，精确 disposer 嵌套决定事件是否丢失。
3. Cordis isolate 解决“同名服务实例隔离”，dsh-scope 解决“注册表继承和事件路由”，两者职责正交。
4. 持久化层以精确 Session 生命周期、每 ID 串行链和独占 cold preparation 防止恢复、HMR、flush 与新建发生竞态。
5. Retry、Compaction 都通过 Agent 事件扩展 Loop，并把策略过程本身写入日志；核心循环无需知道指数退避或摘要算法。
6. `surface.replaceGeneration` 是恢复插件证明“请求输入确实发生持久变化”的事务信号。
7. 请求重建、工具配对、持久化关系并非只依赖测试，项目可通过 package invariant companion 在运行时检查。

### 本轮待动态验证

以下内容从源码可推导，但尚未通过实际运行验证：

- 服务提供方 HMR 时，依赖 Fiber 的实际 unload/reload 时间顺序；
- JSONL 200ms 固定窗口在高频 chunk 流下的实际批次数；
- 后台写失败后新事件触发恢复写入的日志表现；
- Context overflow 下“先 prune 成功、summary 失败、仍重试”的完整 Session 事件序列；
- Web profile 下 preset rebind 后各 scoped registry 的实时视图；
- fail-loud 释放终端的 2 秒上限在 Windows/macOS 上的行为。

## 第三轮：Shell、文件系统、沙箱、审批、子进程与后台任务

### 研究范围

本轮贯通模型执行能力与安全策略链，主要阅读：

- `deepseek-harness/packages/shell/tool-bash/src/index.ts`
- `deepseek-harness/packages/shell/bash-local/src/index.ts`
- `deepseek-harness/packages/shell/bash-sandbox/src/index.ts`
- `deepseek-harness/packages/shell/bash-sandbox/src/helpers.ts`
- `deepseek-harness/packages/subprocess/subprocess/src/index.ts`
- `deepseek-harness/packages/subprocess/subprocess-local/src/index.ts`
- `deepseek-harness/packages/subprocess/subprocess-local/src/spawn.ts`
- `deepseek-harness/packages/fs/tool-fs/src/read.ts`
- `deepseek-harness/packages/fs/tool-fs/src/write.ts`
- `deepseek-harness/packages/fs/tool-fs/src/edit.ts`
- `deepseek-harness/packages/fs/tool-fs/src/sandbox.ts`
- `deepseek-harness/packages/fs/fs-observation-policy/src/index.ts`
- `deepseek-harness/packages/fs/fs-local/src/index.ts`
- `deepseek-harness/packages/fs/fs-local/src/fsio.ts`
- `deepseek-harness/packages/fs/fs-sandbox/src/index.ts`
- `deepseek-harness/packages/sandbox/sandbox-policy/src/index.ts`
- `deepseek-harness/packages/sandbox/sandbox/src/escalation.ts`
- `deepseek-harness/packages/sandbox/sandbox-local/src/index.ts`
- `deepseek-harness/packages/sandbox/sandbox-local/src/profiles.ts`
- `deepseek-harness/packages/interaction/user-approval/src/index.ts`
- `deepseek-harness/packages/interaction/permission-presets/src/index.ts`
- `deepseek-harness/packages/jobs/jobs-local/src/index.ts`
- `deepseek-harness/packages/jobs/tool-jobs/src/index.ts`

### 1. Bash 的端到端执行链

模型调用 `bash` 后，真实链路为：

```text
ToolRuntime
  -> tools/pre-execute / guards
  -> dsh-tool-bash.execute()
  -> 解析当前 Session sandbox policy
  -> 可选 approveEscalation()
  -> 收集当前 DSH_* 环境
  -> ctx.shell.resolve(request)
  -> SandboxBashExecutor.run/start
  -> ctx.sandbox.confine(['bash', '-c', command], policy)
  -> LocalBashExecutor.runArgv/startArgv
  -> ctx.subprocess.spawn(fullSpec)
  -> Node child process / detached process tree
  -> 输出收集、退出与沙箱结果分类
  -> ToolRuntime post/finalize/result
  -> Session tool/result
```

关键职责没有混在一个包里：

| 层 | 职责 |
| --- | --- |
| `tool-bash` | 模型 schema、工作目录、提权、前后台分支、模型/UI 渲染 |
| `shell` | 请求/spec 和结果公共词汇 |
| `bash-local` | 默认值、timeout、输出预算、bash argv |
| `bash-sandbox` | 每调用 policy、runner 包装、拒绝/runner failure 分类 |
| `sandbox-local` | 平台 runner 选择与 profile argv |
| `subprocess-local` | 真正 spawn、stdio、进程树、spill、终止升级 |
| `jobs-local` | 后台 job ID、所有权、控制、通知 |

### 2. Shell 请求与执行 Spec 显式分离

`ShellExecRequest` 允许省略：

- workdir；
- timeout；
- stdout 上限；
- sandbox policy。

`ctx.shell.resolve()` 将其转换成完整 `ShellExecSpec`。`run()` 与 `start()` 只接收已解析 spec，不在执行中偷偷补默认值。

`LocalBashExecutor.resolve()` 每次调用都读取当前 settings，因此修改：

- 默认 timeout；
- timeout 上限；
- 输出上限；
- spill 上限；
- kill grace；

会对下一条命令生效，不需要重新构建派生状态。

模型传入 timeout 会通过 `clampTimeout()` 限制在部署上限内。前台默认值当前是 120 秒，最大 600 秒；基础 bundle 另行覆盖时以组合值为准。

### 3. 前台 timeout 与 caller abort 是 first-cause 分类

前台命令使用一个融合 deadline：

```text
caller AbortSignal
  + executor timeout
  -> one AbortSignal
  -> subprocess terminate()
```

执行完成后：

- deadline 原因是 `BASH_TIMEOUT`：`timedOut: true`；
- 上游 signal 先到：`aborted: true`；
- 两者互斥；
- exitCode/signal 仍独立报告。

所以即使命令捕获 TERM 后以退出码 0 退出，结果仍能保留“这次运行被 timeout 或 abort 提前截断”的事实，不会被误认为普通成功。

模型工具层再把 `aborted: true` 转成 `TOOL_ABORTED`，使 Agent Loop 按工具取消处理，而不是把一条被用户取消的 bash 结果继续喂给模型。

### 4. Background Bash 与 Job 生命周期分离

后台 bash 不使用 executor timeout。模型得到 JobId 后，生命周期转移给 `ctx.jobs`：

```text
bash tool call signal
  --只负责到 job commit之前-->
ctx.jobs.start()
  -> shell.start()
  -> JobHooks.cancel/done/readOutput
```

提交 JobId 后：

- 原工具调用取消不再终止后台进程；
- `job_kill`、Agent owner dispose 或 Jobs service dispose 才负责终止；
- 后台进程由 subprocess service 持有，因此单独 HMR 重载 shell executor 不会丢掉进程；
- subprocess service 卸载时会终止并等待完整进程树。

一个已知限制直接写在源码 TODO 中：未沙箱化的后台 spawn infrastructure failure 当前可能表现为无 signal 的 `killed`，还没有独立映射成 Job `failed`；真实非零命令退出仍必须保持 `completed + exit code`。

### 5. 子进程环境默认执行凭据清除

`subprocess.scrubbedParentEnv()` 会从父进程环境移除：

- 名称匹配 `/KEY|PASSWORD|SECRET|TOKEN/i` 的变量；
- 所有大小写形式的 `DSH_*` 变量。

PATH、HOME、locale、proxy 等普通环境保留。

之后调用方显式 `env` 再合并，因此：

- 普通情况下 API key 不会隐式泄漏给模型启动的命令；
- 受信任插件仍可显式转发某个凭据；
- 显式 `undefined` 是 tombstone，可删除继承变量；
- `shellEnv.collect()` 产生的当前 DSH facts 最后合并，不能被普通 env 覆盖。

这是一项“默认防泄漏”机制，不是绝对秘密边界：可信插件显式传值仍会恢复对应环境变量。

### 6. 子进程 stdio 没有隐藏默认值

`SubprocessSpawnSpec` 必须完整声明三条流：

```text
stdin: ignore | pipe | { data }
stdout/stderr: pipe | inherit | bounded collect
```

Bash 选择：

- stdin 默认为 ignore；
- 受信任调用方可提供一次性 stdin；
- stdout/stderr 使用 bounded collect；
- stdout 与 stderr 各自有独立内存尾部和 spill；
- background 读取使用全流 byte offset，不是破坏性消费底层 collector。

LSP、ACP 等协议消费方可以选择 raw pipe；诊断可以选择 inherit。Subprocess seam 不知道 shell、模型或 JSON-RPC。

### 7. 输出收集器保留尾部并可提供完整 spill

`OutputCollector` 始终维护有界内存尾部。选择 tail 而非 head 的理由是：错误和最终结果更常出现在输出末尾。

启用 spill 时：

1. 第一次内存溢出才创建文件；
2. 把此前内存内容和后续 chunk 都写进去；
3. spill 仍在完整流上限以内时，文件包含完整输出；
4. 超过 spill 上限后删除该文件，只保留内存尾部；
5. close/writeback 失败时停止公布 spill path。

默认 spill 目录：

```text
OS tmpdir/dsh-subprocess-<random>/
```

安全措施：

- 目录通过 `mkdtemp` 创建；
- 文件名含随机后缀；
- 使用 `wx` 排他创建；
- 文件权限 0600；
- 目录默认只属于当前进程用户。

这降低本机其他用户猜路径或预植 symlink 的风险。完整输出仍可能包含敏感任务数据，因此 spill path 本身只应暴露给本地授权使用者。

### 8. 进程树终止而不是只杀直接 child

POSIX spawn 使用 detached process group；Windows 使用 `taskkill /T /F`。

`terminate()`：

```text
SIGTERM whole tree
  -> graceMs
  -> SIGKILL whole tree
```

重要细节：

- 直接 child 退出不代表后代退出；
- SIGKILL 定时器不会因 direct child settle 自动清除；
- `waitForExit()` 观察整棵树；
- Linux 会检查 group 中是否只剩不能执行工作的 zombie；
- inherited pipe 被后代持有时，close 等待同样受 grace 约束；
- Node `exit` 同步阶段还有最后一次 force-kill 兜底。

Subprocess service 正常 dispose 会先请求终止，再等待 whole-tree quiescence。若等待失败，会执行同步 final kill 并聚合错误。

### 9. 沙箱 policy 的单一解析顺序

`SandboxPolicyService.resolve()` 的优先级：

```text
一次性已批准 override
  > Session 最新 sandbox/mode 事件
  > 部署 defaultMode
```

workspace root：

```text
SessionHeader.cwd
  > 服务配置 workspaceRoot
  > process.cwd()
```

路径会先通过 native realpath 语义取得文件系统身份，再做绝对路径规范化，避免 symlink/`..` 组合被纯词法折叠成错误边界。

Policy 还携带可选 SessionId，供 Windows ACL 后端建立按会话隔离的临时能力。

沙箱/审批状态通过 runtime-context snapshot 进入模型历史，而不是改写稳定 system prompt。这有利于前缀缓存，同时保证状态变化可回放。

### 10. Shipped Base 与服务默认值不是同一件事

`SandboxPolicyService` 自身 schema 的保守默认是：

```text
read-only
```

但官方 `dsh-base` bundle 显式配置：

```text
workspace-write
```

并将 approval 默认设为：

```text
ask
```

所以“类默认值”描述裸插件组合，“官方产品默认值”由 bundle 决定。阅读配置时不能只看 `static Config`。

### 11. 平台 Sandbox Runner 选择

默认 runner chain：

```text
Linux:  bwrap -> Landlock
macOS:  sandbox-exec / Seatbelt
Windows: windows-acl restricted-token runner
```

Linux 有多个候选，因此按顺序执行功能探测：

- bwrap 可用则优先；
- 否则探测 Landlock；
- Landlock probe 可报告 full、partial 或 unusable。

macOS 和 Windows 当前各只有一个候选，选择阶段不额外 probe；真正执行时如果 runner 自身拒绝，消费方仍按 fatal signature fail closed。

选择结果在 provider 生命周期内缓存，不会每条命令重新探测。

用户配置 `runnerCommand` 时，系统把它视作部署方明确断言，不执行默认探测；但必须同时配置至少一个 runner failure signature。

### 12. 各 runner 的文件策略表达

#### bwrap

```text
--ro-bind / /
--dev /dev
--unshare-pid
--proc /proc
--die-with-parent
```

workspace-write 额外：

```text
--tmpfs /tmp
--bind workspaceRoot workspaceRoot
```

#### Landlock

- `/` read-only；
- `/dev/null` 可写；
- workspace-write 时 `/tmp` 和 workspaceRoot 可写。

#### Seatbelt

基础 profile：

```text
allow default
deny file-write*
allow /dev/null
```

workspace-write 再允许规范化的 workspace 与平台 temp roots。

#### Windows ACL

- 使用 restricted token 和 ACL grant；
- workspace root 使用稳定 workspace SID；
- 每个 Session/workspace 组合获得随机私有 temp 目录及独立 SID；
- temp grant 在 provider dispose 时撤销；
- workspace grant 是 standing reuse cache，不在正常 dispose 时撤销。

Windows 后端明确报告 `partial`：Everyone ACL 与 NTFS hard-link 边界无法提供绝对文件效果承诺。

### 13. Sandbox 不隔离网络

本轮具体 profile 再次证实，`SandboxMode` 的承诺只覆盖文件效果：

- bwrap 未声明网络 namespace 隔离；
- Seatbelt profile 是 file-write policy；
- Landlock 是文件访问控制；
- Windows ACL 是文件权限。

因此 workspace-write/read-only 不能解释成“无网络”或“看不到宿主进程”。需要网络、内核或机器级隔离时，应替换为容器、microVM、远程 sandbox 等完整执行世界，而不是假设 `ctx.sandbox` 已覆盖这些维度。

### 14. Runner failure 与正常 policy denial 分开分类

受限命令失败有两种完全不同的含义：

1. runner 成功运行，内核拒绝命令的文件操作；
2. runner 自己在命令启动前失败，命令根本没运行。

分类优先级：

```text
runner failure > sandbox denial > ordinary command failure
```

runner failure 需要：

- 非零退出；
- 可选的限定 exit code；
- stderr 中匹配 fatal signature；
- 先移除精确 informational lines。

例如 Landlock 的“older ABI partial enforcement”提示是信息行，不得被误判为 runner 崩溃。

前台 runner failure 抛 `SANDBOX_UNAVAILABLE`；后台无法再 reject 已返回的进程句柄，因此在 `ShellProcess.sandbox.runnerFailed` 中报告。

这套分类仍依赖 runner stderr 方言，但只使用当前实际选中的 runner 签名，不会拿所有平台错误文本做大联合。

### 15. 一次性提权必须严格变宽

共享提权梯度：

```text
read-only -> workspace-write -> danger-full-access
workspace-write -> danger-full-access
danger-full-access -> 无可提权目标
```

Schema 公布所有可能 target，但执行时根据本次调用真实 effective mode 验证“严格更宽”。这样 Session 被动态切到更窄模式后仍有完整可用 schema，同时不能把相同或更窄模式伪装成提权请求。

`sandbox_permissions` 与 `justification` 必须成对出现，justification 不能为空。

提权顺序：

```text
验证严格变宽
  -> 检查 approval service
  -> 检查调用具有 Agent
  -> approval.request()
  -> 只有 allowed-once 返回目标 mode
  -> 仅给当前调用覆盖 policy
```

非严格变宽请求不会打扰用户。任何 unavailable/rejected/cancelled 都 fail closed，且在批准前不执行任何目标操作。

### 16. Bash 与 FS 共享同一个提权词汇

Bash 和文件写/edit 都使用 `dsh-sandbox/escalation.ts`：

- 同一 mode ladder；
- 同一参数配对验证；
- 同一 `[sandbox: ...]` denial marker；
- 同一 approval outcome 映射；
- 同一“只重试当前操作一次”的模型提示。

这样模型不会遇到“bash 拒绝使用一种说法，write 拒绝使用另一种说法”的协议漂移。

文件 read 不需要提权，因为当前 fs policy 对读取不施加限制；只有 write/edit 携带 sandbox policy。

### 17. FS Sandbox 是可信代码路径 fence，不是内核沙箱

`dsh-fs-sandbox` 继承 `LocalFileSystem`，只在 write/edit 前增加路径 policy 检查：

```text
read-only         拒绝所有 mutation
workspace-write   仅允许 writableRoots 下的目标
danger-full-access 直接委托
```

源码明确说明：这是 trusted in-process code 对 model-controlled path 的 containment，不是针对不可信代码的 kernel boundary。

workspace-write 会在 mutation 前重新 resolve 目标，捕获从工具初始解析后发生的 ancestor symlink 替换，并把**这个重新解析后的 target**交给实际写入，避免“检查 A、写 B”。

仍然接受一个残余 TOCTOU：最后一次 containment 检查与系统调用之间，外部进程再次替换 ancestor symlink。该威胁模型认为可信工具代码加尽量靠近写入的重新规范化已经足够；运行不可信程序仍必须走 Shell Sandbox。

### 18. FS Target 与版本 token

本地 `FsTarget.targetKey` 是 realpath 派生的规范身份：

- 已存在 symlink 文件解析到真实目标；
- 不存在目标会 realpath 最近存在的 ancestor，再拼回缺失 suffix；
- 所以同一文件的 alias 共享 observation/CAS 身份；
- `displayPath` 保留调用方看到的绝对拼写。

版本 token：

```text
dev : ino : size : mtimeNs : ctimeNs
```

消费方不解析该字符串，只做等值比较。

每个 targetKey 有独立 FIFO mutation Promise chain，因此同目标并发 write/edit 的读取、版本检查、匹配和发布不会互相穿插；一个成功后，后续旧版本操作稳定地得到 stale rejection。

### 19. 先读后写策略实际是 Session 级 CAS

`fs-observation-policy` 不提供服务，也不执行 I/O。它维护：

```text
WeakMap<SessionObject, Map<FsTargetKey, Observation>>
```

Observation：

```text
unseen
absent
present(version)
```

策略：

| 操作 | unseen | absent | present(v) |
| --- | --- | --- | --- |
| write | createIfAbsent | createIfAbsent | replaceIfVersion(v) |
| edit | FS_NOT_OBSERVED | FS_NOT_FOUND | version(v) |

因此：

- 新文件不要求先执行失败的 read；
- 但 `createIfAbsent` 在发布点发现文件已存在时会拒绝，绝不盲目覆盖；
- 覆盖已有文件必须先 read；
- edit 必须先 read 或继承此前成功 write/edit 记录的新版本；
- 外部修改后旧版本 write/edit 报 `FS_STALE_VERSION`，要求重新读取。

State 以精确 Session 对象为 owner，Agent dispose 后可以被 GC；策略 HMR 时会主动换掉整个 WeakMap，避免旧 observation 跨代继续授权。

### 20. Read 的部分窗口同样形成完整新鲜度授权

`read` 先做一次 stat：

- 缺失时记录 absent；
- 非普通文件拒绝；
- 小文件整体读取；
- 大于等于 10MiB 或 size 未知时使用 stream；
- 返回行窗口和精确 totalLines；
- 成功后记录 stat 时的 version。

即使只读取一部分行，也记录完整文件版本。授权依据是“目标版本已被观察”，不是“内容是否全部显示”。后续 mutation 仍在提供方锁内重新检查 version，所以 read 后的并发外部改动只会导致 stale，而不会利用部分窗口覆盖未知新内容。

Read 标记为 `isConcurrencySafe: true`，可与同步骤其他并行安全工具重叠；版本 CAS 负责在后续 mutation 处 fail closed。

### 21. 文件写入的原子发布

Local FS 写入使用目标同目录下的私有 staging directory：

1. 创建随机 0700 staging dir；
2. 使用 `wx` 创建 0600 temp file；
3. Windows 替换时先复制原目标 DACL；
4. 写入完整 UTF-8 内容；
5. `FileHandle.sync()`；
6. 恢复原文件 mode；
7. 发布到最终路径；
8. 清理 staging dir。

普通替换：

- POSIX 使用 rename；
- Windows 使用保留安全描述符的 ReplaceFile，目标竞态消失时退回 rename。

`createIfAbsent` 使用 hard link no-replace 发布：如果并发创建者抢先生成目标，link 失败后重新检查目标并返回 `FS_NOT_OBSERVED`，不会覆盖对方文件。

一旦最终路径已提交，staging 清理失败不会把成功写入改报失败；只留下 owner-only residue。

### 22. Edit 是提供方原子 read-match-write

`editText()` 在同一个 per-target lock 内完成：

```text
probe + version guard
  -> 读取并验证 UTF-8/非二进制
  -> CRLF 规范化为 LF
  -> literal match
  -> 唯一匹配或 replace_all
  -> 恢复原行尾风格
  -> atomic publish
  -> 返回 before/after/version
```

Guard 在 literal matching 之前，因此旧内容导致的是 `FS_STALE_VERSION`，而不是误导性的 `old_string not found`。

默认模式要求 old_string 恰好一次：

- 0 次：`FS_EDIT_NOT_FOUND`；
- 多次：`FS_AMBIGUOUS_EDIT`；
- `replace_all: true` 才替换全部。

UI diff 基于 LF-normalized before/after，避免 CRLF 文件看起来每一行都被修改；存储写回仍恢复原行尾风格。

### 23. Approval 请求本身也必须可审计

`ApprovalService.request()` 只允许在 open turn 中调用。顺序：

```text
append approval/asked
  -> 执行 answerer waterfall
  -> append approval/decided
  -> 返回 outcome
```

原因：审批对必须位于拥有该工具调用的持久轮次内；游离在轮次之间的审批记录无法可靠归属执行边界。

Policy：

```text
ask    -> 分发 answerer；无人处理时 unavailable
never  -> 服务内部直接 rejected，不调用 answerer
```

`never` 在服务方法内部检查，而不是注册一个 prepend listener，因此无论之后谁注册更早的 answerer，都不能绕过 deterministic reject。

Answerer 防御：

- 同步 throw 与异步 reject 都归一化为 unavailable；
- 返回未知字符串归一化为 unavailable；
- signal abort 先赢时返回 cancelled；
- 晚到 answer 被丢弃；
- 只有 `allowed-once` 是 grant。

审批请求不复制工具参数，只保存 toolName、callId 和 reason；UI 已经通过 callId 持有原始调用展示，避免出现第二份可能漂移的参数副本。

### 24. Permission Preset 同时保存用户意图与独立旋钮

Permission preset 组合：

```text
preset name
  -> sandbox mode
  -> approval policy
```

切换时事件顺序：

```text
permission/preset
  -> sandbox/mode（如变化）
  -> approval/policy（如变化）
```

执行层仍只读取各自旋钮；`permission/preset` 保存用户选中了哪个名字。当两个 preset 映射到相同旋钮组合时，回放仍能保留用户选择，而不是仅靠反向匹配猜第一个名字。

如果当前旋钮不匹配任何表项，客户端投影显示 `custom`，但 `custom` 不能作为配置 preset 名。

新建 Session 会把缺失的三个事实补齐并写入日志。真正全新的 Session 使用当前 settings 默认 preset；seeded Session 保留已有旋钮，只补缺失项。

官方组合中的常见预设：

```text
read-only          -> read-only + ask
workspace-write    -> workspace-write + ask
danger-full-access -> danger-full-access + never
```

### 25. Job 启动前要求 Agent 可控制它

`jobs.start()` 首先检查是否存在覆盖该 owner scope 的 controller。`tool-jobs` 插件加载时通过：

```text
ctx.jobs.attachController('tool-jobs')
```

注册 controller。

如果某个 Agent preset 没有安装 job controls，即使 host 有全局 Jobs registry，该 Agent 的 producer 也不能偷偷启动一个模型无法查看或终止的后台任务。

Global controller 服务所有 owner；scoped controller 只服务其 scope chain 覆盖的 Agent。

### 26. Job 所有权不是依赖不可猜 ID

JobId 是可预测的：

```text
bash-1
subagent-1
...
```

授权通过 owner SessionId 比较：

- owned job 只允许同 Session Agent list/get/read/kill/wait；
- 无 Agent 调用方不能读取 owned job；
- unowned job 对任何调用方开放；
- start 时要求 owner 是 AgentRegistry 中的精确 live Agent 对象；
- owner.ctx 安装 awaited cleanup，Agent 销毁时取消并等待所有 owned jobs。

默认每个精确 owner 最多 10 个 running/stopping job；所有 unowned job 共享一个单独的服务级桶。

### 27. Job settlement 采用 first-wins

终态：

```text
completed | killed | failed
```

Settlement 顺序：

1. 第一项终态写入 mutable record；
2. 记录 detail/output/finishedAt；
3. 释放 waiters；
4. resolve `settled`；
5. 通知可见集合变化；
6. 最后通知 completion listeners。

完成通知最后执行，是因为 listener 可能同步唤醒 Agent 开新轮次；在此之前所有状态观察者必须已经看见终态。

Producer `done` 按约定不应 reject；若 reject，registry 会包含该违例并把 Job settle 为 failed，避免 waiter 永久挂起。

Teardown cancel 如果同步 throw，registry 会 force-fail 记录并警告“work may be orphaned”。如果 cancel 返回但 producer 永远不 settle，teardown 仍可能等待；这属于 producer 违反 quiescence 约定，注册表不能硬杀任意同进程资源。

### 28. Job 输出读取与完成唤醒有自激预算

`job_output` 可以：

- 立即读取增量；
- `wait: true` 有界等待；
- timeout 只结束等待，不杀 Job；
- caller abort 取消等待；
- terminal read 将记录标为 reported，抑制重复完成通知。

`tool-jobs` 默认在 idle owner 的 Job 完成时用 `followup()` 唤醒模型；busy owner 使用 `inject()` 合并到下一步骤。

为防止以下自激链：

```text
完成通知唤醒 Agent
  -> Agent 又启动 Job
  -> Job 完成再次唤醒
  -> 无限循环
```

每个精确 Agent 默认最多连续 3 次 completion wake。真正的用户消息被 inbox claim 后预算重置；超出预算的完成通知降级为不唤醒的 inject。

### 29. 本轮安全边界总结

#### 可以依赖的保证

- 模型 bash 默认不会继承 credential-shaped 环境变量；
- 受限 shell runner 不可用时 fail closed；
- 进程终止覆盖树而非仅 direct child；
- 文件写/edit 使用原子发布；
- 默认 fs policy 防止未读覆盖与陈旧编辑；
- 提权只授权一次确切调用；
- approval 无应答时拒绝；
- owned Job 由 Session 身份授权并随精确 Agent 生命周期清理。

#### 不能误解成的保证

- file sandbox 不等于 network sandbox；
- fs-sandbox 是 trusted path fence，不是内核隔离；
- 环境 scrub 不阻止可信插件显式转发 secret；
- spill 文件虽然权限受限，仍是敏感本地工件；
- `partial` enforcement 不能当作绝对文件隔离；
- 同进程 producer 不遵守 cancel/done 约定时，Jobs registry 无法硬杀其任意工作。

### 本轮综合结论

1. Shell 安全不是一个拦截器，而是 tool、policy、approval、sandbox provider、executor、subprocess 六层共同形成的闭合链。
2. Bash 与 FS 共享 policy 和提权协议，但执行强制方式不同：bash 依赖 kernel/runner，FS 依赖可信进程内路径 fence。
3. `fs-observation-policy + FsVersion + per-target lock + atomic publish` 共同组成真正的先读后写 CAS；只看工具提示词无法理解这一保证。
4. Subprocess seam 将所有进程树、stdio 和输出持有逻辑下沉，因此 Shell、LSP、ACP 与 PTY 不必各自实现不一致的 kill/cleanup。
5. 后台任务必须先证明 owner scope 中存在控制器，避免 producer 创建模型无法收集或停止的工作。
6. Permission preset 不替代底层旋钮事件；它额外记录用户选择意图，执行与回放仍由 sandbox/approval 各自拥有。

### 本轮待动态验证

- Linux 主机上 bwrap 与 Landlock probe 的实际选择结果；
- macOS Seatbelt `/tmp` 与 `/private/tmp` 规范化后的真实写入行为；
- Windows ACL partial enforcement、private temp SID 和 standing workspace grant；
- descendant 捕获 TERM 后，SIGKILL 与 `waitForExit()` 的实际时序；
- 大输出超过内存 cap、超过 spill cap 两种情况下模型结果文本；
- 外部进程在 read/write 间修改文件时的 `FS_STALE_VERSION` 事件与 UI 表现；
- Job completion 连续唤醒预算在真实多轮 Agent 中的收敛效果。

## 第四轮：Web Host、RPC、事件流、浏览器插件与客户端状态收敛

### 研究范围

主要阅读：

- `deepseek-harness/packages/host/webserver/src/index.ts`
- `deepseek-harness/packages/client/connection/src/api-request-trust.ts`
- `deepseek-harness/packages/client/connection/src/index.ts`
- `deepseek-harness/packages/client/connection/src/http-bridge.ts`
- `deepseek-harness/packages/client/connection/src/rpc-host.ts`
- `deepseek-harness/packages/client/connection/src/websocket-downlink.ts`
- `deepseek-harness/packages/client/connection/src/client/connection.ts`
- `deepseek-harness/packages/client/connection/src/client/web-api-client.ts`
- `deepseek-harness/packages/host/apiproxy/src/fetch/handler.ts`
- `deepseek-harness/packages/host/apiproxy/src/fetch/client.ts`
- `deepseek-harness/packages/host/apiproxy/src/api-proxy.ts`
- `deepseek-harness/packages/client/modules/src/index.ts`
- `deepseek-harness/packages/client/modules/src/client/system.ts`
- `deepseek-harness/packages/client/web/src/boot.ts`
- `deepseek-harness/packages/client/runtime/src/client/sessions/manager.ts`
- `deepseek-harness/packages/client/runtime/src/client/sessions/session.ts`
- `deepseek-harness/packages/client/runtime/src/client/sessions/projection-store.ts`
- `deepseek-harness/packages/client/runtime/src/client/ordered-baseline.ts`

### 1. Web 不是一个固定 React Bundle

Web 运行时分为两棵 Cordis 树：

```text
Host Cordis tree
  -> WebServer
  -> API/Connection
  -> ClientModuleRegistry
  -> index 注入 __DSH_BOOT__

Browser Cordis tree
  -> ClientModuleSystem
  -> Browser Loader
  -> client plugin fibers
  -> uiRenderer
  -> React mount
```

`apps/web/src/main.ts` 只找到 `#root` 并调用 `new AppWebEntry(el).run()`。真正的浏览器应用由 Host 当前组合出的 client plugin graph 决定。

因此增加一个 Web 功能通常不是修改中央路由或中央 React App，而是：

- 包声明 `dsh.client`；
- 导出 `./client` bundle；
- Host 插件进入 Loader 树；
- Node half 将其加入 boot graph；
- Browser Loader 动态挂载 client half。

### 2. WebServer 是无业务知识的路由载体

`dsh-host-webserver` 只负责：

- `node:http` listen；
- exact route；
- longest-prefix route；
- exact upgrade route；
- 唯一 fallback seat；
- index injection/tap。

匹配顺序：

```text
exact
  -> longest matching prefix
  -> fallback
  -> 404
```

重复 `(kind, path)`、重复 upgrade path、第二个 fallback 都在插件加载时失败。注册顺序不用于解决冲突。

监听地址只允许：

```text
127.0.0.1
0.0.0.0
```

WebServer 本身不提供 TLS、认证、CORS 或 Origin policy。请求 handler 抛错会记录 warning，并在 header 尚未发送时返回 400；不会因为单个畸形 URL 或中断 body 退出整个进程。

Dispose 时：

- `server.close()`；
- `closeAllConnections()`；
- 显式跟踪并 destroy upgrade sockets；
- 等待所有 socket 关闭。

Node 的 `closeAllConnections()` 不包含已经 upgrade 的 WebSocket，所以后者必须单独持有。

### 3. `/api` 信任栅栏首先防 DNS Rebinding

每一个 `/api` HTTP 或 WebSocket 请求都检查 Host authority，不能因为请求没有 Origin 就绕过。

原因：普通浏览器导航、图片等明文 HTTP 请求可能没有 Origin 和 Fetch Metadata；DNS rebinding 页面仍能把本地 API 当资源读取，而 Host header 是攻击页面无法伪造成 `localhost` 的关键事实。

允许 Host：

```text
loopback hostname
  或
trustedHosts 中的 canonical authority
```

`trustedHosts` 规则：

- `host:port` 只匹配该端口；
- `host` 匹配该 hostname 的任意端口；
- 两边用 WHATWG URL 规范化；
- 配置项必须本身就是规范的裸 authority；
- path、userinfo、空格、悬空冒号、补零端口、非规范 IP、未加括号 IPv6 等在插件加载时拒绝。

额外浏览器校验：

- `sec-fetch-site: cross-site` 直接拒绝；
- 存在 Origin 时，必须与 Host authority 完全一致；
- `Origin: null` 拒绝。

HTTP 在进入 RPC handler 前返回 403；WebSocket 在协议升级前返回 403。

### 4. trustedHosts 是可达性栅栏，不是认证

源码和文档都明确说明：`trustedHosts` 不是用户身份认证。

即使一个 LAN hostname 被信任，以下配置/宿主操作仍强制用空 trust list 再检查一次，即只允许 loopback：

- settings describe/open/update/replace/mutate；
- credentials describe/set/unset；
- LLM endpoint discovery；
- Agent preset read/copy/open/remove；
- native directory picker；
- host openPath。

这些读取也被视为特权，因为会暴露配置命名空间、凭据来源状态、插件组合或驱动宿主桌面。

但普通 session create/prompt、preset list/select、模型目录等不在 loopback-only 集合。原因之一是默认 preset 本身已经有 Bash/FS；只限制 preset 切换并不能形成真正能力边界。

#### 安全结论

绑定 `0.0.0.0` 并信任 LAN authority 后，匿名可达客户端仍可能创建并驱动拥有默认编码工具的 Session。当前没有认证层，所以这不是适合暴露到不可信网络的多用户服务。官方注释也将远程全接口访问描述为“认证层出现之前有意不支持”。

### 5. HTTP Bridge 的请求体是整体缓冲

Node request 会先完整读入 `Buffer[]`，再构造 WHATWG `Request`。

默认上限：

```text
300 MiB
```

这是根据默认 200MiB 图片聚合限制经过 Base64 膨胀和 envelope headroom 推导的。

防护：

- Content-Length 已超限时立即 413、close、destroy request；
- chunked body 在累计超限时同样 413；
- response close 且尚未正常结束时 abort Fetch request；
- 流式 response 写入遵守 `res.write()` backpressure，等待 drain 或 close。

限制：请求体仍整体驻留内存，因此 300MiB 同时是单请求内存上界，不是流式上传。若要降低内存而不降低图片限额，需要真正流式 request body path。

### 6. POST 必须是 application/json

`toFetchHandler()` 拒绝 `text/plain`、form 等浏览器 simple POST，返回 415。

这迫使浏览器跨站写请求触发 CORS preflight，而服务不开放对应跨站 CORS，从而防止恶意页面“虽然读不到响应，但能盲发 session.prompt”一类副作用请求。

HTTP status 与业务错误严格分开：

| 状况 | 表示 |
| --- | --- |
| 非 JSON media type | 415 |
| body 不是 JSON | 400 |
| 未知 endpoint | 404 |
| handler 崩溃 | 500 |
| 合法业务拒绝 | HTTP 200 + `RpcResult { ok:false }` |

### 7. RPC 采用双层 schema 校验

Client 请求完整 envelope：

```text
type
rpcId
method
payload
```

Host 处理顺序：

1. path 映射到 method；
2. 解析通用 ClientRequest envelope；
3. 要求 envelope method 与 URL path 一致；
4. 用该 method 专属 Zod schema 解析 payload；
5. 调用业务实现；
6. 业务结果包装为 ServerResponse。

Client 处理响应：

1. 解析通用 ServerResponse；
2. 校验响应 rpcId 等于请求 rpcId；
3. error branch 直接返回；
4. success value 再用 method 专属 schema 校验。

因此把某个 endpoint 的 schema 误贴到另一个 endpoint，在 TypeScript 路由表层就会报错；即使 Host 业务返回畸形成功值，Client 仍会拒绝。

无法从坏 envelope 读到 rpcId 时，Host 使用稳定 sentinel：

```text
invalid-request
```

保证错误响应自身仍满足协议 schema。

### 8. 一元调用与事件流是两个协议

Typert Remote 只处理 request-response 方法。Session event、queue、projection、pending question 等使用独立流协议，不伪装成 Remote 方法。

`/api` shared handler 的优先级：

```text
Typert Gateway interceptor 匹配严格 Remote endpoint
  -> 否则 ApiProxy fallback
```

Typert strict descriptor 会验证：

- endpoint；
- args 字段全集；
- wire codec；
- lookup identity；
- scoped Context；
- service binding；
- return value。

源码模式下有较弱 SRC decorator fallback，但 Browser Client 仍依赖最近生成的严格 Remote client 产物；运行时 Host decorator 不会自动把新类型同步给浏览器。

### 9. 浏览器下行使用两条只读 WebSocket

物理通道：

```text
POST /api/...          Client -> Host unary/respond
WS /api/events.mux     Host -> Client session细粒度事件
WS /api/events.host    Host -> Client实体/状态事件
```

WebSocket 是 downlink-only。Client 向 socket 发送任何消息时，Host 以 code 1008 关闭；上行永远走 HTTP POST。

Mux stream 主要承载：

- session/event；
- session/subscribed；
- queue snapshot；
- projection；
- jobs；
- approval/question requested/resolved。

Host stream 主要承载：

- session added/removed/status；
- agent error；
- workspace changes；
- allowlisted Remote events。

两条 stream 之间没有全局顺序，因此 Client 在多个位置做幂等清理。例如 Job rows 既在 mux empty snapshot 时移除，也在 host/session-removed 时移除，保证任一帧先到都收敛。

### 10. Connection Generation 的就绪条件

每次连接 generation 同时启动：

- mux WebSocket；
- host WebSocket；
- `host.describe` unary。

正常就绪要求：

```text
mux opened
+ host opened
+ host.describe success
```

之后才触发 `onConnected()`，这样 resync 不会跑在订阅 baseline 之前。

`streamOpenTimeoutMs` 默认 3 秒。若某个 carrier 永远不触发 onOpen，超时后 generation 仍可进入 connected，后续依赖 history/live gap repair 修复遗漏。

任一 socket 结束都会：

- abort 当前 generation；
- 让两条 stream 一起失效；
- 状态变为 reconnecting；
- 指数 backoff + 半区间 jitter；
- 重建两条 stream 和 describe。

默认 backoff：500ms 起步、乘 2、上限 10s，实际等待位于 cap 的 1/2 到 cap。

业务 sink 抛错会被记录但不会杀连接 pump；坏业务投影不能拖垮 transport reconnect。

### 11. Host Pending Interaction 跨浏览器断线存活

Host 维护：

```text
pendingQuestions
pendingApprovals
```

每项有稳定 rpcId。Mux 新连接打开时会重放所有仍 pending 的 requested frame，使用**同一个 rpcId**。

因此：

- 页面刷新/临时断线后仍可回答；
- Client 在 generation 断开时清掉本地 generation-scoped pending；
- 新 stream baseline 重新创建仍有效的 PendingWait；
- 已解决等待不会复活；
- turn signal abort 会从 Host pending map 删除并广播 resolved/cancelled。

它们不能跨 Host 进程重启，因为 map 和 promise resolver 位于内存；Host 重启也会结束原 turn。

### 12. Approval 的 Host 桥处理并行 asked 配对

ApprovalService 在 dispatch answerer 前已写 `approval/asked`，但多个并行工具可能在 answerer microtask 开始前连续写多条 asked。

API Proxy 不能简单取最后一条事件。它会：

- 收集已经决定的 ApprovalRequestId；
- 收集已经被 pending entry 认领的 ID；
- 从日志尾部找最新“未决定、未认领、callId 对称匹配”的 asked；
- 没找到时调用 `next()`，不冒充该请求的回答通道。

这样并行工具审批不会互相偷用 audit ID。

浏览器 respond 必须同时匹配：

- envelope rpcId；
- approvalId；
- sessionId。

否则返回 `bad-response`。

### 13. Mux 每个 generation 都有明确 baseline

Mux 打开时先为每个 live Session 推送：

```text
session/subscribed { lastSeq }
```

然后重放：

- pending questions；
- pending approvals；
- 非空 queue snapshots；
- 非空 jobs snapshots。

随后注册 live listeners：

- session event；
- new/disposed session；
- job changes。

`session/subscribed` 是 Client 的 generation cut：

- 清理旧 queue mirror；
- 清理旧 jobs mirror；
- 截断声称位于 Host durable baseline 之后的 projection rows；
- 建立 live event gap 检测参照。

空 queue/jobs 不单独发 `[]` baseline，因此 subscribed frame 必须先把上一 generation 的镜像清空。

### 14. Session event 与 Tool UI view 分离传输

Mux `session/event` 携带原始持久事件，并可附加 Host 现场计算的 `view`。

Host 为 tool result 找调用参数时：

- 先读当前 openCalls 表；
- stream 在 call 之后才打开时，回扫 Session events；
- 参数 JSON 无法解析时放弃 specialized view，Client 使用 generic fallback。

View 是分页/传输时派生数据，不进入持久 Session event。持久化的是工具自己的 `meta` 和模型内容；Host presenter 可在 replay 时重新计算 neutral render intent。

### 15. 浏览器模块图由 Host 当前插件树生成

`ClientModuleRegistry` 监听 Host Loader 的 `internal/plugin`，按 entry package 增量扫描：

1. 解析 package.json；
2. 检查 `dsh.client.platform === 'web'`；
3. 读取 `exports['./client']`；
4. 读取 built client bundle；
5. 计算 12 位 SHA-1 rev；
6. 建立 `/plugins/<id>/client.js?rev=...`；
7. 根据 `dsh.client.external` 做依赖拓扑排序；
8. 组合整个 graph rev。

包 metadata，包括“不是 client package”的负结论，按包名永久缓存到进程重启。Bundle 内容变化不靠重新扫描；HMR watcher 必须显式调用 `rebuilt(id)` 重新 hash。

首次激活时缺少 bundle 会聚合成一个带包名和路径的启动失败，提示先运行 `pnpm run build`。稳态某一个包损坏只 warning，不毒害其他包；如果新图出现 cycle，则继续服务最后一个可排序图。

### 16. Boot HTML 注入顺序

Host index injection：

```text
1. 安装 window.__ModuleLoader__ queue facade
2. parser-blocking preload client-modules bundle
3. parser-blocking preload client-runtime bundle
4. 写入 window.__DSH_BOOT__ graph
5. Vite shell 执行 AppWebEntry
```

提前执行 bundle script 只调用：

```text
window.__ModuleLoader__.load({ id, factory })
```

不会立即执行模块主体。

AppWebEntry 调用 facade.create() 时，先物化 client-modules 自身 factory，建立正式 module system，再把 queue 中其余 registration 切换到 live registry。

### 17. 浏览器模块系统是 Lazy Factory CJS

解析优先级：

```text
platform/static seed
  -> materialized cache
  -> boot graph row（异步加载 script）
  -> registered factory
  -> error
```

Script 到达只注册 factory。真正 import 时：

```text
factory(require)
  -> 模块副作用/CSS
  -> exports
  -> memoized ClientModuleRecord
```

同步 `require()` 不能进行网络加载，所以动态 external provider 必须在 consumer materialize 前先 arrive。Host graph 和 Browser runtime 都检查依赖 cycle。

Factory CJS 不支持 partial exports，因此 require cycle 直接报错，不尝试 CommonJS 式半初始化对象。

模块系统还会记录：

- observed require edges；
- factory 拥有的 style tags；
- materialized exports。

`invalidate(id)` 删除非 bootstrap factory/cache，下一次 import 重新加载；样式和 fiber 的实际卸载顺序由 HMR 驱动器负责，不由 module loader 自己处理。

### 18. Browser Boot 要求完整插件名册成功

`AppWebEntry`：

1. 创建 module system；
2. 预取 `immediately` tier；
3. 创建 Browser Cordis Context；
4. 挂载 vendored Loader；
5. 将 Loader internal import 替换为 ClientModuleSystem；
6. 为 manifest 每个 plugin 创建 entry；
7. 等待 Loader settle；
8. 审计每个 entry ACTIVE；
9. 通过 `ctx.uiRenderer.mount()` 挂载 UI。

任何 import、apply 或 missing service 失败都会停留在不依赖 React 的原生 boot page 并展示错误。当前不支持“部分 UI 可用”。

### 19. Client RPC 自身仍校验 transport 关联

每次 unary：

```text
mint rpcId
  -> envelope tap
  -> POST
  -> 解析 ServerResponse
  -> 校验 echo rpcId
  -> 解析 method-specific value
  -> 返回 narrow RpcResponse
```

默认 unary timeout 为 30 秒。原生目录选择器等 user-paced 操作不使用这个健康超时，只接受 caller/connection abort。

Envelope observer 按 microtask 批量通知，frame storm 不会让诊断消费者每帧更新一次；observer throw 被隔离。

WebSocket/SSE 遇到畸形单帧时记录并丢弃，不立即杀整个 stream。Session seq gap repair 会重新拉取尾页恢复该帧可能携带的持久事件；非持久瞬态帧则依赖下一 generation baseline。

### 20. SessionManager 不让慢 baseline 覆盖新帧

`session.list` 请求发出后，Manager 建立 `listMutations` 数组。请求在途期间收到：

- session added/removed；
- running status；
- activity；
- 本地 accepted prompt；

会立即应用，同时记录到 mutation 数组。

List response 到达后：

1. 安装 baseline；
2. 按到达顺序重放在途 mutations；
3. 每次 mutation 后更新 running->idle completion edge；
4. 发布最终 snapshot。

这避免一个较慢的 list response 把刚刚到达的 running、removed 或新 Session 覆盖回旧状态。

首次 baseline 直接采用 Host 顺序；后续 baseline 使用 `mergeOrderedBaseline()`：保留当前已知 identity 的相对顺序，插入 baseline-only identity，并删除 baseline 已不存在的 identity。这减少刷新导致的列表跳动，同时仍使用 baseline 的最新 row value。

### 21. 未实例化 Session 只缓存 history 无法补回的瞬态

SessionManager 不会因为每个 mux frame 都创建 Browser Session 对象。

对未实例化 Session：

- 普通 session/event 丢弃，打开时由 history 回填；
- approval requested 缓存；
- question requested 缓存；
- queue latest snapshot 缓存；
- corresponding resolved frame 会删除 pending 缓存；
- projection 进入独立 ProjectionValueStore；
- jobs 进入 manager 级 mirror。

因此冷 Session 列表可以持续接收标题、pending interaction 和 job 状态，而不为每行构造完整 Conversation assembler。

实例化时先重放 pending/queue，再同步 list 的 blank/running bits，保证打开即有可回答对象。

### 22. Projection 使用唯一规则：higher seq wins

每个 Session 有：

```text
key -> { value, seq }
```

Frame：只有 `seq > current.seq` 才更新。

Tail history baseline：

- carried key 使用同一规则；
- baseline 未携带的 key 表示该能力在 cut 时不存在；
- 只有旧 row `seq <= baseline.asOfSeq` 才可清除；
- 更新 frame 比 baseline 新时，baseline 不能覆盖或清除它。

`session/subscribed.lastSeq` 还会删除 seq 高于 Host 当前 durable baseline 的本地 row。这种 row 可能来自上一个 Host generation 已推送、但进程重启后没有持久化的状态；若不删，它会永远压住新 Host 从较低 seq 重建出的真值。

Projection store 提供：

- 每 key identity-stable observable；
- 全值 reference-stable snapshot；
- microtask-batched any-key notification。

领域插件不在 Client 重复 fold Session events；Host 是 projection 的唯一计算点。

### 23. Browser Session 保持连续事件窗口

Session 打开时拉取 tail history，实时 frame 在 loading 期间进入 `liveBuffer`。安装历史后按 seq 拼接：

- `seq <= tail`：视为重放重叠，丢弃；
- `seq === tail + 1`：直接追加；
- `seq > tail + 1`：发现 gap，缓冲并重新拉 tail page；
- repair 期间新 frame 继续缓冲；
- 新 tail 安装后再次按 seq 拼接。

如果 subscribed baseline 指出 Host lastSeq 大于首次 history tail，也会再拉一次 tail page。

客户端始终维持一个连续 raw event range，不渲染有洞的历史。这对 tool call/result、compaction summary 引用和 Conversation Definition 关联非常重要。

Reconnect resync 会增加 `openGeneration`。旧 history promise 即使晚到，也因 generation 不匹配而丢弃全部写入，不能把死连接结果装进新窗口。

### 24. Blank Session 的 Client 状态只降不升

Blank 的 Host 定义是：日志中没有 `turn/start`。

Client 清空 blank 的信号：

- prompt RPC 被 Host 接受；
- running:true frame；
- list baseline 报 blank:false。

仅“发起 prompt 尝试”不会永久清空；拒绝的首条 prompt 仍保持 blank，可被 New Session 流程复用。

一旦 blank 已因 accepted/running/历史清空，较旧的 `blank:true` frame 不会把它重新隐藏。

Composer 独立维护：

```text
blank -> engaging -> active
```

第一次 prompt 在 await 前同步进入 engaging，避免 UI 首帧仍显示空白；只有可见内容、running、pending interaction 或已确认非 blank 才进入 active。

### 25. 两条事件流的状态采用幂等而不是跨流事务

Mux 与 Host WebSocket 没有共享序号，也没有跨流原子提交。代码通过以下方式收敛：

- Session durable events 用 per-session seq；
- projection 用 key seq；
- queue/jobs 使用 whole snapshot last-wins；
- pending interaction 用稳定业务 ID；
- session list 在途 mutation replay；
- host removal 与 mux emptying 双路径清理；
- reconnect 使用 subscribed baseline + history resync。

这比试图在两个独立 socket 上建立全局顺序更实际，但要求每个新增 frame 设计自己的稳定 identity、baseline 或幂等清理规则。

### 26. 本轮安全与可靠性结论

#### 已实现的边界

- 每个 API 请求先做 Host authority 检查；
- cross-site Fetch Metadata/Origin 被拒绝；
- privileged config/credential/native 方法强制 loopback；
- 非 JSON simple POST 不能执行 RPC；
- request/response 做 method-specific schema；
- WebSocket 只允许 Host 下行；
- reconnect 后 pending、queue、jobs、projection 和 history 各有重建机制；
- Browser plugin graph 与运行时模块依赖都拒绝 cycle。

#### 仍需部署者注意

- trustedHosts 不是认证；
- `0.0.0.0` 暴露的是本地编码 Agent，不是只读 UI；
- 单请求可占用接近 300MiB body 内存；
- 两条 WebSocket 不提供全局事件顺序；
- Host pending interaction 不能跨 Host 重启；
- Browser 当前要求所有 client plugin ACTIVE，不支持局部降级；
- Client module/runtime 卸载链仍有已记录的 stub/延后工作。

### 本轮综合结论

1. Web 安全的第一道边界是 canonical Host authority，而不是只依赖 Origin；这是针对本地 HTTP 服务 DNS rebinding 的正确姿态。
2. Web transport 清楚区分 unary、respond、mux 和 host stream；不同数据类型使用不同收敛原语，而不是共享一个万能事件总线。
3. Host 与 Browser 都保留 Cordis 插件模型，因此产品 UI 也能按组合安装、隔离和 HMR，而不是只能扩展后端。
4. Client 不重新解释所有 Session 领域状态：持久 conversation 从事件窗口组装，通用 projection 则只接收 Host 完成值，两条路径职责分开。
5. `baseline + in-flight mutation replay + per-key/per-session seq` 是前端抵御响应乱序和重连竞态的主要模式。
6. 当前 Web 适合本机单用户使用；信任 authority 只能阻止浏览器混淆代理攻击，不能替代远程认证和授权。

### 本轮待动态验证

- 实际浏览器 DNS rebinding/cross-site 请求在各浏览器的 Host、Origin、Sec-Fetch-Site 组合；
- 300MiB body 上限附近的 Node 内存峰值和断连回收速度；
- 两条 WebSocket 任一断开时 generation 重建和 UI 状态过渡；
- 故意丢弃一个 session/event frame 后 tail gap repair 的表现；
- Host 重启前有未 flush projection frame 时 subscribed truncate 的收敛；
- Client HMR invalidate 后 style、fiber 与 module factory 的真实释放顺序；
- 大量并发 Session/Job/Projection frame 下 microtask 与 animation-frame 批处理效果。

## 第五轮：Code Runtime、Workflow 与 Subagent 深层执行

### 1. Code Mode 的 worker 是强制终止容器，不是安全沙箱

`code-runtime-worker-thread` 每次运行创建一个全新 Worker：

- TypeScript 先用 Node `stripTypeScriptTypes` 擦除类型；
- 代码作为 strict async-function body 执行，支持顶层 `await`/`return`；
- Worker 环境设为 `{}`，不继承宿主环境；
- `execArgv: []`，不继承 tsx/test loader；
- 配置 V8 old-generation heap 上限；
- 完成、异常、超时、中止和 worker death 都解析成 `CodeRunResult.error`，不是 Promise rejection。

但源码明确把它定义为 containment，而非 security boundary：模型代码具有 Bash 等级的信任。Worker/AsyncFunction 隔离的价值是：

- 同步死循环不能阻塞 Host event loop；
- compute budget 可按 Worker ELU active time测量；
- wall-clock ceiling 处理永不 resolve 的 await；
- abort/timeout 可以 `worker.terminate()` 硬停止；
- 每次 run 无跨运行状态。

### 2. Code Runtime 的 hostile-peer 防御

Worker 中运行的是模型代码，因此 Host 不相信 TypeScript protocol 类型。每条 worker message 都逐字段重建：

- 未知 tag、错误字段类型和 poisoned object 直接丢弃；
- duplicate call id 忽略；
- binding 使用 own-property lookup，`constructor`/`__proto__` 不会走原型链；
- 参数和返回值双向要求 lossless JSON；
- namespace 对象使用 null prototype；
- binding rejection 可投影为程序内真实自定义 Error class。

Worker JSON snapshot 特意捕获 intrinsic 方法引用，避免模型修改 `Object.keys`、`Array.isArray`、`Set.prototype` 等全局后影响边界校验。

### 3. Code Runtime 的双预算

默认配置：

```text
computeMs = 60s busy time
maxWallMs = 600s wall time
maxOutputBytes = 64MiB
maxOldGenerationSizeMb = 512
```

Compute 使用 Worker 自己的 `eventLoopUtilization().active`：等待慢工具不消耗 compute，热循环无论是否挂着异步调用都会消耗。Host 每 25ms 采样，这是内部精度而非部署调优项。

日志数组、完成值和失败信息共享一个 JSON 字节账本；超限时返回显式 `output-limit`，保留可装下的日志前缀，不以截断字符串冒充合法完整返回值。

### 4. Workflow 是“脚本编排 Subagent”，不是通用 Host 代码入口

Workflow 脚本位于每 run 一个 Worker 的 `vm.Context` 中，暴露：

```text
agent()
parallel()
pipeline()
phase()
log()
args
```

它同样是可逃逸 containment，不是 security boundary。Worker 的意义是防 Host 阻塞和允许有界强制终止。

启动前同步校验：

- meta；
- script parse；
- provider 存在；
- per-run maxTotalAgents 不超过部署 ceiling。

返回 `WorkflowRun` 后，脚本错误不 reject result，而是 `stopReason: error/cancelled`。

### 5. Workflow 的资源上限和 fatal discipline

默认：

- 并发 agent 数：`min(16, max(1, cores - 2))`；
- 总 agent 调用：1000；
- 单次 parallel/pipeline item：4096；
- 同步 vm slice：5s；
- cancel/dispose grace：5s。

错误参数、未知/deferred option、非法 schema、cap、provider start failure 和 cancellation 都是 fatal WorkflowError。`parallel()`/`pipeline()` 不把 fatal 错误吞成单项 null；只有正常 child failure 或普通阶段失败允许局部结果。

### 6. Workflow Host 保证 child lifecycle 配对

Worker 与 Host 通过显式消息协议桥接 child start/result/dispose。Host 维护：

- pending provider starts；
- published children；
- per child memoized disposal；
- `workflow/agent-start` 未配对账本；
- terminal first-wins 状态。

Cancel 会：

1. 通知 worker；
2. abort 所有 child start/run；
3. 启动 grace timer；
4. grace 后合成缺失 `agent-end(cancelled)`；
5. 强制结算 workflow cancelled；
6. terminate worker。

`dispose()` 立即开始 child disposal，使 child cleanup 与 worker grace 重叠；到期后 worker 一定结束，慢 child cleanup 按约定可能被放弃而不无限阻塞调用方。

### 7. One-shot Subagent 的共享驱动

Spawn 与 Fork provider 都调用 `subagent-in-process-driver`：

- spawn 无 seed；
- fork 使用父日志最后 completed-turn prefix；
- 创建前捕获父级权限旋钮和模型设置；
- child scope 应用 persona/tool restriction；
- 可选 structured-output tool；
- descriptor 在 child 首个已接受 pre-step、首请求前写入；
- prompt 作为普通 user message；
- 等待 child idle；
- 从 activation boundary 后的事件读取最终 assistant 与 stop reason。

One-shot 只拥有一个结果；取消通过 parent cause 进入 child Agent，dispose 同时等待 handle 和 result settlement。

### 8. Continuable Subagent 是持久 Session + 临时 Activation

`SubagentContinuationManager` 的核心状态：

```text
persisted child Session
  <-> zero or one live Activation
       -> AgentHandle
       -> ownedChildren
       -> ancestry set
       -> Agent inbox FIFO
```

Fresh start：

- 预留 child ID；
- 生成 versioned descriptor；
- provider 只贡献可选 seed；
- private activation-owner Context 创建 child；
- 提交 initial prompt；
- inbox 接受后返回 childId/messageId，不等待 turn。

Followup：

- resident running/waiting Activation 直接入队；
- disposal cutoff 后等待并重试；
- 无 Activation 时从 persistence cold resume；
- 每 child lock 串行 materialize/delivery。

### 9. Continuation 权限与清理

权限使用对象身份和 durable direct parent，不使用来源文本：

- followup 要求 exact live direct parent；
- interrupt 的 ancestor 必须是 exact live Agent 且存在于 ancestry；
- user interrupt 必须提供正确 parentSessionId；
- report sender 必须是 resident Activation 的 exact child Agent；
- report recipient 从 child header 推导，调用方不能指定。

Manager-wide drain 先同步关闭 admission，再等待已准入 materialization，最后按动态森林 child-first dispose。Scoped drain 只关闭指定 Host root 的后代树，不影响其他 Agent 森林。这个动态顺序无法仅靠 Cordis effect 逆序表达，因此 manager 自己维护图。

## 第六轮：设置、凭据、存储、工作区、附件与会话读模型

### 1. Settings 是三层解析，不替代 Cordis 配置

每 namespace 的生效值：

```text
schema defaults
  <- composition base
  <- user document section
```

Owner 注册 schema、base、live/restart 提示和额外 validate。写入按 namespace 串行，候选先验证、provider persist 后再成为权威；外部坏编辑保留 last-good 并 warning。

`update` 稀疏合并，`replace` 整节重置，`mutate` 做路径 set/unset。Web 使用 `expectedRevision` 防 stale write。脱敏 describe 删除 secret 值并只返回 `{path,set}`，所以持有红acted view 的 Client 必须 mutate，不能重建整节 replace，否则会删除从未看见的 secret。

### 2. Credentials 与 Settings 的秘密边界

Settings/cordis.yml 保存 CredentialRef（环境变量名），不保存值。Credential provider 每操作 resolve，LLM 下一请求即可看到轮换后的 key。

Local source 优先级由 provider 定义，常见为环境、managed file、项目/用户 `.env`。环境值遮蔽 managed store 时 `writable:false`，set/unset 拒绝，避免“写入成功但解析仍被环境遮蔽”的假象。所有 describe/list API 不返回 secret value。

Credential records 是另一套 key space，用于 OAuth/authorization grant；`modifyRecord` 是跨进程安全的 read-decide-replace 唯一路径，防并发 refresh token 覆盖。

### 3. Storage Hub、Backend 和 Domain 三层

- `ctx.storage`：名称到 backend、form 到 facility 的无 IO hub；
- JSON/SQLite backend：拥有介质与 KV facet；
- `ctx.storageDomain`：领域 schema、路由、内存态和写链；
- Workspace/Feedback 等业务只使用 Domain，不碰 backend。

Domain 写入顺序：backend durable -> 更新内存 -> `domain/changed`。失败时内存不变。每 domain 单写链让 `update(key, fn)` 成为原子 read-modify-write。版本不匹配和坏介质 fail loud，不做预发布迁移。

### 4. Workspace 的身份不是路径

WorkspaceId 是 UUID；path 经 realpath 规范化并保持唯一。Membership 要同时满足：

- workspace 有序 account 含 SessionId；
- live/persisted SessionHeader.cwd 规范化后等于 workspace path。

Registry create/delete 涉及 global order + table 两次写，因此先写 pending marker；崩溃恢复时 interrupted create 回滚，interrupted delete 补完。删除 Workspace 只删注册，不删目录和 Session log，Session 变为 Ungrouped。

### 5. Attachment 采用先持久化、后事件

图片字节不进入 Session log。Local store：

- 解码验证真实媒体类型/尺寸；
- 应用源图片和规范化限制；
- 保存内容寻址对象；
- 返回不可变 ref；
- UserMessage 只存 ref。

批量图片在保存任一成员前先验证全部，防部分发布。读取按 ref 重新验证 digest、媒体和尺寸。Provider-specific request version 以 attachment + policy + encoder 参数生成 variantId，共享并发变换、每 waiter 可独立取消。

当前缺少引用 GC，因为 fork/resume 可共享对象，不能随单 Session 删除附件。

### 6. Session Projection 是 Host 纯 fold 单元

领域注册同步 `init/apply/view` 单元；Registry 唯一订阅 Session event。`apply` 对无关事件必须返回相同引用，`Object.is` 决定是否产生下游 frame。

Projection cache 保存 `(key, stateVersion, seq, state)`；冷读取优先 cache + `readFrom` tail，log shrink/版本变化时回退完整 fold。Cache fail-soft，因为它是加速层，不是真源。

### 7. Session Query 的 live-preferred 语料库

同 ID 同时 live/persisted 时，查询优先 live。它提供：

- 完整日志/surface/title读取；
- session/event metadata filter；
- semantic text extraction；
- lineage；
- source/replacement event trace；
- bounded event window；
- 可选 SQLite FTS。

FTS query 被当数据，不直接接受可执行 FTS 语法；cursor 与 normalized query/filter/generation 绑定，索引变化会产生 stale cursor。

### 8. Session Title 是日志状态而非列表字段

`session/title` 记录 normalized title、输入 user-message seq 和 source。用户 rename source 会 pin，自动 provider 不再覆盖；explicit refresh 是有意 unpin/retry。

LLM title 请求在 dispatch 前完整记录 route/system/messages/maxTokens，即使生成失败也可重建。异步 provider 结果必须只引用 request 给出的 message seq，接纳层再执行 title normalization/byte cap。

### 9. Telemetry 的默认事实

Telemetry 只捕获导出副本，不改模型或 Session log。它镜像除多数 assistant chunk 外的 ledger，并记录 agent-error/shutdown ops。每 step 只保留第一 chunk，seq gap 是正常投影。

`session-telemetry/record` 是同步 redaction waterfall，但 seam 自身没有任何脱敏规则；无 listener 时原始副本到 backend。因此实际隐私取决于部署规则。交接是 best-effort，可能重复或丢失，接收端按 `(session.id,event.seq)` 去重 ledger。

## 第七轮：Skills、LSP、Web、Commands、Goal、Plan、Schedule 与扩展

### 1. Skills 是 scope-aware 发现目录

Provider 在 global/preset scope 分层注册；读取合并 scope chain。单层重名按 rank、provider order、local order；近 scope 同名直接遮蔽祖先。

Snapshot 带 `complete`：任何 provider 失败/目录并发变化会返回可用但不完整候选，不缓存；Consumer 保留 last-good。完整正文不缓存，每次 skill() 重新读取；目录只在 metadata 变化时更新，正文修改不改变 prompt catalog。

调用策略独立为 modelInvocable/userInvocable。目录消息是持久 user-role context，digest 变化才发布完整替换；压缩隐藏后会重新建立。

### 2. LSP 只暴露四项标准化查询

`goToDefinition/findReferences/goToImplementation/hover` 是 closed union。Provider 按小写扩展名原子注册，冲突全量回滚；seam 不暴露任意 JSON-RPC escape hatch。坐标使用 LSP 0-based UTF-16，Tool 转成模型 1-based。

Stdio provider 通过 subprocess raw pipe 承载协议；进程、文档同步和 malformed response 留在 provider，Consumer 只看标准 locations/hover。

### 3. Web Search/Fetch 选择不依赖注册顺序

显式 provider id 优先；无配置时恰好一个 available 才自动选，多于一个报 ambiguous。`available()` 禁止联网，只判断本地配置。

Fetch 非 2xx 仍是成功结果，因为状态码是资源事实。HTTP provider 限 URL/redirect/bytes/chars/time，但明确**不阻止私网目标**；能访问敏感内网的部署不应启用模型 `web_fetch`。官方 base 只启用 search，fetch 工具关闭且不挂 provider。

### 4. Commands 不进入模型轮次

Slash command 命中后直接执行 handler，并记录 `command/run`/`command/done`，不创建模型 user message。Unknown/syntax miss 不落账。命令可声明图片；附件同样先批量持久化。`sourceEventSeq` 可把 command lifecycle 与领域事件关联，不靠邻接或解析文本。

### 5. Goal 使用 revision CAS

Goal 每次 mutation 写完整 snapshot 或 clear tombstone，`GoalRef{id,revision}` 防 stale UI/并发调用。Durable phase 与 process-local activation 分开；只有正式获准的 goal-round user message 推进 round count。Pause/block/complete/disarm 不混为一种状态。

### 6. Plan Mode 是软指引，不是权限边界

Plan state 来自 `plan/mode` fold；系统指引只在 active 时出现。Tool catalog 保持稳定，`exit_plan_mode` 始终注册以保护 request cache。

Open turn 中的选择要到下一个 accepted pre-step 才提交，确保本步骤其余工具仍受旧模式；between turns 可立即提交。Plan 不限制文件或 shell，真正 enforcement 仍是 Sandbox/Approval。

### 7. Schedule 是 live Session 内至少一次提醒

Schedule 唯一真源是 `schedule/change`。after/at/every 规范成 UTC；本地时间必须显式 zone，DST gap 拒绝、overlap 取较早实例。Every 固定速率、至少 5 分钟，busy/cold 漏过多个周期只投递最新到期一次。

到期后等待 Agent idle maintenance，普通 `followup()` 入队，再记录 dispatch。Queue 已接纳但 dispatch event 前崩溃可能重复，所以是 best-effort at-least-once，不是 exactly-once。Cold Session 不运行 timer，恢复后 overdue。

### 8. Spill 是工具结果的 best-effort 外置

Policy 在 `tools/post-execute` 保存超大纯文本，模型结果变为 head/tail preview + locator/hint。Local store 使用 session hash 目录、随机排他 0600 文件。保存失败保留原 inline 成功结果，不把工具变成 error。

### 9. Dynamic Cordis Extension 是受控的自修改平面

Agent 定义 immutable Package versions，Host/Client 两半分别加载。Plugin/Package/Run ID 防 stale Client 调用；Client half 需要用户批准，授权可选择覆盖未来版本。Host runner 提供 source-free inventory、exact package inspect、active run invoke 和 render failure reporting。

这是高权限能力：Package source 本质为运行时代码；安全依赖 Session ownership、run identity、Client approval 与 VM/guard，而不是把源码当普通 JSON 配置。

## 第八轮：ACP、MCP、Hooks、Terminal、UI、Native、Python 与工程流程

### 1. ACP 是自动化传输，不是完整 UI 协议

ACP 只支持新 Session、prompt/cancel、committed assistant content 和一次性 permission。每 Session 只允许一个在途 prompt，并等待 admission、Agent idle 和有序输出交付完全停稳。

它故意不发 raw chunks/reasoning/tool progress，避免 retry/未提交输出泄漏给自动化调用方。图片能力只有 attachment store 存在且 exact route 明确支持 image 时才公告。连接 dispose 会关闭准入、取消 prompt、drain 其 roots 的 continuable descendants，再释放 handles。

### 2. MCP 只桥接 Tools

每 server 一个插件实例，支持 stdio/streamable-http。公开名是 `(serverName,rawName)` 的确定性规范化；64 字符外追加 12 hex hash 防碰撞。工具 generation 全量替换，冲突时回滚，不留下半集合。

MCP 规范 JSON value 保留完整 content/structuredContent；Native projection 将支持图片先持久化。Audio/embedded/未知块转诊断文本，不静默丢失。重连指数退避并有失败预算；旧 generation 在中断期间保持注册但调用失败。

### 3. Hook Bridges 把 Claude/Codex 方言映射到规范事件

共享 hook-protocol 负责 matcher、shell runner、stdout/stderr codec、decision merge、`hook/invoked/result` 审计和 detached drain。Claude/Codex 插件只负责事件名、payload 和各自配置解析。

Shell hook 走 ctx.shell，因此继承环境 scrub、timeout、process-tree cleanup。多个 hook outcome 以明确 rank 合并；阻塞与 permission decision 不靠 exit text 猜测。SessionStart 位于 turn 外，因此不写 hook audit pair，其他 pre/post/stop 位于所属 turn。

### 4. Terminal 是进程内 PTY Registry

Terminal backend 负责真实 PTY/readiness/scrollback；Service 负责 backend type、exact Agent ownership、ID 和 cleanup。每 session 同时一个 send；wait reason 与顶层 session status 正交。原始 PTY 状态不持久，模型 input/output 仍经 tool call/result 和 jobs 记录。

### 5. Client UI 使用 Slots + Conversation Definitions

UI 没有中央业务组件 switch：

- `SlotMap` declaration merging 定义位置；
- SlotCore 验证声明/ownership/store；
- keyed/chain slot 选择业务 renderer；
- ConversationNodeDefinition 将 event family 组装成稳定 node；
- `ui-tool` 递归渲染 root/code subcall，再按 tool name keyed dispatch；
- unknown node/intent/context form 使用 generic/opaque fallback。

ui-renderer 是唯一 React root/hydrate 入口；ui-layout 拥有三栏瞬态几何；ui-conversation 拥有 composer/chat shell；其他 30+ `ui-*` 包只占 slot 或注册 node/view。Appendix 的 client group 描述列出了每个插件职责。

### 6. Native Landlock Run

约 300 行 C11、musl 静态链接。Launcher 在自身安装 ruleset 后 exec command，规则跨 execve 继承；Host 本身不被限制。Probe 返回 full/partial/unusable；失败时不执行命令。发布拆成 entry + linux-x64 + linux-arm64 可选包，无本地编译 fallback。

Exit 125 不能单独证明 launcher failure，因为 child 也可能返回 125；消费方必须同时匹配 launcher fatal diagnostic，这与 bash-sandbox classifier 一致。

### 7. Python SDK

Python 高层 `DeepSeekHarness` 懒启动 JSON-RPC runtime，可复用多个 Session。`Session.run()` 先等待该 MessageId 的 durable inbox splice receipt，再收集事件直到 root Session idle；最终响应从最后 assistant/message 文本派生，finish reason 从最后 turn/end 严格读取。

Runtime carrier：

- production：linux/macos x64/arm64 single exe + ripgrep sidecar，macOS 加 node-pty helper；
- dev-only：显式 `DSH_RUNTIME_MODE=node` 使用完整 node_modules closure；
- 自动模式只选 exe，绝不悄悄使用源码 Node carrier；
- runtime 始终要求显式 Cordis config，Python client 在零配置 bundled launch 时注入 checked-in default。

### 8. Build、Release 和 Gate Graph

完整 build 固定顺序：Host tsc/tsdown -> Client tsc/tsdown -> Web。Typert 只在 Host tsdown 运行，并生成 Client 后续消费的 Remote contract。Build 记录公开 client 环境和 commit hash，release/built-Web 会拒绝缺失或被局部构建污染的记录。

`run-gates.ts` 将门禁建模为 DAG：`needs` 要求依赖 passed，`after` 只要求 settled；启动前检查 unknown edge/cycle，失败依赖导致 skip。Local heavy modes并发上限 4，CI按 CPU/模式分配。

Release family 计算 workspace publish graph、打包 tarball、验证 identity/order、built install，再按依赖顺序发布；npm transient code 有有限重试。构建产物、NodeNext consumer、publint 和 built-bin smoke 是独立门禁，不用源码测试替代。

### 9. 测试体系收口

- Unit：源码 paths，注册表 HMR、错误/并发/事件顺序；
- Coverage：`packages/*/*/src` 按文件 100%；
- E2E：真实 provider，自无 key 跳过；
- Snapshot：ACP/headless 持久协议和 transcript；
- Web：Chromium replay 快照；
- Built smoke：plain Node + package exports + worker/bin；
- Python single-exe snapshot：独立投影；
- Docs：类型等价、Cordis catalog、工具/config/persistence catalog、图、链接、翻译 pairing。

本研究未执行这些命令，因此动态正确性仍以项目 CI 为准。

## 全仓静态研究完成度声明

截至本节，已完成：

1. **227 个 `packages/*/*` 包全部枚举**，见下方自动盘点；
2. 所有 package group 的职责、依赖角色和主要 entrypoint 已覆盖；
3. 核心、持久化、执行安全、Web/Client、Subagent/Workflow/Code Runtime 做了实现级深读；
4. 其余领域按 Service Definition、Provider、Consumer、事件/持久化和限制做了包级静态复核；
5. Native、Python、构建、发布与测试平面已纳入；
6. 源码仓库未修改。

这里的“全部完成”定义为**全仓包级静态架构与主要实现路径研究完成**，而不是声称人工逐字解释 60 万行代码，也不等于动态验证。逐行证明必须依赖构建、100% coverage、E2E、Web/SDK snapshot 和平台矩阵；本次未安装依赖或运行它们。

## 全仓逐包源码覆盖清单（自动静态盘点）

本清单枚举 `packages/*/*/package.json` 的全部工作区包。`源码` 统计 `src/` 下 TS/TSX/JS/MJS/C/C++/Rust 文件；`测试` 统计包内 spec/e2e/snapshot 文件。它用于证明包级覆盖范围和定位后续复核入口，不等同于动态行为验证。

- 包总数：**227**
- 源码文件：**1381**
- 源码行：**244,956**
- 包内测试文件：**857**

### 分组统计

| 分组 | 包 | 源码文件 | 源码行 | 测试 |
| --- | ---: | ---: | ---: | ---: |
| `acp` | 1 | 4 | 847 | 8 |
| `api` | 2 | 10 | 1,861 | 4 |
| `attachment` | 2 | 14 | 1,655 | 9 |
| `boot` | 2 | 5 | 1,481 | 7 |
| `bundle` | 3 | 8 | 683 | 7 |
| `client` | 40 | 517 | 74,897 | 252 |
| `code-runtime` | 3 | 13 | 2,696 | 10 |
| `compaction` | 4 | 18 | 2,880 | 12 |
| `context` | 6 | 27 | 4,092 | 13 |
| `core` | 8 | 44 | 13,497 | 59 |
| `credentials` | 3 | 8 | 1,989 | 10 |
| `e2b` | 3 | 11 | 2,659 | 5 |
| `examples` | 3 | 10 | 619 | 6 |
| `experimental` | 2 | 16 | 2,763 | 5 |
| `extensions` | 4 | 44 | 16,710 | 14 |
| `feedback` | 2 | 6 | 785 | 5 |
| `fs` | 7 | 35 | 5,804 | 22 |
| `goal` | 4 | 16 | 2,589 | 8 |
| `guard` | 2 | 4 | 374 | 2 |
| `hooks` | 3 | 15 | 1,813 | 18 |
| `host` | 8 | 66 | 10,685 | 34 |
| `identity` | 1 | 2 | 131 | 2 |
| `interaction` | 5 | 16 | 2,060 | 9 |
| `jobs` | 3 | 8 | 1,423 | 5 |
| `llm` | 5 | 52 | 11,020 | 43 |
| `lsp` | 3 | 17 | 2,486 | 14 |
| `mcp` | 1 | 5 | 1,171 | 5 |
| `plan` | 1 | 4 | 601 | 4 |
| `preset` | 2 | 11 | 1,783 | 9 |
| `runtime-diagnostics` | 1 | 2 | 230 | 1 |
| `sandbox` | 4 | 22 | 3,904 | 25 |
| `schedule` | 1 | 8 | 2,003 | 7 |
| `sdk` | 3 | 13 | 1,760 | 7 |
| `session` | 13 | 49 | 9,519 | 35 |
| `session-query` | 4 | 30 | 5,295 | 15 |
| `settings` | 2 | 6 | 1,507 | 8 |
| `shell` | 10 | 29 | 4,321 | 23 |
| `skill` | 4 | 8 | 2,520 | 5 |
| `spill` | 3 | 9 | 664 | 3 |
| `storage` | 4 | 20 | 2,088 | 6 |
| `subagent` | 11 | 46 | 9,573 | 36 |
| `subprocess` | 2 | 9 | 2,220 | 7 |
| `terminal` | 3 | 11 | 2,405 | 9 |
| `test-support` | 6 | 27 | 7,093 | 15 |
| `todo` | 1 | 4 | 329 | 5 |
| `typert` | 4 | 19 | 8,433 | 12 |
| `util` | 7 | 14 | 1,305 | 7 |
| `web` | 6 | 24 | 3,010 | 14 |
| `workflow` | 4 | 19 | 3,581 | 14 |
| `workspace` | 1 | 6 | 1,142 | 2 |

### 逐包清单

| 分组 | npm 包 | 路径 | 源码 | 行数 | 测试 | 标记 | 职责描述 |
| --- | --- | --- | ---: | ---: | ---: | --- | --- |
| `acp` | `@deepseek-ai/dsh-acp` | `packages/acp/acp` | 4 | 847 | 8 | - | Automation-only Agent Client Protocol server for driving DeepSeek Harness agents over JSON-RPC stdio |
| `api` | `@deepseek-ai/dsh-api-gateway` | `packages/api/gateway` | 4 | 1,403 | 2 | client | Typert Remote Host dispatcher and Client API endpoint |
| `api` | `@deepseek-ai/dsh-api-remotes` | `packages/api/remotes` | 6 | 458 | 2 | client | Remote BFF assembly and Host Agent/Session lookup policy |
| `attachment` | `@deepseek-ai/dsh-attachment` | `packages/attachment/attachment` | 6 | 385 | 2 | - | Durable immutable attachment storage seam for the DeepSeek Harness |
| `attachment` | `@deepseek-ai/dsh-attachment-local` | `packages/attachment/attachment-local` | 8 | 1,270 | 7 | - | Private content-addressed DSH_HOME attachment storage |
| `boot` | `@deepseek-ai/dsh-app-boot` | `packages/boot/app-boot` | 3 | 1,279 | 6 | - | Shared boot glue for the app bins: .env loading, fail-loud Loader guards, snapshot-aware config resolution, and the Loader boot sequence |
| `boot` | `@deepseek-ai/dsh-cmdline` | `packages/boot/cmdline` | 2 | 202 | 1 | - | Immutable command-line handoff from a dsh launcher to any app plugin that injects cmdlineArgs |
| `bundle` | `@deepseek-ai/dsh-base` | `packages/bundle/base` | 2 | 37 | 1 | bundle | The shared dsh core as a profile bundle: every profile's first patch layer, inserting the base plugin rows over the empty profile root |
| `bundle` | `@deepseek-ai/dsh-headless` | `packages/bundle/headless` | 3 | 237 | 2 | bundle | The dsh one-shot bundle: a direct core Agent/Session runner over dsh-base with no Host, HTTP, or browser layer |
| `bundle` | `@deepseek-ai/dsh-web-app` | `packages/bundle/web-app` | 3 | 409 | 4 | bundle | The dsh browser-surface bundle: the web patch layer over dsh-base plus the runtime glue plugin (frontend dist serving, web-surface prompt, bash runtime variables, URL line) |
| `client` | `@deepseek-ai/dsh-client-connection` | `packages/client/connection` | 16 | 4,822 | 10 | client | Wire consumer layer: HTTP-up/WebSocket-down client, ConnectionController dual streams with reconnect, and fixture api |
| `client` | `@deepseek-ai/dsh-client-hmr` | `packages/client/hmr` | 4 | 447 | 1 | client | Dev-only hot-reload driver for script-loaded client entries: SSE rebuilt frames → invalidate/prefetch → fiber swap through the vendored Loader entry |
| `client` | `@deepseek-ai/dsh-client-locale` | `packages/client/locale` | 11 | 716 | 7 | client | Locale plugin: Host-backed zh/en preference, browser-derived fallback, locale snapshots, and typed namespace dictionaries |
| `client` | `@deepseek-ai/dsh-client-modules` | `packages/client/modules` | 5 | 1,201 | 2 | client | Client module system, dual-face: node half composes the __DSH_BOOT__ entry graph (incremental dsh.client scan, bundle route, index tap, webPlugins service); browser half is the lazy-CJS module table the vendored cordis Loader consumes as its internal seam |
| `client` | `@deepseek-ai/dsh-client-runtime` | `packages/client/runtime` | 43 | 9,032 | 24 | client | Client core services: SlotRegistry, SessionRuntime (scope tree + object layer) |
| `client` | `@deepseek-ai/dsh-client-ui-agent-preset` | `packages/client/ui-agent-preset` | 13 | 2,071 | 7 | client | Agent-preset surfaces: the default for later sessions, this session's seat, and the composition editor |
| `client` | `@deepseek-ai/dsh-client-ui-attachment` | `packages/client/ui-attachment` | 11 | 709 | 7 | client | Dynamic attachment presentation plugin for conversation input and message-image slots |
| `client` | `@deepseek-ai/dsh-client-ui-brand-official` | `packages/client/ui-brand-official` | 4 | 82 | 2 | client | Official DeepSeek Harness brand occupants for the Web client's sidebar and conversation Hero slots |
| `client` | `@deepseek-ai/dsh-client-ui-commands` | `packages/client/ui-commands` | 10 | 1,366 | 5 | client | Client command surface: global directory cache, '/' source, three command UI kinds, popupSelect registry |
| `client` | `@deepseek-ai/dsh-client-ui-conversation` | `packages/client/ui-conversation` | 72 | 11,974 | 29 | client | Conversation domain: skeleton, ordered chat flow, composer with the Host-backed busy-Enter preference, and details host |
| `client` | `@deepseek-ai/dsh-client-ui-deliverables` | `packages/client/ui-deliverables` | 7 | 493 | 2 | client | Produced-files turn tail and clickable final-response file references for Web |
| `client` | `@deepseek-ai/dsh-client-ui-directory-picker-browse` | `packages/client/ui-directory-picker-browse` | 6 | 1,224 | 2 | client | In-app directory browsing surface: the workspace directory-flow owner rendering the host's listing and creation primitives |
| `client` | `@deepseek-ai/dsh-client-ui-directory-picker-native` | `packages/client/ui-directory-picker-native` | 4 | 146 | 1 | client | Native directory-picker surface: the renderless workspace directory-flow occupant driving the host's OS chooser |
| `client` | `@deepseek-ai/dsh-client-ui-goal` | `packages/client/ui-goal` | 9 | 501 | 3 | client | Session goal surface: GoalBar docked above the composer, read from the goal session projection |
| `client` | `@deepseek-ai/dsh-client-ui-input-trigger` | `packages/client/ui-input-trigger` | 14 | 1,454 | 5 | client | Input trigger pipeline: '/' and '@' detection, candidate menu, pick routing to registered sources |
| `client` | `@deepseek-ai/dsh-client-ui-jobs` | `packages/client/ui-jobs` | 6 | 315 | 2 | client | Session-header background-job list: live registry state mirrored from session/jobs frames |
| `client` | `@deepseek-ai/dsh-client-ui-layout` | `packages/client/ui-layout` | 9 | 678 | 6 | client | Shell plugin: three-column AppFrame with drag handles, ctx.layout viewing-state service (navigation + panels) |
| `client` | `@deepseek-ai/dsh-client-ui-message-feedback` | `packages/client/ui-message-feedback` | 8 | 942 | 4 | client | Per-message feedback controls contributed to the assistant-message action strip, backed by the messageFeedback Host Remote |
| `client` | `@deepseek-ai/dsh-client-ui-model-selection` | `packages/client/ui-model-selection` | 9 | 940 | 2 | client | Model selection: the /model popupSelect over session.models / session.selectModel |
| `client` | `@deepseek-ai/dsh-client-ui-permission-presets` | `packages/client/ui-permission-presets` | 8 | 627 | 3 | client | Permission surfaces: a new-session default in General settings and a current-session /permission popup over the permissions projection |
| `client` | `@deepseek-ai/dsh-client-ui-plan` | `packages/client/ui-plan` | 6 | 202 | 2 | client | Plan-mode composer control: the conversation.input.plan seat over the plan projection and the /plan command channel |
| `client` | `@deepseek-ai/dsh-client-ui-primitives` | `packages/client/ui-primitives` | 46 | 6,870 | 22 | - | Pure React atoms for the dsh web UI: controls, icons, markdown, and JSON inspectors (zero cordis) |
| `client` | `@deepseek-ai/dsh-client-ui-reference` | `packages/client/ui-reference` | 4 | 212 | 1 | client | Unified Web @file and @session reference source |
| `client` | `@deepseek-ai/dsh-client-ui-renderer` | `packages/client/ui-renderer` | 9 | 1,291 | 9 | client | Browser UI renderer: React slot bindings, ctx.uiRenderer, and the assembled application root |
| `client` | `@deepseek-ai/dsh-client-ui-settings` | `packages/client/ui-settings` | 8 | 910 | 5 | client | Settings domain base plugin: the settings-namespace scope service and the canonical settings slot-type contract |
| `client` | `@deepseek-ai/dsh-client-ui-settings-general` | `packages/client/ui-settings-general` | 11 | 718 | 7 | client | Settings ownerless-copy and product onboarding plugin: the General section, shell trigger/header chrome content, settings dictionaries, and the versioned welcome notice |
| `client` | `@deepseek-ai/dsh-client-ui-settings-models` | `packages/client/ui-settings-models` | 19 | 3,452 | 10 | client | Models settings and shared product-onboarding dialogs over existing settings and credential joins |
| `client` | `@deepseek-ai/dsh-client-ui-settings-plugin-inventory` | `packages/client/ui-settings-plugin-inventory` | 6 | 322 | 3 | client | Read-only Cordis Loader inventory tab in Web Plugins settings |
| `client` | `@deepseek-ai/dsh-client-ui-settings-plugins` | `packages/client/ui-settings-plugins` | 18 | 1,672 | 5 | client | Plugins settings section with feature-owned tabs and configurable host-plane plugin cards |
| `client` | `@deepseek-ai/dsh-client-ui-sidebar` | `packages/client/ui-sidebar` | 7 | 448 | 8 | client | Sidebar plugin: session multi-level tree, search, grouping, state dots |
| `client` | `@deepseek-ai/dsh-client-ui-skill` | `packages/client/ui-skill` | 6 | 434 | 2 | client | Web skill references and the dedicated skill tool row |
| `client` | `@deepseek-ai/dsh-client-ui-slots` | `packages/client/ui-slots` | 4 | 1,563 | 4 | - | Slot registry pure core: SlotMap declaration merging, single register composition API, four-share props types, store-seat types, renderer install seam |
| `client` | `@deepseek-ai/dsh-client-ui-subagent` | `packages/client/ui-subagent` | 7 | 1,090 | 2 | client | Subagent conversation catalog, continuation routing UI, and '@' reference source |
| `client` | `@deepseek-ai/dsh-client-ui-theme` | `packages/client/ui-theme` | 10 | 726 | 9 | client | Theme plugin: Host bootstrap for the pre-plugin palette; DOM-free ThemeRuntime for light/dark/system state; --dsw-* token styles and Appearance settings row |
| `client` | `@deepseek-ai/dsh-client-ui-tool` | `packages/client/ui-tool` | 25 | 2,334 | 15 | client | Client Tool call-tree renderer and keyed per-tool presentation slot |
| `client` | `@deepseek-ai/dsh-client-ui-trajectory` | `packages/client/ui-trajectory` | 28 | 7,901 | 8 | client | Trajectory event ledger with an interactive timing overview: pure-consumer plugin registering into the conversation ViewMap (no service) |
| `client` | `@deepseek-ai/dsh-client-ui-user-questions` | `packages/client/ui-user-questions` | 8 | 811 | 4 | client | Web ask_user_question feature: host tool mount plus composer-takeover question UI |
| `client` | `@deepseek-ai/dsh-client-ui-workflow-run` | `packages/client/ui-workflow-run` | 7 | 777 | 1 | client | Durable workflow-run Conversation Node and nested member disclosure for dsh web |
| `client` | `@deepseek-ai/dsh-client-ui-workspace` | `packages/client/ui-workspace` | 11 | 3,023 | 8 | client | Workspace picker plugin: one WorkspacePicker registered into the sidebar and empty-state workspace slots |
| `client` | `@deepseek-ai/dsh-client-web` | `packages/client/web` | 8 | 401 | 3 | - | Web boot kernel: static module table, Cordis loader, framework-free boot page, and UI-renderer handoff |
| `code-runtime` | `@deepseek-ai/dsh-code-runtime` | `packages/code-runtime/code-runtime` | 3 | 294 | 2 | - | Abstract code-execution seam (ctx.codeRuntime) for the DeepSeek Harness |
| `code-runtime` | `@deepseek-ai/dsh-code-runtime-python` | `packages/code-runtime/code-runtime-python` | 3 | 706 | 2 | - | CPython subprocess implementation of the DeepSeek Harness code-execution seam |
| `code-runtime` | `@deepseek-ai/dsh-code-runtime-worker-thread` | `packages/code-runtime/code-runtime-worker-thread` | 7 | 1,696 | 6 | - | Worker-thread implementation of the DeepSeek Harness code-execution seam |
| `compaction` | `@deepseek-ai/dsh-command-compact` | `packages/compaction/command-compact` | 2 | 136 | 3 | - | Human-facing slash command for explicit session compaction |
| `compaction` | `@deepseek-ai/dsh-compaction` | `packages/compaction/compaction` | 6 | 792 | 3 | - | Abstract compaction service seam (ctx.compaction) for the DeepSeek Harness |
| `compaction` | `@deepseek-ai/dsh-compaction-basic` | `packages/compaction/compaction-basic` | 6 | 1,621 | 4 | - | Token-meter-driven compaction policy and LLM summarization backend for the DeepSeek Harness |
| `compaction` | `@deepseek-ai/dsh-compaction-tool-result-pruner` | `packages/compaction/compaction-tool-result-pruner` | 4 | 331 | 2 | - | Replay-safe model-free head/middle/tail pruning for tool-result surface nodes |
| `context` | `@deepseek-ai/dsh-agent-instructions` | `packages/context/agent-instructions` | 7 | 1,863 | 2 | - | Workspace context loader for AGENTS.md/CLAUDE.md instruction files |
| `context` | `@deepseek-ai/dsh-file-reference` | `packages/context/file-reference` | 4 | 161 | 2 | - | File-reference discovery contract and shared @file grammar |
| `context` | `@deepseek-ai/dsh-file-reference-local` | `packages/context/file-reference-local` | 3 | 464 | 3 | - | Local-filesystem ctx.fileReferences provider with bounded fuzzy indexes |
| `context` | `@deepseek-ai/dsh-session-reference` | `packages/context/session-reference` | 7 | 807 | 1 | - | Cross-session snapshot references and durable untrusted model context (ctx.sessionReferenceResolver) |
| `context` | `@deepseek-ai/dsh-time-context` | `packages/context/time-context` | 4 | 520 | 4 | - | Opt-in durable per-step context with the current time and elapsed time |
| `context` | `@deepseek-ai/dsh-tmux-context` | `packages/context/tmux-context` | 2 | 277 | 1 | - | Opt-in durable per-step context with this agent's tmux pane and window location |
| `core` | `@deepseek-ai/dsh-agent` | `packages/core/agent` | 8 | 1,636 | 6 | - | Agent interface, registry, initiator scope, and event vocabulary for the DeepSeek Harness |
| `core` | `@deepseek-ai/dsh-agent-default-model` | `packages/core/agent-default-model` | 2 | 137 | 1 | - | Default model selection shared by Agent entry points |
| `core` | `@deepseek-ai/dsh-agent-loop` | `packages/core/agent-loop` | 6 | 1,662 | 19 | - | The concrete agent loop plugin for the DeepSeek Harness |
| `core` | `@deepseek-ai/dsh-agent-tool-presentation` | `packages/core/agent-tool-presentation` | 2 | 104 | 1 | - | Agent-plane presentation selector: composes one agent's tools as Code Mode, native, or both |
| `core` | `@deepseek-ai/dsh-scope` | `packages/core/scope` | 4 | 561 | 3 | - | Scoped-context registration primitive (scope tags, scope-filtered event dispatch) for the DeepSeek Harness |
| `core` | `@deepseek-ai/dsh-session` | `packages/core/session` | 10 | 3,164 | 13 | - | Event-sourced session store for the DeepSeek Harness |
| `core` | `@deepseek-ai/dsh-system-prompt` | `packages/core/system-prompt` | 2 | 605 | 4 | - | System prompt assembly registry for the DeepSeek Harness |
| `core` | `@deepseek-ai/dsh-tools` | `packages/core/tools` | 10 | 5,628 | 12 | - | Tool registry and execution pipeline for the DeepSeek Harness |
| `credentials` | `@deepseek-ai/dsh-authorization` | `packages/credentials/authorization` | 3 | 573 | 2 | - | Authorization seam (ctx.authorization): plugin-owned flows that obtain a credential through a conversation with the human |
| `credentials` | `@deepseek-ai/dsh-credentials` | `packages/credentials/credentials` | 3 | 450 | 2 | - | Abstract credential seam (ctx.credentials): settings carry references to secrets, providers own the values |
| `credentials` | `@deepseek-ai/dsh-credentials-local` | `packages/credentials/credentials-local` | 2 | 966 | 6 | - | File-backed credentials provider ($DSH_HOME/.env under the live process environment) for the DeepSeek Harness |
| `e2b` | `@deepseek-ai/dsh-e2b` | `packages/e2b/e2b` | 2 | 212 | 2 | - | Shared E2B sandbox lifecycle for DeepSeek Harness provider adapters |
| `e2b` | `@deepseek-ai/dsh-fs-e2b` | `packages/e2b/fs-e2b` | 2 | 612 | 1 | - | E2B filesystem implementation for DeepSeek Harness |
| `e2b` | `@deepseek-ai/dsh-subprocess-e2b` | `packages/e2b/subprocess-e2b` | 7 | 1,835 | 2 | - | E2B subprocess implementation for DeepSeek Harness |
| `examples` | `@deepseek-ai/dsh-acp-demo` | `packages/examples/acp-demo` | 3 | 206 | 3 | bin | ACP automation server app: agent spine + JSONL persistence + ACP transport, with a JSON-RPC stdio bin |
| `examples` | `@deepseek-ai/dsh-agent-spine-demo` | `packages/examples/agent-spine-demo` | 2 | 295 | 3 | - | The default executor-less/UI-less agent spine with fallback session titles, provider-routed retry, and optional persisted goals |
| `examples` | `@deepseek-ai/dsh-sdk-jsonrpc-demo` | `packages/examples/jsonrpc-demo` | 5 | 118 | 0 | bin | Bin that boots an external Cordis config for the stdio JSON-RPC SDK runtime |
| `experimental` | `@deepseek-ai/dsh-experimental-agent-team` | `packages/experimental/agent-team` | 14 | 2,327 | 4 | private | Implicit-root Agent Teams roster, durable peer mailbox, and shared task DAG |
| `experimental` | `@deepseek-ai/dsh-experimental-tool-agent-team` | `packages/experimental/tool-agent-team` | 2 | 436 | 1 | private | Scoped model-facing Agent Teams tools over ctx.agentTeams |
| `extensions` | `@deepseek-ai/dsh-cordis-client-runner` | `packages/extensions/cordis-client-runner` | 12 | 5,246 | 5 | client | Browser half of dynamic dual-half plugin packages: event subscription, closure evaluation, guard facade, and loader entries |
| `extensions` | `@deepseek-ai/dsh-cordis-host-runner` | `packages/extensions/cordis-host-runner` | 8 | 3,387 | 5 | - | Dynamic package definition registry, host-half sandbox lifecycle, and invoke handler table for model-mounted dual-half packages |
| `extensions` | `@deepseek-ai/dsh-tool-cordis` | `packages/extensions/tool-cordis` | 8 | 6,392 | 1 | - | Self-referential cordis toolset: inspect the live runtime, mount and dispose model-written plugins |
| `extensions` | `@deepseek-ai/dsh-client-ui-cordis` | `packages/extensions/ui-cordis` | 16 | 1,685 | 3 | client | Cordis dynamic-plugin definition card: the keyed cordis_define tool row with its run/stop switch |
| `feedback` | `@deepseek-ai/dsh-command-feedback` | `packages/feedback/command-feedback` | 2 | 138 | 2 | - | Log-only session feedback producer and human-facing slash command |
| `feedback` | `@deepseek-ai/dsh-message-feedback` | `packages/feedback/message-feedback` | 4 | 647 | 3 | - | Lifecycle-bound per-message rating and note sidecar for the DeepSeek Harness |
| `fs` | `@deepseek-ai/dsh-fs` | `packages/fs/fs` | 3 | 503 | 2 | - | Abstract filesystem capability seam (ctx.fs) for the DeepSeek Harness — vocabulary types, the FileSystem service (text IO + optional version-guarded atomic mutations), and the fs/* policy event vocabulary |
| `fs` | `@deepseek-ai/dsh-fs-local` | `packages/fs/fs-local` | 4 | 1,210 | 3 | - | Local-filesystem implementation of the DeepSeek Harness filesystem seam (ctx.fs) |
| `fs` | `@deepseek-ai/dsh-fs-observation-policy` | `packages/fs/fs-observation-policy` | 3 | 189 | 1 | - | File-context policy plugin for the DeepSeek Harness — observed-state, read-before-edit, and version-guarded write/edit added over the ctx.fs provider seam through the fs/* event gate (no service API) |
| `fs` | `@deepseek-ai/dsh-fs-sandbox` | `packages/fs/fs-sandbox` | 3 | 254 | 2 | - | Sandbox-enforcing implementation of the DeepSeek Harness filesystem seam: fences write/edit by the per-call sandbox mode (read-only denies mutation, workspace-write contains it to the workspace + temp roots) while reads pass through |
| `fs` | `@deepseek-ai/dsh-tool-fs` | `packages/fs/tool-fs` | 12 | 1,517 | 7 | - | Model-facing filesystem tools (read, write, edit) over the DeepSeek Harness filesystem seam (ctx.fs) |
| `fs` | `@deepseek-ai/dsh-tool-fs-search` | `packages/fs/tool-fs-search` | 8 | 1,578 | 6 | - | Model-facing filesystem discovery tools (glob, grep) backed by the packaged ripgrep binary (@vscode/ripgrep) |
| `fs` | `@deepseek-ai/dsh-tool-str-replace-editor` | `packages/fs/tool-str-replace-editor` | 2 | 553 | 1 | - | Model-facing view, create, literal replace, and line insert tool over the Harness filesystem service |
| `goal` | `@deepseek-ai/dsh-command-goal` | `packages/goal/command-goal` | 2 | 226 | 1 | - | Human-facing slash command for persisted same-session goals |
| `goal` | `@deepseek-ai/dsh-goal` | `packages/goal/goal` | 7 | 1,291 | 4 | - | Event-sourced same-session goal state and lifecycle service for the DeepSeek Harness |
| `goal` | `@deepseek-ai/dsh-goal-round-driver` | `packages/goal/goal-round-driver` | 3 | 555 | 2 | - | Race-fenced same-session goal-round driver |
| `goal` | `@deepseek-ai/dsh-tool-goal` | `packages/goal/tool-goal` | 4 | 517 | 1 | - | Model-facing same-session goal tools with execution-time authority checks |
| `guard` | `@deepseek-ai/dsh-repeat-tool-reminder` | `packages/guard/repeat-tool-reminder` | 2 | 263 | 1 | - | Repeat-tool-call guard plugin: advisory reminders when an agent loops on identical tool calls |
| `guard` | `@deepseek-ai/dsh-tool-call-timeout-policy` | `packages/guard/timeout-policy` | 2 | 111 | 1 | - | Tool-call timeout policy: a tools/execute wrapper that arms a per-tool deadline on exec.signal and returns TOOL_TIMEOUT when it wins |
| `hooks` | `@deepseek-ai/dsh-hook-protocol` | `packages/hooks/hook-protocol` | 9 | 854 | 7 | - | Shared Claude Code / Codex hook wire protocol: matcher engine, stdin/exit-code/stdout codec, multi-hook merge, and hook/* session events |
| `hooks` | `@deepseek-ai/dsh-hooks-claude-code` | `packages/hooks/hooks-claude-code` | 3 | 514 | 6 | - | Bridge plugin: run a Claude Code hooks.json / settings hook config on the DeepSeek Harness interception seams |
| `hooks` | `@deepseek-ai/dsh-hooks-codex` | `packages/hooks/hooks-codex` | 3 | 445 | 5 | - | Bridge plugin: run a Codex hooks.json hook config on the DeepSeek Harness interception seams |
| `host` | `@deepseek-ai/dsh-host-apiproxy` | `packages/host/apiproxy` | 42 | 8,470 | 20 | - | API gateway: the ApiProxy contract (api/), the fetch carrier pair (fetch/), and the host-side gateway plugin providing ctx.apiProxy |
| `host` | `@deepseek-ai/dsh-host-directory-picker` | `packages/host/directory-picker` | 2 | 168 | 1 | - | Abstract workspace-directory picking seam (ctx.directoryPicker) for the DeepSeek Harness web GUI host |
| `host` | `@deepseek-ai/dsh-host-directory-picker-auto` | `packages/host/directory-picker-auto` | 4 | 220 | 2 | - | Adaptive chooser of the directory-picker seam: resolves the host situation at boot and mounts the native or browse backend for the DeepSeek Harness web GUI host |
| `host` | `@deepseek-ai/dsh-host-directory-picker-browse` | `packages/host/directory-picker-browse` | 2 | 349 | 1 | - | In-app browsing backend of the directory-picker seam (listing/creation primitives over the host filesystem) |
| `host` | `@deepseek-ai/dsh-host-directory-picker-native` | `packages/host/directory-picker-native` | 8 | 737 | 6 | - | Native-OS-chooser backend of the directory-picker seam for the DeepSeek Harness web GUI host |
| `host` | `@deepseek-ai/dsh-host-frontend-static` | `packages/host/frontend-static` | 2 | 155 | 1 | - | SPA dist server for the Web shell: owns the webserver fallback seat, serving explicit index entries and static assets with traversal rejection and 404 misses |
| `host` | `@deepseek-ai/dsh-host-plugin-inventory` | `packages/host/plugin-inventory` | 3 | 120 | 2 | - | Read-only Remote projection of current Cordis Loader plugin state |
| `host` | `@deepseek-ai/dsh-host-webserver` | `packages/host/webserver` | 3 | 466 | 1 | - | Web route-registration plugin: HTTP and upgrade routes, index transform taps, and static dist fallback; knows no harness concepts |
| `identity` | `@deepseek-ai/dsh-anonymous-user-id` | `packages/identity/anonymous-user-id` | 2 | 131 | 2 | - | Shared anonymous user identity for DeepSeek Harness telemetry and feedback correlation |
| `interaction` | `@deepseek-ai/dsh-commands` | `packages/interaction/commands` | 4 | 661 | 2 | - | Plugin-owned human command registry for DeepSeek Harness UIs |
| `interaction` | `@deepseek-ai/dsh-permission-presets` | `packages/interaction/permission-presets` | 4 | 542 | 3 | - | User-facing permission presets (ctx.permissionPresets) for the DeepSeek Harness: one product-level Permissions select bundling the sandbox-mode and approval-policy knobs, written through to their own session events |
| `interaction` | `@deepseek-ai/dsh-tool-ask-user` | `packages/interaction/tool-ask-user` | 2 | 131 | 1 | - | Model-facing ask_user_question tool over the ctx.userQuestions seam |
| `interaction` | `@deepseek-ai/dsh-user-approval` | `packages/interaction/user-approval` | 3 | 487 | 2 | - | User-approval seam (ctx.approval) for the DeepSeek Harness: one-shot permission decisions dispatched to composed answerers over the approval/request waterfall, fail-closed by default |
| `interaction` | `@deepseek-ai/dsh-user-questions` | `packages/interaction/user-questions` | 3 | 239 | 1 | - | Abstract user-questions seam (ctx.userQuestions) for asking the human during agent runs |
| `jobs` | `@deepseek-ai/dsh-jobs` | `packages/jobs/jobs` | 4 | 424 | 2 | - | Background job registry (ctx.jobs) for the DeepSeek Harness — shared ids, owner isolation, polling, cancellation, and completion listeners for long-running tool work |
| `jobs` | `@deepseek-ai/dsh-jobs-local` | `packages/jobs/jobs-local` | 2 | 567 | 2 | - | Process-local implementation of the DeepSeek Harness background job registry seam |
| `jobs` | `@deepseek-ai/dsh-tool-jobs` | `packages/jobs/tool-jobs` | 2 | 432 | 1 | - | Model-facing background job control tools (job_output, job_list, job_kill) over the ctx.jobs registry |
| `llm` | `@deepseek-ai/dsh-llm` | `packages/llm/llm` | 14 | 2,958 | 12 | - | Provider-neutral LLM service interface for the DeepSeek Harness |
| `llm` | `@deepseek-ai/dsh-llm-deepseek` | `packages/llm/llm-deepseek` | 11 | 2,831 | 10 | - | DeepSeek chat-completions adapter for the DeepSeek Harness LLM seam |
| `llm` | `@deepseek-ai/dsh-llm-pi-ai` | `packages/llm/llm-pi-ai` | 12 | 3,710 | 13 | - | pi-ai-backed DeepSeek adapter for the DeepSeek Harness LLM seam (design-verification twin of dsh-llm-deepseek) |
| `llm` | `@deepseek-ai/dsh-llm-retry` | `packages/llm/llm-retry` | 5 | 494 | 5 | - | Provider-routed LLM request retry policy for the DeepSeek Harness |
| `llm` | `@deepseek-ai/dsh-token-meter` | `packages/llm/token-meter` | 10 | 1,027 | 3 | - | Replay-aware token measurement service (ctx.tokenMeter) for the DeepSeek Harness |
| `lsp` | `@deepseek-ai/dsh-lsp` | `packages/lsp/lsp` | 4 | 339 | 1 | - | Abstract LSP capability seam (ctx.lsp) for the DeepSeek Harness — language-server provider registry keyed by branded id and extension mapping, order-independent per-query selection, normalized definition/references/implementation/hover requests and results, and the LspError taxonomy |
| `lsp` | `@deepseek-ai/dsh-lsp-stdio` | `packages/lsp/lsp-stdio` | 9 | 1,664 | 9 | - | Generic stdio language-server provider for the DeepSeek Harness LSP capability seam (ctx.lsp) — spawns configured servers, translates JSON-RPC, and serves transient-open goToDefinition/findReferences/goToImplementation/hover queries in the host filesystem namespace |
| `lsp` | `@deepseek-ai/dsh-tool-lsp` | `packages/lsp/tool-lsp` | 4 | 483 | 4 | - | Model-facing lsp tool over the DeepSeek Harness LSP capability seam (ctx.lsp) — one read-only tool with goToDefinition/findReferences/goToImplementation/hover operations, one-based UTF-16 cursor coordinates, bounded location rendering, and hover normalization |
| `mcp` | `@deepseek-ai/dsh-mcp-client` | `packages/mcp/mcp-client` | 5 | 1,171 | 5 | - | MCP client bridge: connects to MCP servers and registers their tools on ctx.tools |
| `plan` | `@deepseek-ai/dsh-plan-mode` | `packages/plan/plan-mode` | 4 | 601 | 4 | - | Logged per-agent plan mode with deployment guidance, a direct slash command, and a user-reviewed exit |
| `preset` | `@deepseek-ai/dsh-agent-presets` | `packages/preset/agent-presets` | 9 | 1,684 | 8 | - | Per-session agent composition from preset cordis.yml files for the DeepSeek Harness |
| `preset` | `@deepseek-ai/dsh-persona` | `packages/preset/persona` | 2 | 99 | 1 | - | Composition-authored deployment persona section for the DeepSeek Harness |
| `runtime-diagnostics` | `@deepseek-ai/dsh-invariants` | `packages/runtime-diagnostics/invariants` | 2 | 230 | 1 | - | Registry service for package-owned DeepSeek Harness runtime invariants |
| `sandbox` | `@deepseek-ai/dsh-sandbox` | `packages/sandbox/sandbox` | 4 | 452 | 3 | - | Abstract process-sandbox seam (ctx.sandbox) for the DeepSeek Harness: same-world confinement vocabulary and the SandboxProvider contract |
| `sandbox` | `@deepseek-ai/dsh-sandbox-local` | `packages/sandbox/sandbox-local` | 3 | 655 | 6 | - | Local process-sandbox backends for the DeepSeek Harness sandbox seam: bwrap, the npm-distributed landlock-run launcher, macOS Seatbelt, or the Windows ACL restricted-token runner — functionally probed, fail-closed |
| `sandbox` | `@deepseek-ai/dsh-sandbox-policy` | `packages/sandbox/sandbox-policy` | 3 | 267 | 2 | - | Per-call sandbox policy resolver and current model context: deployment fallbacks plus each session's mode and workspace root, shared by every enforcing capability family |
| `sandbox` | `@deepseek-ai/dsh-sandbox-windows-acl` | `packages/sandbox/sandbox-windows-acl` | 12 | 2,530 | 14 | - | Windows ACL write-restriction sandbox backend (restricted-token spawn with capability-SID write allowlist) for the DeepSeek Harness sandbox seam |
| `schedule` | `@deepseek-ai/dsh-schedule` | `packages/schedule/schedule` | 8 | 2,003 | 7 | - | Agent-scoped durable after, at, and fixed-rate reminders over the session event log |
| `sdk` | `@deepseek-ai/dsh-sdk-client` | `packages/sdk/client` | 6 | 952 | 2 | - | TypeScript client SDK for driving a DeepSeek Harness runtime subprocess over stdio JSON-RPC: the DeepSeekHarness high-level turns API and the lower-level HarnessClient |
| `sdk` | `@deepseek-ai/dsh-sdk-protocol` | `packages/sdk/protocol` | 4 | 440 | 1 | - | Shared wire protocol for the DeepSeek Harness SDK runtime: the newline-delimited JSON-RPC stdio transport and the named request, result, and notification types spoken between the runtime server and SDK clients |
| `sdk` | `@deepseek-ai/dsh-sdk-jsonrpc-server` | `packages/sdk/server` | 3 | 368 | 4 | - | Stdio JSON-RPC server plugin for out-of-process DeepSeek Harness SDK clients |
| `session` | `@deepseek-ai/dsh-session-checkpoint-policy` | `packages/session/session-checkpoint-policy` | 2 | 113 | 2 | - | Semantic session durability checkpoints before model requests and tool side effects |
| `session` | `@deepseek-ai/dsh-session-persistence` | `packages/session/session-persistence` | 6 | 2,160 | 3 | - | Abstract durable session persistence seam (ctx.sessionPersistence) for the DeepSeek Harness |
| `session` | `@deepseek-ai/dsh-session-persistence-jsonl` | `packages/session/session-persistence-jsonl` | 7 | 1,939 | 4 | - | JSONL durable session persistence backend for the DeepSeek Harness |
| `session` | `@deepseek-ai/dsh-session-persistence-sqlite` | `packages/session/session-persistence-sqlite` | 7 | 1,742 | 6 | - | SQLite durable session persistence with physical chunk-row packing |
| `session` | `@deepseek-ai/dsh-session-projection` | `packages/session/session-projection` | 3 | 559 | 1 | - | Session-projection seam: the merge-extensible projection type table, the provider contract, and the ctx.sessionProjections registry serving whole current values of log-derived per-session state |
| `session` | `@deepseek-ai/dsh-session-projection-cache` | `packages/session/session-projection-cache` | 3 | 404 | 1 | - | Persisted projection cache (ctx.sessionProjectionCache): durable per-session projection checkpoints over the domain data form, throttled write-behind, and the cold-read ladder (cache row + persistence tail replay) |
| `session` | `@deepseek-ai/dsh-session-stats` | `packages/session/session-stats` | 5 | 329 | 2 | - | Whole-log conversation counts and wall times projection (sessionStats) for the DeepSeek Harness |
| `session` | `@deepseek-ai/dsh-session-telemetry` | `packages/session/session-telemetry` | 3 | 529 | 2 | - | SessionTelemetryBackend seam for the DeepSeek Harness: session-event capture, projection, redaction, and handoff to a reporting backend |
| `session` | `@deepseek-ai/dsh-session-telemetry-otel` | `packages/session/session-telemetry-otel` | 2 | 332 | 2 | - | OpenTelemetry backend for the DeepSeek Harness telemetry seam: hands captured session records to the OTel JS SDK's log pipeline |
| `session` | `@deepseek-ai/dsh-session-title` | `packages/session/session-title` | 5 | 952 | 7 | - | Log-backed session title service and provider registry for the DeepSeek Harness |
| `session` | `@deepseek-ai/dsh-session-title-all-prompts-llm` | `packages/session/session-title-all-prompts-llm` | 2 | 66 | 1 | - | All-user-messages LLM provider plugin for DeepSeek Harness session titles |
| `session` | `@deepseek-ai/dsh-session-title-first-prompt-llm` | `packages/session/session-title-first-prompt-llm` | 2 | 70 | 3 | - | First-message LLM provider plugin for DeepSeek Harness session titles |
| `session` | `@deepseek-ai/dsh-session-title-llm` | `packages/session/session-title-llm` | 2 | 324 | 1 | - | Shared LLM generation policy for DeepSeek Harness session-title providers |
| `session-query` | `@deepseek-ai/dsh-session-log-export` | `packages/session-query/session-log-export` | 8 | 350 | 7 | client | Web Session-log export command and shared download dialog |
| `session-query` | `@deepseek-ai/dsh-session-query` | `packages/session-query/session-query` | 11 | 1,718 | 3 | - | Combined session query service contract with concrete reads, traces, and filters |
| `session-query` | `@deepseek-ai/dsh-session-query-sqlite` | `packages/session-query/session-query-sqlite` | 4 | 1,783 | 3 | - | Concrete ctx.sessionQuery backend with SQLite FTS5 search |
| `session-query` | `@deepseek-ai/dsh-tool-session-query` | `packages/session-query/tool-session-query` | 7 | 1,444 | 2 | - | Workspace-authorized model-facing session history search, trace, and event read tools |
| `settings` | `@deepseek-ai/dsh-settings` | `packages/settings/settings` | 4 | 1,106 | 3 | - | Abstract user-settings seam (ctx.settings) for the DeepSeek Harness |
| `settings` | `@deepseek-ai/dsh-settings-file` | `packages/settings/settings-file` | 2 | 401 | 5 | - | File-backed settings provider (settings.yaml) for the DeepSeek Harness |
| `shell` | `@deepseek-ai/dsh-bash-local` | `packages/shell/bash-local` | 2 | 363 | 2 | - | Local-subprocess implementation of the DeepSeek Harness bash executor seam |
| `shell` | `@deepseek-ai/dsh-bash-sandbox` | `packages/shell/bash-sandbox` | 3 | 328 | 5 | - | Sandbox-consuming implementation of the DeepSeek Harness bash executor seam (confines every command via ctx.sandbox, reports denial/enforcement result facts) |
| `shell` | `@deepseek-ai/dsh-pwsh-local` | `packages/shell/pwsh-local` | 3 | 472 | 2 | - | Local PowerShell implementation of the DeepSeek Harness bash executor seam |
| `shell` | `@deepseek-ai/dsh-pwsh-sandbox` | `packages/shell/pwsh-sandbox` | 3 | 339 | 2 | - | Sandbox-consuming implementation of the DeepSeek Harness PowerShell executor seam (confines every command via ctx.sandbox, reports denial/enforcement result facts) |
| `shell` | `@deepseek-ai/dsh-shell` | `packages/shell/shell` | 4 | 350 | 2 | - | Abstract bash executor seam (ctx.shell) for the DeepSeek Harness |
| `shell` | `@deepseek-ai/dsh-shell-env` | `packages/shell/shell-env` | 2 | 247 | 1 | - | Tool-independent managed DSH_* shell environment registry |
| `shell` | `@deepseek-ai/dsh-tool-bash` | `packages/shell/tool-bash` | 4 | 554 | 2 | - | Model-facing bash tool with optional generic background-job and sandbox-escalation support |
| `shell` | `@deepseek-ai/dsh-tool-bash-persistent` | `packages/shell/tool-bash-persistent` | 2 | 503 | 2 | - | Model-facing owner-scoped persistent Bash tool backed by the Harness PTY service |
| `shell` | `@deepseek-ai/dsh-tool-pwsh` | `packages/shell/tool-pwsh` | 4 | 620 | 3 | - | Model-facing pwsh tool over the bash executor seam |
| `shell` | `@deepseek-ai/dsh-tool-pwsh-persistent` | `packages/shell/tool-pwsh-persistent` | 2 | 545 | 2 | - | Model-facing owner-scoped persistent PowerShell tool backed by the Harness PTY service |
| `skill` | `@deepseek-ai/dsh-skill` | `packages/skill/skill` | 2 | 898 | 1 | - | Agent skill provider registry for the DeepSeek Harness |
| `skill` | `@deepseek-ai/dsh-skill-badge` | `packages/skill/skill-badge` | 2 | 90 | 1 | - | Bundled dsh badge skill provider for DeepSeek Harness |
| `skill` | `@deepseek-ai/dsh-skill-filesystem` | `packages/skill/skill-filesystem` | 2 | 1,071 | 2 | - | Local filesystem skill provider for the DeepSeek Harness |
| `skill` | `@deepseek-ai/dsh-tool-skill` | `packages/skill/tool-skill` | 2 | 461 | 1 | - | Model-facing skill loading tool for the DeepSeek Harness |
| `spill` | `@deepseek-ai/dsh-spill` | `packages/spill/spill` | 3 | 161 | 1 | - | Abstract spill storage seam (ctx.spillStore) for the DeepSeek Harness — save oversized tool text and return a retrieval locator |
| `spill` | `@deepseek-ai/dsh-spill-local` | `packages/spill/spill-local` | 3 | 215 | 1 | - | Local-filesystem implementation of the DeepSeek Harness spill storage seam (private session-scoped files) |
| `spill` | `@deepseek-ai/dsh-spill-policy` | `packages/spill/spill-policy` | 3 | 288 | 1 | - | Tool-result spill policy for the DeepSeek Harness — replaces oversized plain-text tool results with a retained preview plus a spill-file path (no service API) |
| `storage` | `@deepseek-ai/dsh-storage` | `packages/storage/storage` | 5 | 331 | 1 | - | Storage hub (ctx.storage): named backend registry plus mounted data-form facilities for the DeepSeek Harness |
| `storage` | `@deepseek-ai/dsh-storage-domain` | `packages/storage/storage-domain` | 6 | 857 | 2 | - | Domain data form (ctx.storage.domain): schema-validated, event-emitting KV domains over storage backends for the DeepSeek Harness |
| `storage` | `@deepseek-ai/dsh-storage-json` | `packages/storage/storage-json` | 5 | 424 | 1 | - | JSON file KV storage backend for the DeepSeek Harness storage hub |
| `storage` | `@deepseek-ai/dsh-storage-sqlite` | `packages/storage/storage-sqlite` | 4 | 476 | 2 | - | SQLite storage backend (kv facet) for the DeepSeek Harness storage hub |
| `subagent` | `@deepseek-ai/dsh-subagent` | `packages/subagent/subagent` | 18 | 4,684 | 10 | - | Abstract subagent seam (ctx.subagents): named-provider registry for delegating to child agents |
| `subagent` | `@deepseek-ai/dsh-subagent-acp` | `packages/subagent/subagent-acp` | 3 | 587 | 3 | - | Out-of-process ACP subagent backend: drives a child agent in a spawned subprocess over the Agent Client Protocol |
| `subagent` | `@deepseek-ai/dsh-subagent-claude-code` | `packages/subagent/subagent-claude-code` | 4 | 936 | 4 | bundle | One-shot Claude Code subagent provider over the official Agent SDK |
| `subagent` | `@deepseek-ai/dsh-subagent-codex` | `packages/subagent/subagent-codex` | 4 | 1,348 | 4 | bundle | One-shot Codex subagent provider over the official app-server protocol |
| `subagent` | `@deepseek-ai/dsh-subagent-dsh-sdk` | `packages/subagent/subagent-dsh-sdk` | 3 | 375 | 2 | - | Out-of-process SDK subagent backend: drives a child DeepSeek Harness runtime subprocess over stdio JSON-RPC through the TypeScript SDK client |
| `subagent` | `@deepseek-ai/dsh-subagent-fork-in-process` | `packages/subagent/subagent-fork-in-process` | 2 | 124 | 2 | - | In-process fork subagent backend: runs a child agent seeded with a prefix of the parent's log |
| `subagent` | `@deepseek-ai/dsh-subagent-in-process-driver` | `packages/subagent/subagent-in-process-driver` | 3 | 405 | 4 | - | Shared in-process subagent run driver: drives a child agent on ctx.agents (used by the spawn and fork backends) |
| `subagent` | `@deepseek-ai/dsh-subagent-spawn-in-process` | `packages/subagent/subagent-spawn-in-process` | 2 | 94 | 2 | - | In-process spawn subagent backend: runs a fresh child agent on ctx.agents |
| `subagent` | `@deepseek-ai/dsh-tool-subagent` | `packages/subagent/tool-subagent` | 2 | 506 | 2 | - | Model-facing subagent delegation tool over the ctx.subagents seam |
| `subagent` | `@deepseek-ai/dsh-tool-subagent-control` | `packages/subagent/tool-subagent-control` | 3 | 342 | 2 | - | Globally named send_message, interrupt_agent, and list_agents tools over ctx.subagents continuations |
| `subagent` | `@deepseek-ai/dsh-tool-subagent-report` | `packages/subagent/tool-subagent-report` | 2 | 172 | 1 | - | Child-scoped report tool over ctx.subagents continuations |
| `subprocess` | `@deepseek-ai/dsh-subprocess` | `packages/subprocess/subprocess` | 3 | 428 | 1 | - | Subprocess seam (ctx.subprocess) for the DeepSeek Harness — managed process groups, bounded spill-backed output, and escalated kills behind one abstract service |
| `subprocess` | `@deepseek-ai/dsh-subprocess-local` | `packages/subprocess/subprocess-local` | 6 | 1,792 | 6 | - | Local-subprocess implementation of the DeepSeek Harness subprocess seam |
| `terminal` | `@deepseek-ai/dsh-terminal` | `packages/terminal/terminal` | 3 | 683 | 1 | - | Persistent PTY session seam for the DeepSeek Harness — owner-scoped ids, backend registry, interactive sends, reads, signals, and awaited cleanup |
| `terminal` | `@deepseek-ai/dsh-terminal-bash` | `packages/terminal/terminal-bash` | 5 | 1,116 | 5 | - | Persistent shell PTY backend over the DeepSeek Harness subprocess terminal primitive |
| `terminal` | `@deepseek-ai/dsh-tool-terminal` | `packages/terminal/tool-terminal` | 3 | 606 | 3 | - | Six model-facing persistent PTY tools with owner isolation and generic background-job integration |
| `test-support` | `@deepseek-ai/dsh-acp-snapshot` | `packages/test-support/acp-snapshot` | 6 | 3,218 | 3 | - | ACP test kit: shared subprocess launcher, snapshot scenario harness, expected-output normalizers, and suite factory |
| `test-support` | `@deepseek-ai/dsh-agent-loop-testkit` | `packages/test-support/agent-loop-testkit` | 2 | 76 | 1 | - | Shared prerequisite mounting for tests that exercise the concrete agent loop |
| `test-support` | `@deepseek-ai/dsh-client-test-runtime` | `packages/test-support/client-runtime` | 10 | 1,537 | 4 | - | jsdom slot test runtime: real Cordis Context + SlotRegistry + UI renderer with test-owned session/workspace doubles for feature specs |
| `test-support` | `@deepseek-ai/dsh-llm-mock-server` | `packages/test-support/llm-mock-server` | 4 | 1,031 | 3 | - | Scriptable OpenAI-compatible HTTP/SSE fault server for LLM recovery tests |
| `test-support` | `@deepseek-ai/dsh-llm-replay` | `packages/test-support/llm-replay` | 2 | 889 | 1 | - | Replay LLM plugin: short-circuits llm/stream with model chunks reconstructed from a recorded session JSONL (keyless snapshot tests) |
| `test-support` | `@deepseek-ai/dsh-loader-smoke` | `packages/test-support/loader-smoke` | 3 | 342 | 3 | - | Shared subprocess and direct-agent harness for keyless real-Loader example smoke tests |
| `todo` | `@deepseek-ai/dsh-tool-todo` | `packages/todo/tool-todo` | 4 | 329 | 5 | - | Model-facing todo_write tool over the DeepSeek Harness event-sourced session log |
| `typert` | `@deepseek-ai/dsh-typert-generator` | `packages/typert/generator` | 9 | 6,251 | 9 | - | TypeScript project analyzer and model-driven Typert artifact generator |
| `typert` | `@deepseek-ai/dsh-typert-loader` | `packages/typert/loader` | 2 | 472 | 1 | - | Loader integration for generated Typert package contributions |
| `typert` | `@deepseek-ai/dsh-typert-protocol` | `packages/typert/protocol` | 3 | 801 | 1 | - | Compiler-independent Remote metadata and Typert provider protocols |
| `typert` | `@deepseek-ai/dsh-typert-registry` | `packages/typert/registry` | 5 | 909 | 1 | client | Runtime registry for generated package reflection and Zod schemas |
| `util` | `@deepseek-ai/dsh-atomic-write` | `packages/util/atomic-write` | 2 | 184 | 2 | - | Zero-dependency atomic file replacement: exclusive-create random-suffix temp + rename carrying the caller-stated permissions (writeFileAtomic) |
| `util` | `@deepseek-ai/dsh-brand` | `packages/util/brand` | 2 | 57 | 0 | - | Type-only Branded<B> nominal-typing primitive for the DeepSeek Harness |
| `util` | `@deepseek-ai/dsh-home-paths` | `packages/util/home-paths` | 2 | 142 | 1 | - | Shared filesystem path helpers for the DeepSeek Harness |
| `util` | `@deepseek-ai/dsh-launch-environment` | `packages/util/launch-environment` | 2 | 154 | 1 | - | Immutable DeepSeek Harness launch environment that records which layer supplied each value |
| `util` | `@deepseek-ai/dsh-native-command` | `packages/util/native-command` | 2 | 75 | 1 | - | Zero-dependency no-shell execFile runner for host-native OS integrations: utf8 stdio capture, abort propagation, Windows hide |
| `util` | `@deepseek-ai/dsh-output-retention` | `packages/util/output-retention` | 2 | 473 | 1 | - | Zero-dependency bounded-retention primitive: ItemRetainer/TextRetainer + neutral notice helpers (what did we keep, what did we omit) |
| `util` | `@deepseek-ai/dsh-timeout` | `packages/util/timeout` | 2 | 220 | 1 | - | Zero-dependency timeout/deadline primitive: clampTimeout, deadline, timeoutOf, TimeoutReason (timing + classification only, no termination) |
| `web` | `@deepseek-ai/dsh-tool-web` | `packages/web/tool-web` | 5 | 1,008 | 4 | - | Model-facing web tools (web_search, web_fetch) over the DeepSeek Harness web capability seam (ctx.web) |
| `web` | `@deepseek-ai/dsh-web` | `packages/web/web` | 3 | 362 | 1 | - | Abstract web access capability seam (ctx.web) for the DeepSeek Harness — search/fetch provider registry, registration-order-independent selection, request/result vocabulary, and the WebError taxonomy |
| `web` | `@deepseek-ai/dsh-web-fetch-http` | `packages/web/web-fetch-http` | 4 | 476 | 1 | - | Anonymous public HTTP(S) fetch provider for the DeepSeek Harness web capability seam (ctx.web) |
| `web` | `@deepseek-ai/dsh-web-search-deepseek` | `packages/web/web-search-deepseek` | 4 | 565 | 4 | - | DeepSeek-backed search provider (native web_search via the Anthropic-compatible API) for the DeepSeek Harness web capability seam (ctx.web) |
| `web` | `@deepseek-ai/dsh-web-search-exa` | `packages/web/web-search-exa` | 4 | 303 | 2 | - | Exa-backed search provider for the DeepSeek Harness web capability seam (ctx.web) |
| `web` | `@deepseek-ai/dsh-web-search-perplexity` | `packages/web/web-search-perplexity` | 4 | 296 | 2 | - | Perplexity-backed search provider for the DeepSeek Harness web capability seam (ctx.web) |
| `workflow` | `@deepseek-ai/dsh-tool-ralph` | `packages/workflow/tool-ralph` | 2 | 509 | 2 | - | Model-facing fresh-agent Ralph loop over the workflow and subagent seams |
| `workflow` | `@deepseek-ai/dsh-tool-workflow` | `packages/workflow/tool-workflow` | 3 | 566 | 2 | - | Model-facing workflow tool: run a JavaScript orchestration script over ctx.workflowEngine |
| `workflow` | `@deepseek-ai/dsh-workflow` | `packages/workflow/workflow` | 4 | 519 | 2 | - | Workflow capability seam: ctx.workflowEngine service, run vocabulary, and workflow/* events |
| `workflow` | `@deepseek-ai/dsh-workflow-worker-thread` | `packages/workflow/workflow-worker-thread` | 10 | 1,987 | 8 | - | worker-thread workflow engine: executes model-written orchestration scripts off the host event loop, bridging agent() calls back to ctx.subagents |
| `workspace` | `@deepseek-ai/dsh-workspace` | `packages/workspace/workspace` | 6 | 1,142 | 2 | - | Workspace entity registry (ctx.workspaceRegistry): durable workspace records with validated session attachment over the domain data form for the DeepSeek Harness |


# DeepSeek Harness 源码讲解（通俗版）

> 本章是前面技术研究的通俗重述。它不要求读者了解 Cordis、事件溯源、DI、RPC、CAS、Fiber 等术语。遇到必须使用的术语，会先用普通话解释。

## 1. 这个项目到底是做什么的？

可以把 DeepSeek Harness 理解成一个“让大模型真正干活的运行平台”。

大模型本身只擅长接收文字并产生文字。它不会天然知道：

- 当前工作目录在哪里；
- 有哪些文件；
- 如何修改文件；
- 如何运行命令；
- 如何保存聊天记录；
- 如何在网页上显示过程；
- 用户是否允许某个危险操作；
- 对话太长以后应该怎么办。

DeepSeek Harness 把这些能力组织起来，让模型成为一个可以持续工作的编码助手。

它主要负责五件事：

1. 把用户的问题整理后发给模型；
2. 把模型要求执行的工具真正执行；
3. 把每一步完整记录下来；
4. 把结果显示到网页、命令行或 SDK；
5. 在权限、取消、崩溃和超长对话等情况下正确收尾。

## 2. 为什么项目里有两百多个包？

因为项目把不同职责拆得很细。

例如“运行 Bash 命令”没有全部写在一个文件里，而是拆成：

- 工具层：告诉模型 Bash 工具叫什么、参数是什么；
- Shell 层：定义一次命令运行应返回什么；
- 本地执行层：真正启动 `bash -c`；
- 沙箱层：限制命令可以修改哪些文件；
- 子进程层：负责杀掉整个进程树；
- 后台任务层：让长命令在后台继续运行；
- 网页层：把命令和输出画成终端卡片。

这种拆法的优点是可以单独替换其中一层。例如把本地执行替换成远程沙箱时，模型工具和网页显示不必重写。

缺点是第一次看源码时容易迷路，因为一个功能可能横跨多个包。

## 3. 什么是“插件”？

这里的插件可以理解成一个可安装、可卸载的功能模块。

一个插件可能：

- 提供一项服务；
- 注册一个模型工具；
- 增加一段系统提示词；
- 监听某个运行时机；
- 增加一个网页组件；
- 提供一种存储后端。

插件卸载时，它注册的内容也应该一起消失。

例如文件工具插件卸载后：

- 模型下一次请求不再看到 `read/write/edit`；
- 工具注册表中找不到它们；
- 插件自己的监听器也会移除。

项目所说的“一切皆插件”，意思是：模型、工具、会话、网页、存储和主循环都不是写死的唯一实现。

## 4. 什么是“服务”？

服务可以理解成插件之间约定好的功能入口。

例如：

```text
ctx.llm          调用大模型
ctx.tools        查找和执行工具
ctx.sessions     管理会话
ctx.agents       管理正在运行的 Agent
ctx.fs           读写文件
ctx.shell        运行 shell 命令
ctx.subprocess   启动和终止子进程
```

调用方只依赖这个功能入口，不直接绑定某个具体实现。

例如文件工具只要求“有一个 `ctx.fs`”，至于它背后是：

- 本机文件系统；
- 带路径限制的本机文件系统；
- E2B 远程文件系统；

文件工具并不关心。

## 5. 程序是怎样启动的？

命令入口是：

`deepseek-harness/apps/cli/src/bin.ts`

用户执行：

```sh
dsh web
```

程序会完成以下步骤：

```text
解析命令行
  -> 找到 web profile
  -> 加载基础功能清单
  -> 加载 Web 功能清单
  -> 加载用户自己的修改配置
  -> 得到最终插件列表
  -> 启动所有依赖已满足的插件
  -> 启动 HTTP 服务和网页
```

基础功能清单在：

`deepseek-harness/packages/bundle/base/cordis.patch.yml`

Web 增量清单在：

`deepseek-harness/packages/bundle/web-app/cordis.patch.yml`

所以 Web 版本不是一套独立 Agent。它是在同一个 Agent 核心上增加了：

- HTTP 服务；
- 浏览器通信；
- Workspace 管理；
- 网页插件和 React 界面。

## 6. Headless 模式与 Web 模式有什么不同？

Headless 模式适合一次性任务：

```sh
dsh --profile headless "检查这个项目"
```

它会：

1. 创建一个新 Agent；
2. 把任务放进 Agent 的消息队列；
3. 等待 Agent 完成；
4. 保存会话；
5. 打印最后一条回答；
6. 退出进程。

它不启动网页和 HTTP 服务。

Web 模式则长期运行，可以：

- 创建多个会话；
- 切换工作区；
- 继续历史对话；
- 实时显示工具和模型输出；
- 修改设置和模型；
- 管理子 Agent。

两者底层使用同一套 Agent、模型、工具和会话代码。

## 7. 一个 Agent 是什么？

在这个项目中，一个 Agent 不是一个抽象名字，而是一个正在工作的对象。

它包含：

- 一个唯一会话 ID；
- 一份会话记录；
- 两个待处理消息队列；
- 当前模型和提供方；
- 当前是空闲还是运行中；
- 只属于它自己的插件作用范围；
- 取消和等待完成的方法。

主要源码：

- `packages/core/agent/src/index.ts`
- `packages/core/agent-loop/src/agent.ts`

## 8. 用户发一条消息后发生了什么？

假设用户输入：

> 请修改 README，把安装步骤写清楚。

整个过程大致如下：

```text
用户消息到达
  -> 写入 Agent 待处理队列
  -> 唤醒 Agent
  -> 开始一个新轮次
  -> 取出这条消息
  -> 组装系统提示词和工具列表
  -> 从会话记录重建历史消息
  -> 调用大模型
  -> 保存模型流式输出
  -> 模型可能要求读取 README
  -> 执行 read 工具
  -> 把读取结果交给模型
  -> 模型要求 edit
  -> 检查权限与文件是否变化
  -> 执行 edit
  -> 把修改结果交给模型
  -> 模型生成最终回答
  -> 结束轮次
  -> 保存全部记录
  -> 网页显示最终状态
```

这段流程的主干位于：

`packages/core/agent-loop/src/agent.ts`

## 9. 什么是“轮次”和“步骤”？

可以这样理解：

- **轮次**：从一条用户请求开始，到 Agent 暂时没有工作为止；
- **步骤**：一次模型请求，加上这次模型请求产生的工具调用。

一个轮次可能有多个步骤。

例如：

```text
轮次 1
  步骤 1：模型决定读取 README
  步骤 2：模型根据读取结果决定编辑 README
  步骤 3：模型看到编辑成功，生成最终回答
```

记录中会出现：

```text
turn/start
step/start
user/message
assistant/message
tool/call
tool/result
step/end
...
turn/end
```

这些标记让程序知道每条消息和工具结果属于哪次工作。

## 10. 为什么还有两个待处理队列？

Agent 有两类待处理消息：

### next-turn

普通用户消息。它会开启一个新的轮次。

例如 Agent 正忙时，用户又发了一条普通消息，这条消息排在后面，等当前轮次结束后再处理。

### next-step

希望尽快放进当前工作的下一次模型请求。

例如：

- 用户中途补充说明；
- 后台任务完成通知；
- 子 Agent 发回结果；
- 系统注入新的上下文。

这类消息不一定开启独立轮次，而是尽量进入最近的下一步骤。

## 11. followup、steer 和 inject 的区别

### followup

普通后续消息，会唤醒 Agent，并安排成一个新轮次。

### steer

中途引导。如果 Agent 正在运行，它会尽量进入下一步骤；如果 Agent 空闲，它也会唤醒 Agent。

### inject

注入上下文，但不主动唤醒空闲 Agent。

例如后台系统发现文件发生变化，可以先 inject 一条提醒。Agent 下次因为用户消息被唤醒时会看到它，但不会仅为这条提醒浪费一次模型调用。

## 12. 项目是怎样保存聊天记录的？

它不是只保存最后拼好的聊天消息，而是保存发生过的每一个重要事实。

例如：

- 用户消息；
- 模型每个流式分片；
- 拼好的模型回答；
- 工具调用；
- 工具结果；
- 模型和工具配置；
- 消息队列变化；
- 审批问题和回答；
- 对话压缩；
- 计划模式和权限状态。

这份只允许向后追加的记录就是 `Session`。

主要源码：

- `packages/core/session/src/index.ts`
- `packages/core/session/src/types.ts`

## 13. 为什么连消息队列变化也要保存？

因为用户发出的消息可能还没被模型处理，进程就退出了。

如果队列只存在内存里，这条消息会丢失。

现在每次插入、编辑、删除和领取消息都会写入会话记录。恢复时可以重新计算出还有哪些消息等待处理。

## 14. “模型看到的历史”和“完整记录”不是一回事

完整记录中有很多模型不需要看到的信息，例如：

- 轮次开始和结束标记；
- 原始流式分片；
- 审批审计；
- 标题；
- 后台状态；
- 压缩过程记录。

程序会从完整记录中挑出当前模型应该看到的内容，组成模型历史。

主要包括：

- 用户消息；
- 完整的模型消息；
- 工具结果。

这就是 `deriveMessages()` 的工作。

## 15. 为什么对话压缩不会删除旧记录？

对话太长时，模型无法继续接收全部历史。

项目会让模型生成一份摘要，然后在“模型看到的历史”里用摘要替换较早的一段内容。

但是原始记录仍然保留。

所以会同时得到：

- 模型使用更短的上下文；
- 用户仍能查看原对话；
- 程序仍能审计摘要来自哪些消息；
- 崩溃恢复不会丢失原始事实。

## 16. 系统提示词是怎样组成的？

系统提示词不是写在一个超大字符串里。

不同插件分别贡献一段：

- Harness 身份；
- 当前角色；
- Plan 模式说明；
- Bash 使用建议；
- 文件工具建议；
- 当前权限状态；
- 当前工作目录和模型变量。

每次模型请求前，程序按顺序把当前有效片段组合起来。

某个 Agent preset 还可以覆盖同名片段，例如给不同 Agent 使用不同角色。

主要源码：

`packages/core/system-prompt/src/index.ts`

## 17. 模型工具是怎样注册的？

每个工具会声明：

- 工具名称；
- 给模型看的说明；
- 参数格式；
- 返回值格式；
- 真正执行的函数；
- 怎样把结构化返回值写成模型可读文本；
- 网页应该显示哪种卡片。

模型只看到名称、说明和参数格式，不会看到执行函数和内部配置。

主要源码：

`packages/core/tools/src/index.ts`

## 18. 工具执行前后为什么有那么多层？

因为“能找到工具”不等于“应该立刻执行”。

一次工具调用会经过：

```text
参数检查
  -> 权限策略
  -> 是否需要用户批准
  -> 最终拒绝规则
  -> 超时包装
  -> 工具主体
  -> 结果检查或改写
  -> 最后内容整理
  -> 保存工具结果
```

这样可以在不修改每个工具的情况下统一增加：

- 用户审批；
- 超时；
- 日志；
- 大结果外置；
- 敏感结果过滤；
- 重复工具提醒。

## 19. 为什么工具返回“结构化值”和“模型文本”两份东西？

结构化值适合程序使用。

例如后台任务启动结果应该返回：

```json
{
  "kind": "background",
  "jobId": "bash-1"
}
```

而不是让程序从一句：

```text
started background job bash-1
```

里面猜出 ID。

模型文本则适合大模型阅读。

Code Mode 使用结构化值；普通模型工具结果使用渲染后的文本。这样同一个工具既适合模型直接调用，也适合模型写程序批量调用。

## 20. Code Mode 是什么？

普通模式下，模型一次直接调用一个工具。

Code Mode 下，模型看到一个 `run_code` 工具，并在代码中写：

```ts
const a = await tools.read({ file_path: 'a.ts' })
const b = await tools.read({ file_path: 'b.ts' })
return { a, b }
```

代码中的每次 `tools.xxx()` 仍会重新进入完整工具权限流程，不是绕过检查直接调用内部函数。

运行代码的 Worker 可以强制结束死循环，但它不是不可信代码安全沙箱。模型代码仍被视为拥有较高权限。

## 21. 模型是怎样被调用的？

Agent Loop 不直接写死 DeepSeek HTTP 请求。

它先向模型服务询问：

- 哪个提供方负责这个 provider 名称；
- 模型支持什么；
- 默认输出 token 数是多少；
- 是否支持图片；
- 是否支持不同推理强度；
- 上下文窗口多大。

然后把本次调用绑定到这一版模型配置，再开始流式请求。

这样设置或插件热更新时，不会出现“用旧配置准备请求，却突然交给新配置发送”的混合状态。

## 22. DeepSeek 适配器做了什么？

DeepSeek 适配器负责把项目内部的统一消息格式转换成 DeepSeek Chat Completions 格式。

它处理：

- system message；
- user/assistant/tool message；
- thinking/reasoning；
- 工具调用；
- token usage；
- SSE 流；
- HTTP 错误；
- 图片 Files API；
- Base64 图片回退；
- 请求超时和取消。

主要源码：

`packages/llm/llm-deepseek/src/adapter.ts`

## 23. 模型请求失败后如何重试？

重试不是 DeepSeek HTTP 库偷偷完成的。

失败后，Agent 层会得到一个统一错误，例如：

- 鉴权失败；
- 限流；
- 服务错误；
- 空响应；
- 上下文过长。

重试插件会：

1. 判断这个错误是否允许重试；
2. 把“计划重试”写进会话记录；
3. 等待退避时间；
4. 把“重试开始”写进记录；
5. 让同一步骤再次请求模型。

用户刷新网页后仍能看到曾经发生过重试，而不是只看到最后结果。

## 24. 文件 read/write/edit 为什么比较安全？

默认文件策略要求“先观察，再修改”。

### 修改现有文件

先读取文件时，程序记住一个版本标记。

写入或编辑时，底层再次检查文件版本：

- 版本一致：允许；
- 文件被外部程序改过：拒绝，要求重新读取；
- 文件消失：拒绝或按新建逻辑处理。

### 新建文件

不要求先读取一个不存在的文件，但最终发布时必须确认目标仍不存在。如果有其他程序抢先创建了文件，本次写入拒绝，不会覆盖对方内容。

### 原子写入

新内容先写到同目录的私有临时文件，完整写入并同步后才替换最终文件。进程中途崩溃时，不容易留下只写了一半的正式文件。

## 25. 文件工具与 Bash 沙箱有什么区别？

文件工具由项目自己的可信代码执行。它只需要判断目标路径是否位于允许目录中。

Bash 会运行模型控制的任意程序，所以必须使用操作系统级限制工具：

- Linux：bwrap 或 Landlock；
- macOS：Seatbelt；
- Windows：受限令牌和 ACL。

文件工具的路径检查不是通用代码沙箱。Bash 沙箱也只承诺限制文件修改，不代表没有网络访问。

## 26. 三种权限模式是什么意思？

### read-only

受限制的操作不能修改文件。

### workspace-write

允许修改当前工作区和部分临时目录。

### danger-full-access

Harness 文件沙箱不限制修改范围。

官方基础组合默认使用：

```text
workspace-write + ask
```

也就是工作区内可写，扩大权限时询问用户。

## 27. 模型怎样申请临时扩大权限？

模型不能随意把权限改成更大。

它必须在工具调用中同时提供：

- `sandbox_permissions`；
- `justification`。

程序先检查目标权限确实比当前权限更宽，然后发起一次用户审批。

只有用户选择“允许一次”，当前这一次操作才使用更宽权限。下一条命令仍回到原权限。

用户拒绝、取消、无人回答或没有审批服务时都不会执行。

## 28. 子进程为什么要管理“整棵进程树”？

一条命令可能继续启动子进程。

如果只杀最外层 Bash，里面的服务或脚本可能继续运行，形成孤儿进程。

所以本地子进程实现会：

- POSIX 使用独立进程组；
- Windows 使用 taskkill 的 tree 模式；
- 先发送温和终止；
- 等待一段时间；
- 仍未退出则强制终止；
- 等待整个进程树不再运行。

## 29. 长输出怎样处理？

命令输出不会无限保存在内存中。

程序优先保留输出尾部，因为错误和最终结果通常在末尾。

如果输出超过内存限制，可以把完整输出写入权限受限的临时文件，并给模型一个路径。

如果连完整输出文件的上限也超过，就删除不完整的外置文件，只保留内存尾部，避免磁盘无限增长。

## 30. 后台任务是怎样工作的？

长命令可以返回一个 JobId，例如：

```text
bash-1
```

模型之后可以：

- `job_list` 查看；
- `job_output` 读取增量输出；
- `job_kill` 请求停止。

后台任务必须属于某个确切 Agent。另一个会话即使猜到 `bash-1` 也不能读取。

Agent 被销毁时，它的后台任务会收到取消并被等待。

为了防止“任务完成唤醒 Agent，Agent 又启动任务”的无限循环，自动完成通知有连续唤醒次数上限。

## 31. 子 Agent 是什么？

主 Agent 可以把任务交给另一个 Agent。

项目支持：

- 新建一个空白子 Agent；
- 从父 Agent 已完成历史复制一份上下文；
- 调用外部 ACP Agent；
- 调用 Codex；
- 调用 Claude Code；
- 调用另一个 DSH SDK runtime。

子 Agent 有自己的会话和工具范围，不是父 Agent 内的一条普通函数调用。

## 32. 一次性子 Agent 与可继续子 Agent

### 一次性

接收一个任务，返回一个最终结果，然后释放。

### 可继续

子会话会保存下来。父 Agent 以后可以继续发送消息。

可继续子 Agent 没有另外设计一套消息队列，仍使用普通 Agent inbox。没有运行中的子 Agent 对象时，可以从保存的 Session 冷恢复。

权限根据真实父子关系判断，不根据消息中自称“我是父 Agent”的文字判断。

## 33. Workflow 是什么？

Workflow 允许模型写一个编排脚本，一次启动很多子 Agent。

例如：

```js
const results = await parallel([
  () => agent('检查安全问题'),
  () => agent('检查性能问题'),
  () => agent('检查测试缺口')
])
return results
```

它适合批量审查、迁移和多角度研究。

Workflow 有并发数、总 Agent 数、单批项目数和取消宽限时间限制。脚本卡死时可以强制结束 Worker，并清理已经启动的子 Agent。

## 34. 对话太长时如何处理？

程序会估算当前上下文大小。

估算优先使用模型返回的 token usage；数据不完整或看起来偏小时，使用本地保守估算。

达到阈值后：

1. 先缩短特别大的工具结果；
2. 再选取较早且不会拆开工具调用/结果的一段历史；
3. 调用模型生成结构化摘要；
4. 确认摘要真的比原内容短；
5. 用摘要替换模型视图中的旧区段；
6. 保留全部原始记录。

如果模型明确报告上下文过长，程序可以跳过普通阈值，强制尝试一次有效缩减。

## 35. Web 页面怎样收到实时变化？

浏览器上行使用普通 HTTP POST：

- 发送消息；
- 创建会话；
- 修改设置；
- 回答审批。

Host 下行使用两条 WebSocket：

- 一条传会话细节；
- 一条传会话列表、状态和 Workspace 变化。

任意一条断开，浏览器会重建这一代连接，然后重新拉取列表和历史。

## 36. 为什么需要两条 WebSocket？

会话内部流式事件很多，而全局实体变化相对较少。分开后职责更清楚。

但两条连接之间没有统一顺序。因此客户端不能假设“某帧一定先到”，而是通过：

- 会话序号；
- 完整快照；
- 稳定请求 ID；
- 重连基线；
- 重复清理；

让不同到达顺序最终得到相同状态。

## 37. 浏览器漏掉一条会话事件怎么办？

客户端保存的会话窗口必须连续。

如果收到的事件序号不是当前尾部加一，客户端不会直接把它插进去，而是：

1. 暂存后续实时事件；
2. 重新请求最近一页历史；
3. 安装连续历史；
4. 按序号去重并拼回实时事件。

所以单个坏帧或重连窗口不会让工具调用和结果错配。

## 38. Web API 有哪些安全限制？

每个 `/api` 请求都会检查 Host 地址，防止恶意网页利用 DNS Rebinding 访问本机服务。

同时检查：

- 明确 cross-site 的请求；
- Origin 是否与 Host 一致；
- POST 是否为 JSON；
- Host 是否为 loopback 或配置的可信地址。

设置、凭据、打开本机文件、编辑 Agent preset 等高权限 API 只允许 loopback。

但是 `trustedHosts` 不是登录认证。把服务开放到 `0.0.0.0` 后，普通 Session API 仍可能让远程访问者驱动编码 Agent。因此当前版本不应直接暴露到不可信网络。

## 39. 浏览器页面为什么也采用插件？

Host 会扫描当前插件列表，找出带浏览器部分的包，生成一张启动清单。

浏览器按清单加载每个 `client.js`，再用浏览器内的 Cordis 启动这些插件。

所以：

- 增加后端插件可以同时增加前端界面；
- 不需要在中央 App 中手写全部功能；
- 每个 UI 功能可以独立注册自己的位置；
- 插件移除后，对应界面也可以移除。

## 40. UI Slot 是什么？

Slot 可以理解成网页中事先声明的插槽。

例如：

- 侧边栏；
- 输入栏左侧；
- 输入栏右侧；
- 会话标题操作区；
- 工具卡片；
- 设置页面；
- 对话视图标签页。

业务插件把组件放进指定插槽，不直接修改中央页面组件。

有些插槽允许多个组件按顺序排列；有些只能有一个；有些根据条件选择第一个匹配组件。

## 41. 网页是怎样显示不同业务事件的？

不是一个巨大的 `switch(event.type)` 处理所有 UI。

业务插件可以注册“如何把一组事件组成一个界面节点”。

例如 Workflow 会把：

- run start；
- phase；
- child start/end；
- run end；

组合成一个可展开的 Workflow 节点。

工具调用也先组成稳定调用树，再由工具名选择对应卡片。

未知类型仍使用通用显示，不会因为插件已经卸载就无法打开旧记录。

## 42. Settings 与 Credentials 为什么分开？

Settings 适合保存普通配置，例如：

- 模型名称；
- timeout；
- 是否启用某功能；
- provider 地址。

Credentials 专门保存秘密，例如 API key。

Settings 中只写：

```text
apiKeyEnv: DEEPSEEK_API_KEY
```

真正的 key 由 Credentials 提供。

网页读取设置时，秘密字段会被删除，只显示“是否已设置”。因此浏览器永远不需要收到完整 key。

## 43. 修改设置如何避免覆盖别人的新修改？

每个设置分区有一个递增版本号。

网页读取后，写入时带回自己看到的版本号。如果期间另一个标签页已经修改，Host 会拒绝旧版本写入，而不是覆盖新值。

对于已经删除秘密字段的网页视图，修改单个字段使用路径操作，不会重建整份配置并误删自己看不到的秘密。

## 44. Workspace 是什么？

Workspace 是一个用户工作目录的持久记录。

它包含：

- 稳定 ID；
- 规范化目录路径；
- 显示名称；
- 会话排列顺序。

Workspace ID 不使用路径，因为路径拼写、软链接和目录名可能改变。

删除 Workspace 注册不会删除真实目录，也不会删除会话。会话只是变成未分组状态。

## 45. 图片为什么不直接存进聊天 JSON？

Base64 图片很大，会让每条日志膨胀，也不利于多个 fork 会话共享。

所以流程是：

```text
接收图片字节
  -> 验证格式和尺寸
  -> 规范化
  -> 持久保存
  -> 得到内容寻址引用
  -> 会话只记录引用
```

模型请求时，再根据具体模型限制生成适合该模型的图片版本。

## 46. Skills、MCP 和 Hooks 分别是什么？

### Skills

可发现的 Markdown 工作说明。模型先看到简短目录，需要时通过 `skill` 工具加载全文。

### MCP

连接外部 MCP Server，把对方工具变成本项目原生工具。公开名称包含服务器名，避免不同服务器的 `search` 重名。

### Hooks

兼容 Claude Code 和 Codex 的外部 shell 钩子。在会话开始、用户提交、工具前后和停止时执行外部命令，并把结果翻译成本项目的统一决定。

## 47. ACP 和 SDK 有什么区别？

ACP 面向通用自动化客户端，支持创建新会话、发送提示、取消和权限回答，但不提供完整 Web UI 功能。

DSH JSON-RPC SDK 是项目自己的进程外调用协议。TypeScript 和 Python SDK 通过 stdio 启动一个 Harness runtime，并订阅 Session event/status。

Python 高层 `run()` 会先确认自己的消息确实进入 inbox，然后一直等到 Agent 空闲，再从事件中取最终回答。

## 48. Telemetry 默认会发送什么？

官方组合默认关闭遥测。

显式开启后，遥测模块会复制大多数会话事件和少量运行错误，再交给 OpenTelemetry 后端。

必须注意：遥测核心本身不自带脱敏规则。部署没有安装脱敏监听器时，导出副本就是捕获到的原始数据。因此开启遥测前应明确检查实际规则和目标地址。

## 49. 项目的测试为什么这么多？

因为这类程序最容易在以下地方出错：

- 取消与完成同时发生；
- 插件热更新时资源提前释放；
- 子进程留下后代；
- 网络流中断；
- 历史恢复缺少最后一条；
- 工具调用和结果错配；
- 浏览器帧乱序；
- 源码运行正常但构建产物坏了。

项目因此同时使用：

- 单元测试；
- 每文件覆盖率；
- 真实模型测试；
- 协议快照；
- 浏览器快照；
- 构建产物冒烟测试；
- Python runtime 测试；
- 多 Node 版本和多平台检查。

## 50. 最值得先读的源码

如果只想理解主流程，按这个顺序：

1. `apps/cli/src/bin.ts`——命令入口；
2. `apps/cli/src/profile-boot.ts`——插件清单怎样组成；
3. `packages/bundle/base/cordis.patch.yml`——官方默认加载什么；
4. `packages/core/agent-loop/src/agent.ts`——Agent 怎样跑；
5. `packages/core/session/src/index.ts`——记录怎样保存；
6. `packages/core/tools/src/index.ts`——工具怎样执行；
7. `packages/llm/llm/src/index.ts`——模型怎样选择；
8. `packages/llm/llm-deepseek/src/adapter.ts`——DeepSeek 请求怎样发送；
9. `packages/host/apiproxy/src/api-proxy.ts`——Web 怎样驱动 Agent；
10. `packages/client/runtime/src/client/sessions/session.ts`——浏览器怎样恢复和显示会话。

## 51. 用一个完整例子串起来

任务：

> 请读取 package.json，把版本号改掉，然后运行测试。

程序的实际动作：

```text
1. Web 把用户消息 POST 给 Host
2. Host 创建 UserMessage 并放入 Agent inbox
3. inbox 变化写入 Session
4. Agent Loop 开启 turn
5. 组装系统提示词、工具 schema 和历史
6. LLM Adapter 调用 DeepSeek
7. 模型返回 read(package.json)
8. ToolRuntime 检查参数和权限
9. FileSystem 读取文件并记录版本
10. 结果写入 tool/result
11. Agent Loop 再次调用模型
12. 模型返回 edit(package.json, old, new)
13. FS observation policy 取出先前版本
14. LocalFileSystem 在锁内确认版本未变
15. 写入临时文件并原子替换
16. 新版本和 diff 写入 tool/result
17. Agent Loop 再次调用模型
18. 模型返回 bash(test command)
19. Sandbox policy 解析 workspace-write
20. Sandbox provider 包装 bash argv
21. Subprocess 启动独立进程树并收集输出
22. 测试退出结果写入 tool/result
23. 模型生成最终说明
24. turn/end 写入 Session
25. Persistence flush 保存日志
26. Mux/Host WebSocket 推送事件和状态
27. Browser Conversation assembler 更新聊天节点
28. React 工具卡片显示 read、diff 和 terminal
```

这个例子基本贯穿了项目最重要的源码。

## 52. 常见术语翻译

| 术语 | 通俗解释 |
| --- | --- |
| Harness | 把模型、工具、记录和界面组织起来的运行平台 |
| Agent | 一个拥有会话、队列、模型和工具范围的工作实例 |
| Plugin | 可安装、可卸载的功能模块 |
| Service | 插件之间约定好的功能入口 |
| Context | 当前插件能访问的服务和作用范围 |
| Fiber | 一个插件本次运行的生命周期对象 |
| Effect | 插件安装的、卸载时必须撤销的操作 |
| Inject | 声明“这个插件启动前需要哪些服务” |
| Waterfall | 一串可以继续、改写或截断的处理器 |
| Session | 只追加的完整会话记录 |
| Event | 会话或运行中发生的一项事实 |
| Surface | 当前模型应该看到的消息列表 |
| Projection | 从完整记录算出的当前状态摘要 |
| Provider | 某项能力的具体实现者 |
| Consumer | 使用某项能力的工具或业务插件 |
| Seam | 可以替换实现的能力接口 |
| Profile | 一套可启动的插件组合 |
| Bundle | 向插件组合中增加或修改条目的配置层 |
| Preset | 某类 Agent 专用的提示词和工具组合 |
| Scope | 某个 Agent 或 preset 能看到的注册范围 |
| Isolate | 让同名服务在不同组合中拥有独立实例 |
| Inbox | Agent 尚未处理的消息队列 |
| Turn | 一次用户工作从开始到暂时结束 |
| Step | 一次模型请求及其工具执行 |
| Adapter | 把统一模型格式翻译成某家 API 格式 |
| Compaction | 用摘要缩短模型上下文，但保留原始记录 |
| Retry | 失败后按策略再次请求 |
| Backoff | 重试前逐步延长等待时间 |
| Snapshot | 某一时刻的完整状态副本 |
| Baseline | 重连或首次加载时的权威起始状态 |
| HMR | 进程不退出的情况下卸载并重载插件 |
| RPC | 浏览器/SDK 调用 Host 方法的消息格式 |
| SSE/WebSocket | Host 持续向客户端推送事件的通道 |
| Branded ID | 看起来是字符串，但类型上不能与其他 ID 混用 |
| CAS | 只有数据仍是之前看到的版本时才修改 |
| Atomic write | 要么完整替换成功，要么旧文件保持不变 |
| Quiescence | 所有正在运行和清理的工作都真正停稳 |
| Fail closed | 无法确认安全或权限时默认拒绝 |
| Tool result spill | 把过大的工具文本保存到文件，只内联预览 |
| Continuable subagent | 可以保存并在以后继续对话的子 Agent |

## 53. 用最简单的话总结架构

DeepSeek Harness 的核心思想可以浓缩成下面几句话：

1. 用户消息先进入可保存的队列，不直接调用模型；
2. 模型看到的一切都必须能从会话记录重新算出来；
3. 模型工具经过统一的参数、权限、取消和结果流程；
4. 文件修改使用版本检查和原子替换，避免盲目覆盖；
5. Shell 命令由沙箱和进程树管理，但文件沙箱不代表网络隔离；
6. 对话过长时只缩短模型视图，不删除原始历史；
7. 子 Agent、Workflow、Web 和 SDK 都复用同一个 Agent 核心；
8. 后端和浏览器都由插件组成，因此功能可以替换；
9. 每种实时数据都设计了恢复或重连办法；
10. 无法确认权限、状态或格式时，项目多数路径选择明确失败，而不是悄悄降级。
