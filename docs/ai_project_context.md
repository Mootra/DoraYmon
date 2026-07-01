# AI 项目上下文

## 一句话定位

DoraYmon 是一个 Python QQ Bot 项目，使用 `botpy` 接入 QQ，按插件组织命令，`/chat` 通过 DeepSeek 服务完成单轮问答，业务数据使用 SQLite。

## 启动入口

- `main.py` 负责加载配置、初始化日志、创建运行目录、初始化 SQLite 表，并启动 `MyClient`。
- `doraymon/config.py` 从 `.env` 和 `config.yaml` 读取配置，`.env` 优先级更高。
- `storage/db.py` 提供 SQLite 连接和基础表初始化。

## 消息处理链路

```text
QQ 消息
  -> doraymon/client.py
  -> doraymon/router.py
  -> plugins/
  -> services/ 或 storage/
  -> 返回文本给 QQ
```

- 私聊消息会进入 `route_incoming_message(..., fallback_command="chat")`。
- 群聊未 @ 且不是显式命令时直接忽略，避免机器人主动插话。
- 群聊 @ 或显式命令才进入路由。
- 回复发送、长度截断和 QQ API 调用留在 `doraymon/client.py`。

## 路由和插件机制

- `doraymon/router.py` 的 `COMMANDS` 注册命令到插件处理函数。
- `parse_command()` 处理 `/命令 参数`。
- `route_natural_message()` 目前只处理食物自然语言意图。
- 食物自然语言入口优先于私聊 `/chat` 回退；未命中自然语言意图时，私聊普通文本才回退到 `/chat`。
- 插件只接收 `BotContext` 并返回文本，不直接发送 QQ 消息。

## services 层职责

- `services/deepseek_service.py` 封装 DeepSeek API 请求；测试中必须 mock/fake，不能真实联网。
- `services/intent_service.py` 做轻量规则意图识别。
- `services/food_recommend_service.py` 做本地食物推荐，不依赖外部 API。
- 外部 API、模型调用、搜索能力应放在 `services/`，不要散落在插件里。

## storage 层职责

- `storage/db.py` 统一 SQLite 连接和表初始化。
- `storage/food_preference_store.py` 保存用户明确提交的口味偏好。
- `storage/sign_store.py` 保存签到记录。
- storage 层不读取 `.env` 里的密钥，不向 QQ 输出数据库内容。

## 当前主要功能

- QQ Bot 长连接启动。
- 命令路由和插件式命令。
- `/chat` 单轮 DeepSeek 问答。
- `/吃什么` 本地食物推荐。
- 私聊和群聊 @ 场景下的食物自然语言入口。
- 明确口味的保存、查看、删除。
- 签到、状态、帮助、管理员状态命令。
- 天气、待办、钓鱼、宠物等部分功能仍是占位或轻量实现。

## 当前不要做的功能

- 不实现未 @ 群消息主动推荐。
- 不实现联网搜索或外卖平台搜索。
- 不实现无限聊天记录。
- 不自动从普通聊天提取长期记忆。
- 不实现群成员共享私人上下文。
- 不新增需要真实 QQ Bot 或真实 DeepSeek API 才能验证的功能。

## 新增插件推荐步骤

1. 在 `plugins/` 新增插件文件，提供接收 `BotContext` 的 `handle()`。
2. 如果需要外部服务，把调用逻辑放到 `services/`。
3. 如果需要持久化，把 SQLite 读写放到 `storage/`。
4. 在 `doraymon/router.py` 的 `COMMANDS` 注册命令。
5. 增加离线单元测试，优先 mock 外部 API 和 QQ 消息对象。
6. 更新必要文档，说明命令入口、配置项和安全边界。

## 新增测试推荐思路

- 路由测试优先覆盖命令优先级、自然语言意图、私聊回退和群聊忽略规则。
- 插件测试使用 fake `BotContext` 或 `SimpleNamespace`。
- services 测试不要联网；DeepSeek、天气、搜索等必须 mock HTTP 调用。
- storage 测试使用临时 SQLite 数据库，不写入真实 `data/`。
- 测试不能依赖真实 `.env`、QQ Bot token、DeepSeek API Key 或真实聊天记录。

## AI/Codex 修改代码注意事项

- 先读 README、`docs/`、核心代码和现有测试，再决定修改点。
- 只做和当前目标有关的最小必要修改。
- 不重构无关文件，不顺手改格式。
- 不读取、不打印、不提交 `.env`、`config.yaml`、日志、数据库、真实聊天内容或私有 skill。
- 错误返回给 QQ 时保持简短，不暴露堆栈、请求头、密钥或完整配置。
- 新增功能默认应可被本地离线测试验证。
