# Oh My Pi 全部源码静态研究与 Pi 差异分析

> 研究对象：Oh My Pi（OMP）  
> 上游仓库：https://github.com/can1357/oh-my-pi  
> 本地源码：<code>D:\0715\git_codex\20260825\oh-my-pi-main</code>  
> OMP 固定提交：<code>eab72e88e447a4be45bea2bc302995844c0c51a2</code>  
> OMP 提交时间：2026-08-25 16:16:30 UTC  
> OMP 工作区版本：<code>18.0.5</code>  
> 对照项目：Pi  
> Pi 本地源码：<code>D:\0715\git_codex\20260825\pi</code>  
> Pi 固定提交：<code>dcd461925db2edf69a43c8135db1180d418afd54</code>  
> Pi 工作区版本：<code>0.84.3</code>  
> 分析日期：2026-08-26  
> 分析方法：固定提交源码静态审查、仓库内文档交叉验证、注册表/配置默认值/调用链核对；未执行真实模型、浏览器、桌面、LSP、DAP 和远程协作端到端测试。

---

## 目录

- [第一部分：技术版源码解析](#第一部分技术版源码解析)
  - [1. 研究范围与核心结论](#1-研究范围与核心结论)
  - [2. OMP 与 Pi 的关系](#2-omp-与-pi-的关系)
  - [3. 仓库规模、语言与运行时](#3-仓库规模语言与运行时)
  - [4. 总体架构与包边界](#4-总体架构与包边界)
  - [5. CLI 启动与会话创建链](#5-cli-启动与会话创建链)
  - [6. Agent Loop：从轻量循环到可中断运行内核](#6-agent-loop从轻量循环到可中断运行内核)
  - [7. 模型、供应商、角色与凭证](#7-模型供应商角色与凭证)
  - [8. 工具注册、发现与权限模型](#8-工具注册发现与权限模型)
  - [9. 文件读取、搜索与编辑能力](#9-文件读取搜索与编辑能力)
  - [10. Rust 原生层与跨平台 Shell](#10-rust-原生层与跨平台-shell)
  - [11. LSP 代码智能系统](#11-lsp-代码智能系统)
  - [12. DAP 调试系统](#12-dap-调试系统)
  - [13. Eval 持久化代码执行](#13-eval-持久化代码执行)
  - [14. 子代理、Agent Hub 与 Advisor](#14-子代理agent-hub-与-advisor)
  - [15. Web、GitHub、浏览器与桌面控制](#15-webgithub浏览器与桌面控制)
  - [16. MCP、扩展、规则与内部 URL](#16-mcp扩展规则与内部-url)
  - [17. 记忆、自学习与技能](#17-记忆自学习与技能)
  - [18. 会话、分支、压缩与恢复](#18-会话分支压缩与恢复)
  - [19. 协作、RPC、ACP 与远程运行](#19-协作rpcacp-与远程运行)
  - [20. 安全边界与风险审查](#20-安全边界与风险审查)
  - [21. 构建、测试与发布体系](#21-构建测试与发布体系)
  - [22. OMP 相比 Pi 的功能差异总表](#22-omp-相比-pi-的功能差异总表)
  - [23. OMP 不是 Pi 的严格超集](#23-omp-不是-pi-的严格超集)
  - [24. 源码阅读路线](#24-源码阅读路线)
- [第二部分：白话版源码讲解](#第二部分白话版源码讲解)
- [第三部分：专业名词中英对照](#第三部分专业名词中英对照)
- [第四部分：建议的动态验证清单](#第四部分建议的动态验证清单)
- [结论](#结论)

---

# 第一部分：技术版源码解析

## 1. 研究范围与核心结论

### 1.1 一句话定位

Pi 是一个“小而清晰、可扩展的终端编码代理框架”；Oh My Pi 则在其思想和部分代码基础上，发展成一个“多模型、多工具、多代理、多运行时、带代码智能和远程协作能力的完整代理工作台”。

### 1.2 最重要的结论

1. **OMP 的提升不是单点功能，而是产品层级的扩张。**  
   它把 Pi 的四个默认编码工具扩展成 29 个注册表内建工具、3 个隐藏控制工具，以及按配置动态挂载的图像生成、语音、MCP 和扩展工具。

2. **OMP 的 Agent Loop 已经不是 Pi 那个刻意保持精简的循环。**  
   OMP 的 <code>packages/agent/src/agent-loop.ts</code> 约 2950 行，加入截止时间、全局暂停门、运行中转向、对等代理中断、软工具要求、动态工具选择、调用参数变换、合成工具结果、遥测和更细的失败恢复。

3. **工具并发从“统一并行”升级为“共享/独占调度”。**  
   每个工具可声明 <code>shared</code> 或 <code>exclusive</code>。共享工具在相邻独占屏障之间并发；独占工具等待此前所有共享任务和前一个独占任务完成，适合写文件、调试器状态等不可安全并发的操作。

4. **OMP 增加真正的一等子代理系统。**  
   <code>task</code> 工具支持批量并发、递归、持久会话、转向、暂停/恢复、结构化输出校验，以及 APFS、btrfs、ZFS、reflink、overlayfs、ProjFS、block clone、Git worktree/递归复制等隔离后端。

5. **OMP 把 LSP 与 DAP 做成了代理可直接调用的核心能力。**  
   LSP 暴露 14 类操作，DAP 暴露 28 类操作。写入、重命名、格式化和诊断不是孤立工具，而是与编辑链路联动。

6. **OMP 的 <code>eval</code> 不是一次性脚本执行。**  
   Python、Bun/JavaScript、Ruby、Julia 后端可维护持久内核状态，并允许执行中的代码再次调用代理工具；这使它更像代理的交互式计算环境。

7. **OMP 用 Rust 原生层替换大量对外部命令和平台差异的依赖。**  
   原生层负责嵌入式 Shell、grep/glob/文件遍历、AST、隔离、音频、桌面、PDF/图像等能力，在 Windows 上不要求 WSL 才能获得统一的 Unix 风格工具体验。

8. **模型层明显扩大。**  
   源码中有 70 个供应商描述符、66 个模型目录键和约 4516 条打包模型目录记录；同时引入 default、smol、slow、vision、plan、designer、commit、tiny、task、advisor 十个内建模型角色。

9. **编辑链路更强调“防止模型基于旧上下文误改”。**  
   Hashline 用文件内容短哈希绑定编辑锚点；AST Edit 支持结构化变更和暂存接受；LSP writethrough 在写入后更新语言服务器并收集诊断。

10. **会话仍以 Pi 风格 JSONL 追加树为主，但压缩策略大幅增强。**  
    除模型摘要外，还有 provider-native remote compaction、handoff、shake 机械删减，以及把旧历史渲染为位图帧的 snapcompact。

11. **OMP 新增了记忆、自学习、浏览器、桌面、实时协作和语音等高权限功能。**  
    功能越强，隐私和执行面越大；尤其是浏览器、computer、eval、MCP、扩展、协作和持久记忆，需要单独配置安全策略。

12. **OMP 不是当前 Pi 的严格超集。**  
    当前 Pi 仓库含 client、protocol、server、telemetry、evals 和实验性 durable harness/session v4/SQLite 方向；OMP 并未以相同包和相同架构保留这套新设计，而是继续强化自己的 AgentSession + JSONL + Bun/Rust 产品架构。

### 1.3 本报告里的“多了什么”如何定义

本报告区分四种情况：

- **真正新增**：Pi 基线没有，OMP 中存在完整实现。
- **明显增强**：Pi 有同名或相似能力，但 OMP 增加了协议、状态机、并发或跨平台实现。
- **架构替换**：OMP 实现了相同目标，但采用不同底层设计。
- **未继承/不同方向**：当前 Pi 已新增，而 OMP 没有以同等形式保留。

这样可以避免只看 README 功能清单造成错误结论。

---

## 2. OMP 与 Pi 的关系

### 2.1 它是长期分叉，不是简单主题包

OMP 仓库自己的 <code>docs/porting-from-pi-mono.md</code> 明确把 Pi 视为需要持续“语义移植”的上游。历史同步标记是 2026-03-22 的上游提交 <code>b21b42d...</code>，但文档也强调：

- 不做整仓覆盖；
- 先比较旧实现和新实现；
- 保护 OMP 已增强的功能；
- 遇到上游重构时按语义迁移，而不是机械合并；
- 某些上游文件和架构明确跳过。

这说明双方已经形成独立演化线。

### 2.2 明确的架构分歧

| 领域 | Pi | Oh My Pi |
|---|---|---|
| 主运行时 | Node.js 22，部分二进制用 Bun 编译 | Bun 优先，TypeScript 源码直接运行/打包 |
| 包作用域 | <code>@earendil-works/*</code> | <code>@oh-my-pi/*</code> |
| 测试框架 | Vitest、Node test | <code>bun:test</code> 与自定义分片执行器 |
| 工具构造 | 按 cwd/options 创建工具 | <code>createTools(ToolSession)</code> 注册表工厂 |
| 认证存储 | auth.json + proper-lockfile | <code>agent.db</code> + <code>bun:sqlite</code> |
| 凭证模型 | 每供应商单凭证为主 | 多凭证轮询、会话亲和、退避 |
| 扩展加载 | jiti/Node 模块体系 | Bun 原生动态导入、能力发现、EventBus |
| 状态栏 | FooterDataProvider | StatusLineComponent |
| 剪贴板图片 | 上游工具文件 | Rust/native 后端 + TS 包装 |
| 会话主线 | JSONL v3 + 新实验 durable harness v4 | 强化版 JSONL v3 与大量运行时扩展 |

### 2.3 公平的比较基线

本报告用用户提供的本地 Pi 提交做基线，而不是用 OMP 分叉时的旧 Pi。这样会出现一个重要结果：

> OMP 在“终端代理产品能力”上远多于 Pi，但当前 Pi 在“可嵌入协议化 Agent Harness”方向上有 OMP 未按同样方式实现的新工作。

---

## 3. 仓库规模、语言与运行时

### 3.1 文件规模

对固定源码快照使用 <code>rg --files</code> 统计：

| 指标 | 数量 |
|---|---:|
| 全部文件 | 6613 |
| TypeScript/TSX | 4702 |
| Rust | 389 |
| Python | 176 |
| Markdown | 525 |
| 测试/fixture 特征路径 | 2736 |

说明：

- “测试/fixture”按路径和文件名启发式判断，不等同于真实测试用例数。
- 源码包含生成文件、前端、基准、fixture 和 vendored Rust 代码，不能把总行数直接视为手写生产逻辑。
- README 所说的“约 8 万行 Rust core”采用的是项目自己的核心口径；若把全部 crate、测试和 vendored 代码算入，数字会更大。

### 3.2 技术栈

| 层 | 主要技术 |
|---|---|
| CLI/代理业务 | TypeScript、Bun |
| TUI | 自研差分终端 UI |
| 原生性能层 | Rust、N-API |
| 持久执行/远程 SDK | Python、Ruby、Julia、JavaScript |
| 本地数据库 | <code>bun:sqlite</code> |
| 浏览器 | Puppeteer Core、CDP |
| Web 协作界面 | React/Solid/Vite 相关包 |
| Schema | ArkType、omptype、JSON Schema/JTD 转换 |
| 遥测 | OpenTelemetry |
| 构建与发布 | Bun、Cargo、Bazel、Nix、Homebrew、安装脚本 |

### 3.3 为什么采用多语言

- TypeScript 适合模型协议、会话状态和工具编排。
- Rust 适合搜索、文件遍历、Shell、PTY、AST、隔离、音视频和跨平台系统调用。
- Python/Ruby/Julia 是被代理控制的计算后端，也是远程服务生态的一部分。
- 浏览器协作界面需要前端技术栈。

OMP 的代价是构建矩阵、调试路径和发布复杂度显著高于 Pi。

---

## 4. 总体架构与包边界

### 4.1 工作区包

OMP 的 17 个 JavaScript/TypeScript 工作区包如下：

| 目录 | 包名 | 职责 |
|---|---|---|
| agent | <code>@oh-my-pi/pi-agent-core</code> | Agent Loop、消息、压缩、暂停、遥测 |
| ai | <code>@oh-my-pi/pi-ai</code> | 模型协议、供应商适配、认证网关、用量 |
| browser-relay | <code>@oh-my-pi/browser-relay</code> | Chrome 扩展与 browser 工具中继 |
| catalog | <code>@oh-my-pi/pi-catalog</code> | 模型目录、供应商描述与能力数据 |
| coding-agent | <code>@oh-my-pi/pi-coding-agent</code> | CLI、会话、工具、TUI、扩展和产品功能 |
| collab-web | <code>@oh-my-pi/collab-web</code> | 浏览器访客端、协作中继 |
| hashline | <code>@oh-my-pi/hashline</code> | 带内容哈希的行锚定补丁语言 |
| metaharness | <code>@oh-my-pi/pi-metaharness</code> | 基准运行和结果存储 |
| mnemopi | <code>@oh-my-pi/pi-mnemopi</code> | 本地 SQLite 记忆引擎 |
| natives | <code>@oh-my-pi/pi-natives</code> | Rust N-API 绑定 |
| omptype | <code>@oh-my-pi/omptype</code> | 运行时 Schema 验证 |
| snapcompact | <code>@oh-my-pi/snapcompact</code> | 面向视觉模型的位图上下文压缩 |
| stats | <code>@oh-my-pi/omp-stats</code> | 本地使用量和统计面板 |
| tui | <code>@oh-my-pi/pi-tui</code> | 差分终端 UI |
| typescript-edit-benchmark | 同名 benchmark 包 | TypeScript 编辑基准 |
| utils | <code>@oh-my-pi/pi-utils</code> | 通用工具 |
| wire | <code>@oh-my-pi/pi-wire</code> | 跨包线协议类型 |

Python 侧还有：

- <code>python/omp-rpc</code>：OMP RPC 客户端；
- <code>python/robomp</code>：远程运行、Web 服务与容器化相关组件。

### 4.2 Rust crate

| crate | 主要作用 |
|---|---|
| pi-natives | JS 可见的 N-API 总入口 |
| pi-builtins | Shell 内建命令和进程内 coreutils/findutils/sed/jq 等 |
| pi-shell | 持久嵌入式 Shell、管道、进程和 PTY |
| pi-voice | 录音、播放、Opus/WebRTC |
| pi-ast | tree-sitter/ast-grep 语言注册、匹配、编辑、代码块分析 |
| pi-iso | 多平台隔离、快照、差异提取 |
| pi-walker | 并行、可缓存、遵守 ignore 规则的文件遍历 |
| vendor/brush-core | 嵌入式 Shell 解析和执行引擎 |

### 4.3 依赖关系

~~~mermaid
flowchart TD
    CLI[omp CLI / TUI / RPC / ACP] --> SESSION[AgentSession]
    SESSION --> CORE[pi-agent-core]
    SESSION --> TOOLS[ToolSession + createTools]
    SESSION --> AI[pi-ai + catalog]
    SESSION --> EXT[能力发现 / 扩展 / MCP / 规则]
    SESSION --> STORE[JSONL 会话 / SQLite 凭证与记忆]

    CORE --> LOOP[Agent Loop]
    CORE --> COMPACT[Compaction / Shake / Snapcompact]
    TOOLS --> CODE[Read / Grep / Hashline / AST]
    TOOLS --> INTEL[LSP / DAP]
    TOOLS --> EXEC[Bash / Eval]
    TOOLS --> MULTI[Task / Hub / Advisor]
    TOOLS --> REMOTE[Web / GitHub / Browser / Computer]

    CODE --> NATIVE[pi-natives N-API]
    EXEC --> NATIVE
    INTEL --> NATIVE
    NATIVE --> RUST[pi-shell / pi-builtins / pi-ast / pi-iso / pi-walker / pi-voice]
~~~

### 4.4 最大的工程特征

OMP 的中心不是某一个工具类，而是 <code>ToolSession</code>。它是“会话级依赖胶囊”，向工具提供：

- cwd、工作区树、附加目录和可信状态；
- UI 与审批能力；
- 设置和当前模型；
- LSP、MCP、扩展、自定义工具；
- Agent Registry、子代理深度、任务并发；
- 记忆后端和技能；
- Blob、artifact、异步 Job；
- 环境、认证和会话生命周期清理。

优点是工具可以复用完整运行环境；缺点是该接口很大，模块之间的隐式耦合也更强。

---

## 5. CLI 启动与会话创建链

### 5.1 入口

核心入口：

- <code>packages/coding-agent/src/cli.ts::runCli()</code>
- <code>packages/coding-agent/src/main.ts::main()</code>
- <code>packages/coding-agent/src/main.ts::runRootCommand()</code>
- <code>packages/coding-agent/src/sdk.ts::createAgentSession()</code>

### 5.2 启动顺序

~~~mermaid
sequenceDiagram
    participant U as 用户/宿主
    participant C as runCli
    participant M as runRootCommand
    participant S as Settings/Auth
    participant R as ModelRegistry
    participant SM as SessionManager
    participant E as Extensions
    participant AS as AgentSession
    participant MODE as TUI/RPC/ACP/Print

    U->>C: omp 参数
    C->>C: 解析根命令/子命令/worker
    C->>M: 进入主命令
    M->>M: 初始化主题、cwd、启动 watchdog
    par 并行预加载
        M->>E: plugin roots
        M->>S: AuthStorage
        M->>S: Settings
    end
    M->>R: ModelRegistry + 模型作用域
    M->>SM: 新建/继续/恢复/导入会话
    M->>E: 扩展、技能、规则、MCP 发现
    M->>AS: createAgentSession(options)
    M->>MODE: 交互、print、RPC、RPC-UI 或 ACP
~~~

### 5.3 启动阶段的关键设计

1. **协议模式先占用 stdin。**  
   RPC/ACP 的标准输入是 JSON 帧，不能被普通 piped prompt 读取。

2. **认证、设置、插件根预加载并行。**  
   OMP 明确在启动路径上优化 IO 重叠，并有启动 watchdog。

3. **恢复会话可能切换 cwd。**  
   切换后重新加载项目设置、插件根和模型作用域，避免沿用错误项目配置。

4. **扩展参数在创建 AgentSession 前解析。**  
   这样扩展定义的 CLI flag 可以影响会话，又不会因参数错误留下空会话。

5. **运行模式分支明显。**  
   交互 TUI、单次打印、RPC、带 UI 的 RPC、ACP 都共享会话内核，但输入输出宿主不同。

---

## 6. Agent Loop：从轻量循环到可中断运行内核

### 6.1 Pi 基线

Pi 的 Agent Loop 主要职责是：

1. 组装上下文；
2. 调模型流；
3. 收集 assistant 消息和 tool call；
4. 执行工具；
5. 按稳定顺序返回 tool result；
6. 有新工具调用则继续下一轮。

它刻意把策略交给上层 AgentSession 和扩展。

### 6.2 OMP 增强点

OMP 在此基础上加入：

- 绝对截止时间和超时 AbortSignal；
- 全进程暂停门 <code>agentPauseGate</code>；
- 模型调用前 gate；
- 模型调用前上下文同步；
- 每轮动态 <code>toolChoice</code>；
- 软工具要求：先提醒，必要时升级成强制工具选择；
- 中途转向消息；
- 父代理和同级代理的 IRC 输入；
- 运行中非消费式队列探测；
- 工具参数转换、回退工具、before/after tool hook；
- 供应商方言与 Harmony 相关恢复；
- provider 流错误后的合成 tool result；
- 截断 tool call 禁止执行；
- 运行级与工具级遥测；
- 暂停/继续后的状态保护。

### 6.3 一次调用的主链

~~~mermaid
flowchart TD
    A[agentLoop / agentLoopContinue] --> B[runLoop]
    B --> C[runLoopBody]
    C --> D{截止时间或暂停?}
    D -- 暂停 --> E[等待 pause gate]
    E --> C
    D -- 可运行 --> F[合并 steering/asides]
    F --> G[prepareProviderCall]
    G --> H[beforeModelCall gate]
    H --> I[streamAssistantResponse]
    I --> J{是否有 tool call}
    J -- 否 --> K[post-turn steering/aside/idle 检查]
    J -- 是 --> L[prepareToolCallDispatch]
    L --> M[executeToolCalls]
    M --> N[按 shared/exclusive 调度]
    N --> O[检测 steering/IRC]
    O --> P[真实或合成 tool result]
    P --> C
    K --> Q{还有输入?}
    Q -- 是 --> C
    Q -- 否 --> R[agent_end]
~~~

### 6.4 Shared/Exclusive 并发屏障

OMP 每个工具可声明并发模式：

- <code>shared</code>：允许与同一阶段的其他共享工具并行；
- <code>exclusive</code>：必须等待此前共享任务和此前独占任务完成，完成前阻止后续阶段跨越。

近似调度逻辑：

~~~text
shared A ─┐
shared B ─┼─ 并行 ─┐
shared C ─┘        │
                   ├─> exclusive D ─> shared E + shared F ─> exclusive G
此前 exclusive ────┘
~~~

这比简单 <code>Promise.all</code> 更适合具有副作用的工具集。

### 6.5 运行中转向为何复杂

转向消息到来时，OMP 不会粗暴终止所有工具：

- 纯等待型、声明可中断的工具，可被硬中止；
- bash 等可能产生部分副作用的前台工具只收到协作式 <code>steeringSignal</code>；
- 尚未启动的工具可以跳过；
- 已完成的真实结果必须保留；
- 已开始但中断的工具标记为可能有部分执行；
- 完全未执行的调用生成带 <code>__synthetic</code> 元数据的配对结果。

这解决了两个协议问题：

1. 模型供应商通常要求每个 tool call 都有 tool result；
2. 不能把“没有执行”伪装成“本地工具执行失败”，否则恢复和审计会失真。

### 6.6 长度截断保护

若模型因输出 token 上限停止，且工具参数未完整生成，OMP：

- 不执行该工具；
- 生成 <code>assistant_stop_length</code> 合成结果；
- 明确提示不要原样重试超大 payload，应拆分写入或编辑。

这是重要的数据安全边界。

---

## 7. 模型、供应商、角色与凭证

### 7.1 供应商和模型目录

静态注册表可见：

- 70 个供应商描述符；
- 66 个 <code>models.json</code> 供应商键；
- 约 4516 条打包模型目录记录。

二者数量不同并不矛盾：描述符包含可动态发现、别名、代理网关或无固定静态目录的供应商；目录记录也可能包含同模型的不同供应商映射。

支持范围包含 OpenAI、Anthropic、Google、Bedrock、Azure、OpenRouter、GitHub Copilot、GitLab Duo、DeepSeek、Kimi、MiniMax、Qwen、ZAI、xAI、Cerebras、Groq、Fireworks、Together、Hugging Face、Ollama、vLLM、LM Studio 等大量云端和本地后端。

### 7.2 十个内建模型角色

| 角色 | 中文解释 | 典型用途 |
|---|---|---|
| default | 默认模型 | 主对话与编码 |
| smol | 快速小模型 | 低成本辅助任务 |
| slow | 深度思考模型 | 高难推理 |
| vision | 视觉模型 | 图片理解 |
| plan | 规划模型 | 架构与计划 |
| designer | 设计模型 | UI/视觉设计 |
| commit | 提交模型 | 提交分析和消息 |
| tiny | 极小模型 | 分类、标题等轻任务 |
| task | 子任务模型 | 子代理默认角色 |
| advisor | 顾问模型 | 异步代码审查/监督 |

角色把“模型选择”从单一全局设置变成按职责路由。

### 7.3 多凭证认证

OMP 的认证差异是架构级的：

- 凭证保存在 SQLite <code>agent.db</code>；
- 一个供应商可保存多个凭证；
- 支持轮询选择；
- 保持会话亲和，避免每个请求频繁切换身份；
- 失败凭证进入退避；
- 支持 OAuth、API Key、认证代理/网关等不同通道。

这对团队账号、额度分流和多个订阅入口很实用，但也提高了凭证生命周期和审计复杂度。

---

## 8. 工具注册、发现与权限模型

### 8.1 注册表中的 29 个内建工具

<code>BUILTIN_TOOL_NAMES</code> 是源码权威清单：

| 分组 | 工具 |
|---|---|
| 文件与搜索 | read、write、edit、ast_grep、ast_edit、glob、grep |
| 执行与代码智能 | bash、eval、lsp、debug、security_scan |
| 交互与协调 | ask、task、hub、todo |
| Web 与桌面 | github、web_search、browser、computer、inspect_image |
| 恢复与记忆 | checkpoint、rewind、memory_edit、retain、recall、reflect、learn、manage_skill |

隐藏控制工具：

- <code>yield</code>：结构化子代理提交；
- <code>goal</code>：Goal 模式生命周期；
- <code>think</code>：外部思考通道。

README 所称“31 个内建工具”还包括按设置动态提供的 <code>generate_image</code> 和 <code>tts</code>。因此：

> 注册表常量是 29，隐藏控制工具是 3，产品文档口径是 31；MCP、扩展工具和动态设备不计入固定数字。

### 8.2 工具创建不是简单实例化

<code>createTools(session)</code> 会执行：

1. 规范化工具名和旧别名；
2. 应用工具白名单和受限会话规则；
3. 探测 Python/Ruby/Julia 后端；
4. 按设置启用或禁用 LSP、DAP、browser、memory 等；
5. 成对启用 checkpoint/rewind；
6. 为 grep/edit 自动补充 AST 对应工具；
7. 按记忆后端补充 recall/retain/reflect/memory_edit；
8. 按子代理深度决定是否暴露 task；
9. 并行调用工具工厂；
10. 用元信息和审批包装器封装；
11. 建立工具注册表；
12. 把低频工具挂到 <code>xd://</code>，降低每轮工具 Schema 的上下文成本。

### 8.3 xd://：把工具当成可发现设备

默认 <code>tools.xdev = true</code>。低频工具、MCP 工具和扩展工具可以不全部顶层暴露，而是：

- 在系统提示中只给出目录；
- 用 read 读取某个设备的说明/Schema；
- 用 write 调用或向设备提交数据；
- 核心常用工具仍保持顶层。

它解决了“工具越多，模型每轮都要支付越多 Schema token”的问题。

但有两个边界：

- 没有 write 权限的会话不会自动借由 <code>xd://</code> 获得写通道；
- 受限结构化子代理只暴露宿主明确授予的工具。

### 8.4 审批分级

工具可声明：

- read：只读；
- write：工作区写入；
- exec：执行、浏览器、子代理等高权限动作。

三种全局模式：

| 模式 | 自动批准 |
|---|---|
| always-ask | 只读 |
| write | 只读 + 写入 |
| yolo | read + write + exec |

用户级/工具级 allow、prompt、deny 规则可以覆盖全局模式。

非常重要：OMP 源码默认 <code>tools.approvalMode = yolo</code>。这意味着开箱默认倾向于自动批准所有层级；安全敏感环境应显式改为 <code>always-ask</code> 或至少 <code>write</code>。

---

## 9. 文件读取、搜索与编辑能力

### 9.1 Read 不只读取本地文本

OMP 的 read 工具同时承担资源统一入口，能够处理：

- 普通本地文件；
- 目录和工作区路径提示；
- HTTP/HTTPS 页面；
- PDF 和部分多媒体内容；
- GitHub/issue/PR；
- artifact、memory、skill、rule、security、MCP、SSH、历史和工具设备等内部 URL。

因此 read 的真实角色是“统一资源解析器”，而不仅是 <code>readFile()</code> 的包装。

### 9.2 原生 Grep/Glob/Walker

Pi 的 grep/find/ls 是可选编码工具，常依赖系统工具或 Node 文件遍历。OMP 把主要搜索路径下沉到 Rust：

- 并行文件遍历；
- ignore/globset 规则；
- 扫描缓存；
- 跨平台一致的 grep/glob；
- 与工作区树、增量刷新和内部 Shell 共享底层能力。

这在 Windows 上尤其重要：无需额外安装 GNU 工具或 WSL。

### 9.3 Hashline 编辑

Hashline 是 OMP 的独立包 <code>@oh-my-pi/hashline</code>，核心思想是：

1. 模型读取文件时，系统记录规范化全文的内容哈希；
2. 编辑段以 <code>[PATH#TAG]</code> 绑定读取时快照；
3. PUT/CUT/REM/MV 等操作使用行或语法块锚点；
4. 应用前验证当前文件是否仍匹配快照；
5. 若文件已变化，拒绝或基于旧快照进行三方恢复；
6. 多文件补丁先整体预检，减少批次只落一半。

它解决的是 LLM 编辑常见竞态：

> 模型看到的是旧文件，但在它提交补丁前，用户、格式化器、LSP 或另一个代理已经修改文件。

### 9.4 Hashline 操作语义

| 操作 | 含义 |
|---|---|
| PUT A.=B | 替换 A 到 B 行 |
| PUT A* | 替换从 A 开始的语法块 |
| PUT &lt;A / &gt;A | 在 A 前/后插入 |
| CUT A.=B / A* | 删除并捕获行或语法块 |
| REM | 删除整文件 |
| MV DEST | 移动/重命名文件 |

它还支持命名寄存器，把一个位置 CUT 的内容粘贴到另一位置。

### 9.5 AST Grep 与 AST Edit

<code>ast_grep</code> 和 <code>ast_edit</code> 通过 Rust <code>pi-ast</code> 与 tree-sitter/ast-grep 工作：

- 按语法结构匹配，而不是纯文本；
- 识别函数、类、调用、声明等节点；
- 批量结构化重写；
- staged preview；
- 通过 <code>xd://resolve</code> 或 <code>xd://reject</code> 接受/拒绝暂存变更；
- 与 Hashline 和格式化/诊断链路组合。

相较 Pi 的精确字符串 edit，这种方式更适合大范围重构，但语法支持、节点模式和格式化器仍会成为失败点。

### 9.6 LSP 写穿

文件写入后的链路不是“写完即结束”：

~~~mermaid
flowchart LR
    A[read/Hashline/AST 获取基线] --> B[edit/write]
    B --> C[审批与冲突检查]
    C --> D[落盘]
    D --> E[LSP didOpen/didChange/didSave]
    E --> F[格式化或重命名工作区编辑]
    F --> G[诊断 ledger]
    G --> H[延迟注入下一模型边界]
~~~

这样模型能在写入后看到编译/类型诊断，而不必每次手动跑构建。

---

## 10. Rust 原生层与跨平台 Shell

### 10.1 边界结构

~~~text
TypeScript 工具
  → @oh-my-pi/pi-natives
    → pi-natives（N-API 类型转换、异步任务、平台绑定）
      → pi-shell → brush-core + pi-builtins
      → pi-ast
      → pi-iso
      → pi-walker
      → pi-voice
~~~

### 10.2 嵌入式 Shell

OMP 的 bash 工具不等于每次启动系统 <code>bash -lc</code>：

- 会话复用持久 Shell 状态；
- 支持管道、进程、PTY 和取消；
- 内建大量 Bash/POSIX builtins；
- 进程内实现 cat、grep/rg、sed、ls、find、fd、diff、jq/jaq、ps、top、kill 和 moreutils 一类工具；
- 必要时仍可启动外部程序；
- Windows 下提供统一执行体验。

### 10.3 Bash 运行时增强

源码和文档显示 BashTool 还处理：

- cwd 与开头 <code>cd</code>；
- direnv/devenv 环境；
- 默认 300 秒超时，0 表示关闭；
- 输出流式传输；
- 大输出截断和 artifact 外置；
- 自动后台化；
- 异步 Job 交付；
- 内部 URL 参数展开；
- 宿主终端桥接；
- 无 Shell 场景的降级；
- 命令审批与拦截。

### 10.4 为什么比 Pi 更重

Pi 更倾向于组合系统已有工具，代码容易理解和维护。OMP 选择把执行底座纳入自己的发布物，得到：

- 一致性；
- 性能和缓存；
- 更细取消控制；
- Windows 友好；
- 可观察的进程边界。

代价是 Rust/Cargo/Bazel/N-API/多平台二进制发布链都必须维护。

---

## 11. LSP 代码智能系统

### 11.1 14 个动作

<code>packages/coding-agent/src/lsp/types.ts</code> 的 Schema 定义：

1. diagnostics
2. definition
3. references
4. hover
5. symbols
6. rename
7. rename_file
8. code_actions
9. type_definition
10. implementation
11. status
12. reload
13. capabilities
14. request

### 11.2 子系统组成

- Server defaults/config：按项目和语言发现服务器；
- Client：JSON-RPC 请求、通知、取消和超时；
- Mux daemon：复用语言服务器；
- Writethrough：写入/重命名同步；
- Diagnostics ledger：聚合、去重、延迟投递；
- Workspace diagnostics：多语言服务器并行；
- Formatting/options：保存后格式化；
- Tool：把 LSP 能力映射成模型可调用动作。

### 11.3 权限不是统一等级

LSP 工具会根据 action 动态分级：

- hover、definition、references、diagnostics 等属于 read；
- rename、rename_file、code action 应用等属于 write。

这比把整个 LSP 工具一律视为只读或执行更准确。

### 11.4 与 Pi 的差异

Pi 可以通过扩展实现 LSP，但基线没有将它作为写入链路的一等子系统。OMP 的关键增加不是“能查定义”，而是：

- 开机发现；
- 多服务器管理；
- 写穿；
- 格式化；
- 工作区重命名；
- 诊断异步注入；
- 子代理可按设置选择是否携带 LSP。

---

## 12. DAP 调试系统

### 12.1 28 个动作

<code>packages/coding-agent/src/tools/debug.ts</code> 定义：

1. launch
2. attach
3. set_breakpoint
4. remove_breakpoint
5. set_instruction_breakpoint
6. remove_instruction_breakpoint
7. data_breakpoint_info
8. set_data_breakpoint
9. remove_data_breakpoint
10. continue
11. step_over
12. step_in
13. step_out
14. pause
15. evaluate
16. stack_trace
17. threads
18. scopes
19. variables
20. disassemble
21. read_memory
22. write_memory
23. modules
24. loaded_sources
25. custom_request
26. output
27. terminate
28. sessions

### 12.2 架构

~~~mermaid
flowchart LR
    A[debug tool] --> B[DAP Session Manager]
    B --> C[DAP Client]
    C --> D[调试适配器]
    D --> E[Node/Python/C++/Rust/其他目标]
    D --> C
    C --> B
    B --> A
~~~

它维护调试会话、断点和事件，不是每次调用都重启调试器。

### 12.3 价值与风险

价值：

- 代理能观察真实运行时状态，而不是只猜源码；
- 可检查变量、调用栈、线程、内存；
- 适合复杂状态错误。

风险：

- 调试器本身可能读写进程内存；
- attach 可能接触非目标进程；
- evaluate 可执行目标语言表达式；
- 必须纳入执行级审批。

---

## 13. Eval 持久化代码执行

### 13.1 四种后端

工具创建逻辑可启用：

- Python；
- Bun/JavaScript；
- Ruby；
- Julia。

若 JavaScript 可用，工具可以先暴露，再在首次调用时检查其他后端；只有纯外部后端配置时，创建阶段会做可用性预检。

### 13.2 持久内核

与一次性命令不同：

- 变量和导入跨调用保留；
- 一个会话对应可复用 kernel；
- 支持超时、取消和重启；
- Python 使用宿主与 runner 间 NDJSON 协议；
- matplotlib/图片等输出可以成为 artifact；
- 大输出受截断和外置策略控制。

### 13.3 工具重入

预置环境暴露：

- completion/agent 等模型能力；
- 工具调用桥；
- 并行与 pipeline；
- 日志、阶段和预算辅助；
- artifact/文件等宿主能力。

因此模型可以先写一段 Python/JS，对数据循环处理，并在代码内部批量调用代理工具。

### 13.4 并发上限复用

Python、Ruby 和 JavaScript 的 <code>parallel()</code>/<code>pipeline()</code> 共同读取 <code>task.maxConcurrency</code>：

- 默认 32；
- 0 表示不设上限；
- 与 task 子代理共享同一用户心智和配置入口。

### 13.5 安全要点

Eval 是 exec 级能力。尤其要注意：

- Bash 的命令模式规则只保护 bash；
- Eval 代码可以自行创建进程或 Shell；
- 因此限制 bash 并不等于限制 eval；
- 持久内核会保存变量，敏感数据可能在后续调用继续存在。

---

## 14. 子代理、Agent Hub 与 Advisor

### 14.1 Task 工具

OMP 把子代理作为核心工具，支持：

- 单个或批量任务；
- 并发限制；
- 每项选择代理定义、模型和思考强度；
- 空白上下文启动；
- 父任务向子任务即时 steering；
- 子任务返回文本或 Schema 校验后的对象；
- 保活、停放、恢复、终止；
- 递归生成子代理；
- 独立会话记录和成本统计。

### 14.2 默认预算

| 设置 | 默认值 | 含义 |
|---|---:|---|
| task.maxConcurrency | 32 | 同时运行的子代理上限；0 为无限 |
| task.maxRecursionDepth | 2 | 子代理递归深度；负数为无限 |
| task.maxRuntimeMs | 0 | 单子代理硬运行时间；0 关闭 |
| task.agentIdleTtlMs | 420000 | 空闲 7 分钟后停放 |
| task.softRequestBudget | 200 | 软请求预算 |

软请求预算达到后先注入收尾提示；达到约 1.5 倍时强制要求提交部分结果。它控制的是“模型请求次数”，不是 token 总额。

### 14.3 结构化输出

Task 支持 <code>outputSchema</code> 和两种模式：

- permissive：保留兼容行为，可在部分失败情形接受输出并标记；
- strict：Schema 不通过就返回 <code>schema_violation</code>。

子代理可通过隐藏 <code>yield</code> 工具分段提交带标签的数据，最终组装后再验证。这样父代理不需要从自由文本中猜 JSON。

### 14.4 隔离后端

<code>task.isolation.mode</code> 可选：

- none；
- auto；
- apfs；
- btrfs；
- zfs；
- reflink；
- overlayfs；
- projfs；
- block-clone；
- rcopy。

auto 按平台优先选择写时复制/快照机制，再退回 Git worktree 或递归复制。默认仍是 <code>none</code>，即隔离能力存在但不开启。

成功任务可：

- 生成 patch 并应用；
- 建分支、提交后合并；
- 保留 patch/branch artifact 而不自动应用。

### 14.5 Agent Hub

Agent Hub 是运行时代理控制台，可：

- 查看代理树、状态、模型和用量；
- 打开子代理 transcript；
- 向运行中的代理发送消息；
- steer、revive、kill；
- 查看停放代理；
- 查看只读 advisor transcript。

它把并发子代理从“后台 Promise”提升为可管理实体。

### 14.6 Advisor/Watchdog

Advisor 是与主代理并行的第二模型审查器：

- 有自己的 Agent、ToolSession、模型角色和 transcript；
- 默认只给 read/grep/glob 与 <code>advise</code>；
- 可配置其他工具，但仍走审批；
- 读取主会话增量而不是共享同一上下文；
- 可发送 nit、concern、blocker；
- 压缩、切会话、清空时重置；
- 主代理不会因 advisor 错误长期阻塞；
- 有重复建议去重、循环保护和危险输出隔离。

它适合做持续代码审查、架构监督和安全提示。

---

## 15. Web、GitHub、浏览器与桌面控制

### 15.1 Web Search

<code>web_search</code> 支持多个搜索供应商和无凭证公共引擎聚合：

- 多引擎并行；
- 软/硬截止时间；
- 结果去重；
- 部分成功返回；
- 供应商错误分类；
- 可通过凭证系统选择商业后端。

### 15.2 Read URL/PDF

read 工具可读取 URL 和 PDF，并把内容变成模型可消费文本或图像/附件。它与 browser 的区别：

- read 偏静态资源抓取和解析；
- browser 偏交互式页面自动化；
- computer 偏整个桌面的像素和可访问性控制。

### 15.3 GitHub

OMP 同时提供：

- github 工具；
- <code>issue://</code>、<code>pr://</code> 内部资源；
- 读取 GitHub 内容像读取文件；
- GitHub 缓存和 TTL；
- 提交/PR/Issue 场景的专门表示。

“GitHub 像文件系统”减少了工具切换，但写操作仍应单独审批。

### 15.4 Browser

Browser 使用 Puppeteer Core/CDP：

- 可启动 Chromium；
- 可连接用户提供的 CDP 地址；
- 可通过浏览器 relay/Chrome 扩展接入；
- 执行脚本化导航、点击、输入和页面检查；
- 默认 <code>browser.enabled = true</code>。

### 15.5 Computer

Computer 面向整个宿主桌面：

- 截屏；
- 鼠标、键盘和像素操作；
- 可访问性树优先；
- 剪贴板；
- 等待条件；
- 多显示器合成；
- 受平台安全检查和审批包装。

它默认关闭。

### 15.6 图像与语音

- <code>inspect_image</code>：当前模型无视觉能力时，可委托 vision 角色；默认 auto。
- <code>generate_image</code>：生成/编辑图片；默认关闭。
- <code>tts</code>：本地或远端语音合成；默认关闭。
- <code>pi-voice</code>：录音、播放、Opus/WebRTC，为语音和实时模式提供原生支持。

---

## 16. MCP、扩展、规则与内部 URL

### 16.1 MCP 生命周期

OMP 的 MCP 不是启动时一次性读取：

- 发现全局和项目配置；
- 快速启动，失败时延迟回退；
- 运行中重载；
- 服务端通知；
- 断线重连；
- 退出清理；
- 部分服务失败不必拖垮全部工具；
- MCP resource 可通过 read 的 URI fallback 访问。

工具名采用 <code>mcp__&lt;server&gt;_&lt;tool&gt;</code>。

### 16.2 扩展体系

OMP 使用：

- Bun 原生 import；
- <code>pkg.omp</code> 优先，兼容 <code>pkg.pi</code>；
- capability-based discovery；
- Settings 单例；
- EventBus；
- 工具、hooks、commands、skills、rules、prompts、MCP 子树；
- 旧 Pi 管理器仅作为兼容 shim。

扩展仍是进程内代码，不是安全沙箱。

### 16.3 规则导入

规则发现可导入：

- OMP 原生规则；
- Agents/AGENTS 类文件；
- Cursor；
- Windsurf；
- Cline；
- GitHub 等格式。

流水线会做优先级、匹配、去重和分桶：

- rulebook；
- always-apply；
- TTSR。

### 16.4 TTSR

TTSR 是“时间旅行式流规则”：

1. 注册正则或 AST 条件；
2. 监视模型流式输出；
3. 规则命中时可中途终止当前输出；
4. 注入提醒；
5. 重试当前轮；
6. 按 repeat policy 控制是否再次触发。

它能在模型完成错误动作前纠偏，但规则过强会导致重试循环，因此源码有次数和生命周期控制。

### 16.5 15 个路由内建协议

<code>InternalUrlRouter</code> 注册：

| Scheme | 作用 |
|---|---|
| omp:// | OMP 内部资源 |
| agent:// | 代理和 Agent Hub 资源 |
| artifact:// | 大输出和生成物 |
| memory:// | 记忆 |
| local:// | 会话/任务共享本地资源 |
| vault:// | 受控持久资源 |
| skill:// | 技能 |
| rule:// | 规则 |
| security:// | 安全扫描资源 |
| mcp:// | MCP 资源 |
| issue:// | GitHub Issue |
| pr:// | GitHub PR |
| history:// | 会话历史 |
| ssh:// | 远程文件 |
| xd:// | 工具和动态设备 |

README 还把 <code>conflict://</code> 算入产品内部 URL 体系；它用于冲突解决流程，但不是 <code>InternalUrlRouter</code> 构造函数里的常驻 handler。因此可理解为“15 个路由原生 scheme + 1 个冲突工作流 scheme”。

### 16.6 内部 URL 的架构意义

它把大量异构能力统一成：

- read 解析资源；
- write 提交可写资源；
- autocomplete 枚举路径；
- handler 声明 immutable/read-only；
- MCP 任意 URI 作为后备解析。

这是一种“代理虚拟文件系统”。

---

## 17. 记忆、自学习与技能

### 17.1 四种后端

<code>memory.backend</code>：

| 值 | 含义 |
|---|---|
| off | 关闭，默认 |
| local | 本地 rollout 摘要与 lesson 管线 |
| hindsight | 远程 Hindsight 记忆服务 |
| mnemopi | 本地 SQLite 记忆，可选 embedding |

### 17.2 记忆工具

- recall：检索；
- retain：保存；
- reflect：综合多条记忆；
- memory_edit：直接修改 Mnemopi 记录；
- learn：把当前经验保存到记忆或技能；
- manage_skill：创建/增强受管理技能。

### 17.3 Local 模式

Local 后端会：

- 在任务或会话阶段抽取摘要；
- 生成项目作用域的 <code>memory_summary.md</code>；
- 注入有限 token 的相关记忆；
- 从完成任务中提取 lesson；
- 通过 <code>memory://</code> 暴露资源。

### 17.4 Mnemopi

Mnemopi 是独立包和 SQLite 引擎：

- 结构化记忆记录；
- recall/retain/edit；
- 可选本地 embedding；
- 进程内数据库访问；
- 与 sessionId、项目和别名关联。

### 17.5 Auto-Learn

默认关闭。启用后，主代理停止时可以被提醒：

- 捕获可复用经验；
- 写入当前记忆后端；
- 新建或增强 managed skills；
- 限定顶层代理，避免子代理白名单被悄悄扩大。

### 17.6 风险

- 旧记忆可能与当前代码不一致；
- 记忆可能包含路径、代码片段或敏感业务信息；
- 远程后端引入数据外发；
- 自动生成技能属于持久行为变化；
- 因此记忆只能作为启发，关键结论必须重新验证源码。

---

## 18. 会话、分支、压缩与恢复

### 18.1 JSONL 追加树

OMP 保留并扩展了 Pi 的会话理念：

- 一行一个 JSON 对象；
- 每条 entry 有 id、parentId 和 timestamp；
- parentId 形成可分支历史树；
- 切分支不需要复制全部历史；
- 旧历史仍可导出和审计。

主要 entry 类型：

- message；
- thinking_level_change；
- model_change；
- service_tier_change；
- compaction；
- branch_summary；
- custom/custom_message；
- label；
- title_change；
- ttsr_injection；
- session_init；
- mode_change；
- reset_boundary。

### 18.2 大文件和 Blob

- 当前文件前部有固定宽度标题槽，便于快速改标题；
- 大于约 8 MiB 时使用流式 JSONL loader；
- 大图片等二进制进入内容寻址 Blob Store；
- 列表扫描只读有限前缀和尾部，避免加载所有会话；
- 缓存按文件 stat 失效；
- 支持损坏/遗留备份的有限恢复。

### 18.3 压缩策略

默认方法顺序在源码文档中为：

1. remote；
2. snapcompact；
3. handoff；
4. shake；
5. soft。

#### Remote

使用供应商原生或 OpenAI-compatible compaction，保留可重放的供应商 payload。失败后尝试后续方法。

#### Handoff

让模型生成接力文档，总结目标、进度、文件和下一步。

#### Shake

本地机械压缩：

- 不调用模型；
- 删除已消费、可恢复或上下文价值低的工具结果；
- 大 fenced/XML 内容外置到 <code>artifact://</code>；
- 保护最近 token 窗口；
- 达不到最小节省阈值则继续下一个策略。

#### Snapcompact

把被移出的历史：

- 序列化并压缩空白；
- 用像素字体渲染到 PNG 帧；
- 针对 Claude、Gemini、GPT/Codex、Kimi/GLM 采用不同画布和计费估计；
- 将旧文本、图像中段、新文本按顺序重建为压缩消息；
- 不需要摘要模型或网络；
- 仅适用于当前视觉模型；
- archive 持久化在 compaction entry 中。

它是一种很激进但有研究依据的“让视觉模型读取历史截图”策略。

### 18.4 Checkpoint/Rewind

默认关闭，并强制成对出现：

- checkpoint：建立上下文恢复点；
- rewind：回退到恢复点；
- 子代理默认不自动获得；
- 启用一方时创建工具会补上另一方。

### 18.5 与当前 Pi 的关键差异

OMP 主线仍是成熟的 AgentSession + SessionManager + JSONL v3。当前 Pi 另外存在 durable harness/session v4、protocol、client、server、SQLite backend 的新方向。二者解决的问题重叠，但持久化抽象和嵌入式架构不同。

---

## 19. 协作、RPC、ACP 与远程运行

### 19.1 实时协作

<code>/collab</code> 可：

- 创建访客链接；
- 设置 read-write 或 view-only；
- 用 Web 客户端查看会话；
- 通过中继同步；
- 使用端到端加密；
- 自托管 relay；
- 在浏览器端展示工具状态和会话输出。

仓库中的 <code>collab-web</code> 是访客端和 relay，<code>coding-agent/src/collab</code> 是主机侧状态与协议。

### 19.2 RPC

OMP RPC 使用 JSONL：

- command/response 通过 ID 关联；
- 事件异步推送；
- 支持宿主工具；
- 支持宿主 URI；
- 支持扩展 UI 子协议；
- 支持子代理订阅；
- Python <code>omp-rpc</code> 提供客户端封装。

### 19.3 ACP

ACP 模式服务编辑器/IDE 集成：

- 协议模式独占 stdin；
- 使用专门 session factory；
- UI 行为与交互 TUI 不完全相同；
- 某些扩展 UI API 在 ACP/RPC/headless 中只是 no-op stub。

### 19.4 robomp

<code>python/robomp</code> 展示 OMP 还面向：

- 容器内运行代理；
- Web/服务端托管；
- worker 管理；
- 远程任务和界面；
- Docker Compose 开发部署。

这超出 Pi “本地终端应用”默认定位。

---

## 20. 安全边界与风险审查

### 20.1 总体判断

OMP 有审批、项目信任、秘密混淆、工具分级、provider safety check、隔离后端和输出审计，但这些能力不能被误解为统一安全沙箱。

更准确的理解是：

> OMP 是一个具备多层安全控制的高权限代理运行时；最终风险取决于启用的工具、审批模式、扩展/MCP 来源、记忆与协作配置，以及宿主系统权限。

### 20.2 风险矩阵

| 风险面 | 源码控制 | 剩余风险 | 建议 |
|---|---|---|---|
| Bash | 审批、命令规则、超时、取消、输出限制 | 命令可读写任意进程权限范围内资源 | 非可信项目用 always-ask |
| Eval | exec 审批、超时、环境过滤 | 可自行启动进程，绕过 Bash 专属规则 | 与 Bash 分开 deny/prompt |
| Edit/Write | write 分级、Hashline、LSP、冲突检测 | 用户批准后仍可大范围修改 | Git 工作区、备份、隔离子代理 |
| Task | 父任务审批、并发/深度/预算、可选隔离 | 子代理默认 yolo 执行内部动作 | 启用 isolation，限制工具白名单 |
| Browser | exec 审批、CDP 边界 | 可访问登录态和网页敏感数据 | 使用独立浏览器 Profile |
| Computer | safety checks、read_only 声明、审批 | 可控制整个桌面；read_only 是调用声明 | 默认关闭，仅临时开启 |
| MCP | 发现、生命周期、审批包装 | 第三方服务和本地命令可接触数据 | 只信任明确配置的服务器 |
| 扩展 | 项目信任、发现规则 | 进程内任意代码，非沙箱 | 审查扩展源码与依赖 |
| 记忆 | 默认关闭、后端选择、项目范围 | 持久泄密、陈旧信息、远端外发 | 敏感项目关闭或使用本地后端 |
| 协作 | E2E 加密、read/write/view 权限 | 链接泄露、屏幕/输出包含秘密 | 最小权限、短时共享、自托管 |
| 凭证 | SQLite、多凭证、退避、秘密处理 | 本机数据库和环境仍是高价值目标 | 最小 OS 权限、磁盘加密 |

### 20.3 默认 Yolo 是最重要的审查发现

<code>tools.approvalMode</code> 默认值为 <code>yolo</code>。在这一模式：

- read、write、exec 默认自动批准；
- 明确用户规则仍可 prompt 或 deny；
- 没有配置覆盖时，bash/eval/browser/task 一类操作不会因为层级而自动弹出确认。

若将 OMP 用于未知仓库，应至少：

~~~yaml
tools:
  approvalMode: always-ask
~~~

实际配置格式需按当前 Settings 文档写入，以上只表达策略意图。

### 20.4 审批不是沙箱

几个必须区分的概念：

- **Approval**：执行前是否征得同意；
- **Policy**：某类工具/参数允许、询问或拒绝；
- **Trust**：是否把项目配置和扩展视为可信；
- **Isolation**：把工作目录副本与主目录分开；
- **Sandbox**：限制进程真正可访问的系统资源。

OMP 的审批系统并不自动提供 OS 级沙箱。

### 20.5 Bash 与 Eval 的规则缺口

源码文档明确提醒：

- Bash 模式匹配只检查 bash 工具；
- Eval 可以用 Python/JS/Ruby/Julia 启动子进程；
- 如果策略只 deny bash，而允许 eval，模型仍可能获得等价执行能力。

安全策略应按能力而不是工具名字设计。

### 20.6 Computer 的 read_only

Computer 工具的只读/写入分级依赖调用参数声明。它不是对脚本做静态证明：

- 声明 read_only 不代表脚本绝对不会产生副作用；
- 任何点击、键盘、剪贴板写入都可能改变外部状态；
- 对桌面控制应整体按高权限能力处理。

### 20.7 Secret Obfuscation

秘密混淆子系统可：

- 从环境和 secrets 配置发现值；
- 用 HMAC 支持确定性可逆占位符；
- 在消息发给模型前替换秘密；
- 工具执行前恢复真实参数；
- 可选不可逆替换模式；
- advisor 增量也走秘密处理。

但它默认/配置依赖，且无法保证识别所有业务秘密。秘密防护不能替代最小化上下文。

### 20.8 子代理边界

子代理内部设置会改为 yolo，以免每一步都无法在无 UI 环境确认；真正的审批边界是父代理的 task 调用。源码仍保留用户显式 deny 规则。

这意味着：

- 用户应在 spawn 时限制工具；
- 高风险任务用隔离工作区；
- 不要把宽泛 task 批准理解为只批准一次读操作。

---

## 21. 构建、测试与发布体系

### 21.1 TypeScript

根脚本包含：

- <code>bun run check</code>：并行 TypeScript/Rust 检查；
- <code>bun run test</code>：本地分片测试；
- coding-agent singleton/UI/runtime/native/heavy 等 CI 分组；
- workspace check、lint、fmt、fix；
- 工具视图、模型目录、统计和 native binding 生成。

### 21.2 Rust

- Cargo workspace 明确列出 crate；
- correctness/suspicious 是 deny；
- clippy all/nursery/pedantic/perf/style 是 warn；
- release 使用 fat LTO、单 codegen unit、strip；
- panic 使用 unwind，边界捕获后转成 JS Promise rejection；
- 本地、CI、profiling 有不同 profile；
- Bazel 参与原生发布物构建。

### 21.3 Python

- <code>python/omp-rpc/tests</code>；
- <code>python/robomp/tests</code>；
- pytest；
- ruff lint/format；
- robomp 容器集成 smoke。

### 21.4 发布矩阵

仓库同时准备：

- 一键 Shell；
- PowerShell；
- Homebrew；
- Bun/npm；
- Nix；
- Docker；
- 多平台 native npm 包；
- 校验和与发布脚本。

这与 Pi 较轻的 npm/二进制发布相比覆盖更广，也增加供应链复杂度。

### 21.5 本次验证范围

本报告没有执行全量 <code>bun test</code>、Rust test 或真实模型集成测试，原因是：

- 任务是源码审查，不是修改后回归；
- 全量测试依赖 Bun、Rust/Bazel、各平台原生依赖和外部服务；
- 浏览器、桌面、LSP、DAP、协作和模型供应商需要环境配置。

因此所有“已实现”结论来自注册表、调用链、配置和仓库文档交叉验证；运行质量仍需第四部分的动态测试。

---

## 22. OMP 相比 Pi 的功能差异总表

### 22.1 核心能力矩阵

| 能力 | Pi 基线 | Oh My Pi | 差异性质 |
|---|---|---|---|
| 默认编码工具 | read/bash/edit/write | 29 注册工具 + 动态/隐藏工具 | 大幅新增 |
| 可选搜索工具 | grep/find/ls | 原生 grep/glob + AST + 缓存 walker | 增强/替换 |
| Agent Loop | 小型、清晰、稳定并行顺序 | 截止时间、暂停、转向、IRC、复杂恢复 | 深度增强 |
| 工具并发 | 并行执行并稳定回传 | shared/exclusive 屏障 + 中断监视 | 深度增强 |
| 工具发现 | 默认/扩展工具列表 | 注册表 + capability + xd:// 设备 | 新增架构 |
| 工具审批 | 扩展可介入 | read/write/exec + allow/prompt/deny | 增强 |
| LSP | 非内建核心 | 14 操作、Mux、写穿、诊断 ledger | 新增 |
| DAP | 无核心调试工具 | 28 操作、持久调试会话 | 新增 |
| 代码执行 | bash | Python/Bun/Ruby/Julia 持久内核 | 新增 |
| 代码重入工具 | 无 | eval 内调用工具/agent/parallel | 新增 |
| 编辑 | 精确文本 edit/write | Hashline + AST Edit + staged preview | 深度增强 |
| 并发子代理 | 非一等内建 | task、批量、递归、Hub、持久会话 | 新增 |
| 子代理隔离 | 无统一核心 | 多种 CoW/快照/worktree 后端 | 新增 |
| 结构化子任务 | 无核心 | outputSchema + strict/permissive + yield | 新增 |
| 顾问模型 | 无 | Advisor/Watchdog 异步审查 | 新增 |
| Agent Hub | 无 | 状态、用量、消息、恢复、终止 | 新增 |
| 模型角色 | 主模型/部分辅助选择 | 十个内建角色 + 自定义角色 | 新增 |
| 多凭证 | 主要单凭证 | SQLite、多凭证、轮询、亲和、退避 | 深度增强 |
| 模型供应商 | 约 40 个内建文本供应商口径 | 70 描述符、动态发现、广泛网关 | 扩大 |
| Web 搜索 | 非默认核心 | 多供应商、公共聚合、并发/截止时间 | 新增 |
| URL/PDF | 基础附件/读取能力 | read 统一资源解析 | 增强 |
| GitHub | 通过 bash/扩展 | github 工具 + issue:// + pr:// | 新增 |
| Browser | 非核心 | Puppeteer/CDP/Chrome relay | 新增 |
| Computer Use | 无 | 桌面截屏、输入、可访问性 | 新增 |
| 图像理解 | 模型附件为主 | vision 角色委托 inspect_image | 增强 |
| 图像生成 | 无核心 | generate_image | 新增 |
| TTS/Voice | 无核心 | TTS、录放音、WebRTC | 新增 |
| MCP | 可通过生态扩展 | 内建发现、重载、重连、资源路由 | 新增/增强 |
| SSH | 无核心 | ssh://、连接管理、SSHFS | 新增 |
| 规则导入 | AGENTS/扩展体系 | 多 Agent 产品格式 + TTSR | 深度增强 |
| 实时流纠偏 | 无 | 正则/AST 命中、中断、注入、重试 | 新增 |
| 内部 URL | 无如此广泛的 VFS | 15 路由 scheme + conflict 工作流 | 新增架构 |
| 记忆 | 会话历史 | local/Hindsight/Mnemopi/off | 新增 |
| 自学习技能 | 无核心 | learn/manage_skill/managed skills | 新增 |
| 会话存储 | JSONL v3 树 | 强化 JSONL v3、Blob、扫描缓存 | 增强 |
| 压缩 | 摘要/分支摘要 | remote/handoff/shake/snapcompact/soft | 深度增强 |
| Checkpoint/Rewind | 无核心工具对 | 可选成对恢复工具 | 新增 |
| 实时协作 | 无 | E2E guest sharing + relay | 新增 |
| RPC | JSONL RPC | 扩展 UI、host tool/URI、子代理订阅 | 增强 |
| ACP | 可集成但非当前产品重点 | 独立 ACP 模式 | 新增/强化 |
| 提交工作流 | 模型可通过工具完成 | <code>omp commit</code> map-reduce、校验、changelog、push | 新增 |
| 安全扫描 | 无内建核心 | security_scan + security:// | 新增 |
| 本地统计 | 基础用量/UI | stats 包和本地 dashboard | 新增 |
| 跨平台原生命令 | 系统命令/Node | Rust Shell + 进程内工具 | 架构替换 |

### 22.2 31 个产品工具如何理解

README 的工具口径可归纳为：

| 组 | 工具 |
|---|---|
| Files & Search | read、write、edit、ast_edit、ast_grep、grep、glob |
| Runtime | bash、eval |
| Code Intelligence | lsp、debug、security_scan |
| Coordination | task、hub、todo、ask |
| Desktop & Web | browser、computer、web_search、github、generate_image、inspect_image、tts |
| Memory | checkpoint、rewind、retain、recall、reflect、memory_edit、learn、manage_skill |

固定注册表、隐藏工具和动态设备采用不同统计口径，阅读源码时不能只依赖“31”这个市场化数字。

### 22.3 默认启用状态

| 功能 | 默认 |
|---|---|
| tools.approvalMode | yolo |
| tools.xdev | 开 |
| LSP | 开 |
| Debug | 开 |
| Web Search | 开 |
| Browser | 开 |
| Ask | 开 |
| Memory | off |
| Auto-Learn | 关 |
| Computer | 关 |
| Security Scan | 关 |
| Generate Image | 关 |
| TTS | 关 |
| Checkpoint/Rewind | 关 |
| Task Isolation | none |

“源码里有”与“默认暴露”不是同一件事。

---

## 23. OMP 不是 Pi 的严格超集

### 23.1 Pi 当前多出的包方向

用户提供的 Pi 源码包含：

- <code>client</code>；
- <code>protocol</code>；
- <code>server</code>；
- <code>telemetry</code>；
- <code>evals</code>；
- <code>session-backends/sqlite-node</code>；
- Agent 包中的 experimental harness/session。

OMP 的工作区没有按相同边界保留这些包，而是：

- 用 <code>wire</code>、自有 RPC/ACP、coding-agent session 和 Python SDK 解决协议问题；
- 把 OpenTelemetry 直接融入 agent/coding-agent；
- 用 metaharness 和多个 benchmark 包处理评估；
- 继续使用加强版 JSONL 会话，并把 SQLite 用于认证、记忆和其他本地状态。

### 23.2 Durable Harness 差异

当前 Pi 的新方向更适合：

- 作为库嵌入其他服务；
- 明确 client/server 协议；
- durable execution；
- Session v4 与 SQLite backend；
- 可测试的独立 harness。

OMP 更适合：

- 完整终端产品；
- 高密度工具；
- 本地原生能力；
- 多代理工作流；
- 丰富 UI 和用户配置。

### 23.3 OMP 维护成本

OMP 的主要工程风险：

1. 长期分叉导致上游安全修复需要语义移植；
2. 大型 ToolSession 和 AgentSession 容易形成高耦合；
3. Agent Loop 的中断/恢复状态空间很大；
4. 多运行时、多平台 native 发布矩阵复杂；
5. 默认开放 browser/web/debug 和 yolo 审批，部署前必须安全加固；
6. 功能开关组合多，测试矩阵呈乘法增长；
7. 内部 URL 把许多能力汇入 read/write，权限审查要看 URI 语义而非只看工具名。

### 23.4 选型建议

| 需求 | 更合适 |
|---|---|
| 学习最小 Agent Loop | Pi |
| 构建可嵌入 durable agent service | 当前 Pi 的 harness/protocol 方向 |
| 终端里使用大量开箱工具 | OMP |
| Windows 原生一致工具链 | OMP |
| 多代理并发、隔离、Hub 管理 | OMP |
| LSP + DAP + 浏览器 + 桌面一体化 | OMP |
| 尽量小的可信计算基 | Pi |
| 接受复杂配置并追求功能上限 | OMP |

---

## 24. 源码阅读路线

### 24.1 第一阶段：看懂启动与会话

1. <code>packages/coding-agent/src/cli.ts</code>
2. <code>packages/coding-agent/src/main.ts</code>
3. <code>packages/coding-agent/src/sdk.ts</code>
4. <code>packages/coding-agent/src/session/agent-session.ts</code>
5. <code>packages/coding-agent/src/session/session-manager.ts</code>

问题目标：一次 <code>omp</code> 启动如何得到 AgentSession。

### 24.2 第二阶段：看懂 Agent Loop

1. <code>packages/agent/src/types.ts</code>
2. <code>packages/agent/src/agent.ts</code>
3. <code>packages/agent/src/agent-loop.ts</code>
4. <code>packages/agent/src/pause.ts</code>
5. <code>packages/agent/src/replay-policy.ts</code>
6. <code>packages/agent/src/telemetry.ts</code>

问题目标：模型流、工具批次、转向、中止和合成结果如何闭环。

### 24.3 第三阶段：看懂工具装配

1. <code>packages/coding-agent/src/tools/builtin-names.ts</code>
2. <code>packages/coding-agent/src/tools/index.ts</code>
3. <code>packages/coding-agent/src/tools/essential-tools.ts</code>
4. <code>packages/coding-agent/src/session/session-tools.ts</code>
5. <code>packages/coding-agent/src/tools/xdev.ts</code>
6. <code>packages/coding-agent/src/extensibility</code>

问题目标：一个工具为什么出现、以何种 Schema 出现、怎样审批。

### 24.4 第四阶段：按专业子系统阅读

| 主题 | 入口 |
|---|---|
| LSP | <code>packages/coding-agent/src/lsp</code> |
| DAP | <code>packages/coding-agent/src/dap</code>、<code>tools/debug.ts</code> |
| Eval | <code>packages/coding-agent/src/eval</code> |
| 子代理 | <code>packages/coding-agent/src/task</code> |
| Agent Hub | <code>packages/coding-agent/src/tools/hub</code>、<code>agent-registry</code> |
| Advisor | <code>packages/coding-agent/src/advisor</code> |
| 记忆 | <code>memories</code>、<code>memory-backend</code>、<code>mnemopi</code>、<code>hindsight</code> |
| Browser/Web | <code>browser</code>、<code>web</code>、<code>tools/browser.ts</code> |
| MCP | <code>packages/coding-agent/src/mcp</code> |
| 规则/TTSR | <code>rules</code>、<code>rulebook</code>、相关 docs |
| 内部 URL | <code>packages/coding-agent/src/internal-urls</code> |
| 会话压缩 | <code>packages/agent/src/compaction</code> |
| Hashline | <code>packages/hashline</code> |
| Rust Native | <code>packages/natives</code>、<code>crates/pi-*</code> |
| 协作 | <code>coding-agent/src/collab</code>、<code>packages/collab-web</code> |

### 24.5 第五阶段：阅读安全边界

1. <code>docs/approval-mode.md</code>
2. <code>docs/secrets.md</code>
3. <code>packages/coding-agent/src/extensibility/extensions/wrapper.ts</code>
4. <code>packages/coding-agent/src/tools/bash.ts</code>
5. <code>packages/coding-agent/src/tools/computer.ts</code>
6. <code>packages/coding-agent/src/task/executor.ts</code>

阅读时不要只问“有检查吗”，还要问“检查的是哪个入口，是否存在等价能力绕行”。

---

# 第二部分：白话版源码讲解

## 1. 把 Pi 想成一辆轻量越野车

Pi 的核心结构很容易理解：

- 模型是驾驶员；
- Agent Loop 是发动机控制器；
- read/bash/edit/write 是基本工具；
- JSONL 是行车记录仪；
- TUI 是驾驶舱；
- 扩展接口允许自己加设备。

它的优点是结构清楚，想改什么通常能快速找到。

## 2. OMP 把这辆车改成了移动指挥中心

OMP 仍能完成 Pi 的基本工作，但又加上：

- 代码导航系统（LSP）；
- 真调试器（DAP）；
- 多个持久计算舱（Python/JS/Ruby/Julia）；
- 一组可以并行工作的分队（子代理）；
- 分队控制台（Agent Hub）；
- 独立监督员（Advisor）；
- 浏览器和整台电脑的控制能力；
- 长期记忆；
- 远程访客和实时协作；
- 自己的跨平台 Shell 和系统工具；
- 历史压缩与恢复系统。

因此它更强，也更像完整产品，而不再只是一个小型编码代理框架。

## 3. 一次任务是怎样工作的

假设用户说：“修复登录失败并验证。”

1. CLI 读取设置、凭证、项目规则和可用模型。
2. AgentSession 建立会话，创建 Agent Loop 和工具。
3. 主模型读取代码。
4. 它可以用 LSP 查定义和引用。
5. 它可以启动多个子代理：一个找根因、一个写测试、一个审安全。
6. 子代理可进入独立 worktree，避免同时改坏主目录。
7. 主模型用 Hashline 或 AST Edit 修改。
8. 写入后 LSP 自动收到变更并返回诊断。
9. 需要时用 DAP 真正启动程序、下断点看变量。
10. Advisor 在旁边观察，有严重问题就发 blocker。
11. 历史太长时，shake/snapcompact/remote compaction 压缩旧内容。
12. 最终结果进入 JSONL，会话和子代理 transcript 可恢复。

## 4. 为什么要做 Hashline

普通文本编辑假设：“我刚才读到的第 100 行现在还是第 100 行。”

并发代理环境里这个假设经常不成立。Hashline 相当于在文件上盖章：

> “我这个补丁是基于内容版本 ABCD 写的。”

如果当前文件已经不是那个版本，就不盲目套补丁，而是拒绝或做三方恢复。它减少了最危险的静默错位。

## 5. 为什么 LSP 和 DAP 都需要

- LSP 回答“源码看起来是什么”：定义、引用、类型、诊断。
- DAP 回答“程序运行时实际是什么”：栈、变量、线程、内存。

只靠 LSP，动态状态错误看不到；只靠 DAP，重构和符号导航很笨。OMP 把两者都交给模型。

## 6. 为什么 Eval 比 Bash 更强

Bash 每次更像“执行一条命令”；Eval 更像打开一台长期存在的 Jupyter 风格计算器：

- 上次定义的变量还在；
- 可以循环和并行；
- 可以调用 OMP 工具；
- 可以直接处理结构化对象；
- 适合数据分析、批量检查和复杂编排。

它也因此比 Bash 更难限制。

## 7. 子代理为什么不是简单多开几个模型

OMP 需要解决：

- 同时最多跑几个；
- 子代理还能不能再生子代理；
- 谁给谁发消息；
- 怎么暂停、恢复、终止；
- 子代理输出是不是合法 JSON；
- 多个代理改同一文件怎么办；
- 子代理退出后会话是否还能恢复；
- 成本和状态怎样显示。

这些问题由 task executor、Agent Registry、Hub、隔离后端和结构化 yield 共同解决。

## 8. Snapcompact 是什么

普通压缩会让模型“写摘要”。Snapcompact 走另一条路：

1. 把旧对话排版成密集图片；
2. 把图片放回上下文；
3. 让视觉模型自己读取。

好处是不需要另一个摘要模型，也可能保留更多原文细节；限制是只有视觉模型能用，而且效果依赖字体、画布、供应商图片计费和视觉识别能力。

## 9. OMP 最需要谨慎的地方

不是某个 bug，而是权限组合：

- 默认 yolo；
- browser 默认开；
- web search 默认开；
- debug 默认开；
- eval 能启动进程；
- 扩展是进程内代码；
- MCP 可以连接外部服务；
- memory 可以长期保留信息；
- computer 一旦开启能操作整个桌面。

个人可信仓库里，这些是生产力；未知仓库或企业敏感环境里，必须先做策略收缩。

## 10. Pi 和 OMP 应该怎么选

想学习代理核心、做自己的轻量框架，先读 Pi。  
想直接获得工具密度、并发子代理、LSP/DAP、浏览器、跨平台 Native 和协作，OMP 更合适。  
想做服务端可嵌入、协议清晰、durable harness 的系统，还要重点看当前 Pi 新的 protocol/client/server/session v4 方向。

---

# 第三部分：专业名词中英对照

| 英文 | 建议中文译法 | 在本项目中的含义 |
|---|---|---|
| Agent | 智能代理/代理 | 能调用模型与工具的执行主体 |
| Agent Loop | 代理循环 | 模型—工具—结果—再调用的主循环 |
| Agent Session | 代理会话 | 会话状态、模型、工具、历史和生命周期 |
| Agent Harness | 代理运行框架 | 面向嵌入、协议和持久执行的运行容器 |
| Subagent | 子代理 | 由父代理派生的独立执行代理 |
| Agent Hub | 代理中心 | 查看和控制代理树的界面/工具 |
| Advisor | 顾问代理 | 异步观察并给主代理建议的第二模型 |
| Watchdog | 监督器 | Advisor 配置和持续审查机制 |
| Steering | 运行中转向 | 向正在运行的代理注入新指令 |
| Aside | 旁路消息 | 在安全边界注入、不一定来自用户的附加消息 |
| IRC | 代理间即时通信 | 父子/同级代理的消息与中断通道 |
| Tool Call | 工具调用 | 模型输出的结构化工具请求 |
| Tool Result | 工具结果 | 工具执行后返回给模型的消息 |
| Synthetic Tool Result | 合成工具结果 | 为未执行调用补齐协议配对的结果 |
| Tool Choice | 工具选择约束 | 允许、禁止或强制模型调用某工具 |
| Soft Tool Requirement | 软工具要求 | 先提醒，必要时再升级强制的工具要求 |
| Shared Concurrency | 共享并发 | 可与同阶段其他共享工具并行 |
| Exclusive Concurrency | 独占并发 | 与前后任务形成串行屏障 |
| Deadline | 截止时间 | 绝对时间点，到达即中止运行 |
| AbortSignal | 中止信号 | 跨异步调用传递取消状态 |
| Pause Gate | 暂停门 | 让新模型轮次/工具停在边界等待恢复 |
| Replay Policy | 重放策略 | 决定错误或恢复时哪些历史可再次使用 |
| Compaction | 上下文压缩 | 缩短历史以适应模型上下文窗口 |
| Handoff | 接力摘要 | 为下一段上下文生成交接文档 |
| Shake | 机械删减 | 不调用模型，移除低价值可恢复内容 |
| Snapcompact | 位图上下文压缩 | 将历史渲染成图片供视觉模型读取 |
| Context Window | 上下文窗口 | 模型单次请求可接受的 token/图像容量 |
| LSP | 语言服务器协议 | 定义、引用、诊断、重命名等代码智能 |
| Writethrough | 写穿同步 | 文件写入同时同步到 LSP 状态 |
| Diagnostics Ledger | 诊断账本 | 聚合、去重和延迟投递诊断 |
| DAP | 调试适配器协议 | 启动、断点、单步、变量、栈等调试协议 |
| Persistent Kernel | 持久化内核 | 多次 eval 间保留变量和运行状态 |
| Tool Re-entry | 工具重入 | eval 代码内部重新调用代理工具 |
| Hashline | 哈希行补丁 | 绑定文件内容哈希的行/语法块编辑格式 |
| Stale Anchor | 过期锚点 | 基于旧文件位置生成、已不可靠的编辑定位 |
| Three-way Merge | 三方合并 | 依据基线、当前、目标恢复变更 |
| AST | 抽象语法树 | 代码语法结构表示 |
| AST Grep | 语法树搜索 | 按代码结构匹配 |
| AST Edit | 语法树编辑 | 按结构重写代码 |
| Worktree | Git 工作树 | 同一仓库的独立检出目录 |
| Copy-on-write | 写时复制 | 只有修改时才复制数据的快照机制 |
| Isolation | 隔离 | 子任务在独立目录/快照中执行 |
| Schema | 模式/结构约束 | 对工具参数或输出结构的机器校验 |
| Schema Violation | 模式违规 | 输出不满足所声明结构 |
| Yield | 提交/交付 | 子代理向父代理提交结构化阶段结果 |
| Capability Discovery | 能力发现 | 按供应者加载工具、规则、技能等能力 |
| MCP | 模型上下文协议 | 连接外部工具与资源服务器的协议 |
| Internal URL | 内部资源地址 | 用 URI 统一访问工具、记忆、代理等资源 |
| Virtual Filesystem | 虚拟文件系统 | 把异构资源统一成类似读写文件的界面 |
| ACP | 代理客户端协议 | 编辑器/宿主与代理集成协议 |
| RPC | 远程过程调用 | 外部程序通过 JSONL 控制会话 |
| CDP | Chrome DevTools 协议 | 浏览器自动化和调试接口 |
| Computer Use | 计算机操作 | 截屏、鼠标、键盘、可访问性控制 |
| Credential Affinity | 凭证亲和 | 同一会话尽量复用同一凭证 |
| Backoff | 退避 | 失败后延迟再次使用凭证或服务 |
| Secret Obfuscation | 秘密混淆 | 发给模型前用占位符替换敏感值 |
| Approval Tier | 审批等级 | read/write/exec 权限分类 |
| Yolo Mode | 全自动批准模式 | 自动批准全部工具等级 |
| End-to-end Encryption | 端到端加密 | 中继无法直接读取协作明文 |
| Blob Store | 大对象存储 | 把图片等大内容从 JSONL 外置 |
| Content-addressed | 内容寻址 | 以内容哈希作为对象标识 |
| Telemetry | 遥测 | 追踪、指标、日志与工具运行观测 |
| OpenTelemetry | 开放遥测标准 | OMP 导出 traces/metrics/logs 的标准 |

---

# 第四部分：建议的动态验证清单

## 1. 最小启动验证

1. 执行 <code>omp --version</code>、<code>omp --help</code> 和 smoke test。
2. 新建空目录启动交互模式。
3. 验证 Settings、AuthStorage、ModelRegistry 和空会话创建。
4. 分别验证 print、RPC、ACP 模式不会误读 stdin。

## 2. 工具注册验证

1. 记录默认顶层工具和 <code>xd://</code> 设备目录。
2. 逐个切换 LSP、Debug、Browser、Computer、Memory、Security、Checkpoint。
3. 验证受限工具白名单不会被自动扩大。
4. 验证 checkpoint/rewind 始终成对。
5. 验证没有 write 的会话不会因 xd:// 获得写能力。

## 3. Agent Loop 验证

1. 同轮生成多个 read，确认并行且结果顺序稳定。
2. 混合 shared 和 exclusive 工具，验证屏障。
3. 运行长 bash 时发送 steering，确认自动后台化或边界注入。
4. 让纯 wait 工具处于等待，确认可立即中断。
5. 模拟 provider 在 tool call 后断流，检查合成结果。
6. 模拟 stop_reason=length 的截断 write，确认不会执行。
7. 测试全局 pause/resume 不会重跑已完成工具。

## 4. 编辑链验证

1. read 后由外部进程修改文件，再提交 Hashline 补丁。
2. 验证过期哈希拒绝或三方恢复。
3. 多文件补丁故意让一项失败，确认预检避免半落盘。
4. AST Edit 暂存后分别 resolve/reject。
5. 写入后检查 LSP didChange、格式化和诊断注入。

## 5. LSP/DAP 验证

1. TypeScript、Python、Rust 项目各启动一个 LSP。
2. 测试 definition/references/rename_file/code_actions。
3. 多语言 workspace diagnostics 并发。
4. DAP 启动、断点、单步、变量、栈和终止。
5. attach/evaluate/read_memory/write_memory 验证审批等级。

## 6. Eval 验证

1. 四种后端分别探测。
2. 跨两次调用验证变量持久化。
3. 在 eval 内调用 read/grep/task。
4. <code>parallel()</code> 在 task.maxConcurrency=1、2、0 下测试。
5. 触发超时、取消、内核重启和大输出 artifact。
6. 验证 Bash deny、Eval allow 时确实存在进程执行能力，确认策略配置。

## 7. 子代理验证

1. 批量启动超过 maxConcurrency 的任务。
2. 达到 maxRecursionDepth 后确认 task 工具消失或拒绝。
3. 测试软请求预算提醒和 1.5 倍强制交付。
4. strict/permissive Schema 失败。
5. Agent Hub steer/revive/kill。
6. 空闲 TTL 后停放，再消息唤醒。
7. 每种本机可用隔离后端运行修改并应用 patch/branch。
8. 两个隔离代理制造同文件冲突，检查 conflict 流程。

## 8. 记忆与压缩验证

1. 四个 memory backend 切换，确认工具集合随之变化。
2. Local 记忆生成、检索和项目隔离。
3. Mnemopi SQLite recall/retain/edit。
4. 压缩分别强制 remote/handoff/shake/snapcompact/soft。
5. 无视觉模型时 snapcompact 必须回退。
6. 压缩后 advisor、TTSR、文件操作清单和分支历史保持一致。

## 9. Browser/Computer/Collab 安全验证

1. 使用独立测试浏览器 Profile。
2. 验证 browser CDP 连接和 relay。
3. Computer 只读动作、写动作和审批。
4. Collab view-only 无法提交输入或执行工具。
5. read-write guest 的所有动作可审计。
6. 中继抓包确认只有密文。
7. 链接撤销、主机退出和异常断线后会话清理。

## 10. 发布验证

1. Windows、macOS、Linux 安装。
2. Native addon ABI 和缺失二进制回退。
3. Nix/Homebrew/Bun/PowerShell 安装路径。
4. 二进制校验和。
5. Rust panic 能转成 JS 错误而非整个进程崩溃。

---

# 结论

Oh My Pi 相比 Pi 多出的核心，不应概括成“多了 27 个工具”。更准确的结论是：

1. 它把轻量编码代理扩展成多模型角色的代理工作台；
2. 把 LSP、DAP、持久 Eval、子代理和浏览器/桌面控制纳入核心；
3. 用 Rust 建立跨平台执行、搜索、AST 和隔离底座；
4. 用 Hashline、LSP 写穿和隔离子代理提高并发编辑可靠性；
5. 用 Advisor、Agent Hub、实时协作和 RPC/ACP 扩大人机与代理间协作；
6. 用多种记忆与压缩策略延长长期任务能力；
7. 同时显著扩大了权限面、状态空间、构建矩阵和维护成本。

如果把 Pi 看作“可读、可改、可嵌入的代理内核”，OMP 更像“围绕这个内核理念建成的完整代理操作环境”。它在产品功能上明显更强，但并未以同样形式继承当前 Pi 的 protocol/client/server/durable harness 新路线，因此两者已经不是简单的基础版和增强版关系，而是同源后沿不同目标持续演化的两个工程。

---

## 主要源码依据

| 主题 | 关键文件/目录 |
|---|---|
| 启动 | <code>packages/coding-agent/src/cli.ts</code>、<code>main.ts</code>、<code>sdk.ts</code> |
| Agent Loop | <code>packages/agent/src/agent-loop.ts</code>、<code>types.ts</code> |
| 工具注册 | <code>packages/coding-agent/src/tools/builtin-names.ts</code>、<code>tools/index.ts</code> |
| 设置默认值 | <code>packages/coding-agent/src/config/settings-schema.ts</code> |
| 子代理 | <code>packages/coding-agent/src/task</code> |
| LSP | <code>packages/coding-agent/src/lsp</code> |
| DAP | <code>packages/coding-agent/src/dap</code>、<code>tools/debug.ts</code> |
| Eval | <code>packages/coding-agent/src/eval</code> |
| Advisor | <code>packages/coding-agent/src/advisor</code> |
| 记忆 | <code>memories</code>、<code>memory-backend</code>、<code>packages/mnemopi</code> |
| 压缩 | <code>packages/agent/src/compaction</code>、<code>packages/snapcompact</code> |
| Hashline | <code>packages/hashline</code> |
| 内部 URL | <code>packages/coding-agent/src/internal-urls</code> |
| Rust 原生层 | <code>packages/natives</code>、<code>crates</code> |
| 协作 | <code>packages/coding-agent/src/collab</code>、<code>packages/collab-web</code> |
| 分叉策略 | <code>docs/porting-from-pi-mono.md</code> |

> 静态审查结论绑定本文开头列出的两个提交。后续主分支更新、默认配置变化或上游再次同步后，应重新核对注册表、Settings Schema 和端到端行为。
