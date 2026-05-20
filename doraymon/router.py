from __future__ import annotations

import inspect
import logging
import re
from collections.abc import Awaitable, Callable

from doraymon.context import BotContext
from plugins import admin, chat, fish, fortune, help, pet, ping, sign_in, status, todo, weather


logger = logging.getLogger(__name__)
Handler = Callable[[BotContext], str | Awaitable[str]]


COMMANDS: dict[str, Handler] = {
    "help": help.handle,
    "ping": ping.handle,
    "status": status.handle,
    "chat": chat.handle,
    "天气": weather.handle,
    "今日运势": fortune.handle,
    "签到": sign_in.handle,
    "我的签到": sign_in.handle_my_sign,
    "fish": fish.handle,
    "pet": pet.handle,
    "todo": todo.handle,
    "admin": admin.handle,
}


def normalize_content(content: str) -> str:
    content = (content or "").strip()
    return re.sub(r"^<@!?.+?>\s*", "", content).strip()


def parse_command(content: str, prefix: str) -> tuple[str, str]:
    normalized = normalize_content(content)
    if not normalized.startswith(prefix):
        return "", normalized

    body = normalized[len(prefix) :].strip()
    if not body:
        return "", ""

    command, _, args = body.partition(" ")
    return command.strip(), args.strip()


async def route_message(context: BotContext) -> str:
    command, args = parse_command(context.raw_content, context.settings.command_prefix)
    context.command = command
    context.args = args

    if not command:
        return ""

    handler = COMMANDS.get(command)
    if handler is None:
        return f"未知命令：/{command}\n发送 /help 查看可用命令。"

    try:
        result = handler(context)
        if inspect.isawaitable(result):
            result = await result
        return str(result or "").strip()
    except Exception:
        logger.exception("命令处理失败：%s", command)
        return "命令处理失败，请稍后再试。"
