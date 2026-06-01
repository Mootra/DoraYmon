# DeepSeek 配置

DoraYmon 使用 DeepSeek API 处理 `/chat` 命令。普通群消息不会自动触发模型请求。

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

## 更强模型

需要更强模型时，可以把模型改为：

```bash
DEEPSEEK_MODEL=deepseek-v4-pro
```

`deepseek-chat` 和 `deepseek-reasoner` 是旧模型名，DeepSeek 官方文档标注将在 2026-07-24 停用。新配置不要再使用这两个名称。

## 安全

- 未配置 `DEEPSEEK_API_KEY` 时，只有 `/chat` 会返回配置缺失提示。
- API 请求失败时，只向 QQ 群返回简短错误。
- 不把完整请求头、API Key 或堆栈信息发到 QQ 群。
