"""P2a/P2c groundwork: selection must not outrun the evidence."""

from __future__ import annotations

import unittest

from services.nearby_meal_service import (
    MealCatalog,
    MealRequest,
    render_selection,
    select_meal,
)
from services.recommendation_types import (
    FlavourNote,
    MealItem,
    PriceRecord,
    Shop,
    SourceKind,
    SourceRef,
    StartPoint,
    StartPrecision,
)
from services.synthetic_catalog import synthetic_items, synthetic_shops
from datetime import datetime, timezone


def _source(kind: SourceKind = SourceKind.MENU, reference: str = "src-1") -> SourceRef:
    return SourceRef(kind=kind, reference=reference, fetched_at=datetime.now(timezone.utc))


def _landmark(text: str = "示例大学") -> StartPoint:
    return StartPoint(text=text, precision=StartPrecision.LANDMARK)


def _catalog() -> MealCatalog:
    return MealCatalog(shops=synthetic_shops(), items=synthetic_items())


class RefusalTest(unittest.TestCase):
    def test_city_start_is_refused_instead_of_treated_as_nearby(self) -> None:
        selection = select_meal(
            _catalog(),
            StartPoint(text="武汉", precision=StartPrecision.CITY),
        )

        self.assertFalse(selection.is_usable)
        self.assertIn("范围太大", selection.refused_reason)

    def test_unknown_precision_is_refused(self) -> None:
        selection = select_meal(_catalog(), StartPoint(text="某地"))

        self.assertFalse(selection.is_usable)

    def test_empty_catalog_is_refused_without_inventing_a_shop(self) -> None:
        selection = select_meal(MealCatalog(), _landmark())

        self.assertFalse(selection.is_usable)
        self.assertIn("空的", selection.refused_reason)

    def test_empty_catalog_renders_without_a_recommendation(self) -> None:
        selection = select_meal(MealCatalog(), _landmark())

        text = render_selection(selection, _landmark())

        self.assertIn("空的", text)
        self.assertNotIn("：", text.split("，")[0].replace("本地餐点资料还是", ""))


class SelectionTest(unittest.TestCase):
    def test_complete_item_is_preferred_over_incomplete_ones(self) -> None:
        selection = select_meal(_catalog(), _landmark())

        self.assertTrue(selection.is_usable)
        self.assertEqual(selection.recommendation.item.item_id, "item-beef-noodle")
        self.assertEqual(selection.missing, ())

    def test_selection_reports_the_shop_and_item_together(self) -> None:
        selection = select_meal(_catalog(), _landmark())

        self.assertEqual(selection.recommendation.shop.shop_id, "shop-noodle")
        self.assertEqual(selection.recommendation.item.shop_id, "shop-noodle")
        self.assertTrue(selection.recommendation.is_grounded)
        self.assertTrue(selection.recommendation.is_decision_ready)

    def test_alternatives_are_returned_without_replacing_the_first_choice(self) -> None:
        selection = select_meal(_catalog(), _landmark())

        self.assertLessEqual(len(selection.alternatives), 2)
        for alternative in selection.alternatives:
            self.assertNotEqual(alternative.item.item_id, selection.recommendation.item.item_id)

    def test_excluded_shop_is_not_recommended(self) -> None:
        selection = select_meal(
            _catalog(),
            _landmark(),
            MealRequest(excluded_shop_ids=("shop-noodle",)),
        )

        self.assertTrue(selection.is_usable)
        self.assertNotEqual(selection.recommendation.shop.shop_id, "shop-noodle")

    def test_excluding_everything_refuses_rather_than_repeating(self) -> None:
        selection = select_meal(
            _catalog(),
            _landmark(),
            MealRequest(excluded_shop_ids=("shop-noodle", "shop-poi-only", "shop-stale")),
        )

        self.assertFalse(selection.is_usable)
        self.assertIn("不编造", selection.refused_reason)

    def test_distance_filter_drops_shops_without_a_known_distance(self) -> None:
        shop = Shop(
            shop_id="shop-unknown-distance",
            name="距离未知的店",
            source=_source(SourceKind.MAP),
            address="示例路 5 号",
        )
        item = MealItem(
            item_id="item-unknown-distance",
            shop_id="shop-unknown-distance",
            name="套餐",
            source=_source(),
        )
        catalog = MealCatalog(shops=(shop,), items=(item,))

        selection = select_meal(catalog, _landmark(), MealRequest(max_distance_meters=500))

        self.assertFalse(selection.is_usable)

    def test_distance_filter_keeps_near_shops(self) -> None:
        selection = select_meal(_catalog(), _landmark(), MealRequest(max_distance_meters=500))

        self.assertTrue(selection.is_usable)
        self.assertEqual(selection.recommendation.shop.shop_id, "shop-noodle")


class BudgetHonestyTest(unittest.TestCase):
    def test_budget_without_any_menu_price_is_reported_as_unverifiable(self) -> None:
        """A per-person map figure must never be used to claim a budget fits."""
        shop = Shop(shop_id="s1", name="只有人均的店", source=_source(SourceKind.MAP), distance_meters=100)
        item = MealItem(
            item_id="i1",
            shop_id="s1",
            name="套餐",
            source=_source(SourceKind.MAP),
            prices=(PriceRecord(basis="per_person", source=_source(SourceKind.MAP), amount_cny=25),),
        )
        catalog = MealCatalog(shops=(shop,), items=(item,))

        selection = select_meal(catalog, _landmark(), MealRequest(budget_cny=30))

        self.assertTrue(selection.is_usable)
        notes = " ".join(selection.notes)
        self.assertIn("无法确认", notes)
        # The reply must never claim the budget was met.
        self.assertNotIn("菜单价在", notes)
        rendered = render_selection(selection, _landmark())
        # The render states that the menu price is unknown, and the per-person
        # figure is never presented as a price for this item.
        self.assertIn("菜单价：资料里还没有", rendered)
        self.assertNotIn("¥25", rendered)
        self.assertNotIn("per_person", rendered)

    def test_menu_price_within_budget_is_stated_as_checked(self) -> None:
        selection = select_meal(_catalog(), _landmark(), MealRequest(budget_cny=25))

        self.assertTrue(selection.is_usable)
        self.assertEqual(selection.recommendation.item.item_id, "item-beef-noodle")

    def test_menu_price_over_budget_is_disqualified(self) -> None:
        """A known price over budget must not win just because others are unknown."""
        selection = select_meal(_catalog(), _landmark(), MealRequest(budget_cny=10))

        self.assertTrue(selection.is_usable)
        self.assertNotEqual(selection.recommendation.item.item_id, "item-beef-noodle")
        self.assertIn("无法确认", " ".join(selection.notes))

    def test_budget_refuses_when_every_priced_item_is_over(self) -> None:
        shop = Shop(shop_id="s1", name="贵店", source=_source(SourceKind.MAP), distance_meters=50)
        item = MealItem(
            item_id="i1",
            shop_id="s1",
            name="豪华套餐",
            source=_source(),
            prices=(PriceRecord(basis="menu", source=_source(), amount_cny=88),),
        )
        catalog = MealCatalog(shops=(shop,), items=(item,))

        selection = select_meal(catalog, _landmark(), MealRequest(budget_cny=20))

        self.assertFalse(selection.is_usable)
        self.assertIn("超过", selection.refused_reason)

    def test_priced_item_within_budget_wins_over_unpriced_items(self) -> None:
        selection = select_meal(_catalog(), _landmark(), MealRequest(budget_cny=25))

        self.assertEqual(selection.recommendation.item.item_id, "item-beef-noodle")

    def test_render_never_prints_a_zero_price(self) -> None:
        selection = select_meal(_catalog(), _landmark())

        text = render_selection(selection, _landmark())

        self.assertNotIn("¥0", text)
        self.assertNotIn("0 元", text)


class RenderTest(unittest.TestCase):
    def test_render_includes_shop_item_and_evidence(self) -> None:
        selection = select_meal(_catalog(), _landmark())

        text = render_selection(selection, _landmark())

        self.assertIn("示例兰州拉面（合成）", text)
        self.assertIn("牛肉拉面（合成）", text)
        self.assertIn("组成：", text)
        self.assertIn("份量：", text)
        self.assertIn("口味：", text)
        self.assertIn("菜单价：", text)
        self.assertIn("直线距离约320米", text)

    def test_render_states_missing_evidence_when_present(self) -> None:
        shop = Shop(shop_id="s1", name="只有 POI 的店", source=_source(SourceKind.MAP))
        item = MealItem(item_id="i1", shop_id="s1", name="招牌", source=_source(SourceKind.MAP))
        catalog = MealCatalog(shops=(shop,), items=(item,))

        selection = select_meal(catalog, _landmark())

        text = render_selection(selection, _landmark())

        self.assertIn("还缺：", text)
        self.assertIn("菜单价：资料里还没有", text)

    def test_render_of_a_refusal_includes_the_reason(self) -> None:
        selection = select_meal(MealCatalog(), _landmark())

        text = render_selection(selection, _landmark())

        self.assertTrue(text)
        self.assertIn("空的", text)


class RequestValidationTest(unittest.TestCase):
    def test_negative_budget_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MealRequest(budget_cny=0)

    def test_party_size_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            MealRequest(party_size=0)

    def test_suitable_meal_filter_applies(self) -> None:
        selection = select_meal(_catalog(), _landmark(), MealRequest(suitable_meal="早餐"))

        # No synthetic item is suitable for breakfast, so nothing qualifies and
        # the refusal is explicit rather than a mismatched suggestion.
        self.assertFalse(selection.is_usable)


class CatalogTest(unittest.TestCase):
    def test_item_referencing_an_unknown_shop_is_rejected(self) -> None:
        item = MealItem(item_id="i1", shop_id="missing", name="套餐", source=_source())

        with self.assertRaises(ValueError):
            MealCatalog(items=(item,))

    def test_add_item_requires_the_shop_first(self) -> None:
        catalog = MealCatalog()
        item = MealItem(item_id="i1", shop_id="s1", name="套餐", source=_source())

        with self.assertRaises(ValueError):
            catalog.add_item(item)

    def test_lookups_work_after_adding(self) -> None:
        catalog = MealCatalog()
        shop = Shop(shop_id="s1", name="示例店", source=_source(SourceKind.MAP))
        catalog.add_shop(shop)
        catalog.add_item(MealItem(item_id="i1", shop_id="s1", name="套餐", source=_source()))

        self.assertEqual(catalog.shop_count, 1)
        self.assertEqual(catalog.item_count, 1)
        self.assertEqual(catalog.shop_by_id("s1").name, "示例店")
        self.assertEqual(len(catalog.items_for_shop("s1")), 1)
        self.assertEqual(catalog.items_for_shop("missing"), ())


if __name__ == "__main__":
    unittest.main()
