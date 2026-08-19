# DoraYmon 项目说明

## 项目定位

DoraYmon 是一个插件化的 Python QQ AI 助手，使用 botpy 接入 QQ，并通过 DeepSeek 提供大模型对话。

## 短期上下文

短期上下文默认关闭。管理员可通过环境变量 `BOT_ENABLE_CHAT_HISTORY=true` 开启。历史消息按私聊用户或“群 + 用户”隔离，不会自动成为长期记忆或知识库。

## 本地运行

在 Windows PowerShell 中运行 `scripts/run_local.ps1` 可以创建虚拟环境、安装依赖并启动 DoraYmon。首次启动前需要在 `.env` 中填写 QQ Bot 配置；需要 AI 对话时还要填写 DeepSeek API Key。

## 当前 AI 能力边界

当前项目支持 DeepSeek 对话、可控短期上下文、规则式食物意图识别，以及基于 SQLite FTS5 的本地知识问答和来源引用。Embedding、向量检索、长期个人记忆和 Agent 尚未实现。
