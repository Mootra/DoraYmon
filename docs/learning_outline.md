# DoraYmon 学习路线

这份文档用来安排 DoraYmon 的后续扩展。节奏是先让项目稳定运行，再逐步加入私聊、上下文、记忆和联网查询。

## 1. 先理解当前项目

当前 DoraYmon 是一个群聊 QQ Bot 骨架：

- `main.py` 启动机器人。
- `doraymon/client.py` 接收 QQ 消息并发送回复。
- `doraymon/router.py` 把命令分发给插件。
- `plugins/` 写具体命令逻辑并返回文本。
- `services/` 调用外部 API，例如 DeepSeek、天气和搜索。
- `storage/` 负责 SQLite 数据库读写。

当前已支持：

- `/help`
- `/ping`
- `/status`
- `/chat 你好`
- `/签到`
- `/我的签到`
- `/admin status`

## 2. 群聊和私聊入口

当前项目主要处理群聊消息：

- `on_group_at_message_create`
- `on_group_message_create`

要支持私聊，可以在 `doraymon/client.py` 增加 C2C 入口：

```python
async def on_c2c_message_create(self, message):
    await self._handle_private_message(message)
```

第一版可以保持简单：

- 群聊只处理 `/` 开头的命令。
- 私聊也先只处理 `/` 开头的命令。
- 普通私聊文本先不自动调用 DeepSeek，避免额度消耗不可控。

后续可以增加配置：

```env
BOT_ENABLE_GROUP=true
BOT_ENABLE_PRIVATE=false
```

## 3. 哪些功能需要 API

不需要外部 API 的功能：

- `/help`
- `/ping`
- `/status`
- `/签到`
- `/我的签到`
- `/todo`

需要业务 API 的功能：

- `/天气 南昌`
- `/金价`
- 图片上传
- 网页截图

需要模型 API 的功能：

- `/chat 你好`
- 总结
- 润色
- 解释代码
- 长对话问答

DeepSeek 负责生成回答，不会自动联网搜索。

## 4. DeepSeek 问答

当前 `/chat` 是一次性问答：

```text
用户发送 /chat 你好
        ↓
plugins/chat.py
        ↓
services/deepseek_service.py
        ↓
DeepSeek API
        ↓
返回模型回复
```

当前不会保存历史消息，也不会记录普通群聊内容。

先保持这几条规则：

- 只有 `/chat` 调用 DeepSeek。
- 普通群消息不调用 DeepSeek。
- DeepSeek API Key 只写在 `.env`。

## 5. 短期上下文

第一阶段做短期上下文记忆，让机器人能参考最近几轮 `/chat`。

目标效果：

```text
/chat 我叫小明
/chat 我刚才说我叫什么？
```

机器人可以根据最近的 `/chat` 内容回答。

实现方式：

- 在 SQLite 新增 `chat_messages` 表。
- 只保存 `/chat` 内容。
- 按 `user_openid` 或 `group_openid` 隔离上下文。
- 每次请求 DeepSeek 前读取最近 6 到 10 条消息。
- 不保存普通群聊消息。

配置项：

```env
BOT_ENABLE_CHAT_HISTORY=false
BOT_CHAT_HISTORY_LIMIT=10
```

默认关闭，需要时再手动开启。

## 6. 私人记忆库

第二阶段做命令式记忆，只在用户明确要求时写入。

推荐命令：

```text
/记住 我喜欢周末晚上写代码
/我的记忆
/忘记 我喜欢周末晚上写代码
/清空记忆
```

实现方式：

- 新增 `plugins/memory.py`。
- 新增 `storage/memory_store.py`。
- SQLite 新增 `user_memories` 表。
- 每条记忆绑定 `user_openid`。
- 只有用户发送 `/记住` 才写入。
- 不从普通聊天里提取隐私。

配置项：

```env
BOT_ENABLE_PRIVATE_MEMORY=false
```

## 7. 群聊共享记忆

群聊共享记忆适合记录群偏好、群规和常用信息。

推荐命令：

```text
/群记住 本群常用称呼是 Dora
/群记忆
/群忘记 本群常用称呼是 Dora
```

权限规则：

- 默认关闭。
- 只允许管理员或白名单用户写入。
- 普通成员可以查看，但不能随意修改。

配置项：

```env
BOT_ENABLE_GROUP_MEMORY=false
```

## 8. 技能和人格 Prompt

`skills/` 目录用来放提示词、人格和技能说明。

公开示例：

```text
skills/example_skill.md
```

私有人格或好友总结放到：

```text
skills/private/
skills/friend_text_compact.md
```

这些路径已被 `.gitignore` 忽略，不会上传 GitHub。

后续可以增加配置：

```env
BOT_SKILL_FILE=skills/example_skill.md
```

## 9. 联网查询

DeepSeek 不会自动联网。要做联网查询，需要先调用搜索服务，再把搜索结果交给模型整理。

推荐流程：

```text
用户：/联网问 今天有什么 AI 新闻
        ↓
services/search_service.py 调用搜索 API
        ↓
拿到标题、摘要、链接
        ↓
按需要抓取网页正文
        ↓
交给 DeepSeek 总结
        ↓
回复答案并附来源链接
```

推荐新增文件：

```text
plugins/search.py
services/search_service.py
services/web_fetch_service.py
```

推荐命令：

```text
/搜索 关键词
/联网问 问题
```

可选搜索服务：

- Brave Search API
- Bing Web Search API
- SerpAPI
- Tavily

不建议直接爬百度、Google、Bing 搜索结果页。搜索结果页容易遇到验证码、封禁和页面结构变化。

配置项：

```env
BOT_ENABLE_WEB_SEARCH=false
SEARCH_PROVIDER=brave
SEARCH_API_KEY=
SEARCH_RESULT_LIMIT=5
WEB_FETCH_TIMEOUT=10
```

## 10. 推荐实现顺序

建议按这个顺序做：

1. 私聊入口。
2. 短期上下文。
3. `/记住`、`/忘记`、`/我的记忆`。
4. `skills/` 人格 Prompt 加载。
5. `/搜索` 只返回搜索结果。
6. `/联网问` 使用搜索结果和 DeepSeek 生成回答。
7. 群聊共享记忆。

每一步先做最小可用版本，再补配置、文档和安全限制。

## 11. 安全边界

始终保持这些规则：

- 不实现任意服务器命令执行。
- 不提供读取 `.env`、`config.yaml`、密钥文件的 QQ 命令。
- 默认不记录完整聊天内容。
- 不上传真实数据库、日志和私有 skill。
- 管理员命令必须校验 `BOT_ADMIN_OPENIDS`。
- 联网查询回答尽量附来源链接。
- API 错误返回给 QQ 群时保持简短，不暴露堆栈和密钥。
