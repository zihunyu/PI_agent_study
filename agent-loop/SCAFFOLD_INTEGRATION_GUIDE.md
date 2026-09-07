# 脚手架接入真实项目指南

本目录只维护领域无关的 Agent/Harness 能力。真实项目代码应位于独立仓库或独立
Python 包，通过公开接口注入，避免业务实现反向污染框架。

## 一、业务包负责什么

业务包负责：

- Tool 实现和严格参数校验；
- Capability 与角色权限；
- Router/Planner 的 Intent 配置；
- 登录、身份验证和租户绑定；
- HTTP/数据库 Adapter；
- 业务状态机、幂等键和核对接口；
- 业务配置、Mock、集成测试和部署文档。

这些内容不应加入 `pi_agent_loop.__init__`，也不应由框架 import。业务入口应显式
导入自己的包，例如：

```python
from my_business.tools import create_tool_bundle
from my_business.routing import create_router
from pi_agent_loop import DurableAgentWorkspace
```

## 二、脚手架负责什么

脚手架负责：

- Agent Loop、流式事件与 Tool Call 协议闭合；
- Tool 批量预检、调度、取消、Timeout、资源锁和 Retry；
- Project/Session 上下文、事务 Journal、Snapshot 和 Migration；
- Approval、Write、Outcome Unknown 和 Recovery；
- Model/Tool Runtime、Telemetry、Compaction 和 PlanExecutor；
- TaskDecision、Durable Plan、执行结果校验和有限安全纠正；
- 多 Worker Claim、Heartbeat、Fencing Token 与故障接管；
- 通用身份、Capability、Provider 与 Store 扩展接口。

## 三、最小接入顺序

1. 在业务包实现 Tool，并给写 Tool 设置 `replay_policy="never"`。
2. 使用 `CapabilityRegistry` 注册角色、读写类型和 `requires_approval`。
3. 实现 Router 或加载业务 Intent 配置；需要身份权限时注入
   `authorization_policy`。
4. 把可信身份放在 Tool 闭包/执行上下文中，不接受模型传入身份。
5. 使用 `DurableAgentWorkspace` 创建 Project 和 Session。
6. 将业务 `tools`、`router`、`capabilities` 和 `stream_fn` 传给 Host。
7. 分布式部署时通过 `DurableHostResourceFactory` 注入事务 Store。
8. 为自主任务设置 Step/Tool/Model/Token/Cost/Duration 硬预算；启用
   Model/Token/Cost 上限时注入可预留、可结算的可信 Usage Meter。
9. 按数据分类决定是否装配内容安全与语义记忆；两者都必须显式注入，核心包不会
   自动选择企业审核服务、Embedding Provider、KMS 或数据保留政策。
10. 使用 Mock 完成权限、审批、崩溃恢复和并发测试后再真实联调。

### 3.1 身份和审批不是字段校验

`StaticIdentityVerifier` 只用于本地开发。生产 OIDC/JWT/IAM/SSO Adapter 必须在 Host
信任边界验证真实凭证，并给 `ApprovalService` 或 `DurableAgentHost` 注入
`identity_validator`/`approval_identity_validator`。直接构造 `VerifiedIdentity`、只检查
issuer/role 字符串、信任模型输出或从持久事件恢复身份字段都不是验证。validator 应绑定
签名证明、可信登录会话或服务端认证记录；认证通过以后，Authorization 和精确 Action
Approval 仍是独立检查。

### 3.2 内容安全需要真实策略 Adapter

只有显式传入 `Agent(content_safety=ContentSafetyPipeline(...))` 才会检查 `model_input`、
`model_output` 和 `tool_output`。策略异常、超时或无效决策会 fail closed；没有管线时则
没有内置审核。`UntrustedToolOutputPolicy` 只是把工具文本标成不可信数据，不能替代
企业审核、Prompt Injection 隔离、Tool Authorization 或 Approval。审计 sink 应只接收
安全元数据，业务策略也不得把原始敏感内容塞进 reason/code。

启用安全管线时，模型 partial `message_update` 会缓冲到 terminal Message 完成并通过
`model_output` 检查；失败或阻止时不会向 listener 暴露缓冲内容，也不会派发 Tool。
这是明确的安全/体验取舍：安全模式没有实时逐 token 展示；不配置管线时保持普通流式
行为。Policy 必须使用可取消的 `async def inspect`，网络 Adapter 应使用可取消
异步 I/O 或自行设置硬 deadline。共享管线对同一策略的正常检查排队执行，排队和执行
共同消耗 `policy_timeout_seconds`；超时后仍拒绝退出的旧策略任务会使后续检查失败关闭。
取消排队检查不会取消其他请求的在途审核，也不会留下占用的策略锁。

工具结果在交给 `after_tool_call` 前先过一次 `tool_output` 检查，因此 hook 只接收安全
快照；hook 的 override 必须再次通过同一边界才能进入模型。业务 hook 不能借改写绕过
策略，policy 也应支持确定、可重复的复审，并把额外策略调用计入 deadline/容量规划。

### 3.3 澄清状态和语义记忆分别装配

两轮缺参续接使用 `ClarificationStateStore`，严格绑定可信的 tenant/session scope，
只保存一个 pending Intent、字段和 TTL。内置 Store 是进程内实现；跨重启应用需提供
事务化 `load/save/clear` Adapter。启用 Store 却缺少可信 scope 或 Store 不可用时，
Router 会保持澄清态并停止执行。

长期记忆使用独立 `MemoryManager` 和 `MemoryScope(tenant_id, subject_id, namespace)`。
它不会自动接入 Agent。`SQLiteMemoryStore` 加密 record payload，但 scope、memory ID、
时间戳和 key ID 是索引/AAD 明文字段；部署方仍需文件权限、备份保护和 KMS。内置
`HashingEmbeddingProvider` 只适合离线测试，生产质量需要真实 Embedding Provider。
把检索结果写入 prompt 必须在 Provider 构造和每次调用两次 opt-in；敏感记忆另需独立
开关。业务包还要自行实现同意、分类、保留、删除请求和合规审计。

### 3.4 多模型与多 Agent 的边界

`ResilientModelRouter` 的 capability、质量、价格和延迟都是可信配置，不是动态模型目录
或账单事实；内置健康状态是进程内状态。它只在首个可见输出前对结构化 retryable 错误
故障转移，一旦发布文本或 Tool Call 就不会换模型。跨进程熔断、全局配额、动态价格和
Provider SLA 需要外部控制面，模型选择也不能替代 Authorization/Approval。

`MultiAgentOrchestrator` 适合单进程可信 Worker DAG：Worker 注册由 Host 创建，消息、
结果、并发、调用次数和 deadline 有界；固定 Agent Adapter 会串行并清空 transcript。
它的 run state 和 resource lock 是进程内实现，不是 Durable Worker 集群。多机任务应
使用共享的 Durable Plan/Worker Store、Claim、Heartbeat、Fencing 和分布式资源锁；
自定义 Worker 必须合作传播取消，框架不能强杀阻塞同步代码或恶意吞取消的协程。

复杂任务还需要注册：

```python
host = await DurableAgentHost.create(
    ...,
    tools=business_tools,
    router=router,
    capabilities=capabilities,
    planner=planner,
    plan_policies=trusted_plan_policies,
    # Intent 到 Tool 的映射来自业务配置，不由模型决定。
    plan_tool_bindings={
        "records.read": "read_record",
        "records.update": "update_record",
    },
    tool_identity=verified_identity,
    plan_result_validator=validate_business_result,
    plan_replanner=replan_safe_reads,
    plan_result_synthesizer=synthesize_result,
    plan_correction_budget=ClosedLoopBudget(
        max_correction_rounds=2,
        max_correction_actions=4,
    ),
)
```

Router 必须为复杂请求返回结构化 `TaskDecision`。Planner 只能使用 Router 给出的
Intent、参数和依赖作为候选输入，最终的写入、审批、重放、Capability 与审批角色
必须来自 `trusted_plan_policies`。Policy 应为危险 Intent 声明参数合同，并可使用
可信依赖、受限 JSON Path 参数绑定、前置条件和结果合同。不要在 Replanner 中自动
重复写操作；只有纯读取且 `replay_policy="safe"` 的纠正计划可以自动执行。

低层 `plan_step_executor` 只保留给纯读取、本地组合场景。写操作、审批操作和
`replay_policy="never"` 的 Plan 必须通过 `plan_tool_bindings` 进入统一
`ToolDispatchRuntime`，这样身份、参数 Schema、Timeout、Hook、资源锁、审批回执和
Fencing Context 才不会被绕开。

Replay Policy 必须按最严格来源合并。Tool Registry、可信 Intent Policy、Workflow
和 Plan Step 任意一层声明 `never`，最终策略就是 `never`；业务 Router、模型输出、
调用参数或 Recovery Callback 不得把它放宽成 `safe`。Plan 写 Step 还必须复用完整的
Approval + `WriteOperationService` 管线，不得回退到裸 Callback。Timeout、取消或落盘
不确定时保持 `submitting/outcome_unknown`，先调用业务核对接口 Reconcile，只有明确
的 `succeeded/failed` 事实才能收敛终态。

## 四、Store Adapter 合同

自定义资源工厂接收 `DurableResourceRequest`，返回 `DurableHostResources`：

```python
async def create_resources(request):
    return DurableHostResources(
        root=request.state_dir,
        operation_store=operation_store,
        runtime_store=runtime_store,
        retry_store=retry_store,
        # 使用 DurableAgentWorkspace、managed_session 或 Durable Plan 时，
        # 还必须提供实现统一 Session Journal 合同的 Adapter 和可信 Principal。
        journal=session_journal,
        journal_principal=journal_principal,
        owned_resources=[database_pool],
    )
```

只运行非受管的低层 Host 时，可以不提供 `journal`。通过
`DurableAgentWorkspace` 创建 Project/Session，或启用持久 Plan 时，资源工厂必须同时
返回兼容 `SQLiteSessionEventJournal` 公开方法的统一 Journal Adapter 与
`JournalPrincipal`；Catalog、Context Projection、Plan Store 和 Session Migration
都会使用这条边界。缺少它们时 Host 会失败关闭，而不是退化为未隔离的本地状态。

生产 Operation Store 必须支持事务 Batch、expected sequence CAS、唯一约束和跨进程
Lease Claim，并声明 `supports_cross_process_claims = True`。Runtime Store 还必须按
`tenant_id + session_id` 分区自己的 sequence、CAS 和重放范围，不能让两个 Session
共享一个未分区的全局 Runtime stream。JSONL 只用于底层单进程或离线迁移兼容；
`DurableAgentHost` 会直接拒绝 JSONL Backend，不能把它作为 Session Writer 或
Recovery Store。底层兼容使用时，一个 JSONL 文件也只能对应一个 Runtime stream，
不同 Session 必须使用不同文件并由调用方保证单进程读写。

内置旧 `sqlite` 兼容后端把 `runtime_events` 按 `session_id` 分区。旧数据库若存在
没有 `session_id` 的 Runtime 事件会 fail-closed；只有迁移人确认其唯一归属后，才可
调用 `migrate_legacy_sqlite_runtime_events(..., session_id=...)` 显式迁移。生产新项目
仍应优先使用统一、加密的 `journal` 后端。

多机 Adapter 还必须保证 Claim generation 单调递增，并原子实现 acquire、renew、
verify、release。业务侧的 Plan Step、Tool 和写 Handler 应接收框架传入的
`fencing_token`，在外部数据库更新中把它作为版本/Fence 条件；只在框架内检查
Lease、却让下游无条件接受旧 Worker 写入，并不能形成真正的分布式安全。
Tool Context 同时提供 `fencing_scope`，数据库必须在同一 scope 内比较 token。

`workflow` Claim、Run Controller Claim 和资源锁 Claim 是不同的 Fence Scope，不能
因为 generation 数字相同就互相替代。每次状态提交都要在 Store 事务中校验精确的
`resource_id + owner + generation`；每次真实副作用都要把相应的 `fencing_scope` 和
`fencing_token` 传给下游，并由下游更新语句原子拒绝旧 generation。Heartbeat 只能
说明 Worker 仍活着，不能代替提交时的 Fence 校验。

持久 Plan 后端应实现 `DurablePlanStore` 协议，并用
`DurablePlanStoreCapabilities` 如实声明：

- `atomic_fenced_append`：Lease generation 校验/续租、Plan Head CAS 和
  Event Append 在同一事务内完成；
- `supports_cross_process`：是否支持同一主机多进程；
- `supports_multi_host`：是否真正支持多台主机共享协调。

SQLite 内置 Store 只声明“单机多进程”。多机部署必须显式传入
caller-owned `plan_store`，并设置 `distributed_execution=True`；Host 会校验
tenant/session 作用域、原子 fencing 和多机能力，且不会代替调用方关闭
该 Store。危险 Plan 还必须使用 `plan_tool_bindings` 进入
`ToolDispatchRuntime`，并注入声明多机能力的 `resource_lock_backend`；
直接 Step Callback、本地锁或 SQLite 资源锁都会被严格模式拒绝。

### 4.1 Autonomous 原子边界与 Completion Outbox

受管 Autonomous 模式必须在一个事务中提交以下启动事实：

```text
Plan initialized
+ Run initial-plan-bound
+ Conversation Operation/Link
+ Run dispatchable
```

事务使用精确覆盖所有相关 Stream 的 expected-sequence CAS；Correction Plan 的
初始化和 Run 注册也必须同事务提交。任何一步冲突或失败都应全部回滚，Worker 只扫描
`linked + dispatchable` 的 Plan。当前内置 Host 只有在 Plan、Run、Conversation 共享
同一 Session Journal，并使用 `SessionJournalPlanStore` 时提供这条边界。普通自定义
`DurablePlanStore` 的 `atomic_fenced_append` 只声明单 Plan Stream 的 Fenced Append，
不等于三流 Bootstrap；受管 Host 会在装配 Autonomous Runner 时直接拒绝不兼容共享
Journal 边界的 Store，直接组合底层组件时也必须在 Bootstrap/派发前 fail-closed。
自定义后端若要接入完整 Autonomous
路径，必须实现与当前 Session Journal 路径兼容的等价事务，不能用事后补偿伪装原子
提交。同 Run 的并发 Begin 还必须使用稳定 Operation 身份；CAS 失败方只能重载并核对
同一 Request/Plan，不能另建随机 Operation 或留下孤立 Plan。

Plan 进入 `waiting_approval` 或终态时，状态变化和 `completion_pending` Envelope
必须同事务写入。Envelope 包含稳定 `delivery_id`、generation、目标阶段、Plan 状态
版本和摘要。Consumer 通过 fenced Claim 至少一次处理，且必须接收 Envelope、以
`delivery_id` 做幂等消费；只有精确匹配 Envelope 的 `completion_ack` 才能清除 Pending。
内置 Projector 对 Waiting 使用 `delivery_id`，对 Final 使用稳定 `run_id + final` 和
CAS。它会在 Waiting Conversation 投影提交后才 Ack。自定义 Handler 只会被框架检查
是否接收 Envelope；它必须自行持久化 Waiting 通知并按 `delivery_id` 去重，框架不能
验证任意业务投影已经提交。因此这里承诺的是 **at-least-once + 幂等消费合同**，并不
承诺任意外部 Handler 天然 exactly-once。投影成功但 Ack 丢失会重新投递，旧
generation 的 Ack 不得清除新 Pending。

### 4.2 Autonomous 持久硬预算

预算至少覆盖 `plan_steps`、实际 `step_attempts`、实际 `tool_calls`、全部
`model_calls`、Token、Cost 和墙钟 Duration。Planner、Validator、Replanner、
Synthesizer 均计入 Model/Token/Cost/Duration，不能只统计最终 Agent Loop。

当前实现按资源类型处理：Plan Step 数在绑定 Plan 时持久预扣；Step/Tool 离散尝试在
每次派发前按一次精确预扣，不再 Settle；Duration 由持久墙钟 Deadline 截止；只有
Model/Token/Cost 使用 `plan_usage_meter` 的 `reserve → dispatch → settle`；
`model_calls` 统计失败重试在内的物理 Provider Attempt，Token/Cost 累计整个 Retry
Tree。进程在 Dispatch 后、Settle 前崩溃时，Reservation 保守
保留；只有能够持久证明没有派发时才可释放。预算状态跨重启、接管和 Correction Plan
延续，任一上限不足都必须在外部调用前拒绝。Model/Token/Cost 缺少 Meter 时配置会
fail-closed；Meter 还必须对每个物理 Provider Attempt 实现 Admission `dispatch`。
Attempt Scope 可阻止后续重试并核对实际累计值，但第一个请求的单次 Token/Cost 上界
仍需由受信 Meter/Provider 参数在调用前限制。

## 五、Provider Adapter 合同

Harness 的模型边界是 `stream_fn(model, context, options)`，并不要求
OpenAI Chat Completions。自定义 Provider 如需由 Host 关闭，应显式提供：

```python
class MyProvider:
    manage_with_host = True

    def stream(self, model, context, options):
        ...

    async def aclose(self):
        ...
```

OpenAI-compatible 配置和 `/chat/completions` 只是内置 Adapter，不是 Harness 核心限制。

## 六、允许回迁到脚手架的修改

可以回迁：

- 与领域无关的安全边界和并发修复；
- 通用 Tool/Provider/Store/Router 接口；
- 通用 Approval、Recovery、Compaction 和 Telemetry；
- 不引用业务名词的回归测试。

不能回迁：

- 真实 API URL、账号、角色、Token 或数据库结构；
- 某个项目的 Tool/Router/Intent/初始数据；
- 业务专用 System Prompt、配置和操作文档；
- 只能由某个业务异常类型触发的框架分支。

判断标准：删除业务适配包后，核心导入、完整通用测试和公开接口仍应正常工作。
