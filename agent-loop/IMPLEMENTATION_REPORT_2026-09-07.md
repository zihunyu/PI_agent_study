# 通用 Agent 四项扩展验收记录

实施目录：`D:\0715\git_codex\20260825\PI_agent_study_upload\agent-loop`。

四项扩展已实现，保留此前 P1/P2 修复和原有本地配置。业务代码未写入其他同名项目，没有接入真实业务服务、收费模型或新增生产数据库。

| 项目 | 实现结果 |
| --- | --- |
| 业务包单入口 | `BusinessBundle`、`load_business_bundle` 统一工具工厂、Capability、Intent、Plan Policy 和工具绑定；Host 拒绝与旧式业务装配混用；保留旧接口。 |
| 执行策略 | `ExecutionPolicy` 和 `ExecutionContext` 贯通 Agent、Host 共享模型/工具 Runtime、Router、业务包 Planner、恢复和 Plan 工具输出。先转换再审核输入；模型输出审核后公开；工具结果在 Hook 前检查，覆盖结果再次检查；审核启用时不公开未经审核的工具进度。 |
| 可替换协议 | `SessionEventJournal`、能力声明及 Plan/Run/Operation 事务参与者协议替代实现类限制；保留共享 Journal/Principal/session、原子多流 CAS、Fencing、租约和完成去重。恢复优先异步公开接口。`RuntimeBoundRouter` 支持独立实例绑定与物理调用 Admission 计量。 |
| Provider 参数 | 配置默认值和调用覆盖支持二选一 Token 上限、temperature、top_p、stream_options.include_usage；HTTP 前严格校验，重试配置固定，预算只能收紧；区分有效零 usage 与缺失用量。 |

SQLite 表结构及事件协议保持兼容。没有通过放宽事务、审批或重放约束来实现适配器替换。业务配置、工具合同和执行策略版本的变化仍使用受管会话迁移机制。

## 离线验收

| 检查 | 结果 |
| --- | --- |
| 全量 pytest（通过 coverage 执行） | **853 passed**，312.08 秒；780 项基线加 73 项新增场景。 |
| Ruff | `src`、`tests`、分发验证脚本、外部业务包示例全部通过。 |
| Mypy | `--no-incremental src`，145 个源文件通过。 |
| 项目分支覆盖率 | **78.36%**，高于 75% 门槛。 |
| 评估模块覆盖率 | **84.48%**，高于 80% 门槛。 |
| 分发构建 | wheel、sdist 成功；使用项目 CI 锁定的 setuptools 84.0.0，局部安装于忽略的 build 目录，未修改全局构建器。 |
| 打包后验证 | wheel 元数据验证、13 个公开 API、5 个示例资产及隔离加载后的离线示例运行通过；sdist 包含对应示例资产。 |
| Diff 空白检查 | 通过。 |

新增测试覆盖业务配置冲突、审批限制合并、策略版本迁移、回调重新注入、模型/工具拒绝输出防泄漏、上下文并发/取消/超时、非继承适配器、三流中途故障回滚、CAS、旧 Fencing、重复恢复与通知去重、Router 零/多次调用、失败重试、未知用量预留，以及 Mock HTTP 请求参数与 usage-only 片段。

`scripts/check_generic_distribution.py` 已加入 CI 的分发验证步骤。可复查：

```powershell
python -m coverage run -m pytest -q
python -m coverage report --precision=2
python -m coverage report --include="src/pi_agent_loop/evaluation/*" --fail-under=80 --precision=2
python -m ruff check src tests scripts/check_generic_distribution.py examples/business_package
python -m mypy --no-incremental src
python scripts/check_generic_distribution.py build/extension-dist
```

## 接入入口

后续业务通过工具工厂和 TOML 装配，不需要修改核心执行分支。示例见 [业务包说明](examples/business_package/README.md)，接口和边界说明见 [GENERIC_EXTENSIONS.md](GENERIC_EXTENSIONS.md)。真实写业务仍需提供已有合同要求的身份、审批、幂等和资源 Fencing 等部署适配；自定义模型组件必须使用注入的 Runtime，持久化适配器必须实际满足其声明的事务能力。
