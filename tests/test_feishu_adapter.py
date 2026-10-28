"""P1a: Feishu ``im.message.receive_v1`` payloads convert into the contract."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone

from doraymon.identity import PLATFORM_FEISHU, PLATFORM_QQ_ACCOUNT, namespace_for
from services.feishu_adapter import (
    ACTION_BOT_INFO,
    ACTION_CREATE_MESSAGE,
    ACTION_GET_MESSAGE,
    ACTION_REPLY_MESSAGE,
    RECEIVE_ID_CHAT,
    RECEIVE_ID_OPEN,
    FeishuActionError,
    FeishuAdapter,
    from_feishu_event,
)
from services.message_contract import AttachmentKind, ConversationKind, QuotedMessage

BOT = "ou_bot"
USER = "ou_alice"
OTHER = "ou_bob"

BOT_MENTION = {"key": "@_user_1", "id": {"open_id": BOT}, "name": "哆啦Y梦"}

# Feishu reports create_time as a millisecond epoch string.
CREATED_MS = "1760000000000"


def _message(**overrides) -> dict:
    message = {
        "message_id": "om_1",
        "chat_id": "oc_private",
        "chat_type": "p2p",
        "message_type": "text",
        "create_time": CREATED_MS,
        "content": json.dumps({"text": "你好"}, ensure_ascii=False),
    }
    message.update(overrides)
    return message


def _payload(
    message: dict,
    *,
    open_id: str = USER,
    sender_type: str = "user",
    event_id: str = "ev_1",
    event_type: str = "im.message.receive_v1",
) -> dict:
    return {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "event_type": event_type,
            "create_time": CREATED_MS,
            "app_id": "cli_example",
        },
        "event": {
            "sender": {
                "sender_id": {
                    "open_id": open_id,
                    "union_id": "on_example",
                    "user_id": "u_example",
                },
                "sender_type": sender_type,
            },
            "message": message,
        },
    }


def private_payload(*, message: dict | None = None, **overrides) -> dict:
    return _payload(message if message is not None else _message(), **overrides)


def group_payload(
    *,
    text: str = "@_user_1 你好",
    mentions=None,
    include_chat_id: bool = True,
    **overrides,
) -> dict:
    message = _message(
        chat_id="oc_group" if include_chat_id else "",
        chat_type="group",
        content=json.dumps({"text": text}, ensure_ascii=False),
    )
    if mentions is not False:
        message["mentions"] = [BOT_MENTION] if mentions is None else mentions
    return _payload(message, **overrides)


class ConversionTest(unittest.TestCase):
    def test_private_message_converts(self) -> None:
        event = from_feishu_event(private_payload(), bot_account=BOT)
        assert event is not None
        self.assertEqual(event.platform, PLATFORM_FEISHU)
        self.assertEqual(event.bot_account, BOT)
        self.assertEqual(event.kind, ConversationKind.PRIVATE)
        self.assertEqual(event.message_id, "om_1")
        self.assertEqual(event.raw_user_id, USER)
        self.assertEqual(event.raw_group_id, "")
        self.assertEqual(event.text, "你好")
        self.assertFalse(event.is_group)

    def test_group_message_converts_with_a_mention(self) -> None:
        event = from_feishu_event(group_payload(), bot_account=BOT)
        assert event is not None
        self.assertEqual(event.kind, ConversationKind.GROUP)
        self.assertEqual(event.raw_group_id, "oc_group")
        self.assertTrue(event.mentions_bot)
        self.assertTrue(event.should_consider_for_reply())

    def test_group_message_without_a_mention_is_not_addressed(self) -> None:
        payload = group_payload(mentions=False)
        payload["event"]["message"]["content"] = json.dumps(
            {"text": "大家好啊"}, ensure_ascii=False
        )
        event = from_feishu_event(payload, bot_account=BOT)
        assert event is not None
        self.assertFalse(event.mentions_bot)
        self.assertFalse(event.should_consider_for_reply())

    def test_group_message_without_a_chat_id_is_rejected(self) -> None:
        self.assertIsNone(
            from_feishu_event(group_payload(include_chat_id=False), bot_account=BOT)
        )

    def test_unknown_chat_type_is_ignored_rather_than_guessed(self) -> None:
        """Reading a group as private would make every message look addressed."""
        payload = private_payload(message=_message(chat_type=""))
        self.assertIsNone(from_feishu_event(payload, bot_account=BOT))

    def test_non_message_event_is_ignored(self) -> None:
        payload = private_payload(event_type="im.chat.updated_v1")
        self.assertIsNone(from_feishu_event(payload, bot_account=BOT))

    def test_identity_is_required(self) -> None:
        payload = private_payload()
        payload["event"]["sender"]["sender_id"] = {"union_id": "on_example"}
        self.assertIsNone(from_feishu_event(payload, bot_account=BOT))

    def test_message_without_a_message_id_is_rejected(self) -> None:
        self.assertIsNone(
            from_feishu_event(
                private_payload(message=_message(message_id="")), bot_account=BOT
            )
        )

    def test_non_dict_payload_is_ignored(self) -> None:
        for payload in (None, "text", 42, []):
            self.assertIsNone(from_feishu_event(payload, bot_account=BOT))

    def test_content_that_is_not_json_does_not_break_the_event(self) -> None:
        payload = private_payload(message=_message(content="{not json"))
        event = from_feishu_event(payload, bot_account=BOT)
        assert event is not None
        self.assertEqual(event.text, "")

    def test_creation_time_is_read_as_milliseconds(self) -> None:
        event = from_feishu_event(private_payload(), bot_account=BOT)
        assert event is not None
        self.assertEqual(
            event.received_at,
            datetime.fromtimestamp(1760000000, tz=timezone.utc),
        )

    def test_message_time_falls_back_to_the_header_time(self) -> None:
        payload = private_payload(message=_message(create_time="not-a-time"))
        event = from_feishu_event(payload, bot_account=BOT)
        assert event is not None
        self.assertEqual(
            event.received_at,
            datetime.fromtimestamp(1760000000, tz=timezone.utc),
        )

    def test_unparseable_time_falls_back_to_now(self) -> None:
        before = datetime.now(timezone.utc)
        payload = private_payload(message=_message(create_time="not-a-time"))
        payload["header"]["create_time"] = ""
        event = from_feishu_event(payload, bot_account=BOT)
        assert event is not None
        self.assertGreaterEqual(event.received_at, before)

    def test_mention_placeholder_of_the_bot_is_removed(self) -> None:
        """``@_user_1 吃什么`` has to reach the intent matcher as ``吃什么``."""
        event = from_feishu_event(
            group_payload(text="@_user_1 吃什么"), bot_account=BOT
        )
        assert event is not None
        self.assertEqual(event.text, "吃什么")

    def test_trailing_bot_mention_does_not_leave_a_stray_space(self) -> None:
        event = from_feishu_event(group_payload(text="你好 @_user_1"), bot_account=BOT)
        assert event is not None
        self.assertEqual(event.text, "你好")

    def test_mention_of_another_member_keeps_a_readable_name(self) -> None:
        mentions = [{"key": "@_user_2", "id": {"open_id": OTHER}, "name": "张三"}]
        event = from_feishu_event(
            group_payload(text="@_user_2 你好", mentions=mentions), bot_account=BOT
        )
        assert event is not None
        self.assertEqual(event.text, "@张三 你好")
        self.assertFalse(event.mentions_bot)

    def test_mentions_are_recorded_with_their_open_ids(self) -> None:
        mentions = [
            BOT_MENTION,
            {"key": "@_user_2", "id": {"open_id": OTHER}, "name": "张三"},
        ]
        event = from_feishu_event(
            group_payload(text="@_user_1 @_user_2 你好", mentions=mentions),
            bot_account=BOT,
        )
        assert event is not None
        self.assertEqual(event.mentions_raw_ids(), (BOT, OTHER))

    def test_a_mention_without_an_open_id_is_not_matched_to_the_bot(self) -> None:
        """``user_id`` lives in another identifier space and must not be used."""
        mentions = [{"key": "@_user_1", "id": {"user_id": "u_bot"}, "name": "哆啦Y梦"}]
        event = from_feishu_event(
            group_payload(text="@_user_1 你好", mentions=mentions), bot_account=BOT
        )
        assert event is not None
        self.assertEqual(event.mentions, ())
        self.assertFalse(event.mentions_bot)

    def test_post_message_text_is_flattened(self) -> None:
        body = {
            "title": "标题",
            "content": [
                [
                    {"tag": "text", "text": "第一行"},
                    {"tag": "a", "text": "链接", "href": "https://example.com"},
                ]
            ],
        }
        payload = private_payload(
            message=_message(
                message_type="post", content=json.dumps(body, ensure_ascii=False)
            )
        )
        event = from_feishu_event(payload, bot_account=BOT)
        assert event is not None
        self.assertEqual(event.text, "标题\n第一行\n链接")

    def test_locale_wrapped_post_text_is_flattened(self) -> None:
        body = {"zh_cn": {"title": "标题", "content": [[{"tag": "text", "text": "正文"}]]}}
        payload = private_payload(
            message=_message(
                message_type="post", content=json.dumps(body, ensure_ascii=False)
            )
        )
        event = from_feishu_event(payload, bot_account=BOT)
        assert event is not None
        self.assertEqual(event.text, "标题\n正文")

    def test_image_message_has_an_attachment_and_no_text(self) -> None:
        payload = private_payload(
            message=_message(
                message_type="image",
                content=json.dumps({"image_key": "img_v2_example"}),
            )
        )
        event = from_feishu_event(payload, bot_account=BOT)
        assert event is not None
        self.assertEqual(event.text, "")
        self.assertEqual(event.attachment_kinds(), (AttachmentKind.IMAGE,))
        self.assertEqual(event.attachments[0].reference, "img_v2_example")

    def test_file_message_keeps_the_filename(self) -> None:
        payload = private_payload(
            message=_message(
                message_type="file",
                content=json.dumps(
                    {"file_key": "file_v2_example", "file_name": "报告.pdf"},
                    ensure_ascii=False,
                ),
            )
        )
        event = from_feishu_event(payload, bot_account=BOT)
        assert event is not None
        self.assertEqual(event.attachment_kinds(), (AttachmentKind.FILE,))
        self.assertEqual(event.attachments[0].filename, "报告.pdf")

    def test_parent_id_becomes_a_quote_without_an_author(self) -> None:
        payload = group_payload()
        payload["event"]["message"]["parent_id"] = "om_quoted"
        event = from_feishu_event(payload, bot_account=BOT)
        assert event is not None
        assert event.quote is not None
        self.assertEqual(event.quote.message_id, "om_quoted")
        self.assertEqual(event.quote.raw_user_id, "")

    def test_root_id_is_used_when_there_is_no_parent_id(self) -> None:
        payload = private_payload()
        payload["event"]["message"]["root_id"] = "om_root"
        event = from_feishu_event(payload, bot_account=BOT)
        assert event is not None
        assert event.quote is not None
        self.assertEqual(event.quote.message_id, "om_root")

    def test_private_message_never_needs_a_quote_lookup(self) -> None:
        payload = private_payload()
        payload["event"]["message"]["parent_id"] = "om_quoted"
        event = from_feishu_event(payload, bot_account=BOT)
        assert event is not None
        self.assertFalse(FeishuAdapter(bot_account=BOT).needs_quote_lookup(event))

    def test_group_quote_needs_a_lookup(self) -> None:
        payload = group_payload()
        payload["event"]["message"]["parent_id"] = "om_quoted"
        event = from_feishu_event(payload, bot_account=BOT)
        assert event is not None
        self.assertTrue(FeishuAdapter(bot_account=BOT).needs_quote_lookup(event))

    def test_a_resolved_quote_addresses_the_bot(self) -> None:
        payload = group_payload()
        payload["event"]["message"]["parent_id"] = "om_quoted"
        event = from_feishu_event(payload, bot_account=BOT)
        assert event is not None
        resolved = replace(
            event,
            quote=QuotedMessage(message_id="om_quoted", raw_user_id=BOT),
        )
        self.assertTrue(resolved.replies_to_bot())
        self.assertTrue(resolved.should_consider_for_reply())

    def test_identity_is_namespaced_to_feishu(self) -> None:
        event = from_feishu_event(private_payload(), bot_account=BOT)
        assert event is not None
        self.assertEqual(
            event.session_key,
            ("private", f"feishu:{BOT}:{USER}", f"feishu:{BOT}:{USER}"),
        )

    def test_the_same_string_id_on_two_platforms_never_shares_a_session(self) -> None:
        shared = "10001"
        feishu = namespace_for(PLATFORM_FEISHU, BOT).session_key(shared)
        account = namespace_for(PLATFORM_QQ_ACCOUNT, "12345").session_key(shared)
        self.assertNotEqual(feishu, account)


class AdapterTest(unittest.TestCase):
    def test_duplicate_event_is_not_adapted_twice(self) -> None:
        adapter = FeishuAdapter(bot_account=BOT)
        self.assertIsNotNone(adapter.adapt(private_payload(event_id="ev_1")))
        # A redelivery is a new event carrying the same message.
        self.assertIsNone(adapter.adapt(private_payload(event_id="ev_2")))

    def test_distinct_messages_are_both_adapted(self) -> None:
        adapter = FeishuAdapter(bot_account=BOT)
        self.assertIsNotNone(adapter.adapt(private_payload()))
        self.assertIsNotNone(
            adapter.adapt(private_payload(message=_message(message_id="om_2")))
        )

    def test_own_message_is_ignored(self) -> None:
        adapter = FeishuAdapter(bot_account=BOT)
        self.assertIsNone(adapter.adapt(private_payload(open_id=BOT)))

    def test_another_app_message_is_ignored(self) -> None:
        """Two bots answering each other is a loop, not a conversation."""
        adapter = FeishuAdapter(bot_account=BOT)
        self.assertIsNone(adapter.adapt(private_payload(sender_type="app")))

    def test_own_message_is_ignored_before_the_duplicate_check(self) -> None:
        adapter = FeishuAdapter(bot_account=BOT)
        self.assertIsNone(adapter.adapt(private_payload(open_id=BOT)))
        self.assertEqual(len(adapter.duplicates), 0)

    def test_malformed_payload_is_ignored_without_recording(self) -> None:
        adapter = FeishuAdapter(bot_account=BOT)
        self.assertIsNone(adapter.adapt({"header": {"event_type": "im.message.receive_v1"}}))
        self.assertEqual(len(adapter.duplicates), 0)

    def test_non_message_event_has_no_event_id(self) -> None:
        adapter = FeishuAdapter(bot_account=BOT)
        self.assertEqual(adapter.event_id_of(private_payload(event_type="im.chat.updated_v1")), "")

    def test_event_id_prefers_the_message_id(self) -> None:
        adapter = FeishuAdapter(bot_account=BOT)
        self.assertEqual(adapter.event_id_of(private_payload(event_id="ev_9")), "om_1")

    def test_event_id_falls_back_to_the_header_event_id(self) -> None:
        adapter = FeishuAdapter(bot_account=BOT)
        payload = private_payload(message=_message(message_id=""), event_id="ev_9")
        self.assertEqual(adapter.event_id_of(payload), "ev_9")

    def test_bot_account_may_be_unknown_without_answering_its_own_messages(self) -> None:
        adapter = FeishuAdapter(bot_account="")
        self.assertIsNone(adapter.adapt(private_payload(sender_type="app")))
        self.assertIsNotNone(adapter.adapt(private_payload()))


class QuoteAuthorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = FeishuAdapter(bot_account=BOT)

    def test_author_is_read_from_the_message_response(self) -> None:
        response = {
            "code": 0,
            "msg": "success",
            "data": {
                "items": [
                    {
                        "message_id": "om_quoted",
                        "sender": {"id": BOT, "id_type": "open_id", "sender_type": "app"},
                    }
                ]
            },
        }
        self.assertEqual(self.adapter.quote_author_from_response(response), BOT)

    def test_a_nested_sender_id_is_also_accepted(self) -> None:
        response = {
            "code": 0,
            "data": {"items": [{"sender": {"sender_id": {"open_id": OTHER}}}]},
        }
        self.assertEqual(self.adapter.quote_author_from_response(response), OTHER)

    def test_refused_lookup_yields_no_author(self) -> None:
        self.assertEqual(
            self.adapter.quote_author_from_response({"code": 230002, "msg": "no permission"}),
            "",
        )

    def test_empty_or_odd_responses_yield_no_author(self) -> None:
        for response in (None, "x", {}, {"code": 0}, {"code": 0, "data": {"items": []}}):
            self.assertEqual(self.adapter.quote_author_from_response(response), "")


class SendErrorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = FeishuAdapter(bot_account=BOT)

    def test_successful_response_is_not_an_error(self) -> None:
        self.assertIsNone(self.adapter.send_error_from_response({"code": 0, "msg": "success"}))

    def test_http_200_with_an_error_code_is_still_an_error(self) -> None:
        """Feishu answers 200 with a non-zero code, so the body decides."""
        error = self.adapter.send_error_from_response({"code": 230020, "msg": "refused"})
        assert error is not None
        self.assertIsInstance(error, FeishuActionError)
        self.assertEqual(error.code, 230020)
        self.assertIn("refused", str(error.action_message))

    def test_odd_responses_are_not_errors(self) -> None:
        for response in (None, "x", {}, {"msg": "no code"}):
            self.assertIsNone(self.adapter.send_error_from_response(response))


class ReplyActionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = FeishuAdapter(bot_account=BOT)

    def _private_event(self):
        event = from_feishu_event(private_payload(), bot_account=BOT)
        assert event is not None
        return event

    def _group_event(self):
        event = from_feishu_event(group_payload(), bot_account=BOT)
        assert event is not None
        return event

    def test_private_reply_targets_the_open_id(self) -> None:
        action = self.adapter.reply_action(self._private_event(), "你好呀")
        self.assertEqual(action.name, ACTION_CREATE_MESSAGE)
        self.assertEqual(action.params["receive_id_type"], RECEIVE_ID_OPEN)
        self.assertEqual(action.params["receive_id"], USER)

    def test_group_reply_targets_the_chat_id(self) -> None:
        action = self.adapter.reply_action(self._group_event(), "群回复")
        self.assertEqual(action.params["receive_id_type"], RECEIVE_ID_CHAT)
        self.assertEqual(action.params["receive_id"], "oc_group")

    def test_reply_can_quote_the_triggering_message(self) -> None:
        action = self.adapter.reply_action(
            self._group_event(), "回复", reply_to_message_id="om_1"
        )
        self.assertEqual(action.name, ACTION_REPLY_MESSAGE)
        self.assertEqual(action.params["message_id"], "om_1")
        self.assertNotIn("receive_id", action.params)

    def test_text_content_is_json_and_keeps_chinese_readable(self) -> None:
        action = self.adapter.reply_action(self._private_event(), "你好")
        self.assertEqual(json.loads(action.params["content"]), {"text": "你好"})
        self.assertIn("你好", action.params["content"])

    def test_empty_reply_text_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.adapter.reply_action(self._private_event(), "   ")

    def test_echo_defaults_to_the_triggering_message(self) -> None:
        action = self.adapter.reply_action(self._private_event(), "你好")
        self.assertEqual(action.echo, "om_1")

    def test_get_message_action_asks_for_one_message(self) -> None:
        action = self.adapter.get_message_action("om_quoted")
        self.assertEqual(action.name, ACTION_GET_MESSAGE)
        self.assertEqual(action.params["message_id"], "om_quoted")

    def test_get_message_action_requires_an_id(self) -> None:
        with self.assertRaises(ValueError):
            self.adapter.get_message_action("  ")

    def test_bot_info_action_asks_for_the_app_identity(self) -> None:
        action = self.adapter.bot_info_action()
        self.assertEqual(action.name, ACTION_BOT_INFO)
        self.assertEqual(action.params, {})


if __name__ == "__main__":
    unittest.main()
