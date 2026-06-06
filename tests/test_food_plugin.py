from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from doraymon.router import COMMANDS
from plugins.food import (
    handle_forget_preference,
    handle_my_preferences,
    handle_remember_preference,
)


class FoodPluginTest(unittest.TestCase):
    def _context(self, args: str = "", user_openid: str = "user-a") -> SimpleNamespace:
        return SimpleNamespace(args=args, user_openid=user_openid)

    @patch("plugins.food.add_food_preference", return_value=True)
    @patch("plugins.food.list_food_preferences", return_value=[])
    def test_remember_preference(self, list_mock, add_mock) -> None:
        reply = handle_remember_preference(self._context("我不吃香菜"))

        self.assertEqual(reply, "记住了：我不吃香菜")
        add_mock.assert_called_once_with("user-a", "我不吃香菜")

    @patch(
        "plugins.food.list_food_preferences",
        return_value=["我不吃香菜", "我喜欢辣"],
    )
    def test_list_preferences(self, list_mock) -> None:
        reply = handle_my_preferences(self._context())

        self.assertIn("1. 我不吃香菜", reply)
        self.assertIn("2. 我喜欢辣", reply)

    @patch("plugins.food.remove_food_preference", return_value=True)
    def test_forget_preference(self, remove_mock) -> None:
        reply = handle_forget_preference(self._context("我不吃香菜"))

        self.assertEqual(reply, "已经忘记：我不吃香菜")
        remove_mock.assert_called_once_with("user-a", "我不吃香菜")

    def test_memory_commands_require_user_openid(self) -> None:
        context = self._context("我喜欢辣", user_openid="")

        self.assertIn("无法识别用户", handle_remember_preference(context))
        self.assertIn("无法识别用户", handle_my_preferences(context))
        self.assertIn("无法识别用户", handle_forget_preference(context))

    def test_food_memory_commands_are_registered(self) -> None:
        self.assertIn("记住口味", COMMANDS)
        self.assertIn("我的口味", COMMANDS)
        self.assertIn("忘记口味", COMMANDS)


if __name__ == "__main__":
    unittest.main()
