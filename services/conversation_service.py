"""Orchestrate context, prompts, model calls and reply formatting."""
from __future__ import annotations

from dataclasses import dataclass

from doraymon.config import Settings
# Keep legacy exports for existing integrations.
from services.context_builder import (
    ContextWindow, build_context, build_context_window, build_retrieval_query,
    is_explicit_topic_switch, retrieve_optional_knowledge,
    select_complete_history, summarize_history,
)
from services.conversation_types import ConversationContext
from services.deepseek_service import DeepSeekService, is_model_error
from services.prompt_builder import build_prompt, _build_user_message
from services.rag_service import RagService
from services.reply_postprocess import finalize_reply, _append_referenced_sources
from storage.chat_history_store import ChatMessage
from storage.knowledge_store import KnowledgeSearchResult


@dataclass(frozen=True)
class ConversationResponse:
    answer: str
    knowledge_results: list[KnowledgeSearchResult]
    is_error: bool = False


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
            settings, model_service=self.model_service,
        )

    def answer(
        self,
        prompt: str,
        history: list[ChatMessage] | ConversationContext | None = None,
        group_openid: str = "",
        user_openid: str = "",
        *,
        ctx: ConversationContext | None = None,
    ) -> ConversationResponse:
        """Accept a prepared context or the legacy history/identity arguments."""
        if ctx is None:
            ctx = history if isinstance(history, ConversationContext) else build_context(
                self.settings, prompt,
                scene="group" if group_openid else "private",
                user_openid=user_openid, group_openid=group_openid,
                history=history, rag_service=self.rag_service,
            )
        bundle = build_prompt(ctx, prompt, self.settings)
        raw = self.model_service.chat_messages(list(bundle.messages), **bundle.options)
        return ConversationResponse(
            answer=finalize_reply(raw, ctx, bundle, self.settings),
            knowledge_results=list(bundle.included_knowledge),
            is_error=is_model_error(str(raw or "").strip()),
        )

    def _retrieve_optional_knowledge(
        self, prompt: str, group_openid: str, user_openid: str,
    ) -> list[KnowledgeSearchResult]:
        return retrieve_optional_knowledge(
            self.settings, self.rag_service, prompt, group_openid, user_openid,
        )
