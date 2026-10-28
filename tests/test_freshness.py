"""Staleness: an old price must not read like a current one.

SourceRef.is_stale existed and was tested, but nothing called it, so a price from a
year-old page rendered exactly like one fetched this morning. These tests cover the
check, the per-fact age limits, and the rendering that now carries the age.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from services.freshness import (
    DEFAULT_MAX_AGE_DAYS,
    MAX_AGE_DAYS_BY_KIND,
    assess,
    assess_item,
    assess_shop,
    max_age_days_for,
    stale_note,
)
from services.nearby_meal_service import MealCatalog, MealRequest, render_selection, select_meal
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

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


def _source(kind: SourceKind, reference: str = "ref", *, days: int = 0) -> SourceRef:
    return SourceRef(
        kind=kind, reference=reference, fetched_at=NOW - timedelta(days=days)
    )


def _item(*, days: int = 0, price_days: int | None = None) -> MealItem:
    source = _source(SourceKind.MENU, "menu/1", days=days)
    price_source = _source(
        SourceKind.MENU, "menu/1#price", days=days if price_days is None else price_days
    )
    return MealItem(
        item_id="i1",
        shop_id="s1",
        name="牛肉拉面",
        source=source,
        composition=("拉面", "牛肉"),
        composition_source=source,
        portion="大碗约 600 克",
        portion_source=source,
        flavours=(FlavourNote(description="清汤牛肉汤底，面条偏硬", source=source),),
        prices=(PriceRecord(basis="menu", source=price_source, amount_cny=18),),
    )


def _shop(*, days: int = 0) -> Shop:
    return Shop(
        shop_id="s1",
        name="示例拉面店",
        source=_source(SourceKind.MAP, "map/1", days=days),
        address="示例路 1 号",
        distance_meters=320,
    )


class AgeLimitTest(unittest.TestCase):
    def test_each_source_kind_has_a_limit(self) -> None:
        for kind in SourceKind:
            with self.subTest(kind=kind):
                self.assertGreater(max_age_days_for(kind), 0)

    def test_a_menu_price_expires_sooner_than_a_location(self) -> None:
        """They do not go out of date at the same rate."""
        self.assertLess(
            max_age_days_for(SourceKind.MENU), max_age_days_for(SourceKind.MAP)
        )

    def test_an_unknown_kind_gets_the_default(self) -> None:
        self.assertEqual(max_age_days_for("telepathy"), DEFAULT_MAX_AGE_DAYS)

    def test_a_kind_given_as_a_string_is_accepted(self) -> None:
        self.assertEqual(max_age_days_for("menu"), MAX_AGE_DAYS_BY_KIND[SourceKind.MENU])


class AssessTest(unittest.TestCase):
    def test_a_recent_source_is_not_stale(self) -> None:
        freshness = assess(_source(SourceKind.MENU, days=5), now=NOW)

        self.assertFalse(freshness.stale)
        self.assertEqual(freshness.describe(), "")

    def test_an_old_source_is_stale_and_states_its_age(self) -> None:
        freshness = assess(_source(SourceKind.MENU, days=400), now=NOW)

        self.assertTrue(freshness.stale)
        self.assertIn("400 天", freshness.describe())
        self.assertIn("2025-09-14", freshness.describe())

    def test_the_boundary_day_is_not_stale(self) -> None:
        """Exactly at the limit is still within it."""
        limit = max_age_days_for(SourceKind.MENU)
        freshness = assess(_source(SourceKind.MENU, days=limit), now=NOW)

        self.assertFalse(freshness.stale)

    def test_one_day_past_the_limit_is_stale(self) -> None:
        limit = max_age_days_for(SourceKind.MENU)
        freshness = assess(_source(SourceKind.MENU, days=limit + 1), now=NOW)

        self.assertTrue(freshness.stale)

    def test_a_map_source_survives_longer_than_a_menu_source(self) -> None:
        days = max_age_days_for(SourceKind.MENU) + 10

        self.assertTrue(assess(_source(SourceKind.MENU, days=days), now=NOW).stale)
        self.assertFalse(assess(_source(SourceKind.MAP, days=days), now=NOW).stale)

    def test_nothing_to_assess_yields_nothing(self) -> None:
        self.assertIsNone(assess(None, now=NOW))

    def test_a_naive_timestamp_is_compared_as_utc_not_guessed(self) -> None:
        naive = SourceRef(
            kind=SourceKind.MENU,
            reference="ref",
            fetched_at=(NOW - timedelta(days=400)).replace(tzinfo=None),
        )

        freshness = assess(naive, now=NOW)

        self.assertTrue(freshness.stale)

    def test_a_future_timestamp_is_clamped_to_zero_not_negative(self) -> None:
        future = SourceRef(
            kind=SourceKind.MENU,
            reference="ref",
            fetched_at=NOW + timedelta(days=10),
        )

        freshness = assess(future, now=NOW)

        self.assertEqual(freshness.age_days, 0)
        self.assertFalse(freshness.stale)


class AssessItemTest(unittest.TestCase):
    def test_a_fresh_item_is_not_stale(self) -> None:
        self.assertFalse(assess_item(_item(days=5), now=NOW).stale)

    def test_a_stale_price_makes_the_item_stale(self) -> None:
        """The price is what a user acts on, so it decides."""
        item = _item(days=5, price_days=400)

        freshness = assess_item(item, now=NOW)

        self.assertTrue(freshness.stale)
        self.assertIn("400 天", freshness.describe())

    def test_the_oldest_stale_source_is_reported(self) -> None:
        item = _item(days=400, price_days=200)

        freshness = assess_item(item, now=NOW)

        self.assertIn("400 天", freshness.describe())

    def test_nothing_to_assess_yields_nothing(self) -> None:
        self.assertIsNone(assess_item(None, now=NOW))


class AssessShopTest(unittest.TestCase):
    def test_a_recent_shop_is_not_stale(self) -> None:
        self.assertFalse(assess_shop(_shop(days=10), now=NOW).stale)

    def test_a_very_old_shop_is_stale(self) -> None:
        self.assertTrue(assess_shop(_shop(days=999), now=NOW).stale)

    def test_nothing_to_assess_yields_nothing(self) -> None:
        self.assertIsNone(assess_shop(None, now=NOW))


class StaleNoteTest(unittest.TestCase):
    def test_no_stale_sources_means_no_note(self) -> None:
        self.assertEqual(stale_note(assess(_source(SourceKind.MENU, days=1), now=NOW)), "")

    def test_nothing_at_all_means_no_note(self) -> None:
        self.assertEqual(stale_note(None, None), "")

    def test_the_note_names_the_date_and_the_limit(self) -> None:
        note = stale_note(assess(_source(SourceKind.MENU, days=400), now=NOW))

        self.assertIn("2025-09-14", note)
        self.assertIn(str(max_age_days_for(SourceKind.MENU)), note)

    def test_the_worst_source_wins(self) -> None:
        note = stale_note(
            assess(_source(SourceKind.MENU, days=100), now=NOW),
            assess(_source(SourceKind.MENU, days=800), now=NOW),
        )

        self.assertIn("800 天", note)


class RenderedStalenessTest(unittest.TestCase):
    """The rendered answer is where this actually matters."""

    def _render(self, *, days: int) -> str:
        # NOW is passed in so the rendered age is exactly `days`: it is derived from
        # the same instant the fixture was built from, not from wall-clock time.
        catalog = MealCatalog(shops=(_shop(days=10),), items=(_item(days=days),))
        start = StartPoint(text="示例大学", precision=StartPrecision.LANDMARK)
        selection = select_meal(catalog, start, MealRequest(max_distance_meters=1500))
        return render_selection(selection, start, now=NOW)

    def test_a_fresh_price_carries_no_age_warning(self) -> None:
        rendered = self._render(days=5)

        self.assertIn("¥18", rendered)
        self.assertNotIn("可能已变", rendered)

    def test_a_stale_price_states_its_age(self) -> None:
        rendered = self._render(days=400)

        self.assertIn("¥18", rendered)
        self.assertIn("可能已变", rendered)
        self.assertIn("400 天", rendered)

    def test_a_stale_price_is_shown_not_withheld(self) -> None:
        """Dropping it would turn a possibly-useful answer into no answer."""
        rendered = self._render(days=400)

        self.assertIn("菜单价：", rendered)
        self.assertNotIn("资料里还没有", rendered)

    def test_the_warning_is_its_own_line(self) -> None:
        rendered = self._render(days=400)

        self.assertTrue(
            any("保鲜期" in line for line in rendered.splitlines()),
            rendered,
        )


if __name__ == "__main__":
    unittest.main()
