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
- 写操作在 Approval 完成前不得执行。
