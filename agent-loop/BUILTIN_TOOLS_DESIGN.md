# Python Agent 内置工具系统架构设计

> 状态：总体架构设计已完成；当前已完成工具执行基础、模型/幂等工具重试、运行预算、HybridModelRouter、CapabilityRegistry、Approval Gate 和 RequiredToolCallGuard。
> 目标项目：`agent-loop/`  
> 目标：为现有 Python Agent Loop 增加安全、可测试、可扩展的内置工具系统。  
> 当前教学工具：`add`、`multiply`、`divide`。
> 后续文件工具：`read`、`list_dir`、`write`、`edit`、`shell`、`find`、`grep`。

---

## 1. 先给结论

建议采用下面的分层方式：

```text
Agent Loop
   ↓ 只认识 AgentTool
Tool Factory / Registry
   ↓ 创建并注册工具
Built-in Tools
   ↓ 调用共享能力
Tool Services
   ├─ WorkspacePathPolicy     工作区路径安全
   ├─ FileObservationStore   文件版本观察
   ├─ FileMutationQueue      同文件修改排队
   ├─ AtomicFileWriter       原子写入
   ├─ ProcessRunner          Shell 进程管理
   └─ OutputAccumulator      输出截断与完整输出保存
```

最重要的原则是：

> **不要把文件、Shell、安全和输出逻辑继续塞进 `loop.py`。Agent Loop 仍然只调用统一的 `AgentTool.execute()`。**

内置工具通过工厂函数创建 `AgentTool`，并把共享服务保存在闭包中，因此现有 Agent Loop 不需要大改。

---

## 2. 设计目标

### 2.1 功能目标

内置工具系统需要支持：

- 读取文本文件；
- 列出目录；
- 创建或覆盖文件；
- 对文件做精确局部修改；
- 执行 Shell 命令；
- 查找文件；
- 搜索文件内容；
- 工具参数校验；
- 工具取消和超时；
- 大输出截断；
- 完整输出保存；
- 文件并发修改保护；
- 工作目录边界保护；
- 统一错误类型；
- 单元测试和 Agent Loop 集成测试。

### 2.2 教学目标

代码需要满足：

- 中文注释；
- 每个工具职责单一；
- 不把所有逻辑写进一个大文件；
- 测试无需真实模型；
- 关键安全规则可以单独测试；
- README 能解释每个工具如何工作。

### 2.3 当前阶段不做的功能

第一版暂不实现：

- Docker 或操作系统内核沙箱；
- 网络访问控制；
- MCP；
- 插件动态安装；
- 远程文件系统；
- 多租户；
- 管理员权限提升；
- 图片读取和图片压缩；
- 后台任务系统；
- 完整终端模拟器。

这些以后可以通过扩展能力增加，不应阻塞第一版工具系统。

---

## 3. 与当前 Agent Loop 的关系

当前 Agent Loop 已经支持：

- `AgentTool`；
- `validate_args`；
- `prepare_arguments`；
- `execute`；
- `before_tool_call`；
- `after_tool_call`；
- 串行和并行执行；
- 工具流式 update；
- 工具错误转 ToolResultMessage。

因此工具系统只需要负责“创建高质量的 AgentTool”，不需要重新实现 Agent Loop 的工具调度。

最终使用方式计划如下：

```python
services = ToolServices.create(
    workspace="D:/project",
    security_profile="workspace-write",
)

tools = create_builtin_tools(
    services,
    names=["read", "list_dir", "write", "edit", "shell"],
)

agent = Agent(
    model=model,
    stream_fn=provider.stream,
    tools=tools,
)
```

上面只是接口草图，具体代码后续按实现阶段编写。

---

## 4. 推荐目录结构

```text
agent-loop/
├─ README.md
├─ BUILTIN_TOOLS_DESIGN.md
├─ src/pi_agent_loop/
│  ├─ agent.py
│  ├─ loop.py
│  ├─ types.py
│  └─ tools/
│     ├─ __init__.py
│     ├─ errors.py
│     ├─ services.py
│     ├─ registry.py
│     ├─ validators.py
│     ├─ path_policy.py
│     ├─ observations.py
│     ├─ mutation_queue.py
│     ├─ atomic_writer.py
│     ├─ output.py
│     ├─ process_runner.py
│     └─ builtins/
│        ├─ __init__.py
│        ├─ read.py
│        ├─ list_dir.py
│        ├─ write.py
│        ├─ edit.py
│        ├─ shell.py
│        ├─ find.py
│        └─ grep.py
└─ tests/
   ├─ test_read_tool.py
   ├─ test_list_dir_tool.py
   ├─ test_write_tool.py
   ├─ test_edit_tool.py
   ├─ test_shell_tool.py
   ├─ test_path_policy.py
   ├─ test_file_observations.py
   ├─ test_mutation_queue.py
   ├─ test_output_accumulator.py
   └─ test_builtin_tools_integration.py
```

### 4.1 为什么不把所有工具写在一个文件里

因为以下逻辑会被多个工具共享：

- 路径解析；
- 工作区边界；
- 文件版本；
- 修改排队；
- 输出截断；
- 取消；
- 错误分类。

如果每个工具都自己实现，会出现规则不一致和重复 Bug。

### 4.2 为什么也不拆成很多 Python 包

这些文件属于同一个内置工具子系统，目前没有独立发布需求，所以先放在同一个 `pi_agent_loop.tools` 包中。

这比一开始拆出很多独立安装包更适合教学和小团队维护。

---

## 5. 核心对象设计

## 5.1 `ToolServices`

`ToolServices` 是内置工具共享能力的集合。

计划包含：

```text
workspace_root       工作区根目录
path_policy          路径安全策略
observations         文件版本观察表
mutation_queue       文件修改队列
atomic_writer        原子写服务
process_runner       Shell 进程执行器
output_policy        输出限制配置
spill_store          完整输出临时存储
```

通俗解释：

> 每个工具不自己准备一套文件锁、路径检查和进程管理，而是共同使用一套标准服务。

## 5.2 `BuiltinToolRegistry`

职责：

- 按名字保存工具工厂；
- 检查工具重名；
- 根据 profile 创建工具；
- 返回 `list[AgentTool]`；
- 支持以后注册自定义工具工厂。

它不负责执行工具。真正执行仍由 Agent Loop 完成。

## 5.3 `ToolSecurityProfile`

建议提供三种配置：

| Profile | 中文说明 | 默认工具 |
|---|---|---|
| `read-only` | 只读 | `read`、`list_dir`、`find`、`grep` |
| `workspace-write` | 允许修改工作区文件 | 只读工具 + `write`、`edit` |
| `full-access` | 允许 Shell 和更开放的操作 | 上述工具 + `shell` |

推荐默认使用：

```text
workspace-write
```

但 `shell` 默认不启用，需要用户明确选择。

原因：Shell 可以绕过普通文件工具的版本检查和路径规则，风险明显更高。

---

## 6. 工具统一执行链

Agent Loop 已经提供部分阶段，内置工具系统补充文件和进程安全阶段。

完整链路设计为：

```text
模型给出原始参数
  ↓
prepare_arguments      兼容参数格式
  ↓
validate_args          参数类型校验
  ↓
before_tool_call       外部审批或拦截
  ↓
工具内部安全检查        路径、文件版本、超时
  ↓
execute                真正执行
  ↓
输出规范化              截断、spill、details
  ↓
after_tool_call        扩展修改结果
  ↓
ToolResultMessage      交回模型
```

### 6.1 哪些检查属于 Agent Loop

- 工具是否存在；
- 参数 validator；
- before hook；
- after hook；
- 串行或并行调度；
- 事件产生。

### 6.2 哪些检查属于工具系统

- 文件路径是否在工作区；
- symlink 是否逃出工作区；
- 文件是否被修改；
- 同文件是否并发写；
- Shell cwd 是否合法；
- 命令是否超时；
- 输出是否太大；
- 完整输出保存在哪里。

不要在 Agent Loop 和工具内部重复相同规则。

---

## 7. 第一批内置工具设计

## 7.1 `read`

### 用途

读取文本文件内容。

### 参数

```text
path       文件路径，必填
offset     从第几行开始，默认 1
limit      最多读取多少行，可选
```

### 行为

1. 解析相对路径；
2. 检查路径在工作区；
3. 解析 symlink 后再次检查；
4. 检查目标是普通文件；
5. 读取文本；
6. 默认最多返回 2,000 行或 50 KiB；
7. 记录文件 observation token；
8. 告诉模型下一次从哪个 offset 继续。

### 返回 details

```text
absolutePath       规范路径
lineStart          起始行
lineEnd            结束行
totalLines         文件总行数
truncated          是否截断
observation        文件版本令牌
```

### 安全规则

- 默认禁止读取工作区外路径；
- 不自动读取目录；
- 第一版只支持 UTF-8 文本；
- 二进制文件给出明确错误；
- 文件太大时分段读取。

## 7.2 `list_dir`

### 用途

列出目录直接子项。

### 参数

```text
path       目录路径，默认当前工作区
limit      最大条目数，默认 500
```

### 行为

- 包含普通文件和目录；
- 目录名增加 `/`；
- 按名称排序；
- 返回相对工作区路径；
- 不递归；
- 超过限制时提示继续缩小范围。

### 为什么需要独立工具

如果只有 Shell，模型为了查看目录必须执行命令。独立 `list_dir` 更容易控制权限，也更容易跨平台。

## 7.3 `write`

### 用途

创建新文件，或者完整覆盖已有文件。

### 参数

```text
path                目标文件
content             完整新内容
expectedVersion     可选的文件版本
```

### 推荐安全语义

- 创建新文件：允许；
- 覆盖已有文件：默认要求先通过 `read` 观察；
- 若提供 expectedVersion，必须与当前文件版本一致；
- 不一致时返回 `stale_observation`；
- 自动创建父目录；
- 使用临时文件原子替换；
- 同一文件修改进入 mutation queue。

### 为什么覆盖已有文件要更严格

模型可能基于旧内容生成完整文件。如果用户或其他工具已经修改，直接覆盖会丢失新改动。

## 7.4 `edit`

### 用途

对文件执行一个或多个精确文本替换。

### 参数

```text
path
edits[]
  oldText
  newText
expectedVersion     可选
```

### 行为

1. 必须先读取文件；
2. 检查 observation/version；
3. 每个 oldText 必须唯一；
4. 所有 edit 都对原文件匹配；
5. edit 之间不能重叠；
6. 按位置逆序应用；
7. 保留 BOM；
8. 保留 CRLF 或 LF；
9. 无变化时返回错误；
10. 使用原子写入；
11. 返回 diff 和新版本。

### 第一版是否做 fuzzy match

推荐分两阶段：

- 第一版：只做 exact match；
- 第二版：再增加保守 fuzzy match。

原因：exact match 更容易验证正确性。先把版本检查、并发和原子写做好，再增加智能引号、尾随空白等兼容。

## 7.5 `shell`

### 用途

在工作区中执行 Shell 命令。

### 参数

```text
command      命令文本
timeout      超时秒数，可选
cwd          子工作目录，可选，必须位于工作区
```

### 行为

- Windows 使用 PowerShell 或明确配置的 Shell；
- Linux/macOS 使用 `/bin/bash` 或配置 Shell；
- stdout 和 stderr 都收集；
- 支持流式 update；
- 支持取消；
- 支持超时；
- 尝试终止整个进程树；
- 默认保留最后 2,000 行或 50 KiB；
- 超出部分写入临时文件；
- 返回 exit code 和完整输出路径。

### 安全提醒

`shell` 不是安全沙箱。

即使命令 cwd 在工作区，仍然可能：

- 读取工作区外文件；
- 修改其他目录；
- 访问网络；
- 读取环境变量；
- 启动后台进程。

因此第一版建议：

- 默认不启用；
- 通过 `before_tool_call` 请求用户确认；
- 清除敏感环境变量；
- 文档明确说明它是受信本机代码执行。

## 7.6 `find`

### 用途

按 glob 查找文件路径。

### 参数

```text
pattern      例如 **/*.py
path         搜索根目录，默认工作区
limit        最大结果数
```

### 第一版实现选择

优先使用 Python 标准库：

- `pathlib.Path.glob()`；
- 明确跳过 `.git`、`node_modules`、虚拟环境等目录。

以后可选接入 `fd` 获得更好的 `.gitignore` 行为和性能。

## 7.7 `grep`

### 用途

搜索文件内容。

### 参数

```text
pattern
path
fileGlob
ignoreCase
literal
contextLines
limit
```

### 第一版实现选择

可以先使用 Python：

- `re`；
- 流式逐行读取；
- 文件大小限制；
- 单行长度限制；
- 最大匹配数量。

以后再选择 `ripgrep` 提升性能。

---

## 8. 路径安全设计

## 8.1 基本原则

所有文件工具都必须经过同一个 `WorkspacePathPolicy`。

检查顺序：

```text
用户输入路径
  -> 展开相对路径
  -> 生成绝对路径
  -> 规范化 .. 和分隔符
  -> 对已存在路径解析 realpath/symlink
  -> 检查是否仍位于 workspace root
```

## 8.2 为什么检查两次

只检查字符串前缀不安全。

示例：

```text
workspace/link -> C:/secret
```

用户访问：

```text
workspace/link/password.txt
```

字符串看起来在 workspace，解析 symlink 后实际在外部。因此必须检查真实路径。

## 8.3 新文件路径

新文件本身不存在，无法直接 realpath。

处理方式：

1. 找最近存在的父目录；
2. 解析父目录真实路径；
3. 确认父目录位于 workspace；
4. 再拼接新文件名。

## 8.4 是否允许绝对路径

推荐默认规则：

- 可以输入绝对路径；
- 但绝对路径仍必须位于 workspace；
- `full-access` profile 才允许工作区外路径。

这样模型生成绝对路径不会无故失败，同时仍保持边界。

---

## 9. 文件 Observation 与 CAS

## 9.1 Observation 是什么

`read` 工具读取文件时记录：

```text
规范路径
文件大小
修改时间
内容哈希
```

组合成版本令牌。

通俗解释：

> Agent 读完文件时给它拍一张“版本照片”。修改前再拍一次，确认文件没有被别人动过。

## 9.2 为什么不能只看修改时间

文件系统时间精度可能不足；文件内容变化后大小和时间也可能碰巧相同。

推荐版本令牌：

```text
SHA-256(文件字节)
```

第一版为了简单和可靠，可直接使用内容哈希。

## 9.3 修改前检查

`write` 或 `edit` 在文件锁内：

1. 获取最后一次 observation；
2. 重新读取当前文件；
3. 计算当前哈希；
4. 比较哈希；
5. 不一致就拒绝；
6. 一致才修改。

## 9.4 修改后更新

写入成功后：

- 计算新文件哈希；
- 更新 ObservationStore；
- 在 tool result details 中返回新版本。

---

## 10. 同文件修改队列

## 10.1 问题

两个并行工具同时编辑同一个文件时，可能都读取旧版本，然后互相覆盖。

## 10.2 设计

`FileMutationQueue` 按规范路径保存 `asyncio.Lock`。

```text
不同文件：可以并行
同一个文件：必须排队
```

## 10.3 锁的范围

锁必须覆盖：

```text
读取当前内容
  -> 版本检查
  -> 计算新内容
  -> 写临时文件
  -> 原子替换
  -> 更新 observation
```

不能只锁最后的 `write()`，否则前面的版本检查仍然存在竞态。

## 10.4 取消处理

操作取得锁后，即使收到取消，也不能在底层文件写尚未结束时提前释放锁。

正确方式：

- 每个 await 后检查取消；
- 已经开始的文件系统调用先等待完成；
- 完成或失败后再释放锁。

---

## 11. 原子写入设计

## 11.1 基本流程

```text
在目标文件同目录创建临时文件
  -> 写入完整内容
  -> flush
  -> 可选 fsync
  -> 保留原权限
  -> rename/replace 正式文件
```

## 11.2 为什么临时文件要在同目录

不同磁盘或文件系统之间的 rename 不一定原子。

放在同目录更容易保证：

- 同一个文件系统；
- 原子替换；
- 权限和路径行为一致。

## 11.3 失败处理

- 写临时文件失败：删除临时文件，原文件不变；
- replace 失败：删除临时文件，原文件不变；
- 删除临时文件失败：记录清理错误，但保留主要错误。

---

## 12. 输出截断与 Spill

## 12.1 默认限制

建议沿用 Pi 的教学默认值：

```text
最大 2,000 行
最大 50 KiB
```

## 12.2 Read 工具

保留文件开头，因为模型通常从上往下阅读。

## 12.3 Shell 工具

保留命令输出结尾，因为错误和最终结果通常出现在最后。

## 12.4 Spill 是什么

如果完整输出超过限制：

- 模型只收到截断预览；
- 完整输出保存到临时文件；
- details 返回完整输出路径。

示例：

```text
[输出已截断，显示最后 2,000 行。完整输出：C:/Temp/agent-shell-xxx.log]
```

## 12.5 内存限制

Shell 输出不能先全部存在内存，结束后再截断。

`OutputAccumulator` 应流式：

- 只保留有限尾部；
- 超限后立即把原始 chunk 写入临时文件；
- 最终关闭文件并返回路径。

---

## 13. ProcessRunner 设计

## 13.1 职责

`ProcessRunner` 统一处理：

- Shell 选择；
- cwd；
- 环境变量；
- 子进程创建；
- stdout/stderr；
- timeout；
- cancellation；
- 进程树清理；
- exit code。

`shell` 工具只负责参数校验和结果格式化，不直接堆积跨平台进程细节。

## 13.2 Windows

优先支持：

- PowerShell；
- `CREATE_NEW_PROCESS_GROUP`；
- 必要时 `taskkill /T /F` 清理后代。

## 13.3 Linux/macOS

优先支持：

- `/bin/bash -lc` 或配置 Shell；
- 新 process group/session；
- 取消时向进程组发信号；
- 等待短暂 grace period；
- 仍未退出再强制杀死。

## 13.4 环境变量

默认继承环境前，应允许配置敏感变量黑名单，例如：

- API key；
- access token；
- cloud credential；
- Agent 内部认证信息。

第一版至少提供：

```text
inherit_env=True/False
extra_env={}
blocked_env_names=set()
```

---

## 14. 统一错误模型

建议定义：

```text
ToolError
  code
  message
  path
  details
  cause
```

错误 code：

| Code | 中文含义 |
|---|---|
| `invalid_args` | 参数错误 |
| `path_outside_workspace` | 路径超出工作区 |
| `not_found` | 文件或目录不存在 |
| `not_file` | 目标不是文件 |
| `not_directory` | 目标不是目录 |
| `permission_denied` | 权限不足 |
| `binary_file` | 不支持的二进制文件 |
| `stale_observation` | 文件已被别人修改 |
| `edit_not_unique` | oldText 不唯一 |
| `edit_not_found` | 找不到 oldText |
| `edit_overlap` | 多个修改重叠 |
| `timeout` | 工具超时 |
| `aborted` | 用户取消 |
| `spawn_error` | 进程启动失败 |
| `non_zero_exit` | 命令非零退出 |
| `internal` | 未分类内部错误 |

### 14.1 模型看到什么

模型主要看到简洁、可行动的中文或英文错误文本，例如：

```text
文件已在读取后发生变化，请重新 read 后再 edit。
```

### 14.2 UI 和审计看到什么

结构化 details 保留：

- error code；
- path；
- expected version；
- actual version；
- exit code；
- timeout；
- spill path。

当前 Agent Loop 在工具抛异常时主要保留字符串。正式实现阶段可以决定是否小幅增强 Loop，让 `ToolError` details 也进入错误 ToolResultMessage。

---

## 15. Tool Result 统一格式

每个工具返回现有的 `AgentToolResult`：

```text
content              给模型看的文字或图片
details              给 UI、日志和程序使用的结构化信息
usage                工具自身用量，可选
added_tool_names      动态增加的工具，可选
terminate             是否建议终止，可选
```

### 15.1 `content`

要求：

- 简洁；
- 模型能理解；
- 包含下一步建议；
- 不直接放入超大内容。

### 15.2 `details`

要求：

- 可 JSON 序列化；
- 字段稳定；
- 不包含文件句柄、锁和进程对象；
- 不包含 API key 等秘密。

---

## 16. 工具 Profile 和默认集合

## 16.1 `read-only`

```text
read
list_dir
find
grep
```

适合：

- 代码审查；
- 文档分析；
- 初次运行；
- 不允许修改项目的场景。

## 16.2 `workspace-write`

```text
read
list_dir
find
grep
write
edit
```

适合：

- 普通 Coding Agent；
- 文件修改受工作区和版本检查保护。

## 16.3 `full-access`

```text
workspace-write 的全部工具
shell
```

适合：

- 明确信任模型和工作区；
- 用户了解 Shell 风险；
- before hook 可以审批危险命令。

---

## 17. 测试架构

## 17.1 单元测试

每个公共服务独立测试：

- PathPolicy；
- ObservationStore；
- MutationQueue；
- AtomicWriter；
- OutputAccumulator；
- ProcessRunner；
- 每个内置工具。

## 17.2 集成测试

使用 `ScriptedProvider`：

```text
模型调用 read
  -> read 返回内容
  -> 模型调用 edit
  -> edit 修改文件
  -> 模型生成最终回答
```

## 17.3 必须测试的安全场景

### 路径

- `../` 逃出工作区；
- 绝对路径逃出工作区；
- symlink 指向工作区外；
- 新文件父目录是 symlink；
- Windows 大小写和分隔符。

### 文件修改

- oldText 找不到；
- oldText 出现多次；
- edits 重叠；
- 文件读后被外部修改；
- 两个并行 edit 修改同一文件；
- 原子 replace 失败；
- BOM 和 CRLF 保留。

### Shell

- 正常退出；
- 非零退出；
- timeout；
- 用户取消；
- stdout 很大；
- stderr 很大；
- 子进程创建后代；
- 工作目录不存在。

### 输出

- 按行截断；
- 按字节截断；
- 单行超过 50 KiB；
- UTF-8 多字节字符边界；
- spill 文件完整；
- 临时文件关闭和清理。

## 17.4 测试原则

- 默认测试不依赖网络；
- 默认测试不修改真实项目；
- 每个测试使用临时工作区；
- 测试结束删除临时目录；
- Windows 和 Linux 路径行为分别覆盖；
- 并发测试使用可控 barrier，而不是只依赖 sleep。

---

## 18. 分阶段实现计划

根据依赖关系，推荐按以下顺序编写代码。

## 阶段 A：公共基础设施

实现：

1. `errors.py`；
2. `path_policy.py`；
3. `observations.py`；
4. `mutation_queue.py`；
5. `atomic_writer.py`；
6. 对应单元测试。

完成标准：

- 路径不能逃出工作区；
- 能记录和比较文件版本；
- 同文件修改会排队；
- 原子写失败不破坏旧文件。

## 阶段 B：只读工具

实现：

1. `read`；
2. `list_dir`；
3. 共享 validators；
4. 只读工具测试；
5. Agent Loop 集成测试。

完成标准：

- 假模型能调用 read；
- 读取结果能回灌模型；
- 大文件会截断并提示继续；
- 读取后产生 observation。

## 阶段 C：文件修改工具

实现：

1. `write`；
2. `edit` exact match；
3. CAS；
4. mutation queue；
5. atomic write；
6. diff；
7. 并发和外部修改测试。

完成标准：

- read → edit 完整链路通过；
- 旧版本修改被拒绝；
- 并行修改不会互相覆盖；
- BOM/换行符保持。

## 阶段 D：输出系统和 Shell

实现：

1. `output.py`；
2. spill store；
3. `process_runner.py`；
4. `shell`；
5. timeout/cancel/process tree 测试。

完成标准：

- 正常命令返回；
- 非零退出给出明确错误；
- 超时和取消能终止进程树；
- 大输出只回传尾部并保存完整文件。

## 阶段 E：搜索工具

实现：

1. `find`；
2. `grep`；
3. ignore 规则；
4. 搜索限制和截断。

完成标准：

- 可按 glob 找文件；
- 可按正则和文字搜索；
- 不扫描明显无关的大目录；
- 不因单个不可读文件使整个搜索失败。

## 阶段 F：工具集合与文档

实现：

1. `ToolServices`；
2. `BuiltinToolRegistry`；
3. profile；
4. `create_builtin_tools()`；
5. README 教学；
6. 完整示例。

---

## 19. 需要用户确认的设计选择

正式编写代码前，建议确认以下问题。

### 19.1 工作区边界

推荐默认：所有文件工具只能访问 workspace 内部。

待确认：

- 是否允许 `full-access` 访问工作区外文件？

### 19.2 Shell 默认状态

推荐默认：不启用 Shell，用户明确选择 `full-access` 才启用。

待确认：

- 是否希望 Coding profile 默认包含 Shell？

### 19.3 文件修改规则

推荐默认：覆盖和 edit 已存在文件前必须先 read。

待确认：

- 是否接受“未 read 就拒绝修改”的严格行为？

### 19.4 外部依赖

推荐第一版只使用 Python 标准库。

待确认：

- 参数校验是否允许使用 Pydantic 或 `jsonschema`？

### 19.5 平台优先级

当前开发环境是 Windows。

推荐：

- 第一版同时设计跨平台接口；
- 优先把 Windows 行为测试通过；
- Linux/macOS 使用 CI 或后续环境验证。

待确认：

- 是否要求第一版就完整支持 Linux/macOS？

### 19.6 工具范围

推荐第一批：

```text
read
list_dir
write
edit
shell
```

第二批：

```text
find
grep
```

待确认：

- 是否按这个顺序开发？

---

## 20. 最终推荐方案

当前最合适的实现策略是：

```text
保持现有 Agent Loop 不变
  + 创建 pi_agent_loop.tools 子系统
  + 第一阶段先做公共文件安全基础
  + 第二阶段完成 read/list_dir
  + 第三阶段完成 write/edit
  + 第四阶段完成 shell
  + 最后加入 find/grep 和 profile
```

推荐默认安全配置：

```text
文件工具只允许 workspace
write/edit 已有文件前要求 read
同文件修改串行
写入使用临时文件原子替换
Shell 默认关闭
输出限制 2,000 行或 50 KiB
完整大输出保存到临时文件
```

这套设计结合了：

- Pi 工具定义和输出控制；
- Pi 同文件 mutation queue；
- Pi edit 的模型友好参数；
- DeepSeek Harness 的 observation/CAS；
- DeepSeek Harness 的原子文件发布和安全边界。

后续代码开发应严格按阶段进行，每个阶段先写测试，再写实现，测试通过后再进入下一阶段。
