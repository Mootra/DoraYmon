from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

from doraymon.config import Settings
from doraymon.context import BotContext
from doraymon.router import COMMANDS, route_incoming_message


class RouterTest(unittest.TestCase):
    def _context(self, content: str) -> BotContext:
        return BotContext(
            settings=Settings(),
            started_at=datetime.now(),
            user_openid="user-a",
            raw_content=content,
        )

    @patch("doraymon.router.food.handle", return_value="自然语言食物推荐")
    def test_natural_food_message_routes_to_food_plugin(self, food_mock) -> None:
        context = self._context("今天很累，晚饭吃什么")

        reply = asyncio.run(route_incoming_message(context, fallback_command="chat"))

        self.assertEqual(reply, "自然语言食物推荐")
        self.assertEqual(context.command, "吃什么")
        self.assertEqual(context.args, "今天很累，晚饭吃什么")
        food_mock.assert_called_once_with(context)

    @patch("doraymon.router.COMMANDS")
    def test_private_non_food_message_falls_back_to_chat(self, commands_mock) -> None:
        chat_handler = Mock(return_value="聊天回复")
        commands_mock.get.return_value = chat_handler
        context = self._context("你好")

        reply = asyncio.run(route_incoming_message(context, fallback_command="chat"))

        self.assertEqual(reply, "聊天回复")
        self.assertEqual(context.command, "chat")
        self.assertEqual(context.args, "你好")

    @patch("doraymon.router.food.handle", return_value="不应调用")
    def test_unmatched_group_message_returns_empty(self, food_mock) -> None:
        context = self._context("今天工作有点忙")

        reply = asyncio.run(route_incoming_message(context))

        self.assertEqual(reply, "")
        food_mock.assert_not_called()

    def test_explicit_command_keeps_priority(self) -> None:
        context = self._context("/吃什么 清淡一点")
        food_handler = Mock(return_value="命令推荐")

        with patch.dict(COMMANDS, {"吃什么": food_handler}):
            reply = asyncio.run(route_incoming_message(context, fallback_command="chat"))

        self.assertEqual(reply, "命令推荐")
        self.assertEqual(context.command, "吃什么")
        self.assertEqual(context.args, "清淡一点")

    @patch("doraymon.router.food.handle", return_value="不应调用")
    def test_natural_trigger_can_be_disabled(self, food_mock) -> None:
        context = self._context("今晚吃什么")
        context.settings = Settings(food_natural_trigger_enabled=False)

        reply = asyncio.run(route_incoming_message(context))

        self.assertEqual(reply, "")
        food_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
