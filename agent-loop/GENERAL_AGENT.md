# 通用 Agent 接入与验收

本项目提供可嵌入的 Python Agent Runtime 和可恢复 Host。通用模式接受开放任务，
从已注册工具生成规划目录，执行后核验实际工具结果和交付物，再决定完成、纠正、
等待审批或人工处理。工具、模型描述和技能正文都不能修改框架权限。

本轮未增加第 7 项所指的原生模型协议。现有 OpenAI Compatible Provider 仅增加
派发审计与上下文输出预留的传递，原有调用和生成参数仍兼容。

## 单入口装配

```python
bundle = await load_general_agent_bundle(
    "my_agent/agent.toml",
    artifact_store=artifacts,
    tool_factories=tool_factories,
    package_factories=package_factories,
    citation_resolver=documents.resolve_citation,
    validation_checks=trusted_checks,
)
host = await DurableAgentHost.create(
    session_id=session_id, tenant_id=tenant_id, state_dir=state_dir,
    model=model, stream_fn=provider.stream, system_prompt=system_prompt,
    general_bundle=bundle,
)
result = await host.submit_task("stable-request-id", user_request)
```

`agent.toml` 的工具工厂接收一个 settings 字典。工厂应返回同名 `AgentTool`，
认证和客户端由可信 Python 闭包注入。示例 `examples/general_agent` 只使用公开 API，
可以在安装后直接运行。旧 `BusinessBundle` 和旧式 Host 配置继续可用，不能与
`general_bundle` 混用。

工具参数仍经过完整 JSON Schema/`validate_args` 校验。规划器可以先读取资料，
再用实际返回值规划下一批步骤，无须预先列出所有业务意图。新增业务所需的真实
接口、认证、幂等键、业务前置条件和“怎样才算办成”仍需由业务实现提供。
改变工具合同、能力、技能、执行环境、验收或策略版本后，受管会话要求显式迁移。

## 验收、纠错和交付物

`TaskContract` 可以要求指定工具成功执行、指定名称/MIME/大小的交付物、必要段落、
JSON 字段及可解析的来源引用。自定义检查返回 `ResultValidation`，适合验证
“测试是否通过”“查询结果是否满足业务规则”等语义条件。

`ArtifactStore` 存储加密、不可变、内容寻址的文件内容与来源；重复创建同一内容只
产生一个交付物。模型只拿到交付物标识，不能借名称指定任意文件路径。发布到工作区
通过显式 `artifacts.export(...)`，目标环境负责访问权限和预期文件摘要校验。

格式检查不等于内容真实。需要事实核验的任务，应提供可信检查器和
`citation_resolver`，而不是只检查模型写了“成功”。纠错观察来自持久工具结果。
连续重复相同行动且未通过验收时停止；不确定写入不自动重试。

## 执行环境

`ExecutionEnvironment` 统一文件和进程操作，包含实际隔离能力与配置指纹。
`create_environment_tools(environment)` 生成绑定该环境的工具。

* `LocalExecutionEnvironment` 用于可信本机文件操作；不宣称操作系统隔离。
  本机进程只允许显式 `allow_trusted_processes=True`，不能生成模型进程工具。
* `DockerExecutionEnvironment.create(workspace, image=...)` 要求已运行的 Linux
  Docker Engine 和已下载的包含 Python 的镜像。启动时验证引擎、固定镜像 ID、
  非 root 运行；缺失时拒绝，不回退本机执行。
* `include_process=True` 仅允许完整隔离、无网络、工作区只读的环境。容器临时运行，
  不挂载 Docker Socket、主机凭证或其他目录。根文件系统只读、删除 capabilities、
  禁止提权，限制 CPU/内存/进程数/输出/时间；取消后销毁所属容器。

工作区应只包含该任务可以读取的文件。容器的网络和进程隔离不能替代数据权限。
文件读写和命令使用同一个容器环境，不能通过辅助文件工具绕过隔离。
允许写入的业务工具仍须走审批、资源锁和下游 fencing，不能仅声明“支持 fencing”。

## MCP、Skills 和扩展

安装 `.[extensions,mcp]`。支持 MCP SDK 1.x 的 stdio 与 Streamable HTTP；工具发现、
JSON Schema、超时、取消、结果体积、会话关闭和有限重连都受管。仅执行本地
`MCPToolPolicy` 显式允许的工具；服务端注释不能授予只读或免审批权限。
写工具默认需审批且禁止重放。断线后不重发失败调用，重连合同变化须重新装配。

```python
async def search_package(settings, dependencies):
    client = await MCPClient.connect(
        MCPServerConfig("search", command=("python", "trusted_server.py")),
        tool_policies={"lookup": MCPToolPolicy(read_only=True, requires_approval=False)},
    )
    return client.package()
```

扩展 TOML 用 `[[packages]]` 的 name/factory/depends_on/settings 显式注册包；
`[skills] directories=["skills"]` 指定技能目录。`agent.toml` 可以用
`[extensions] config="extensions.toml"` 一次加载。工厂名必须来自应用映射，配置不执行导入。
依赖先于使用方启动，失败时逆序关闭已交付资源。工厂在返回包之前打开的资源由该工厂
负责在异常时关闭；返回 `ExtensionPackage(resources=...)` 后生命周期移交给 Host。

技能只先加载 name/description/version，模型通过 `load_skill` 按需读取正文；
`disable-model-invocation: true` 需业务入口显式用户调用。技能版本变更、路径越界、
YAML 对象或别名不被接受。技能是操作说明，不能授予工具权限。

## 上下文预算和派发审计

`ExecutionPolicy(context_budget=ContextBudget(...))` 在请求前压缩上下文，保留原任务、
标记的事实和工具调用/结果配对，并扣除系统提示、工具描述、输出预留及安全余量。
采用可配置的保守估算，不伪称所有模型的精确 tokenizer。无法容纳时在派发前拒绝。
安全管线改写后的输入再次检查大小；原始对话历史保持完整。

默认 Journal Host 在实际派发边界写入加密 `model_request_snapshot`。它记录动态
上下文、阶段、策略版本、工具描述及生成选项；内置 HTTP Provider 还记录实际请求体。
每次物理重试分别记录，通过 `host.model_runtime.request_audit.load()` 重建。
不保存 API Key/Authorization Header；不能把审计快照中的历史内容当作可信回调反序列化。
调用其他自定义 Provider 时，必须通过共享 Runtime 并实现相应 admission/审计边界，
私自绕开 Runtime 的网络调用不在这些保证内。

## 文档与语义检索

安装 `.[knowledge]`。`SentenceTransformerEmbeddingProvider.create(local_model_path)`
使用管理员预先提供的本地权重，CPU 推理，不下载权重、不执行远程模型代码。
每次调用验证模型文件指纹；模型变更必须重新建立索引。推理与 PDF/DOCX 解析在
有期限的子进程中执行，取消会回收进程。

`DocumentLibrary` 接收可信 `authorize_source` 回调，只有显式 `authorized=True`
才允许导入。支持 UTF-8 文本、Markdown、CSV、JSON、PDF、DOCX；限制输入、解压体积、
页数、文本和分块数。索引、分块和向量加密持久化；检索返回稳定的版本引用。
版本被替换、来源被撤权或删除后，旧引用无法通过核验。删除是停止检索的持久标记，
历史事件的彻底清除遵循既有 Journal 保留/清除机制。

`knowledge_search` 通过正常工具流程执行。给模型注入记忆应继续使用显式授权的
`ExecutionPolicy.transform_context`，按执行阶段、可信租户和会话选择检索范围。

## 持久子任务

`DurableChildSessionManager` 保存子会话标识、收件箱、结果与投递确认。可信工厂接收
`ChildSessionRequest`，返回绑定相同 tenant/session/budget 的通用 Host；所有重启
必须使用相同持久目录、密钥和配置。`enqueue` 原子预留共享预算，`run_next` 执行或
恢复，`results` 获取未确认结果，`acknowledge(message_id, delivery_id)` 去重确认。
`cancel` 写入持久取消标记，在其他控制进程也可观察到。

每个子会话一次只允许一个带 fencing 的控制者；子 Host 还有独占会话写租约。
父、子租约分别续期；父租约已经过期而子写租约尚未过期时，`run_next` 返回 `None`，
收件箱保持待执行，调用方稍后重试。仅暂时占用使用此行为，不支持事务或 fencing 的
后端仍在启动时报错。
相同请求编号绑定同一运行，子任务完成、父任务记录结果前崩溃时，恢复直接读取
原结果，不重复执行。等待审批时保留对应子计划和 approvalIds，审批仍通过子 Host
的身份验证入口，再调用 `run_next` 恢复。

使用 `RuntimeUsageMeter` 和有限 `ClosedLoopBudget` 控制物理模型尝试，包含失败重试。
父预算为每个消息保守预留完整额度，不自动返还失败或未知用量；恢复不会重置额度。
子任务从父级分得的时间受持久总期限约束。`DurableHostWorker` 可接入已有
`MultiAgentOrchestrator`，不改变其 Worker 接口。它不能把未完成或等待审批当作成功。

## 验证方式

基础测试只访问本地临时文件、加密 SQLite 和模拟 HTTP。MCP 测试启动真实本地
服务子进程。用 `PI_AGENT_EMBEDDING_MODEL` 指向预置权重运行神经语义集成检查；
用 `PI_AGENT_DOCKER_IMAGE` 指向本地镜像运行真实隔离检查。没有依赖时这些集成测试
明确跳过，普通测试不自动安装服务或下载镜像/模型。CI 独立集成作业显式准备依赖。

所有基础回归、Ruff、Mypy、分支覆盖率、最低构建器以及安装包 API/示例资产检查
必须通过。具备这些机制仍需要应用针对自身业务正确性、负载、真实模型和部署环境
做上线验收，不能由框架的离线测试替代。

参考接口：[Docker run](https://docs.docker.com/reference/cli/docker/container/run/)、
[无网络容器](https://docs.docker.com/engine/network/drivers/none/)、
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)、
[SentenceTransformer 本地模型接口](https://www.sbert.net/docs/package_reference/sentence_transformer/model.html)。
