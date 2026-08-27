# 本地配置说明

仓库只提交 `*.toml.example`，真实 `*.toml` 配置由每位开发者在本机创建。

Windows：

```bat
copy config\agent.toml.example config\agent.toml
copy config\providers.toml.example config\providers.toml
copy config\business.toml.example config\business.toml
```

Linux/macOS：

```bash
cp config/agent.toml.example config/agent.toml
cp config/providers.toml.example config/providers.toml
cp config/business.toml.example config/business.toml
```

然后编辑 `config/providers.toml`，填写第三方平台提供的 Base URL、API Key 和模型 ID。若本地文件创建于 Retry 功能之前，可从 `providers.toml.example` 复制 `[profiles.third_party.retry]` 分区；缺少该分区时模型重试默认关闭。

`business.toml.example` 是当前唯一业务配置，不需要正则表达式，由 HybridModelRouter 理解自然语言。接入真实产品前，请先填写项目根目录的 `BUSINESS_REQUIREMENTS.md`，再让 AI 根据它生成可以直接加载的业务配置和工具代码。

安全规则：

- 不要把真实 API Key 写入 `.example`；
- 不要把 Key 打印到终端、异常、日志、测试快照或聊天记录；
- 提交前执行 `git diff --cached --name-only`；
- 如果 Key 曾经进入 Git 历史，应立即撤销并轮换，删除最新文件并不能清除历史秘密。
