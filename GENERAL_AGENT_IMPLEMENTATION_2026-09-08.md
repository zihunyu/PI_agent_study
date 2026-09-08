# 通用 Agent 改造交付记录

实现目录：`D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop`。
范围对应同目录 `GENERAL_AGENT_GAP_ANALYSIS_2026-09-08.md` 的第 1—6、8—10 项。
第 7 项原生 Provider 协议扩展按要求保留；没有接入收费模型或真实业务 API。
原有业务包、审批、未知写入处理、Journal 事务与会话迁移规则继续使用。

## 已交付能力

| 原编号 | 改造 | 实际行为与主要入口 |
| --- | --- | --- |
| 1 | 通用任务入口 | `load_general_agent_bundle` / `create_general_agent_bundle` 根据获准工具生成规划目录；TOML 配置工具与验收合同，不要求逐项维护业务 Intent。`Host.submit_task` 用稳定请求编号提交、恢复并去重。 |
| 2 | 统一执行环境 | `ExecutionEnvironment`、Local 与 Docker 实现使用一致的文件/进程接口。模型代码执行需要实际隔离能力；容器限制网络、权限、挂载、资源与时间，后端不可用则拒绝执行。 |
| 3 | 模型派发审计 | 加密 Journal 保存转换、审核、压缩后的输入；内置 HTTP Provider 保存实际请求 JSON，每次物理重试单独记录。重启后仍可查询旧请求，不保存认证请求头。 |
| 4 | 目标核验与纠错 | `TaskContract` / `TaskResultValidator` 检查实际工具结果、报告内容、MIME、JSON 字段和引用；支持可信业务检查器、重新规划及无进展停止。`ArtifactStore` 保存可验证、可导出的实际文件。 |
| 5 | 扩展接入 | MCP stdio / Streamable HTTP 工具发现、参数校验、取消、关闭与有限重连；Skills 按需加载并绑定版本；TOML 扩展包按依赖启动，失败和连续取消时清理资源。 |
| 6 | 主动上下文预算 | `ContextBudget` 在请求前为输出预留空间，保留目标、受保护事实及工具配对；安全改写后重新检查大小，无法容纳时不派发模型。原始历史保持完整。 |
| 8 | 文档、语义检索与引用 | 本地 SentenceTransformer 真实神经嵌入；文本、JSON、CSV、PDF、DOCX 解析和分块；来源授权、版本索引、检索、撤权与删除；引用可回查对应版本，模型权重变化要求重建索引。 |
| 9 | 持久子任务 | `DurableChildSessionManager` 保存父子身份、任务收件箱、共享预算预留、取消状态和结果确认；`DurableHostWorker` 接入原编排器。父进程退出后的恢复复用已完成子任务。 |
| 10 | 可复现验收 | 增加跨工具任务、真实本地 MCP、模型请求审计、文档解析、进程终止恢复、去重、取消和真实语义模型测试；场景清单随包发布，CI 增加 Docker 与语义模型集成作业。 |

## 如何接入自己的业务

完整说明见 `agent-loop/GENERAL_AGENT.md`，可运行示例见
`agent-loop/examples/general_agent/run_demo.py` 与同目录 `agent.toml`。

1. 在独立业务包中实现工具，以显式 Python 工厂注入客户端、认证与授权规则。
2. 在 TOML 中登记工具及“怎样才算完成”的合同；需要长连接、技能或检查器时使用扩展包。
3. 单入口加载后，使用 `DurableAgentHost.create(general_bundle=...)` 和 `submit_task(...)`。

框架可以组合工具和检查证据；真实业务的幂等键、写入对账、权限、业务规则及语义验收仍由业务包提供。
工具、技能和远端 MCP 注释不能放宽本地权限。模型说“已完成”本身不会通过验收。

## 验证记录

最终命令结果记录在 `agent-loop/build/general-final-*.log`；该目录为本地构建输出，不纳入源码发布。

| 检查 | 本次结果 |
| --- | --- |
| 独立 Python 3.12 环境的全量 pytest | **931 passed、2 skipped，另有 70 subtests passed**；耗时 323.07 秒。保留并通过原有 878 项回归。 |
| 真实本地语义模型 | 单独设置预置权重后 **1 passed**，补跑了全量中的语义环境跳过项；耗时 130.68 秒。使用多语言 MiniLM 的固定权重版本，实际 CPU 推理。 |
| 项目覆盖率（含分支） | **78.24%**，通过既有 75% 门槛。 |
| evaluation 模块覆盖率 | **84.48%**，通过既有 80% 门槛。 |
| Ruff | 源码、测试、脚本与通用示例全部通过。 |
| Mypy | **162 个源码文件通过**，使用 `--no-incremental`。 |
| 最低构建器与包检查 | setuptools **84.0.0** 构建 wheel/sdist 成功；**39 个公开接口、8 个示例资产、两个离线示例**通过，12 个通用验收场景随包发布。 |
| 依赖 | **116 个固定版本**按 SHA-256 安装；`pip check` 通过；本次 `pip-audit` 未发现已知漏洞。MCP 最低版本与 CI 版本已更新为 **1.28.1**。 |
| CI 配置和平台依赖 | 工作流语法、12 个场景对应测试路径，以及 Linux/Python 3.11—3.13、Windows/Python 3.12 的依赖元数据闭包检查通过。 |

额外复现场景覆盖：父进程不执行清理直接退出、父子租约到期时间不同、连续取消扩展启动、
文档并发导入突破数量上限、分块配置变化导致旧引用失效，以及独立 Python 环境的标准库同名模块冲突。
这些场景已加入回归；短暂的写租约占用保持任务待执行，后端不具备所需能力仍明确拒绝启动。

可复现命令（先按 `agent-loop/requirements/README.md` 安装锁定依赖）：

```console
python -m coverage run -m pytest -q
python -m coverage report
python -m coverage report --include="src/pi_agent_loop/evaluation/*" --fail-under=80
python -m ruff check src tests scripts examples/general_agent
python -m mypy --no-incremental src
python scripts/check_minimum_build.py
python -m pip_audit --strict --disable-pip --require-hashes -r requirements/ci.lock
```

本机没有 Docker Engine，因此真实容器隔离测试在本机跳过；已有实际后端实现、合同测试和独立 CI 集成作业。
Linux、Python 3.11/3.13 的 CI 作业需要在相应环境运行。本机验证不代替这些平台的实跑结果。
离线验收证明执行、恢复和扩展合同；正式业务的任务成功率、质量、成本与负载需使用其真实验收数据衡量。
