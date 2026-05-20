# DoraYmon

DoraYmon 是一个干净、独立、可维护、可部署 的 Python QQ Bot 项目骨架。它使用 `botpy` 接入 QQ Bot，默认使用 DeepSeek API 提供 LLM 能力，并预留插件、SQLite、日志和旧功能迁移空间。

## 项目结构

```text
DoraYmon/
├── main.py
├── doraymon/
├── plugins/
├── services/
├── storage/
├── data/
├── skills/
├── resources/
├── logs/
├── scripts/
└── docs/
```

## 功能列表

- QQ Bot 长连接启动
- 命令路由
- 插件式命令
- DeepSeek `/chat`
- SQLite 签到
- 日志输出到控制台和 `logs/doraymon.log`
- 本地运行脚本
- 宝塔部署文档
- GitHub 初始化文档

## 本地运行

```bash
cd DoraYmon
python -m venv .venv
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
notepad .env
python main.py
```

Linux/macOS：

```bash
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
python main.py
```

缺少 QQBot 配置时，启动会提示填写 `QQBOT_APPID` 和 `QQBOT_SECRET`。

## .env 配置

复制 `.env.example` 为 `.env`，至少填写：

```bash
QQBOT_APPID=
QQBOT_SECRET=
DEEPSEEK_API_KEY=
```

完整配置：

```bash
QQBOT_SANDBOX=true
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_TEMPERATURE=0.7
BOT_COMMAND_PREFIX=/
BOT_ADMIN_OPENIDS=
LOG_LEVEL=INFO
DATA_DIR=data
LOG_DIR=logs
```

`.env` 的优先级高于 `config.yaml`。仓库只提供 `config.example.yaml`，不会生成真实 `config.yaml`。

## DeepSeek API

`/chat` 命令会请求：

```text
POST https://api.deepseek.com/chat/completions
```

默认模型：

```bash
DEEPSEEK_MODEL=deepseek-chat
```

需要推理模型时可改为：

```bash
DEEPSEEK_MODEL=deepseek-reasoner
```

未配置 `DEEPSEEK_API_KEY` 时，只有 `/chat` 会返回：`DeepSeek API Key 未配置，请检查 .env。`

## 初始命令

```text
/help
/ping
/status
/chat 你好
/天气 南昌
/今日运势
/签到
/我的签到
/todo
/admin status
```

普通群消息默认不会自动调用 DeepSeek，避免刷屏和消耗额度。

## 宝塔部署

上传项目到：

```bash
/www/wwwroot/DoraYmon
```

执行：

```bash
cd /www/wwwroot/DoraYmon
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
python main.py
```

宝塔进程守护管理器：

```text
名称：DoraYmon
运行目录：/www/wwwroot/DoraYmon
启动命令：/www/wwwroot/DoraYmon/venv/bin/python /www/wwwroot/DoraYmon/main.py
```

默认不需要 Nginx 反向代理和域名。后续改 Webhook 模式时，再配置域名和 HTTPS。

## GitHub 上传

```bash
git init
git add .
git commit -m "init DoraYmon qqbot framework"
git branch -M main
git remote add origin <你的仓库地址>
git push -u origin main
```

提交前执行：

```bash
git status
```

确认没有提交 `.env`、`config.yaml`、`*.db`、`logs/`、真实 API Key、QQBot 密钥、真实聊天记录和私有 skill。

## 新增插件

1. 在 `plugins/` 新建文件。
2. 写一个接收 `BotContext` 并返回字符串的 `handle` 函数。
3. 在 `doraymon/router.py` 的 `COMMANDS` 注册命令。
4. 数据库逻辑放 `storage/`，外部 API 放 `services/`。

## 旧项目迁移

| 旧文件 | 新位置 |
| --- | --- |
| `weather_api.py` | `plugins/weather.py` + `services/weather_service.py` |
| `fortune_by_sqlite.py` | `plugins/fortune.py` + `storage/db.py` |
| `sign_in.py` | `plugins/sign_in.py` + `storage/sign_store.py` |
| `user_todo_list.py` | `plugins/todo.py` + `storage/user_store.py` |
| `llm_api.py` | `plugins/chat.py` + `services/deepseek_service.py` |
| `img_upload.py` | `services/image_service.py` |
| `skills/SKILL.md` | `skills/example_skill.md` |
| `friend_text_compact.md` | 本地私有 skills 文件，不默认上传 GitHub |

## 安全注意事项

- 不实现任意 Linux 命令执行。
- 不提供读取 `.env`、`config.yaml`、密钥文件的 QQ 命令。
- 管理员命令必须校验 `BOT_ADMIN_OPENIDS`。
- 返回到 QQ 群的错误信息保持简短，不暴露堆栈和密钥。
- `.env`、`config.yaml`、数据库、日志和私有技能默认不会上传 GitHub。
