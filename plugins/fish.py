from __future__ import annotations

from doraymon.context import BotContext


def handle(context: BotContext) -> str:
    return "钓鱼系统待迁移。后续会接入 plugins/fish.py + storage/fish_store.py。"
