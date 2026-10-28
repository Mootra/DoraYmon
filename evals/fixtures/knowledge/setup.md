# 本地安装与启动

首次运行需要 Python 3.10 或更高版本。创建虚拟环境后，执行 `python -m pip install -r requirements.txt` 安装依赖。

把 `.env.example` 复制成 `.env`，在 `.env` 中填写 QQ 机器人的 `QQBOT_APPID` 和 `QQBOT_SECRET`。测试环境还应设置 `QQBOT_SANDBOX=true`。

使用 `python main.py` 启动机器人。启动后在 QQ 私聊发送 `/ping`，收到 `pong` 表示基本连接和命令路由正常。
