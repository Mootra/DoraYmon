"""P0: delivery outcomes are recorded honestly, including failures."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from doraymon.client import MyClient
from doraymon.config import Settings
from services.reply_budget import REPLY_BUDGET
from services.send_result import (
    ERROR_CONNECTION,
    ERROR_HTTP_FORMAT,
    ERROR_TIMEOUT,
    ERROR_UNKNOWN,
    SEND_LOG,
    SendLog,
    SendResult,
    classify_send_error,
)


class HttpFailure(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"http {status}")
        self.status = status


class SendResultContractTest(unittest.TestCase):
    def setUp(self) -> None:
        SEND_LOG.clear()
        REPLY_BUDGET.clear()

    def tearDown(self) -> None:
        SEND_LOG.clear()
        REPLY_BUDGET.clear()

    def test_classify_error_labels_are_short_and_safe(self) -> None:
        self.assertEqual(classify_send_error(HttpFailure(500)), ERROR_HTTP_FORMAT.format(status=500))
        self.assertEqual(classify_send_error(TimeoutError("slow")), ERROR_TIMEOUT)
        self.assertEqual(classify_send_error(ConnectionError("reset")), ERROR_CONNECTION)
        self.assertEqual(classify_send_error(ValueError("boom")), ERROR_UNKNOWN)

    def test_classify_error_never_leaks_the_raw_message(self) -> None:
        secret = "appid=123 secret=abcdef token=xyz"

        label = classify_send_error(ValueError(secret))

        self.assertEqual(label, ERROR_UNKNOWN)
        self.assertNotIn("abcdef", label)

    def test_results_are_bounded_per_session(self) -> None:
        log = SendLog()
        session = ("private", "qqbot::user-a", "qqbot::user-a")

        for index in range(30):
            log.record(
                SendResult(
                    scope_type="private",
                    session_key=session,
                    segment_index=index,
                    success=True,
                    recorded_at=datetime.now(),
                )
            )

        self.assertLessEqual(len(log.results_for(session)), 20)

    def test_results_are_isolated_per_session(self) -> None:
        log = SendLog()
        first = ("private", "qqbot::user-a", "qqbot::user-a")
        second = ("private", "qqbot::user-b", "qqbot::user-b")

        log.record(
            SendResult(
                scope_type="private",
                session_key=first,
                segment_index=1,
                success=False,
                recorded_at=datetime.now(),
                error_label=ERROR_TIMEOUT,
            )
        )

        self.assertEqual(log.counts_for(second), (0, 0))
        self.assertIsNone(log.last_failure(second))

    def test_counts_split_delivered_and_failed_segments(self) -> None:
        log = SendLog()
        session = ("group", "qqbot::group-a", "qqbot::user-a")
        for success in (True, True, False):
            log.record(
                SendResult(
                    scope_type="group",
                    session_key=session,
                    segment_index=1,
                    success=success,
                    recorded_at=datetime.now(),
                    error_label="" if success else ERROR_UNKNOWN,
                )
            )

        self.assertEqual(log.counts_for(session), (2, 1))

    def test_results_without_a_session_are_not_stored(self) -> None:
        log = SendLog()

        log.record(
            SendResult(
                scope_type="private",
                session_key=None,
                segment_index=1,
                success=True,
                recorded_at=datetime.now(),
            )
        )

        self.assertEqual(log.clear(), None)
        self.assertEqual(log.results_for(("private", "x", "x")), ())


class ClientSendRecordingTest(unittest.TestCase):
    def setUp(self) -> None:
        SEND_LOG.clear()
        REPLY_BUDGET.clear()

    def tearDown(self) -> None:
        SEND_LOG.clear()
        REPLY_BUDGET.clear()

    def _client(self) -> MyClient:
        client = object.__new__(MyClient)
        # botpy exposes ``robot`` as a read-only property, so the bot account
        # falls back to the configured appid, exactly as it does before login.
        client.settings = Settings(
            reply_max_segments=3,
            reply_max_total_chars=4000,
            qqbot_appid="bot-1",
        )
        client.started_at = datetime.now()
        client.api = SimpleNamespace(
            post_c2c_message=AsyncMock(),
            post_group_message=AsyncMock(),
        )
        return client

    def test_successful_segments_are_recorded_as_delivered(self) -> None:
        client = self._client()
        message = SimpleNamespace(
            id="message-1",
            content="你好",
            author=SimpleNamespace(user_openid="user-a"),
        )

        async def run() -> None:
            with patch(
                "doraymon.client.route_incoming_message",
                new=AsyncMock(return_value="第一段。" + ("内容" * 1100)),
            ):
                await client._handle_message(message, is_at_message=False)

        asyncio.run(run())

        session = ("private", "qqbot:bot-1:user-a", "qqbot:bot-1:user-a")
        delivered, failed = SEND_LOG.counts_for(session)
        self.assertEqual(failed, 0)
        self.assertGreaterEqual(delivered, 2)

    def test_failed_segment_is_recorded_with_a_safe_label(self) -> None:
        client = self._client()
        client.api.post_c2c_message = AsyncMock(side_effect=HttpFailure(500))
        message = SimpleNamespace(
            id="message-1",
            content="你好",
            author=SimpleNamespace(user_openid="user-a"),
        )

        async def run() -> None:
            with patch(
                "doraymon.client.route_incoming_message",
                new=AsyncMock(return_value="很短的一段"),
            ):
                await client._handle_message(message, is_at_message=False)

        asyncio.run(run())

        session = ("private", "qqbot:bot-1:user-a", "qqbot:bot-1:user-a")
        delivered, failed = SEND_LOG.counts_for(session)
        self.assertEqual((delivered, failed), (0, 1))
        failure = SEND_LOG.last_failure(session)
        self.assertIsNotNone(failure)
        self.assertEqual(failure.error_label, ERROR_HTTP_FORMAT.format(status=500))

    def test_group_reply_records_group_scope(self) -> None:
        client = self._client()
        message = SimpleNamespace(
            id="message-2",
            content="@bot 你好",
            group_openid="group-a",
            author=SimpleNamespace(member_openid="user-a"),
        )

        async def run() -> None:
            with patch(
                "doraymon.client.route_incoming_message",
                new=AsyncMock(return_value="群回复"),
            ):
                await client._handle_message(message, is_at_message=True)

        asyncio.run(run())

        session = ("group", "qqbot:bot-1:group-a", "qqbot:bot-1:user-a")
        results = SEND_LOG.results_for(session)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].scope_type, "group")
        self.assertTrue(results[0].success)


if __name__ == "__main__":
    unittest.main()
