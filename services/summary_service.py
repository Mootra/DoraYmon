from __future__ import annotations

import logging

from doraymon.config import Settings
from services.context_builder import summarize_history
from services.deepseek_service import DeepSeekService, is_model_error
from storage.chat_history_store import ChatMessage
from storage.chat_summary_store import save_summary_if_current

logger = logging.getLogger(__name__)


def generate_summary(
    history: list[ChatMessage],
    model_service: DeepSeekService,
    max_chars: int,
) -> str:
    normalized_max_chars = max(0, int(max_chars))
    if normalized_max_chars <= 0:
        return ""

    fallback = summarize_history(history, max_chars=normalized_max_chars)
    complete_history = _complete_history(history)
    if not complete_history:
        return fallback

    transcript = _build_transcript(
        complete_history,
        max_chars=max(4000, min(16000, normalized_max_chars * 10)),
    )
    prompt = (
        "请把下面的短期聊天记录压缩成可继续对话使用的事实摘要。"
        "只保留用户明确说过的事实、已经完成的决定和未完成事项，不新增推测；"
        f"使用中文纯文本，最多 {normalized_max_chars} 字。\n\n{transcript}"
    )
    try:
        raw = model_service.chat_messages(
            [
                {
                    "role": "system",
                    "content": "你负责压缩短期对话上下文，不要把摘要写成对用户的直接回复。",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=max(128, min(4096, normalized_max_chars * 2)),
        )
    except Exception:
        logger.exception("生成聊天摘要失败，退回本地节选")
        return fallback

    normalized = str(raw or "").strip()
    if not normalized or is_model_error(normalized):
        return fallback
    return normalized[:normalized_max_chars]


def update_persisted_summary(
    settings: Settings,
    session: tuple[str, str, str],
    history: list[ChatMessage],
    model_service: DeepSeekService,
) -> bool:
    if not history:
        return False
    expected_last_id = max((message.id for message in history), default=0)
    summary = generate_summary(
        history,
        model_service,
        max_chars=max(0, int(settings.chat_context_summary_max_chars)),
    )
    if not summary:
        return False
    try:
        return save_summary_if_current(
            *session,
            expected_last_message_id=expected_last_id,
            summary=summary,
            retention_days=settings.chat_summary_retention_days,
        )
    except Exception:
        logger.exception("保存聊天摘要失败")
        return False


def _complete_history(
    history: list[ChatMessage],
) -> list[tuple[ChatMessage, ChatMessage]]:
    turns: list[tuple[ChatMessage, ChatMessage]] = []
    for index in range(len(history) - 1):
        user_message, assistant_message = history[index], history[index + 1]
        if user_message.role == "user" and assistant_message.role == "assistant":
            turns.append((user_message, assistant_message))
    return turns


def _build_transcript(
    complete_history: list[tuple[ChatMessage, ChatMessage]],
    max_chars: int,
) -> str:
    """Keep background summary requests bounded when stored turns are large."""
    budget = max(1, int(max_chars))
    selected: list[str] = []
    used = 0
    omitted = False
    for user_message, assistant_message in reversed(complete_history):
        turn = f"用户：{user_message.content}\n助手：{assistant_message.content}"
        if used and used + len(turn) + 2 > budget:
            omitted = True
            break
        if not selected and len(turn) > budget:
            turn = turn[: max(1, budget - 1)].rstrip() + "…"
        selected.append(turn)
        used += len(turn) + (2 if used else 0)

    selected.reverse()
    prefix = "[较早记录已省略]\n\n" if omitted else ""
    return prefix + "\n\n".join(selected)
