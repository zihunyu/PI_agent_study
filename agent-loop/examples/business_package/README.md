# 单入口业务包（离线示例）

本目录是虚构业务包，`tools.py` 只导入 `pi_agent_loop` 公开接口。示例客户端返回固定文本，不连接外部服务。

安装框架后执行：

```powershell
python examples/business_package/run_demo.py
```

业务接入步骤：

1. 在业务包的 `tools.py` 或 `tools/` 中实现 `AgentTool`，返回显式工厂映射。客户端、凭据由闭包注入。
2. 在 `business.toml` 的 `[[tools]]` 中注册工具和能力，在 `[[intents]]` 中描述意图；需要计划执行时添加 `[[plans]]`。
3. 调用 `load_business_bundle(path, tool_factories=...)`，将结果作为 `business_bundle` 传给 `DurableAgentHost.create`。

```python
bundle = load_business_bundle(config_path, tool_factories=tool_factories(client))
host = await DurableAgentHost.create(
    session_id="session", state_dir=state_dir,
    model=model, stream_fn=provider.stream, system_prompt="业务助手",
    business_bundle=bundle,
)
try:
    await host.prompt("业务请求")
finally:
    await host.close()
```

`[[tools]]` 的字段是 `name`、可选 `factory`（默认同名）、`capabilities`、`domain`，以及原有 Capability 字段 `operation`、`risk`、`requires_approval`、`priority`、`side_effect`。工厂必须返回同名工具；TOML 不接受 Python 导入路径或任意请求体。

`[[intents]]` 和 `[product]` / `[[denied]]` 保留原简化配置格式。`[[plans]]` 增加 `intent` 和 `tool`，其余字段复用 `IntentPlanPolicy.to_dict()` 的名称：`requiresApproval`、`write`、`replayPolicy`、`capabilities`、`approvalRoles`、`parameterContract`、`requiredPredecessorIntents`、`argumentBindings`、`preconditions`、`allowParallelSideEffects`、`resultContract`。

省略的 Plan 参数合同由工具 JSON Schema 顶层 `properties`、`required` 和明确的 `type` 生成。工具仍须实现实际参数校验，嵌套约束、枚举和外部授权不能只靠这个顶层合同。显式 Plan 合同必须与工具合同一致；必填路由字段也须一致。审批取工具、能力、意图和 Plan 的最严格要求，冲突在加载或 Host 装配时拒绝。写工具仍需身份、Approval、幂等键、资源锁、下游 Fencing 和已有 Write 边界配置。

`business_bundle` 不能与 `tools`、`router`、`capabilities`、`planner`、`plan_policies`、`plan_tool_bindings`、`plan_step_executor` 混用。存储、身份、安全策略、预算等基础设施参数仍可注入。旧 `load_simple_business_config` 和旧式 Host 装配调用保持支持。

不自动扫描目录，不动态导入 TOML 指定的代码，不热加载。工具实现/合同、业务配置或策略版本变化应通过现有受管会话配置迁移机制处理。
