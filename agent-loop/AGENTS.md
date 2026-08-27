# Agent Loop 项目开发规则

## 业务功能、工具和配置

当任务涉及以下任一内容时，AI 必须先完整阅读项目根目录的 `BUSINESS_REQUIREMENTS.md`：

- 新增或修改真实业务功能；
- 新增或修改 AgentTool；
- 新增 Domain、Intent 或 Capability；
- 生成或修改 `config/business.toml.example`；
- 接入真实 API、数据库或写操作；
- 修改 Router、Policy、Approval 或 Guard。

`BUSINESS_REQUIREMENTS.md` 是业务需求的唯一入口。若其中的信息不足，AI 必须先向用户提问，不得自行编造 API、数据库字段、权限、幂等规则或业务成功结果。

新增、修改或删除任何 `AgentTool` 时，还必须完整阅读 `TOOLS_IMPLEMENTATION_GUIDE.md`，按其中的 Schema、校验、取消、Timeout、结果、注册、测试和 Definition of Done 执行。

新增或修改业务 State、Event、Transition、Reducer、Approval、WriteOperation 或 Recovery 时，必须完整阅读 `STATE_MACHINE_IMPLEMENTATION_GUIDE.md`，并以 `BUSINESS_REQUIREMENTS.md` 中经业务确认的状态转换表为唯一需求来源。

## 配置和秘密

- 真实 `config/*.toml` 不得提交；
- 只能提交脱敏的 `config/*.toml.example`；
- 禁止在代码、文档、日志、测试和回答中输出真实 API Key；
- 生成配置后必须调用对应 Loader 做严格校验；
- 业务配置变化必须增加 Router、Capability 和 Guard 测试。

## 分层

- `loop.py` 只保留低层 Agent Loop 职责；
- 自然语言业务分类放在 `routing/`；
- 外部操作放在 `tools/`；
- Capability 与具体工具名分离；
- 实时数据和写操作不得仅依赖 `tool_choice="auto"`；
- 写操作在 Approval 完成前不得执行；
- 通用状态 Reducer/Invariant 放在 `runtime/`，持久化放在 `session/`；
- 具体业务状态转换放在 `domains/`，不得写入 `loop.py`；
- 用户或模型文本不是业务状态事实，状态必须来自工具、业务 API、审批或持久事件；
- 新增状态和转换时必须更新 `BUSINESS_REQUIREMENTS.md` 并增加非法转换测试；
- 可恢复工具必须声明 `replay_policy`，写工具默认 `never`；
- Approval 必须绑定可信身份和精确 Action Hash，禁止仅使用布尔 `approved=True`；
- 写操作必须使用 Idempotency Key Hash、持久事件和 outcome_unknown 核对；
- Recovery Callback 是 Host 信任边界，不能绕过 Tool Guard、权限或审批；
- 新工具必须在 parallel/exclusive/resource_locked 中选择执行策略；
- resource_locked 必须使用已校验参数生成稳定 Resource Key，禁止在 Key 中包含密钥；
- 写工具不得因为全局 parallel 而省略执行策略、Approval、Idempotency 和 expected_version；
- 任何并行调度改动都必须验证外层取消会清理嵌套 Execute/Update/Timer/Waiter Task；
- `CancellationToken.detach()` 必须位于不可跳过的 finally，清理错误不得覆盖主要错误；
- 每个已提交 Assistant Tool Call 在下一条 User/Assistant 前必须有且只有一个 ToolResult；
- 取消、跳过和 Scheduler 异常必须生成 Synthetic Error ToolResult，不能留下未闭合历史；
- Provider 序列化前必须执行 Transcript Closure 校验；
- Approval Resume 必须以 `approval_resume_registered` 为恢复锚点，不得只扫描 Started；
- waiting/approved/consumed/started/completed 必须按当前状态幂等推进；
- Approved 恢复必须解析可信 Consumer，禁止伪造身份；
- Completed Resume 不得再次执行回调，跨进程 Claim 必须依赖事务 Store；
- Operation Reducer 必须消费 Approval/Write Event，不能用“忽略后在 Startup 特判”代替正式状态；
- Recovery Planner 必须先检查 Approval/Write，再规划普通 Model/Tool 恢复；
- Waiting Approval 禁止 Tool Dispatch，Never Tool 的布尔授权不能替代 Consumed Approval；
- Approval Action Hash、Tool Name/Arguments、Write ID 和 Tool Call ID 必须一致。
- 每次 Model Request 必须持久化独立策略快照：可见工具、tool_choice、Capability、允许工具和预期参数。
- Recovery 缺少请求策略时必须进入 Manual Intervention，禁止回退到“全部工具 + auto”。
- Required/Named Tool 完成后的 Continuation Policy 必须预先持久化，不能在恢复时猜测。
- Approval 写操作完成后的模型请求默认 tools=[]、tool_choice=none；若模型仍返回 Tool Call，Operation 必须失败且日志保持闭合。
- Operation Finished 必须先经过 Reducer/Transcript Closure 预验证，再使用 expected version 条件追加。
- 单机多进程默认使用 SQLite Transaction Store；JSONL 只保留为单实例兼容实现。
- Approval/Write 的读取、状态检查和 Event 追加必须使用 CAS/事务，禁止裸露的 read-check-append。
- Approval Consume、Write Claim、Tool Dispatch Intent 必须在同一 Store Transaction 中提交。
- 外部写操作不能包在长数据库事务中；必须先持久 Claim，再依赖 Idempotency Key 和 Reconciliation 完成。
