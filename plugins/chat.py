from __future__ import annotations

import asyncio
import logging

from doraymon.context import BotContext
from services.blocking_io import BlockingIOBusy, MODEL_IO
from services.conversation_service import ConversationService
from services.context_builder import (
    build_context,
    summarize_history,
)
from services.deepseek_service import DeepSeekService
from services.pending_reply import stage_reply
from services.session_history import (
    clear_history,
    generate_revision,
    get_session_key as _get_session,
    read_history,
    read_summary,
    reply_allowed,
)
from services.summary_service import update_persisted_summary
from storage.chat_history_store import (
    count_messages,
    expire_messages,
    init_chat_history_table,
    list_recent_messages,
)


logger = logging.getLogger(__name__)
_SUMMARY_TASKS: set[asyncio.Task] = set()


async def handle(context: BotContext) -> str:
    prompt = context.args.strip()
    if not prompt:
        return "请在 /chat 后面输入要发送给 DeepSeek 的内容，例如：/chat 你好"

    service = DeepSeekService(context.settings)
    history = read_history(context, prompt)
    stored_summary = read_summary(context)
    # Fence: a clear, topic switch or expiry during the model call retires this
    # revision, and the answer is then neither written back nor sent.
    revision = generate_revision(context)

    conversation = ConversationService(context.settings, model_service=service)
    try:
        ctx = await MODEL_IO.run(
            build_context, context.settings, prompt,
            scene="group" if context.group_openid else "private",
            user_openid=context.user_openid, group_openid=context.group_openid,
            history=history, summary=stored_summary, rag_service=conversation.rag_service,
            group_dialogue=context.group_dialogue,
        )
        response = await MODEL_IO.run(conversation.answer, prompt, ctx)
    except BlockingIOBusy:
        return "模型请求正忙，请稍后再试。"
    reply = response.answer
    if response.is_error:
        return reply

    if not reply_allowed(context, revision):
        logger.info("会话在生成期间被清空或重置，已丢弃本次回复，不写入历史")
        return ""

    return stage_reply(
        context, prompt, reply, revision,
        on_saved=lambda turn: _schedule_summary_update(
            context, history + list(turn), service,
        ),
    )


def handle_clear_context(context: BotContext) -> str:
    session = _get_session(context)
    if session is None:
        return "无法识别当前会话，暂时不能清空上下文。"

    scope_type = session[0]
    try:
        removed_count = clear_history(context)
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
        expire_messages(
            scope_type,
            scope_openid,
            user_openid,
            context.settings.chat_context_ttl_minutes,
        )
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
            f"上下文字符预算：{max(200, int(context.settings.chat_context_max_chars))}",
            f"上下文过期时间：{_ttl_label(context.settings.chat_context_ttl_minutes)}",
        ]
    )


def handle_context_summary(context: BotContext) -> str:
    session = _get_session(context)
    if session is None:
        return "无法识别当前会话，暂时不能查看上下文摘要。"

    scope_type, scope_openid, user_openid = session
    try:
        init_chat_history_table()
        expire_messages(
            scope_type,
            scope_openid,
            user_openid,
            context.settings.chat_context_ttl_minutes,
        )
        history = list_recent_messages(
            scope_type,
            scope_openid,
            user_openid,
            max(0, int(context.settings.chat_history_limit)),
        )
    except Exception:
        logger.exception("读取聊天上下文摘要失败")
        return "上下文摘要读取失败，请稍后再试。"

    summary = summarize_history(
        history,
        max_chars=min(
            1200,
            max(200, int(context.settings.chat_context_summary_max_chars)),
        ),
    )
    if not summary:
        summary = read_summary(context)
    if not summary:
        return f"当前{_session_label(scope_type)}还没有可用的完整问答上下文。"
    return f"当前{_session_label(scope_type)}：\n{summary}"


def _schedule_summary_update(
    context: BotContext,
    history,
    model_service: DeepSeekService,
) -> None:
    session = _get_session(context)
    if session is None:
        return
    if sum(len(message.content) for message in history) <= max(
        200, int(context.settings.chat_context_max_chars)
    ):
        return
    task = asyncio.create_task(
        MODEL_IO.run(
            update_persisted_summary,
            context.settings,
            session,
            history,
            model_service,
        )
    )
    _SUMMARY_TASKS.add(task)
    owned = context.background_tasks
    if owned is not None:
        owned.add(task)

    def finished(done: asyncio.Task) -> None:
        _SUMMARY_TASKS.discard(done)
        if owned is not None:
            owned.discard(done)
        if not done.cancelled() and done.exception() is not None:
            logger.error("后台摘要任务失败：%s", type(done.exception()).__name__)

    task.add_done_callback(finished)


def _session_label(scope_type: str) -> str:
    return "群聊个人会话" if scope_type == "group" else "私聊会话"


def _ttl_label(ttl_minutes: int) -> str:
    normalized_ttl = int(ttl_minutes)
    if normalized_ttl <= 0:
        return "不自动过期"
    return f"{normalized_ttl} 分钟"
