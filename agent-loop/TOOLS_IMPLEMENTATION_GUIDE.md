# AgentTool 完整实现与注册指南（AI 必读）

> 适用范围：新增、修改、删除任何工具，以及把工具接入通用 Agent 或业务 Capability。
> AI 规则：开始工具开发前，必须同时阅读本文件、`AGENTS.md` 和 `BUSINESS_REQUIREMENTS.md`。

---

## 1. 什么叫一个“完整工具”

一个完整工具不只是一个 Python 函数，至少包含：

```text
业务目的
稳定工具名
模型可读说明
严格 JSON Schema
运行时参数校验
异步 execute
CancellationToken
进度 update
独立 Timeout
崩溃恢复 replay_policy
结构化 Tool Result
错误和敏感信息处理
注册与公开导出
Capability 映射（业务工具）
单元测试
Agent 集成测试
中文文档
```

缺少其中关键部分时，工具只能算实验函数，不能算可复用 AgentTool。

---

## 2. Tool、Capability、Intent 的区别

```text
Intent
用户想完成的业务目标，例如 order.get_status

Capability
系统稳定业务能力，例如 orders.read_current

Tool
Capability 的具体 Python 实现，例如 get_order_status

Tool Call
模型某一次具体调用，例如 get_order_status(order_id="1001")
```

原则：

- Intent 不应依赖具体函数名；
- Capability 与工具实现分离；
- 替换工具实现时，Intent 和业务配置尽量不变；
- 工具名是模型协议的一部分，发布后不要随意改名。

---

## 3. 文件放在哪里

先区分工具归属。只有领域无关的内置能力或明确标注、可删除的虚构教学 Tool 才能放在
脚手架核心，例如：

```text
src/pi_agent_loop/tools/<tool_name>.py
src/pi_agent_loop/tools/add.py
src/pi_agent_loop/tools/multiply.py
src/pi_agent_loop/tools/divide.py
```

真实项目的 Tool、校验器和工厂必须放在独立业务仓库或独立 Python 包：

```text
my_business/tools/<tool_name>.py
my_business/tools/validators.py
my_business/tools/__init__.py
```

真实业务测试也跟随业务包：

```text
my_business/tests/test_<domain>_tools.py
```

业务入口显式导入并注册，不允许把真实业务模块写入或反向导入到
`pi_agent_loop.__init__`：

```python
from my_business.tools import create_tool_bundle
from pi_agent_loop import ToolRegistry
```

---

## 4. 命名规则

### 工具名

使用稳定英文 snake_case：

```text
add
divide
get_order_status
cancel_order
```

禁止：

```text
tool1
do_work
万能查询
临时工具
```

### Label

Label 面向中文 UI：

```python
label="查询订单状态"
```

### Description

Description 面向模型，必须说明：

```text
工具做什么
什么时候调用
数据是否实时
关键参数含义
重要限制
```

不要写模糊描述：

```text
处理订单
执行操作
查询数据
```

---

## 5. JSON Schema 要求

Schema 是给模型看的调用合同。

示例：

```python
parameters={
    "type": "object",
    "properties": {
        "order_id": {
            "type": "string",
            "description": "需要查询的订单号",
        }
    },
    "required": ["order_id"],
    "additionalProperties": False,
}
```

必须：

- 顶层是 object；
- 每个字段有类型；
- 关键字段有 description；
- 必要字段列入 required；
- 默认使用 `additionalProperties=False`；
- 枚举使用 enum；
- 数值写明范围；
- 不要让模型传入 API Key、数据库连接或内部权限对象。

Schema 只是提示和协议，不能代替 Python 运行时校验。

---

## 6. 运行时参数校验

每个工具必须提供：

```python
validate_args(arguments)
```

校验内容包括：

- 参数是否是 dict；
- 必要字段是否存在；
- 拒绝 bool 冒充 int；
- 字符串是否为空；
- 数值是否有限；
- ID 格式和长度；
- 枚举是否合法；
- 路径是否越界；
- 除数是否为零；
- 是否存在未知字段；
- 敏感参数是否禁止进入日志。

校验失败使用明确的 `ValueError`，Agent Loop 会转换成错误 Tool Result。

不要在 execute 内部才开始猜测或补造关键参数。

---

## 7. execute 标准签名

```python
async def execute(
    tool_call_id,
    arguments,
    cancellation,
    on_update,
) -> AgentToolResult:
    ...
```

参数含义：

```text
tool_call_id
模型本次工具调用的唯一 ID

arguments
已经通过 validate_args 的参数

cancellation
Agent 或工具 Timeout 的取消令牌

on_update
向 UI 报告中间进度，不是最终结果
```

工具必须异步；阻塞式 SDK 应放入线程或子进程，不能阻塞事件循环。

---

## 8. CancellationToken

工具至少在以下位置检查取消：

```python
cancellation.throw_if_cancelled()
```

建议：

```text
开始执行前
长步骤之间
外部请求返回后
提交副作用前
循环内部
```

长时间等待应使用可取消的异步 API。

完全阻塞或不响应取消的代码需要子进程隔离。

Agent Runtime 会在 Scheduler/Listener 异常时取消并等待嵌套 `execute_task`，但工具仍必须正确传播 `asyncio.CancelledError`，不能故意吞掉取消。

---

## 9. 进度更新

使用：

```python
on_update(
    AgentToolResult(
        content=[{"type": "text", "text": "正在查询订单……"}],
        details={"phase": "querying"},
    )
)
```

规则：

- Update 只表示进度；
- 最终结果必须通过 return；
- 不要在 Update 中泄露密钥；
- 高频进度要节流；
- Timeout 或取消后的迟到 Update 会被 Agent Loop 忽略。

---

## 10. 独立 Timeout

每个工具必须评估自己的最大执行时间：

```python
timeout_seconds=5
```

原则：

- 只读内存计算可以较短；
- HTTP/数据库查询根据服务 SLA；
- 写操作要考虑事务和幂等；
- Timeout 不是模拟延时；
- Timeout 后要取消底层请求并释放资源。

工具自身 Timeout 优先于 Agent 默认 Timeout。

### 10.1 执行策略

每个工具必须评估：

```text
parallel
纯计算或互不影响的只读操作

exclusive
需要形成全局屏障，执行时不能与同批其他工具重叠

resource_locked
只锁定具体业务资源；相同资源串行，不同资源可并行
```

`resource_locked` 必须提供：

```python
resolve_resource_keys=lambda args: f"order:{args['order_id']}"
```

规则：

- Resource Key 使用已校验参数生成；
- 使用稳定前缀，例如 `order:1001`、`file:/path`；
- 不得包含 API Key、Token 或不必要隐私；
- 多个 Key 会按稳定顺序加锁，避免死锁；
- Retry Backoff 期间释放锁；
- 写工具必须结合 Approval、Idempotency 和 expected_version；
- `sequential` 仅保留为 `exclusive` 的向后兼容别名，新工具不要再使用。

模型不能决定并发安全；`execution_mode` 和资源键由 Host/工具作者定义。

---

## 11. 最终结果

成功结果：

```python
return AgentToolResult(
    content=[
        {"type": "text", "text": "订单 1001 当前状态：已发货"}
    ],
    details={
        "operation": "get_order_status",
        "orderId": "1001",
        "status": "shipped",
        "source": "order_api",
        "toolCallId": tool_call_id,
    },
)
```

要求：

- content 给模型和用户阅读；
- details 给程序、审计和测试使用；
- details 使用结构化字段；
- 不返回 API Key、Cookie、数据库密码；
- 不把完整隐私数据塞入 content；
- 明确数据来源和操作结果。

### 11.1 Tool Call Closure

只要 Assistant Tool Call 已写入历史，Runtime 必须为它生成一个 ToolResult。

工具未执行也要使用 Synthetic Error Result：

```text
tool_aborted_before_dispatch
tool_not_executed_due_run_error
tool_result_missing_repaired
```

禁止在取消时简单跳过剩余 Tool Call。Provider 序列化前还会校验 Duplicate、Orphan、Name Mismatch 和 Missing Result。

---

## 12. 错误处理

可预期参数错误在 `validate_args` 中处理。

外部错误应区分：

```text
authentication_error
permission_denied
not_found
conflict
rate_limited
timeout
upstream_unavailable
invalid_response
```

错误消息必须脱敏。

禁止：

```text
捕获异常后返回“操作成功”
把原始 Authorization Header 写入错误
无条件重试写操作
工具失败后编造业务结果
```

### 12.1 瞬时错误和 Tool Retry

只有幂等工具可以声明自动重试：

```python
retry_policy=ToolRetryPolicy(
    max_retries=2,
    retryable_codes=frozenset({"upstream_unavailable"}),
    idempotent=True,
)
```

工具只对明确瞬时错误抛出：

```python
raise RetryableToolError(
    "上游暂时不可用",
    code="upstream_unavailable",
)
```

参数错误、权限拒绝、Approval 拒绝、业务校验失败和用户取消不得标记为 Retryable。

副作用 Tool 一旦进入 Handler，Runtime 会把未分类异常保守地视为
`outcome_unknown`，避免调用方把“外部已提交但响应丢失”误当成普通失败后重放。
只有 Adapter 能证明外部系统尚未收到或提交操作时，才可以显式抛出：

```python
from pi_agent_loop import DefinitelyNotCommittedToolError

raise DefinitelyNotCommittedToolError(
    "internal diagnostic only",
    code="upstream_rejected_before_commit",
    public_message="操作尚未提交，请稍后重试",
)
```

该异常是严格的负提交证明，不得用于 Timeout、连接中断、响应解析失败或任何无法
排除已提交的情况；原始异常文本不会自动进入模型、日志或持久事件。

写工具默认不配置自动 Retry。只有服务端具备 Idempotency Key 和结果核对机制时，才能单独设计。

### 12.2 崩溃恢复 Replay Policy

每个工具必须明确：

```python
replay_policy="safe"   # 只读或严格幂等，可在 Dispatch 后安全重放
replay_policy="never"  # 写操作或结果不确定，必须先 Reconcile
```

默认是 `never`。只有经过幂等性评估的工具才能设置 `safe`。

`DurableOperationRecorder` 会在工具实际进入函数前记录 `tool_dispatch_started`；恢复时根据 `replay_policy` 选择重放或状态核对。

最终 Replay Policy 只能收紧，不能放宽。Tool 注册信息、可信 Intent Policy、Workflow
和 Plan Step 任意一层声明 `never`，执行和恢复都必须按 `never` 处理；不得让 Router、
模型输出、调用参数或 Recovery Callback 把它覆盖成 `safe`。无法确认时使用 `never`。

`AgentTool` 是不可变安全配置。`ToolDispatchRuntime` 在注册时还会封存 replay、审批、
Retry、Timeout、资源锁、Fencing、实现版本及 Handler 身份，并生成
`ToolSecurityContract` 摘要。禁止用同名新实例覆盖已注册 Tool；Prepared Call 在真正
派发前也会重新核对 Runtime 身份、注册代次和合同摘要。Durable Operation 会持久化该
摘要；恢复时，当前 Tool 与原合同不一致，或旧 `safe` 事件缺少摘要，都必须转入人工
处理，不能继续安全重放。Tool 的安全语义或实现发生合法升级时，应升级
`implementation_version` / `security_policy_version`，并显式迁移 Session 配置。

---

## 13. 只读工具与写工具

### 只读工具

例如：

```text
get_order_status
search_inventory
read_file
```

要求：

- 无副作用；
- 可安全重试时明确说明；
- 可以根据策略并行。

### 写工具

例如：

```text
cancel_order
create_refund
send_payment
write_file
```

除通用要求外，必须具备：

```text
Permission
Approval
Idempotency Key
Intent Log
Result Log
Retry Boundary
Conflict Handling
Rollback/Compensation
```

Approval 完成前不能执行写工具。

写工具被 Plan 调用时也不能走裸 `plan_step_executor`。它必须经
`plan_tool_bindings → ToolDispatchRuntime → Approval → WriteOperationService`，复用
相同的 Action Hash、一次性 Receipt、Idempotency 和 `outcome_unknown`/Reconciliation
语义。外部副作用可能已经发生但成功事件落盘失败时，不得返回普通失败并自动重试。

多 Worker 场景下，写 Tool Context 必须携带精确的 `fencing_scope` 和单调
`fencing_token`。业务数据库/API 应在同一事务或条件更新中拒绝旧 token；仅在框架内
检查 Lease 或 Heartbeat 不能阻止已经失去所有权的 Worker 继续提交副作用。

Tool 还要在自身元数据中声明审批和版本边界：

```python
requires_approval=True
implementation_version="2"      # execute/参数语义变化时升级
security_policy_version="3"     # 权限、脱敏、审批边界变化时升级
```

Host 会对 Tool、Capability 和 Intent 的审批声明取并集，任何一层要求审批都
不能被 Router 关闭。受管 Session 的配置哈希包含上述版本；升级后必须新建
Session，或在没有未完成 Operation 时显式执行配置迁移。

---

## 14. 注册方式一：ToolRegistry

通用 Auto Agent 使用：

```python
registry = ToolRegistry()
registry.register(create_add_tool())
registry.register(create_divide_tool())

agent = Agent(
    ...,
    tools=registry.all(),
)
```

`ToolRegistry` 负责：

- 保存工具；
- 拒绝重名；
- 保持注册顺序；
- 导出 `list[AgentTool]`。

它不负责 Intent 和 Capability。

---

## 15. 注册方式二：工具集合工厂

相关工具应有集合工厂：

```python
def create_calculator_tools():
    return [
        create_add_tool(),
        create_multiply_tool(),
        create_divide_tool(),
    ]
```

以及 Registry 工厂：

```python
def create_calculator_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_many(create_calculator_tools())
    return registry
```

新增工具后必须同步集合工厂和测试中的工具名称顺序。

---

## 16. 注册方式三：CapabilityRegistry

受控业务 Agent 使用：

```python
capabilities = CapabilityRegistry()
capabilities.register(
    create_get_order_status_tool(),
    capabilities={"orders.read_current"},
    domain="orders",
    operation="read",
    risk="low",
    requires_approval=False,
)
```

写工具：

```python
capabilities.register(
    create_cancel_order_tool(),
    capabilities={"orders.cancel"},
    domain="orders",
    operation="write",
    risk="high",
    requires_approval=True,
)
```

业务配置引用 Capability，不引用工具名：

```toml
capability = "orders.read_current"
```

---

## 17. 公开导出

新增领域无关的核心 Tool 时，可以按需更新：

```text
src/pi_agent_loop/tools/__init__.py
src/pi_agent_loop/__init__.py
```

并更新 `__all__`。可删除的教学 Tool 应优先使用按需加载，确保删除样例后
`import pi_agent_loop` 仍成功。

例如核心教学工具可以兼容：

```python
from pi_agent_loop import create_divide_tool
```

真实业务 Tool 不得加入上述两个核心导出文件。调用方应从自己的业务包导入：

```python
from my_business.tools import create_order_tool_bundle
```

无论哪一种，都不要要求用户导入内部私有执行函数。

---

## 18. 单元测试要求

每个工具至少测试：

```text
正常结果
details 字段
JSON Schema 关键字段
参数缺失
错误类型
边界值
取消
Timeout
进度 Update
敏感信息不泄露
```

除法还必须测试：

```text
整数结果
小数结果
正零
负零
布尔值
NaN/Infinity
```

---

## 19. Agent 集成测试

至少验证：

```text
模型 Tool Call
→ ToolRegistry 找到工具
→ validate_args
→ execute
→ Tool Result Message
→ 下一模型 Turn
→ 最终回答
```

业务工具额外验证：

```text
Capability Match
Required Tool Guard
Router 参数一致性
Capability Missing
Approval Gate
```

默认自动测试使用 `ScriptedProvider` 或 `httpx.MockTransport`，不访问收费 API。

真实联调必须由用户明确允许。

---

## 20. 文档和示例

新增工具后至少更新其所属包的文档和示例：

```text
README 工具列表
示例注册代码
Timeout 说明
Capability 映射
运行命令
测试数量
```

如果是真实业务工具，以下文件也必须位于业务仓库或业务包中，而不是本脚手架：

```text
my_business/BUSINESS_REQUIREMENTS.md
my_business/config/business.toml.example
```

只有领域无关的核心能力或虚构教学样例，才更新脚手架自己的 README 和示例。

---

## 21. 完整工具模板

```python
from __future__ import annotations

# 真实业务包从脚手架公开 API 导入；不要依赖 pi_agent_loop 内部相对路径。
from pi_agent_loop import AgentTool, AgentToolResult

# 仅当该文件确实属于 src/pi_agent_loop 内的通用/教学 Tool 时，才可以使用：
# from ..types import AgentTool, AgentToolResult


def validate(arguments):
    if not isinstance(arguments, dict):
        raise ValueError("参数必须是对象")
    value = arguments.get("value")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("value 必须是非空字符串")
    return {"value": value.strip()}


async def execute(tool_call_id, arguments, cancellation, on_update):
    cancellation.throw_if_cancelled()
    on_update(
        AgentToolResult(
            content=[{"type": "text", "text": "正在处理……"}],
            details={"phase": "running"},
        )
    )

    # 调用异步外部能力；长步骤之间继续检查 cancellation。
    cancellation.throw_if_cancelled()

    return AgentToolResult(
        content=[{"type": "text", "text": "处理完成"}],
        details={
            "operation": "example",
            "toolCallId": tool_call_id,
        },
    )


def create_example_tool() -> AgentTool:
    return AgentTool(
        name="example",
        label="示例工具",
        description="明确说明模型应在什么情况下使用该工具",
        parameters={
            "type": "object",
            "properties": {
                "value": {
                    "type": "string",
                    "description": "待处理值",
                }
            },
            "required": ["value"],
            "additionalProperties": False,
        },
        validate_args=validate,
        execute=execute,
        execution_mode="parallel",
        timeout_seconds=5,
        replay_policy="safe",  # 只有只读/幂等工具可以使用 safe
    )
```

---

## 22. AI 开发流程

AI 每次新增工具必须按顺序执行：

```text
1. 阅读 AGENTS.md
2. 阅读 BUSINESS_REQUIREMENTS.md
3. 阅读 TOOLS_IMPLEMENTATION_GUIDE.md
4. 确认工具是只读还是写操作
5. 确认 Intent 和 Capability
6. 检查接口信息是否完整
7. 先写或更新测试
8. 实现参数校验
9. 实现 execute
10. 配置取消和 Timeout
11. 注册到 ToolRegistry/CapabilityRegistry
12. 更新所属包的公开导出（真实业务不得修改 pi_agent_loop 根导出）
13. 更新业务包自己的 business.toml（业务工具）
14. 更新所属包自己的 README
15. 运行全部测试
16. 检查 Git 密钥隔离
17. 汇报 Mock 与真实接口边界
```

---

## 23. Definition of Done

只有全部满足才算完成：

- [ ] 工具独立文件；
- [ ] 名称、Label、Description 清晰；
- [ ] JSON Schema 严格；
- [ ] 运行时校验完整；
- [ ] 异步 Execute；
- [ ] CancellationToken；
- [ ] 进度 Update；
- [ ] 独立 Timeout；
- [ ] 已选择 parallel/exclusive/resource_locked；
- [ ] resource_locked 已定义稳定资源键；
- [ ] 已评估幂等性和 Retry Policy；
- [ ] 已声明 replay_policy；
- [ ] 写工具已设计 Approval/Idempotency/outcome_unknown；
- [ ] Retryable/Permanent 错误已区分；
- [ ] 成功 Details 结构化；
- [ ] 错误脱敏；
- [ ] ToolRegistry 注册；
- [ ] CapabilityRegistry 注册（业务工具）；
- [ ] 所属包公开导出（真实业务未污染 `pi_agent_loop` 根导出）；
- [ ] 单元测试；
- [ ] Agent 集成测试；
- [ ] Timeout/取消测试；
- [ ] Scheduler/Listener 异常下无后台 Task 残留；
- [ ] Update Listener 失败后子令牌仍 Detach；
- [ ] 取消/跳过后全部 Tool Call 都有 Synthetic ToolResult；
- [ ] Transcript Closure 校验通过；
- [ ] 所属包 README 更新；
- [ ] 业务包自己的 BUSINESS_REQUIREMENTS 更新（业务工具）；
- [ ] 全部测试通过；
- [ ] 无真实秘密进入 Git。
