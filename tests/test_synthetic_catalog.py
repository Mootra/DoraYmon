"""P0: the synthetic catalog drives the contract without inventing facts."""

from __future__ import annotations

import unittest

from services.recommendation_types import MealRecommendation, SourceKind
from services.synthetic_catalog import (
    item_by_id,
    items_for_shop,
    shop_by_id,
    synthetic_items,
    synthetic_shops,
)


class SyntheticCatalogTest(unittest.TestCase):
    def test_catalog_is_internally_consistent(self) -> None:
        shop_ids = {shop.shop_id for shop in synthetic_shops()}

        for item in synthetic_items():
            with self.subTest(item=item.item_id):
                self.assertIn(item.shop_id, shop_ids)

    def test_lookups_find_known_records_and_miss_unknown_ones(self) -> None:
        self.assertIsNotNone(shop_by_id("shop-noodle"))
        self.assertIsNotNone(item_by_id("item-beef-noodle"))
        self.assertIsNone(shop_by_id("shop-missing"))
        self.assertIsNone(item_by_id("item-missing"))
        self.assertEqual(items_for_shop("shop-missing"), ())

    def test_complete_item_is_decision_ready(self) -> None:
        item = item_by_id("item-beef-noodle")

        self.assertTrue(item.is_decision_ready())
        self.assertEqual(item.kind, "combo")

    def test_poi_only_item_cannot_support_a_budget_claim(self) -> None:
        item = item_by_id("item-poi-guess")

        self.assertFalse(item.is_decision_ready())
        self.assertEqual(item.menu_prices, ())
        self.assertIn("菜单价格", item.missing_evidence())
        self.assertIn("餐点组成", item.missing_evidence())

    def test_recommendation_over_the_poi_only_item_is_not_grounded(self) -> None:
        shop = shop_by_id("shop-poi-only")
        item = item_by_id("item-poi-guess")

        recommendation = MealRecommendation(
            shop=shop,
            item=item,
            source=shop.source,
            missing=item.missing_evidence(),
        )

        self.assertTrue(recommendation.is_grounded)
        self.assertFalse(recommendation.is_decision_ready)
        self.assertIn("份量或规格", recommendation.describe_missing())

    def test_stale_menu_keeps_its_own_timestamp(self) -> None:
        item = item_by_id("item-stale-mixian")
        fresh = item_by_id("item-beef-noodle")

        self.assertLess(item.source.fetched_at, fresh.source.fetched_at)
        self.assertEqual(item.source.kind, SourceKind.MENU)

    def test_incomplete_item_reports_exactly_its_gaps(self) -> None:
        item = item_by_id("item-stale-mixian")

        self.assertEqual(item.missing_evidence(), ("份量或规格", "口味描述"))


if __name__ == "__main__":
    unittest.main()
