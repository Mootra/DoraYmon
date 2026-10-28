"""P1a: the account session handles reconnects, duplicates and send outcomes."""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
import unittest

from doraymon.config import Settings
from services.account_session import (
    SKIP_DUPLICATE,
    SKIP_EMPTY_REPLY,
    SKIP_NOT_ADDRESSED,
    SKIP_NOT_ADAPTED,
    SKIP_SELF,
    AccountSession,
)
from services.send_result import SEND_LOG
from tests.test_onebot_adapter import BOT, group_payload, private_payload


class FakeTransport:
    """Records actions; can be told to fail the way a real endpoint would.

    ``responses`` maps an action echo to a canned reply, so a lookup such as
    ``get_msg`` can be exercised without a live endpoint.
    """

    def __init__(self, *, retcode: int = 0, raises: BaseException | None = None) -> None:
        self.actions = []
        self.retcode = retcode
        self.raises = raises
        self.responses: dict[str, dict] = {}

    async def send_action(self, action):
        self.actions.append(action)
        if self.raises is not None:
            raise self.raises
        if action.echo in self.responses:
            return self.responses[action.echo]
        return {"status": "ok" if self.retcode == 0 else "failed", "retcode": self.retcode}


def _settings() -> Settings:
    return Settings(
        deepseek_api_key="sk-test",
        chat_history_enabled=True,
        qqbot_appid="",
    )


class AccountSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        SEND_LOG.clear()

    def tearDown(self) -> None:
        SEND_LOG.clear()

    def _session(self, transport=None, **kwargs) -> AccountSession:
        return AccountSession(
            settings=_settings(),
            transport=transport or FakeTransport(),
            bot_account=BOT,
            **kwargs,
        )

    def _patch_router(self, reply: str):
        from unittest.mock import AsyncMock, patch

        return patch(
            "services.account_session.route_incoming_message",
            new=AsyncMock(return_value=reply),
        )

    def test_private_message_is_routed_and_answered(self) -> None:
        transport = FakeTransport()
        session = self._session(transport)

        with self._patch_router("你好呀"):
            event = asyncio.run(session.handle_payload(private_payload()))

        self.assertIsNotNone(event)
        self.assertEqual(len(transport.actions), 1)
        action = transport.actions[0]
        self.assertEqual(action.action, "send_private_msg")
        self.assertEqual(action.params["user_id"], 67890)
        self.assertEqual(session.stats.replied, 1)
        self.assertEqual(session.stats.segments_sent, 1)

    def test_unaddressed_group_message_is_skipped(self) -> None:
        transport = FakeTransport()
        session = self._session(transport)
        payload = group_payload(
            message=[{"type": "text", "data": {"text": "随便说说"}}],
            raw_message="随便说说",
        )

        with self._patch_router("不该发出"):
            event = asyncio.run(session.handle_payload(payload))

        self.assertIsNone(event)
        self.assertEqual(transport.actions, [])
        self.assertEqual(session.stats.skipped.get(SKIP_NOT_ADDRESSED), 1)

    def test_addressed_group_message_quotes_the_trigger(self) -> None:
        transport = FakeTransport()
        session = self._session(transport)

        with self._patch_router("群回复"):
            asyncio.run(session.handle_payload(group_payload()))

        action = transport.actions[0]
        self.assertEqual(action.action, "send_group_msg")
        self.assertEqual(action.params["group_id"], 555)
        self.assertIsInstance(action.params["message"], list)
        self.assertEqual(action.params["message"][0]["type"], "reply")

    def test_group_reply_mode_text_does_not_quote(self) -> None:
        transport = FakeTransport()
        session = self._session(transport, group_reply_mode="text")

        with self._patch_router("群回复"):
            asyncio.run(session.handle_payload(group_payload()))

        self.assertEqual(transport.actions[0].params["message"], "群回复")

    def test_duplicate_event_is_answered_once(self) -> None:
        transport = FakeTransport()
        session = self._session(transport)

        with self._patch_router("回复"):
            first = asyncio.run(session.handle_payload(private_payload()))
            second = asyncio.run(session.handle_payload(private_payload()))

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(transport.actions), 1)
        self.assertEqual(session.stats.skipped.get(SKIP_DUPLICATE), 1)

    def test_duplicate_still_suppressed_after_a_reconnect(self) -> None:
        transport = FakeTransport()
        session = self._session(transport)

        with self._patch_router("回复"):
            asyncio.run(session.handle_payload(private_payload()))
            session.on_disconnected()
            session.on_reconnected()
            replayed = asyncio.run(session.handle_payload(private_payload()))

        self.assertIsNone(replayed)
        self.assertEqual(len(transport.actions), 1)

    def test_reconnect_does_not_resend_anything(self) -> None:
        """An interrupted send has an unknown outcome; resending could duplicate."""
        transport = FakeTransport(raises=ConnectionError("link lost"))
        session = self._session(transport)

        with self._patch_router("回复"):
            asyncio.run(session.handle_payload(private_payload()))

        self.assertEqual(session.stats.segments_failed, 1)
        before = len(transport.actions)

        session.on_reconnected()

        self.assertEqual(len(transport.actions), before)
        self.assertEqual(session.stats.segments_failed, 1)

    def test_own_message_is_skipped(self) -> None:
        transport = FakeTransport()
        session = self._session(transport)

        with self._patch_router("不该发出"):
            event = asyncio.run(session.handle_payload(private_payload(user_id=12345)))

        self.assertIsNone(event)
        self.assertEqual(session.stats.skipped.get(SKIP_SELF), 1)
        self.assertEqual(transport.actions, [])

    def test_non_message_event_is_skipped(self) -> None:
        session = self._session()

        event = asyncio.run(session.handle_payload({"post_type": "meta_event"}))

        self.assertIsNone(event)
        self.assertEqual(session.stats.skipped.get(SKIP_NOT_ADAPTED), 1)

    def test_empty_reply_sends_nothing(self) -> None:
        transport = FakeTransport()
        session = self._session(transport)

        with self._patch_router("   "):
            event = asyncio.run(session.handle_payload(private_payload()))

        self.assertIsNone(event)
        self.assertEqual(transport.actions, [])
        self.assertEqual(session.stats.skipped.get(SKIP_EMPTY_REPLY), 1)

    def test_router_failure_does_not_crash_the_session(self) -> None:
        from unittest.mock import AsyncMock, patch

        transport = FakeTransport()
        session = self._session(transport)

        with patch(
            "services.account_session.route_incoming_message",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            event = asyncio.run(session.handle_payload(private_payload()))

        self.assertIsNone(event)
        self.assertEqual(transport.actions, [])

    def test_endpoint_retcode_failure_is_recorded(self) -> None:
        transport = FakeTransport(retcode=100)
        session = self._session(transport)

        with self._patch_router("回复"):
            asyncio.run(session.handle_payload(private_payload()))

        self.assertEqual(session.stats.segments_failed, 1)
        self.assertEqual(session.stats.segments_sent, 0)
        session_key = ("private", f"qqaccount:{BOT}:67890", f"qqaccount:{BOT}:67890")
        failure = SEND_LOG.last_failure(session_key)
        self.assertIsNotNone(failure)

    def test_long_reply_is_split_into_segments(self) -> None:
        transport = FakeTransport()
        session = self._session(transport)

        with self._patch_router("内容" * 1400):
            asyncio.run(session.handle_payload(private_payload()))

        self.assertGreaterEqual(len(transport.actions), 2)
        self.assertEqual(session.stats.segments_sent, len(transport.actions))

    def test_a_failed_first_segment_stops_the_reply_and_does_not_count_as_replied(self) -> None:
        transport = FakeTransport(retcode=100)
        session = self._session(transport)
        with self._patch_router("内容" * 1400):
            asyncio.run(session.handle_payload(private_payload()))
        self.assertEqual(len(transport.actions), 1)
        self.assertEqual(session.stats.segments_failed, 1)
        self.assertEqual(session.stats.segments_sent, 0)
        self.assertEqual(session.stats.replied, 0)

    def test_invalidating_the_conversation_stops_remaining_segments(self) -> None:
        from unittest.mock import patch
        from services.session_revisions import SessionRevisions

        transport = FakeTransport()
        session = self._session(transport)
        original_send = transport.send_action
        revisions = SessionRevisions()

        async def send_and_clear(action):
            result = await original_send(action)
            revisions.bump(("private", f"qqaccount:{BOT}:67890", f"qqaccount:{BOT}:67890"))
            return result

        transport.send_action = send_and_clear
        with self._patch_router("内容" * 1400), patch("services.account_session.SESSION_REVISIONS", revisions):
            asyncio.run(session.handle_payload(private_payload()))
        self.assertEqual(len(transport.actions), 1)
        self.assertEqual(session.stats.segments_sent, 1)
        self.assertEqual(session.stats.replied, 1)

    def test_session_key_namespaces_the_real_qq_number(self) -> None:
        session = self._session()

        with self._patch_router("回复"):
            event = asyncio.run(session.handle_payload(private_payload()))

        self.assertEqual(
            session.session_key_for(event),
            ("private", f"qqaccount:{BOT}:67890", f"qqaccount:{BOT}:67890"),
        )

    def test_group_session_key_includes_group_and_speaker(self) -> None:
        session = self._session()

        with self._patch_router("回复"):
            event = asyncio.run(session.handle_payload(group_payload()))

        self.assertEqual(
            session.session_key_for(event),
            ("group", f"qqaccount:{BOT}:555", f"qqaccount:{BOT}:67890"),
        )

    def test_connection_state_is_tracked(self) -> None:
        session = self._session()

        self.assertFalse(session.connected)
        session.on_connected()
        self.assertTrue(session.connected)
        session.on_disconnected()
        self.assertFalse(session.connected)

    def test_stats_count_received_payloads(self) -> None:
        session = self._session()

        with self._patch_router("回复"):
            asyncio.run(session.handle_payload(private_payload()))

        self.assertEqual(session.stats.received, 1)


if __name__ == "__main__":
    unittest.main()
