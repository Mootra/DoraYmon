from __future__ import annotations

import unittest
from unittest.mock import patch

from services.food_recommend_service import FoodItem, load_food_catalog, parse_food_conditions, recommend_food


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

    def test_saved_preference_is_used_when_query_has_no_flavor(self) -> None:
        result = recommend_food("今天吃晚饭", ["我喜欢辣"])

        self.assertIn(result.food_name, {"砂锅米线", "麻辣烫", "小火锅"})
        self.assertIn("保存的辣口味", result.reason)

    def test_current_flavor_overrides_saved_preference(self) -> None:
        conditions = parse_food_conditions("今天想吃清淡的", ["我喜欢辣"])

        self.assertIn("清淡", conditions.flavors)
        self.assertNotIn("辣", conditions.flavors)
        self.assertEqual(conditions.remembered_flavors, ())

    def test_low_budget_still_returns_affordable_food(self) -> None:
        result = recommend_food("15以内")

        self.assertIn(result.food_name, {"砂锅米线", "鸡腿盖饭", "番茄鸡蛋面", "鲜肉馄饨", "皮蛋瘦肉粥"})

    def test_restrictions_are_respected_even_when_other_food_matches_mood(self) -> None:
        catalog = {item.name: item for item in load_food_catalog()}
        for _ in range(20):
            result = recommend_food("今天心情好，早餐吃什么，不辣")
            self.assertIn("不辣", catalog[result.food_name].flavors)
            self.assertIn("早餐", catalog[result.food_name].scenes)

    def test_no_match_does_not_silently_ignore_budget_or_flavor(self) -> None:
        for query in ("只有5元", "早餐，重口，不辣"):
            with self.subTest(query=query):
                result = recommend_food(query)
                self.assertEqual(result.food_name, "")
                self.assertIn("没找到", result.text)

    def test_budget_range_is_honest_and_full_range_is_preferred(self) -> None:
        result = recommend_food("20元以内")
        self.assertEqual(result.food_name, "皮蛋瘦肉粥")
        result = recommend_food("只有10元")
        self.assertIn("可能超过你的10元预算", result.reason)
        self.assertNotIn("控制在", result.reason)

    def test_mild_spice_and_unhappy_do_not_match_opposites(self) -> None:
        conditions = parse_food_conditions("我不开心，吃点微辣的，预算30")
        self.assertEqual(conditions.flavors, ("微辣",))
        self.assertNotIn("开心", conditions.moods)
        self.assertEqual(conditions.budget_max, 30)

    def test_time_limit_is_not_a_budget(self) -> None:
        self.assertIsNone(parse_food_conditions("只有10分钟吃饭").budget_max)
        self.assertEqual(parse_food_conditions("只有10分钟吃饭，预算30").budget_max, 30)

    def test_tied_candidates_are_all_available_to_choice(self) -> None:
        foods = [FoodItem(name, (), (), (), (), 10, 20) for name in ("合成面", "合成饭")]
        with patch("services.food_recommend_service.load_food_catalog", return_value=foods), patch("services.food_recommend_service.random.choice", side_effect=lambda items: items[-1]) as choice:
            result = recommend_food()
        self.assertEqual(result.food_name, "合成饭")
        self.assertEqual(choice.call_args.args[0], foods)


if __name__ == "__main__":
    unittest.main()
