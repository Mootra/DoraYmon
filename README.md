# DoraYmon

DoraYmon 是一个 Python QQ Bot 项目。它使用 `botpy` 接入 QQ Bot，命令按插件组织，聊天能力通过 DeepSeek API 提供。项目内置 SQLite 存储、日志、本地运行脚本和部署说明，方便持续扩展机器人功能。

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

主要分层：

- `main.py` 负责启动。
- `doraymon/` 放客户端、路由、配置和日志。
- `plugins/` 放具体命令。
- `services/` 放外部服务调用。
- `storage/` 放 SQLite 连接和数据读写。
- `docs/` 放架构、部署和功能扩展说明。

## 当前功能

- QQ Bot 长连接启动
- 命令路由
- 插件式命令
- DeepSeek `/chat`
- SQLite 签到
- 控制台日志和 `logs/doraymon.log`
- 本地运行脚本
- 宝塔部署文档
- GitHub 初始化文档

## 学习和协作

协作前请先查看本地私人说明 `docs/private/project_goal.md`，了解本项目偏好的教学式、小步推进方式。`docs/private/` 由 `.gitignore` 忽略，不会提交到 Git。

后续扩展私聊入口、长对话、私人记忆库、技能 Prompt 和联网搜索，可以按 [docs/learning_outline.md](docs/learning_outline.md) 逐步推进。

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

如果没有填写 QQBot 配置，启动时会提示补充 `QQBOT_APPID` 和 `QQBOT_SECRET`。

## 配置

复制 `.env.example` 为 `.env`，至少填写：

```bash
QQBOT_APPID=
QQBOT_SECRET=
DEEPSEEK_API_KEY=
```

完整配置项：

```bash
QQBOT_SANDBOX=true
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_TEMPERATURE=0.7
BOT_COMMAND_PREFIX=/
BOT_ADMIN_OPENIDS=
BOT_ENABLE_FOOD_NATURAL_TRIGGER=true
LOG_LEVEL=INFO
DATA_DIR=data
LOG_DIR=logs
```

`.env` 的优先级高于 `config.yaml`。仓库只提供 `config.example.yaml`，真实配置需要在本地创建。

## DeepSeek

`/chat` 命令会请求：

```text
POST https://api.deepseek.com/chat/completions
```

默认模型：

```bash
DEEPSEEK_MODEL=deepseek-v4-flash
```

需要更强模型时，可以改为：

```bash
DEEPSEEK_MODEL=deepseek-v4-pro
```

未配置 `DEEPSEEK_API_KEY` 时，只有 `/chat` 会返回配置缺失提示。

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
/吃什么 今天很累
/记住口味 我喜欢辣
/我的口味
/忘记口味 我喜欢辣
/todo
/admin status
```

私聊可以直接说“今晚吃什么”或“外卖点什么”；群聊需要先 @ 机器人。未 @ 的普通群消息不会触发食物助手。

普通群消息不会自动调用 DeepSeek，以免刷屏和消耗额度。

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

宝塔进程守护管理器配置：

```text
名称：DoraYmon
运行目录：/www/wwwroot/DoraYmon
启动命令：/www/wwwroot/DoraYmon/venv/bin/python /www/wwwroot/DoraYmon/main.py
```

当前使用 `botpy` 长连接，不需要 Nginx 反向代理和域名。改成 Webhook 模式时，再配置域名和 HTTPS。

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

不要提交 `.env`、`config.yaml`、数据库、日志、真实 API Key、QQBot 密钥、真实聊天记录和私有 skill。

## 新增插件

1. 在 `plugins/` 新建文件。
2. 写一个接收 `BotContext` 并返回字符串的 `handle` 函数。
3. 在 `doraymon/router.py` 的 `COMMANDS` 注册命令。
4. 数据库逻辑放 `storage/`，外部 API 放 `services/`。

## 安全注意事项

- 不实现任意 Linux 命令执行。
- 不提供读取 `.env`、`config.yaml`、密钥文件的 QQ 命令。
- 管理员命令必须校验 `BOT_ADMIN_OPENIDS`。
- 返回到 QQ 群的错误信息保持简短，不暴露堆栈和密钥。
- `.env`、`config.yaml`、数据库、日志和私有技能不上传 GitHub。
