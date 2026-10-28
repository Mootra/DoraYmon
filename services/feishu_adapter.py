"""Feishu (Lark) adapter: pure translation, no network.

This module turns ``im.message.receive_v1`` events into the platform-independent
``MessageEvent`` and turns replies into Feishu API calls. It performs no I/O, so
the whole mapping can be verified offline against payloads taken from the Feishu
documentation. The transport lives elsewhere (entry point / ``scripts``).

What this layer is responsible for, mirroring the OneBot adapter:

- message and action conversion, including quoted messages and mention info;
- duplicate event suppression, so a redelivered event does not answer twice;
- ignoring anything not sent by a person, so two bots cannot talk in a loop;
- keeping the app's bot ``open_id`` as the identity namespace, because a Feishu
  open_id and a real QQ number are different identities and must never share a
  session.

Feishu reports a message's text as a JSON *string* inside ``content``, and a
mention only as a placeholder key (``@_user_1``) that the ``mentions`` array
resolves. The text is therefore rewritten here: the bot's own placeholder is
removed and every other one becomes ``@<display name>``. Leaving the
placeholders in would hand ``@_user_1 吃什么`` to the intent matcher.

A received message does not say who wrote the message it replies to, so a group
reply that quotes the bot cannot be recognised from the payload alone. That is
the same problem OneBot has, and it is solved the same way: the adapter reports
that a lookup is needed and reads the answer back out of the response.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from doraymon.identity import PLATFORM_FEISHU
from services.action_error import ActionError
from services.event_dedup import DEFAULT_MAX_SEEN_EVENTS, DuplicateEventTracker
from services.message_contract import (
    MessageEvent,
    from_feishu_event,
    needs_quote_lookup,
)

EVENT_TYPE_MESSAGE_RECEIVE = "im.message.receive_v1"

CHAT_TYPE_PRIVATE = "p2p"
CHAT_TYPE_GROUP = "group"

SENDER_TYPE_USER = "user"

MESSAGE_TYPE_TEXT = "text"
MESSAGE_TYPE_POST = "post"
MESSAGE_TYPE_IMAGE = "image"
MESSAGE_TYPE_AUDIO = "audio"
MESSAGE_TYPE_MEDIA = "media"
MESSAGE_TYPE_FILE = "file"
MESSAGE_TYPE_STICKER = "sticker"

MSG_TYPE_TEXT = "text"

RECEIVE_ID_CHAT = "chat_id"
RECEIVE_ID_OPEN = "open_id"

ACTION_CREATE_MESSAGE = "im.v1.message.create"
ACTION_REPLY_MESSAGE = "im.v1.message.reply"
ACTION_GET_MESSAGE = "im.v1.message.get"
ACTION_BOT_INFO = "bot.v3.info"


class FeishuActionError(ActionError):
    """The Feishu API refused an action (``code`` != 0)."""

    def __init__(self, code: Any, message: Any = "") -> None:
        super().__init__("feishu", code=code, message=message)
        self.feishu_message = str(message or "")


@dataclass(frozen=True)
class FeishuAction:
    """One outbound Feishu API call, independent of how it is transported."""

    name: str
    params: dict[str, Any] = field(default_factory=dict)
    echo: str = ""


def encode_text_content(text: str) -> str:
    """Feishu takes text as a JSON string, not as a plain field."""
    return json.dumps({"text": str(text or "")}, ensure_ascii=False)


def _sender_of(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    event = payload.get("event")
    if not isinstance(event, dict):
        return {}
    sender = event.get("sender")
    return sender if isinstance(sender, dict) else {}


def _message_of(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    event = payload.get("event")
    if not isinstance(event, dict):
        return {}
    message = event.get("message")
    return message if isinstance(message, dict) else {}


def _header_of(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    header = payload.get("header")
    return header if isinstance(header, dict) else {}


def sender_open_id(sender: dict[str, Any]) -> str:
    """The sender's open_id, or "" when the payload does not carry one.

    Only ``open_id`` is accepted. Feishu also reports ``user_id`` and
    ``union_id``, which are *different identifier spaces*: mixing them would let
    a comparison against the bot's open_id match the wrong account.
    """
    if not isinstance(sender, dict):
        return ""
    identity = sender.get("sender_id")
    if not isinstance(identity, dict):
        return ""
    return str(identity.get("open_id", "") or "").strip()


class FeishuAdapter:
    """Stateless translation plus duplicate and self-message filtering."""

    def __init__(
        self,
        *,
        bot_account: str,
        platform: str = PLATFORM_FEISHU,
        max_seen_events: int = DEFAULT_MAX_SEEN_EVENTS,
    ) -> None:
        self.bot_account = str(bot_account or "").strip()
        self.platform = platform
        self.duplicates = DuplicateEventTracker(max_events=max_seen_events)

    def is_self_message(self, payload: Any) -> bool:
        """True for anything that was not written by a person.

        Feishu marks a message authored by an app with a ``sender_type`` other
        than ``user``. Comparing the open_id is the precise test and is used
        whenever the bot's own id is configured; the sender type is what still
        holds when it is not, and it also keeps a *second* bot's messages from
        being answered, which is how two bots end up in a loop.
        """
        sender = _sender_of(payload)
        if not sender:
            return False
        open_id = sender_open_id(sender)
        if self.bot_account and open_id and open_id == self.bot_account:
            return True
        sender_type = str(sender.get("sender_type", "") or "").strip().lower()
        return bool(sender_type) and sender_type != SENDER_TYPE_USER

    def adapt(self, payload: Any) -> MessageEvent | None:
        """Convert one payload, or None when it must not be handled.

        None covers: not a message event, malformed identity, a duplicate of an
        already handled event, and anything the app itself sent.
        """
        if self.is_self_message(payload):
            return None
        event_id = self.event_id_of(payload)
        if event_id and self.duplicates.seen_before(event_id):
            return None
        event = from_feishu_event(
            payload, bot_account=self.bot_account, platform=self.platform
        )
        if event is None:
            return None
        if event_id:
            self.duplicates.remember(event_id)
        return event

    def event_id_of(self, payload: Any) -> str:
        """Stable id for one inbound event, or "" when it is not a message.

        The message id is preferred over ``header.event_id``: a redelivery is a
        *new* event carrying the *same* message, and it is the message that must
        not be answered twice.
        """
        header = _header_of(payload)
        event_type = str(header.get("event_type", "") or "").strip()
        if event_type and event_type != EVENT_TYPE_MESSAGE_RECEIVE:
            return ""
        message_id = str(_message_of(payload).get("message_id", "") or "").strip()
        if message_id:
            return message_id
        return str(header.get("event_id", "") or "").strip()

    # -- hooks the session needs to stay protocol-agnostic -------------------

    def needs_quote_lookup(self, event: MessageEvent) -> bool:
        return needs_quote_lookup(event)

    def quote_author_from_response(self, response: Any, *, event: MessageEvent | None = None) -> str:
        """Read the quoted message's author out of a message-get response."""
        if not isinstance(response, dict):
            return ""
        if response.get("code") not in (None, 0):
            return ""
        data = response.get("data")
        if not isinstance(data, dict):
            return ""
        items = data.get("items")
        if not isinstance(items, list) or not items:
            return ""
        first = items[0]
        if not isinstance(first, dict):
            return ""
        if event is not None and event.is_group and first.get("chat_id") is not None and str(first["chat_id"]) != event.raw_group_id:
            return ""
        sender = first.get("sender")
        if not isinstance(sender, dict):
            return ""
        # The documented shape is {"id": "ou_…", "id_type": "open_id", …}; the
        # nested form is accepted because some endpoints report sender_id.
        for field in ("id", "open_id"):
            candidate = sender.get(field)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        identity = sender.get("sender_id")
        if isinstance(identity, dict):
            candidate = str(identity.get("open_id", "") or "").strip()
            if candidate:
                return candidate
        return ""

    def send_error_from_response(self, response: Any) -> BaseException | None:
        """Turn a Feishu API response into a failure, or None on success.

        The HTTP status is not enough: Feishu answers ``200`` with a non-zero
        ``code`` when it refuses the call, so the body decides.
        """
        if not isinstance(response, dict):
            return None
        code = response.get("code")
        if code in (None, 0):
            return None
        return FeishuActionError(code, response.get("msg") or response.get("message"))

    def quote_text_from_response(self, response: Any, *, event: MessageEvent) -> str:
        if not isinstance(response, dict) or self.send_error_from_response(response) is not None:
            return ""
        data = response.get("data")
        items = data.get("items") if isinstance(data, dict) else None
        first = items[0] if isinstance(items, list) and items else None
        if event.is_group and (not isinstance(first, dict) or str(first.get("chat_id", "")) != event.raw_group_id):
            return ""
        body = first.get("body") if isinstance(first, dict) else None
        try:
            content = json.loads(body.get("content", "{}")) if isinstance(body, dict) else {}
        except (TypeError, ValueError):
            return ""
        return str(content.get("text", ""))[:600] if isinstance(content, dict) else ""

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
        echo: str = "",
    ) -> FeishuAction:
        """Build the action that sends a reply to this event.

        Quoting the triggering message is explicit rather than automatic: the
        plan requires a real quote, never a fabricated one.
        """
        content = str(text or "")
        if not content.strip():
            raise ValueError("reply text must not be empty")
        payload = encode_text_content(content)
        target = str(reply_to_message_id or "").strip()
        if target:
            return FeishuAction(
                name=ACTION_REPLY_MESSAGE,
                params={
                    "message_id": target,
                    "msg_type": MSG_TYPE_TEXT,
                    "content": payload,
                },
                echo=echo or event.message_id,
            )
        if event.is_group:
            # A group is addressed by its chat id; a private chat is addressed by
            # the person's open_id, which is the same conversation either way.
            receive_id_type = RECEIVE_ID_CHAT
            receive_id = event.raw_group_id
        else:
            receive_id_type = RECEIVE_ID_OPEN
            receive_id = event.raw_user_id
        return FeishuAction(
            name=ACTION_CREATE_MESSAGE,
            params={
                "receive_id_type": receive_id_type,
                "receive_id": receive_id,
                "msg_type": MSG_TYPE_TEXT,
                "content": payload,
            },
            echo=echo or event.message_id,
        )

    def get_message_action(self, message_id: str, *, echo: str = "") -> FeishuAction:
        """Ask the API for one message, used to resolve an unknown quote."""
        normalized = str(message_id or "").strip()
        if not normalized:
            raise ValueError("message_id must not be empty")
        return FeishuAction(
            name=ACTION_GET_MESSAGE,
            params={"message_id": normalized},
            echo=echo or f"get_message:{normalized}",
        )

    def bot_info_action(self, *, echo: str = "bot_info") -> FeishuAction:
        """Confirm which bot identity the credentials actually belong to."""
        return FeishuAction(name=ACTION_BOT_INFO, params={}, echo=echo)
