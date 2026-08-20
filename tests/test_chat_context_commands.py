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
from doraymon.router import COMMANDS, route_incoming_message
from storage.chat_history_store import (
    init_chat_history_table,
    list_recent_messages,
    save_message,
)


class ChatContextCommandsTest(unittest.TestCase):
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

    def _context(
        self,
        raw_content: str,
        user_openid: str = "user-a",
        group_openid: str = "",
        history_enabled: bool = True,
    ) -> BotContext:
        return BotContext(
            settings=Settings(
                chat_history_enabled=history_enabled,
                chat_history_limit=10,
            ),
            started_at=datetime.now(),
            user_openid=user_openid,
            group_openid=group_openid,
            raw_content=raw_content,
        )

    def _contents(self, scope_type: str, scope_openid: str, user_openid: str) -> list[str]:
        return [
            message.content
            for message in list_recent_messages(scope_type, scope_openid, user_openid, 10)
        ]

    def test_clear_context_command_is_registered(self) -> None:
        self.assertIn("清空上下文", COMMANDS)
        self.assertIn("上下文状态", COMMANDS)
        self.assertIn("上下文摘要", COMMANDS)

    def test_clear_context_clears_current_private_user(self) -> None:
        save_message("private", "user-a", "user-a", "user", "A 的问题")
        save_message("private", "user-a", "user-a", "assistant", "A 的回答")

        reply = asyncio.run(route_incoming_message(self._context("/清空上下文")))

        self.assertIn("已清空", reply)
        self.assertNotIn("A 的问题", reply)
        self.assertEqual(self._contents("private", "user-a", "user-a"), [])

    def test_clear_context_does_not_affect_other_private_user(self) -> None:
        save_message("private", "user-a", "user-a", "user", "A 的问题")
        save_message("private", "user-b", "user-b", "user", "B 的问题")

        asyncio.run(route_incoming_message(self._context("/清空上下文", user_openid="user-a")))

        self.assertEqual(self._contents("private", "user-a", "user-a"), [])
        self.assertEqual(self._contents("private", "user-b", "user-b"), ["B 的问题"])

    def test_clear_context_clears_current_group_user(self) -> None:
        save_message("group", "group-a", "user-a", "user", "A 在群 A")

        reply = asyncio.run(
            route_incoming_message(
                self._context("/清空上下文", user_openid="user-a", group_openid="group-a")
            )
        )

        self.assertIn("已清空", reply)
        self.assertEqual(self._contents("group", "group-a", "user-a"), [])

    def test_clear_context_does_not_affect_same_group_other_user(self) -> None:
        save_message("group", "group-a", "user-a", "user", "A 在群 A")
        save_message("group", "group-a", "user-b", "user", "B 在群 A")

        asyncio.run(
            route_incoming_message(
                self._context("/清空上下文", user_openid="user-a", group_openid="group-a")
            )
        )

        self.assertEqual(self._contents("group", "group-a", "user-a"), [])
        self.assertEqual(self._contents("group", "group-a", "user-b"), ["B 在群 A"])

    def test_clear_context_does_not_affect_other_group(self) -> None:
        save_message("group", "group-a", "user-a", "user", "A 在群 A")
        save_message("group", "group-b", "user-a", "user", "A 在群 B")

        asyncio.run(
            route_incoming_message(
                self._context("/清空上下文", user_openid="user-a", group_openid="group-a")
            )
        )

        self.assertEqual(self._contents("group", "group-a", "user-a"), [])
        self.assertEqual(self._contents("group", "group-b", "user-a"), ["A 在群 B"])

    def test_context_status_does_not_return_full_chat_content(self) -> None:
        save_message("private", "user-a", "user-a", "user", "这是一段不该返回的完整聊天内容")

        reply = asyncio.run(route_incoming_message(self._context("/上下文状态")))

        self.assertIn("短期上下文状态", reply)
        self.assertNotIn("这是一段不该返回的完整聊天内容", reply)

    def test_context_status_shows_enabled_state(self) -> None:
        reply = asyncio.run(route_incoming_message(self._context("/上下文状态", history_enabled=True)))

        self.assertIn("是否开启：开启", reply)

    def test_context_status_shows_message_count_and_session_type(self) -> None:
        save_message("group", "group-a", "user-a", "user", "第一条")
        save_message("group", "group-a", "user-a", "assistant", "第二条")

        reply = asyncio.run(
            route_incoming_message(
                self._context("/上下文状态", user_openid="user-a", group_openid="group-a")
            )
        )

        self.assertIn("当前会话类型：group", reply)
        self.assertIn("当前会话已保存消息数量：2", reply)
        self.assertIn("读取历史上限：10", reply)
        self.assertIn("上下文过期时间：60 分钟", reply)

    def test_context_summary_shows_only_complete_current_session_turns(self) -> None:
        save_message("private", "user-a", "user-a", "user", "A 的完整问题")
        save_message("private", "user-a", "user-a", "assistant", "A 的完整回答")
        save_message("private", "user-a", "user-a", "user", "A 的半轮问题")
        save_message("private", "user-b", "user-b", "user", "B 的秘密问题")
        save_message("private", "user-b", "user-b", "assistant", "B 的秘密回答")

        reply = asyncio.run(route_incoming_message(self._context("/上下文摘要")))

        self.assertIn("A 的完整问题", reply)
        self.assertIn("A 的完整回答", reply)
        self.assertNotIn("A 的半轮问题", reply)
        self.assertNotIn("B 的秘密问题", reply)

    def test_context_summary_reports_empty_session(self) -> None:
        reply = asyncio.run(route_incoming_message(self._context("/上下文摘要")))

        self.assertIn("还没有可用的完整问答上下文", reply)

    def test_commands_are_safe_when_history_is_disabled(self) -> None:
        status_reply = asyncio.run(
            route_incoming_message(self._context("/上下文状态", history_enabled=False))
        )
        clear_reply = asyncio.run(
            route_incoming_message(self._context("/清空上下文", history_enabled=False))
        )

        self.assertIn("是否开启：关闭", status_reply)
        self.assertIn("已清空", clear_reply)

    @patch("doraymon.router.food.handle", return_value="食物推荐")
    def test_food_natural_language_priority_is_unchanged(self, food_mock) -> None:
        context = self._context("今晚吃什么")

        reply = asyncio.run(route_incoming_message(context, fallback_command="chat"))

        self.assertEqual(reply, "食物推荐")
        food_mock.assert_called_once_with(context)

    def test_no_real_env_or_api_key_required(self) -> None:
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
