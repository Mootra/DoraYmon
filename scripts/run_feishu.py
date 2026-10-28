"""Start the Feishu entry (official long connection) on its own.

Kept separate from ``main.py`` and from the QQ account entry so each runs with its
own configuration and data directory, as the plan requires. Nothing here connects
anywhere until ``BOT_FEISHU_ENABLED=true`` and an allowlist is configured.

Run:
    .venv/bin/python -m scripts.run_feishu
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from doraymon.config import load_settings
from doraymon.feishu_entry import main as feishu_main
from doraymon.logger import setup_logging
from services.feishu_sdk import build_default_event_source
from storage.db import init_all_tables, use_data_directory


def main() -> int:
    settings = load_settings()
    logger = setup_logging(settings.log_dir, settings.log_level)

    if not settings.feishu_enabled:
        logger.error(
            "飞书入口未开启。设置 BOT_FEISHU_ENABLED=true 后重试；"
            "该入口会连接真实飞书应用，请先确认允许列表。"
        )
        return 1

    # The Feishu entry keeps its own runtime data so it can run beside the QQ
    # entries without sharing a database.
    feishu_data_dir = settings.data_path / "feishu"
    feishu_data_dir.mkdir(parents=True, exist_ok=True)
    settings.log_path.mkdir(parents=True, exist_ok=True)

    logger.info("飞书入口使用独立数据目录：%s", feishu_data_dir)

    # Assembled before the database is opened: a channel that cannot connect
    # should fail before it touches shared state.
    try:
        event_source = build_default_event_source(settings)
    except Exception as error:
        logger.error("飞书长连接未能装配：%s", error)
        return 1

    with use_data_directory(feishu_data_dir):
        init_all_tables()
        return feishu_main(settings, event_source=event_source)


if __name__ == "__main__":
    raise SystemExit(main())
