"""P2b: bounded, attributed lookups that never invent a place or a price."""

from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import threading
from types import SimpleNamespace

from services.nearby_lookup import (
    LookupBudget,
    NearbyLookup,
    ProviderPage,
    ProviderPlace,
    lookup_for,
    reset_lookup_cache,
)
from services.recommendation_types import StartPoint, StartPrecision


class FakePlaceProvider:
    def __init__(self, places=None, *, raises: BaseException | None = None) -> None:
        self.places = list(places or [])
        self.raises = raises
        self.calls: list[tuple[str, int]] = []

    def search_nearby(self, start, *, limit=5):
        self.calls.append((start.text, limit))
        if self.raises is not None:
            raise self.raises
        return self.places[:limit]


class FakePageProvider:
    def __init__(self, pages=None, *, raises: BaseException | None = None) -> None:
        self.pages = dict(pages or {})
        self.raises = raises
        self.calls: list[str] = []

    def fetch(self, reference):
        self.calls.append(reference)
        if self.raises is not None:
            raise self.raises
        return self.pages.get(reference)


def _landmark(text: str = "示例大学") -> StartPoint:
    return StartPoint(text=text, precision=StartPrecision.LANDMARK)


def _place(provider_id: str = "p1", name: str = "示例店", **overrides) -> ProviderPlace:
    payload = {
        "provider_id": provider_id,
        "name": name,
        "address": "示例路 1 号",
        "distance_meters": 300,
    }
    payload.update(overrides)
    return ProviderPlace(**payload)


class BudgetTest(unittest.TestCase):
    def test_spending_stops_at_the_limit(self) -> None:
        budget = LookupBudget(max_searches=1, max_page_reads=1)

        self.assertTrue(budget.spend_search())
        self.assertFalse(budget.spend_search())
        self.assertEqual(budget.searches_left, 0)
        self.assertTrue(budget.spend_page_read())
        self.assertFalse(budget.spend_page_read())
        self.assertTrue(budget.exhausted)

    def test_negative_budget_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            LookupBudget(max_searches=-1)

    def test_zero_budget_is_immediately_exhausted(self) -> None:
        budget = LookupBudget(max_searches=0, max_page_reads=0)

        self.assertTrue(budget.exhausted)
        self.assertIn("可搜索 0 次", budget.describe())


class FindShopsTest(unittest.TestCase):
    def test_shops_are_converted_with_a_map_source(self) -> None:
        lookup = NearbyLookup(places=FakePlaceProvider([_place()]))

        shops = lookup.find_shops(_landmark())

        self.assertEqual(len(shops), 1)
        self.assertEqual(shops[0].shop_id, "p1")
        self.assertEqual(shops[0].source.kind.value, "map")
        self.assertIn("map:p1", shops[0].source.reference)

    def test_source_reference_carries_the_map_prefix_once(self) -> None:
        lookup = NearbyLookup(places=FakePlaceProvider([_place(provider_id="map:B1")]))

        shops = lookup.find_shops(_landmark())

        self.assertEqual(shops[0].source.reference, "map:B1")

    def test_source_reference_is_prefixed_when_the_id_is_bare(self) -> None:
        lookup = NearbyLookup(places=FakePlaceProvider([_place(provider_id="B1")]))

        shops = lookup.find_shops(_landmark())

        self.assertEqual(shops[0].source.reference, "map:B1")

    def test_coarse_start_point_is_refused_without_calling_the_provider(self) -> None:
        provider = FakePlaceProvider([_place()])
        lookup = NearbyLookup(places=provider)

        shops = lookup.find_shops(StartPoint(text="武汉", precision=StartPrecision.CITY))

        self.assertEqual(shops, ())
        self.assertEqual(provider.calls, [])
        self.assertTrue(any("范围太大" in note for note in lookup.notes))

    def test_a_place_without_a_location_is_dropped(self) -> None:
        nameless_location = ProviderPlace(provider_id="p9", name="无地址店")
        lookup = NearbyLookup(places=FakePlaceProvider([nameless_location]))

        shops = lookup.find_shops(_landmark())

        self.assertEqual(shops, ())

    def test_provider_failure_yields_nothing_and_a_note(self) -> None:
        lookup = NearbyLookup(places=FakePlaceProvider(raises=ConnectionError("down")))

        shops = lookup.find_shops(_landmark())

        self.assertEqual(shops, ())
        self.assertTrue(any("请求失败" in note for note in lookup.notes))

    def test_missing_provider_is_reported(self) -> None:
        lookup = NearbyLookup()

        self.assertEqual(lookup.find_shops(_landmark()), ())
        self.assertTrue(any("没有配置地点服务" in note for note in lookup.notes))

    def test_results_are_cached_within_the_ttl(self) -> None:
        provider = FakePlaceProvider([_place()])
        lookup = NearbyLookup(places=provider)

        lookup.find_shops(_landmark())
        lookup.find_shops(_landmark())

        self.assertEqual(len(provider.calls), 1)

    def test_zero_ttl_disables_the_cache(self) -> None:
        provider = FakePlaceProvider([_place()])
        lookup = NearbyLookup(places=provider, cache_ttl_minutes=0)

        lookup.find_shops(_landmark())
        lookup.find_shops(_landmark())

        self.assertEqual(len(provider.calls), 2)

    def test_exhausted_search_budget_reports_instead_of_fetching(self) -> None:
        provider = FakePlaceProvider([_place()])
        lookup = NearbyLookup(
            places=provider, budget=LookupBudget(max_searches=0, max_page_reads=0)
        )

        shops = lookup.find_shops(_landmark())

        self.assertEqual(shops, ())
        self.assertEqual(provider.calls, [])
        self.assertTrue(any("搜索次数已用完" in note for note in lookup.notes))

    def test_limit_is_passed_to_the_provider(self) -> None:
        provider = FakePlaceProvider([_place("p1"), _place("p2"), _place("p3")])
        lookup = NearbyLookup(places=provider)

        shops = lookup.find_shops(_landmark(), limit=2)

        self.assertEqual(len(shops), 2)
        self.assertEqual(provider.calls[0][1], 2)


class PerTurnStateTest(unittest.TestCase):
    """A reused lookup must not carry one turn's state into the next."""

    def test_begin_turn_resets_notes_and_budget_but_keeps_the_cache(self) -> None:
        provider = FakePlaceProvider([_place()])
        lookup = NearbyLookup(
            places=provider, budget=LookupBudget(max_searches=1, max_page_reads=1)
        )

        lookup.find_shops(_landmark())
        lookup.notes.append("上一轮的说明")
        lookup.find_shops(_landmark())

        lookup.begin_turn()

        self.assertEqual(lookup.notes, [])
        self.assertEqual(lookup.budget.searches_left, 1)
        self.assertIsNone(lookup.resolved_start)
        self.assertEqual(len(provider.calls), 1)

    def test_places_are_cached_per_limit(self) -> None:
        """A "换一家" turn asks for 25; the cached 5 would leave nothing to show."""
        provider = FakePlaceProvider([_place(f"p{index}") for index in range(1, 11)])
        lookup = NearbyLookup(places=provider)

        lookup.find_shops(_landmark(), limit=5)
        shops = lookup.find_shops(_landmark(), limit=25)

        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(len(shops), 10)

    def test_a_successful_geocode_is_cached_and_a_failure_is_not(self) -> None:
        class Geocoder:
            def __init__(self) -> None:
                self.calls = 0

            def geocode(self, text: str):
                self.calls += 1
                if text == "定位不了的地方":
                    raise RuntimeError("read timeout")
                return StartPoint(
                    text=f"{text}（已定位）",
                    precision=StartPrecision.COORDINATE,
                    latitude=30.0,
                    longitude=114.0,
                )

        geocoder = Geocoder()
        lookup = NearbyLookup(places=FakePlaceProvider([_place()]), geocoder=geocoder)

        lookup.find_shops(_landmark("示例大学"))
        lookup.find_shops(_landmark("示例大学"))
        self.assertEqual(geocoder.calls, 1)

        lookup.find_shops(_landmark("定位不了的地方"))
        lookup.find_shops(_landmark("定位不了的地方"))
        self.assertEqual(geocoder.calls, 3)


class LookupReuseTest(unittest.TestCase):
    def tearDown(self) -> None:
        reset_lookup_cache()

    @staticmethod
    def _settings(**overrides):
        payload = {
            "nearby_provider": "amap",
            "nearby_provider_key": "synthetic-key",
            "nearby_provider_city": "",
            "nearby_provider_radius_meters": 1500,
        }
        payload.update(overrides)
        return SimpleNamespace(**payload)

    def test_the_same_configuration_reuses_one_lookup(self) -> None:
        settings = self._settings()

        self.assertIs(lookup_for(settings), lookup_for(settings))

    def test_a_changed_credential_or_radius_is_a_different_lookup(self) -> None:
        first = lookup_for(self._settings())
        second = lookup_for(self._settings(nearby_provider_key="another-key"))
        third = lookup_for(self._settings(nearby_provider_radius_meters=1000))

        self.assertIsNot(first, second)
        self.assertIsNot(first, third)

    def test_reset_drops_every_cached_lookup(self) -> None:
        settings = self._settings()
        before = lookup_for(settings)

        reset_lookup_cache()

        self.assertIsNot(lookup_for(settings), before)

    def test_the_factory_runs_once_per_configuration(self) -> None:
        built: list[NearbyLookup] = []

        def factory(_settings):
            lookup = NearbyLookup()
            built.append(lookup)
            return lookup

        settings = self._settings()
        first = lookup_for(settings, factory=factory)
        second = lookup_for(settings, factory=factory)

        self.assertEqual(len(built), 1)
        self.assertIs(first, second)


class PriceBasisTest(unittest.TestCase):
    def test_per_person_figure_stays_per_person(self) -> None:
        lookup = NearbyLookup()
        place = _place(per_person_cny=25)

        price = lookup.price_from_place(place)

        self.assertIsNotNone(price)
        self.assertEqual(price.basis, "per_person")
        self.assertFalse(price.is_menu_price)
        self.assertIsNone(price.is_within_budget(30))

    def test_place_without_a_per_person_figure_yields_no_price(self) -> None:
        self.assertIsNone(NearbyLookup().price_from_place(_place()))


class FetchPageTest(unittest.TestCase):
    def test_page_is_fetched_once_and_cached(self) -> None:
        provider = FakePageProvider({"menu/1": ProviderPage(url="menu/1", text="菜单")})
        lookup = NearbyLookup(pages=provider)

        first = lookup.fetch_page("menu/1")
        second = lookup.fetch_page("menu/1")

        self.assertIsNotNone(first)
        self.assertEqual(len(provider.calls), 1)
        self.assertIsNotNone(second)

    def test_page_budget_is_bounded(self) -> None:
        provider = FakePageProvider({"a": ProviderPage(url="a"), "b": ProviderPage(url="b")})
        lookup = NearbyLookup(
            pages=provider, budget=LookupBudget(max_searches=2, max_page_reads=1)
        )

        lookup.fetch_page("a")
        second = lookup.fetch_page("b")

        self.assertIsNone(second)
        self.assertEqual(provider.calls, ["a"])
        self.assertTrue(any("页面读取次数已用完" in note for note in lookup.notes))

    def test_page_failure_is_not_a_page(self) -> None:
        lookup = NearbyLookup(pages=FakePageProvider(raises=TimeoutError("slow")))

        self.assertIsNone(lookup.fetch_page("menu/1"))
        self.assertTrue(any("页面读取失败" in note for note in lookup.notes))

    def test_missing_page_provider_is_reported(self) -> None:
        lookup = NearbyLookup()

        self.assertIsNone(lookup.fetch_page("menu/1"))
        self.assertTrue(any("没有配置页面服务" in note for note in lookup.notes))

    def test_empty_reference_is_not_fetched(self) -> None:
        provider = FakePageProvider()
        lookup = NearbyLookup(pages=provider)

        self.assertIsNone(lookup.fetch_page("   "))
        self.assertEqual(provider.calls, [])

    def test_source_is_only_recorded_for_a_fetched_page(self) -> None:
        lookup = NearbyLookup(pages=FakePageProvider({"menu/1": ProviderPage(url="menu/1")}))

        self.assertIsNone(lookup.source_for_page("menu/1"))
        lookup.fetch_page("menu/1")

        source = lookup.source_for_page("menu/1")
        self.assertIsNotNone(source)
        self.assertEqual(source.kind.value, "web")

    def test_items_are_not_invented_from_page_text(self) -> None:
        lookup = NearbyLookup()

        items = lookup.items_from_page(ProviderPage(url="menu/1", text="牛肉面 18 元"), "s1")

        self.assertEqual(items, ())
        self.assertTrue(any("人工核对" in note for note in lookup.notes))

    def test_none_page_yields_no_items(self) -> None:
        self.assertEqual(NearbyLookup().items_from_page(None, "s1"), ())


class DebugStateTest(unittest.TestCase):
    def test_state_reports_cache_and_budget(self) -> None:
        lookup = NearbyLookup(places=FakePlaceProvider([_place()]))

        lookup.find_shops(_landmark())
        state = lookup.debug_state()

        self.assertEqual(state["shops_cached"], 1)
        self.assertIn("可搜索", state["budget"])



class ConcurrentLookupTest(unittest.TestCase):
    def test_turn_budgets_notes_and_origins_are_isolated_while_caches_are_reused(self):
        barrier = threading.Barrier(2)
        calls = []

        class Places:
            def search_nearby(self, start, **options):
                calls.append(start.text)
                barrier.wait(timeout=2)
                return [_place(provider_id=start.text)]

        template = NearbyLookup(places=Places(), budget=LookupBudget(max_searches=1))
        first, second = template.new_turn(), template.new_turn()
        with ThreadPoolExecutor(max_workers=2) as executor:
            a = executor.submit(first.find_shops, _landmark('甲地'))
            b = executor.submit(second.find_shops, _landmark('乙地'))
            self.assertEqual(a.result()[0].shop_id, '甲地')
            self.assertEqual(b.result()[0].shop_id, '乙地')
        self.assertEqual(first.resolved_start.text, '甲地')
        self.assertEqual(second.resolved_start.text, '乙地')
        first.notes.append('只属于甲')
        self.assertEqual(second.notes, [])
        self.assertEqual(first.budget.searches_used, 1)
        self.assertEqual(second.budget.searches_used, 1)
        third = template.new_turn()
        self.assertEqual(third.find_shops(_landmark('甲地'))[0].shop_id, '甲地')
        self.assertEqual(third.budget.searches_used, 0)
        self.assertEqual(len(calls), 2)

    def test_place_page_and_attribution_caches_have_capacity_limits(self):
        lookup = NearbyLookup(
            places=FakePlaceProvider([_place()]),
            pages=FakePageProvider({str(i): ProviderPage(url=str(i)) for i in range(5)}),
            max_cache_entries=2,
        )
        for index in range(5):
            turn = lookup.new_turn()
            turn.find_shops(_landmark(str(index)))
            turn.fetch_page(str(index))
        self.assertEqual(len(lookup._place_cache), 2)
        self.assertEqual(len(lookup._page_cache), 2)
        self.assertEqual(len(lookup._page_refs), 2)
        self.assertIsNone(lookup.source_for_page('0'))
        self.assertIsNotNone(lookup.source_for_page('4'))

    def test_an_unrelated_cache_read_removes_expired_entries_and_sources(self):
        lookup = NearbyLookup(pages=FakePageProvider({'old': ProviderPage(url='old')}))
        lookup.fetch_page('old')
        old_time = datetime.now(timezone.utc) - timedelta(days=1)
        lookup._page_cache['old'] = (old_time, ProviderPage(url='old'))
        lookup.fetch_page('new')
        self.assertNotIn('old', lookup._page_cache)
        self.assertIsNone(lookup.source_for_page('old'))

if __name__ == "__main__":
    unittest.main()
