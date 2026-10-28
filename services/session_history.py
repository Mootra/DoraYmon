"""Shared short-term history policy for conversational plugins."""
from __future__ import annotations

import logging

from doraymon.context import BotContext
from doraymon.identity import IdentityNamespace, SessionKey, namespace_for
from services.context_builder import is_explicit_topic_switch
from services.meal_session import MEAL_STATES
from services.session_revisions import SESSION_REVISIONS
from storage.chat_history_store import (
    ChatMessage, clear_messages, expire_messages, init_chat_history_table,
    list_recent_messages, save_turn,
)
from storage.chat_summary_store import (
    clear_summary as clear_persisted_summary,
    get_summary,
    init_chat_summary_table,
)

logger = logging.getLogger(__name__)


def context_namespace(context: BotContext) -> IdentityNamespace:
    """The platform plus bot account that saw this message."""
    return namespace_for(
        getattr(context, "platform", "") or "",
        getattr(context, "bot_account", "") or "",
    )


def get_session_key(context: BotContext) -> SessionKey | None:
    """Storage identity for this message, namespaced by platform and bot account."""
    return context_namespace(context).session_key(
        context.user_openid, context.group_openid,
    )


def generate_revision(context: BotContext) -> int | None:
    """Capture the revision an in-flight reply is being generated against."""
    session = get_session_key(context)
    if session is None:
        return None
    return SESSION_REVISIONS.begin(session)


def reply_allowed(context: BotContext, revision: int | None) -> bool:
    """True when a generated reply may still be written back to history.

    False means the conversation was cleared, switched or expired while the
    model was answering, so that answer belongs to a conversation that no
    longer exists.
    """
    if revision is None:
        return True
    session = get_session_key(context)
    if session is None:
        return True
    return SESSION_REVISIONS.is_current(session, revision)


def read_history(context: BotContext, prompt: str) -> list[ChatMessage]:
    session = get_session_key(context)
    if not context.settings.chat_history_enabled or session is None:
        return []
    try:
        init_chat_history_table()
        removed = expire_messages(*session, context.settings.chat_context_ttl_minutes)
        if is_explicit_topic_switch(prompt):
            MEAL_STATES.forget(session)
            clear_messages(*session)
            try:
                clear_summary(*session)
            except Exception:
                logger.exception("换话题时清理聊天摘要失败")
            return []
        if removed:
            MEAL_STATES.forget(session)
            # Expiry retired the conversation; in-flight replies must not revive it.
            SESSION_REVISIONS.bump(session)
        return list_recent_messages(*session, max(0, int(context.settings.chat_history_limit)))
    except Exception:
        logger.exception("读取聊天历史失败，已退化为无历史对话")
        return []


def read_summary(context: BotContext) -> str:
    session = get_session_key(context)
    if not context.settings.chat_history_enabled or session is None:
        return ""
    try:
        init_chat_summary_table()
        summary = get_summary(*session)
        return summary.summary if summary is not None else ""
    except Exception:
        logger.exception("读取聊天摘要失败，已退化为无摘要对话")
        return ""


def clear_summary(scope_type: str, scope_openid: str, user_openid: str) -> int:
    return clear_persisted_summary(scope_type, scope_openid, user_openid)


def clear_history(context: BotContext) -> int:
    """Clear one conversation and retire every generation started before it."""
    session = get_session_key(context)
    if session is None:
        raise ValueError("session identity is unavailable")
    init_chat_history_table()
    removed = clear_messages(*session)
    clear_summary(*session)
    MEAL_STATES.forget(session)
    SESSION_REVISIONS.bump(session)
    return removed


def save_history(
    context: BotContext, prompt: str, reply: str,
) -> tuple[ChatMessage, ChatMessage] | None:
    session = get_session_key(context)
    if not context.settings.chat_history_enabled or session is None:
        return None
    try:
        return save_turn(
            *session,
            prompt,
            reply,
            max_content_length=max(1, int(context.settings.chat_history_max_content_length)),
            retain_messages=max(0, int(context.settings.chat_history_limit)),
        )
    except ValueError:
        logger.warning("聊天历史包含不允许保存的内容，已跳过保存")
        return None
    except Exception:
        logger.exception("保存聊天历史失败")
        return None
