# 通用扩展接口

业务单入口示例见 [examples/business_package](examples/business_package/README.md)。框架提供运行、审核、恢复和事务边界；外部业务包提供工具实现、工厂和 TOML，核心执行代码不需要加入具体业务分支。

## 执行策略

```python
from pi_agent_loop import ExecutionPolicy

async def transform(messages, cancellation, context):
    # context.phase / context.tenant_id / context.session_id 来自执行 Runtime。
    # 只在业务已明确授权时调用记忆 API；不要仅因配置回调就打开 opt_in。
    if context.phase == "agent":
        ...
    return messages

policy = ExecutionPolicy(
    content_safety=safety_pipeline,
    transform_context=transform,
    version="business-policy-2",
)
# Agent(..., execution_policy=policy)
# await DurableAgentHost.create(..., execution_policy=policy)
```

旧 `Agent(content_safety=..., transform_context=...)` 通过兼容层工作，不能和 `execution_policy` 混用。旧两参数回调 `(messages, cancellation)` 继续有效，新回调增加 `ExecutionContext`。上下文转换后检查模型输入；审核启用时不公开原始流片段，最终模型输出通过检查后才交给 Router、Agent 或恢复处理。工具输出先检查，再交给 after-hook；Hook 返回覆盖结果时再次检查。审核启用时工具进度片段不公开，避免最终结果被拒绝前已经泄漏未经检查的内容；只公开已审核的最终结果。

Host 的普通请求、路由、业务包 Planner 和 `model_runtime.request` 恢复共用 Runtime；内部 Agent 不重复注入。上下文转换只影响当前模型请求，不写回历史，所以恢复时由当前可信回调重新构造。`version` 进入受管会话配置摘要，旧配置无策略时保持原摘要。

自定义 Planner / Validator / Replanner / Synthesizer 如需要模型，应调用共享 Runtime，或提供 `bind_runtime(*, stream_fn)` 返回独立实例。规则回调继续有效。任意 Python 回调自行持有并调用的外部客户端不可能被框架自动拦截；适配器必须遵守注入的 Runtime 合同。Runtime 的重试和上下文压缩在最终输入审核之外装配，每次重新进入 Provider 边界都审核当前输入；上下文转换每个逻辑请求仅执行一次。

审核服务和异步转换回调应响应取消。`transform_timeout_seconds` 限制请求等待时间，取消清理最多额外等待 50ms；超时后拒绝晚到结果。旧同步转换回调在独立守护线程中运行，支持返回消息列表或 awaitable，不能依赖事件循环线程身份；需要事件循环的实现应改成异步回调。回调收到独立的子取消令牌，超时不会取消其他请求。若回调忽略取消，策略会跟踪其实际完成，并在此期间拒绝新的转换调用，避免重复堆积任务。Python 不能强制终止同进程内任意代码；必须响应取消且避免在异步回调中执行阻塞代码，需要强制终止的外部工作应由业务方在可终止的进程或服务中隔离。

## Journal 与事务参与者

公开接口包括 `SessionEventJournal`、`SessionJournalCapabilities`、`JournalPlanStore`、`JournalRunStore` 和 `JournalOperationStore`。适配器只需满足协议，不要求继承 SQLite 类。可通过 `resource_factory` 注入共享 Journal 的 Runtime、Operation、Retry Store，并通过 `plan_store` 注入 Plan 参与者。

`SessionEventJournal` 的 `append_events` / `append_events_if_fenced_claim` 必须在同一事务中检查所有流的 CAS 和租约 generation，追加所有事件或全部回滚。能力声明不能替代真实实现；`atomic_multi_stream_append`、`atomic_fenced_append`、跨进程及多机标记必须反映实际部署。默认 SQLite 支持单机多进程，不能声明多机支持。

Plan、Run、Conversation 的原子启动、纠正计划注册和完成事件仍共享同一 Journal 对象、可信 Principal 和 session。`initialization_spec` / `event_spec` 返回参与同一事务的事件，不自行提前提交。更换后端不需要事件格式或 SQLite 表结构迁移。本轮只提供 SQLite 和使用组合实现的离线合同适配器，没有新增生产数据库。

恢复优先调用异步公开读取。旧同步 Retry 发现通过可选 `SynchronousSessionEventJournal.load_events_sync` 兼容；异步后端应调用 `incomplete_chains_async`，无需暴露 SQLite 私有方法。`RetryRecoveryManager` 优先异步发现，对旧同步 Store 使用线程兼容层。

## Router 和硬预算

自定义 Router 实现 `RuntimeBoundRouter`：保留 `route(...)`，并实现 `bind_runtime(*, stream_fn, retry_event_sink, durable_metadata_provider=None)` 返回独立的、仍符合协议的实例。所有模型请求使用注入的 `stream_fn`。多个 Host 可以使用同一个配置模板，不能共用绑定后的请求状态。

硬预算仍需要符合已有 Admission 合同的 Usage Meter，负责预留整个重试树的用量上界、派发约束和结算。框架从物理模型调用的 Admission Scope 验证实际尝试、失败重试和用量，不读取 Router 的 `call_count` / `evaluation_metrics`。明确未派发或规则路径观察到零次调用时释放；失败或取消后的 usage 不明时保留持久预留并停止，不能自动按零费用继续。

## Provider 参数

在已有 Provider TOML 中可选添加：

```toml
[profiles.third_party.generation]
max_completion_tokens = 1024
temperature = 0.2
top_p = 0.9

[profiles.third_party.generation.stream_options]
include_usage = true
```

Python 使用 `GenerationOptions(max_completion_tokens=1024, temperature=0.2, include_usage=True)` 传给 `ProviderProfile(generation=...)`。`provider.stream(model, context, options)` 的同名参数覆盖默认值；嵌套使用 `stream_options={"include_usage": True}`。`max_tokens` 和 `max_completion_tokens` 二选一，Token 上限为正整数；temperature 范围 0–2，top_p 范围 0–1，布尔值不能冒充数字，拒绝 NaN/Infinity。

未设置字段不发送。不按模型名称猜支持情况，不支持任意请求体覆盖。参数在 HTTP 派发前校验，重试使用同一有效配置；已设置的输出 Token 上限还会被当前预算进一步收紧。总预算仍包括输入、输出和服务计费用量，因此仅靠 HTTP 输出上限不能替代可信 Usage Meter。

`usageObserved` 区分有效零用量和缺失/不完整 usage；usage-only 流片段会纳入最终结果。缺失用量不会被受管 Admission 当作可靠零费用。真实服务协议差异、价格和安全服务依旧由部署方配置，本轮测试使用 Mock HTTP 和离线 Provider。

内置 Provider 同时收到输入、输出和总 Token 数时，要求总数与两项之和一致。矛盾用量按协议错误处理并标记为未知；通用 Runtime 也拒绝把小于输入/输出小计的总量作为可靠计量，保留预算预留并阻止后续尝试。

## 配置与构建回归

TOML Loader 与直接构造的 `BusinessBundle` 共用最终校验，包括 Intent 必需能力、工具参数合同、前置条件的两侧引用以及所有计划依赖的循环检查。新增依赖配置应在装配时失败，而非等到规划模型调用后才发现缺失引用。

最低构建器为 `setuptools>=84.0.0`，与当前 CI 锁定版本一致。`python scripts/check_minimum_build.py` 要求使用恰好等于最低声明的版本，离线构建 wheel / sdist 并验证元数据、公开接口和示例；CI 保留此检查，防止仅在较新构建器上通过而最低版本失效。
