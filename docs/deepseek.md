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

chat 插件默认带当前会话的有限短期上下文；如需退回单轮请求，可显式关闭：

```bash
BOT_ENABLE_CHAT_HISTORY=true
BOT_CHAT_HISTORY_LIMIT=40
BOT_CHAT_HISTORY_MAX_CONTENT_LENGTH=4000
BOT_CHAT_CONTEXT_MAX_CHARS=6000
BOT_CHAT_CONTEXT_SUMMARY_MAX_CHARS=1200
BOT_CHAT_CONTEXT_TTL_MINUTES=720
BOT_CHAT_SUMMARY_RETENTION_DAYS=7
BOT_REPLY_MAX_SEGMENTS=3
BOT_REPLY_MAX_TOTAL_CHARS=4000
```

历史按私聊用户或“群 + 用户”隔离，只把完整问答轮次发送给模型。`BOT_CHAT_CONTEXT_MAX_CHARS` 控制历史总字符预算；超出预算时，旧轮次后台优先由低温度模型压缩，失败时退回本地提取式节选，最近轮次保留原文。`BOT_CHAT_CONTEXT_TTL_MINUTES` 按最后活跃时间让闲置会话整体过期，设为 `0` 可关闭自动过期；`BOT_CHAT_SUMMARY_RETENTION_DAYS` 控制后台摘要保留时间。明确以“换个话题”“忽略之前的对话”“重新开始”等表达开头时，当前会话从新话题重新开始；普通转折表达不会触发清空。`/上下文状态` 不返回聊天正文，`/上下文摘要` 优先返回后台摘要，没有时退回本地节选，`/清空上下文` 只清理当前会话。聊天历史不会自动成为知识库或长期记忆。普通聊天开启 RAG 时只注入相关度足够高的前 1～2 条资料；严格 `/知识问` 继续执行资料不足拒答并附实际来源。

模型参数按场景区分：无可选资料的普通聊天使用配置温度加 0.1（上限 2.0）和 `max_tokens=8192`；含可选资料的普通聊天及严格 `/知识问` 将温度限制为不高于 0.4，使用 `max_tokens=4096`。模型名称仍由 `DEEPSEEK_MODEL` 控制，不自动切换 pro。完整回复约束见 [docs/reply_style.md](reply_style.md)。

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
