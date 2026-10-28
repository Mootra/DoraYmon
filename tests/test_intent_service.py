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
            "今晚叫什么外卖",
            "附近有什么吃的",
            "江西师范大学附近吃什么",
            "这附近有什么餐馆",
            "学校周边哪家好吃",
            # 真实聊天里出现的说法：这些此前全部落到普通聊天。
            "附近有什么",
            "附近有啥",
            "附近有什么好吃的",
            "附近有什么店",
            "附近吃点啥",
            "江西师范大学附近",
            "学校附近",
            "瑶湖校区附近有什么",
            "师大附近有推荐的店吗",
            "推荐个附近的",
            "附近有奶茶吗",
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
        self.assertIsNone(detect_intent("附近有什么图书馆"))
        self.assertIsNone(detect_intent("学校附近租房贵吗"))
        self.assertIsNone(detect_intent("推荐附近的图书馆"))
        self.assertIsNone(detect_intent("附近推荐的超市"))
        self.assertIsNone(detect_intent("附近哪家超市便宜"))

    def test_nearby_phrases_that_are_not_about_food_stay_out(self) -> None:
        """“地点 + 附近”只在整句以附近收尾时才算找饭吃。

        放宽意图的代价是把普通问题当成点餐，和漏掉点餐一样是错的。
        """
        for message in (
            "附近有超市吗",
            "这附近有什么公交站",
            "附近适合散步吗",
            "附近租房贵吗",
            "学校附近哪里可以打印",
        ):
            with self.subTest(message=message):
                self.assertIsNone(detect_intent(message))

    def test_names_and_non_food_orders_do_not_trigger_food(self) -> None:
        for message in ("我叫什么", "我的猫叫什么", "你叫什么名字", "这个函数叫什么", "你知道我叫什么吗", "现在几点什么安排"):
            with self.subTest(message=message):
                self.assertIsNone(detect_intent(message))


if __name__ == "__main__":
    unittest.main()
