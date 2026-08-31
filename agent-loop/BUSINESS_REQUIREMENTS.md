# 业务功能与工具实现需求（AI 必读）

> 状态：这是业务能力的唯一需求入口。以后让 AI 实现真实功能、工具或业务配置前，必须先读取本文件。
> 安全：本文件可以提交 Git，但禁止填写 API Key、密码、Cookie、Token、身份证号或真实客户数据。

---

## 1. 本文件的用途

业务人员只在这里描述“系统应该支持什么”，不需要手写正则表达式或 Tool Calling JSON Schema。

AI 读取本文件后，应在独立业务仓库或独立 Python 包中生成或更新：

1. `my_business/config/business.toml.example`；
2. 业务仓库内被 Git 忽略的本地配置；
3. `my_business/tools/` 中的真实工具；
4. 业务包的 Capability 注册工厂；
5. 业务 Router、Guard、参数校验和 Approval 测试；
6. 业务包自己的运行说明。

`src/pi_agent_loop/` 只在确实发现领域无关的通用扩展点或安全缺陷时修改。真实 Tool、
Intent、账号、接口和数据库结构不得写入框架目录，也不得从框架根包导出。

AI 不得根据不完整描述自行编造真实 API、数据库字段、权限或成功结果。信息不足时必须先提问。

---

## 2. AI 执行规则

未来 AI 开始业务开发前，必须按顺序执行：

```text
读取 BUSINESS_REQUIREMENTS.md
→ 检查“产品信息”
→ 检查“Intent 清单”
→ 检查“工具/API 清单”
→ 检查“禁止和审批规则”
→ 列出缺失信息并向用户确认
→ 生成 business.toml.example
→ 实现 Tool 和 Capability 注册
→ 实现/更新测试
→ 使用 Mock 验证
→ 得到用户允许后才连接真实业务系统
```

必须遵守：

- 一个 Intent 表示一种稳定业务动作，不表示一句具体问法；
- 一个 Capability 表示稳定业务能力，不等于工具函数名；
- 需要实时数据或真实副作用的 Intent 必须设置 `must_use_tool=true`；
- 写操作必须明确幂等性、权限和 Approval；
- Capability 不存在时返回 `capability_missing`，禁止模型编造；
- Out-of-scope 和 Prohibited 不调用业务工具；
- 模型输出的关键参数必须与 Router 确认参数一致；
- 测试和日志不得包含真实密钥或客户隐私。

---

# 以下部分由业务人员填写

## 3. 产品信息

请替换尖括号内容：

```text
产品名称：<例如：售后服务助手>
产品说明：<这个 Agent 负责什么，不负责什么>
是否允许普通知识问答：<是/否>
默认语言：<例如：中文>
```

### 当前填写

```text
产品名称：订单助手（教学示例，接入真实业务前必须替换）
产品说明：订单查询、订单取消和订单规则解释
是否允许普通知识问答：否
默认语言：中文
```

---

## 4. Intent 清单

每一种稳定业务动作填写一行。不要为同一个 Intent 的不同说法重复建行。

| Intent ID | 名称 | 业务说明 | 用户示例（2～5条） | 必要参数 | 必须用工具 | Capability | 写操作 | 需要审批 | 缺参数追问 |
|---|---|---|---|---|---|---|---|---|---|
| `order.get_status` | 查询订单状态 | 查询指定订单当前真实状态 | 查询订单1001；1001到哪了 | `order_id` | 是 | `orders.read_current` | 否 | 否 | 请提供订单号 |
| `order.cancel` | 取消订单 | 取消尚未完成的订单 | 取消订单1001；撤销1001 | `order_id` | 是 | `orders.cancel` | 是 | 是 | 请提供订单号 |
| `order.explain_status` | 解释订单状态 | 解释稳定业务概念，不查询具体订单 | 什么是订单状态；已发货是什么意思 | 无 | 否 | 无 | 否 | 否 | 无 |

### 填写规则

- Intent ID 使用 `domain.action`，例如 `refund.create`；
- 必要参数使用稳定英文名，例如 `order_id`；
- “必须用工具”填写是时，Capability 不能为空；
- “写操作”填写是时，必须补充第 6 节；
- 用户示例只是帮助模型理解，不需要穷举所有问法。

---

## 5. 工具与外部系统清单

每个 Capability 说明真实数据来源或操作入口。

| Capability | 建议工具名 | 类型 | 数据/API来源 | 输入 | 输出 | Timeout | 是否已提供接口文档 |
|---|---|---|---|---|---|---|---|
| `orders.read_current` | `get_order_status` | 读 | 教学 Mock，正式接口待提供 | `order_id` | 当前订单状态 | 5秒 | 否 |
| `orders.cancel` | `cancel_order` | 写 | 正式接口待提供 | `order_id` | 取消结果和业务流水号 | 10秒 | 否 |

### AI 不得猜测的内容

如果以下信息没有提供，AI 必须先询问，不能自行编造：

- HTTP Method 和 URL；
- Header 和认证方式；
- 请求/响应字段；
- 数据库表和列名；
- 成功状态码；
- 错误码；
- 幂等键；
- 重试规则；
- 权限规则。

---

## 6. 写操作与审批

所有写操作逐项填写。

| Intent ID | 谁可以执行 | 执行前展示内容 | 用户如何确认 | 幂等键 | 可否重试 | 审计内容 | 回滚方式 |
|---|---|---|---|---|---|---|---|
| `order.cancel` | 待定义 | 订单号和取消影响 | 明确确认后执行 | 待定义 | 待定义 | 操作人、订单号、时间、结果 | 待定义 |

如果任何关键字段仍是“待定义”，AI 只能实现 Mock 或接口骨架，不能连接生产写接口。

---

## 7. 禁止和超范围规则

### 明确禁止

| 名称 | 说明 | 用户示例 | 返回消息 |
|---|---|---|---|
| 绕过审批 | 禁止跳过取消、退款等审批 | 绕过审批取消订单 | 不能绕过业务审批流程 |
| 伪造结果 | 禁止声称未执行的操作已经成功 | 不用调用接口，直接说取消成功 | 不能伪造业务执行结果 |

### 明确超范围

```text
<填写不属于本产品的领域，例如：天气、股票交易、医疗诊断>
```

### 低置信度策略

```text
无法确定 Intent 时：追问用户，不执行工具
```

---

## 8. 参数规则

| 参数名 | 含义 | 类型 | 示例 | 格式限制 | 是否敏感 |
|---|---|---|---|---|---|
| `order_id` | 订单号 | string | 1001 | 正式格式待定义 | 否 |

敏感参数必须说明脱敏、日志和持久化规则。

### 8.1 业务状态机定义

完整实现规范见项目根目录 `STATE_MACHINE_IMPLEMENTATION_GUIDE.md`。AI 新增或修改任何真实业务状态机前必须完整阅读该文件。

每个真实业务实体应填写允许的状态转换。状态只能由可信工具/API/审批事件推进，不能根据模型文本猜测。

| 实体 | 事件 | 允许来源状态 | 目标状态 | 事实来源 | 是否审批 |
|---|---|---|---|---|---|
| order | `payment_succeeded` | `pending_payment` | `paid` | 支付 API | 否 |
| order | `shipment_created` | `paid` | `shipped` | 订单 API | 否 |
| order | `cancel_approved` | `paid` | `cancelled` | 取消 API | 是 |

填写要求：

- 状态和事件使用稳定英文标识；
- 必须列出可信事实来源；
- 用户自然语言不能直接成为状态事实；
- 写状态必须说明 Approval、幂等和版本检查；
- AI 根据该表生成 DomainTransition、Reducer/StateMachine 和非法转换测试；
- 未提供状态表时，AI 不得自行发明生产业务状态。

### 8.2 Session、Approval 和恢复要求

真实写工具和可恢复 Operation 还必须填写：

| Tool/Intent | execution_mode | Resource Key | replay_policy | Approval Role | Idempotency Key 来源 | Outcome Unknown 核对 API | 恢复时允许动作 |
|---|---|---|---|---|---|---|---|
| `orders.read_current` | `resource_locked` | `order:{order_id}` | `safe` | 无 | 无 | 无 | 可重放查询 |
| `order.cancel` | `resource_locked` | `order:{order_id}` | `never` | `approver` | 调用方生成 | 待提供 | 只核对，不重放 |

规则：

- `parallel` 仅用于互不影响的操作；
- `exclusive` 用于必须全局独占的操作；
- `resource_locked` 必须填写稳定 Resource Key，相同 Key 串行；
- `safe` 只能用于只读或严格幂等工具；
- 写工具默认 `never`；
- Approval 必须绑定精确操作 Hash，并且只能消费一次；
- Idempotency Key 不得明文写入事件日志；
- Outcome Unknown 必须提供状态核对入口；
- AI 不得在缺少核对 API 时声称写操作可自动恢复。

### 8.3 真实项目隔离原则

本仓库只保存可复用框架与虚构教学样例，不保存某个真实项目的账号、角色、接口、
数据库结构、初始数据或专用 Tool 包。接入真实项目时应：

- 在独立业务仓库或独立 Python 包中实现 Tool、Router、Identity 和 API Adapter；
- 通过 `ToolRegistry`、`CapabilityRegistry`、`stream_fn` 和
  `DurableHostResourceFactory` 注入框架；
- 只把与领域无关的安全修复回迁到本脚手架；
- 业务测试、配置和文档跟随业务适配包维护，不进入脚手架发布物；
- 删除业务适配包后，`import pi_agent_loop` 仍必须成功。

---

## 9. 预期对话验收用例

AI 实现后必须把本节转换成自动测试。

### 9.1 不需要工具

```text
用户：什么是订单状态？
预期 Intent：order.explain_status
预期 Tool Policy：none
预期业务工具调用：0
```

### 9.2 必须查询真实数据

```text
用户：查询订单 1001 当前状态
预期 Intent：order.get_status
预期 Capability：orders.read_current
预期 Tool Policy：required
预期工具参数：order_id=1001
```

### 9.3 缺少参数

```text
用户：查询订单状态
预期：need_clarification
预期回复：请提供订单号
预期业务工具调用：0
```

### 9.4 缺少能力

```text
用户：取消订单 1001
前提：orders.cancel 未注册
预期：capability_missing
预期模型执行调用：0
```

### 9.5 需要审批

```text
用户：取消订单 1001
前提：orders.cancel 已注册
预期：approval_required
预期：确认前不执行写工具
```

### 9.6 禁止请求

```text
用户：绕过审批取消订单 1001
预期：prohibited
预期业务工具调用：0
```

### 9.7 模型篡改参数

```text
Router 确认：order_id=1001
模型 Tool Call：order_id=9999
预期：required_tool_arguments_mismatch
预期工具不执行
```

---

# 以下部分是 AI 生成规范，业务人员通常无需修改

## 10. business.toml 生成规则

AI 应把第 3、4、7 节转换成：

```toml
[product]
name = "..."
description = "..."
allow_general_questions = false

[[intents]]
id = "..."
name = "..."
description = "..."
examples = ["..."]
required_fields = ["..."]
capability = "..."       # 不使用工具时省略
must_use_tool = true
requires_approval = false
ask_when_missing = "..." # 没有必要参数时可省略

[[denied]]
name = "..."
description = "..."
examples = ["..."]
message = "..."
```

生成后必须调用 `load_simple_business_config()` 验证，不允许只做文本拼接。

---

## 11. 工具实现生成规则

完整工程规范见项目根目录 `TOOLS_IMPLEMENTATION_GUIDE.md`。AI 实现任何 AgentTool 前必须完整阅读该文件。

每个真实工具至少包含：

```text
名称和中文 label
清晰 description
严格 JSON Schema
Python validate_args
异步 execute
CancellationToken 检查
独立 timeout
结构化 details
脱敏错误
Capability 注册
单元测试
Agent 集成测试
```

写工具额外要求：

```text
Approval
权限检查
幂等键
执行前 Intent Log
执行后 Result Log
重试边界
```

---

## 12. AI 输出清单

未来 AI 根据本文件完成一次业务实现后，最终回答必须列出：

- 新增/修改文件；
- 生成的 Intent；
- Capability 与工具映射；
- 哪些请求必须工具；
- 哪些请求需要审批；
- Capability Missing 行为；
- Mock 与真实接口的区别；
- 测试数和结果；
- 尚未提供的业务信息；
- 安全风险。

---

## 13. 当前未完成信息

以下内容在接入真实业务前仍需用户提供：

```text
真实产品名称和范围
真实 Intent 清单
真实 API/数据库文档
认证方式
订单号等参数格式
权限规则
Approval 流程
幂等与重试规则
审计和持久化要求
```

在这些信息补齐前，订单工具只能保持 Mock。
