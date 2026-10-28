"""Start the QQ account entry (OneBot v11 / NapCat) on its own.

Kept separate from ``main.py`` so the official bot and the account entry can run
independently, each with its own configuration and data directory, as the plan
requires. Nothing here connects anywhere until ``BOT_QQ_ACCOUNT_ENABLED=true``.
QQ defaults to normal chat; ``BOT_QQ_ACCOUNT_ACCESS_MODE=allowlist`` selects
closed acceptance with explicit lists.

Run:
    .venv/bin/python -m scripts.run_account
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from doraymon.account_entry import main as account_main
from doraymon.config import load_settings
from doraymon.logger import setup_logging
from storage.db import init_all_tables, use_data_directory


def main() -> int:
    settings = load_settings()
    logger = setup_logging(settings.log_dir, settings.log_level)

    if not settings.qq_account_enabled:
        logger.error(
            "QQ 账号入口未开启。设置 BOT_QQ_ACCOUNT_ENABLED=true 后重试；"
            "该入口会驱动真实 QQ 号，访问范围由 BOT_QQ_ACCOUNT_ACCESS_MODE 和群列表配置。"
        )
        return 1

    # The account entry keeps its own runtime data so it can run beside the
    # official bot without sharing a database.
    account_data_dir = settings.data_path / "qq_account"
    account_data_dir.mkdir(parents=True, exist_ok=True)
    settings.log_path.mkdir(parents=True, exist_ok=True)

    logger.info("QQ 账号入口使用独立数据目录：%s", account_data_dir)
    with use_data_directory(account_data_dir):
        init_all_tables()
        return account_main(settings)


if __name__ == "__main__":
    raise SystemExit(main())
