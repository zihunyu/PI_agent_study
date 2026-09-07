# PI_agent_study_upload 源码审查

更新：本报告下文保留的是修复前的审查证据。2026-09-07 已修复 F1—F5，修复后全量测试
为 **780 passed**，Ruff/Mypy 通过。当前修复状态和通用 Agent 接入分析见
[通用 Agent 评估与修复记录](D:/0715/git_codex/20260825/PI_agent_study_upload/GENERIC_AGENT_ASSESSMENT_2026-09-07.md)。

审查日期：2026-09-07。审查目录：`D:\0715\git_codex\20260825\PI_agent_study_upload`，Python 项目位于其 `agent-loop` 子目录。提交基线：`00a3e23`，同时检查了当前工作区的未提交变更。

此前针对同级另一个 `agent-loop` 目录的审查不能用于本项目。本报告重新核对了本目录的源码、测试和接入说明，没有沿用那份业务问题结论。

本项目已经包含较完整的 Agent 基础设施：模型与工具循环、文件工具、受信 Shell、工具调度与审批、持久执行与恢复、计划数据传递、模型路由、多代理、可选内容安全、加密记忆和 CI。主要不足集中在模块组合时的并发与故障处理，以及生产服务适配尚需应用方补齐。以下发现来自重点路径审查和离线复现，不表示对所有运行环境做了穷尽验证。

## 1. 验证结果与业务代码来源

| 检查 | 结果 |
| --- | --- |
| 本地环境 | Windows，Python 3.12.1 |
| Python 源文件 / 测试模块 | 142 / 81 |
| `python -m pytest -q` | **759 passed，8 failed**，331.69 秒 |
| `python -m ruff check src tests` | 通过 |
| `python -m mypy --no-incremental src` | 142 个源文件通过 |
| 提交版示例配置的隔离复测 | 相关两个测试模块 **14 passed** |
| 补充复现 | 下述 5 个源码问题均用合成数据、临时文件或注入式故障复现 |

8 项失败来自 `test_hybrid_router.py` 的 7 项及 `test_simple_business_config.py` 的 1 项。当前 `config/business.toml.example` 有未提交变更，原来的意图集合已换成 `test_demo` 配置，而测试仍依赖原始意图 ID、数量和产品名。在临时目录复制这两个测试模块、使用 `git show HEAD:agent-loop/config/business.toml.example` 提取的配置，并显式加载本目录源码后，14 项全部通过。**这说明当前工作区测试确实失败，但不能将这 8 项直接归因为路由实现缺陷；也不能据此宣称完整提交版的全量测试已通过。**

当前 `config/README.md` 也有未提交修改，`config/dingdan.toml.example` 是未跟踪文件。配置说明引用了 `examples/dingdan_usage.py`，但该文件在本项目中不存在。源码 `src` 未检索到此前报告指称的 Dingdan/test_demo HTTP 业务适配实现。因此，“申请退款”“确认收货”出现在样例配置中，并不证明已经接入真实业务，更不能据此认定它们实际调用了哪个接口。现有测试和评估里的订单工具也是合成示例。仅凭工作区状态无法判断这些本地配置由谁、何时引入。

建议把配置适配工作与框架测试夹具分开：测试使用独立、固定的 fixture；业务接入示例只有在对应适配器和入口存在时才写入运行说明。

本次没有修改源码、原有配置或原有测试，没有连接真实模型或业务服务。没有重跑完整 CI 的覆盖率、供应链与打包任务，不能把静态检查通过等同于这些任务通过。

## 2. 已复现的源码问题

### F1 · P1：任务结果保存失败，整个多代理运行仍返回成功

位置：[multi_agent.py:1175](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/multi_agent.py:1175)、[multi_agent.py:966](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/multi_agent.py:966)。

`_run_task` 先将成功结果写入局部 `results`，随后调用 `state_store.record_task_result`。即使存储调用抛出异常，`finally` 仍会置位完成事件。上层 `gather(..., return_exceptions=True)` 的异常结果没有被检查，最终状态又根据局部成功结果计算。

复现使用两个依赖任务 `a → b`，Worker 都正常返回成功；仅让状态存储在保存 `a` 的结果时抛出 `OSError`。实际结果如下：

```text
run_status: succeeded
executed_tasks: [a, b]
stored_task_statuses: {a: running, b: succeeded}
stored_results: [b]
```

影响：调用方收到整体成功，依赖任务已经执行，但存储仍认为前置任务运行中。接入外部状态存储时，暂时的存储故障会造成返回结果、状态查询和恢复依据不一致。

建议：把结果保存视为编排的完成条件；显式处理任务收集到的异常，只有保存完成才能向依赖者发布成功。保存失败必须暴露明确的编排故障或不确定状态，同时保留“Worker 可能已经完成操作”的事实，避免把它当作可无条件重试的业务失败。

### F2 · P2：并行工具输出相互干扰内容安全检查

位置：[safety.py:196](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/safety.py:196)，调用入口为 [agent.py:695](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/agent.py:695)。

`ContentSafetyPipeline` 按策略下标仅保存一个正在执行的任务。第二次检查只要发现同一个策略仍在运行，就直接抛出 `ContentSafetyUnavailable`。这一判断没有区分正常并发请求和超时后仍未退出的旧策略任务。

复现通过真实 `Agent` 调度两项并行的合成只读工具。审核策略只在检查工具输出时异步等待 50 毫秒，然后始终允许。两个工具都执行成功，但其中一个结果被替换为：

```text
safety code: policy_still_running
tool result code: tool_output_unavailable_after_commit
effectCommitted: true
retryable: false
```

影响：审核服务正常、内容合法时，正常的并行工具调用也会丢失一项可用结果并停止后续自动执行。这不是内容被策略拒绝，而是管线自己的并发限制触发。

建议：为合法并发提供有界队列或并发槽；将“超时且拒绝退出的策略任务”与正常在途任务分开管理。增加“同一 Agent 并行工具 + 异步审核策略”的组合回归用例。

### F3 · P2：grep 漏掉长行后半部分，却报告搜索完整

位置：[workspace_files.py:540](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/tools/workspace_files.py:540)。

每一行先被裁剪成前 4096 个字符，然后才执行匹配。后面的文本从未参与搜索，但这种裁剪不会将 `truncated` 标为真，也不会增加 `skippedFiles`。

复现：临时文本文件的内容为 5000 个 `x` 后接 `REVIEW_NEEDLE`；文件远小于工具的文件大小限制。搜索该标记得到：

```text
matches: 0
truncated: false
skipped_files: 0
```

影响：压缩 JSON、生成的代码和长行日志中的真实内容可能被判定为不存在，影响定位、修改和审查的准确性。

建议：分离搜索范围与展示范围。对允许的文件进行完整匹配，再限制返回片段；如果为了资源上限必须截断扫描，应明确报告扫描不完整，不能返回完整的零命中结果。

### F4 · P2：同进程的两个文件工具服务仍可能互相覆盖修改

位置：[services.py:119](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/tools/services.py:119)、[workspace_mutations.py:75](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/tools/workspace_mutations.py:75)、[atomic_writer.py:50](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/tools/atomic_writer.py:50)。

每次 `ToolServices.create` 都新建自己的 `FileMutationQueue`。文件版本检查和 `os.replace` 发布则是两个分离的步骤。因此，针对同一工作区创建两套服务，即使都在同一进程，也没有共享的检查与写入互斥。

复现使用两套实际文件工具服务：两者先读取同一个文件并获得同一个版本；在注入的 `AtomicFileWriter` 中仅用线程同步控制交错，让两次版本检查都完成，再依次发布 A、B 内容。其余读写行为仍由原实现执行。结果为：

```text
successful_writes: 2
same_expected_version: true
final_content: B
```

影响：两个会话可能都收到写入成功，后写入者却覆盖前一会话的修改，没有得到预期的版本冲突。

范围说明：共享同一个服务实例时，其队列可以保护上述过程；本问题针对同工作区的独立服务实例，不依赖恶意外部进程。README 声明的“单进程 CAS”还需要补充“共享同一互斥域”这一条件。原子替换本身不等于原子的版本比较后更新。

建议：按规范工作区路径共享互斥服务，并在同一个锁范围完成重新校验和发布；或明确只支持单写入者并强制验证装配方式。若要支持多进程，还需要跨进程的文件锁或相应写网关。

### F5 · P2：同步仲裁器可阻塞事件循环，使总 deadline 失效

位置：[multi_agent.py:1252](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/multi_agent.py:1252)。

`ResultArbitrator` 接口允许同步或异步返回，但同步 `arbitrate` 直接在事件循环执行。总 deadline 依赖同一个事件循环上的异步等待；同步调用阻塞时，计时与取消无法及时得到执行。

复现：两个副本正常成功，使用一个同步仲裁器，等待 150 毫秒后执行原来的精确匹配仲裁。配置总 deadline 为 30 毫秒，实际约 156 毫秒后仍返回 `succeeded`。

影响：接入含同步计算或 I/O 的可信仲裁适配器时，既可能拖住其他任务，也可能把已经超时的运行报告为成功。

建议：统一要求可取消的异步仲裁适配器，或将同步仲裁移出事件循环并对等待施加剩余期限；最终成功前再次校验 deadline。线程方式只能约束等待，不能强杀底层同步操作；需要硬终止时必须另做进程隔离。现有文档关于恶意 Worker 无法强杀的说明，不应替代这个受支持同步接口的超时处理。

## 3. 功能缺口与接入边界

以下多数属于目前没有交付的产品或适配能力，其中一些已在 README 明确列为应用方职责，不能混同于上面五个实现错误。

| 能力 | 已有实现 | 尚需补齐 |
| --- | --- | --- |
| 模型生成参数及预算兑现 | Provider、重试、模型路由与 `PlanUsageMeter` 协议 | 内置请求序列化未传递 `max_tokens`、`max_completion_tokens`、`temperature` 和服务端 `stream_options.include_usage`。需要显式支持、校验常用参数，以及在每次实际派发中落实 Token/费用预算的真实计量适配器。 |
| 真实外部工具 | 文件读写、搜索、受信本机 Shell，以及业务工具注册接口 | 浏览器、MCP 与具体业务 API 适配包；本项目的样例业务配置不能代替这些实现。 |
| 登录、审核与审批产品流程 | 身份验证注入点、授权/审批框架、内容安全管线 | OIDC/SSO 等可信身份适配、真实内容审核、审批界面及通知接入。 |
| 跨重启的交互状态 | Session Journal、Durable Host/Plan；澄清状态接口与内存实现 | 内置澄清 Store 不跨重启；steering/follow-up Inbox 也在进程内，产品若要求可靠续接，需要持久存储、投递及去重。 |
| 多机运行 | 本机 SQLite 事务、Claim/Lease/Fencing 路径、Durable Plan Store 协议 | 共享事务后端、分布式资源锁和部署层 Worker 唤醒等；单进程 MultiAgentOrchestrator 不是完整的多机 Worker 系统。 |
| 生产语义记忆 | 加密 SQLite 记忆、TTL、显式检索接入 | 内置 `HashingEmbeddingProvider` 为离线确定性实现；生产检索质量需要真实 Embedding，并接入密钥管理和保留/删除等生命周期流程。 |
| 不受信代码执行 | 子进程超时和尽力清理进程树 | 容器或 OS 级沙箱。当前 Shell 按设计执行可信本机操作，不能作为不受信代码隔离设施。 |
| 真实模型效果验证 | Agent 评估器、证据校验、回归 gate、CI | 现有 `agent-core-v1.json` 的 5 个案例为教学 Mock；CI 的真实 Agent gate 仍使用 `ScriptedProvider`。需要另外准备实际模型的任务数据集、重复采样和质量/延迟/费用基线。 |

模型参数缺口已做本地序列化验证：向 `serialize_chat_request` 传入上述四类参数后，生成请求仍只有 `messages`、`model`、`stream` 三个键。这是当前内置 Provider 的能力范围；不能因为上层存在 `stream_options` 或预算对象，就认为对应服务端参数已经生效。

接入边界依据：[SCAFFOLD_INTEGRATION_GUIDE.md:64](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/SCAFFOLD_INTEGRATION_GUIDE.md:64)、[README.md:1544](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/README.md:1544)。参数实现见 [serialize.py:246](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/providers/serialize.py:246)，硬预算协议见 [resources.py:128](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/src/pi_agent_loop/planning/resources.py:128)。评估样例见 [agent-core-v1.json](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/evals/agent-core-v1.json)，CI 使用的测试见 [test_agent_evaluation.py:1020](D:/0715/git_codex/20260825/PI_agent_study_upload/agent-loop/tests/test_agent_evaluation.py:1020)。

## 4. 建议处理顺序

1. 优先处理 F1 的状态保存故障传播，以及 F2 的合法并发审核失败；补充组合场景回归测试。
2. 明确同一工作区的写入互斥范围，处理 F4，防止多个会话静默覆盖修改。
3. 修复 F3 的搜索完整性标记和 F5 的仲裁超时。
4. 整理当前配置与测试夹具，清除引用缺失入口的接入说明，使当前工作区重新得到可信的测试基线。
5. 根据实际使用目标选择适配能力：本地开发助手优先补模型参数、文件协作与工具接入；需要长期或多机运行时，再补持久交互状态、共享后端和运维装配。具体业务功能应在需求明确后单独设计，不从订单样例推导。
