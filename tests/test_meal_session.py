"""P2c: follow-ups keep what the user did not change."""

from __future__ import annotations

import unittest

from services.meal_session import (
    DEFAULT_RADIUS_METERS,
    INTENT_ANOTHER,
    INTENT_BUDGET,
    INTENT_CHEAPER,
    INTENT_MOVE,
    INTENT_NEARBY,
    INTENT_PARTY,
    MIN_RADIUS_METERS,
    RADIUS_STEP_METERS,
    MealDecisionSession,
    extract_start_point,
    parse_meal_turn,
)
from services.nearby_meal_service import MealCatalog
from services.recommendation_types import MealStateStore
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


def _source(kind: SourceKind = SourceKind.MENU, reference: str = "s") -> SourceRef:
    return SourceRef(kind=kind, reference=reference, fetched_at=datetime.now(timezone.utc))


def _landmark(text: str = "示例大学") -> StartPoint:
    return StartPoint(text=text, precision=StartPrecision.LANDMARK)


def _complete_item(item_id: str, shop_id: str, name: str, price: float) -> MealItem:
    return MealItem(
        item_id=item_id,
        shop_id=shop_id,
        name=name,
        source=_source(),
        composition=("主料", "配菜"),
        composition_source=_source(),
        portion="一份",
        portion_source=_source(),
        flavours=(FlavourNote(description="清汤底，面条偏硬", source=_source()),),
        prices=(PriceRecord(basis="menu", source=_source(), amount_cny=price),),
    )


def _three_shop_catalog() -> MealCatalog:
    """Three complete shops at 300m, 700m and 1200m with rising prices."""
    shops = (
        Shop(shop_id="near", name="近店", source=_source(SourceKind.MAP), distance_meters=300),
        Shop(shop_id="mid", name="中店", source=_source(SourceKind.MAP), distance_meters=700),
        Shop(shop_id="far", name="远店", source=_source(SourceKind.MAP), distance_meters=1200),
    )
    items = (
        _complete_item("i-near", "near", "近店套餐", 14),
        _complete_item("i-mid", "mid", "中店套餐", 20),
        _complete_item("i-far", "far", "远店套餐", 30),
    )
    return MealCatalog(shops=shops, items=items)


class ParseTurnTest(unittest.TestCase):
    def test_nearby_question_words_are_not_locations(self) -> None:
        for text in ("附近有什么吃的", "附近有什么餐馆", "学校附近吃什么", "这边附近吃什么", "我这附近有什么吃的"):
            with self.subTest(text=text):
                self.assertIsNone(extract_start_point(text))

    def test_named_places_do_not_require_a_leading_preposition(self) -> None:
        for text, expected in (
            ("江西师范大学附近", "江西师范大学"),
            ("在示例大学附近吃什么", "示例大学"),
            ("示例广场周边有啥吃的", "示例广场"),
            ("换到示例大学北校区", "示例大学北校区"),
        ):
            with self.subTest(text=text):
                self.assertEqual(extract_start_point(text).text, expected)

    def test_intents_are_recognised(self) -> None:
        cases = {
            "换一家": INTENT_ANOTHER,
            "再近一点": INTENT_NEARBY,
            "便宜点": INTENT_CHEAPER,
            "预算30元": INTENT_BUDGET,
            "两个人": INTENT_PARTY,
            "3个人": INTENT_PARTY,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_meal_turn(text).intent, expected)

    def test_another_shop_wins_over_a_budget_word(self) -> None:
        turn = parse_meal_turn("换一家便宜点的")

        self.assertEqual(turn.intent, INTENT_ANOTHER)

    def test_budget_value_is_extracted(self) -> None:
        self.assertEqual(parse_meal_turn("不超过 25 元").budget_cny, 25.0)
        self.assertEqual(parse_meal_turn("预算30块").budget_cny, 30.0)

    def test_party_size_is_extracted(self) -> None:
        self.assertEqual(parse_meal_turn("3个人").party_size, 3)

    def test_explicit_start_becomes_a_move(self) -> None:
        turn = parse_meal_turn("我换地方了", start=_landmark("另一所大学"))

        self.assertEqual(turn.intent, INTENT_MOVE)
        self.assertEqual(turn.start.text, "另一所大学")

    def test_plain_text_uses_the_default_intent(self) -> None:
        self.assertEqual(parse_meal_turn("随便来点").intent, INTENT_NEARBY)


class SessionFlowTest(unittest.TestCase):
    SESSION = ("private", "qqaccount:1:2", "qqaccount:1:2")

    def setUp(self) -> None:
        # Each test gets its own store so decisions never leak between them.
        self.session = MealDecisionSession(
            catalog=_three_shop_catalog(), store=MealStateStore()
        )

    def test_begin_selects_the_nearest_complete_option(self) -> None:
        outcome = self.session.begin(self.SESSION, _landmark())

        self.assertTrue(outcome.is_usable)
        self.assertEqual(outcome.selection.recommendation.shop.shop_id, "near")

    def test_another_shop_excludes_the_current_one(self) -> None:
        self.session.begin(self.SESSION, _landmark())

        outcome = self.session.handle(self.SESSION, "换一家")

        self.assertTrue(outcome.is_usable)
        self.assertNotEqual(outcome.selection.recommendation.shop.shop_id, "near")
        self.assertIn("near", outcome.state.excluded)

    def test_another_shop_twice_keeps_moving(self) -> None:
        self.session.begin(self.SESSION, _landmark())
        self.session.handle(self.SESSION, "换一家")

        outcome = self.session.handle(self.SESSION, "换一家")

        self.assertTrue(outcome.is_usable)
        self.assertEqual(outcome.selection.recommendation.shop.shop_id, "far")
        self.assertEqual(len(outcome.state.excluded), 2)

    def test_exhausting_the_catalogue_refuses_instead_of_repeating(self) -> None:
        self.session.begin(self.SESSION, _landmark())
        self.session.handle(self.SESSION, "换一家")
        self.session.handle(self.SESSION, "换一家")

        outcome = self.session.handle(self.SESSION, "换一家")

        self.assertFalse(outcome.is_usable)
        self.assertIn("不编造", outcome.selection.refused_reason)

    def test_closer_tightens_the_distance_bound(self) -> None:
        self.session.begin(self.SESSION, _landmark())

        outcome = self.session.handle(self.SESSION, "再近点")

        self.assertLess(outcome.state.radius_meters, DEFAULT_RADIUS_METERS or 10**9)
        self.assertTrue(outcome.is_usable)

    def test_closer_keeps_the_choice_when_nothing_is_nearer(self) -> None:
        """There is nothing closer than the nearest shop, so it must say so."""
        self.session.begin(self.SESSION, _landmark())

        outcome = self.session.handle(self.SESSION, "再近点")

        self.assertTrue(outcome.is_usable)
        self.assertIn("没有更近的选择", outcome.changed)
        # The stated bound must still contain the shop that was recommended.
        self.assertLessEqual(
            outcome.selection.recommendation.shop.distance_meters,
            outcome.state.radius_meters,
        )

    def test_closer_keeps_the_reported_range_consistent_with_the_answer(self) -> None:
        """The stated bound must not contradict the shop that was recommended."""
        self.session.begin(self.SESSION, _landmark())
        outcome = self.session.handle(self.SESSION, "再近点")

        if outcome.is_usable:
            self.assertLessEqual(
                outcome.selection.recommendation.shop.distance_meters,
                outcome.state.radius_meters,
            )

    def test_closer_from_a_wide_bound_still_binds(self) -> None:
        """A fixed step from a wide default can be a no-op; this must not be."""
        shops = (
            Shop(shop_id="v-near", name="稍近店", source=_source(SourceKind.MAP), distance_meters=700),
            Shop(shop_id="far", name="稍远店", source=_source(SourceKind.MAP), distance_meters=800),
        )
        items = (
            _complete_item("i-near", "v-near", "稍近套餐", 14),
            _complete_item("i-far", "far", "稍远套餐", 20),
        )
        session = MealDecisionSession(
            catalog=MealCatalog(shops=shops, items=items), store=MealStateStore()
        )
        session.begin(self.SESSION, _landmark())
        # The wide default bound (1500m) cannot exclude the 800m option.
        started = session.handle(self.SESSION, "预算 50 元")
        self.assertEqual(started.selection.recommendation.shop.shop_id, "far")

        outcome = session.handle(self.SESSION, "再近点")

        self.assertTrue(outcome.is_usable)
        self.assertEqual(outcome.selection.recommendation.shop.shop_id, "v-near")
        self.assertLess(outcome.state.radius_meters, 800)

    def test_closer_reports_when_already_at_the_floor(self) -> None:
        """At the floor the reply must say so instead of pretending to narrow."""
        state = self.session.store.start(self.SESSION, _landmark())
        self.session.store.update(
            state.with_radius(MIN_RADIUS_METERS + RADIUS_STEP_METERS)
        )

        outcome = self.session.handle(self.SESSION, "再近点")

        # The range could not really change, so the reply must not claim it did.
        self.assertNotIn("范围收紧", outcome.changed)
        self.assertTrue(
            "没有更近的选择" in outcome.changed or "已是最小范围" in outcome.changed
        )

    def test_repeated_closer_reaches_the_floor(self) -> None:
        self.session.begin(self.SESSION, _landmark())
        for _ in range(8):
            outcome = self.session.handle(self.SESSION, "再近点")

        self.assertGreaterEqual(outcome.state.radius_meters, MIN_RADIUS_METERS)

    def test_repeated_closer_stops_at_the_minimum(self) -> None:
        self.session.begin(self.SESSION, _landmark())
        for _ in range(10):
            outcome = self.session.handle(self.SESSION, "再近一点")

        self.assertGreaterEqual(outcome.state.radius_meters, MIN_RADIUS_METERS)

    def test_closer_eventually_drops_distant_shops(self) -> None:
        self.session.begin(self.SESSION, _landmark())
        for _ in range(4):
            outcome = self.session.handle(self.SESSION, "再近一点")

        if outcome.is_usable:
            self.assertLessEqual(
                outcome.selection.recommendation.shop.distance_meters,
                outcome.state.radius_meters,
            )

    def test_budget_is_remembered_across_follow_ups(self) -> None:
        self.session.begin(self.SESSION, _landmark())

        budgeted = self.session.handle(self.SESSION, "预算 25 元")
        moved = self.session.handle(self.SESSION, "换一家")

        self.assertEqual(budgeted.state.budget_cny, 25.0)
        # The budget survives an unrelated follow-up and still binds.
        self.assertEqual(moved.state.budget_cny, 25.0)
        self.assertTrue(moved.is_usable)
        self.assertNotEqual(moved.selection.recommendation.shop.shop_id, "far")

    def test_budget_is_still_applied_after_another_shop(self) -> None:
        """Moving on must not silently drop the budget the user set."""
        self.session.begin(self.SESSION, _landmark())
        self.session.handle(self.SESSION, "预算 25 元")

        moved = self.session.handle(self.SESSION, "换一家")

        self.assertEqual(moved.state.budget_cny, 25.0)
        self.assertTrue(moved.is_usable)
        # The 20-yuan mid shop fits 25 yuan, so the budget was applied.
        self.assertEqual(moved.selection.recommendation.shop.shop_id, "mid")

    def test_budget_refuses_when_no_priced_option_fits(self) -> None:
        """Every priced option exceeds the budget, so nothing is recommended."""
        self.session.begin(self.SESSION, _landmark())
        self.session.handle(self.SESSION, "预算 18 元")

        moved = self.session.handle(self.SESSION, "换一家")

        self.assertFalse(moved.is_usable)
        self.assertIn("超过", moved.selection.refused_reason)

    def test_unpriced_option_reports_the_budget_as_unverifiable(self) -> None:
        """When only unknown prices remain, the reply must not imply they fit."""
        catalog = MealCatalog(
            shops=(
                Shop(shop_id="cheap", name="便宜店", source=_source(SourceKind.MAP), distance_meters=100),
                Shop(shop_id="priced", name="贵店", source=_source(SourceKind.MAP), distance_meters=150),
                Shop(shop_id="unknown", name="价格未知的店", source=_source(SourceKind.MAP), distance_meters=300),
            ),
            items=(
                _complete_item("i-cheap", "cheap", "便宜套餐", 14),
                _complete_item("i-priced", "priced", "贵套餐", 88),
                MealItem(item_id="i-unknown", shop_id="unknown", name="套餐", source=_source(SourceKind.MAP)),
            ),
        )
        session = MealDecisionSession(catalog=catalog, store=MealStateStore())
        session.begin(self.SESSION, _landmark())
        session.handle(self.SESSION, "预算 20 元")

        # Drop the affordable option, leaving an over-budget one and an unpriced
        # one, so only the unpriced candidate can be offered.
        moved = session.handle(self.SESSION, "换一家")

        self.assertTrue(moved.is_usable)
        self.assertEqual(moved.selection.recommendation.shop.shop_id, "unknown")
        self.assertIn("无法确认", " ".join(moved.notes))

    def test_cheaper_lowers_the_existing_budget(self) -> None:
        self.session.begin(self.SESSION, _landmark())
        self.session.handle(self.SESSION, "预算 30 元")

        outcome = self.session.handle(self.SESSION, "便宜点")

        self.assertLess(outcome.state.budget_cny, 30.0)
        self.assertIn("预算下调", outcome.changed)

    def test_cheaper_without_a_budget_says_so(self) -> None:
        self.session.begin(self.SESSION, _landmark())

        outcome = self.session.handle(self.SESSION, "便宜点")

        self.assertIn("没有可下调的预算", outcome.changed)

    def test_moving_the_start_point_discards_old_candidates(self) -> None:
        self.session.begin(self.SESSION, _landmark("示例大学"))
        self.session.handle(self.SESSION, "预算 30 元")

        outcome = self.session.handle(
            self.SESSION,
            "换个地方",
            start=_landmark("另一所大学"),
        )

        self.assertEqual(outcome.state.start.text, "另一所大学")
        self.assertEqual(outcome.state.candidates, ())
        self.assertIn("起点", outcome.changed)

    def test_moving_keeps_the_budget_but_resets_distance(self) -> None:
        self.session.begin(self.SESSION, _landmark())
        self.session.handle(self.SESSION, "预算 25 元")
        for _ in range(2):
            self.session.handle(self.SESSION, "再近点")

        outcome = self.session.handle(self.SESSION, "换个地方", start=_landmark("另一所大学"))

        self.assertEqual(outcome.state.budget_cny, 25.0)
        self.assertEqual(outcome.state.radius_meters, DEFAULT_RADIUS_METERS)

    def test_follow_up_without_a_session_starts_one(self) -> None:
        outcome = self.session.handle(self.SESSION, "换一家", start=_landmark())

        self.assertTrue(outcome.is_usable)

    def test_reset_forgets_the_decision(self) -> None:
        self.session.begin(self.SESSION, _landmark())

        self.assertTrue(self.session.reset(self.SESSION))
        self.assertIsNone(self.session.store.get(self.SESSION))

    def test_current_shop_is_tracked_after_selection(self) -> None:
        outcome = self.session.begin(self.SESSION, _landmark())

        self.assertEqual(outcome.state.current_item_id, "i-near")

    def test_outcome_notes_surface_missing_evidence(self) -> None:
        catalog = MealCatalog(
            shops=(Shop(shop_id="s", name="店", source=_source(SourceKind.MAP), distance_meters=100),),
            items=(MealItem(item_id="i", shop_id="s", name="招牌", source=_source(SourceKind.MAP)),),
        )
        session = MealDecisionSession(catalog=catalog, store=MealStateStore())

        outcome = session.begin(self.SESSION, _landmark())

        self.assertTrue(any("还缺" in note for note in outcome.notes))


class IntegrationWithSyntheticCatalogTest(unittest.TestCase):
    SESSION = ("private", "qqaccount:1:2", "qqaccount:1:2")

    def test_synthetic_catalogue_drives_the_flow(self) -> None:
        session = MealDecisionSession(
            catalog=MealCatalog(shops=synthetic_shops(), items=synthetic_items()),
            store=MealStateStore(),
        )

        outcome = session.begin(self.SESSION, _landmark())

        self.assertTrue(outcome.is_usable)
        self.assertEqual(outcome.selection.recommendation.item.item_id, "item-beef-noodle")


if __name__ == "__main__":
    unittest.main()
