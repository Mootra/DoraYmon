"""P0: a clear during an in-flight generation must retire that reply."""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from doraymon.config import Settings
from doraymon.context import BotContext
from plugins import chat
from tests.presentation import present_reply
from services.session_history import generate_revision, get_session_key, reply_allowed
from services.session_revisions import SESSION_REVISIONS
from storage.chat_history_store import init_chat_history_table, list_recent_messages
from storage.chat_summary_store import init_chat_summary_table


class SessionRevisionFenceTest(unittest.TestCase):
    def setUp(self) -> None:
        SESSION_REVISIONS.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.connection_patch = patch(
            "storage.chat_history_store.get_connection",
            side_effect=self._get_connection,
        )
        self.connection_patch.start()
        init_chat_history_table()
        init_chat_summary_table()

    def tearDown(self) -> None:
        self.connection_patch.stop()
        self.temp_dir.cleanup()
        SESSION_REVISIONS.clear()

    def _get_connection(self, name: str) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _context(self, args: str = "问题") -> BotContext:
        return BotContext(
            settings=Settings(
                deepseek_api_key="sk-test",
                chat_history_enabled=True,
                chat_history_limit=10,
                chat_history_max_content_length=4000,
            ),
            started_at=datetime.now(),
            user_openid="user-a",
            raw_content=f"/chat {args}",
            command="chat",
            args=args,
        )

    def _stored_contents(self) -> list[str]:
        return [
            message.content
            for message in list_recent_messages(
                "private", "qqbot::user-a", "qqbot::user-a", 10
            )
        ]

    def test_bump_retires_earlier_revisions_only(self) -> None:
        session = ("private", "qqbot::user-a", "qqbot::user-a")

        first = SESSION_REVISIONS.begin(session)
        self.assertTrue(SESSION_REVISIONS.is_current(session, first))

        SESSION_REVISIONS.bump(session)

        self.assertFalse(SESSION_REVISIONS.is_current(session, first))
        second = SESSION_REVISIONS.begin(session)
        self.assertNotEqual(first, second)
        self.assertTrue(SESSION_REVISIONS.is_current(session, second))

    def test_revisions_are_isolated_per_session(self) -> None:
        first = ("private", "qqbot::user-a", "qqbot::user-a")
        second = ("private", "qqbot::user-b", "qqbot::user-b")

        revision = SESSION_REVISIONS.begin(first)
        SESSION_REVISIONS.bump(second)

        self.assertTrue(SESSION_REVISIONS.is_current(first, revision))

    def test_reply_is_dropped_when_session_is_cleared_mid_generation(self) -> None:
        context = self._context(args="清空前的问题")
        call_count = 0

        def answer_then_clear(*args, **kwargs) -> str:
            nonlocal call_count
            call_count += 1
            # The user clears the conversation while the model is still working.
            chat.handle_clear_context(context)
            return "清空之后才生成的回复"

        with patch("plugins.chat.DeepSeekService") as service_cls_mock:
            service_cls_mock.return_value.chat_messages.side_effect = answer_then_clear

            reply = asyncio.run(present_reply(chat.handle(context)))

        self.assertEqual(call_count, 1)
        self.assertEqual(reply, "")
        self.assertEqual(self._stored_contents(), [])

    def test_reply_is_saved_when_no_clear_intervenes(self) -> None:
        context = self._context(args="普通问题")

        with patch("plugins.chat.DeepSeekService") as service_cls_mock:
            service_cls_mock.return_value.chat_messages.return_value = "普通回复"

            reply = asyncio.run(present_reply(chat.handle(context)))

        self.assertEqual(reply, "普通回复")
        self.assertEqual(self._stored_contents(), ["普通问题", "普通回复"])

    def test_reply_allowed_reports_a_stale_revision(self) -> None:
        context = self._context(args="问题")
        revision = generate_revision(context)
        self.assertIsNotNone(revision)

        chat.handle_clear_context(context)

        self.assertFalse(reply_allowed(context, revision))
        self.assertTrue(reply_allowed(context, generate_revision(context)))

    def test_clear_returns_a_report_and_retires_history(self) -> None:
        context = self._context(args="会被清空的问题")

        with patch("plugins.chat.DeepSeekService") as service_cls_mock:
            service_cls_mock.return_value.chat_messages.return_value = "会被清空的回复"
            asyncio.run(present_reply(chat.handle(context)))

        self.assertEqual(self._stored_contents(), ["会被清空的问题", "会被清空的回复"])

        report = chat.handle_clear_context(context)

        self.assertIn("已清空", report)
        self.assertIn("2 条消息", report)
        self.assertEqual(self._stored_contents(), [])

    def test_session_key_is_available_for_the_fence(self) -> None:
        context = self._context()

        self.assertEqual(
            get_session_key(context),
            ("private", "qqbot::user-a", "qqbot::user-a"),
        )


if __name__ == "__main__":
    unittest.main()
