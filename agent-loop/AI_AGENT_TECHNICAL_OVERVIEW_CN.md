# PI Agent Loop 技术全景与生产级差距说明

## 总结结论

这个项目已经不只是“调用大模型的示例”，而是一套相对完整的 **AI Agent Runtime / Harness 教学与工程框架**。

它覆盖了：

- 模型循环与流式输出
- OpenAI-compatible Provider
- Tool Calling 与并发调度
- 重试、熔断、预算和取消
- Session 持久化与崩溃恢复
- 审批和高风险写操作
- 意图路由、任务规划和多 Agent
- 长期记忆与上下文压缩
- 安全策略、身份与租户隔离
- 遥测、评测、回归门禁和供应链检查

它的核心思想不是“让模型拥有更大权限”，而是：

> 模型只负责提出建议；可信代码负责授权、执行、持久化和验证。

## 一、总体架构

```text
用户 / API / CLI
       │
       ▼
DurableAgentHost
Session、恢复、Lease、审批、持久化
       │
       ▼
Agent
状态、Transcript、事件、steer/follow-up
       │
       ▼
Agent Loop
模型 → Tool Call → Tool Result → 再调用模型
       │
       ├───────────────┐
       ▼               ▼
ModelCallRuntime    ToolDispatchRuntime
重试/压缩/预算       校验/授权/锁/超时
       │               │
       ▼               ▼
OpenAI Provider     本地或业务工具
HTTP + SSE          文件、计算、订单等
       │               │
       └──────┬────────┘
              ▼
    加密 SQLite Event Journal
```

规划、路由、多 Agent 位于这套基础运行时之上；安全、遥测和评测横跨所有层。

核心入口可以从以下文件阅读：

- [`loop.py`](src/pi_agent_loop/loop.py)
- [`agent.py`](src/pi_agent_loop/agent.py)
- [`durable_agent_host.py`](src/pi_agent_loop/harness/durable_agent_host.py)
- [`README.md`](README.md)

## 二、主要技术栈

| 技术 | 在项目中的作用 |
|---|---|
| Python 3.11+ | 主语言，使用类型标注、Protocol、dataclass、asyncio |
| asyncio | 模型流式请求、工具并发、取消和超时 |
| httpx | OpenAI-compatible HTTP 客户端和连接池 |
| SSE | 接收模型的文本、thinking、Tool Call 增量 |
| SQLite | Session、Operation、Approval、Write、Plan、Memory 持久化 |
| AES-GCM | Journal 和长期记忆的静态加密 |
| HMAC-SHA256 | 完整性清单和评测证据签名 |
| TOML | Agent、Provider、业务 Intent/Capability 配置 |
| JSON Schema | Tool 参数结构描述 |
| Event Sourcing | 通过事件重建 Session 和 Operation 状态 |
| CAS / Lease / Fencing | 防并发覆盖、双写和过期 Worker 提交 |
| pytest | 单元、集成、恢复和安全测试 |
| Ruff / mypy | 代码风格和静态类型检查 |
| pip-audit / CycloneDX | 漏洞扫描和 SBOM |
| GitHub Actions | Linux、Windows、多 Python 版本质量门禁 |

运行依赖控制得比较克制，主要外部依赖只有 `httpx` 和 `cryptography`，其余大量能力基于标准库实现，详见 [`pyproject.toml`](pyproject.toml)。

## 三、Agent Loop 是怎样工作的

低层循环负责以下闭环：

```text
用户消息
  → 请求模型
  → 接收 Assistant Message
  → 是否包含 Tool Call？
      ├─ 是：校验并执行工具
      │      → 添加 Tool Result
      │      → 再次请求模型
      └─ 否：结束本次运行
```

它支持两种运行中消息：

- `steer()`：当前一批工具结束后、下一次模型请求前插入。
- `follow_up()`：Agent 原本准备结束时，再追加一个任务。

循环还维护三类基本预算：

- `max_turns`：最多请求模型多少轮。
- `max_tool_calls`：最多执行多少个逻辑工具调用及重试尝试。
- `max_parallel_tools`：工具最大并发数。

需要注意：裸 `Agent` 的预算不等于严格的 Token、费用和物理 HTTP Attempt 上限。这类硬预算由外层 `ModelCallRuntime` 和 Autonomous Plan 层负责。

## 四、Provider 和流式协议

Provider 的内部抽象很小：

```python
stream_fn(model, context, options) -> AssistantMessageEventStream
```

当前真实 Provider 使用 OpenAI-compatible Chat Completions：

1. 把内部消息转换为 OpenAI 消息。
2. POST `/chat/completions`。
3. 解析 SSE 字节流。
4. 把文本、thinking 和 Tool Call 增量转换成内部事件。
5. 聚合成最终 Assistant Message。

关键实现：

- [`openai_compatible.py`](src/pi_agent_loop/providers/openai_compatible.py)
- [`sse.py`](src/pi_agent_loop/providers/sse.py)
- [`serialize.py`](src/pi_agent_loop/providers/serialize.py)
- [`translate.py`](src/pi_agent_loop/providers/translate.py)

SSE 解析设置了多层边界：

- 严格 UTF-8
- 单行大小
- 单事件大小
- 总响应大小
- 最大事件数量
- Tool Arguments 大小
- 必须收到 `[DONE]`

HTTP 429、408、409 和部分 5xx 可以重试；认证失败和不存在的模型等永久错误不会盲目重试。

目前主要支持 Chat Completions 流式协议，不是完整的 Responses API 实现。

## 五、EventStream 与背压

[`EventStream`](src/pi_agent_loop/event_stream.py) 同时支持：

- 异步遍历实时事件
- 独立等待最终 `result()`
- 事件数量上限
- 保留字节数上限
- 高频 delta 合并
- 终止事件保护
- 背压错误

文本增量可以适度合并，但以下事件不能静默丢失：

- 生命周期开始/结束
- Tool 提交和完成
- 模型终态
- 错误和取消终态

这防止一个读取过慢的 UI 将整个 Agent 内存无限撑大。

## 六、Tool Calling 和调度

Tool 定义包含：

- 名称和描述
- JSON Schema
- 参数校验与规范化
- `prepare`
- `execute`
- 超时
- 重试策略
- 调度策略
- 副作用和回放策略
- 授权与审批要求

正式执行边界是 [`ToolDispatchRuntime`](src/pi_agent_loop/tool_runtime.py)。

工具批次分两阶段处理：

1. 整批参数、工具名、白名单和 Schema 预检。
2. 整批通过后才进入授权、锁、Hook 和执行。

这避免一批工具中前几个已产生副作用，最后一个才发现参数非法。

支持三种主要调度方式：

- `parallel`：可并行执行。
- `exclusive`：全局排他执行。
- `resource_locked`：按文件、订单等资源键加读写锁。

工具完成事件按真实完成顺序发布，但 Tool Result 按原始 Tool Call 顺序回灌模型，保证上下文确定性。

Transcript 还强制执行 Tool Call 闭合：

> 每一个 Assistant Tool Call，在下一条 User/Assistant 消息前，必须恰好对应一个 Tool Result。

即使取消、超时、预算耗尽，也会产生结构化的 Synthetic Tool Result。

## 七、重试、熔断、取消和上下文压缩

模型重试包含：

- 错误分类
- 指数退避
- Jitter
- `Retry-After`
- 最大尝试次数
- 总时间上限
- Circuit Breaker

一个逻辑模型 Turn 可以包含多个物理 HTTP Attempt。失败 Attempt 的部分输出不会直接污染正式 Transcript，只有最终选定的 Attempt 才会公开。

工具只在明确声明幂等时自动重试。高风险写工具默认不自动重放。

[`TokenAwareStructuredCompactor`](src/pi_agent_loop/retry/compaction.py) 在上下文超限时：

- 保留最近消息
- 保留 Tool Call/Result 配对
- 保留审批、约束和业务事实
- 把旧内容转换为结构化摘要
- 验证压缩后 Token 确实下降
- 再次验证 Transcript 闭合

取消采用父子 `CancellationToken`。单个工具超时只取消自己的子 Token，不影响并行兄弟工具。但同步阻塞代码无法被 asyncio 真正强杀，这是当前硬超时能力的重要边界。

## 八、持久化与崩溃恢复

持久化结构大致是：

```text
Project
  └─ Session
      ├─ Operation
      │   ├─ model_request_started
      │   ├─ model_request_finished
      │   ├─ tool_dispatch_started
      │   ├─ tool_dispatch_finished
      │   └─ operation_finished
      ├─ Approval
      ├─ Write Operation
      └─ Durable Plan
```

核心是 [`SQLiteSessionEventJournal`](src/pi_agent_loop/session/journal.py)。

它采用 Event Sourcing：

```text
当前状态 = reducer(全部历史事件)
```

重要技术包括：

- AES-GCM 加密事件正文
- AAD 绑定元数据
- SHA-256 校验
- HMAC 完整性清单
- Snapshot
- Schema Migration
- Tenant 隔离
- Retention、Export、Delete
- Writer Lease
- CAS 版本检查

这里的几个核心概念：

- **Commit Boundary**：某项结果必须先可靠持久化，才能对外宣称成功。
- **CAS**：只有当前版本仍等于预期版本时才允许更新。
- **Lease**：某个 Worker 在限定时间内拥有执行权。
- **Fencing Token**：单调递增代际，防止已过期 Worker 恢复后继续提交。
- **Idempotency**：同一个业务请求重复提交时不能重复产生副作用。
- **outcome_unknown**：外部操作可能已经执行，但本地没有可靠记录成功或失败。

`outcome_unknown` 不能自动当作失败重试。例如付款请求发出后进程崩溃，再次付款可能导致重复扣款，正确做法是查询外部系统并对账。

恢复入口见 [`startup_recovery.py`](src/pi_agent_loop/harness/startup_recovery.py)。

## 九、审批与高风险写操作

审批由 [`ApprovalService`](src/pi_agent_loop/approval/state_machine.py) 管理，关键约束包括：

- 申请人身份
- 审批人身份和角色
- 精确 Action Hash
- 有效期
- 一次性消费
- 审批内容不能被执行阶段替换

写操作由 [`WriteOperationService`](src/pi_agent_loop/writes/state_machine.py) 管理，典型状态为：

```text
prepared
→ waiting_approval
→ approved
→ submitting
→ succeeded
```

如果 `submitting` 后失去确定结果，则进入：

```text
outcome_unknown
```

这套设计比“模型调用一个写 API”可靠得多，因为它区分了：

- 模型建议做什么
- 谁授权做
- 执行的具体参数
- 是否真正提交
- 是否可靠确认成功

## 十、安全设计

安全边界的首要原则是：

> 模型输出、Router 输出、工具返回内容，都不能天然视为可信指令。

[`ContentSafetyPipeline`](src/pi_agent_loop/safety.py) 可以审核：

- 模型输入
- 模型最终输出
- 工具输出
- 不可信工具结果中的提示注入

其它安全能力包括：

- Verified Identity
- Tenant 隔离
- Tool 白名单
- Tool 安全合同密封
- 参数重新校验
- Secret Redaction
- 工作区路径边界
- 符号链接和 Windows Reparse Point 防护
- Windows ADS 防护
- Shell 输出截断和进程树取消
- Approval Action Hash

但 AES-GCM 只保护磁盘数据，不能保护网络传输。运行时看到的：

```text
安全警告：当前使用远程明文 HTTP
```

表示 Bearer Token、Prompt 和模型结果在传输中没有 TLS 保护。生产环境必须使用 HTTPS。

此外，取消令牌也不等于 Sandbox。运行不可信 Python、Shell 或第三方代码，仍需要操作系统、容器或虚拟机级隔离。

## 十一、意图路由、规划与多 Agent

### 1. 业务 Intent 路由

[`HybridModelRouter`](src/pi_agent_loop/routing/hybrid_router.py) 使用模型进行意图分类，但可信配置决定：

- Intent
- Capability
- 允许使用哪些工具
- 风险等级
- 是否需要审批
- Tool Choice 是 `none`、`auto`、`required` 还是指定工具

Router 的结果只是分类结果，不是授权凭证。

### 2. 多模型路由

系统还支持按以下因素选择 Provider/模型：

- 模型能力
- 上下文窗口
- 成本
- 延迟
- 健康状态
- 熔断状态

只有在尚未公开任何非空输出时才能切换备用模型，防止把两个模型的半截回复拼在一起。

### 3. Durable Plan

[`HybridRequestPlanner`](src/pi_agent_loop/planning/planner.py) 把路由结果转换成可信计划。

[`PlanExecutor`](src/pi_agent_loop/planning/executor.py) 支持：

- DAG 依赖
- 条件执行
- 参数引用
- 结果合同
- 审批屏障
- Claim、Lease 和 Fencing
- 失败传播
- 重规划

### 4. 多 Agent

[`MultiAgentOrchestrator`](src/pi_agent_loop/multi_agent.py) 提供：

- Worker Registry
- 角色和能力约束
- DAG 调度
- 全局和每 Worker 并发限制
- 依赖失败后跳过下游
- Deadline 和取消传播
- 结果仲裁
- Tenant/Run 隔离

但它目前是单进程、有界的 Worker 编排器，不是完整的跨机器分布式 Agent 集群。

## 十二、记忆系统

长期记忆采用显式、可控的设计：

- Tenant / Project / Session / User Scope
- 来源与 Provenance
- TTL
- AES-GCM 加密 SQLite
- Embedding 协议
- 离线 Hash Embedding 退化实现

Memory 和 Clarification 不应混为一谈：

- Memory：长期可检索事实。
- Clarification：当前任务缺少参数，需要临时追问。

核心文件：

- [`manager.py`](src/pi_agent_loop/memory/manager.py)
- [`store.py`](src/pi_agent_loop/memory/store.py)

## 十三、遥测、评测与 CI

遥测包含：

- Metrics
- Trace
- Structured Log
- Alert
- 模型/工具延迟
- Token 和成本
- 重试、熔断和错误码
- Session、Operation、Plan 状态

评测系统覆盖：

- 意图分类混淆矩阵
- Router 校准
- Required/Forbidden Tool
- 多轮任务成功率
- 未授权操作
- 副作用数量
- `outcome_unknown`
- 崩溃恢复
- 信息泄漏
- Token、成本和延迟
- 绝对阈值和相对回归门禁

核心评测入口是 [`evaluation/agent.py`](src/pi_agent_loop/evaluation/agent.py)。

CI 已包含：

- Ubuntu Python 3.11/3.12/3.13
- Windows Python 3.12
- Ruff
- mypy
- pytest + branch coverage
- pip-audit
- CycloneDX SBOM
- License 清单
- wheel/sdist 构建
- Twine 校验
- wheel smoke test

前次完整验证记录为 `767 passed`，另有 `70 subtests`，分支覆盖率约 `78%`。

## 十四、`basic_usage.py` 为什么失败

完整调用正常应该是：

```text
读取配置
→ 打开 SQLite Session
→ 执行启动恢复
→ 添加用户消息
→ 模型生成 add Tool Call
→ 本地执行 add
→ 模型根据 Tool Result 回答“2”
→ Operation 完成
```

当时失败在“启动恢复”阶段。

旧 Session `basic-usage` 中存在未结束 Operation：

```text
20aa210c-d6da-483c-8a66-8ab070f8dfb7
```

旧进程已经完成第一次模型调用和 `add` 工具，但第二次模型请求开始后进程中断，并且恢复记录缺少可以安全续接的 Request Policy。系统因此无法证明：

- 请求是否已经由 Provider 接收
- 是否已经生成过最终回答
- 重新发送会不会造成重复效果
- 应使用什么工具权限恢复

所以它选择 fail-closed，抛出 `StartupRecoveryBlockedError`，禁止新 Prompt 覆盖旧状态。

这不是 `1+1`、API Key 或模型本身失败，而是持久化恢复安全门主动阻止继续运行。使用新 Session 可以绕过旧状态，例如：

```powershell
python basic_usage.py --session-id basic-usage-2 "1+1"
```

旧 Operation 则应通过核对、持久化恢复决策或专门的管理流程处理，不建议直接删除整个数据库。

示例入口：[`basic_usage.py`](examples/basic_usage.py)

## 十五、目前离“满分生产级 Agent”的主要差距

当前版本属于优秀的工程骨架和教学实现，但还不能简单等同于生产级满分系统。深度审查中最重要的剩余问题是：

1. Tool 的部分 `on_update` 输出仍存在绕过统一内容安全审核的路径。
2. 一些恢复终态分类和 `auto_recover=False` 路径仍不够严格。
3. 无审批写操作的执行者身份与原始请求人绑定需要加强。
4. 多处 Deadline 依赖协作式取消，无法强杀阻塞 Handler。
5. 共用 Safety Pipeline 的并发隔离需要进一步验证和整改。
6. EventStream Observer 仍可能反向阻塞生产者。
7. 评测证据存在旧执行结果换新 Challenge 重签、派生指标伪造风险。
8. 当前 CI 的真实 Agent 回归门禁样本太少，且基线和候选可能来自同一 Checkout。
9. `loop.py`、启动恢复、审批等关键模块的分支覆盖率仍偏低。
10. Wheel Smoke 环境、SBOM 开发依赖范围、License allow/deny、构建 Provenance 还需增强。
11. 跨机器部署仍需要共享事务存储、分布式锁、可靠队列和下游 CAS。
12. Shell、浏览器和不可信代码仍需要真正的 OS/容器 Sandbox。

所以要区分三个概念：

- 测试通过：当前测试覆盖的行为正确。
- 工程功能丰富：模块和设计考虑全面。
- 生产级满分：所有故障、安全、并发、分布式和供应链边界都有可验证保证。

这个项目已经很好地完成了前两项，但第三项仍有明确工作量。

## 十六、建议学习顺序

1. 先运行 [`minimal_text.py`](examples/minimal_text.py)，理解最小模型调用。
2. 阅读 `types.py → loop.py → agent.py`，掌握 Agent 核心循环。
3. 阅读 `providers/`，理解 HTTP、SSE 和消息转换。
4. 阅读 `tool_runtime.py`，理解 Tool 校验、并发、锁和重试。
5. 运行 [`durable_host_usage.py`](examples/durable_host_usage.py)，理解 Event Journal 和恢复。
6. 阅读审批与写状态机，理解生产 Agent 为什么不能直接执行副作用。
7. 运行 [`business_routing_usage.py`](examples/business_routing_usage.py)，理解 Intent 与 Capability。
8. 最后学习 `planning/`、`multi_agent.py`、`evaluation/` 和 CI。

最值得掌握的主线是：

> Agent Loop → Tool Runtime → Durable Journal → Approval/Write → Planning → Evaluation

理解这条链，就基本理解了整个项目的技术价值和设计哲学。

---

## 十七、持续问答记录

> 记录规则：自本节开始，后续围绕本项目的用户问题和技术回答将持续追加到本文档中。

### 问答 1：是否支持主流 Agent 工作模式

**问题**

是否能够做到 ReAct、Plan-and-Execute、状态机、任务拆解、工具路由、条件分支、循环终止、反思与自修复、多 Agent 协作？

**回答**

可以。源码层面已经覆盖这些能力，但需要分清“框架支持”和“当前 `basic_usage.py` 已启用”。

#### 能力矩阵

| 能力 | 支持程度 | 当前实现 |
|---|---:|---|
| ReAct | 完整支持 | 模型 → Tool Call → Tool Result → 再次推理 |
| Plan-and-Execute | 完整支持 | Planner 生成持久化 DAG，Executor 按依赖执行 |
| 状态机 | 强支持 | Runtime、Plan、Approval、Write、Domain 多套状态机 |
| 任务拆解 | 支持 | 多 Intent 拆解、步骤依赖、参数引用 |
| 工具路由 | 强支持 | Intent → Capability → Tool 白名单 → Required Tool Guard |
| 条件分支 | 支持 | 前置条件、后置条件、结果引用和跳过逻辑 |
| 循环终止 | 完整支持 | 无工具、`terminate`、预算、超时、取消、审批暂停 |
| 反思与自修复 | 有条件支持 | Execute → Validate → Correct/Replan，需要注入可信 Validator/Replanner |
| 多 Agent 协作 | 本地完整 | Worker DAG、并发、角色、资源锁、仲裁；不是完整分布式集群 |

#### 1. ReAct

ReAct 的典型结构是：

```text
Reason
→ Action
→ Observation
→ Reason
→ ...
→ Final Answer
```

项目实现的是更安全的 Tool Calling 版本：

```text
模型推理
→ 输出结构化 Tool Call
→ Tool Runtime 执行
→ 生成 Tool Result
→ 模型观察结果并继续
```

核心代码是 [`loop.py`](src/pi_agent_loop/loop.py)。

与传统 ReAct 不同，它不要求模型把 `Thought:` 明文输出。推理可以留在模型内部，外部只处理结构化 Tool Call，这样更容易校验、持久化和控制权限。

`basic_usage.py` 已经在使用这种模式：

```text
用户：1+1
→ 模型调用 add
→ add 返回 2
→ 模型输出最终答案
```

#### 2. Plan-and-Execute

项目拥有独立的规划和执行层：

```text
用户请求
→ Intent Router
→ HybridRequestPlanner
→ MultiIntentPlan
→ PlanExecutor
→ Result Synthesizer
```

对应代码：

- [`planner.py`](src/pi_agent_loop/planning/planner.py)
- [`executor.py`](src/pi_agent_loop/planning/executor.py)
- [`autonomous.py`](src/pi_agent_loop/harness/autonomous.py)

计划不是一段自由文本，而是可验证的 DAG：

```text
步骤 A：查询订单
   ├─ 成功 → 步骤 B：检查库存
   │             └─ 有库存 → 步骤 C：创建发货单
   └─ 失败 → 步骤 D：请求人工核对
```

它还支持持久化、暂停、恢复和审批后继续：

```python
plan = await host.plan(request)
result = await host.execute_plan(plan.plan_id)
result = await host.resume_plan(plan.plan_id)
```

#### 3. 状态机

这是项目最强的部分之一，不是只有一个简单 Agent 状态。

现有状态机包括：

- Runtime Operation 状态
- Tool Invocation 状态
- Approval 状态
- Write Operation 状态
- Durable Plan 状态
- Plan Step 状态
- Domain Entity 状态
- Recovery 状态

典型写操作状态为：

```text
prepared
→ waiting_approval
→ approved
→ submitting
→ succeeded
```

异常情况下可能进入：

```text
submitting
→ outcome_unknown
→ reconciling
→ succeeded / failed
```

主要代码：

- [`domains/state_machine.py`](src/pi_agent_loop/domains/state_machine.py)
- [`planning/state_machine.py`](src/pi_agent_loop/planning/state_machine.py)
- [`approval/state_machine.py`](src/pi_agent_loop/approval/state_machine.py)
- [`writes/state_machine.py`](src/pi_agent_loop/writes/state_machine.py)

#### 4. 任务拆解

支持将一个复杂请求拆成多个 Intent 和 Plan Step。

例如：

```text
“检查订单，如果已经付款且库存充足，就创建发货单并通知客户”
```

可以拆解成：

```text
1. 查询订单
2. 校验付款状态
3. 查询库存
4. 创建发货单
5. 发送通知
```

步骤可以声明：

- `depends_on`
- 输入参数
- 前置条件
- 后置条件
- 结果合同
- Tool/Capability
- 是否需要审批
- Replay Policy
- 资源锁

模型提出的计划不会直接执行。Planner 会根据可信 Policy 重新绑定工具、参数和权限，避免模型通过“任务拆解”提升权限。

#### 5. 工具路由

项目有多层工具路由：

```text
用户文本
→ Intent 分类
→ Capability 映射
→ Tool 白名单
→ tool_choice
→ RequiredToolCallGuard
→ ToolDispatchRuntime
```

相关实现：

- [`hybrid_router.py`](src/pi_agent_loop/routing/hybrid_router.py)
- [`routed_agent.py`](src/pi_agent_loop/routing/routed_agent.py)
- [`guard.py`](src/pi_agent_loop/routing/guard.py)
- [`tool_runtime.py`](src/pi_agent_loop/tool_runtime.py)

支持：

- `tool_choice=none`
- `tool_choice=auto`
- `tool_choice=required`
- 指定具体 Tool
- 当前轮工具白名单
- Required Tool 参数精确匹配

Router 只负责分类，最终授权仍由可信配置和 Runtime 决定。

#### 6. 条件分支

Plan Step 支持受约束的条件表达，而不是任意执行模型生成的 Python 代码。

可以表达：

```text
if 查询结果.status == "paid":
    执行发货
else:
    跳过发货
```

也支持：

- 依赖步骤成功或失败
- JSON 结果字段比较
- 前置条件
- 后置条件
- 参数从上游结果中引用
- 条件不满足时跳过
- 下游依赖失败传播

类型定义位于 [`planning/types.py`](src/pi_agent_loop/planning/types.py)，对应测试是 [`test_plan_dataflow_conditions.py`](tests/test_plan_dataflow_conditions.py)。

#### 7. 循环终止

循环具有多重终止机制：

1. 模型没有返回 Tool Call。
2. Tool Result 设置 `terminate=True`。
3. 达到 `max_turns`。
4. 达到 `max_tool_calls`。
5. 达到总 Deadline。
6. CancellationToken 被取消。
7. 工具执行进入不可安全继续状态。
8. Plan 完成、失败或需要人工介入。
9. 等待审批时暂停。
10. 没有剩余 `steer()` 或 `follow_up()` 消息。

因此不会只依赖模型自己说“任务完成了”。

#### 8. 反思与自修复

项目实现的不是单纯让模型输出：

```text
“让我反思一下刚才哪里做错了”
```

而是更可控的闭环：

```text
Execute
→ Observe
→ Validate
→ 是否满足目标？
    ├─ 是：Synthesize
    └─ 否：Correction Plan / Replan
             → 再执行
```

相关接口包括：

- `PlanResultValidator`
- `PlanReplanner`
- `CorrectionPlanner`
- `ClosedLoopBudget`
- `ResultSynthesizer`

核心实现：

- [`closed_loop.py`](src/pi_agent_loop/planning/closed_loop.py)
- [`autonomous.py`](src/pi_agent_loop/harness/autonomous.py)

自修复受到以下限制：

- 最大修正次数
- 最大模型调用数
- Token/费用预算
- 总时间预算
- 不能扩大原始 Capability
- 不能绕过审批
- 不能放宽 Replay Policy
- `outcome_unknown` 不能直接当作失败重试

但需要注意：必须配置可信 `ResultValidator` 和 `Replanner`。没有 Validator 时，框架不会仅凭模型自我评价就宣称成功，而会保守返回 `outcome_unknown`。

所以这一项属于“框架已实现，但需要业务方提供可靠判定标准”。

#### 9. 多 Agent 协作

[`MultiAgentOrchestrator`](src/pi_agent_loop/multi_agent.py) 已支持：

- Worker Registry
- Worker 角色和 Capability
- DAG 依赖调度
- 无依赖任务并行
- 全局并发限制
- 每 Worker 并发限制
- Tenant/Run 隔离
- 资源读写锁
- Deadline 与取消传播
- 依赖失败后跳过下游
- 多副本执行
- Result Arbitrator
- 精确一致性仲裁
- Prompt、消息和结果大小限制

例如：

```text
Coordinator
├─ Research Agent
├─ Code Agent
├─ Security Review Agent
└─ Test Agent
       │
       ▼
Result Arbitrator
       │
       ▼
Final Synthesizer
```

但当前主要是单进程编排。要成为跨机器生产级多 Agent，还需要：

- 分布式任务队列
- 共享事务存储
- 分布式锁
- Worker 心跳
- 跨机器 Fencing
- 可靠消息投递
- Worker 身份认证
- 下游幂等 CAS

#### 当前 `basic_usage.py` 实际启用了什么

| 能力 | `basic_usage.py` |
|---|---:|
| ReAct | 已启用 |
| 工具调用 | 已启用 |
| 基础工具路由 | 已启用，只注册 add/multiply/divide |
| 循环终止和预算 | 已启用 |
| Session 状态和恢复 | 已启用 |
| Plan-and-Execute | 未启用 |
| 复杂任务拆解 | 未启用 |
| 条件 Plan | 未启用 |
| 反思和自动重规划 | 未启用 |
| 多 Agent | 未启用 |

综上：

> 框架层面能够覆盖所列出的全部模式；但现有 `basic_usage.py` 只是“Durable ReAct + Tool Calling”示例。要同时展示 Plan、条件分支、自修复和多 Agent，需要另外装配 Planner、Plan Policy、Result Validator、Replanner、Worker Registry 与 MultiAgentOrchestrator。

功能具备不等于已经达到满分生产质量；前面审查发现的恢复边界、并发安全、硬超时和评测证据问题仍需要完成整改。

### 问答 2：为什么没有使用 LangChain 或 LangGraph

**问题**

别人不都是使用 LangChain、LangGraph 吗？为什么本项目没有使用 LangChain、LangGraph？

**回答**

不是所有 Agent 项目都必须使用 LangChain 或 LangGraph。它们是成熟、流行的框架，但不是 Agent 系统必须遵守的行业协议。

LangChain 官方将自己定位为用于快速构建 Agent 的高层框架，提供预构建 Agent 架构以及大量模型、工具集成。LangChain 当前的 `create_agent` 底层实际上使用 LangGraph。LangGraph 则是更低层的有状态 Agent 编排 Runtime，重点提供 Durable Execution、Streaming、Human-in-the-loop 和 Persistence：

- [LangChain 官方概览](https://docs.langchain.com/oss/python/langchain/overview)
- [LangGraph 官方概览](https://docs.langchain.com/oss/python/langgraph/overview)
- [Framework、Runtime 与 Harness 的官方区别](https://docs.langchain.com/oss/python/concepts/products)

本项目没有直接依赖它们，不代表没有这些能力，而是选择自行实现 Agent Loop、Tool Runtime、Plan Executor、State Machine 和 Durable Journal。

#### 1. 本项目为什么会选择自研

仓库中没有一份 ADR 明确写着“拒绝 LangChain/LangGraph 的原因”，所以下面是根据依赖和源码边界得出的工程判断，而不是对原作者动机的无证据断言。

##### 原因一：项目目标包含学习和展示 Agent Runtime 的底层原理

如果直接使用 LangChain：

```python
agent = create_agent(model, tools=tools)
```

模型循环、Tool Call 转换、状态更新、停止条件等大量细节都会隐藏在框架内部。

本项目则把这些能力显式实现为：

- `loop.py`
- `agent.py`
- `event_stream.py`
- `tool_runtime.py`
- `model_runtime_adapter.py`
- `session/journal.py`

因此更适合研究以下底层问题：

- 一次 Tool Call 如何闭合
- 流式事件如何形成最终 Assistant Message
- 重试 Attempt 如何避免污染正式 Transcript
- 取消怎样向模型和工具传播
- 崩溃后哪些动作能够重放
- 什么情况下必须进入 `outcome_unknown`

##### 原因二：需要严格控制持久化和副作用语义

LangGraph 的 Persistence 采用 Checkpoint 保存 Graph State，可以支持恢复、Memory、Human-in-the-loop 和 Time Travel。官方文档说明，失败后可以从上一个成功步骤或 Super-step 恢复：

- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)

本项目除了保存状态，还专门记录：

- Model Request Intent 和 Request Policy
- Tool Dispatch Intent
- Tool Commit Boundary
- Approval Receipt
- Write Operation
- Idempotency Key
- `outcome_unknown`
- Reconciliation
- Action Hash
- Writer Lease 和 Fencing Generation

这是一套偏审计、安全和外部副作用一致性的事件模型。若直接换成通用 Graph Checkpoint，仍需要自行实现这些业务不变量。

##### 原因三：希望保持最小 Provider 合同

本项目的核心模型合同只有：

```python
stream_fn(model, context, options) -> AssistantMessageEventStream
```

Agent Loop 不依赖 OpenAI、LangChain 或特定模型厂商。当前 OpenAI-compatible Provider 只是这个小协议的一个 Adapter。

这样可以：

- 单独测试 Loop
- 使用 ScriptedProvider 离线测试
- 自己控制 SSE 边界
- 自己定义错误分类和脱敏
- 避免 Provider SDK 的行为泄漏进核心循环

##### 原因四：控制依赖数量和供应链

[`pyproject.toml`](pyproject.toml) 中的运行时外部依赖主要只有：

- `httpx`
- `cryptography`

没有 LangChain/LangGraph 后：

- 安装体积更小
- 依赖树更短
- 版本升级影响面更小
- SBOM 和许可证检查更简单
- 更容易进行离线和确定性测试

但这也意味着项目必须自己维护大量框架代码。

##### 原因五：需要完全掌控流式、并发和恢复边界

本项目自行定义：

- EventStream 背压
- 不可丢弃的结构事件
- Tool Result 原始顺序回灌
- 整批 Tool Call 原子预检
- Resource Lock、Exclusive 和 Parallel 调度
- Model Commit Boundary
- Tool Replay Policy
- Startup Recovery Gate

这些语义如果建立在第三方框架之上，就必须接受或适配第三方框架的执行生命周期。

#### 2. 两种方案的实际对比

| 维度 | LangChain / LangGraph | 本项目自研 Runtime |
|---|---|---|
| 上手速度 | 快，预构建 Agent 较多 | 慢，需要自己装配 |
| 模型集成 | 非常丰富 | 当前主要是 OpenAI-compatible |
| Tool 集成 | 社区生态丰富 | 数量少，但控制严格 |
| Agent Loop | LangChain 提供现成实现 | 完全自行实现 |
| 图编排 | LangGraph State/Node/Edge | Durable Plan、DAG、状态机 |
| 持久化 | Graph Checkpoint | 加密 Event Journal |
| Human-in-the-loop | Interrupt/Resume | Approval 和 Write 状态机 |
| 可观测性 | 通常结合 LangSmith | 内建 Telemetry 和 Evaluator |
| 副作用恢复 | 需要节点和应用正确设计 | 显式 Intent、Commit、Unknown、Reconcile |
| 安全审计 | 依赖应用和 Middleware | 深度嵌入 Tool/Approval/Journal |
| 底层透明度 | 部分细节由框架管理 | 控制流基本全部可见 |
| 维护成本 | 主要由框架社区承担 | 本项目自己承担，明显更高 |
| 社区与招聘 | 优势明显 | 生态和人才熟悉度较弱 |

#### 3. 不使用 LangChain/LangGraph 的代价

自研不一定比使用框架更高级，也有明显成本：

- 重复实现已有 Agent Loop 和 Graph 能力
- 需要长期维护 Provider 兼容性
- 缺少大量现成 Tool、Retriever 和 Vector Store 集成
- 缺少成熟的图可视化和调试生态
- 新成员学习成本更高
- 并发、恢复和安全 Bug 需要项目自己承担
- 当前代码量和测试量已经明显大于普通 Agent 应用

前面审查发现的恢复、安全管线、硬超时和评测证据问题，也说明自研 Runtime 的正确性成本非常高。

#### 4. 这个项目是否应该改成 LangChain/LangGraph

不建议仅仅因为“别人都在用”就整体重写。

如果项目目标是以下内容，保留当前自研架构是合理的：

- 学习 Agent Runtime 底层实现
- 研究 Tool Calling 和恢复语义
- 要求最小依赖
- 高度定制 Approval、Write 和 Reconciliation
- 需要精确掌控事件格式和安全不变量

如果项目目标变成以下内容，引入 LangChain/LangGraph 会更有价值：

- 快速接入大量模型、Retriever、数据库和 SaaS Tool
- 希望使用成熟 Graph API 和可视化工具
- 团队已经熟悉 LangChain 生态
- 希望减少自研编排代码
- 需要快速交付常规知识库或工作流 Agent

#### 5. 更稳妥的混合方案

可以增加可选 Adapter，而不是推翻现有核心：

```text
LangChain Model / Tool
        │
        ▼
PI Agent Adapter
        │
        ▼
现有 ToolDispatchRuntime / DurableAgentHost
```

或者：

```text
LangGraph 外层业务工作流
        │
        ▼
一个节点调用 DurableAgentHost
        │
        ▼
高风险 Tool、Approval、Write、Recovery
仍由 PI Runtime 管理
```

混合时必须明确唯一责任边界：

- 只能有一个正式 Transcript 所有者
- 只能有一个副作用 Commit Boundary
- 明确由谁负责 Retry
- 明确由谁负责 Checkpoint/Journal
- 不让 LangGraph 和 PI Runtime 同时自动重放同一个写操作
- 不让两套状态存储互相宣称是最终事实来源

#### 结论

> LangChain 的优势是快速开发和生态集成；LangGraph 的优势是通用图编排、持久化和 Human-in-the-loop；本项目的优势是底层透明、依赖较少，并对审批、写操作、审计和崩溃恢复做了更细的定制。

所以本项目没有使用 LangChain/LangGraph，不是因为它们做不到，而是项目选择自行拥有这些 Runtime 语义。这个选择带来了控制力，也带来了巨大的维护和正确性成本。

当前更合理的方向不是整体替换，而是保持核心安全和持久化边界，同时通过可选 Adapter 获得 LangChain/LangGraph 的模型、工具或外层工作流生态。

### 问答 3：以面试方式形成完整项目总结

**问题**

要求后续回答使用正常面试方式，详细讲解项目技术架构和完整架构图，解释每个技术选型及替代方案，说明为什么自研而不是使用 LangChain/LangGraph，总结不少于二十个技术难题，并提供五十道不同的技术面试题和答案，最终写入 `项目总结.md`。

**回答**

已生成独立面试手册 [`项目总结.md`](项目总结.md)，内容包括：

- 可直接口述的项目开场介绍；
- 完整 Mermaid 技术架构图和请求时序图；
- 分层架构与端到端执行链路；
- 25 项核心技术选型及替代方案比较；
- 自研与 LangChain/LangGraph 的客观取舍；
- 25 个可在源码和测试中对应的工程难题、解决方案和剩余边界；
- 50 道不重复技术面试题及参考答案；
- 当前生产化差距、下一步整改方向和源码阅读索引。

文档已校验：技术难题数量为 25，面试问答数量为 50，两张 Mermaid 图的代码块完整，仓库内 Markdown 链接均能解析到实际文件。

### 问答 4：线上稳定性、安全与回滚专项

**问题**

结合本项目回答线上超时、工具调用失败、上下文膨胀、工具幻觉、并发、成本、安全和回滚问题，并把结果写入项目总结。

**回答**

已在 [`项目总结.md`](项目总结.md) 的“十一、线上工程问题专项面试回答”中增加完整内容，按照“面试官追问—候选人回答—当前实现—不足与生产改进”的方式逐项说明：

- 模型、Tool、锁等待和 Plan 的分层 Deadline；
- Tool 失败阶段分类、幂等重试、Synthetic Result 和 `outcome_unknown`；
- Token-aware Compaction、Memory 分层和调用前 Token Admission；
- Tool 白名单、Required Tool Guard、参数校验和工具合同密封；
- Agent、Tool、Session、Plan、Multi-Agent 五层并发控制；
- Turn、Attempt、Token、Cost、Correction Round 多层预算；
- 内容、身份、授权、审批、文件、秘密、网络与沙箱的纵深安全；
- 内部事件补偿、本地版本写、外部 Reconciliation 与 Saga Compensation。

专项回答强调：外部副作用不存在通用数据库式回滚。提交前可以安全失败，幂等操作可以受控重试，提交后结果不明必须对账，已经成功的外部副作用只能通过新的受审计业务补偿操作撤销。
