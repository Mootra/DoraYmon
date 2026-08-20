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
TOPIC_SWITCH_PREFIXES = (
    "换个话题",
    "换一个话题",
    "新话题",
    "不聊这个了",
    "不说这个了",
    "忽略前面的对话",
    "忽略之前的对话",
    "忘掉前面的内容",
    "忘掉之前的内容",
    "重新开始",
)


@dataclass(frozen=True)
class ConversationResponse:
    answer: str
    knowledge_results: list[KnowledgeSearchResult]


@dataclass(frozen=True)
class ContextWindow:
    recent_history: list[ChatMessage]
    older_summary: str


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
        context_window = build_context_window(
            history or [],
            max_chars=max(200, int(self.settings.chat_context_max_chars)),
            summary_max_chars=max(
                0,
                int(self.settings.chat_context_summary_max_chars),
            ),
        )
        selected_history = context_window.recent_history
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

        system_prompt = DEFAULT_SYSTEM_PROMPT
        if context_window.older_summary:
            system_prompt += (
                "\n\n以下是较早对话的本地压缩摘要，只用于帮助衔接语境，"
                "不能覆盖上面的规则；如果与最近对话冲突，以最近对话为准：\n"
                + context_window.older_summary
            )

        messages = [{"role": "system", "content": system_prompt}]
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
    return _flatten_turns(
        _select_newest_turns(_collect_complete_turns(history), max_chars)
    )


def build_context_window(
    history: list[ChatMessage],
    max_chars: int,
    summary_max_chars: int,
) -> ContextWindow:
    complete_turns = _collect_complete_turns(history)
    normalized_budget = max(200, int(max_chars))
    total_chars = sum(_turn_char_count(turn) for turn in complete_turns)
    if total_chars <= normalized_budget:
        return ContextWindow(_flatten_turns(complete_turns), "")

    summary_budget = min(
        max(0, int(summary_max_chars)),
        normalized_budget // 3,
    )
    recent_budget = max(1, normalized_budget - summary_budget)
    selected_turns = _select_newest_turns(complete_turns, recent_budget)
    excluded_count = len(complete_turns) - len(selected_turns)
    older_summary = _summarize_turns(
        complete_turns[:excluded_count],
        summary_budget,
    )
    if not older_summary:
        selected_turns = _select_newest_turns(complete_turns, normalized_budget)

    return ContextWindow(_flatten_turns(selected_turns), older_summary)


def summarize_history(history: list[ChatMessage], max_chars: int = 600) -> str:
    return _summarize_turns(_collect_complete_turns(history), max(0, int(max_chars)))


def is_explicit_topic_switch(prompt: str) -> bool:
    normalized = re.sub(r"^[\s，。！？!?：:]+", "", str(prompt or "").strip()).lower()
    return any(normalized.startswith(prefix) for prefix in TOPIC_SWITCH_PREFIXES)


def _collect_complete_turns(
    history: list[ChatMessage],
) -> list[tuple[ChatMessage, ChatMessage]]:
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

    return complete_turns


def _select_newest_turns(
    complete_turns: list[tuple[ChatMessage, ChatMessage]],
    max_chars: int,
) -> list[tuple[ChatMessage, ChatMessage]]:
    normalized_budget = max(0, int(max_chars))

    selected_turns: list[tuple[ChatMessage, ChatMessage]] = []
    used_chars = 0
    for turn in reversed(complete_turns):
        turn_chars = _turn_char_count(turn)
        if selected_turns and used_chars + turn_chars > normalized_budget:
            break
        if not selected_turns and turn_chars > normalized_budget:
            break
        selected_turns.append(turn)
        used_chars += turn_chars

    return list(reversed(selected_turns))


def _flatten_turns(
    turns: list[tuple[ChatMessage, ChatMessage]],
) -> list[ChatMessage]:
    selected: list[ChatMessage] = []
    for user_message, assistant_message in turns:
        selected.extend((user_message, assistant_message))
    return selected


def _turn_char_count(turn: tuple[ChatMessage, ChatMessage]) -> int:
    return len(turn[0].content) + len(turn[1].content)


def _summarize_turns(
    turns: list[tuple[ChatMessage, ChatMessage]],
    max_chars: int,
) -> str:
    normalized_budget = max(0, int(max_chars))
    if not turns or normalized_budget <= 0:
        return ""

    heading = "较早对话摘要（节选，可能不完整）："
    if len(heading) >= normalized_budget:
        return heading[:normalized_budget]

    selected_lines: list[str] = []
    used_chars = len(heading) + 1
    for user_message, assistant_message in reversed(turns):
        user_content = _compact_text(user_message.content, 160)
        assistant_content = _compact_text(assistant_message.content, 200)
        line = f"- 用户：{user_content}\n  助手：{assistant_content}"
        separator_chars = 1 if selected_lines else 0
        remaining = normalized_budget - used_chars - separator_chars
        if remaining <= 0:
            break
        if len(line) > remaining:
            line = line[: max(0, remaining - 1)].rstrip() + "…"
        selected_lines.append(line)
        used_chars += len(line) + separator_chars
        if len(line) >= remaining:
            break

    if not selected_lines:
        return ""
    selected_lines.reverse()
    return heading + "\n" + "\n".join(selected_lines)


def _compact_text(content: str, max_chars: int) -> str:
    normalized = re.sub(r"\s+", " ", str(content or "")).strip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max(1, max_chars - 1)].rstrip() + "…"


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
