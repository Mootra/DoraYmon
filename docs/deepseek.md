# DeepSeek 配置

DoraYmon 默认使用 DeepSeek API，不默认使用 OpenAI。

## .env 配置

```bash
DEEPSEEK_API_KEY=你的 DeepSeek API Key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_TEMPERATURE=0.7
```

默认接口：

```text
POST https://api.deepseek.com/chat/completions
```

请求格式使用 OpenAI-compatible chat completions 格式。

## deepseek-reasoner

如果后续需要推理模型，可以改为：

```bash
DEEPSEEK_MODEL=deepseek-reasoner
```

## 安全

- 未配置 `DEEPSEEK_API_KEY` 时，只有 `/chat` 会返回配置缺失提示。
- API 请求失败时，只向 QQ 群返回简短错误。
- 不会把完整请求头、API Key 或堆栈信息发到 QQ 群。
