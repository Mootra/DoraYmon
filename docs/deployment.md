# 部署说明

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

## 宝塔部署

上传项目到：

```bash
/www/wwwroot/DoraYmon
```

创建虚拟环境：

```bash
cd /www/wwwroot/DoraYmon
python3 -m venv venv
source venv/bin/activate
```

安装依赖：

```bash
pip install -r requirements.txt
```

创建配置：

```bash
cp .env.example .env
nano .env
```

至少填写：

```bash
QQBOT_APPID=
QQBOT_SECRET=
DEEPSEEK_API_KEY=
```

短期上下文默认关闭。如需开启，可在 `.env` 中设置：

```bash
BOT_ENABLE_CHAT_HISTORY=true
BOT_CHAT_HISTORY_LIMIT=10
BOT_CHAT_HISTORY_MAX_CONTENT_LENGTH=1000
BOT_CHAT_CONTEXT_MAX_CHARS=6000
```

开启后，运行数据写入本地 SQLite；部署和备份时不要提交或公开真实数据库及聊天记录。

本地知识库默认关闭。将 UTF-8 Markdown/TXT 放入 `resources/knowledge/` 后建立索引：

```bash
python scripts/index_knowledge.py
```

确认索引成功后，在 `.env` 中设置：

```bash
BOT_ENABLE_RAG=true
BOT_KNOWLEDGE_DIR=resources/knowledge
BOT_RAG_TOP_K=3
BOT_RAG_TOKENIZER=trigram
```

知识索引保存在运行数据目录的 `knowledge.db`，不要提交 Git。`/重建知识库` 只允许 `BOT_ADMIN_OPENIDS` 中的管理员使用。

测试运行：

```bash
python main.py
```

宝塔进程守护管理器配置：

```text
名称：DoraYmon
运行目录：/www/wwwroot/DoraYmon
启动命令：/www/wwwroot/DoraYmon/venv/bin/python /www/wwwroot/DoraYmon/main.py
```

当前使用 `botpy` 长连接，不需要 Nginx 反向代理和域名。改成 Webhook 模式时，再配置域名和 HTTPS。
