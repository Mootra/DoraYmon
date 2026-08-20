from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass

from doraymon.config import Settings
from services.deepseek_service import DEFAULT_SYSTEM_PROMPT, DeepSeekService
from services.rag_service import RagService, build_knowledge_context
from storage.chat_history_store import ChatMessage
from storage.knowledge_store import KnowledgeIndexUnavailable, KnowledgeSearchResult


logger = logging.getLogger(__name__)
SOURCE_REFERENCE_PATTERN = re.compile(r"\[(\d+)\]")
CONTEXTUAL_FOLLOW_UP_MARKERS = (
    "它",
    "这个",
    "那个",
    "上述",
    "上面",
    "刚才",
    "继续",
    "呢",
)
SHORT_QUESTION_MARKERS = (
    "怎么",
    "如何",
    "为什么",
    "什么",
    "哪",
    "多少",
    "是否",
    "能否",
    "吗",
    "？",
    "?",
)


@dataclass(frozen=True)
class ConversationResponse:
    answer: str
    knowledge_results: list[KnowledgeSearchResult]


class ConversationService:
    def __init__(
        self,
        settings: Settings,
        model_service: DeepSeekService | None = None,
        rag_service: RagService | None = None,
    ) -> None:
        self.settings = settings
        self.model_service = model_service or DeepSeekService(settings)
        self.rag_service = rag_service or RagService(
            settings,
            model_service=self.model_service,
        )

    def answer(
        self,
        prompt: str,
        history: list[ChatMessage] | None = None,
        group_openid: str = "",
        user_openid: str = "",
    ) -> ConversationResponse:
        normalized_prompt = str(prompt or "").strip()
        selected_history = select_complete_history(
            history or [],
            max_chars=max(200, int(self.settings.chat_context_max_chars)),
        )
        retrieval_query = build_retrieval_query(normalized_prompt, selected_history)
        knowledge_results = self._retrieve_optional_knowledge(
            retrieval_query,
            group_openid=group_openid,
            user_openid=user_openid,
        )
        user_message, included_results = _build_user_message(
            normalized_prompt,
            knowledge_results,
            max_context_chars=max(500, int(self.settings.rag_max_context_chars)),
        )

        messages = [{"role": "system", "content": DEFAULT_SYSTEM_PROMPT}]
        messages.extend(
            {"role": message.role, "content": message.content}
            for message in selected_history
        )
        messages.append({"role": "user", "content": user_message})

        answer = self.model_service.chat_messages(messages)
        return ConversationResponse(
            answer=_append_referenced_sources(str(answer or "").strip(), included_results),
            knowledge_results=included_results,
        )

    def _retrieve_optional_knowledge(
        self,
        prompt: str,
        group_openid: str,
        user_openid: str,
    ) -> list[KnowledgeSearchResult]:
        if not self.settings.rag_enabled or not prompt:
            return []

        try:
            return self.rag_service.retrieve(
                prompt,
                group_openid=group_openid,
                user_openid=user_openid,
            )
        except (KnowledgeIndexUnavailable, sqlite3.Error):
            logger.warning("普通聊天的可选知识检索失败，已退化为普通对话")
            return []


def select_complete_history(
    history: list[ChatMessage],
    max_chars: int,
) -> list[ChatMessage]:
    complete_turns: list[tuple[ChatMessage, ChatMessage]] = []
    index = 0
    while index + 1 < len(history):
        user_message = history[index]
        assistant_message = history[index + 1]
        if user_message.role == "user" and assistant_message.role == "assistant":
            complete_turns.append((user_message, assistant_message))
            index += 2
            continue
        index += 1

    selected_turns: list[tuple[ChatMessage, ChatMessage]] = []
    used_chars = 0
    for turn in reversed(complete_turns):
        turn_chars = len(turn[0].content) + len(turn[1].content)
        if selected_turns and used_chars + turn_chars > max_chars:
            break
        if not selected_turns and turn_chars > max_chars:
            break
        selected_turns.append(turn)
        used_chars += turn_chars

    selected: list[ChatMessage] = []
    for user_message, assistant_message in reversed(selected_turns):
        selected.extend((user_message, assistant_message))
    return selected


def build_retrieval_query(prompt: str, history: list[ChatMessage]) -> str:
    normalized_prompt = str(prompt or "").strip()
    if not normalized_prompt or not history:
        return normalized_prompt
    is_contextual_follow_up = any(
        marker in normalized_prompt for marker in CONTEXTUAL_FOLLOW_UP_MARKERS
    )
    is_short_question = len(normalized_prompt) <= 16 and any(
        marker in normalized_prompt for marker in SHORT_QUESTION_MARKERS
    )
    if not is_contextual_follow_up and not is_short_question:
        return normalized_prompt

    previous_user_message = next(
        (message.content for message in reversed(history) if message.role == "user"),
        "",
    )
    if not previous_user_message:
        return normalized_prompt
    return f"{previous_user_message}\n{normalized_prompt}"


def _build_user_message(
    prompt: str,
    results: list[KnowledgeSearchResult],
    max_context_chars: int,
) -> tuple[str, list[KnowledgeSearchResult]]:
    if not results:
        return prompt, []

    context, included = build_knowledge_context(results, max_context_chars)
    message = "\n\n".join(
        [
            prompt,
            "可选知识资料（只有与当前问题相关时才使用）：",
            context,
        ]
    )
    return message, included


def _append_referenced_sources(
    answer: str,
    results: list[KnowledgeSearchResult],
) -> str:
    if not answer or not results or answer.startswith("DeepSeek "):
        return answer

    referenced_numbers = {
        int(match.group(1))
        for match in SOURCE_REFERENCE_PATTERN.finditer(answer)
        if 1 <= int(match.group(1)) <= len(results)
    }
    if not referenced_numbers:
        return answer

    lines = ["参考资料："]
    for source_number in sorted(referenced_numbers):
        result = results[source_number - 1]
        heading = f" · {result.heading}" if result.heading else ""
        lines.append(f"[{source_number}] {result.title}{heading} ({result.source_path})")
    return f"{answer}\n\n" + "\n".join(lines)
