"""Message session: adapter plus router, transport-agnostic.

One session serves every text channel (the QQ account over OneBot, and the
Feishu app over its long connection). Everything that differs between them lives
in the adapter: payload translation, what counts as a duplicate, the outbound
action shape, and how the protocol reports a refusal. What stays here is what
must behave the same everywhere — addressing, quote resolution, routing,
throttling, segmented sending, and never resending a message whose outcome is
unknown.

It never talks to a socket directly. It receives already-decoded payloads and
emits protocol actions through a transport object, which is what makes the whole
lifecycle testable offline with a fake transport.

Responsibilities, per the plan's P1a:

- adapt a payload, drop duplicates and the bot's own messages;
- route the resulting event through the shared router;
- send the reply through the transport and record each segment's outcome;
- never blindly resend a message whose delivery result is unknown after a
  reconnect: an unknown outcome is reported as unknown, because resending could
  duplicate a message the user already received.

Configuration stays in the environment/config layer; this module receives plain
values.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol

from doraymon.context import BotContext
from doraymon.identity import PLATFORM_FEISHU, PLATFORM_QQ_ACCOUNT
from doraymon.router import parse_command, route_incoming_message
from services.message_contract import MessageEvent, QuotedMessage
from services.group_dialogue import DIRECT_REASONS, GroupDialogue
from services.admin_access import is_admin
from services.input_coalescer import InputBatch, InputCoalescer
from services.intent_service import is_stop_text, redirects_meal_to_chat
from services.meal_session import MEAL_STATES
from services.onebot_adapter import (
    # Re-exported at the end of this module: the error moved next to the
    # adapter that raises it, and older callers import it from here.
    OneBotActionError,
    OneBotAdapter,
)
from services.reply_delivery import (
    DEFAULT_MAX_REPLY_SEGMENTS,
    DEFAULT_MAX_REPLY_TOTAL_CHARS,
    split_reply,
)
from services.account_throttle import (
    ACCOUNT_THROTTLE,
    DISABLED as THROTTLE_DISABLED,
    REASON_ALLOWED as THROTTLE_ALLOWED,
    ThrottlePolicy,
    policy_for_scope,
)
from services.pending_reply import commit_delivered_reply, reply_is_current
from services.send_result import DELIVERED, REJECTED, UNKNOWN, SEND_LOG, SendResult, classify_send_error
from services.session_history import get_session_key
from services.session_revisions import SESSION_REVISIONS
from services.session_scheduler import ScheduleRejected, SessionScheduler
from services.silence import parse_silence_args, should_reply

logger = logging.getLogger(__name__)

MAX_QUOTE_AUTHORS = 256
QuoteKey = tuple[str, str, str, str, str]

# Reasons one payload produced no reply. Kept explicit so a silent bot can be
# explained from the session's counters instead of guesswork.
SKIP_NOT_ADAPTED = "not_adapted"
SKIP_DUPLICATE = "duplicate"
SKIP_SELF = "self_message"
SKIP_NOT_ADDRESSED = "not_addressed"
SKIP_EMPTY_REPLY = "empty_reply"
SKIP_THROTTLED = "throttled"
SKIP_QUOTE_UNRESOLVED = "quote_unresolved"
SKIP_QUEUE_FULL = "queue_full"
SKIP_CLOSED = "closed"
SKIP_BRANCH_ENDED = "branch_ended"
SKIP_SILENCED = "silenced"

# A log line has to name the channel it came from: reporting a Feishu failure as
# a QQ account failure sends whoever reads the log to the wrong entry point.
_PLATFORM_LABELS = {
    PLATFORM_QQ_ACCOUNT: "QQ 账号",
    PLATFORM_FEISHU: "飞书",
}


class MessageAdapter(Protocol):
    """What one session needs from a protocol adapter.

    Stated as a protocol rather than a shared base class because the two
    adapters translate genuinely different payloads; the only thing they have to
    agree on is this list of operations.
    """

    bot_account: str
    platform: str
    duplicates: Any

    def is_self_message(self, payload: Any) -> bool:
        ...

    def event_id_of(self, payload: Any) -> str:
        ...

    def adapt(self, payload: Any) -> MessageEvent | None:
        ...

    def needs_quote_lookup(self, event: MessageEvent) -> bool:
        ...

    def quote_author_from_response(self, response: Any, *, event: MessageEvent | None = None) -> str:
        ...

    def quote_text_from_response(self, response: Any, *, event: MessageEvent) -> str:
        ...

    def sent_message_id(self, response: Any) -> str:
        ...

    def send_error_from_response(self, response: Any) -> BaseException | None:
        ...

    def reply_action(
        self,
        event: MessageEvent,
        text: str,
        *,
        reply_to_message_id: str = "",
        echo: str = "",
    ) -> Any:
        ...

    def get_message_action(self, message_id: str, *, echo: str = "") -> Any:
        ...


class ActionTransport(Protocol):
    """Sends one action and resolves once the result is known.

    ``action`` is whatever the adapter built (``OneBotAction`` or
    ``FeishuAction``); the session only passes it on and never inspects it.
    """

    async def send_action(self, action: Any) -> dict[str, Any]:
        ...


@dataclass
class SessionStats:
    """Counters for diagnosing a quiet bot after a real deployment."""

    received: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    replied: int = 0
    segments_sent: int = 0
    segments_failed: int = 0
    inputs_coalesced: int = 0
    input_wait_seconds: float = 0.0

    def note_skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


class AccountSession:
    """Handles account-side messages without knowing how bytes are moved."""

    def __init__(
        self,
        *,
        settings: Any,
        transport: ActionTransport,
        bot_account: str,
        group_reply_mode: str = "reply",
        platform: str = PLATFORM_QQ_ACCOUNT,
        adapter: MessageAdapter | None = None,
        group_throttle: ThrottlePolicy | None = None,
        private_throttle: ThrottlePolicy | None = None,
        throttle: Any = None,
        scheduler: SessionScheduler | None = None,
        group_dialogue: GroupDialogue | None = None,
        participation_throttle: ThrottlePolicy | None = None,
        input_coalescer: InputCoalescer | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.bot_account = str(bot_account or "").strip()
        self.platform = platform
        self.channel_label = _PLATFORM_LABELS.get(platform, str(platform))
        self.group_reply_mode = str(group_reply_mode or "reply")
        self.adapter = adapter or OneBotAdapter(bot_account=self.bot_account, platform=platform)
        self.started_at = datetime.now()
        self.stats = SessionStats()
        self.connected = False
        self.group_throttle = group_throttle or THROTTLE_DISABLED
        self.private_throttle = private_throttle or THROTTLE_DISABLED
        self.throttle = throttle if throttle is not None else ACCOUNT_THROTTLE
        self.group_dialogue = group_dialogue
        self.participation_throttle = participation_throttle or THROTTLE_DISABLED
        # Authors of quoted messages, keyed by message id. Without this, a group
        # reply that quotes the bot is indistinguishable from one that quotes
        # another member, because OneBot's reply segment carries only an id.
        self._quote_authors: dict[QuoteKey, str] = {}
        self._quote_excerpts: dict[QuoteKey, str] = {}
        self.scheduler = scheduler if scheduler is not None else SessionScheduler()
        self._background_tasks: set[asyncio.Task] = set()
        self._send_locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self.input_coalescer = input_coalescer if input_coalescer is not None else InputCoalescer(
            wait_seconds=(getattr(settings, "qq_account_input_merge_wait_seconds", 0.35)
                          if platform == PLATFORM_QQ_ACCOUNT else 0),
            max_wait_seconds=getattr(settings, "qq_account_input_merge_max_wait_seconds", 1.5),
        )
        self._active_events: dict[object, MessageEvent] = {}

    # -- lifecycle ----------------------------------------------------------

    def on_connected(self) -> None:
        """A (re)connect. Duplicates are still suppressed from here on."""
        self.connected = True

    def on_disconnected(self) -> None:
        self.connected = False

    def on_reconnected(self) -> None:
        """Reconnect keeps the duplicate window, so replayed events are dropped.

        No queued reply is resent: if a send was interrupted by the disconnect,
        its outcome is unknown and resending could duplicate it.
        """
        self.connected = True
        logger.info("%s连接已恢复，重复事件窗口保持有效", self.channel_label)

    async def close(self) -> None:
        self.input_coalescer.clear()
        await self.scheduler.close()
        tasks = tuple(self._background_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.group_dialogue is not None:
            self.group_dialogue.clear()
        self._quote_authors.clear()
        self._quote_excerpts.clear()

    # -- inbound ------------------------------------------------------------

    async def handle_payload(self, payload: dict[str, Any]) -> MessageEvent | None:
        """Handle one inbound payload. Returns the event that produced a reply."""
        self.stats.received += 1

        if self.adapter.is_self_message(payload):
            self.stats.note_skip(SKIP_SELF)
            return None

        event_id = self.adapter.event_id_of(payload)
        if event_id and self.adapter.duplicates.seen_before(event_id):
            self.stats.note_skip(SKIP_DUPLICATE)
            return None

        event = self.adapter.adapt(payload)
        if event is None:
            self.stats.note_skip(SKIP_NOT_ADAPTED)
            return None

        return await self.handle_event(event, counted=True)

    async def handle_event(
        self,
        event: MessageEvent,
        *,
        counted: bool = False,
    ) -> MessageEvent | None:
        """Handle one already-adopted event.

        Separated from ``handle_payload`` so the account entry can apply its
        access policy between adaptation and routing without adapting twice.
        ``counted`` is for callers that already incremented ``received``.
        """
        if not counted:
            self.stats.received += 1

        if self.scheduler.closed:
            self.stats.note_skip(SKIP_CLOSED)
            return None

        stop = not event.is_group and is_stop_text(event.normalized_text)
        if event.is_group and self.group_dialogue is not None:
            if self.group_dialogue.is_stop_request(event) and event.quote is not None and not event.quote.raw_user_id:
                event = await self._resolve_quote(event)
            stop = self.group_dialogue.is_stop(event)
        if stop:
            SESSION_REVISIONS.bump(event.session_key)
            self.input_coalescer.discard(event.session_key)
            MEAL_STATES.forget(event.session_key)
            if event.is_group and self.group_dialogue is not None:
                self.group_dialogue.end_branch(event)
            try:
                await self.scheduler.run(
                    event.session_key, lambda: self._end_branch(event), interrupt=True,
                )
            except ScheduleRejected:
                self.stats.note_skip(SKIP_QUEUE_FULL)
            return None

        if event.is_group and self.group_dialogue is not None:
            self.group_dialogue.observe(event)
            self._remember_quote(event, event.message_id, event.raw_user_id, event.text)

        command, args = parse_command(event.text, self.settings.command_prefix)
        if command != "静默" and not should_reply(self.settings):
            self.stats.note_skip(SKIP_SILENCED)
            return None
        silence_control = (command == "静默" and parse_silence_args(args) in {"on", "off"}
                           and is_admin(event.to_bot_context(self.settings, self.started_at)))
        direction_change = not command and redirects_meal_to_chat(event.normalized_text)
        inherited_reason = ""
        if command == "清空上下文" or silence_control or direction_change:
            # A control must not wait behind the generation it is retiring.
            event = await self._resolve_quote(event)
            if event.is_group:
                reason = self._group_reason(event)
                if direction_change and not reason and self.group_dialogue is not None and not self.group_dialogue.targets_other(event):
                    previous = self._active_events.get(event.session_key) or self.input_coalescer.pending_event(event)
                    if previous is not None and self.input_coalescer.same_branch(previous, event):
                        reason = previous.reply_reason or self._group_reason(previous)
                if not reason:
                    self.stats.note_skip(SKIP_NOT_ADDRESSED)
                    return None
                inherited_reason = reason
            SESSION_REVISIONS.bump(event.session_key)
            self.input_coalescer.discard(event.session_key)
            if direction_change:
                MEAL_STATES.forget(event.session_key)

        interrupt = command == "清空上下文" or silence_control or direction_change
        batch, owner = self.input_coalescer.admit(event, mergeable=not command and not interrupt)
        if not owner:
            self.stats.inputs_coalesced += 1
            return None

        try:
            return await self.scheduler.run(
                event.session_key, lambda: self._handle_batch(batch, inherited_reason=inherited_reason),
                interrupt=interrupt, prepare=lambda: self._wait_for_input(batch),
            )
        except ScheduleRejected as error:
            self.stats.note_skip(SKIP_CLOSED if self.scheduler.closed else SKIP_QUEUE_FULL)
            logger.info("%s事件未入队：%s", self.channel_label, error)
            return None
        finally:
            self.input_coalescer.retire(batch)

    async def _wait_for_input(self, batch: InputBatch) -> None:
        try:
            await batch.wait()
            self.stats.input_wait_seconds += batch.waited_seconds
            if len(batch.events) > 1:
                logger.info("%s连续输入合并：%d条，等待%.0fms", self.channel_label,
                            len(batch.events), batch.waited_seconds * 1000)
        finally:
            self.input_coalescer.retire(batch)

    async def _handle_batch(self, batch: InputBatch, *, inherited_reason: str = "") -> MessageEvent | None:
        resolved = []
        for original in batch.events:
            original = await self._resolve_quote(original)
            if original.is_group and self.group_dialogue is not None:
                self.group_dialogue.observe(original)
            resolved.append(original)
        batch.events = resolved
        return await self._handle_event(batch.combined_event(), inherited_reason=inherited_reason)

    async def _handle_event(self, event: MessageEvent, *, inherited_reason: str = "") -> MessageEvent | None:
        if event.is_group:
            reason = self._group_reason(event) or inherited_reason
            if not reason:
                self.stats.note_skip(SKIP_NOT_ADDRESSED)
                return None
            if self.group_dialogue is not None:
                event = replace(event, reply_reason=reason,
                                group_dialogue=self.group_dialogue.context_for(event, reason))

        command, _ = parse_command(event.text, self.settings.command_prefix)
        control = command in {"清空上下文", "静默"}
        if command == "清空上下文" and event.is_group and self.group_dialogue is not None:
            self.group_dialogue.end_branch(event)
        if command != "静默" and not should_reply(self.settings):
            self.stats.note_skip(SKIP_SILENCED)
            return None
        if not control and not self._throttle_allows(event):
            return None

        self._active_events[event.session_key] = event
        try:
            reply = await self._route(event)
            if command == "静默" and not should_reply(self.settings):
                # The authorized on-handler has already changed the shared
                # switch. Retire waiting/generating turns so a quick off cannot
                # later release answers from before the silence request.
                for key in self.scheduler.session_keys:
                    SESSION_REVISIONS.bump(key)
                self.input_coalescer.clear()
                await self.scheduler.cancel_all()
            if not reply.strip():
                self.stats.note_skip(SKIP_EMPTY_REPLY)
                return None

            if await self._deliver_reply(event, reply, control=control):
                self.stats.replied += 1
            return event
        finally:
            if self._active_events.get(event.session_key) is event:
                self._active_events.pop(event.session_key, None)

    async def _deliver_reply(self, event: MessageEvent, reply: str, *, control: bool) -> bool:
        # Different members may generate concurrently. Serialize the final
        # check+send within each lane so two public replies cannot both pass the
        # same cooldown while neither has received its acknowledgement yet.
        key = self._conversation_key(event)
        lock, users = self._send_locks.get(key, (asyncio.Lock(), 0))
        self._send_locks[key] = (lock, users + 1)
        try:
            async with lock:
                if not control and not self._throttle_allows(event):
                    return False
                return await self._send_reply(event, reply)
        finally:
            _, users = self._send_locks[key]
            if users == 1:
                self._send_locks.pop(key)
            else:
                self._send_locks[key] = (lock, users - 1)

    async def _end_branch(self, event: MessageEvent) -> None:
        # Execute behind any cancelled send so its completion cannot restore the
        # meal state or the public window. This only ends this member's branch.
        MEAL_STATES.forget(event.session_key)
        if event.is_group and self.group_dialogue is not None:
            self.group_dialogue.end_branch(event)
        self.stats.note_skip(SKIP_BRANCH_ENDED)

    def _group_reason(self, event: MessageEvent) -> str:
        if self.group_dialogue is not None:
            return self.group_dialogue.reason_for(event, command_prefix=self.settings.command_prefix)
        return "mention" if event.should_consider_for_reply() else ""

    async def _resolve_quote(self, event: MessageEvent) -> MessageEvent:
        """Fill in the quoted author when the protocol did not provide it.

        A group message that quotes the bot must count as addressed, otherwise
        the bot silently ignores someone who replied to it. OneBot's reply
        segment normally carries only a message id, so the author has to be
        looked up. A failed lookup leaves the event unaddressed and is counted as
        such: guessing that the bot was quoted would be worse than staying quiet.
        """
        if event.quote is None:
            return event

        quoted_id = str(event.quote.message_id or "").strip()
        key = self._quote_key(event, quoted_id)
        author = event.quote.raw_user_id or self._quote_authors.get(key, "")
        excerpt = event.quote.excerpt or self._quote_excerpts.get(key, "")
        needs_author = not author and self.adapter.needs_quote_lookup(event)
        needs_body = (self.group_dialogue is not None and event.is_group and not excerpt
                      and bool(quoted_id) and key not in self._quote_excerpts)
        if needs_author or needs_body:
            found_author, excerpt = await self._lookup_quote(event, quoted_id)
            author = found_author or author
            if not author:
                self.stats.note_skip(SKIP_QUOTE_UNRESOLVED)
                return event
            self._remember_quote(event, quoted_id, author, excerpt)

        return replace(
            event,
            quote=QuotedMessage(
                message_id=quoted_id,
                raw_user_id=author,
                excerpt=excerpt,
            ),
        )

    async def _lookup_quote(self, event: MessageEvent, message_id: str) -> tuple[str, str]:
        if not message_id:
            return "", ""
        try:
            response = await self.transport.send_action(
                self.adapter.get_message_action(message_id)
            )
        except Exception:
            logger.warning("查询被引用消息失败，本条消息按未对机器人说话处理")
            return "", ""
        return (self.adapter.quote_author_from_response(response, event=event),
                self.adapter.quote_text_from_response(response, event=event))

    def _quote_key(self, event: MessageEvent, message_id: str) -> QuoteKey:
        return (event.platform, event.bot_account, "group" if event.is_group else "private",
                event.raw_group_id or event.raw_user_id, message_id)

    def _remember_quote(self, event: MessageEvent, message_id: str, author: str, text: str) -> None:
        if not message_id:
            return
        key = self._quote_key(event, message_id)
        self._quote_authors[key] = author
        self._quote_excerpts[key] = text[:600]
        self._bound_quote_cache()

    def _bound_quote_cache(self) -> None:
        while len(self._quote_authors) > MAX_QUOTE_AUTHORS:
            key = next(iter(self._quote_authors))
            self._quote_authors.pop(key, None)
            self._quote_excerpts.pop(key, None)

    def _conversation_key(self, event: MessageEvent) -> str:
        """Throttling is per conversation, not per person inside a group."""
        scope = event.raw_group_id or event.raw_user_id
        key = f"{self.platform}:{self.bot_account}:{scope}"
        if event.is_group and self.group_dialogue is not None:
            key += ":direct" if event.reply_reason in DIRECT_REASONS else ":participation"
        return key

    def _policy_for(self, event: MessageEvent) -> ThrottlePolicy:
        if event.is_group and self.group_dialogue is not None and event.reply_reason not in DIRECT_REASONS:
            return self.participation_throttle
        return policy_for_scope(
            scope_type="group" if event.is_group else "private",
            group_policy=self.group_throttle,
            private_policy=self.private_throttle,
        )

    def _throttle_allows(self, event: MessageEvent) -> bool:
        """Self-imposed courtesy limit, checked before spending a send."""
        policy = self._policy_for(event)
        if not policy.is_enabled:
            return True
        decision = self.throttle.check(self._conversation_key(event), policy)
        if decision.allowed:
            return True
        self.stats.note_skip(SKIP_THROTTLED)
        logger.info("%s发言被本地节流：%s", self.channel_label, decision.describe())
        return False

    async def _route(self, event: MessageEvent) -> str:
        context = event.to_bot_context(self.settings, self.started_at)
        context.background_tasks = self._background_tasks
        fallback = "" if event.is_group else "chat"
        try:
            return await route_incoming_message(context, fallback_command=fallback)
        except Exception:
            logger.exception("%s消息处理失败", self.channel_label)
            return ""

    def _split(self, reply: str) -> list[str]:
        return split_reply(
            reply,
            max_segments=getattr(
                self.settings, "reply_max_segments", DEFAULT_MAX_REPLY_SEGMENTS
            ),
            max_total_chars=getattr(
                self.settings, "reply_max_total_chars", DEFAULT_MAX_REPLY_TOTAL_CHARS
            ),
        )

    def _reply_target(self, event: MessageEvent) -> str:
        """The message a group reply should quote, when quoting is configured."""
        return event.message_id if self.group_reply_mode == "reply" and event.is_group else ""

    async def _send_reply(self, event: MessageEvent, reply: str) -> bool:
        session = event.session_key
        revision = SESSION_REVISIONS.begin(session)
        delivered: list[str] = []
        try:
            for index, segment in enumerate(self._split(reply), start=1):
                command, _ = parse_command(event.text, self.settings.command_prefix)
                if command != "静默" and not should_reply(self.settings):
                    break
                if not reply_is_current(reply) or not SESSION_REVISIONS.is_current(session, revision):
                    logger.info("%s会话已失效，停止后续回复段", self.channel_label)
                    break
                action = self.adapter.reply_action(
                    event, segment, reply_to_message_id=self._reply_target(event)
                )
                result = await self._send_segment(action, event, session, index, text=segment)
                if not result.success:
                    logger.warning("%s回复发送失败：%s", self.channel_label, result.describe())
                    break
                delivered.append(segment)
        finally:
            commit_delivered_reply(reply, "".join(delivered))
        return bool(delivered)

    async def _send_segment(
        self,
        action: Any,
        event: MessageEvent,
        session: Any,
        index: int,
        *,
        text: str = "",
    ) -> SendResult:
        error: BaseException | None = None
        outcome = DELIVERED
        revision = SESSION_REVISIONS.begin(session)
        try:
            response = await self.transport.send_action(action)
        except asyncio.CancelledError as caught:
            error = caught
            outcome = UNKNOWN
        except Exception as caught:
            logger.exception("%s发送动作失败", self.channel_label)
            error = caught
            outcome = UNKNOWN if getattr(caught, "delivery_unknown", True) else REJECTED
        else:
            # The adapter knows how its protocol reports a refusal: OneBot with
            # retcode != 0, Feishu with a non-zero code inside an HTTP 200 body.
            error = self.adapter.send_error_from_response(response)
            if error is not None:
                outcome = REJECTED

        result = SendResult(
            scope_type="group" if event.is_group else "private",
            session_key=session,
            segment_index=index,
            success=error is None,
            recorded_at=datetime.now(),
            error_label="" if error is None else classify_send_error(error),
            outcome=outcome,
            event_id=event.message_id,
        )
        SEND_LOG.record(result)
        if result.success:
            self.stats.segments_sent += 1
            # Only a delivered reply counts against the throttle.
            if self.group_dialogue is None or event.reply_reason in DIRECT_REASONS or index == 1:
                self.throttle.record(self._conversation_key(event))
            if SESSION_REVISIONS.is_current(session, revision):
                message_id = self.adapter.sent_message_id(response)
                self._remember_quote(event, message_id, self.bot_account, text)
                if event.is_group and self.group_dialogue is not None:
                    command, _ = parse_command(event.text, self.settings.command_prefix)
                    if command != "清空上下文":
                        self.group_dialogue.delivered(event, text, message_id)
        else:
            self.stats.segments_failed += 1
        if isinstance(error, asyncio.CancelledError):
            raise error
        return result

    def get_context_for(self, event: MessageEvent) -> BotContext:
        """Expose the context mapping for tests and diagnostics."""
        return event.to_bot_context(self.settings, self.started_at)

    def session_key_for(self, event: MessageEvent):
        return get_session_key(self.get_context_for(event))

