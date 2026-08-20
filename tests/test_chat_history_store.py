from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from storage.chat_history_store import (
    clear_messages,
    init_chat_history_table,
    list_recent_messages,
    save_message,
    save_turn,
)


class ChatHistoryStoreTest(unittest.TestCase):
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

    def test_table_has_required_columns(self) -> None:
        with closing(self._get_connection("doraymon")) as connection:
            rows = connection.execute("PRAGMA table_info(chat_messages)").fetchall()

        column_names = {str(row["name"]) for row in rows}
        self.assertGreaterEqual(
            column_names,
            {
                "id",
                "scope_type",
                "scope_openid",
                "user_openid",
                "role",
                "content",
                "created_at",
            },
        )

    def test_save_and_read_user_message(self) -> None:
        saved = save_message("private", "user-a", "user-a", "user", "你好")

        messages = list_recent_messages("private", "user-a", "user-a", limit=10)

        self.assertEqual(saved.role, "user")
        self.assertEqual(saved.content, "你好")
        self.assertEqual([message.content for message in messages], ["你好"])

    def test_save_and_read_assistant_message(self) -> None:
        save_message("private", "user-a", "user-a", "assistant", "你好，我是 DoraYmon")

        messages = list_recent_messages("private", "user-a", "user-a", limit=10)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].role, "assistant")
        self.assertEqual(messages[0].content, "你好，我是 DoraYmon")

    def test_recent_message_limit_is_applied(self) -> None:
        for index in range(5):
            save_message("private", "user-a", "user-a", "user", f"消息 {index}")

        messages = list_recent_messages("private", "user-a", "user-a", limit=3)

        self.assertEqual([message.content for message in messages], ["消息 2", "消息 3", "消息 4"])

    def test_save_turn_is_atomic_when_assistant_content_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            save_turn(
                "private",
                "user-a",
                "user-a",
                "正常问题",
                "包含 DEEPSEEK_" + "API_KEY= 的回答",
            )

        self.assertEqual(list_recent_messages("private", "user-a", "user-a", 10), [])

    def test_save_turn_prunes_to_complete_recent_turns(self) -> None:
        for index in range(4):
            save_turn(
                "private",
                "user-a",
                "user-a",
                f"问题 {index}",
                f"回答 {index}",
                retain_messages=5,
            )

        messages = list_recent_messages("private", "user-a", "user-a", 10)

        self.assertEqual(
            [(message.role, message.content) for message in messages],
            [
                ("user", "问题 2"),
                ("assistant", "回答 2"),
                ("user", "问题 3"),
                ("assistant", "回答 3"),
            ],
        )

    def test_messages_are_returned_from_old_to_new(self) -> None:
        save_message("private", "user-a", "user-a", "user", "第一条")
        save_message("private", "user-a", "user-a", "assistant", "第二条")
        save_message("private", "user-a", "user-a", "user", "第三条")

        messages = list_recent_messages("private", "user-a", "user-a", limit=10)

        self.assertEqual([message.content for message in messages], ["第一条", "第二条", "第三条"])

    def test_private_users_are_isolated(self) -> None:
        save_message("private", "user-a", "user-a", "user", "A 的私聊")
        save_message("private", "user-b", "user-b", "user", "B 的私聊")

        messages = list_recent_messages("private", "user-a", "user-a", limit=10)

        self.assertEqual([message.content for message in messages], ["A 的私聊"])

    def test_group_users_are_isolated_by_group_and_user(self) -> None:
        save_message("group", "group-a", "user-a", "user", "A 在群 A")
        save_message("group", "group-a", "user-b", "user", "B 在群 A")
        save_message("group", "group-b", "user-a", "user", "A 在群 B")

        messages = list_recent_messages("group", "group-a", "user-a", limit=10)

        self.assertEqual([message.content for message in messages], ["A 在群 A"])

    def test_group_and_private_sessions_are_isolated(self) -> None:
        save_message("private", "user-a", "user-a", "user", "A 的私聊")
        save_message("group", "group-a", "user-a", "user", "A 的群聊")

        private_messages = list_recent_messages("private", "user-a", "user-a", limit=10)
        group_messages = list_recent_messages("group", "group-a", "user-a", limit=10)

        self.assertEqual([message.content for message in private_messages], ["A 的私聊"])
        self.assertEqual([message.content for message in group_messages], ["A 的群聊"])

    def test_clear_messages_only_removes_target_session(self) -> None:
        save_message("private", "user-a", "user-a", "user", "要清空")
        save_message("private", "user-b", "user-b", "user", "要保留")
        save_message("group", "group-a", "user-a", "user", "群聊也保留")

        removed = clear_messages("private", "user-a", "user-a")

        self.assertEqual(removed, 1)
        self.assertEqual(list_recent_messages("private", "user-a", "user-a", 10), [])
        self.assertEqual(
            [message.content for message in list_recent_messages("private", "user-b", "user-b", 10)],
            ["要保留"],
        )
        self.assertEqual(
            [message.content for message in list_recent_messages("group", "group-a", "user-a", 10)],
            ["群聊也保留"],
        )

    def test_long_content_is_truncated_to_limit(self) -> None:
        save_message(
            "private",
            "user-a",
            "user-a",
            "user",
            "abcdef",
            max_content_length=4,
        )

        messages = list_recent_messages("private", "user-a", "user-a", limit=10)

        self.assertEqual(messages[0].content, "abcd")

    def test_sensitive_configuration_content_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            save_message(
                "private",
                "user-a",
                "user-a",
                "user",
                "DEEPSEEK_" + "API_KEY=secret",
            )

        self.assertEqual(list_recent_messages("private", "user-a", "user-a", 10), [])

    def test_does_not_need_real_env_or_api_key(self) -> None:
        source = Path(__file__).read_text(encoding="utf-8")

        forbidden_patterns = (
            "load_" + "settings()",
            'open("' + '.env"',
            "QQBOT_" + "SECRET=",
            "DEEPSEEK_" + "API_KEY=",
        )
        for pattern in forbidden_patterns:
            with self.subTest(pattern=pattern):
                self.assertNotIn(pattern, source)


if __name__ == "__main__":
    unittest.main()
