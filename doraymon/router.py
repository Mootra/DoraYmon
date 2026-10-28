from __future__ import annotations

import inspect
import logging
import re
from collections.abc import Awaitable, Callable

from doraymon.context import BotContext
from plugins import (
    admin,
    chat,
    fish,
    food,
    fortune,
    help,
    knowledge,
    pet,
    ping,
    sign_in,
    silence,
    status,
    todo,
    weather,
)
from services.intent_service import FOOD_INTENT, detect_intent, redirects_meal_to_chat
from services.meal_session import MEAL_STATES
from services.pending_reply import PendingReply
from services.session_history import get_session_key
from services.silence import SILENCE_COMMAND, should_reply


logger = logging.getLogger(__name__)
Handler = Callable[[BotContext], str | Awaitable[str]]


COMMANDS: dict[str, Handler] = {
    "help": help.handle,
    "ping": ping.handle,
    "status": status.handle,
    "chat": chat.handle,
    "清空上下文": chat.handle_clear_context,
    "上下文状态": chat.handle_context_status,
    "上下文摘要": chat.handle_context_summary,
    "知识问": knowledge.handle_ask,
    "知识库状态": knowledge.handle_status,
    "知识来源": knowledge.handle_sources,
    "重建知识库": knowledge.handle_rebuild,
    "天气": weather.handle,
    "今日运势": fortune.handle,
    "签到": sign_in.handle,
    "我的签到": sign_in.handle_my_sign,
    "吃什么": food.handle,
    "记住口味": food.handle_remember_preference,
    "我的口味": food.handle_my_preferences,
    "忘记口味": food.handle_forget_preference,
    "fish": fish.handle,
    "pet": pet.handle,
    "todo": todo.handle,
    "admin": admin.handle,
    "静默": silence.handle,
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

    # Silence blocks ordinary replies but never the silence switch itself: a
    # control that cannot be released through the same channel would force a
    # restart to undo, which is the opposite of what it is for.
    if command != SILENCE_COMMAND and not should_reply(context.settings):
        logger.info("已静默，跳过命令：%s", command)
        return ""

    try:
        return await _run_handler(handler, context)
    except Exception:
        logger.exception("命令处理失败：%s", command)
        return "命令处理失败，请稍后再试。"


async def route_natural_message(context: BotContext) -> str:
    if not should_reply(context.settings):
        return ""
    normalized = normalize_content(context.raw_content)
    if redirects_meal_to_chat(normalized):
        # Keep explicit direction changes ahead of all old meal follow-ups,
        # including a budget/party-size phrase in the same message.
        session = get_session_key(context)
        if session is not None:
            MEAL_STATES.forget(session)
        return ""
    intent = detect_intent(normalized)
    follows_food = intent is None and food.is_follow_up(context, normalized)
    if intent is None and not follows_food:
        return ""

    if follows_food or intent.name == FOOD_INTENT:
        context.command = "吃什么"
        context.args = normalized
        try:
            return await _run_handler(food.handle, context)
        except Exception:
            logger.exception("自然语言意图处理失败：%s", FOOD_INTENT)
            return "食物推荐处理失败，请稍后再试。"

    return ""


async def route_incoming_message(
    context: BotContext,
    fallback_command: str = "",
) -> str:
    normalized = normalize_content(context.raw_content)
    prefix = context.settings.command_prefix

    if not normalized:
        return ""

    if normalized.startswith(prefix):
        return await route_message(context)

    if context.group_openid and not (context.is_at_message or context.addressed_to_bot):
        return ""

    if context.settings.food_natural_trigger_enabled:
        natural_reply = await route_natural_message(context)
        if natural_reply:
            return natural_reply

    if not fallback_command and (context.is_at_message or context.addressed_to_bot):
        fallback_command = "chat"

    if not fallback_command:
        return ""

    context.raw_content = f"{prefix}{fallback_command} {normalized}".strip()
    return await route_message(context)


async def _run_handler(handler: Handler, context: BotContext) -> str:
    result = handler(context)
    if inspect.isawaitable(result):
        result = await result
    text = str(result or "").strip()
    return result.with_text(text) if isinstance(result, PendingReply) else text
