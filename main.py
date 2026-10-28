from __future__ import annotations

import asyncio
import sys

from doraymon.client import MyClient
from doraymon.config import load_settings
from doraymon.logger import setup_logging
from storage.db import init_all_tables


def _prepare_event_loop() -> asyncio.AbstractEventLoop:
    """Give this thread a current event loop before botpy asks for one.

    botpy calls ``asyncio.get_event_loop()`` while constructing its client, and
    again from ``BotHttp.__del__`` during teardown. That call no longer creates a
    loop implicitly, so on a current Python it raises instead:

        RuntimeError: There is no current event loop in thread 'MainThread'.

    The failure happens before anything is sent, so a perfectly correct AppID and
    secret would look like a broken deployment. The loop is created and installed
    here, and deliberately left open for the process lifetime so teardown also
    finds one.
    """
    # get_event_loop() is the compatible call: with a loop already installed it
    # returns it, and without one it raises, which is the case handled here. The
    # policy API is avoided because it is itself deprecated.
    try:
        return asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop


def main() -> int:
    settings = load_settings()
    logger = setup_logging(settings.log_dir, settings.log_level)

    settings.ensure_runtime_dirs()
    init_all_tables()

    if not settings.qqbot_appid or not settings.qqbot_secret:
        logger.error("QQBot 配置缺失，请复制 .env.example 为 .env 并填写 QQBOT_APPID 和 QQBOT_SECRET。")
        logger.error("可以用 python -m scripts.preflight_official_bot --check 先验证凭证。")
        return 1

    _prepare_event_loop()

    try:
        client = MyClient(settings=settings)
    except Exception:
        logger.exception(
            "创建 QQBot 客户端失败。凭证是否正确可以用 "
            "python -m scripts.preflight_official_bot --check 单独验证。"
        )
        return 1

    logger.info("DoraYmon 正在启动，sandbox=%s", settings.qqbot_sandbox)
    if settings.qqbot_sandbox:
        logger.info(
            "当前是沙箱模式：只能收到沙箱里配置的群/单聊事件，"
            "且沙箱不受 IP 白名单限制，通过沙箱不代表正式环境可用。"
        )
    try:
        client.run(appid=settings.qqbot_appid, secret=settings.qqbot_secret)
    except KeyboardInterrupt:
        logger.info("收到中断，DoraYmon 已停止。")
        return 0
    except Exception:
        logger.exception(
            "QQBot 运行中断。常见原因：AppID/Secret 不正确（错误码 100016）、"
            "AppID 无效或机器人状态异常（100007）、正式环境服务器 IP 不在白名单里。"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
