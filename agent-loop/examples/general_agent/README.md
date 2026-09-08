# 通用任务接入示例

安装包后运行 `python run_demo.py`。示例只用虚构文本和 ScriptedProvider，
不联网、不调用收费模型。它读取资料、核验报告缺失、补做报告，然后再次提交相同
请求编号，验证不会重复调用模型。

新增能力的步骤：

1. 编写 `AgentTool`，声明 JSON 参数、重放、权限、超时及实现版本。
2. 在 `agent.toml` 的 `[[tools]]` 填名称与显式 Python 工厂名。
3. 在 `[[artifacts]]` 或可信 `task.checks` 中声明验收条件。
4. `load_general_agent_bundle(...)` 后传入 `DurableAgentHost.create(general_bundle=...)`。

工具名称会自动生成可规划能力，无须另写业务意图分类。真实业务的认证、客户端、
业务成功判断仍由应用工厂和验收器提供。写操作必须继续接入审批与下游 fencing。

MCP、Skills、Docker 环境、文档库和持久子任务的配置见随包提供的
`GENERAL_AGENT.md`。不要把示例密钥用于真实数据。
