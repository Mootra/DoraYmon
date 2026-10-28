"""A generated reply becomes history only after confirmed delivery."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from doraymon.context import BotContext
from services.session_history import reply_allowed, save_history
from storage.chat_history_store import ChatMessage

logger = logging.getLogger(__name__)
SavedTurn = tuple[ChatMessage, ChatMessage]


@dataclass
class PendingTurn:
    context: BotContext
    prompt: str
    revision: int | None
    on_saved: Callable[[SavedTurn], None] | None = None
    finished: bool = False


class PendingReply(str):
    """Text-compatible routing result carrying an uncommitted conversation turn.

    Routing still returns text. Only delivery owners commit this metadata; error
    replies and ordinary command acknowledgements remain plain strings.
    """

    def __new__(cls, text: str, turn: PendingTurn):
        value = super().__new__(cls, text)
        value.turn = turn
        return value

    def with_text(self, text: str) -> PendingReply:
        return PendingReply(text, self.turn)


def stage_reply(
    context: BotContext,
    prompt: str,
    text: str,
    revision: int | None,
    *,
    on_saved: Callable[[SavedTurn], None] | None = None,
) -> PendingReply:
    return PendingReply(text, PendingTurn(context, prompt, revision, on_saved))


def reply_is_current(reply: str) -> bool:
    if not isinstance(reply, PendingReply):
        return True
    return reply_allowed(reply.turn.context, reply.turn.revision)


def commit_delivered_reply(reply: str, delivered_text: str) -> SavedTurn | None:
    """Commit only the contiguous prefix the platform acknowledged, once.

    Refusals, unknown outcomes and unsent tails are deliberately absent. A clear
    during a send also retires the pending turn, even if that send later succeeds.
    """
    if not isinstance(reply, PendingReply) or reply.turn.finished:
        return None
    turn = reply.turn
    turn.finished = True
    if not delivered_text or not reply_is_current(reply):
        return None
    # The delivery renderer can append its configured truncation marker. Its
    # acknowledged text, including that marker, is the visible conversation.
    saved = save_history(turn.context, turn.prompt, delivered_text)
    if saved is not None and turn.on_saved is not None:
        try:
            turn.on_saved(saved)
        except Exception:
            logger.exception("投递历史已保存，但摘要任务未能启动")
    return saved
