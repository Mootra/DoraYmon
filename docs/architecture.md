# 架构说明

DoraYmon 是一个独立的 Python QQ Bot 项目。项目不放在 `botpy/examples/` 下，方便单独部署、维护和迁移旧功能。

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
plugins/
  ↓
services/ 或 storage/
  ↓
返回文本给 QQ
```

## 旧功能迁移

| 旧文件 | 新位置 |
| --- | --- |
| `weather_api.py` | `plugins/weather.py` + `services/weather_service.py` |
| `fortune_by_sqlite.py` | `plugins/fortune.py` + `storage/db.py` |
| `sign_in.py` | `plugins/sign_in.py` + `storage/sign_store.py` |
| `user_todo_list.py` | `plugins/todo.py` + `storage/user_store.py` |
| `llm_api.py` | `plugins/chat.py` + `services/deepseek_service.py` |
| `img_upload.py` | `services/image_service.py` |
| `skills/SKILL.md` | `skills/example_skill.md` |
| `friend_text_compact.md` | 本地私有 skills 文件，不上传 GitHub |

迁移时一次只处理一个插件。先拆出存储层或服务层，再接入插件命令，最后补文档和验证步骤。
