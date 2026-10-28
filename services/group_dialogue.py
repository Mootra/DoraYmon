"""Bounded, ephemeral group context and explainable participation decisions.

Only messages already admitted by the entry's access policy belong here. Public
group turns never become another member's private history or meal constraints.
No model, storage, configuration or platform API is called by this module.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from services.intent_service import detect_intent, is_stop_text

if TYPE_CHECKING:
    from services.message_contract import MessageEvent


DEFAULT_BOT_NAMES = ("DoraYmon", "哆啦A梦", "哆啦", "小哆")
DIRECT_REASONS = frozenset({"mention", "quote", "name", "command", "continuation"})
_ACK = re.compile(r"^(?:嗯+|哦+|噢+|啊+|好(?:的|了)?|行|收到|明白(?:了)?|知道了|谢谢(?:你)?|多谢|不用了|哈哈+|[哈呵]+|ok|okay|[👍👌]+)[。！!，,～~\s]*$", re.I)
_SOCIAL = re.compile(r"^(?:有人在吗|在吗|有人(?:打游戏|开黑|一起玩吗)|你(?:吃(?:饭)?了吗|到(?:了)?吗|在干嘛)|怎么又(?:下雨|堵车|迟到)|为什么又(?:下雨|堵车|迟到))")
_HELP = re.compile(r"谁(?:知道|能|会)|有没有人|有人(?:知道|会|能)|求助|请教|求(?:推荐|解释|帮助)|麻烦(?:帮|解释)|帮(?:我|忙|大家)|怎么|如何|为什么|为何|什么是|是否|能否|有什么区别|有啥区别|区别是什么")
_QUESTION = re.compile(r"(?:是什么|有哪些|有什么|什么意思|多少|哪里|哪儿|哪个|哪种|吗[？?]?$|[？?]$)")


@dataclass(frozen=True)
class GroupTurn:
    message_id: str
    speaker_id: str
    speaker_name: str
    text: str
    received_at: str
    mentions: tuple[str, ...] = ()
    quote_id: str = ""
    quote_author: str = ""
    quote_text: str = ""
    reply_to_user_id: str = ""


@dataclass(frozen=True)
class GroupDialogueContext:
    current_user_id: str
    current_user_name: str
    trigger: str
    turns: tuple[GroupTurn, ...] = ()


@dataclass
class _Room:
    turns: list[tuple[float, GroupTurn]] = field(default_factory=list)
    active: OrderedDict[str, float] = field(default_factory=OrderedDict)
    muted: OrderedDict[str, float] = field(default_factory=OrderedDict)


class GroupDialogue:
    def __init__(
        self, *, enabled: bool = True, bot_names=DEFAULT_BOT_NAMES,
        clock: Callable[[], float] = time.monotonic,
        max_groups: int = 128, max_turns: int = 24, max_chars: int = 6000,
        max_age_seconds: float = 300, active_seconds: float = 120,
        max_participants: int = 128,
    ) -> None:
        self.enabled = enabled
        if isinstance(bot_names, str):
            bot_names = bot_names.split(",")
        self.bot_names = tuple(str(name).strip() for name in bot_names if str(name).strip())
        self.clock = clock
        self.max_groups = max(1, max_groups)
        self.max_turns = max(1, max_turns)
        self.max_chars = max(1, max_chars)
        self.max_age_seconds = max(0, max_age_seconds)
        self.active_seconds = max(0, active_seconds)
        self.max_participants = max(1, max_participants)
        self._rooms: OrderedDict[tuple[str, str, str], _Room] = OrderedDict()

    def _room(self, event: MessageEvent) -> _Room:
        key = (event.platform, event.bot_account, event.raw_group_id)
        room = self._rooms.setdefault(key, _Room())
        self._rooms.move_to_end(key)
        while len(self._rooms) > self.max_groups:
            self._rooms.popitem(last=False)
        now = self.clock()
        room.turns[:] = [(seen, turn) for seen, turn in room.turns if now - seen <= self.max_age_seconds]
        for branches in (room.active, room.muted):
            for user, seen in tuple(branches.items()):
                if now - seen >= self.active_seconds:
                    branches.pop(user, None)
        return room

    def _called_by_name(self, text: str) -> bool:
        for name in self.bot_names:
            if re.match(r"^(?:嘿|嗨|@)?" + re.escape(name) + r"(?:$|[\s，,：:！!？?]|你|帮|请|在吗|怎么|能不能)", text, re.I):
                return True
        return False

    def _strip_name(self, text: str) -> str:
        for name in self.bot_names:
            text = re.sub(r"^(?:嘿|嗨|@)?" + re.escape(name) + r"[\s，,：:]*", "", text, flags=re.I)
        return text

    def _targets_other(self, event: MessageEvent, room: _Room) -> bool:
        if any(not mention.is_bot and mention.raw_id != "all" for mention in event.mentions):
            return True
        if event.quote is not None and event.quote.raw_user_id != event.bot_account:
            return True
        text = event.normalized_text
        # Also recognise an ordinary name salutation when that name has actually
        # appeared in this room. Do not guess every topic word is a person's name.
        return any(
            turn.speaker_id not in {event.raw_user_id, event.bot_account}
            and turn.speaker_name != turn.speaker_id
            and re.match(re.escape(turn.speaker_name) + r"(?:[，,：:\s]|你)", text)
            for _, turn in room.turns
        )

    def is_stop_request(self, event: MessageEvent) -> bool:
        text = self._strip_name(event.normalized_text).strip()
        return is_stop_text(text)

    def targets_other(self, event: MessageEvent) -> bool:
        return self._targets_other(event, self._room(event))

    def is_stop(self, event: MessageEvent) -> bool:
        return self.is_stop_request(event) and (
            event.mentions_bot or event.replies_to_bot() or not self._targets_other(event, self._room(event))
        )

    def reason_for(self, event: MessageEvent, *, command_prefix: str = "/") -> str:
        room = self._room(event)
        text = event.normalized_text
        if not text or self.is_stop(event):
            return ""
        if event.mentions_bot:
            return "mention"
        if event.replies_to_bot():
            return "quote"
        if self._called_by_name(text):
            return "name"
        if self._targets_other(event, room):
            return ""
        if text.startswith(command_prefix):
            return "command"
        if _ACK.fullmatch(text) or _SOCIAL.search(text):
            return ""
        if event.raw_user_id in room.active:
            return "continuation"
        if not self.enabled or event.raw_user_id in room.muted:
            return ""
        if detect_intent(text) is not None:
            return "food_request"
        if len(text) >= 5 and (_HELP.search(text) or (
            len(text) >= 8 and not re.match(r"^[你他她]", text) and _QUESTION.search(text)
        )):
            return "public_question"
        return ""

    def observe(self, event: MessageEvent) -> None:
        if not event.is_group:
            return
        quote = event.quote
        self._append(event, GroupTurn(
            message_id=event.message_id, speaker_id=event.raw_user_id,
            speaker_name=(event.sender_name or event.raw_user_id)[:80],
            text=event.text[:600], received_at=event.received_at.isoformat(),
            mentions=event.mentions_raw_ids(),
            quote_id=quote.message_id if quote else "",
            quote_author=quote.raw_user_id if quote else "",
            quote_text=quote.excerpt[:600] if quote else "",
        ))

    def delivered(self, event: MessageEvent, text: str, message_id: str = "") -> None:
        room = self._room(event)
        room.muted.pop(event.raw_user_id, None)
        room.active[event.raw_user_id] = self.clock()
        room.active.move_to_end(event.raw_user_id)
        while len(room.active) > self.max_participants:
            room.active.popitem(last=False)
        self._append(event, GroupTurn(
            message_id=message_id, speaker_id=event.bot_account, speaker_name="DoraYmon",
            text=text[:600], received_at=datetime.now(timezone.utc).isoformat(),
            quote_id=event.message_id, quote_author=event.raw_user_id,
            reply_to_user_id=event.raw_user_id,
        ))

    def _append(self, event: MessageEvent, turn: GroupTurn) -> None:
        room = self._room(event)
        if turn.message_id:
            for index, (seen, old) in enumerate(room.turns):
                if old.message_id == turn.message_id:
                    room.turns[index] = (seen, turn)
                    break
            else:
                room.turns.append((self.clock(), turn))
        else:
            room.turns.append((self.clock(), turn))
        while len(room.turns) > self.max_turns or sum(len(str(turn)) for _, turn in room.turns) > self.max_chars:
            room.turns.pop(0)

    def context_for(self, event: MessageEvent, reason: str) -> GroupDialogueContext:
        return GroupDialogueContext(
            current_user_id=event.raw_user_id,
            current_user_name=(event.sender_name or event.raw_user_id)[:80],
            trigger=reason, turns=tuple(turn for _, turn in self._room(event).turns),
        )

    def end_branch(self, event: MessageEvent) -> None:
        room = self._room(event)
        room.active.pop(event.raw_user_id, None)
        room.muted[event.raw_user_id] = self.clock()
        room.muted.move_to_end(event.raw_user_id)
        while len(room.muted) > self.max_participants:
            room.muted.popitem(last=False)
        self.forget_turns(event)

    def forget_turns(self, event: MessageEvent) -> None:
        room = self._room(event)
        room.turns[:] = [
            (seen, turn) for seen, turn in room.turns
            if turn.speaker_id != event.raw_user_id and turn.reply_to_user_id != event.raw_user_id
        ]

    def clear(self) -> None:
        self._rooms.clear()
