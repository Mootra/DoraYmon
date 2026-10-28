"""P0: one message shape for every QQ entry point."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import unittest

from services.message_contract import (
    Attachment,
    AttachmentKind,
    ConversationKind,
    Mention,
    MessageEvent,
    QuotedMessage,
    from_botpy_message,
)


@dataclass
class StubAuthor:
    member_openid: str = ""
    user_openid: str = ""
    id: str = ""


@dataclass
class StubMention:
    id: str = ""


@dataclass
class StubAttachment:
    content_type: str = ""
    url: str = ""
    filename: str = ""


@dataclass
class StubReference:
    message_id: str = ""
    user_openid: str = ""


@dataclass
class StubMessage:
    id: str = "message-1"
    content: str = "你好"
    author: StubAuthor = field(default_factory=StubAuthor)
    group_openid: str = ""
    mentions: list = field(default_factory=list)
    attachments: list = field(default_factory=list)
    message_reference: object = None


def _event(**overrides) -> MessageEvent:
    payload = {
        "platform": "qqbot",
        "bot_account": "bot-1",
        "kind": ConversationKind.PRIVATE,
        "message_id": "m-1",
        "raw_user_id": "user-a",
        "text": "你好",
    }
    payload.update(overrides)
    return MessageEvent(**payload)


class MessageEventValidationTest(unittest.TestCase):
    def test_group_message_requires_a_group_id(self) -> None:
        with self.assertRaises(ValueError):
            _event(kind=ConversationKind.GROUP)

    def test_private_message_needs_no_group_id(self) -> None:
        self.assertEqual(_event().raw_group_id, "")

    def test_identity_fields_are_required(self) -> None:
        for overrides in (
            {"message_id": "  "},
            {"raw_user_id": ""},
            {"platform": ""},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    _event(**overrides)


class SessionKeyTest(unittest.TestCase):
    def test_private_event_namespaces_its_session_key(self) -> None:
        self.assertEqual(
            _event().session_key,
            ("private", "qqbot:bot-1:user-a", "qqbot:bot-1:user-a"),
        )

    def test_group_event_namespaces_group_and_speaker(self) -> None:
        event = _event(kind=ConversationKind.GROUP, raw_group_id="group-a")

        self.assertEqual(
            event.session_key,
            ("group", "qqbot:bot-1:group-a", "qqbot:bot-1:user-a"),
        )

    def test_same_string_id_on_two_platforms_differs(self) -> None:
        official = _event()
        personal = _event(platform="qqaccount")

        self.assertNotEqual(official.session_key, personal.session_key)

    def test_session_key_is_none_without_a_user(self) -> None:
        event = _event()
        object.__setattr__(event, "raw_user_id", "")

        self.assertIsNone(event.session_key)


class AddressingTest(unittest.TestCase):
    def test_private_message_is_always_addressed_to_the_bot(self) -> None:
        self.assertTrue(_event().should_consider_for_reply())

    def test_group_message_without_mention_is_not_addressed(self) -> None:
        event = _event(kind=ConversationKind.GROUP, raw_group_id="group-a")

        self.assertFalse(event.should_consider_for_reply())

    def test_group_mention_is_addressed(self) -> None:
        event = _event(kind=ConversationKind.GROUP, raw_group_id="group-a", mentions_bot=True)

        self.assertTrue(event.should_consider_for_reply())

    def test_quoting_the_bot_counts_as_addressed(self) -> None:
        event = _event(
            kind=ConversationKind.GROUP,
            raw_group_id="group-a",
            quote=QuotedMessage(message_id="m-0", raw_user_id="bot-1"),
        )

        self.assertTrue(event.replies_to_bot())
        self.assertTrue(event.should_consider_for_reply())

    def test_quoting_another_member_is_not_addressed(self) -> None:
        event = _event(
            kind=ConversationKind.GROUP,
            raw_group_id="group-a",
            quote=QuotedMessage(message_id="m-0", raw_user_id="user-b"),
        )

        self.assertFalse(event.replies_to_bot())
        self.assertFalse(event.should_consider_for_reply())

    def test_quote_without_a_speaker_is_not_treated_as_the_bot(self) -> None:
        event = _event(
            kind=ConversationKind.GROUP,
            raw_group_id="group-a",
            quote=QuotedMessage(message_id="m-0"),
        )

        self.assertFalse(event.replies_to_bot())


class TextAndAttachmentTest(unittest.TestCase):
    def test_leading_mention_is_stripped_from_the_text(self) -> None:
        event = _event(text="<@!bot-1> 今晚吃什么")

        self.assertEqual(event.normalized_text, "今晚吃什么")

    def test_plain_text_is_only_trimmed(self) -> None:
        self.assertEqual(_event(text="  你好  ").normalized_text, "你好")

    def test_attachments_are_metadata_only(self) -> None:
        event = _event(
            attachments=(
                Attachment(kind=AttachmentKind.IMAGE, reference="url-1", size_bytes=1024),
            )
        )

        self.assertTrue(event.has_attachments)
        self.assertEqual(event.attachment_kinds(), (AttachmentKind.IMAGE,))
        self.assertFalse(hasattr(event.attachments[0], "content"))

    def test_negative_attachment_size_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Attachment(kind=AttachmentKind.FILE, size_bytes=-1)

    def test_empty_mention_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Mention(raw_id="  ")


class BotpyConversionTest(unittest.TestCase):
    def test_private_message_converts(self) -> None:
        message = StubMessage(
            id="m-1",
            content="你好",
            author=StubAuthor(user_openid="user-a"),
        )

        event = from_botpy_message(message, bot_account="bot-1")

        self.assertEqual(event.platform, "qqbot")
        self.assertEqual(event.kind, ConversationKind.PRIVATE)
        self.assertEqual(event.message_id, "m-1")
        self.assertEqual(event.raw_user_id, "user-a")
        self.assertEqual(event.text, "你好")
        self.assertEqual(event.raw_group_id, "")
        self.assertFalse(event.mentions_bot)
        self.assertIs(event.raw_payload, message)

    def test_group_message_converts_with_group_and_member_id(self) -> None:
        message = StubMessage(
            id="m-2",
            content="@bot 你好",
            author=StubAuthor(member_openid="user-a"),
            group_openid="group-a",
        )

        event = from_botpy_message(message, bot_account="bot-1", is_at_message=True)

        self.assertEqual(event.kind, ConversationKind.GROUP)
        self.assertEqual(event.raw_group_id, "group-a")
        self.assertEqual(event.raw_user_id, "user-a")
        self.assertTrue(event.mentions_bot)

    def test_mention_of_the_bot_marks_the_event_addressed(self) -> None:
        message = StubMessage(
            content="<@!bot-1> 你好",
            author=StubAuthor(member_openid="user-a"),
            group_openid="group-a",
            mentions=[StubMention(id="bot-1")],
        )

        event = from_botpy_message(message, bot_account="bot-1")

        self.assertTrue(event.mentions_bot)
        self.assertEqual(event.mentions_raw_ids(), ("bot-1",))
        self.assertTrue(event.should_consider_for_reply())

    def test_mention_of_another_member_does_not_address_the_bot(self) -> None:
        message = StubMessage(
            content="<@!user-b> 你好",
            author=StubAuthor(member_openid="user-a"),
            group_openid="group-a",
            mentions=[StubMention(id="user-b")],
        )

        event = from_botpy_message(message, bot_account="bot-1")

        self.assertFalse(event.mentions_bot)
        self.assertFalse(event.should_consider_for_reply())

    def test_message_reference_becomes_a_quote(self) -> None:
        message = StubMessage(
            content="接着刚才那个",
            author=StubAuthor(member_openid="user-a"),
            group_openid="group-a",
            message_reference=StubReference(message_id="m-0", user_openid="bot-1"),
        )

        event = from_botpy_message(message, bot_account="bot-1")

        self.assertIsNotNone(event.quote)
        self.assertEqual(event.quote.message_id, "m-0")
        self.assertEqual(event.quote.raw_user_id, "bot-1")
        self.assertTrue(event.should_consider_for_reply())

    def test_reference_without_an_id_is_ignored(self) -> None:
        message = StubMessage(
            author=StubAuthor(user_openid="user-a"),
            message_reference=StubReference(),
        )

        event = from_botpy_message(message, bot_account="bot-1")

        self.assertIsNone(event.quote)

    def test_attachments_are_classified_by_content_type(self) -> None:
        message = StubMessage(
            author=StubAuthor(user_openid="user-a"),
            attachments=[
                StubAttachment(content_type="image/png", url="url-image"),
                StubAttachment(content_type="file", url="url-file", filename="menu.csv"),
                StubAttachment(content_type="application/x-unknown", url="url-other"),
            ],
        )

        event = from_botpy_message(message, bot_account="bot-1")

        self.assertEqual(
            event.attachment_kinds(),
            (AttachmentKind.IMAGE, AttachmentKind.FILE, AttachmentKind.OTHER),
        )
        self.assertEqual(event.attachments[1].filename, "menu.csv")

    def test_conversion_produces_no_platform_objects_in_the_text_fields(self) -> None:
        message = StubMessage(author=StubAuthor(user_openid="user-a"))

        event = from_botpy_message(message, bot_account="bot-1")

        self.assertIsInstance(event.text, str)
        self.assertIsInstance(event.raw_user_id, str)
        self.assertIsInstance(event.received_at, datetime)

    def test_missing_identity_is_rejected_rather_than_guessed(self) -> None:
        message = StubMessage(author=StubAuthor())

        with self.assertRaises(ValueError):
            from_botpy_message(message, bot_account="bot-1")


if __name__ == "__main__":
    unittest.main()
