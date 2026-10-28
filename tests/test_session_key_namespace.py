"""P0: platform and bot-account namespace reaches the stored session key."""

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
from services.session_history import get_session_key
from storage.chat_history_store import (
    init_chat_history_table,
    list_recent_messages,
    save_message,
)


class SessionKeyNamespaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.connection_patch = patch(
            "storage.chat_history_store.get_connection",
            side_effect=self._get_connection,
        )
        self.connection_patch.start()
        init_chat_history_table()

    def tearDown(self) -> None:
        self.connection_patch.stop()
        self.temp_dir.cleanup()

    def _get_connection(self, name: str) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _context(self, *, platform: str, bot_account: str, user_openid: str) -> BotContext:
        return BotContext(
            settings=Settings(
                deepseek_api_key="sk-test",
                chat_history_enabled=True,
                chat_history_limit=10,
            ),
            started_at=datetime.now(),
            user_openid=user_openid,
            raw_content="/chat 你好",
            command="chat",
            args="你好",
            platform=platform,
            bot_account=bot_account,
        )

    def test_session_key_carries_platform_and_bot_account(self) -> None:
        context = self._context(platform="qqbot", bot_account="bot-1", user_openid="user-a")

        self.assertEqual(
            get_session_key(context),
            ("private", "qqbot:bot-1:user-a", "qqbot:bot-1:user-a"),
        )

    def test_two_platforms_do_not_share_stored_history(self) -> None:
        with patch("plugins.chat.DeepSeekService") as service_cls_mock:
            service_cls_mock.return_value.chat_messages.side_effect = ["官方回复", "真号回复"]

            official = self._context(platform="qqbot", bot_account="bot-1", user_openid="user-a")
            personal = self._context(
                platform="qqaccount", bot_account="bot-1", user_openid="user-a"
            )
            asyncio.run(present_reply(chat.handle(official)))
            asyncio.run(present_reply(chat.handle(personal)))

            official_history = list_recent_messages(
                "private", "qqbot:bot-1:user-a", "qqbot:bot-1:user-a", 10
            )
            personal_history = list_recent_messages(
                "private", "qqaccount:bot-1:user-a", "qqaccount:bot-1:user-a", 10
            )

        self.assertEqual([message.content for message in official_history], ["你好", "官方回复"])
        self.assertEqual([message.content for message in personal_history], ["你好", "真号回复"])

    def test_legacy_bare_history_is_not_visible_to_a_platform_session(self) -> None:
        with patch("plugins.chat.DeepSeekService") as service_cls_mock:
            service_cls_mock.return_value.chat_messages.return_value = "回复"

            # A row written by the pre-namespacing schema.
            save_message("private", "user-a", "user-a", "user", "旧库里的历史")

            context = self._context(platform="qqbot", bot_account="bot-1", user_openid="user-a")
            asyncio.run(present_reply(chat.handle(context)))

            namespaced_history = list_recent_messages(
                "private", "qqbot:bot-1:user-a", "qqbot:bot-1:user-a", 10
            )
            legacy_history = list_recent_messages("private", "user-a", "user-a", 10)

        self.assertEqual([message.content for message in namespaced_history], ["你好", "回复"])
        self.assertEqual([message.content for message in legacy_history], ["旧库里的历史"])


if __name__ == "__main__":
    unittest.main()
