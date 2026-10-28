"""P0: recommendation and evidence contract, exercised with synthetic data."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from services.recommendation_types import (
    FeedbackKind,
    FlavourNote,
    MealItem,
    MealRecommendation,
    MealState,
    MealStateStore,
    PriceRecord,
    RecommendationFeedback,
    Shop,
    SourceKind,
    SourceRef,
    StartPoint,
    StartPrecision,
)


def _source(kind: SourceKind = SourceKind.MENU, reference: str = "menu-1") -> SourceRef:
    return SourceRef(kind=kind, reference=reference, fetched_at=datetime.now(timezone.utc))


class PriceRecordTest(unittest.TestCase):
    def test_unknown_amount_is_not_zero(self) -> None:
        with self.assertRaises(ValueError):
            PriceRecord(basis="menu", source=_source())

    def test_basis_must_be_known(self) -> None:
        with self.assertRaises(ValueError):
            PriceRecord(basis="guess", source=_source(), amount_cny=10)

    def test_range_minimum_must_not_exceed_maximum(self) -> None:
        with self.assertRaises(ValueError):
            PriceRecord(basis="menu", source=_source(), min_cny=30, max_cny=20)

    def test_negative_amount_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            PriceRecord(basis="menu", source=_source(), amount_cny=-1)

    def test_per_person_estimate_cannot_decide_a_budget(self) -> None:
        """A map per-capita figure must never stand in for a menu price."""
        per_person = PriceRecord(basis="per_person", source=_source(SourceKind.MAP), amount_cny=25)

        self.assertFalse(per_person.is_menu_price)
        self.assertIsNone(per_person.is_within_budget(30))

    def test_menu_price_decides_a_budget(self) -> None:
        menu = PriceRecord(basis="menu", source=_source(), amount_cny=18)

        self.assertTrue(menu.is_menu_price)
        self.assertTrue(menu.is_within_budget(20))
        self.assertFalse(menu.is_within_budget(15))

    def test_unknown_budget_returns_unknown(self) -> None:
        menu = PriceRecord(basis="menu", source=_source(), amount_cny=18)

        self.assertIsNone(menu.is_within_budget(None))

    def test_range_decides_by_its_upper_bound(self) -> None:
        menu = PriceRecord(basis="menu", source=_source(), min_cny=12, max_cny=22)

        self.assertFalse(menu.is_within_budget(20))
        self.assertTrue(menu.is_within_budget(25))

    def test_describe_never_invents_a_zero(self) -> None:
        unknown_range = PriceRecord(basis="menu", source=_source(), min_cny=12)

        self.assertNotIn("0", unknown_range.describe())


class SourceRefTest(unittest.TestCase):
    def test_source_requires_a_reference(self) -> None:
        with self.assertRaises(ValueError):
            SourceRef(kind=SourceKind.MENU, reference="   ", fetched_at=datetime.now(timezone.utc))

    def test_unknown_source_is_not_verifiable(self) -> None:
        unknown = SourceRef(
            kind=SourceKind.UNKNOWN, reference="placeholder", fetched_at=datetime.now(timezone.utc)
        )

        self.assertFalse(unknown.is_verifiable)


class ShopTest(unittest.TestCase):
    def test_shop_requires_name_and_id(self) -> None:
        with self.assertRaises(ValueError):
            Shop(shop_id="", name="兰州拉面", source=_source(SourceKind.MAP))
        with self.assertRaises(ValueError):
            Shop(shop_id="shop-1", name="  ", source=_source(SourceKind.MAP))

    def test_address_alone_counts_as_a_location(self) -> None:
        shop = Shop(shop_id="shop-1", name="兰州拉面", source=_source(SourceKind.MAP), address="学府路 1 号")

        self.assertTrue(shop.has_location)
        self.assertFalse(shop.has_coordinates)

    def test_negative_distance_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Shop(
                shop_id="shop-1",
                name="兰州拉面",
                source=_source(SourceKind.MAP),
                distance_meters=-5,
            )


class MealItemTest(unittest.TestCase):
    def _item(self, **overrides) -> MealItem:
        payload = {
            "item_id": "item-1",
            "shop_id": "shop-1",
            "name": "牛肉拉面",
            "source": _source(),
        }
        payload.update(overrides)
        return MealItem(**payload)

    def test_composition_requires_its_own_source(self) -> None:
        with self.assertRaises(ValueError):
            self._item(composition=("拉面", "牛肉"))

    def test_portion_requires_its_own_source(self) -> None:
        with self.assertRaises(ValueError):
            self._item(portion="大碗")

    def test_composition_and_portion_may_come_from_different_records(self) -> None:
        item = self._item(
            composition=("拉面", "牛肉", "香菜"),
            composition_source=_source(SourceKind.MENU, "menu-1"),
            portion="约 600 克",
            portion_source=_source(SourceKind.REVIEW, "review-9"),
        )

        self.assertEqual(item.kind, "combo")
        self.assertTrue(item.has_composition)
        self.assertTrue(item.has_portion)

    def test_missing_evidence_names_every_gap(self) -> None:
        item = self._item(name="牛肉拉面")

        self.assertEqual(
            item.missing_evidence(),
            ("餐点组成", "份量或规格", "口味描述", "菜单价格"),
        )
        self.assertFalse(item.is_decision_ready())

    def test_generic_flavour_note_does_not_count_as_evidence(self) -> None:
        item = self._item(
            composition=("拉面",),
            composition_source=_source(),
            portion="大碗",
            portion_source=_source(),
            flavours=(FlavourNote(description="好吃", source=_source(SourceKind.REVIEW)),),
            prices=(PriceRecord(basis="menu", source=_source(), amount_cny=18),),
        )

        self.assertFalse(item.has_informative_flavour)
        self.assertIn("口味描述", item.missing_evidence())

    def test_descriptive_flavour_note_counts_as_evidence(self) -> None:
        item = self._item(
            composition=("拉面", "牛肉"),
            composition_source=_source(),
            portion="大碗，约 600 克",
            portion_source=_source(),
            flavours=(
                FlavourNote(
                    description="清汤牛肉汤底",
                    detail="面条偏硬，辣子另给",
                    source=_source(SourceKind.REVIEW),
                ),
            ),
            prices=(PriceRecord(basis="menu", source=_source(), amount_cny=18),),
        )

        self.assertTrue(item.is_decision_ready())
        self.assertEqual(item.missing_evidence(), ())

    def test_per_person_price_does_not_satisfy_the_price_gap(self) -> None:
        item = self._item(prices=(PriceRecord(basis="per_person", source=_source(SourceKind.MAP), amount_cny=25),))

        self.assertEqual(item.menu_prices, ())
        self.assertIn("菜单价格", item.missing_evidence())


class MealRecommendationTest(unittest.TestCase):
    def _shop(self) -> Shop:
        return Shop(shop_id="shop-1", name="兰州拉面", source=_source(SourceKind.MAP))

    def _item(self, shop_id: str = "shop-1") -> MealItem:
        return MealItem(
            item_id="item-1",
            shop_id=shop_id,
            name="牛肉拉面",
            source=_source(),
            composition=("拉面", "牛肉"),
            composition_source=_source(),
            portion="大碗",
            portion_source=_source(),
            flavours=(FlavourNote(description="清汤牛肉汤底，辣子另给", source=_source()),),
            prices=(PriceRecord(basis="menu", source=_source(), amount_cny=18),),
        )

    def test_item_must_belong_to_the_recommended_shop(self) -> None:
        with self.assertRaises(ValueError):
            MealRecommendation(shop=self._shop(), item=self._item(shop_id="shop-2"), source=_source())

    def test_poi_only_recommendation_is_not_grounded(self) -> None:
        recommendation = MealRecommendation(shop=self._shop(), item=None, source=_source(SourceKind.MAP))

        self.assertFalse(recommendation.is_grounded)
        self.assertFalse(recommendation.is_decision_ready)

    def test_grounded_recommendation_needs_a_specific_item(self) -> None:
        recommendation = MealRecommendation(
            shop=self._shop(), item=self._item(), source=_source(SourceKind.MAP)
        )

        self.assertTrue(recommendation.is_grounded)
        self.assertTrue(recommendation.is_decision_ready)

    def test_missing_summary_is_reported_not_hidden(self) -> None:
        item = MealItem(item_id="item-2", shop_id="shop-1", name="凉皮", source=_source())
        recommendation = MealRecommendation(
            shop=self._shop(),
            item=item,
            source=_source(SourceKind.MAP),
            missing=item.missing_evidence(),
        )

        self.assertFalse(recommendation.is_decision_ready)
        self.assertIn("份量或规格", recommendation.describe_missing())

    def test_recommended_is_not_eaten(self) -> None:
        recommended = RecommendationFeedback(kind=FeedbackKind.RECOMMENDED, shop_id="shop-1")
        eaten = RecommendationFeedback(kind=FeedbackKind.EATEN, shop_id="shop-1")

        self.assertFalse(recommended.means_eaten)
        self.assertTrue(eaten.means_eaten)


class StartPointTest(unittest.TestCase):
    def test_city_name_is_not_usable_for_nearby(self) -> None:
        city = StartPoint(text="武汉", precision=StartPrecision.CITY)

        self.assertFalse(city.is_usable_for_nearby)

    def test_landmark_is_usable_for_nearby(self) -> None:
        landmark = StartPoint(text="武汉大学", precision=StartPrecision.LANDMARK)

        self.assertTrue(landmark.is_usable_for_nearby)

    def test_coordinates_compare_by_value(self) -> None:
        first = StartPoint(text="A", precision=StartPrecision.COORDINATE, latitude=30.5, longitude=114.3)
        same = StartPoint(text="B", precision=StartPrecision.COORDINATE, latitude=30.5, longitude=114.3)
        other = StartPoint(text="C", precision=StartPrecision.COORDINATE, latitude=31.0, longitude=114.3)

        self.assertTrue(first.same_place_as(same))
        self.assertFalse(first.same_place_as(other))

    def test_landmark_text_compares_by_text(self) -> None:
        first = StartPoint(text="武汉大学", precision=StartPrecision.LANDMARK)
        same = StartPoint(text="武汉大学", precision=StartPrecision.LANDMARK)
        other = StartPoint(text="华中科技大学", precision=StartPrecision.LANDMARK)

        self.assertTrue(first.same_place_as(same))
        self.assertFalse(first.same_place_as(other))

    def test_empty_text_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            StartPoint(text="  ")


class MealStateTest(unittest.TestCase):
    SESSION = ("private", "qqbot:bot-1:user-a", "qqbot:bot-1:user-a")

    def _state(self) -> MealState:
        return MealState(session_key=self.SESSION)

    def test_budget_and_party_size_update_independently(self) -> None:
        state = self._state().with_budget(30).with_party_size(2)

        self.assertEqual(state.budget_cny, 30)
        self.assertEqual(state.party_size, 2)

    def test_party_size_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            self._state().with_party_size(0)

    def test_changing_start_point_drops_old_candidates(self) -> None:
        state = (
            self._state()
            .with_start(StartPoint(text="武汉大学", precision=StartPrecision.LANDMARK))
            .with_candidates(("shop-1", "shop-2"))
            .with_current("item-1")
        )

        moved = state.with_start(StartPoint(text="华中科技大学", precision=StartPrecision.LANDMARK))

        self.assertEqual(moved.candidates, ())
        self.assertEqual(moved.current_item_id, "")
        self.assertEqual(moved.start.text, "华中科技大学")

    def test_same_start_point_keeps_candidates(self) -> None:
        start = StartPoint(text="武汉大学", precision=StartPrecision.LANDMARK)
        state = self._state().with_start(start).with_candidates(("shop-1",))

        same = state.with_start(StartPoint(text="武汉大学", precision=StartPrecision.LANDMARK))

        self.assertEqual(same.candidates, ("shop-1",))

    def test_exclusion_applies_to_this_decision_only(self) -> None:
        state = self._state().with_candidates(("shop-1", "shop-2")).exclude("shop-1", "不想吃辣")

        self.assertTrue(state.is_excluded("shop-1"))
        self.assertEqual(state.reason_for("shop-1"), "不想吃辣")
        self.assertEqual(state.remaining_candidates(), ("shop-2",))

    def test_excluding_twice_keeps_one_entry(self) -> None:
        state = self._state().exclude("shop-1", "太远").exclude("shop-1", "换理由")

        self.assertEqual(state.excluded, ("shop-1",))
        self.assertEqual(state.reason_for("shop-1"), "太远")

    def test_state_expires(self) -> None:
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        state = MealState(session_key=self.SESSION, valid_until=past)

        self.assertTrue(state.is_expired())

    def test_state_without_deadline_never_expires(self) -> None:
        self.assertFalse(self._state().is_expired())

    def test_updates_do_not_mutate_the_original(self) -> None:
        original = self._state()

        original.with_budget(50)

        self.assertIsNone(original.budget_cny)


class MealStateStoreTest(unittest.TestCase):
    SESSION = ("private", "qqbot:bot-1:user-a", "qqbot:bot-1:user-a")
    OTHER = ("private", "qqbot:bot-1:user-b", "qqbot:bot-1:user-b")

    def test_start_and_read_back(self) -> None:
        store = MealStateStore()

        store.start(self.SESSION, StartPoint(text="武汉大学", precision=StartPrecision.LANDMARK))

        state = store.get(self.SESSION)
        self.assertIsNotNone(state)
        self.assertEqual(state.start.text, "武汉大学")

    def test_sessions_are_isolated(self) -> None:
        store = MealStateStore()
        store.start(self.SESSION, StartPoint(text="武汉大学", precision=StartPrecision.LANDMARK))

        self.assertIsNone(store.get(self.OTHER))

    def test_reset_clears_only_one_session(self) -> None:
        store = MealStateStore()
        store.start(self.SESSION, StartPoint(text="武汉大学", precision=StartPrecision.LANDMARK))
        store.start(self.OTHER, StartPoint(text="华中科技大学", precision=StartPrecision.LANDMARK))

        self.assertTrue(store.reset(self.SESSION))
        self.assertIsNone(store.get(self.SESSION))
        self.assertIsNotNone(store.get(self.OTHER))

    def test_expired_state_is_not_returned(self) -> None:
        store = MealStateStore(ttl_minutes=60)
        state = store.start(self.SESSION, StartPoint(text="武汉大学", precision=StartPrecision.LANDMARK))
        expired = MealState(
            session_key=self.SESSION,
            start=state.start,
            valid_until=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        store._states[self.SESSION] = expired

        self.assertIsNone(store.get(self.SESSION))
        self.assertEqual(len(store), 0)

    def test_prune_removes_expired_states(self) -> None:
        store = MealStateStore()
        store.start(self.SESSION, StartPoint(text="武汉大学", precision=StartPrecision.LANDMARK))
        store._states[self.SESSION] = MealState(
            session_key=self.SESSION,
            valid_until=datetime.now(timezone.utc) - timedelta(seconds=1),
        )

        self.assertEqual(store.prune(), 1)
        self.assertEqual(len(store), 0)

    def test_store_stays_bounded(self) -> None:
        store = MealStateStore(max_sessions=2)
        for index in range(5):
            session = ("private", f"qqbot:bot-1:user-{index}", f"qqbot:bot-1:user-{index}")
            store.start(session, StartPoint(text=f"地点 {index}", precision=StartPrecision.LANDMARK))

        self.assertLessEqual(len(store), 2)

    def test_clear_drops_every_state(self) -> None:
        store = MealStateStore()
        store.start(self.SESSION, StartPoint(text="武汉大学", precision=StartPrecision.LANDMARK))

        store.clear()

        self.assertEqual(len(store), 0)
        self.assertIsNone(store.get(self.SESSION))

    def test_update_refreshes_the_deadline(self) -> None:
        store = MealStateStore(ttl_minutes=60)
        state = store.start(self.SESSION, StartPoint(text="武汉大学", precision=StartPrecision.LANDMARK))

        updated = store.update(state.with_budget(20))

        self.assertIsNotNone(updated.valid_until)
        self.assertEqual(store.get(self.SESSION).budget_cny, 20)


if __name__ == "__main__":
    unittest.main()
