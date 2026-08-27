# 真实业务状态机实现指南（AI 必读）

> 用途：以后接入真实订单、退款、库存、工单、部署等业务状态机时，AI 必须先完整阅读本文件。
> 需求来源：真实业务状态、事件和转换只填写在 `BUSINESS_REQUIREMENTS.md`，本文件只规定“怎样安全实现”，避免两份需求互相冲突。
> 安全：AI 不得自行发明生产状态、权限、审批、数据库字段、API 或补偿规则。

---

## 1. 最通俗的理解

业务状态机就是一张受控流程表：

```text
现在是什么状态
收到什么可信事件
是否允许变化
变化成什么状态
变化前后还要检查什么
```

例如：

```text
订单当前：paid
可信事件：shipment_created
下一个状态：shipped
```

用户或模型说“订单已经发货”只是文本，不是可信事件，不能直接改变状态。

---

## 2. AI 开始开发前必须读取

顺序：

```text
AGENTS.md
→ BUSINESS_REQUIREMENTS.md
→ TOOLS_IMPLEMENTATION_GUIDE.md（涉及工具时）
→ STATE_MACHINE_IMPLEMENTATION_GUIDE.md
→ 现有 domains/runtime/session/approval/writes 源码和测试
```

如果 `BUSINESS_REQUIREMENTS.md` 缺少状态转换表、事实来源、审批或恢复规则，AI 必须先提问。

---

## 3. 不要混淆五个概念

### State：状态

实体当前处于哪个稳定阶段：

```text
pending_payment
paid
shipped
cancelled
```

### Command：命令

用户或系统“希望做什么”：

```text
CancelOrder
ShipOrder
```

命令只是请求，不代表成功。

### Event：事件

已经真实发生的事实：

```text
order_cancelled
shipment_created
```

只有事件才能推进状态。

### Guard：转换条件

是否允许发生：

```text
当前状态必须是 paid
当前身份必须有 operator 角色
必须存在 Approval
expected_version 必须正确
```

### Action/Effect：外部动作

真正调用 API、数据库或消息系统：

```text
调用取消订单 API
创建物流单
发送通知
```

状态 Reducer 本身必须是纯函数，不能在 Reducer 里调用外部 API。

---

## 4. 通用 Runtime 状态和业务状态要分开

### 通用 Runtime 状态

项目框架已经提供：

```text
routing
requesting_model
executing_tools
retrying
waiting_approval
completed
failed
suspended
```

它描述 Agent 正在做什么。

### 业务 Domain 状态

由真实项目定义：

```text
订单 paid/shipped/cancelled
退款 requested/approved/refunded
部署 building/deploying/healthy
```

它描述业务实体是什么状态。

禁止把业务状态硬编码进 `loop.py`。

---

## 5. 业务人员需要在 BUSINESS_REQUIREMENTS.md 填什么

填写状态转换表：

| 实体 | Event | 允许来源状态 | 目标状态 | 可信事实来源 | 是否审批 | 幂等键 | 失败/核对方式 |
|---|---|---|---|---|---|---|---|
| `<entity>` | `<event>` | `<from>` | `<to>` | `<api/tool>` | 是/否 | `<rule>` | `<rule>` |

每个实体还要说明：

```text
实体 ID：例如 order_id
初始状态：例如 pending_payment
终态：例如 completed/cancelled
状态事实保存在哪里：数据库/API/事件日志
版本字段：例如 version
谁可以发出事件：工具/API/Worker/Approval Service
```

---

## 6. AI 必须先确认的问题

AI 在编码前逐项确认：

1. 业务实体是什么？
2. 实体唯一 ID 是什么？
3. 初始状态是什么？
4. 有哪些终态？
5. 每个事件允许从哪些状态发生？
6. 事件成功后的目标状态是什么？
7. 事实来源是否可信？
8. 是否需要 Approval？
9. 是否属于写操作？
10. Idempotency Key 从哪里来？
11. Timeout 后是否可能已经成功？
12. outcome_unknown 如何查询最终状态？
13. 是否需要补偿/回滚？
14. 并发更新如何检查版本？
15. 状态事件存在哪里？
16. 崩溃后如何恢复？

任何关键答案缺失时，只能实现 Mock、接口或 TODO，不能假装生产流程完整。

---

## 7. 推荐目录

每个真实 Domain 独立目录：

```text
src/pi_agent_loop/domains/<domain>/
├─ __init__.py
├─ states.py          状态 Enum/类型
├─ events.py          业务事件类型
├─ transitions.py     转换表
├─ reducer.py         纯状态归约
├─ policies.py        权限、审批、幂等规则
├─ repository.py      状态和事件存储接口
├─ service.py         Command → Tool/API → Event
├─ projection.py      UI/查询视图
└─ recovery.py        outcome_unknown/补偿/恢复
```

测试：

```text
tests/domains/test_<domain>_transitions.py
tests/domains/test_<domain>_service.py
tests/domains/test_<domain>_recovery.py
```

简单业务可以合并少量文件，但不能把业务转换塞入 Agent Loop。

---

## 8. 使用当前通用 DomainStateMachine

当前骨架：

```python
from pi_agent_loop import (
    DomainEvent,
    DomainStateMachine,
    DomainTransition,
)
```

定义：

```python
machine = DomainStateMachine(
    initial_state="pending_payment",
    transitions=[
        DomainTransition(
            event_type="payment_succeeded",
            from_states=frozenset({"pending_payment"}),
            to_state="paid",
            allowed_sources=frozenset({"payment_api"}),
        ),
        DomainTransition(
            event_type="cancel_approved",
            from_states=frozenset({"paid"}),
            to_state="cancelled",
            allowed_sources=frozenset({"cancel_api"}),
            requires_approval=True,
        ),
    ],
)
```

应用可信事件：

```python
next_state = machine.apply(
    current_state,
    DomainEvent(
        entity_id="order-1001",
        type="payment_succeeded",
        source="payment_api",
        expected_version=current_state.version,
    ),
)
```

该骨架自动检查：

- Entity ID；
- allowed from state；
- trusted source；
- Approval；
- expected_version；
-重复转换定义。

---

## 9. Command 不能直接改状态

错误做法：

```text
用户说“取消订单”
→ 直接 state=cancelled
```

正确做法：

```text
用户 Command：CancelOrder
→ Router 得到 Intent
→ 权限检查
→ Approval
→ WriteOperationService
→ cancel_order Tool/API
→ API 真实成功
→ 产生 order_cancelled Event
→ DomainStateMachine 转到 cancelled
```

如果 API Timeout：

```text
不能产生 order_cancelled
→ 写 operation 进入 outcome_unknown
→ 调用 Reconciliation API
→ 确认成功后才产生 order_cancelled
```

---

## 10. 可信事实来源

`allowed_sources` 必须指向受控 Adapter：

```text
payment_api
order_api
inventory_worker
approval_service
reconciliation_service
```

不应使用：

```text
user_message
assistant_text
model_reasoning
```

模型可以提出 Command 或 Intent，但不能宣布业务事实。

生产实现中，Source 字符串还要由可信 Adapter/Identity 绑定，不能只相信调用方传入的文本。

---

## 11. 可信身份和权限

使用：

```python
VerifiedIdentity
IdentityVerifier
```

生产环境替换：

```text
StaticIdentityVerifier
→ OAuth/OIDC/JWT/IAM/SSO Adapter
```

每个写 Command 检查：

```text
principal_id
roles/scopes
issuer
verification_id
业务资源权限
```

持久事件不保存 Credential、Token、Cookie 或密码。

---

## 12. Approval 状态机

写操作需要审批时使用：

```python
ApprovalService
```

状态：

```text
waiting
→ approved/rejected/expired
approved
→ consumed
```

要求：

- Approval 绑定精确 Action Hash；
-禁止默认自审；
-审批人必须有角色；
-有过期时间；
-只能消费一次；
-参数或 Tool 改变后旧 Approval 失效；
-批准后通过 Durable Operation 恢复原请求；
- `approval_resume_registered` 必须先于 Grant/Consume 持久化；
-恢复扫描必须覆盖 approved/consumed/started，不能只扫描 Started；
-恢复推进必须幂等，Completed 后不能再次执行 Resume；
-多 Worker 必须使用事务 Claim，避免同时恢复同一 Approval；
- Operation Reducer 必须正式归约 Approval/Write Event；
- Recovery Planner 必须先处理 waiting/approved/consumed/outcome_unknown，再处理普通 Tool Call；
- Waiting Approval 时禁止 Tool Dispatch；
- Never Tool 的布尔授权不能替代已消费 Approval 和匹配 Action Hash。

---

## 13. 写操作和幂等

使用：

```python
WriteOperationService
```

状态：

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

规则：

- Idempotency Key 原文不持久化，只保存 Hash；
-同 Key + 同 Action 返回原 Write；
-同 Key + 不同 Action 拒绝；
- succeeded 后重复请求不再次执行；
- outcome_unknown 不自动重放；
-核对成功后才产生业务成功 Event。

---

## 14. Replay Policy

每个 Tool 必须声明：

```python
replay_policy="safe"
```

或：

```python
replay_policy="never"
```

### safe

仅限只读或严格幂等工具：

```text
查询订单
读取库存
纯计算
```

### never

默认用于写操作：

```text
支付
退款
取消
发送消息
创建资源
```

恢复规则：

```text
Intent 已记录但未 Dispatch
→ 可以执行

Dispatch 后无 Result + safe
→ 可以重放

Dispatch 后无 Result + never
→ 必须 Reconcile
```

---

## 15. 完整 Session 和恢复

使用：

```python
DurableOperationRecorder
JsonlOperationEventStore / 生产数据库 Store
DurableSessionRecovery
RecoveryCallbacks
```

必须持久化：

```text
完整 User/Assistant/ToolResult Context
Model Request Started/Completed/Failed
Tool Intent/Dispatch/Result
Approval/Write Event
Operation Started/Finished
配置快照
```

恢复时：

```text
判断 Model Request 是否完成
→ 判断 Tool 是否 Dispatch
→ Safe Replay 或 Reconcile
→ 补写 ToolResult
→ 使用完整 Context 继续模型
```

Recovery Callback 必须通过正式 Provider/Tool Runtime，不能绕过 Guard、权限、Timeout 和 Approval。

每次 `model_request_started` 还必须保存该次请求的独立策略快照：

```text
visible_tool_names
tool_choice
required_capabilities
allowed_tool_names
expected_tool_arguments
continuation_policy
```

Recovery 必须按 Request/Turn 恢复该快照；缺少快照时进入 Manual Intervention，禁止使用“全部工具 + auto”兜底。Required/Named Tool 完成后的 Continuation Policy 必须在原请求前确定并持久化。

Approval 写操作完成后的说明性模型请求默认不暴露工具并使用 `tool_choice=none`。模型若仍返回 Tool Call，不得写 Completed；应结束 Model Request、标记 Operation Failed，并保持 Transcript 闭合。

---

## 16. 并发和 expected_version

所有状态更新都携带：

```text
entity_id
expected_version
```

如果当前版本不是 expected_version：

```text
stale_version
→ 重新读取实体
→ 重新判断 Command 是否仍可执行
```

禁止直接覆盖其他 Worker 已经更新的状态。

单机多进程默认使用 `SQLiteOperationEventStore`：

```text
BEGIN IMMEDIATE
Operation expected_last_sequence CAS
Approval/Write 唯一约束
Idempotency Key Hash 唯一约束
跨进程 Lease Claim
Event Batch Transaction
```

JSONL 只允许单实例兼容使用。跨机器部署仍需要 PostgreSQL 等网络数据库。

Approval Consume、Write Approved、Tool Dispatch Started 和 Write Submitting 必须作为同一批事务事件提交。外部 HTTP/数据库副作用不能放在长 SQLite 事务中，应先提交 Claim，再调用外部系统，最后写 Succeeded/Failed/OutcomeUnknown。

---

## 17. Reducer 必须是纯函数

推荐：

```python
next_state = reduce(current_state, domain_event)
```

Reducer 只能：

-验证转换；
-返回新状态；
-更新版本；
-记录 last_event。

Reducer 不能：

-调用 HTTP；
-写数据库；
-调用大模型；
-发送消息；
-读当前时间决定业务结果；
-执行随机逻辑。

外部副作用由 Service/Tool 完成，成功后产生 Event。

---

## 18. 持久事件要求

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

禁止包含：

```text
API Key
Password
Authorization Header
Cookie
明文 Idempotency Key
不必要的隐私数据
```

生产存储必须加密、鉴权并设置保留周期。

单机事务 Event Store 必须支持：

```text
append_batch(expected_last_sequence=...)
try_acquire_claim(...)
release_claim(...)
```

状态转换必须先纯函数预验证，再通过 CAS 提交；冲突后重新读取状态，不能继续使用旧快照。

---

## 19. Snapshot 和 Migration

事件较多后使用：

```text
Snapshot
+ Snapshot 之后的 Event
```

必须规划：

- Event Schema Version；
- State Snapshot Version；
-旧版本迁移；
-日志损坏检测；
-校验和；
-备份和恢复。

不能修改历史事件的原始含义；需要新版本事件或迁移器。

---

## 20. 终态和可恢复状态

必须明确：

### 终态

```text
completed
cancelled
refunded
rejected
```

终态是否允许重新打开必须明确配置。

### 可恢复状态

```text
suspended
outcome_unknown
waiting_approval
retry_backoff
```

必须说明恢复事件和超时策略。

### Manual Intervention

无法安全自动恢复时进入：

```text
manual_intervention
```

禁止为了“自动完成”而猜测结果。

---

## 21. 错误和补偿

区分：

```text
技术瞬时错误
→ Retry Policy

业务校验错误
→ 不重试

结果不确定
→ Reconcile

部分成功
→ 根据业务定义 Compensation
```

如果需要补偿，必须由业务明确填写：

```text
compensation_requested
compensating
compensated
compensation_failed
```

AI 不得自行发明补偿动作。

---

## 22. Projection

状态机内部状态与 UI 文案分离：

```text
内部：waiting_approval
UI：等待审批
```

Projection 可以提供：

```text
当前状态
允许操作
阻止原因
审批信息
最近事件
版本号
恢复建议
```

UI 不应直接修改状态，只能发送 Command。

---

## 23. 必须测试的情况

每个 Domain 至少测试：

### 正常转换

```text
initial → event → target
```

### 非法来源状态

```text
错误 from state → 拒绝
```

### 不可信来源

```text
user_message/model_text → 拒绝
```

### Approval

```text
无审批 → 拒绝
审批通过 → 成功
审批重复消费 → 拒绝
审批操作不匹配 → 拒绝
```

### 并发

```text
expected_version 过期 → stale_version
```

### 幂等

```text
同 Key 同 Action → 返回原结果
同 Key 不同 Action → conflict
```

### 崩溃恢复

```text
Model Request 未完成
Tool 未 Dispatch
Safe Tool 已 Dispatch
Never Tool 已 Dispatch
Result 已有但 Message 缺失
```

### outcome_unknown

```text
不重放写操作
→ Reconcile
→ 成功/失败/仍未知
```

### 终态

```text
终态后非法事件 → 拒绝
```

---

## 24. AI 推荐实现步骤

```text
1. 读取业务状态表
2. 列出缺失问题
3. 定义 State/Event
4. 定义 Transition Table
5. 定义 Trusted Source
6. 定义 Command 和 Guard
7. 定义 Approval/Identity
8. 定义 Idempotency/Replay Policy
9. 定义 outcome_unknown/Reconcile
10. 实现纯 Reducer
11. 实现 Service/Tool 副作用边界
12. 接入 Durable Operation Event
13. 实现 Projection
14. 编写正常和非法转换测试
15. 编写崩溃恢复测试
16. 运行全部测试
17. 更新 README 和 BUSINESS_REQUIREMENTS
```

---

## 25. AI 生成文件清单

未来 AI 完成一个真实 Domain 后，应输出：

```text
新增的 State
新增的 Event
Transition Table
Trusted Source
Approval Rule
Idempotency Rule
Replay Policy
Recovery Rule
Projection
Store/Repository Adapter
Tool/Capability 映射
测试和结果
仍缺少的真实接口信息
安全风险
```

---

## 26. 常见错误设计

禁止：

```text
模型文本直接改业务状态
Command 当成成功 Event
Reducer 内调用外部 API
所有状态都允许互相转换
写工具 replay_policy=safe 但没有幂等保证
只用 approved=True
明文持久化 Idempotency Key
Timeout 后直接重试写操作
没有 expected_version
终态后继续写事件
为了自动恢复而猜测结果
```

---

## 27. Definition of Done

只有全部满足才算业务状态机完成：

- [ ] 业务状态表已由业务人员确认；
- [ ] 初始状态和终态明确；
- [ ] State/Event 使用稳定名称；
- [ ] Command 与 Event 分离；
- [ ] Transition Table 完整；
- [ ] Trusted Source 明确；
- [ ] Reducer 是纯函数；
- [ ] 非法转换会拒绝；
- [ ] VerifiedIdentity 已接入；
- [ ] Approval 与 Action Hash 绑定；
- [ ] Idempotency Key 规则明确；
- [ ] replay_policy 已评估；
- [ ] outcome_unknown 有 Reconciliation；
- [ ] expected_version 已检查；
- [ ] Durable Event 已接入；
- [ ] 完整 Context 可恢复；
- [ ] Projection 已实现；
- [ ] 正常、非法、并发、审批、恢复测试通过；
- [ ] 敏感信息未进入日志；
- [ ] 文档已更新；
- [ ] 全部项目测试通过。

---

## 28. 当前项目骨架对应关系

```text
通用业务转换
→ DomainStateMachine

运行状态
→ RuntimeStateTracker + Reducer

完整消息和执行事实
→ DurableOperationRecorder

恢复计划和执行
→ DurableSessionRecovery

可信身份
→ IdentityVerifier + VerifiedIdentity

审批
→ ApprovalService

幂等写操作
→ WriteOperationService

工具恢复
→ replay_policy safe/never

模型请求恢复
→ ModelRequestPolicy（含 Continuation Policy）

单机事务持久化
→ SQLiteOperationEventStore + SQLiteRuntimeEventStore
```

真实业务只需要在这些骨架上提供状态表、Adapter、Tool 和 Policy，不应重新实现另一套 Agent Loop。
