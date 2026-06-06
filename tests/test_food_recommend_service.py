from __future__ import annotations

import unittest

from services.food_recommend_service import parse_food_conditions, recommend_food


class FoodRecommendServiceTest(unittest.TestCase):
    def test_parse_budget_and_mood(self) -> None:
        conditions = parse_food_conditions("今天很累，想吃30以内的")

        self.assertEqual(conditions.budget_max, 30)
        self.assertIn("累", conditions.moods)

    def test_recommend_one_food_with_reason(self) -> None:
        result = recommend_food("今天很累，想吃热的，30以内")

        self.assertTrue(result.food_name)
        self.assertIn(result.food_name, result.text)
        self.assertTrue(result.reason.endswith("。"))

    def test_spicy_query_returns_spicy_food(self) -> None:
        result = recommend_food("今天想吃辣的")

        self.assertIn(result.food_name, {"砂锅米线", "麻辣烫", "小火锅"})

    def test_not_spicy_does_not_match_spicy_condition(self) -> None:
        conditions = parse_food_conditions("今天不能吃辣")

        self.assertIn("不辣", conditions.flavors)
        self.assertNotIn("辣", conditions.flavors)

    def test_low_budget_still_returns_affordable_food(self) -> None:
        result = recommend_food("15以内")

        self.assertIn(result.food_name, {"砂锅米线", "鸡腿盖饭", "番茄鸡蛋面", "鲜肉馄饨", "皮蛋瘦肉粥"})


if __name__ == "__main__":
    unittest.main()
