from __future__ import annotations

import sys

from doraymon.client import MyClient
from doraymon.config import load_settings
from doraymon.logger import setup_logging
from storage.db import init_all_tables


def main() -> int:
    settings = load_settings()
    logger = setup_logging(settings.log_dir, settings.log_level)

    settings.ensure_runtime_dirs()
    init_all_tables()

    if not settings.qqbot_appid or not settings.qqbot_secret:
        logger.error("QQBot 配置缺失，请复制 .env.example 为 .env 并填写 QQBOT_APPID 和 QQBOT_SECRET。")
        return 1

    client = MyClient(settings=settings)
    logger.info("DoraYmon 正在启动，sandbox=%s", settings.qqbot_sandbox)
    client.run(appid=settings.qqbot_appid, secret=settings.qqbot_secret)
    return 0


if __name__ == "__main__":
    sys.exit(main())
