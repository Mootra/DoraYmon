"""Platform-independent inbound message contract.

Every entry point must hand the router the same shape, so business logic never
learns which protocol delivered a message:

    official QQ Bot (botpy)  --+
    QQ account (OneBot/NapCat) +--> MessageEvent --> routing and replies
    Feishu app (long conn.)   --+

The badge of a correct conversion is that nothing platform-specific survives: no
botpy objects, no OneBot action names, no Feishu event envelope. This module
therefore contains no imports of any of those SDKs; converters duck-type the
objects they receive, which also keeps the contract testable offline with plain
stubs.

Identity is carried as raw platform identifiers plus an ``IdentityNamespace``.
Raw identifiers stay unqualified here and are namespaced when they reach storage
(see ``doraymon.identity``), so a converter can never accidentally merge two
platforms.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from services.group_dialogue import GroupDialogueContext

from doraymon.identity import (
    PLATFORM_FEISHU,
    PLATFORM_QQ_BOT,
    IdentityNamespace,
    namespace_for,
)

CONVERSATION_PRIVATE = "private"
CONVERSATION_GROUP = "group"


class ConversationKind(str, Enum):
    PRIVATE = CONVERSATION_PRIVATE
    GROUP = CONVERSATION_GROUP


class AttachmentKind(str, Enum):
    IMAGE = "image"
    VOICE = "voice"
    VIDEO = "video"
    FILE = "file"
    OTHER = "other"


@dataclass(frozen=True)
class Mention:
    """One mentioned account, by raw platform identifier."""

    raw_id: str
    is_bot: bool = False

    def __post_init__(self) -> None:
        if not str(self.raw_id or "").strip():
            raise ValueError("mention raw_id must not be empty")


@dataclass(frozen=True)
class QuotedMessage:
    """The message this one replies to or quotes."""

    message_id: str
    raw_user_id: str = ""
    excerpt: str = ""


@dataclass(frozen=True)
class InputPart:
    """One original message inside a short coalesced input burst.

    The model may receive several messages as one request, but the protocol
    identity of each message remains available for diagnostics and prompt
    rendering.  No message body is read from storage here.
    """

    message_id: str
    text: str
    quote_message_id: str = ""
    quote_author: str = ""
    mentions: tuple[str, ...] = ()
    received_at: datetime | None = None


@dataclass(frozen=True)
class Attachment:
    """Metadata only; the binary content never enters the contract."""

    kind: AttachmentKind
    reference: str = ""
    size_bytes: int | None = None
    filename: str = ""

    def __post_init__(self) -> None:
        if self.size_bytes is not None and int(self.size_bytes) < 0:
            raise ValueError("attachment size must not be negative")


@dataclass(frozen=True)
class MessageEvent:
    """One inbound message, identical in shape for every entry point.

    ``raw_user_id`` / ``raw_group_id`` are exactly what the platform reported.
    The same string on two platforms is two different people, which is why the
    namespace travels with the event instead of being guessed later.
    """

    platform: str
    bot_account: str
    kind: ConversationKind
    message_id: str
    raw_user_id: str
    text: str
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw_group_id: str = ""
    mentions_bot: bool = False
    mentions: tuple[Mention, ...] = ()
    quote: QuotedMessage | None = None
    attachments: tuple[Attachment, ...] = ()
    raw_payload: Any = None
    sender_name: str = ""
    reply_reason: str = ""
    group_dialogue: GroupDialogueContext | None = None
    # Non-empty only when several original events were deliberately combined
    # during the short input window.  The normal one-message path stays cheap.
    input_parts: tuple[InputPart, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.platform or "").strip():
            raise ValueError("platform must not be empty")
        if not str(self.message_id or "").strip():
            raise ValueError("message_id must not be empty")
        if not str(self.raw_user_id or "").strip():
            raise ValueError("raw_user_id must not be empty")
        if self.kind is ConversationKind.GROUP and not str(self.raw_group_id or "").strip():
            raise ValueError("a group message must carry raw_group_id")

    @property
    def namespace(self) -> IdentityNamespace:
        return namespace_for(self.platform, self.bot_account)

    @property
    def session_key(self) -> tuple[str, str, str] | None:
        """Storage session key for this event, or None when identity is unusable."""
        return self.namespace.session_key(self.raw_user_id, self.raw_group_id)

    @property
    def is_group(self) -> bool:
        return self.kind is ConversationKind.GROUP

    @property
    def normalized_text(self) -> str:
        """Trimmed text with a leading bot mention removed."""
        text = str(self.text or "").strip()
        if text.startswith("<@") and ">" in text:
            _, _, remainder = text.partition(">")
            return remainder.strip()
        return text

    @property
    def has_attachments(self) -> bool:
        return bool(self.attachments)

    def replies_to_bot(self) -> bool:
        """True when the message quotes a message the bot itself sent."""
        return self.quote is not None and bool(self.quote.raw_user_id) and (
            self.quote.raw_user_id == self.bot_account
        )

    def should_consider_for_reply(self) -> bool:
        """Whether this message is addressed to the bot at all."""
        if not self.is_group:
            return True
        return self.mentions_bot or self.replies_to_bot() or bool(self.reply_reason)

    def mentions_raw_ids(self) -> tuple[str, ...]:
        return tuple(mention.raw_id for mention in self.mentions)

    def attachment_kinds(self) -> tuple[AttachmentKind, ...]:
        return tuple(attachment.kind for attachment in self.attachments)

    def original_input_parts(self) -> tuple[InputPart, ...]:
        """Return original message boundaries, including the one-message case."""
        if self.input_parts:
            return self.input_parts
        quote = self.quote
        return (
            InputPart(
                message_id=self.message_id,
                text=self.text,
                quote_message_id=quote.message_id if quote else "",
                quote_author=quote.raw_user_id if quote else "",
                mentions=self.mentions_raw_ids(),
                received_at=self.received_at,
            ),
        )

    def to_bot_context(self, settings: Any, started_at: datetime) -> Any:
        """Build the router's BotContext with no platform-specific leftovers."""
        from doraymon.context import BotContext

        return BotContext(
            settings=settings,
            started_at=started_at,
            user_openid=self.raw_user_id,
            group_openid=self.raw_group_id,
            raw_content=self.text,
            is_at_message=self.mentions_bot,
            addressed_to_bot=self.should_consider_for_reply(),
            group_dialogue=self.group_dialogue,
            platform=self.platform,
            bot_account=self.bot_account,
            input_parts=self.original_input_parts(),
        )


# ---------------------------------------------------------------------------
# Converters
# ---------------------------------------------------------------------------


def _text_of(value: Any) -> str:
    return str(getattr(value, "content", "") or "").strip() if value is not None else ""


def _first_identifier(source: Any, names: tuple[str, ...]) -> str:
    for name in names:
        candidate = str(getattr(source, name, "") or "").strip()
        if candidate:
            return candidate
    return ""


def _author_raw_id(message: Any) -> str:
    return _first_identifier(
        getattr(message, "author", None),
        ("member_openid", "user_openid", "id"),
    )


def _botpy_mentions(message: Any, bot_account: str) -> tuple[Mention, ...]:
    mentions: list[Mention] = []
    for entry in getattr(message, "mentions", None) or ():
        raw_id = _first_identifier(entry, ("id", "user_openid", "member_openid"))
        if raw_id:
            mentions.append(Mention(raw_id=raw_id, is_bot=raw_id == bot_account))
    return tuple(mentions)


def _botpy_quote(message: Any) -> QuotedMessage | None:
    quote = getattr(message, "message_reference", None)
    if quote is None:
        quote = getattr(message, "msg_elements", None)
    if quote is None:
        return None
    message_id = _first_identifier(quote, ("message_id", "id"))
    if not message_id:
        return None
    return QuotedMessage(
        message_id=message_id,
        raw_user_id=_first_identifier(quote, ("user_openid", "member_openid", "user_id")),
    )


def _botpy_attachments(message: Any) -> tuple[Attachment, ...]:
    raw = getattr(message, "attachments", None) or ()
    attachments: list[Attachment] = []
    for entry in raw:
        content_type = str(getattr(entry, "content_type", "") or "").lower()
        kind = AttachmentKind.OTHER
        for candidate in AttachmentKind:
            if candidate.value in content_type:
                kind = candidate
                break
        attachments.append(
            Attachment(
                kind=kind,
                reference=str(getattr(entry, "url", "") or ""),
                filename=str(getattr(entry, "filename", "") or ""),
            )
        )
    return tuple(attachments)


def from_botpy_message(
    message: Any,
    *,
    bot_account: str,
    platform: str = PLATFORM_QQ_BOT,
    is_at_message: bool = False,
) -> MessageEvent:
    """Convert one botpy c2c/group message object into a MessageEvent.

    The official SDK reports a group only through ``group_openid``; private
    messages have none. Everything else is duck-typed so a stub object is enough
    to test the conversion.
    """
    group_openid = str(getattr(message, "group_openid", "") or "").strip()
    kind = ConversationKind.GROUP if group_openid else ConversationKind.PRIVATE
    mentions = _botpy_mentions(message, bot_account)
    return MessageEvent(
        platform=platform,
        bot_account=str(bot_account or ""),
        kind=kind,
        message_id=str(getattr(message, "id", "") or ""),
        raw_user_id=_author_raw_id(message),
        text=_text_of(message),
        raw_group_id=group_openid,
        mentions_bot=bool(is_at_message)
        or any(mention.is_bot for mention in mentions)
        or any(mention.raw_id == bot_account for mention in mentions),
        mentions=mentions,
        quote=_botpy_quote(message),
        attachments=_botpy_attachments(message),
        raw_payload=message,
    )

def needs_quote_lookup(event: MessageEvent) -> bool:
    """True when a group reply quotes a message whose author is still unknown.

    Neither protocol states who wrote the quoted message in the message event
    itself: OneBot's reply segment normally carries only an id, and Feishu
    reports only ``parent_id``. Telling whether the bot was quoted can require a
    lookup, which is a transport concern; this only reports that the answer is
    not known yet.
    """
    if not event.is_group or event.quote is None:
        return False
    return not event.quote.raw_user_id


# ---------------------------------------------------------------------------
# Feishu
# ---------------------------------------------------------------------------

# Feishu event vocabulary used by the converter below. Kept here rather than in
# the adapter so the contract states, in one place, which payload shapes it
# understands.
FEISHU_EVENT_MESSAGE_RECEIVE = "im.message.receive_v1"
FEISHU_CHAT_TYPE_PRIVATE = "p2p"
FEISHU_CHAT_TYPE_GROUP = "group"
FEISHU_MESSAGE_TYPE_TEXT = "text"
FEISHU_MESSAGE_TYPE_POST = "post"
FEISHU_MESSAGE_TYPE_IMAGE = "image"
FEISHU_MESSAGE_TYPE_AUDIO = "audio"
FEISHU_MESSAGE_TYPE_MEDIA = "media"
FEISHU_MESSAGE_TYPE_FILE = "file"
FEISHU_MESSAGE_TYPE_STICKER = "sticker"
FEISHU_ATTACHMENT_KINDS = {
    FEISHU_MESSAGE_TYPE_IMAGE: AttachmentKind.IMAGE,
    FEISHU_MESSAGE_TYPE_AUDIO: AttachmentKind.VOICE,
    FEISHU_MESSAGE_TYPE_MEDIA: AttachmentKind.VIDEO,
    FEISHU_MESSAGE_TYPE_FILE: AttachmentKind.FILE,
    FEISHU_MESSAGE_TYPE_STICKER: AttachmentKind.OTHER,
}

_SPACE_RUN = re.compile(r"[ \t]{2,}")


def _feishu_node(payload: Any, name: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    node = payload.get(name)
    return node if isinstance(node, dict) else {}


def _feishu_event(payload: Any) -> dict[str, Any]:
    return _feishu_node(payload, "event")


def _feishu_header(payload: Any) -> dict[str, Any]:
    return _feishu_node(payload, "header")


def _feishu_message(payload: Any) -> dict[str, Any]:
    return _feishu_node(_feishu_event(payload), "message")


def _feishu_content(message: dict[str, Any]) -> dict[str, Any]:
    """Parse ``content``, which Feishu sends as a JSON *string*."""
    raw = message.get("content")
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _feishu_mention_open_id(entry: Any) -> str:
    """A mention's open_id, or "" when the payload does not carry one.

    Only ``open_id`` is read. ``user_id`` and ``union_id`` live in different
    identifier spaces, and a mention is compared against the bot's own open_id
    to decide whether the bot was addressed — mixing the spaces there would
    silently mis-address messages.
    """
    if not isinstance(entry, dict):
        return ""
    identity = entry.get("id")
    if not isinstance(identity, dict):
        return ""
    return str(identity.get("open_id", "") or "").strip()


def _feishu_mentions(message: dict[str, Any], bot_account: str) -> tuple[Mention, ...]:
    mentions: list[Mention] = []
    seen: set[str] = set()
    for entry in message.get("mentions") or ():
        open_id = _feishu_mention_open_id(entry)
        if not open_id or open_id in seen:
            continue
        seen.add(open_id)
        mentions.append(
            Mention(raw_id=open_id, is_bot=bool(bot_account) and open_id == bot_account)
        )
    return tuple(mentions)


def _feishu_post_text(body: dict[str, Any]) -> str:
    """Flatten one rich-text (``post``) body into plain lines."""
    # A post sent by a person may be wrapped in a locale key (zh_cn / en_us).
    if not isinstance(body.get("content"), list):
        for value in body.values():
            if isinstance(value, dict) and isinstance(value.get("content"), list):
                body = value
                break
    parts: list[str] = []
    title = str(body.get("title", "") or "").strip()
    if title:
        parts.append(title)
    for line in body.get("content") or ():
        for node in line or ():
            if not isinstance(node, dict):
                continue
            if str(node.get("tag", "") or "") in {"text", "a", "md"}:
                piece = str(node.get("text", "") or "")
                if piece:
                    parts.append(piece)
    return "\n".join(parts).strip()


def _feishu_text(message: dict[str, Any], content: dict[str, Any]) -> str:
    message_type = str(message.get("message_type", "") or "").strip().lower()
    if message_type == FEISHU_MESSAGE_TYPE_TEXT:
        return str(content.get("text", "") or "")
    if message_type == FEISHU_MESSAGE_TYPE_POST:
        return _feishu_post_text(content)
    # Anything else carries no text: the router should see an empty message and
    # the attachment list rather than a fabricated description of the file.
    return ""


def _feishu_resolve_mentions(
    text: str,
    message: dict[str, Any],
    bot_account: str,
) -> str:
    """Replace mention placeholders with something the router can read.

    The bot's own placeholder is dropped — ``@_user_1 吃什么`` has to reach the
    intent matcher as ``吃什么`` — and every other one becomes ``@<name>`` so a
    request aimed at another member keeps that information.
    """
    for entry in message.get("mentions") or ():
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key", "") or "").strip()
        if not key or key not in text:
            continue
        open_id = _feishu_mention_open_id(entry)
        if bot_account and open_id == bot_account:
            text = text.replace(key, "")
            continue
        name = str(entry.get("name", "") or "").strip() or key
        text = text.replace(key, "@" + name)
    return _SPACE_RUN.sub(" ", text).strip()


def _feishu_quote(message: dict[str, Any]) -> QuotedMessage | None:
    """The message this one replies to, identified but without its author."""
    quoted_id = str(message.get("parent_id", "") or "").strip()
    if not quoted_id:
        quoted_id = str(message.get("root_id", "") or "").strip()
    if not quoted_id:
        return None
    return QuotedMessage(message_id=quoted_id)


def _feishu_attachments(
    message: dict[str, Any],
    content: dict[str, Any],
) -> tuple[Attachment, ...]:
    message_type = str(message.get("message_type", "") or "").strip().lower()
    kind = FEISHU_ATTACHMENT_KINDS.get(message_type)
    if kind is None:
        return ()
    reference = str(
        content.get("image_key")
        or content.get("file_key")
        or content.get("file_token")
        or ""
    ).strip()
    return (
        Attachment(
            kind=kind,
            reference=reference,
            filename=str(content.get("file_name", "") or ""),
        ),
    )


def _feishu_received_at(message: dict[str, Any], header: dict[str, Any]) -> datetime:
    """Feishu reports ``create_time`` as a millisecond epoch string."""
    for source in (message.get("create_time"), header.get("create_time")):
        raw = str(source or "").strip()
        if not raw:
            continue
        try:
            return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            continue
    return datetime.now(timezone.utc)


def from_feishu_event(
    payload: Any,
    *,
    bot_account: str = "",
    platform: str = PLATFORM_FEISHU,
    is_at_message: bool = False,
) -> MessageEvent | None:
    """Convert one Feishu ``im.message.receive_v1`` payload, or return None.

    Returning None for non-message and malformed payloads keeps the caller from
    having to know Feishu's event taxonomy. ``chat_type`` is required rather than
    guessed: reading a group message as private would make every message in that
    group look addressed to the bot, and staying quiet is the safer failure.
    """
    header = _feishu_header(payload)
    event_type = str(header.get("event_type", "") or "").strip()
    if event_type and event_type != FEISHU_EVENT_MESSAGE_RECEIVE:
        return None

    message = _feishu_message(payload)
    if not message:
        return None

    chat_type = str(message.get("chat_type", "") or "").strip().lower()
    if chat_type not in {FEISHU_CHAT_TYPE_PRIVATE, FEISHU_CHAT_TYPE_GROUP}:
        return None
    is_group = chat_type == FEISHU_CHAT_TYPE_GROUP

    message_id = str(message.get("message_id", "") or "").strip()
    if not message_id:
        return None

    sender = _feishu_node(_feishu_event(payload), "sender")
    identity = sender.get("sender_id")
    raw_user_id = (
        str(identity.get("open_id", "") or "").strip()
        if isinstance(identity, dict)
        else ""
    )
    if not raw_user_id:
        return None

    chat_id = str(message.get("chat_id", "") or "").strip()
    if is_group and not chat_id:
        return None

    bot = str(bot_account or "").strip()
    content = _feishu_content(message)
    mentions = _feishu_mentions(message, bot)
    text = _feishu_resolve_mentions(_feishu_text(message, content), message, bot)

    return MessageEvent(
        platform=platform,
        bot_account=bot,
        kind=ConversationKind.GROUP if is_group else ConversationKind.PRIVATE,
        message_id=message_id,
        raw_user_id=raw_user_id,
        text=text,
        received_at=_feishu_received_at(message, header),
        raw_group_id=chat_id if is_group else "",
        mentions_bot=bool(is_at_message) or any(m.is_bot for m in mentions),
        mentions=mentions,
        quote=_feishu_quote(message),
        attachments=_feishu_attachments(message, content),
        raw_payload=payload,
    )
