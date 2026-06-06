# 架构说明

DoraYmon 是一个独立的 Python QQ Bot 项目。项目不放在 `botpy/examples/` 下，方便单独部署、维护和扩展功能。

## 分层

- `main.py` 只负责启动。
- `doraymon/client.py` 继承 `botpy.Client`，负责接收消息、调用路由、发送回复。
- `doraymon/router.py` 负责命令分发。
- `plugins/` 放具体命令逻辑。插件返回文本，不直接调用 QQ 发送 API。
- `services/` 放外部服务调用，例如 DeepSeek、天气和图片服务。
- `storage/` 放 SQLite 连接和数据读写，不使用 ORM。
- `data/` 放运行数据和示例数据。真实数据库不提交。
- `skills/` 放提示词、人格和技能文档。私有内容不提交。
- `logs/` 放运行日志。日志文件不提交。

## 数据流

```text
QQ 消息
  ↓
doraymon/client.py
  ↓
doraymon/router.py
  ↓
显式命令或 services/intent_service.py
  ↓
plugins/
  ↓
services/ 或 storage/
  ↓
返回文本给 QQ
```

私聊普通消息会先经过意图识别。明确的用餐决策表达会进入吃什么插件，其他内容回退到 `/chat`。群聊只有显式命令或 @ 机器人时才进入这条流程，未 @ 的普通群消息不会自动触发插件。

## 扩展原则

新增功能时一次只处理一个插件。先确定命令入口，再拆出存储层或服务层，最后接入路由并补充验证步骤。
