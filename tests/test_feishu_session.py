"""The shared session drives a Feishu adapter without learning Feishu.

The point of these tests is the seam, not the Feishu API: the session must
deliver a reply, suppress a redelivery, stay quiet when it was not addressed,
and record a refusal — all without containing a single Feishu-specific branch.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from doraymon.config import Settings
from doraymon.identity import PLATFORM_FEISHU, PLATFORM_QQ_ACCOUNT
from services.account_session import (
    SKIP_DUPLICATE,
    SKIP_EMPTY_REPLY,
    SKIP_NOT_ADDRESSED,
    SKIP_QUOTE_UNRESOLVED,
    SKIP_SELF,
    SKIP_THROTTLED,
    AccountSession,
)
from services.account_throttle import ThrottlePolicy
from services.feishu_adapter import (
    ACTION_CREATE_MESSAGE,
    ACTION_GET_MESSAGE,
    ACTION_REPLY_MESSAGE,
    FeishuAdapter,
)
from services.onebot_adapter import OneBotAdapter
from services.send_result import SEND_LOG
from tests.test_feishu_adapter import BOT, USER, group_payload, private_payload


class FakeTransport:
    """Records Feishu actions; can be told to refuse the way the API does."""

    def __init__(self, *, code: int = 0, raises: BaseException | None = None) -> None:
        self.actions = []
        self.code = code
        self.raises = raises
        self.responses: dict[str, dict] = {}

    async def send_action(self, action):
        self.actions.append(action)
        if self.raises is not None:
            raise self.raises
        if action.echo in self.responses:
            return self.responses[action.echo]
        return {"code": self.code, "msg": "success" if self.code == 0 else "refused"}


def _settings() -> Settings:
    return Settings(
        deepseek_api_key="sk-test",
        chat_history_enabled=False,
        qqbot_appid="",
    )


def _one_reply_throttle() -> ThrottlePolicy:
    """A group limit that allows exactly one delivered reply per minute."""
    return ThrottlePolicy(cooldown_seconds=60.0, window_seconds=60.0, max_replies=1)


def _group_payload_for(chat_id: str, message_id: str) -> dict:
    """A group message in its own chat.

    The throttle is process-wide and keyed by conversation, so two tests sharing
    a chat id would share a window and depend on execution order.
    """
    payload = group_payload()
    payload["event"]["message"]["chat_id"] = chat_id
    payload["event"]["message"]["message_id"] = message_id
    return payload


class FeishuSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        SEND_LOG.clear()

    def tearDown(self) -> None:
        SEND_LOG.clear()

    def _session(self, transport=None, **kwargs) -> AccountSession:
        return AccountSession(
            settings=_settings(),
            transport=transport or FakeTransport(),
            bot_account=BOT,
            platform=PLATFORM_FEISHU,
            adapter=FeishuAdapter(bot_account=BOT),
            **kwargs,
        )

    def _patch_router(self, reply: str):
        return patch(
            "services.account_session.route_incoming_message",
            new=AsyncMock(return_value=reply),
        )

    def test_private_message_is_answered_through_the_open_id(self) -> None:
        transport = FakeTransport()
        session = self._session(transport)
        with self._patch_router("你好呀"):
            asyncio.run(session.handle_payload(private_payload()))

        self.assertEqual(len(transport.actions), 1)
        action = transport.actions[0]
        self.assertEqual(action.name, ACTION_CREATE_MESSAGE)
        self.assertEqual(action.params["receive_id_type"], "open_id")
        self.assertEqual(action.params["receive_id"], USER)
        self.assertEqual(json.loads(action.params["content"]), {"text": "你好呀"})
        self.assertEqual(session.stats.replied, 1)

    def test_group_message_is_answered_in_the_chat(self) -> None:
        transport = FakeTransport()
        session = self._session(transport)
        with self._patch_router("群回复"):
            asyncio.run(session.handle_payload(group_payload()))

        self.assertEqual(len(transport.actions), 1)
        action = transport.actions[0]
        self.assertEqual(action.name, ACTION_REPLY_MESSAGE)
        self.assertEqual(action.params["message_id"], "om_1")

    def test_group_message_without_a_mention_stays_silent(self) -> None:
        transport = FakeTransport()
        session = self._session(transport)
        payload = group_payload(mentions=False)
        payload["event"]["message"]["content"] = json.dumps(
            {"text": "大家好啊"}, ensure_ascii=False
        )
        with self._patch_router("不该发"):
            asyncio.run(session.handle_payload(payload))

        self.assertEqual(transport.actions, [])
        self.assertEqual(session.stats.skipped.get(SKIP_NOT_ADDRESSED), 1)

    def test_a_redelivered_message_is_not_answered_twice(self) -> None:
        transport = FakeTransport()
        session = self._session(transport)
        with self._patch_router("你好呀"):
            asyncio.run(session.handle_payload(private_payload(event_id="ev_1")))
            asyncio.run(session.handle_payload(private_payload(event_id="ev_2")))

        self.assertEqual(len(transport.actions), 1)
        self.assertEqual(session.stats.skipped.get(SKIP_DUPLICATE), 1)

    def test_a_message_from_the_app_itself_is_ignored(self) -> None:
        transport = FakeTransport()
        session = self._session(transport)
        with self._patch_router("不该发"):
            asyncio.run(session.handle_payload(private_payload(open_id=BOT)))

        self.assertEqual(transport.actions, [])
        self.assertEqual(session.stats.skipped.get(SKIP_SELF), 1)

    def test_an_empty_reply_is_not_sent(self) -> None:
        transport = FakeTransport()
        session = self._session(transport)
        with self._patch_router("   "):
            asyncio.run(session.handle_payload(private_payload()))

        self.assertEqual(transport.actions, [])
        self.assertEqual(session.stats.skipped.get(SKIP_EMPTY_REPLY), 1)

    def test_a_refused_send_is_recorded_as_a_failure(self) -> None:
        transport = FakeTransport(code=230020)
        session = self._session(transport)
        with self._patch_router("你好呀"):
            asyncio.run(session.handle_payload(private_payload()))

        self.assertEqual(session.stats.segments_sent, 0)
        self.assertEqual(session.stats.segments_failed, 1)
        session_key = ("private", f"feishu:{BOT}:{USER}", f"feishu:{BOT}:{USER}")
        self.assertIsNotNone(SEND_LOG.last_failure(session_key))

    def test_a_transport_error_is_recorded_without_crashing(self) -> None:
        transport = FakeTransport(raises=ConnectionError("link lost"))
        session = self._session(transport)
        with self._patch_router("你好呀"):
            asyncio.run(session.handle_payload(private_payload()))

        self.assertEqual(session.stats.segments_failed, 1)
        self.assertEqual(session.stats.segments_sent, 0)

    def test_a_failed_send_does_not_count_against_the_throttle(self) -> None:
        transport = FakeTransport(code=230020)
        session = self._session(transport, group_throttle=_one_reply_throttle())
        chat = "oc_undelivered"
        with self._patch_router("群回复"):
            asyncio.run(session.handle_payload(_group_payload_for(chat, "om_a")))
            before = len(transport.actions)
            asyncio.run(session.handle_payload(_group_payload_for(chat, "om_b")))
        # Nothing was delivered, so the next attempt is still allowed through to
        # the send step rather than being silently throttled.
        self.assertGreater(len(transport.actions), before)

    def test_a_delivered_reply_does_count_against_the_throttle(self) -> None:
        transport = FakeTransport()
        session = self._session(transport, group_throttle=_one_reply_throttle())
        chat = "oc_delivered"
        with self._patch_router("群回复"):
            asyncio.run(session.handle_payload(_group_payload_for(chat, "om_a")))
            asyncio.run(session.handle_payload(_group_payload_for(chat, "om_b")))

        self.assertEqual(len(transport.actions), 1)
        self.assertEqual(session.stats.skipped.get(SKIP_THROTTLED), 1)

    def test_a_quoted_bot_message_addresses_the_bot_in_a_group(self) -> None:
        transport = FakeTransport()
        session = self._session(transport)
        payload = group_payload(mentions=False)
        payload["event"]["message"]["content"] = json.dumps(
            {"text": "再来一个"}, ensure_ascii=False
        )
        payload["event"]["message"]["parent_id"] = "om_bot"
        transport.responses["get_message:om_bot"] = {
            "code": 0,
            "data": {"items": [{"message_id": "om_bot", "sender": {"id": BOT}}]},
        }
        with self._patch_router("好的"):
            asyncio.run(session.handle_payload(payload))

        names = [action.name for action in transport.actions]
        self.assertEqual(names[0], ACTION_GET_MESSAGE)
        self.assertEqual([n for n in names if n != ACTION_GET_MESSAGE], [ACTION_REPLY_MESSAGE])
        self.assertEqual(session.stats.replied, 1)

    def test_an_unresolvable_quote_leaves_the_message_unaddressed(self) -> None:
        transport = FakeTransport()
        session = self._session(transport)
        payload = group_payload(mentions=False)
        payload["event"]["message"]["content"] = json.dumps(
            {"text": "再来一个"}, ensure_ascii=False
        )
        payload["event"]["message"]["parent_id"] = "om_unknown"
        with self._patch_router("不该发"):
            asyncio.run(session.handle_payload(payload))

        self.assertEqual([a.name for a in transport.actions], [ACTION_GET_MESSAGE])
        self.assertEqual(session.stats.skipped.get(SKIP_QUOTE_UNRESOLVED), 1)

    def test_the_log_label_names_feishu(self) -> None:
        self.assertEqual(self._session().channel_label, "飞书")

    def test_the_default_label_still_names_the_qq_account(self) -> None:
        session = AccountSession(
            settings=_settings(),
            transport=FakeTransport(),
            bot_account="12345",
        )
        self.assertEqual(session.platform, PLATFORM_QQ_ACCOUNT)
        self.assertEqual(session.channel_label, "QQ 账号")
        self.assertIsInstance(session.adapter, OneBotAdapter)

    def test_reconnect_keeps_the_duplicate_window(self) -> None:
        transport = FakeTransport()
        session = self._session(transport)
        with self._patch_router("你好呀"):
            asyncio.run(session.handle_payload(private_payload(event_id="ev_1")))
        session.on_connected()
        session.on_reconnected()
        with self._patch_router("你好呀"):
            asyncio.run(session.handle_payload(private_payload(event_id="ev_2")))

        self.assertEqual(len(transport.actions), 1)
        self.assertEqual(session.stats.skipped.get(SKIP_DUPLICATE), 1)


if __name__ == "__main__":
    unittest.main()
