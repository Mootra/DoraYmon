# DeepSeek 配置

DoraYmon 使用 DeepSeek API 作为回答生成层。显式 `/chat`、私聊普通文本以及群聊 @ 后未命中规则意图的普通文本会进入 chat 插件；未 @ 且不是显式命令的群消息不会触发模型请求。

## 配置项

```bash
DEEPSEEK_API_KEY=你的 DeepSeek API Key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_TEMPERATURE=0.7
```

`DEEPSEEK_MODEL` 的代码默认值是 `deepseek-v4-flash`。如果 `.env` 写了其他模型，会以 `.env` 为准。

请求地址：

```text
POST https://api.deepseek.com/chat/completions
```

请求格式使用 OpenAI-compatible chat completions 格式。

## 短期上下文

默认情况下，chat 插件发送单轮请求。需要近期对话时可显式开启：

```bash
BOT_ENABLE_CHAT_HISTORY=true
BOT_CHAT_HISTORY_LIMIT=10
BOT_CHAT_HISTORY_MAX_CONTENT_LENGTH=1000
BOT_CHAT_CONTEXT_MAX_CHARS=6000
```

历史按私聊用户或“群 + 用户”隔离，只把最近的完整问答轮次发送给模型。`BOT_CHAT_CONTEXT_MAX_CHARS` 控制历史总字符预算，数据库按 `BOT_CHAT_HISTORY_LIMIT` 清理旧消息。`/上下文状态` 只显示开关、会话类型、消息数、读取上限和字符预算；`/清空上下文` 只清理当前会话。聊天历史不会自动成为知识库或长期记忆。

DeepSeek 在当前项目中只负责生成回答。`/知识问` 会先由 SQLite FTS5 检索本地知识块，再把受限上下文交给 DeepSeek，并由插件附上实际检索来源。开启 RAG 后，普通聊天也会通过统一对话编排器尝试检索；检索失败时退化为普通聊天，短追问会借用上一轮用户问题帮助检索。Embedding 和向量检索尚未实现，不能把当前基线宣传为向量 RAG。

## 更强模型

需要更强模型时，可以把模型改为：

```bash
DEEPSEEK_MODEL=deepseek-v4-pro
```

`deepseek-chat` 和 `deepseek-reasoner` 是旧模型名，DeepSeek 官方文档标注将在 2026-07-24 停用。新配置不要再使用这两个名称。

## 安全

- 未配置 `DEEPSEEK_API_KEY` 时，进入 AI 聊天的消息会返回配置缺失提示。
- API 请求失败时，只向 QQ 群返回简短错误。
- 不把完整请求头、API Key 或堆栈信息发到 QQ 群。
