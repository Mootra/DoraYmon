"""P1a: a group reply that quotes the bot must actually reach the bot."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest

from services.account_session import (
    SKIP_NOT_ADDRESSED,
    SKIP_QUOTE_UNRESOLVED,
    AccountSession,
)
from services.send_result import SEND_LOG
from services.account_throttle import ACCOUNT_THROTTLE
from tests.test_account_session import FakeTransport, _settings
from tests.test_onebot_adapter import BOT, group_payload


def _quoting_payload(*, message_id: int, quoted_id: str, with_author: bool) -> dict:
    """A group message whose reply segment may or may not carry the author."""
    reply_data: dict = {"id": quoted_id}
    if with_author:
        reply_data["user_id"] = BOT
    return group_payload(
        message_id=message_id,
        message=[
            {"type": "reply", "data": reply_data},
            {"type": "text", "data": {"text": "接着说"}},
        ],
        raw_message="[CQ:reply,id=%s]接着说" % quoted_id,
    )


class QuoteResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        SEND_LOG.clear()
        ACCOUNT_THROTTLE.clear()

    def tearDown(self) -> None:
        SEND_LOG.clear()
        ACCOUNT_THROTTLE.clear()

    def _session(self, transport) -> AccountSession:
        return AccountSession(
            settings=_settings(),
            transport=transport,
            bot_account=BOT,
            throttle=ACCOUNT_THROTTLE,
        )

    def _patch_router(self, reply: str = "回复"):
        from unittest.mock import AsyncMock, patch

        return patch(
            "services.account_session.route_incoming_message",
            new=AsyncMock(return_value=reply),
        )

    def test_quote_with_author_needs_no_lookup(self) -> None:
        transport = FakeTransport()
        session = self._session(transport)

        with self._patch_router():
            event = asyncio.run(
                session.handle_payload(
                    _quoting_payload(message_id=2, quoted_id="1000", with_author=True)
                )
            )

        self.assertIsNotNone(event)
        self.assertEqual(transport.actions[0].action, "send_group_msg")

    def test_quote_of_the_bot_is_resolved_and_answered(self) -> None:
        """The reported bug: this used to be dropped silently."""
        transport = FakeTransport()
        transport.responses["get_msg:1000"] = {
            "status": "ok",
            "retcode": 0,
            "data": {"message_id": 1000, "sender": {"user_id": int(BOT)}},
        }
        session = self._session(transport)

        with self._patch_router():
            event = asyncio.run(
                session.handle_payload(
                    _quoting_payload(message_id=2, quoted_id="1000", with_author=False)
                )
            )

        self.assertIsNotNone(event)
        self.assertTrue(event.should_consider_for_reply())
        sent_actions = [action.action for action in transport.actions]
        self.assertIn("get_msg", sent_actions)
        self.assertIn("send_group_msg", sent_actions)

    def test_quote_of_another_member_is_not_answered(self) -> None:
        transport = FakeTransport()
        transport.responses["get_msg:1000"] = {
            "status": "ok",
            "retcode": 0,
            "data": {"sender": {"user_id": 99999}},
        }
        session = self._session(transport)

        with self._patch_router():
            event = asyncio.run(
                session.handle_payload(
                    _quoting_payload(message_id=2, quoted_id="1000", with_author=False)
                )
            )

        self.assertIsNone(event)
        self.assertEqual(session.stats.skipped.get(SKIP_NOT_ADDRESSED), 1)
        self.assertNotIn("send_group_msg", [action.action for action in transport.actions])

    def test_failed_lookup_leaves_the_message_unanswered(self) -> None:
        """Guessing that the bot was quoted would be worse than staying quiet."""
        transport = FakeTransport(retcode=1404)
        session = self._session(transport)

        with self._patch_router():
            event = asyncio.run(
                session.handle_payload(
                    _quoting_payload(message_id=2, quoted_id="1000", with_author=False)
                )
            )

        self.assertIsNone(event)
        self.assertEqual(session.stats.skipped.get(SKIP_QUOTE_UNRESOLVED), 1)
        self.assertEqual(session.stats.skipped.get(SKIP_NOT_ADDRESSED), 1)

    def test_transport_failure_during_lookup_is_handled(self) -> None:
        transport = FakeTransport(raises=ConnectionError("link lost"))
        session = self._session(transport)

        with self._patch_router():
            event = asyncio.run(
                session.handle_payload(
                    _quoting_payload(message_id=2, quoted_id="1000", with_author=False)
                )
            )

        self.assertIsNone(event)
        self.assertEqual(session.stats.skipped.get(SKIP_QUOTE_UNRESOLVED), 1)

    def test_lookup_result_is_cached(self) -> None:
        transport = FakeTransport()
        transport.responses["get_msg:1000"] = {
            "status": "ok",
            "retcode": 0,
            "data": {"sender": {"user_id": int(BOT)}},
        }
        session = self._session(transport)

        with self._patch_router():
            asyncio.run(
                session.handle_payload(
                    _quoting_payload(message_id=2, quoted_id="1000", with_author=False)
                )
            )
            asyncio.run(
                session.handle_payload(
                    _quoting_payload(message_id=3, quoted_id="1000", with_author=False)
                )
            )

        lookups = [action for action in transport.actions if action.action == "get_msg"]
        self.assertEqual(len(lookups), 1)

    def test_quote_cache_stays_bounded(self) -> None:
        transport = FakeTransport()
        session = self._session(transport)
        for quoted_id in range(300):
            transport.responses[f"get_msg:{quoted_id}"] = {
                "status": "ok",
                "retcode": 0,
                "data": {"sender": {"user_id": int(BOT)}},
            }

        with self._patch_router():
            for index in range(300):
                asyncio.run(
                    session.handle_payload(
                        _quoting_payload(
                            message_id=1000 + index, quoted_id=str(index), with_author=False
                        )
                    )
                )

        self.assertLessEqual(len(session._quote_authors), 256)

    def test_private_message_never_triggers_a_lookup(self) -> None:
        from tests.test_onebot_adapter import private_payload

        transport = FakeTransport()
        session = self._session(transport)

        with self._patch_router():
            event = asyncio.run(session.handle_payload(private_payload()))

        self.assertIsNotNone(event)
        self.assertEqual([action.action for action in transport.actions], ["send_private_msg"])

    def test_author_from_flat_data_is_also_accepted(self) -> None:
        transport = FakeTransport()
        transport.responses["get_msg:1000"] = {
            "status": "ok",
            "retcode": 0,
            "data": {"user_id": int(BOT)},
        }
        session = self._session(transport)

        with self._patch_router():
            event = asyncio.run(
                session.handle_payload(
                    _quoting_payload(message_id=2, quoted_id="1000", with_author=False)
                )
            )

        self.assertIsNotNone(event)

    def test_malformed_lookup_data_is_not_guessed(self) -> None:
        transport = FakeTransport()
        transport.responses["get_msg:1000"] = {"status": "ok", "retcode": 0, "data": "weird"}
        session = self._session(transport)

        with self._patch_router():
            event = asyncio.run(
                session.handle_payload(
                    _quoting_payload(message_id=2, quoted_id="1000", with_author=False)
                )
            )

        self.assertIsNone(event)
        self.assertEqual(session.stats.skipped.get(SKIP_QUOTE_UNRESOLVED), 1)


if __name__ == "__main__":
    unittest.main()
