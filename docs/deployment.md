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

填入：

```bash
QQBOT_APPID=
QQBOT_SECRET=
DEEPSEEK_API_KEY=
```

测试运行：

```bash
python main.py
```

宝塔进程守护管理器：

```text
名称：DoraYmon
运行目录：/www/wwwroot/DoraYmon
启动命令：/www/wwwroot/DoraYmon/venv/bin/python /www/wwwroot/DoraYmon/main.py
```

这个 Bot 默认不需要 Nginx 反向代理和域名。如果后续改成 Webhook 模式，再单独配置域名和 HTTPS。
