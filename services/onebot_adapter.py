"""OneBot v11 (NapCat) adapter: pure translation, no network.

This module turns OneBot 11 event payloads into the platform-independent
``MessageEvent`` and turns replies into OneBot action calls. It performs no I/O,
so the whole mapping can be verified offline against payloads captured from a
simulated endpoint. The transport lives elsewhere (``scripts``/entry point).

What this layer is responsible for, per the plan's P1a:

- message and action conversion, including quoted messages and mention info;
- duplicate event suppression, so a replayed event does not answer twice;
- ignoring the bot's own messages;
- keeping the bot's QQ number as the identity namespace, because a real QQ
  number and an official openid are different identities.

OneBot "message" fields are accepted both as a raw string and as the segment
array form, because real deployments report either.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from doraymon.identity import PLATFORM_QQ_ACCOUNT
from services.action_error import ActionError
from services.event_dedup import DEFAULT_MAX_SEEN_EVENTS, DuplicateEventTracker
from services.message_contract import (
    Attachment,
    AttachmentKind,
    ConversationKind,
    Mention,
    MessageEvent,
    QuotedMessage,
    needs_quote_lookup,
)

POST_TYPE_MESSAGE = "message"
MESSAGE_TYPE_PRIVATE = "private"
MESSAGE_TYPE_GROUP = "group"

ACTION_SEND_PRIVATE = "send_private_msg"
ACTION_SEND_GROUP = "send_group_msg"

SEGMENT_TEXT = "text"
SEGMENT_AT = "at"
SEGMENT_REPLY = "reply"
SEGMENT_IMAGE = "image"
SEGMENT_RECORD = "record"
SEGMENT_VIDEO = "video"
SEGMENT_FILE = "file"


@dataclass(frozen=True)
class OneBotAction:
    """One outbound action request, independent of how it is transported."""

    action: str
    params: dict[str, Any] = field(default_factory=dict)
    echo: str = ""


class OneBotActionError(ActionError):
    """The OneBot endpoint refused an action (``retcode`` != 0)."""

    def __init__(self, retcode: Any, message: Any = "") -> None:
        super().__init__("onebot", code=retcode, message=message)
        self.retcode = retcode
        self.onebot_message = str(message or "")


def _event_id(payload: dict[str, Any]) -> str:
    """A stable identity for one inbound event."""
    message_id = payload.get("message_id")
    if message_id is not None and str(message_id).strip():
        return f"msg:{message_id}"
    time_value = payload.get("time")
    user = payload.get("user_id")
    group = payload.get("group_id") or ""
    return f"anon:{time_value}:{group}:{user}"


def _segments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    message = payload.get("message")
    if isinstance(message, list):
        return [segment for segment in message if isinstance(segment, dict)]
    if isinstance(message, str):
        segments: list[dict[str, Any]] = []
        offset = 0
        for match in re.finditer(r"\[CQ:([a-z_]+)((?:,[^\]]*)?)\]", message):
            if match.start() > offset:
                segments.append({"type": "text", "data": {"text": _unescape_cq(message[offset:match.start()])}})
            data = {}
            for item in match.group(2).lstrip(",").split(","):
                key, separator, value = item.partition("=")
                if separator:
                    data[key] = _unescape_cq(value)
            segments.append({"type": match.group(1), "data": data})
            offset = match.end()
        if offset < len(message):
            segments.append({"type": "text", "data": {"text": _unescape_cq(message[offset:])}})
        return segments
    return []


def _unescape_cq(text: str) -> str:
    return text.replace("&#91;", "[").replace("&#93;", "]").replace("&#44;", ",").replace("&amp;", "&")


def _text_from_segments(segments: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for segment in segments:
        if str(segment.get("type", "")) == SEGMENT_TEXT:
            parts.append(str((segment.get("data") or {}).get("text", "")))
    return "".join(parts)


def _raw_message_text(payload: dict[str, Any]) -> str:
    message = payload.get("message")
    segments = _segments(payload)
    if segments:
        return _text_from_segments(segments)
    return str(payload.get("raw_message", "") or "")


def _mentions(segments: list[dict[str, Any]], bot_account: str) -> tuple[Mention, ...]:
    mentions: list[Mention] = []
    for segment in segments:
        if str(segment.get("type", "")) != SEGMENT_AT:
            continue
        data = segment.get("data") or {}
        raw_id = str(data.get("qq", "") or "").strip()
        if not raw_id:
            continue
        mentions.append(Mention(raw_id=raw_id, is_bot=raw_id == str(bot_account)))
    return tuple(mentions)


def _quote(segments: list[dict[str, Any]]) -> QuotedMessage | None:
    for segment in segments:
        if str(segment.get("type", "")) != SEGMENT_REPLY:
            continue
        data = segment.get("data") or {}
        message_id = str(data.get("id", "") or "").strip()
        if not message_id:
            continue
        return QuotedMessage(
            message_id=message_id,
            raw_user_id=str(data.get("user_id", "") or "").strip(),
        )
    return None


def _attachments(segments: list[dict[str, Any]]) -> tuple[Attachment, ...]:
    mapping = {
        SEGMENT_IMAGE: AttachmentKind.IMAGE,
        SEGMENT_RECORD: AttachmentKind.VOICE,
        SEGMENT_VIDEO: AttachmentKind.VIDEO,
        SEGMENT_FILE: AttachmentKind.FILE,
    }
    attachments: list[Attachment] = []
    for segment in segments:
        kind = mapping.get(str(segment.get("type", "")))
        if kind is None:
            continue
        data = segment.get("data") or {}
        attachments.append(
            Attachment(
                kind=kind,
                reference=str(data.get("url") or data.get("file") or ""),
                filename=str(data.get("name") or data.get("file") or ""),
            )
        )
    return tuple(attachments)


def from_onebot_event(
    payload: dict[str, Any],
    *,
    bot_account: str,
    platform: str = PLATFORM_QQ_ACCOUNT,
) -> MessageEvent | None:
    """Convert one OneBot message event, or return None when it is not one.

    Returning None for non-message and malformed payloads keeps the caller from
    having to know OneBot's event taxonomy.
    """
    if not isinstance(payload, dict):
        return None
    if str(payload.get("post_type", POST_TYPE_MESSAGE)) != POST_TYPE_MESSAGE:
        return None
    if str(payload.get("message_type", "")).strip() not in {
        MESSAGE_TYPE_PRIVATE,
        MESSAGE_TYPE_GROUP,
    }:
        return None

    bot = str(bot_account or "").strip()
    raw_user_id = str(payload.get("user_id", "") or "").strip()
    message_id = str(payload.get("message_id", "") or "").strip() or _event_id(payload)
    if not raw_user_id or not message_id:
        return None

    group_id = str(payload.get("group_id", "") or "").strip()
    is_group = str(payload.get("message_type")) == MESSAGE_TYPE_GROUP
    if is_group and not group_id:
        return None

    segments = _segments(payload)
    mentions = _mentions(segments, bot)
    text = _raw_message_text(payload)
    mentions_bot = any(mention.is_bot for mention in mentions)
    quote = _quote(segments)

    timestamp = payload.get("time")
    try:
        received_at = (
            datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
            if timestamp is not None
            else datetime.now(timezone.utc)
        )
    except (TypeError, ValueError, OSError):
        received_at = datetime.now(timezone.utc)

    return MessageEvent(
        platform=platform,
        bot_account=bot,
        kind=ConversationKind.GROUP if is_group else ConversationKind.PRIVATE,
        message_id=message_id,
        raw_user_id=raw_user_id,
        text=text,
        received_at=received_at,
        raw_group_id=group_id,
        mentions_bot=mentions_bot,
        mentions=mentions,
        quote=quote,
        attachments=_attachments(segments),
        raw_payload=payload,
        sender_name=str((payload.get("sender") or {}).get("card") or (payload.get("sender") or {}).get("nickname") or raw_user_id)
        if isinstance(payload.get("sender", {}), dict) else raw_user_id,
    )


class OneBotAdapter:
    """Stateless translation plus duplicate and self-message filtering."""

    def __init__(
        self,
        *,
        bot_account: str,
        platform: str = PLATFORM_QQ_ACCOUNT,
        max_seen_events: int = DEFAULT_MAX_SEEN_EVENTS,
    ) -> None:
        self.bot_account = str(bot_account or "").strip()
        self.platform = platform
        self.duplicates = DuplicateEventTracker(max_events=max_seen_events)

    def is_self_message(self, payload: dict[str, Any]) -> bool:
        if not self.bot_account:
            return False
        return str(payload.get("user_id", "") or "").strip() == self.bot_account

    def adapt(self, payload: dict[str, Any]) -> MessageEvent | None:
        """Convert one payload, or None when it must not be handled.

        None covers: not a message event, malformed identity, a duplicate of an
        already handled event, and the bot's own message.
        """
        if self.is_self_message(payload):
            return None
        event_id = self._event_id_of(payload)
        if event_id and self.duplicates.seen_before(event_id):
            return None
        event = from_onebot_event(
            payload, bot_account=self.bot_account, platform=self.platform
        )
        if event is None:
            return None
        if event_id:
            self.duplicates.remember(event_id)
        return event

    def event_id_of(self, payload: dict[str, Any]) -> str:
        """Stable id for one inbound event, or "" when it is not a message."""
        if not isinstance(payload, dict) or str(payload.get("post_type", POST_TYPE_MESSAGE)) != POST_TYPE_MESSAGE:
            return ""
        return _event_id(payload)

    # Kept as the internal name used before it became part of the session API.
    _event_id_of = event_id_of

    # -- hooks the session needs to stay protocol-agnostic -------------------

    def needs_quote_lookup(self, event: MessageEvent) -> bool:
        return needs_quote_lookup(event)

    def quote_author_from_response(self, response: Any, *, event: MessageEvent | None = None) -> str:
        """Read the quoted message's author out of a ``get_msg`` response."""
        if not isinstance(response, dict):
            return ""
        if response.get("retcode") not in (None, 0):
            return ""
        data = response.get("data")
        if not isinstance(data, dict):
            return ""
        if event is not None and event.is_group and (
            data.get("message_type") == "private"
            or (data.get("group_id") is not None and str(data["group_id"]) != event.raw_group_id)
        ):
            return ""
        sender = data.get("sender")
        if isinstance(sender, dict):
            for field in ("user_id", "qq", "id"):
                candidate = str(sender.get(field, "") or "").strip()
                if candidate:
                    return candidate
        for field in ("user_id", "sender_id"):
            candidate = str(data.get(field, "") or "").strip()
            if candidate:
                return candidate
        return ""

    def send_error_from_response(self, response: Any) -> BaseException | None:
        """Turn an OneBot action response into a failure, or None on success."""
        if not isinstance(response, dict):
            return None
        retcode = response.get("retcode")
        if retcode in (None, 0):
            return None
        return OneBotActionError(
            retcode, response.get("message") or response.get("wording")
        )

    def quote_text_from_response(self, response: Any, *, event: MessageEvent) -> str:
        if self.send_error_from_response(response) is not None or not isinstance(response, dict):
            return ""
        data = response.get("data")
        if not isinstance(data, dict):
            return ""
        # get_msg can retrieve messages outside this group. Missing scope may
        # resolve an author for older endpoints, but must never expose its body.
        if event.is_group and (str(data.get("group_id", "")) != event.raw_group_id or data.get("message_type") == "private"):
            return ""
        return _raw_message_text(data)[:600]

    def sent_message_id(self, response: Any) -> str:
        data = response.get("data") if isinstance(response, dict) else None
        return str(data.get("message_id", "") or "") if isinstance(data, dict) else ""

    # -- outbound -----------------------------------------------------------

    def reply_action(
        self,
        event: MessageEvent,
        text: str,
        *,
        reply_to_message_id: str = "",
        auto_escape: bool = False,
        echo: str = "",
    ) -> OneBotAction:
        """Build the action that sends a reply to this event.

        Quoting the triggering message is explicit rather than automatic: the
        plan requires a real quote, never a fabricated one, and some deployments
        reject replies without quoting support.
        """
        content = str(text or "")
        if not content.strip():
            raise ValueError("reply text must not be empty")
        params: dict[str, Any] = {"message": content, "auto_escape": bool(auto_escape)}
        if event.is_group:
            params["group_id"] = int(event.raw_group_id) if event.raw_group_id.isdigit() else event.raw_group_id
            action = ACTION_SEND_GROUP
        else:
            params["user_id"] = int(event.raw_user_id) if event.raw_user_id.isdigit() else event.raw_user_id
            action = ACTION_SEND_PRIVATE
        if reply_to_message_id:
            params["message"] = [
                {"type": SEGMENT_REPLY, "data": {"id": str(reply_to_message_id)}},
                {"type": SEGMENT_TEXT, "data": {"text": content}},
            ]
        return OneBotAction(action=action, params=params, echo=echo or event.message_id)

    def get_message_action(self, message_id: str, *, echo: str = "") -> OneBotAction:
        """Ask the endpoint for one message, used to resolve an unknown quote."""
        normalized = str(message_id or "").strip()
        if not normalized:
            raise ValueError("message_id must not be empty")
        return OneBotAction(
            action="get_msg",
            params={"message_id": int(normalized) if normalized.isdigit() else normalized},
            echo=echo or f"get_msg:{normalized}",
        )

    def login_info_action(self, *, echo: str = "login_info") -> OneBotAction:
        """Confirm which account is actually logged in."""
        return OneBotAction(action="get_login_info", params={}, echo=echo)
