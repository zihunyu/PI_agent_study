# 通用 Agent 源码对比与演进建议

审查日期：2026-09-08。本报告针对本机源码快照，不以项目名称或 README 中的“教学”字样判断实现水平。

| 项目 | 审查目录 | Git HEAD |
| --- | --- | --- |
| 用户项目 | `D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop` | 根仓库 `275fc29` |
| Pi | `D:/0715/git_codex/20260825/pi` | `b2602be7` |
| DeepSeek Harness | `D:/0715/git_codex/20260825/deepseek-harness` | `c389f96bf` |

本轮检查了核心循环、业务装配、路由、模型 Runtime、执行环境、上下文、规划核验、记忆、多 Agent 及相关扩展实现。结论属于架构和功能差距评估，不代表穷尽全部缺陷。只新增本报告，没有修改框架或两个参考项目；没有连接收费模型或真实业务，没有重跑全量回归。额外执行了一次使用 ScriptedProvider 的离线上下文审计复现，结果见第 3 项。

## 结论

你的代码已经具备可继续产品化的执行基础：统一业务包、统一执行策略、工具审批、安全检查、预算和物理调用计量、重试取消、计划恢复、事务 Journal 协议、SQLite 持久化、记忆作用域、多 Agent 编排和评估框架。不能把它描述成“只有一个 while 循环的教学 Demo”。

目前更准确的定位是：**具备较强控制与恢复能力的 Agent 框架，但通用任务入口、可直接使用的扩展生态、执行环境、长任务上下文和目标验收还没有形成统一的使用路径。**

通用 Agent 不需要预先内置所有业务，也不要求任意任务都成功。它需要在授权能力范围内组合工具、维持目标和证据、识别失败、恢复执行，并可信地报告完成或无法完成。

下述 P1/P2 是建议的产品化实施优先级，不是给全部差距判定同等级安全漏洞。MCP、多 Agent、远程服务是否优先，取决于目标使用场景。

## 1. P1：业务包的默认入口仍然以预定义 Intent 为中心

**现状与影响。** `BusinessBundle.create_router()` 默认装配 `HybridModelRouter`。该 Router 对未匹配 Intent 的任务返回 out_of_scope；general.qa 分支禁止调用工具。因此“读文件、查询资料、综合分析、生成交付物”这类临时组合任务，在默认业务包路径中仍需预先映射业务意图。普通 Agent 已能进行开放工具调用，所以这不是整个框架都不支持通用任务，而是两种能力缺少统一的入口。

源码：[业务包装配](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/business.py:97)、[范围外和普通问答分支](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/routing/hybrid_router.py:760)。

**建议。** 增加明确的通用任务模式：根据目标和当前获准能力选择工具、形成和调整步骤；现有 Intent 模式继续服务有明确规则的业务。两种模式共用审批、预算、Tool Runtime 和恢复机制。开放任务模式不能使原本被禁止的业务操作经由其他工具获得授权。

**验收。** 不为一个测试任务新增 Intent，仅注册已授权的文件、检索和产物工具，就能完成跨工具任务；未授权工具和业务操作仍被拒绝。

## 2. P1：缺少可替换的统一执行环境；本地 Shell 明确不是沙箱

**现状与影响。** `ToolServices` 直接创建本地路径策略、文件写入器和 ProcessRunner，现有文件边界与安全 profile 有价值，但并不等于操作系统隔离。源码明确要求启用 Shell 时显式承认它是受信、非沙箱 Shell。将工具整体迁移到容器或远程工作区，目前仍需逐个适配。

源码：[ToolServices](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/tools/services.py:33)。参考：[Pi ExecutionEnv](D:/0715/git_codex/20260825/pi/packages/agent/src/harness/types.ts:391)、[DeepSeek 本地沙箱适配](D:/0715/git_codex/20260825/deepseek-harness/packages/sandbox/sandbox-local/src/index.ts:1)。

**建议。** 抽出文件、进程、工作区资源及清理的执行环境协议，让文件和 Shell 工具依赖同一个实例。先保留 Local 实现，再实现一种有明确隔离能力的后端；统一报告文件、网络、进程和凭据限制是否实际生效。对只调用业务 API 的部署，可以先不启用代码执行。

**验收。** 同一套工具合同在本地和隔离环境通过；路径越界、进程取消、超时清理、网络限制有实际执行测试。后端不可用时不能静默降级到宿主机。

## 3. P1：动态上下文缺少完整的模型请求审计记录

**现状与影响。** 模型 Runtime 先记录请求策略，再执行上下文转换。转换结果进入模型，但不写回原会话，也没有在该 Runtime 的持久事件中保存实际输入快照。保持原历史不变是合理设计；问题在于缺少另一个可审计的请求记录。当检索数据变化后，仅凭这些事件无法解释旧请求究竟依据了什么。

源码：[请求事件](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/harness/model_runtime_adapter.py:430)、[上下文转换](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/harness/model_runtime_adapter.py:669)。参考：[DeepSeek 动态上下文快照](D:/0715/git_codex/20260825/deepseek-harness/packages/core/agent-loop/src/runtime-context.ts:64)、[模型请求与会话记录的一致性检查](D:/0715/git_codex/20260825/deepseek-harness/packages/core/agent-loop/src/invariant.ts:19)。

**本轮离线复现。** 使用公开 ModelCallRuntime、ExecutionPolicy 和 ScriptedProvider，转换回调注入唯一标记，模型正常返回。观察结果：模型收到标记为 true；Runtime 持久事件包含标记为 false；事件只有 model_request_started 和 model_request_completed。此结果验证该默认边界的记录不足，不代表调用方不能自行额外记录。

**建议。** 在实际模型派发边界生成请求快照，覆盖转换、安全改写、压缩之后的输入，并关联工具合同、参数、来源版本和物理尝试。正文使用受权限保护的加密存储或可解析的不可变引用；摘要用于校验，单独一个 hash 不能还原正文。恢复仍重新注入可信回调并校验当前授权，不从历史反序列化回调，也不能因有旧快照而跳过审批。

**验收。** 修改外部检索内容并重启后，仍能查到旧调用看到的授权输入；新请求使用当前允许的数据。重试、压缩和路由请求的快照都与实际派发一致。

## 4. P1：目标核验与纠错已有接口，缺少开箱可用的实现组合

**现状与影响。** AutonomousPlanRunner 已支持结果核验、重规划和硬预算，但 Host 默认的 result_validator/replanner 为 None。计划结构完成而没有可信核验器时，代码返回 unknown，避免误报成功。这是正确行为，也意味着只加载业务工具与 TOML，尚不能自动获得“目标真的完成”的判断。默认 BundlePlanner 也没有补齐这两项能力。

源码：[Host 装配参数](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/harness/factory.py:147)、[默认完成核验](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/harness/autonomous.py:1542)。参考：[DeepSeek 重复工具调用提醒](D:/0715/git_codex/20260825/deepseek-harness/packages/guard/repeat-tool-reminder/src/index.ts:1)，该实现是提醒机制，不是完整的语义成功判定器。

**建议。** 提供可装配的任务验收合同、确定性核验器、证据集合和重规划策略。先支持文件/产物检查、结构化输出合同、业务查询确认和测试结果，再让模型辅助评估开放结果。补充重复调用、观察未变化、同一失败反复出现的检测；分别处理网络重试和目标策略调整。模型说“成功”不能替代业务证据。

**验收。** 工具返回成功但产物缺失时不得结束为成功；重复读取相同错误结果会停止或调整计划；未知写入结果继续走现有对账与人工处理路径。

## 5. P1/P2：MCP、Skills 和扩展生命周期尚未形成可直接使用的接入层

**现状与影响。** 项目有工具注册、Hooks、工厂和业务 TOML，但当前 src 中未见内置 MCP Client 或技能资源加载器。工具工厂可以包装外部服务，却还要由每个应用自行实现发现、连接、重连、工具同步和关闭；BusinessBundle 的范围也没有覆盖技能与验证器等资源的统一生命周期。

源码：[业务包边界](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/business.py:23)、[工具注册表](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/tools/registry.py:1)。参考：[DeepSeek MCP 启动、命名空间与清理](D:/0715/git_codex/20260825/deepseek-harness/packages/mcp/mcp-client/src/index.ts:145)、[Pi 技能加载与来源](D:/0715/git_codex/20260825/pi/packages/agent/src/harness/skills.ts:51)。

**建议。** 通用外部工具接入优先实现 MCP 的 stdio/HTTP 适配；将远端工具转换为现有 AgentTool，仍经过本地授权和输出安全检查，不能无条件相信服务端的只读标注。随后增加 Skills 元数据发现、按需加载正文、来源与版本记录。扩展包声明工具、技能、验证器、依赖和资源关闭函数，先完成启动校验和失败回滚，暂不追求热加载。

**验收。** 新外部工具接入不修改核心分支；命名冲突、连接断开、取消、工具合同变化和启动中途失败都能受控处理；加载技能文本不能扩大权限。

## 6. P2：长上下文主要依赖溢出后的补救，缺少统一的前置预算分配

**现状与影响。** 已有滑动窗口与结构化 Token 压缩，但默认压缩包装器先请求模型，遇到上下文溢出后才压缩重试。它没有形成每轮请求前统一分配系统约束、任务状态、工具定义、历史、检索内容和输出预留的默认流程。

源码：[溢出后压缩路径](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/retry/compaction.py:306)。参考：[Pi 前置压缩阈值](D:/0715/git_codex/20260825/pi/packages/coding-agent/src/core/compaction/compaction.ts:235)、[包含任务状态的摘要生成](D:/0715/git_codex/20260825/pi/packages/coding-agent/src/core/compaction/compaction.ts:656)。

**建议。** 建立共享 ContextBuilder，按明确的模型窗口配置和使用量估计主动压缩，保留目标、约束、未完成步骤、证据引用和工具消息配对。摘要及检索也受预算、审批事实隔离和请求快照管理；溢出重试继续作为后备措施。

**验收。** 长任务在接近窗口上限时主动处理，不以反复溢出作为正常流程；压缩后不遗失待审批动作，不将摘要内容当作真实执行结果。

## 7. P2：内置模型协议适配范围仍较窄

**现状与影响。** 已经支持生成参数、usage 校验、多模型路由和故障处理；但配置内置协议仅接受 openai_chat_completions，Provider 工厂也固定创建 OpenAICompatibleProvider。通用 StreamFn 可以接入其他实现，不过尚未提供相同成熟度的原生协议适配。

源码：[协议校验](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/providers/settings.py:259)、[Provider 工厂](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/providers/factory.py:12)。参考：[Pi 多种原生协议边界](D:/0715/git_codex/20260825/pi/packages/ai/src/index.ts:8)。

**建议。** 增加 Provider 注册与能力描述，优先做实际需要的第二种协议。按协议明确结构化输出、多模态、推理内容、缓存用量和取消语义，避免按模型名猜测支持情况。所有实现继续通过共享模型 Runtime。

**验收。** 两种不同协议通过实际请求 JSON 和流式响应合同测试；换 Provider 不修改 Host、Router 或预算实现；缺失用量仍不能按零费用结算。

## 8. P2：已有持久记忆，但文档知识与交付物链路仍需补齐

**现状与影响。** 已有 EmbeddingProvider 协议、作用域隔离和持久存储；内置 HashingEmbeddingProvider 明确是离线特征哈希，不是学习到的语义模型。SQLite 检索实现对作用域内受数量限制的记录扫描排名，适合有界规模。尚未形成完整的文档导入、解析分块、版本化索引、带来源引用的检索与产物管理流程。

源码：[Embedding 实现边界](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/memory/embeddings.py:30)、[有界扫描检索](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/memory/store.py:526)。这一项主要根据你的通用文档/知识任务目标提出，不声称 Pi 或 DeepSeek Harness 已提供完整企业知识平台。

**建议。** 接入一个真实可用的本地或服务端语义 Embedding 实现；增加文档来源、版本、权限、解析和引用合同，以及带 MIME、校验和、生成证据的 ArtifactStore。只有数据规模和实测延迟证明有需要时，再增加向量索引后端。长期记忆写入继续要求明确授权。

**验收。** 同义表达检索、文档更新删除、引用可定位和权限撤销均有测试；生成的文件可被用户获取并验证，不只是在回复中声称已经生成。

## 9. P2：多 Agent 已能编排，默认运行状态还不是持久子会话体系

**现状与影响。** MultiAgentOrchestrator 默认使用内存 BoundedRunStateStore，并通过 asyncio 运行任务。已有持久 Plan Worker 不应被忽略，但不能因此认为独立多 Agent 编排的父子关系、收件箱、交付和恢复都已经持久化。

源码：[默认编排状态存储](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/multi_agent.py:919)。参考：[DeepSeek 持久子会话与消息标识](D:/0715/git_codex/20260825/deepseek-harness/packages/subagent/subagent/src/types.ts:31)。

**建议。** 在单 Agent 任务链路可靠之后，将子任务建模为受管 Session/Run：记录父子身份、任务合同、交付物、消息去重、共享预算和取消权限，并接入现有 Journal 与 Worker。需要外部应用使用时再提供统一服务协议，涵盖提交、状态、事件游标、取消和审批；避免在服务层复制执行逻辑。

参考：[DeepSeek SDK 的明确通信协议](D:/0715/git_codex/20260825/deepseek-harness/packages/sdk/protocol/src/index.ts:1)。这不是要求你必须采用 JSON-RPC，也不是认为 Python 库入口不存在。

**验收。** 父进程重启后能找到既有子任务并去重接收结果；全局预算不会因并行子任务分别计数而失效；取消不会留下无人管理的任务。

## 10. P1：上线验收需要通用任务样本，不能仅依据单元测试数量

**现状与影响。** 项目已经有大量回归、安全、恢复和评估基础。当前随包发布的 agent-core-v1 场景集有 5 个 Mock 案例，覆盖若干重要底层合同，但不足以证明开放任务的规划质量、长程完成率和真实产物正确性。这不是说整个测试目录只有 5 个测试。

源码：[随包评估场景](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/evals/agent-core-v1.json:1)。

**建议。** 为前面每项增加跨模块验收，至少包含文件与外部只读工具组合、上下文压缩、工具合同变化、恶意工具输出、目标核验失败、实际进程终止恢复、断网与未知写入结果、审批过期、租户隔离和生成物验证。保留可离线重复的协议夹具，再按正式授权增加隔离环境的服务联调。用任务成功率、错误成功率、恢复后重复副作用、成本与时延评估，而不只看测试总数。

**验收。** 每个场景有独立的结果判定和可定位失败证据；外部观察到的业务结果与 Agent 报告一致。成本和时延阈值根据实际业务目标制定，不凭空宣称已达到生产 SLA。

## 建议实施顺序与模块边界

1. **先交付一条通用任务链路。** 通用任务入口、统一执行环境、一个外部工具接入方案、请求上下文快照、基础产物核验和端到端评估。涉及 Shell 执行时，同步完成隔离后端。
2. **再强化长任务与扩展复用。** 主动上下文管理、Skills、资源生命周期、第二种 Provider 协议、真实语义检索和文档产物链路。每次只选择目标任务实际需要的适配器。
3. **最后按部署需要扩展运行方式。** 持久子会话、远程服务协议、可运维的鉴权与配额入口、多实例协调；有跨主机需求时再实现新的 Journal 后端并运行同一事务合同测试。

推荐在现有 `agent-loop/src/pi_agent_loop` 内逐步建立 `execution/`、`extensions/`、`context/`、`validation/`、`artifacts/` 等边界；这是候选目录设计，不要求立即创建所有空接口。业务包继续位于外部，提供工具实现、配置、必要的技能和核验器。框架负责统一装配、权限、预算、持久化和生命周期，接入业务不修改核心循环。

“以后只在 tools 下新增代码”适合单个 API 工具，但不宜作为所有业务接入的硬限制：长连接需要关闭，任务需要验收合同，技能需要资源文件。更有用的目标是 **新增一个独立业务/能力包，通过公开入口完成装配，不改核心执行分支**。可以兼容现有 `load_business_bundle()`，不必推翻已完成的工作。

首个验收任务可以使用虚构、可控的数据源：读取本地资料 → 查询模拟外部服务 → 整理带来源的分析 → 保存报告 → 独立核验报告；在执行中间杀死进程再恢复，检查无重复写入、审批仍有效、模型输入可追溯。这个例子是建议新增的测试，不是声称现有代码已经完成这些效果。

## 不应照搬或优先重做的部分

- **不要为“通用”删除 SQLite。** 它是现有可靠恢复能力的实现之一，与任务是否通用无直接冲突。先保留，再用明确的部署需求驱动后端替换。
- **不要放宽现有审批、预算和未知结果处理。** 开放任务组合仍然需要确定的执行边界。
- **不要直接照搬参考项目全部功能。** Pi 明确没有内置权限系统，需要外部隔离；DeepSeek Harness 自称 developer preview，并明确报告 Windows 沙箱的 partial enforcement。源码存在某能力不等于已经替你的部署完成验收。
- **不要把改名、去掉“教学”说明、增加 Web UI 当成能力升级的主要证据。** 应以跨工具任务能完成、可核验、可恢复、可扩展为标准。

参考：[Pi 权限说明](D:/0715/git_codex/20260825/pi/README.md:39)、[DeepSeek 发布阶段](D:/0715/git_codex/20260825/deepseek-harness/README.md:11)、[Windows 沙箱能力标记](D:/0715/git_codex/20260825/deepseek-harness/packages/sandbox/sandbox-local/src/index.ts:186)。
