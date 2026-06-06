from __future__ import annotations

import unittest

from services.intent_service import FOOD_INTENT, detect_intent


class IntentServiceTest(unittest.TestCase):
    def test_detects_food_decision_phrases(self) -> None:
        messages = (
            "今晚吃啥",
            "午饭吃什么好",
            "不知道吃什么了",
            "外卖点什么",
            "推荐点吃的",
            "夜宵来点啥",
        )

        for message in messages:
            with self.subTest(message=message):
                result = detect_intent(message)
                self.assertIsNotNone(result)
                self.assertEqual(result.name, FOOD_INTENT)

    def test_does_not_trigger_on_hunger_only(self) -> None:
        self.assertIsNone(detect_intent("我好饿"))

    def test_does_not_trigger_on_food_history(self) -> None:
        messages = (
            "我刚刚吃完晚饭",
            "你吃什么了",
            "昨天吃了什么",
        )

        for message in messages:
            with self.subTest(message=message):
                self.assertIsNone(detect_intent(message))

    def test_does_not_trigger_on_unrelated_chat(self) -> None:
        self.assertIsNone(detect_intent("今天工作有点忙"))
        self.assertIsNone(detect_intent("企鹅吃什么"))


if __name__ == "__main__":
    unittest.main()
