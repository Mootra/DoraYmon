# 宝塔部署 DoraYmon

## 1. 上传项目

把项目上传到：

```bash
/www/wwwroot/DoraYmon
```

## 2. 创建虚拟环境

```bash
cd /www/wwwroot/DoraYmon
python3 -m venv venv
source venv/bin/activate
```

## 3. 安装依赖

```bash
pip install -r requirements.txt
```

## 4. 创建配置

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

## 5. 测试运行

```bash
python main.py
```

## 6. 配置进程守护

宝塔进程守护管理器配置：

```text
名称：DoraYmon
运行目录：/www/wwwroot/DoraYmon
启动命令：/www/wwwroot/DoraYmon/venv/bin/python /www/wwwroot/DoraYmon/main.py
```

## 7. 域名说明

当前使用 `botpy` 长连接，不需要 Nginx 反向代理和域名。改成 Webhook 模式时，再配置域名和 HTTPS。
