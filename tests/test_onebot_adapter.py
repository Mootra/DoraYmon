"""P1a: OneBot v11 payloads convert into the shared message contract."""

from __future__ import annotations

import unittest

from doraymon.identity import PLATFORM_QQ_ACCOUNT
from services.message_contract import AttachmentKind, ConversationKind
from services.onebot_adapter import (
    ACTION_SEND_GROUP,
    ACTION_SEND_PRIVATE,
    DuplicateEventTracker,
    OneBotAdapter,
    from_onebot_event,
    needs_quote_lookup,
)

BOT = "12345"


def private_payload(**overrides) -> dict:
    payload = {
        "post_type": "message",
        "message_type": "private",
        "time": 1760000000,
        "self_id": 12345,
        "user_id": 67890,
        "message_id": 1001,
        "raw_message": "你好",
        "message": "你好",
        "sender": {"user_id": 67890, "nickname": "朋友"},
    }
    payload.update(overrides)
    return payload


def group_payload(**overrides) -> dict:
    payload = {
        "post_type": "message",
        "message_type": "group",
        "time": 1760000000,
        "self_id": 12345,
        "group_id": 555,
        "user_id": 67890,
        "message_id": 1002,
        "raw_message": "[CQ:at,qq=12345] 你好",
        "message": [
            {"type": "at", "data": {"qq": "12345"}},
            {"type": "text", "data": {"text": " 你好"}},
        ],
    }
    payload.update(overrides)
    return payload


class ConversionTest(unittest.TestCase):
    def test_private_message_converts(self) -> None:
        event = from_onebot_event(private_payload(), bot_account=BOT)

        self.assertEqual(event.platform, PLATFORM_QQ_ACCOUNT)
        self.assertEqual(event.kind, ConversationKind.PRIVATE)
        self.assertEqual(event.raw_user_id, "67890")
        self.assertEqual(event.message_id, "1001")
        self.assertEqual(event.text, "你好")
        self.assertEqual(event.raw_group_id, "")
        self.assertFalse(event.mentions_bot)

    def test_group_message_converts_with_a_mention(self) -> None:
        event = from_onebot_event(group_payload(), bot_account=BOT)

        self.assertEqual(event.kind, ConversationKind.GROUP)
        self.assertEqual(event.raw_group_id, "555")
        self.assertEqual(event.raw_user_id, "67890")
        self.assertTrue(event.mentions_bot)
        self.assertEqual(event.mentions_raw_ids(), (BOT,))
        self.assertTrue(event.should_consider_for_reply())

    def test_group_message_without_a_mention_is_not_addressed(self) -> None:
        event = from_onebot_event(
            group_payload(
                message=[{"type": "text", "data": {"text": "随便说说"}}],
                raw_message="随便说说",
            ),
            bot_account=BOT,
        )

        self.assertFalse(event.mentions_bot)
        self.assertFalse(event.should_consider_for_reply())

    def test_group_message_without_a_group_id_is_rejected(self) -> None:
        payload = group_payload()
        payload.pop("group_id")

        self.assertIsNone(from_onebot_event(payload, bot_account=BOT))

    def test_non_message_event_is_ignored(self) -> None:
        for payload in (
            {"post_type": "notice", "notice_type": "group_recall"},
            {"post_type": "meta_event", "meta_event_type": "heartbeat"},
            {"post_type": "message", "message_type": "guild"},
        ):
            with self.subTest(payload=payload):
                self.assertIsNone(from_onebot_event(payload, bot_account=BOT))

    def test_identity_is_required(self) -> None:
        payload = private_payload()
        payload.pop("user_id")

        self.assertIsNone(from_onebot_event(payload, bot_account=BOT))

    def test_string_and_array_message_forms_agree_on_text(self) -> None:
        from_string = from_onebot_event(private_payload(message="你好"), bot_account=BOT)
        from_array = from_onebot_event(
            private_payload(message=[{"type": "text", "data": {"text": "你好"}}]),
            bot_account=BOT,
        )

        self.assertEqual(from_string.text, from_array.text)

    def test_missing_time_does_not_break_the_event(self) -> None:
        payload = private_payload()
        payload.pop("time")

        event = from_onebot_event(payload, bot_account=BOT)

        self.assertIsNotNone(event.received_at)

    def test_unparseable_time_falls_back_to_now(self) -> None:
        event = from_onebot_event(private_payload(time="not-a-time"), bot_account=BOT)

        self.assertIsNotNone(event.received_at)

    def test_attachments_are_classified(self) -> None:
        event = from_onebot_event(
            group_payload(
                message=[
                    {"type": "at", "data": {"qq": BOT}},
                    {"type": "image", "data": {"url": "http://x/1.png", "file": "1.png"}},
                    {"type": "file", "data": {"file": "menu.csv", "name": "menu.csv"}},
                    {"type": "record", "data": {"url": "http://x/1.amr"}},
                ]
            ),
            bot_account=BOT,
        )

        self.assertEqual(
            event.attachment_kinds(),
            (AttachmentKind.IMAGE, AttachmentKind.FILE, AttachmentKind.VOICE),
        )
        self.assertEqual(event.attachments[1].filename, "menu.csv")

    def test_reply_segment_becomes_a_quote(self) -> None:
        event = from_onebot_event(
            group_payload(
                message=[
                    {"type": "reply", "data": {"id": "1000"}},
                    {"type": "text", "data": {"text": "接着说"}},
                ]
            ),
            bot_account=BOT,
        )

        self.assertIsNotNone(event.quote)
        self.assertEqual(event.quote.message_id, "1000")

    def test_quote_with_a_known_bot_author_addresses_the_bot(self) -> None:
        event = from_onebot_event(
            group_payload(
                message=[
                    {"type": "reply", "data": {"id": "1000", "user_id": BOT}},
                    {"type": "text", "data": {"text": "接着说"}},
                ]
            ),
            bot_account=BOT,
        )

        self.assertTrue(event.replies_to_bot())
        self.assertTrue(event.should_consider_for_reply())

    def test_quote_without_an_author_needs_a_lookup(self) -> None:
        event = from_onebot_event(
            group_payload(
                message=[
                    {"type": "reply", "data": {"id": "1000"}},
                    {"type": "text", "data": {"text": "接着说"}},
                ]
            ),
            bot_account=BOT,
        )

        self.assertTrue(needs_quote_lookup(event))
        self.assertFalse(event.should_consider_for_reply())

    def test_quote_with_an_author_needs_no_lookup(self) -> None:
        event = from_onebot_event(
            group_payload(
                message=[{"type": "reply", "data": {"id": "1000", "user_id": BOT}}]
            ),
            bot_account=BOT,
        )

        self.assertFalse(needs_quote_lookup(event))

    def test_private_message_never_needs_a_quote_lookup(self) -> None:
        event = from_onebot_event(private_payload(), bot_account=BOT)

        self.assertFalse(needs_quote_lookup(event))


class DuplicateTrackerTest(unittest.TestCase):
    def test_first_sighting_is_admitted_and_repeat_is_not(self) -> None:
        tracker = DuplicateEventTracker()

        self.assertTrue(tracker.admit("msg:1"))
        self.assertFalse(tracker.admit("msg:1"))

    def test_empty_id_is_never_treated_as_a_duplicate(self) -> None:
        tracker = DuplicateEventTracker()

        self.assertTrue(tracker.admit(""))
        self.assertTrue(tracker.admit(None))

    def test_window_stays_bounded_and_forgets_the_oldest(self) -> None:
        tracker = DuplicateEventTracker(max_events=3)
        for index in range(5):
            tracker.admit(f"msg:{index}")

        self.assertEqual(len(tracker), 3)
        self.assertFalse(tracker.seen_before("msg:0"))
        self.assertTrue(tracker.seen_before("msg:4"))

    def test_clear_resets_the_window(self) -> None:
        tracker = DuplicateEventTracker()
        tracker.admit("msg:1")

        tracker.clear()

        self.assertEqual(len(tracker), 0)
        self.assertTrue(tracker.admit("msg:1"))


class AdapterTest(unittest.TestCase):
    def _adapter(self) -> OneBotAdapter:
        return OneBotAdapter(bot_account=BOT)

    def test_duplicate_event_is_not_adapted_twice(self) -> None:
        adapter = self._adapter()

        first = adapter.adapt(private_payload())
        second = adapter.adapt(private_payload())

        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_own_message_is_ignored(self) -> None:
        adapter = self._adapter()

        self.assertTrue(adapter.is_self_message(private_payload(user_id=12345)))
        self.assertIsNone(adapter.adapt(private_payload(user_id=12345)))

    def test_own_message_ignored_even_before_duplicate_check(self) -> None:
        adapter = self._adapter()
        payload = private_payload(user_id=12345)

        for _ in range(3):
            with self.subTest(repeat=_):
                self.assertIsNone(adapter.adapt(payload))
        self.assertEqual(len(adapter.duplicates), 0)

    def test_malformed_payload_is_ignored_without_recording(self) -> None:
        adapter = self._adapter()

        self.assertIsNone(adapter.adapt({"post_type": "notice"}))
        self.assertEqual(len(adapter.duplicates), 0)

    def test_distinct_events_are_both_adapted(self) -> None:
        adapter = self._adapter()

        self.assertIsNotNone(adapter.adapt(private_payload(message_id=1)))
        self.assertIsNotNone(adapter.adapt(private_payload(message_id=2)))

    def test_anonymous_event_without_message_id_is_still_tracked(self) -> None:
        adapter = self._adapter()

        def anonymous() -> dict:
            payload = private_payload()
            payload.pop("message_id")
            return payload

        self.assertIsNotNone(adapter.adapt(anonymous()))
        self.assertIsNone(adapter.adapt(anonymous()))
        self.assertEqual(len(adapter.duplicates), 1)


class ReplyActionTest(unittest.TestCase):
    def _adapter(self) -> OneBotAdapter:
        return OneBotAdapter(bot_account=BOT)

    def test_private_reply_targets_the_user(self) -> None:
        adapter = self._adapter()
        event = adapter.adapt(private_payload())

        action = adapter.reply_action(event, "你好呀")

        self.assertEqual(action.action, ACTION_SEND_PRIVATE)
        self.assertEqual(action.params["user_id"], 67890)
        self.assertEqual(action.params["message"], "你好呀")
        self.assertEqual(action.echo, "1001")

    def test_group_reply_targets_the_group(self) -> None:
        adapter = self._adapter()
        event = adapter.adapt(group_payload())

        action = adapter.reply_action(event, "你好呀")

        self.assertEqual(action.action, ACTION_SEND_GROUP)
        self.assertEqual(action.params["group_id"], 555)

    def test_reply_can_quote_the_triggering_message(self) -> None:
        adapter = self._adapter()
        event = adapter.adapt(group_payload())

        action = adapter.reply_action(event, "你好", reply_to_message_id="1002")

        self.assertIsInstance(action.params["message"], list)
        self.assertEqual(action.params["message"][0], {"type": "reply", "data": {"id": "1002"}})
        self.assertEqual(action.params["message"][1]["data"]["text"], "你好")

    def test_non_numeric_identifiers_are_passed_through(self) -> None:
        adapter = self._adapter()
        event = adapter.adapt(private_payload(user_id="user-openid"))

        action = adapter.reply_action(event, "你好")

        self.assertEqual(action.params["user_id"], "user-openid")

    def test_empty_reply_text_is_rejected(self) -> None:
        adapter = self._adapter()
        event = adapter.adapt(private_payload())

        with self.assertRaises(ValueError):
            adapter.reply_action(event, "   ")

    def test_get_message_action_asks_for_one_message(self) -> None:
        action = self._adapter().get_message_action("1000")

        self.assertEqual(action.action, "get_msg")
        self.assertEqual(action.params["message_id"], 1000)

    def test_get_message_action_requires_an_id(self) -> None:
        with self.assertRaises(ValueError):
            self._adapter().get_message_action("")

    def test_login_info_action_confirms_the_account(self) -> None:
        action = self._adapter().login_info_action()

        self.assertEqual(action.action, "get_login_info")
        self.assertEqual(action.params, {})


if __name__ == "__main__":
    unittest.main()
