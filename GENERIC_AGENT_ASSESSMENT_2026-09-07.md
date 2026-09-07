# 通用 Agent 接入评估与修复记录

对象：`D:\0715\git_codex\20260825\PI_agent_study_upload\agent-loop`。日期：2026-09-07。

**判断：核心具备通用 Agent 框架的结构，能够通过外部工具和适配器接入不同业务；目前还不是“放一个业务文件到 tools 目录就自动完成接入”的产品。**

这里的判断依据是源码中的依赖与执行路径：模型可替换、工具可注入、循环不依赖具体业务、权限和持久化通过接口装配。没有必要为了“通用”重写现有循环；需要改进的是业务包的统一装配，以及一些高级能力之间的接口完整性。

## 1. 已修复原报告中的 5 项问题

| 原编号 | 修复结果 | 验证重点 |
| --- | --- | --- |
| F1 / P1 | 任务结果经状态存储确认后才向依赖任务发布成功；收集并传播编排异常。新增公开异常 `OrchestrationStateError`，保留已观察到的 `task_result`，避免把已执行任务误当成可以直接重跑的失败。 | 分别模拟“保存前失败”和“保存后确认丢失”，依赖 Worker 均不执行，整体不再返回成功。 |
| F2 / P2 | 同一安全策略的正常检查排队执行，排队与执行共用超时额度；保留对超时后拒绝退出策略的失败关闭。取消等待者会清理锁与等待任务。 | 两个并行工具结果均通过审核并进入下一模型轮次；分别测试 Token 取消、Task 取消和后台任务清理。 |
| F3 / P2 | grep 匹配完整行，只裁剪展示片段。新增字符列号、片段起始列与 `textTruncated`，区分展示裁剪和搜索不完整。 | literal 和受限 regex 均能命中第 4096 字符之后的内容；行尾锚点按真实行尾匹配。 |
| F4 / P2 | 文件修改队列按规范绝对路径共享进程内锁，覆盖独立服务实例和不同事件循环；同一锁内完成版本检查与发布。等待者取消不会在线程池留下迟到的锁占用。 | 两套服务并发 write/edit 时仅一项成功，另一项返回 `stale_observation`；测试跨事件循环协调、取消清理及不同文件可并发。 |
| F5 / P2 | 同步仲裁器在线程中执行；运行完成时校验总 deadline，并保留取消传播。 | 阻塞的同步仲裁器不能阻止 deadline 结束编排，也不能让超时任务返回成功。 |

实现位置：[multi_agent.py](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/multi_agent.py)、[safety.py](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/safety.py)、[workspace_files.py](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/tools/workspace_files.py)、[mutation_queue.py](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/tools/mutation_queue.py)。回归测试位于 [test_review_regressions.py](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/tests/test_review_regressions.py)。

另外，将路由测试依赖的教学配置放入固定测试夹具，避免本地业务示例变更破坏框架测试。原有意图、能力和参数断言仍保留，并另行检查当前 `config/business.toml.example` 能否被严格加载。没有回退或覆盖当前工作区的业务示例配置。

修复边界：文件锁仍只协调本进程；线程超时仍不能强杀线程中的同步代码。写入和编辑工具的实现版本已从 `2` 升至 `3`，grep 的安全合同版本也已更新；受管历史会话应按现有配置迁移流程处理合同变化，不能绕过恢复校验。

## 2. 为什么说它具备通用 Agent 的基础

| 维度 | 代码依据 | 判断 |
| --- | --- | --- |
| 模型接口 | `stream_fn(model, context, options)` 由外部传入 | 核心不要求某一家模型；内置 OpenAI-compatible Provider 只是适配器。 |
| 工具接口 | `AgentTool`、`ToolRegistry`、`Agent(tools=...)` | 工具实现不需要写进循环，也不需要为每种业务新增执行分支。 |
| 业务选择 | `CapabilityRegistry`、`RouterLike`、`RoutedAgent` | 意图、稳定能力和具体函数名分离，可以替换业务工具实现。 |
| 状态与恢复 | 通用 Runtime、Session、Approval、Write、Plan；领域状态机接收外部定义的转换 | 框架负责执行事实与恢复，业务状态可以留在业务系统或业务包。 |
| 外部依赖 | Provider、身份验证、资源工厂、核对回调等注入点 | 能适配不同服务；部分高级组合仍有下文列出的限制。 |

本次还在临时目录创建了一个独立的合成只读工具包：它只从 `pi_agent_loop` 公开 API 导入 `AgentTool` / `AgentToolResult`，由应用使用 `ToolRegistry.register_many()` 注册。通过 `ScriptedProvider` 完整跑通“模型调用工具 → 外部包返回结果 → 下一轮模型读取结果”，不需要修改核心源码。这验证了最基础的外部工具接入路径；它不是生产业务联调。

注册实现见 [registry.py:20](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/tools/registry.py:20)，能力映射见 [capabilities.py:101](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/routing/capabilities.py:101)，通用 Router 接口见 [routed_agent.py:21](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/routing/routed_agent.py:21)。

## 3. 后续接入业务，到底要改什么

**仅新增文件不会生效。** 框架没有扫描任意 `tools/*.py` 并自动导入注册的机制；现有内置工具工厂也只负责文件和 Shell 等指定能力。显式注册本身是合理的控制边界。

| 使用目标 | 应用方最小接入工作 | 通常是否需要改核心 |
| --- | --- | --- |
| 最简、只读的通用 Tool Calling | 实现工具、Schema 和校验；显式导入注册并传入 `Agent(tools=...)`；注入 API 客户端等依赖 | 否 |
| 指定业务范围、必须查实时数据、缺参追问 | 上述内容，加业务 Intent 配置、Capability 映射和 Router/授权装配 | 否 |
| 有审批的可恢复业务写入 | 上述内容，加可信身份、权限、精确审批、幂等键、结果核对与 Durable Host 装配 | 否；所需扩展接口满足该业务时成立 |
| 多步骤自主执行 | 加 Plan Policy、Intent→Tool 绑定、可信结果验证和预算计量；按需求配置安全的纠正计划 | 通常否，但受高级装配限制影响 |
| 多机执行或自定义持久后端 | 事务 Store、Claim/Fencing、分布式锁以及下游条件写入 | 需要额外适配；当前完整 Autonomous 路径还有限制 |

业务 API 本身已经维护实体状态时，Agent 不必再复制一套订单、退款等状态机。只有需要在应用中维护额外的业务状态转换时，才在业务包定义它；不要让模型输出直接修改业务事实。

真实业务建议按项目已有规范放入独立 Python 包，包内仍可以使用你熟悉的 `tools` 目录：

```text
my_business/
  tools/                 API 工具、Schema、参数校验
  clients/               HTTP/数据库客户端及连接管理
  config/                业务意图、允许范围和策略配置
  policies/              权限和业务限制
  recovery/              写入幂等、结果核对；需要时添加
  bootstrap.py           一处完成注册和 Host 装配
  tests/                 Mock 与业务集成测试
```

工具较少时不必立即建立全部目录，可以由一个工具模块和一个注册入口开始。关键是核心不反向导入真实业务包。现有 [BUSINESS_REQUIREMENTS.md:14](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/BUSINESS_REQUIREMENTS.md:14) 和 [TOOLS_IMPLEMENTATION_GUIDE.md](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/TOOLS_IMPLEMENTATION_GUIDE.md) 已明确要求这一点。

如果你把“以后只加 tools”理解为“以后只新增业务实现和业务配置，不再改循环、调度、审批与持久化核心”，现有架构基本支持这个方向。如果理解为“只放一个函数文件，框架自动推断所有意图、权限、幂等与恢复规则”，当前不支持；这些规则也不能仅凭函数名可靠推断。

## 4. 距离统一、方便的业务接入还差什么

### A1. 缺少统一的业务扩展包合同和装配入口

当前应用分别维护 Tool 列表、Capability 注册、Intent 配置，复杂任务还维护 `plan_policies` 和 `plan_tool_bindings`。这些接口可用，但业务越多，人工保持它们一致的成本越高。`ToolRegistry` 只管理工具集合，没有包含所有业务元数据和生命周期的统一 Bundle。

建议增加受信的业务包描述接口，由一个显式注册入口汇总工具、能力、意图、策略、可选核对器和资源释放函数，并在启动时检查引用一致性。这样新增工具及其元数据后，只需在业务包入口登记。自动加载也应来自允许列表或明确配置，不能无条件执行目录里的任意 Python 文件。

这是可简化的装配工作；并不意味着应删除 Capability、审批或幂等规则。

### A2. 普通 Agent 的扩展能力没有完整贯通到 Durable Host

`Agent` 构造函数接受 `content_safety` 和 `transform_context`。但 `DurableAgentHost.create` 没有对应的公开参数，工厂内部创建 Agent 时也没有传入它们。不能把“普通 Agent 支持某能力”直接视为“持久 Host、恢复和 Plan 路径都已接入该能力”。

建议增加明确的 Agent/Host 策略与上下文装配配置，统一接入审核、记忆检索等能力，并验证正常执行、恢复执行和 Plan 工具输出都走同一策略。应避免业务方依赖创建 Host 后手工修改内部对象。

证据：[durable_agent_host.py:137](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/harness/durable_agent_host.py:137)、[factory.py:638](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/harness/factory.py:638)。

### A3. 完整 Autonomous 持久化仍依赖具体实现

虽然有 `DurablePlanStore` 协议，完整 Autonomous 装配仍通过 `isinstance(..., SessionJournalPlanStore)` 限定实现，并要求 Plan、Run、Conversation 使用同一 Journal 的事务边界。单独实现 `DurablePlanStore` 并不能接入所有高级路径。`DurableHostResources.journal` 的类型也仍指向 `SQLiteSessionEventJournal`。

该限制保护启动与完成投影的原子性，不能简单删除类型检查。建议提炼统一 Session Journal / 多流事务协议，把必须原子提交的操作纳入合同，再让 SQLite 和其他后端实现相同的合同测试。

证据：[factory.py:520](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/harness/factory.py:520)、[resources.py:31](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/harness/resources.py:31)。普通 Plan 后端扩展接口仍然有效，此处限制针对完整 Autonomous 组合。

### A4. 自定义 Router 的硬预算接口不完整

普通路由使用 `RouterLike`，但启用硬 Model/Token/Cost 预算时，Host 要求 Router 是 `HybridModelRouter`，其他 Router 会被拒绝，因为尚未提供通用的路由前持久预算预留合同。

建议把路由运行时绑定、物理模型调用计量和预算准入提炼成可实现的接口。保留“不满足预算合同则拒绝执行”的行为，以接口能力取代对具体类的依赖。

证据：[factory.py:560](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/harness/factory.py:560)。

### A5. 生产适配能力尚需另行交付

内置 Provider 尚未透传常用生成上限、温度与流式 usage 请求参数；真实身份、审核、Embedding、跨重启澄清状态以及业务 API 适配器仍需应用方提供。它们不使核心变成“非通用 Agent”，但会影响“接上真实服务即可长期运行”的完成度。

其中一部分可以作为可选通用适配包提供，避免每个项目重复开发。业务特有的权限、写入幂等与结果核对则应继续由业务包负责。

## 5. 后续改进顺序

1. 优先设计 A1 的单一业务包注册入口，使业务扩展集中在自己的 `tools`、配置及注册工厂中。
2. 贯通 A2 的 Host 策略/上下文装配，让审核和记忆等能力不依赖修改内部对象。
3. 需要更换路由器或部署后端时，再落实 A3/A4 的协议抽象与合同测试。
4. 根据实际要连接的服务选择 A5 的适配包；业务 API、权限和写入合同明确后再实现真实业务。

以上 A1—A5 是本次指出的架构与交付不足，没有在本轮擅自实现新的业务包或大规模重构。原审查报告列出的 5 个 P1/P2 实现缺陷已按前述方式修复。

验证结果：Windows / Python 3.12.1 下，`python -m pytest -q` **780 passed**，耗时
268.13 秒；`python -m ruff check src tests` 通过；`python -m mypy --no-incremental src`
对 142 个源文件检查通过；`git diff --check` 通过。新增 12 个缺陷回归用例及 1 个
工作区示例配置检查，原有 767 项测试均保留。独立合成工具包的两轮模型/工具接入验证通过。
没有连接真实模型或业务系统，没有执行完整 CI 的供应链和打包任务。
