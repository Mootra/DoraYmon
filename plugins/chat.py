from __future__ import annotations

import asyncio
import logging

from doraymon.context import BotContext
from services.deepseek_service import DEFAULT_SYSTEM_PROMPT, DeepSeekService
from storage.chat_history_store import (
    ChatMessage,
    clear_messages,
    count_messages,
    init_chat_history_table,
    list_recent_messages,
    save_message,
)


logger = logging.getLogger(__name__)
DEEPSEEK_ERROR_PREFIX = "DeepSeek "


async def handle(context: BotContext) -> str:
    prompt = context.args.strip()
    if not prompt:
        return "请在 /chat 后面输入要发送给 DeepSeek 的内容，例如：/chat 你好"

    service = DeepSeekService(context.settings)
    if not context.settings.chat_history_enabled:
        return await asyncio.to_thread(service.chat, prompt)

    session = _get_session(context)
    if session is None:
        return await asyncio.to_thread(service.chat, prompt)

    scope_type, scope_openid, user_openid = session
    history_limit = max(0, int(context.settings.chat_history_limit))
    max_content_length = max(1, int(context.settings.chat_history_max_content_length))

    try:
        init_chat_history_table()
        history = list_recent_messages(
            scope_type,
            scope_openid,
            user_openid,
            history_limit,
        )
    except Exception:
        logger.exception("读取聊天历史失败")
        return await asyncio.to_thread(service.chat, prompt)

    messages = _build_messages(history, prompt)
    reply = await asyncio.to_thread(service.chat_messages, messages)
    if _is_deepseek_error(reply):
        return reply

    try:
        save_message(
            scope_type,
            scope_openid,
            user_openid,
            "user",
            prompt,
            max_content_length=max_content_length,
        )
        save_message(
            scope_type,
            scope_openid,
            user_openid,
            "assistant",
            reply,
            max_content_length=max_content_length,
        )
    except ValueError:
        logger.warning("聊天历史包含不允许保存的内容，已跳过保存")
    except Exception:
        logger.exception("保存聊天历史失败")

    return reply


def handle_clear_context(context: BotContext) -> str:
    session = _get_session(context)
    if session is None:
        return "无法识别当前会话，暂时不能清空上下文。"

    scope_type, scope_openid, user_openid = session
    try:
        init_chat_history_table()
        removed_count = clear_messages(scope_type, scope_openid, user_openid)
    except Exception:
        logger.exception("清空聊天上下文失败")
        return "清空上下文失败，请稍后再试。"

    return f"当前{_session_label(scope_type)}短期上下文已清空，共清理 {removed_count} 条消息。"


def handle_context_status(context: BotContext) -> str:
    session = _get_session(context)
    if session is None:
        return "无法识别当前会话，暂时不能查看上下文状态。"

    scope_type, scope_openid, user_openid = session
    try:
        init_chat_history_table()
        message_count = count_messages(scope_type, scope_openid, user_openid)
    except Exception:
        logger.exception("读取聊天上下文状态失败")
        return "上下文状态读取失败，请稍后再试。"

    enabled_text = "开启" if context.settings.chat_history_enabled else "关闭"
    history_limit = max(0, int(context.settings.chat_history_limit))
    return "\n".join(
        [
            "短期上下文状态：",
            f"是否开启：{enabled_text}",
            f"当前会话类型：{scope_type}",
            f"当前会话已保存消息数量：{message_count}",
            f"读取历史上限：{history_limit}",
        ]
    )


def _get_session(context: BotContext) -> tuple[str, str, str] | None:
    user_openid = str(context.user_openid or "").strip()
    if not user_openid:
        return None

    group_openid = str(context.group_openid or "").strip()
    if group_openid:
        return "group", group_openid, user_openid
    return "private", user_openid, user_openid


def _build_messages(history: list[ChatMessage], prompt: str) -> list[dict[str, str]]:
    messages = [{"role": "system", "content": DEFAULT_SYSTEM_PROMPT}]
    messages.extend(
        {"role": message.role, "content": message.content}
        for message in history
        if message.role in {"user", "assistant"} and message.content
    )
    messages.append({"role": "user", "content": prompt})
    return messages


def _is_deepseek_error(reply: str) -> bool:
    return str(reply or "").startswith(DEEPSEEK_ERROR_PREFIX)


def _session_label(scope_type: str) -> str:
    return "群聊个人会话" if scope_type == "group" else "私聊会话"
