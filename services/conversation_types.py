"""Shared data contracts for context building and reply generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from storage.chat_history_store import ChatMessage
from storage.knowledge_store import KnowledgeSearchResult
from services.group_dialogue import GroupDialogueContext


@dataclass(frozen=True)
class MemoryItem:
    memory_id: int
    content: str
    category: str = "fact"
    visibility: str = "private"


@dataclass(frozen=True)
class ConversationContext:
    """Selected context; callers must filter memories for the current scene."""

    scene: str
    user_openid: str
    group_openid: str = ""
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    recent_history: tuple[ChatMessage, ...] = ()
    summary: str = ""
    memories: tuple[MemoryItem, ...] = ()
    knowledge: tuple[KnowledgeSearchResult, ...] = ()
    group_dialogue: GroupDialogueContext | None = None


@dataclass(frozen=True)
class PromptBundle:
    """Model messages, selected sources, and per-request model options."""

    messages: tuple[dict[str, str], ...]
    included_knowledge: tuple[KnowledgeSearchResult, ...] = ()
    options: dict[str, Any] = field(default_factory=dict)
    source_offset: int = 0
