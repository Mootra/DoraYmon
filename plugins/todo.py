from __future__ import annotations

from doraymon.context import BotContext


def handle(context: BotContext) -> str:
    return "待办功能待迁移。后续会接入 plugins/todo.py + storage/user_store.py。"
