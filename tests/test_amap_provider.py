"""P2b: the Amap mapping, exercised against captured response shapes."""

from __future__ import annotations

import unittest
from urllib.parse import parse_qs, urlparse

from services.amap_provider import (
    AROUND_URL,
    GEOCODE_URL,
    AmapCredentials,
    AmapError,
    AmapGeocoder,
    AmapPlaceProvider,
    check_status,
    parse_geocode,
    parse_pois,
)
from services.recommendation_types import StartPoint, StartPrecision


def _success(pois: list) -> dict:
    return {"status": "1", "info": "OK", "infocode": "10000", "count": str(len(pois)), "pois": pois}


def _poi(**overrides) -> dict:
    payload = {
        "id": "B000A7BD6C",
        "name": "示例兰州拉面",
        "type": "餐饮服务;中餐厅;清真菜馆",
        "address": "示例路 1 号",
        "location": "114.361200,30.541200",
        "distance": "320",
        "biz_ext": {"rating": "4.5", "cost": "25"},
    }
    payload.update(overrides)
    return payload


def _coordinates_start() -> StartPoint:
    return StartPoint(
        text="示例大学",
        precision=StartPrecision.COORDINATE,
        latitude=30.5412,
        longitude=114.3612,
    )


class CredentialsTest(unittest.TestCase):
    def test_empty_key_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            AmapCredentials(key="  ")


class ParsePoisTest(unittest.TestCase):
    def test_poi_becomes_a_provider_place(self) -> None:
        places = parse_pois(_success([_poi()]))

        self.assertEqual(len(places), 1)
        place = places[0]
        self.assertEqual(place.provider_id, "B000A7BD6C")
        self.assertEqual(place.name, "示例兰州拉面")
        self.assertEqual(place.address, "示例路 1 号")
        self.assertEqual(place.distance_meters, 320)

    def test_cost_is_read_as_per_person_and_never_as_a_menu_price(self) -> None:
        place = parse_pois(_success([_poi()]))[0]

        self.assertEqual(place.per_person_cny, 25.0)

    def test_empty_business_block_yields_no_per_person_figure(self) -> None:
        """Amap returns ``biz_ext`` as an empty list when there is no data."""
        place = parse_pois(_success([_poi(biz_ext=[])]))[0]

        self.assertIsNone(place.per_person_cny)

    def test_empty_string_numbers_are_unknown_not_zero(self) -> None:
        place = parse_pois(_success([_poi(distance="", biz_ext={"cost": ""})]))[0]

        self.assertIsNone(place.distance_meters)
        self.assertIsNone(place.per_person_cny)

    def test_map_url_is_built_from_the_coordinates(self) -> None:
        place = parse_pois(_success([_poi()]))[0]

        self.assertIn("uri.amap.com", place.map_url)
        self.assertIn("114.361200%2C30.541200", place.map_url)

    def test_poi_without_coordinates_has_no_map_url(self) -> None:
        place = parse_pois(_success([_poi(location="")]))[0]

        self.assertEqual(place.map_url, "")

    def test_unnamed_poi_is_dropped(self) -> None:
        places = parse_pois(_success([_poi(name="")]))

        self.assertEqual(places, [])

    def test_poi_without_an_id_is_dropped(self) -> None:
        places = parse_pois(_success([_poi(id="")]))

        self.assertEqual(places, [])

    def test_address_returned_as_a_list_is_ignored(self) -> None:
        place = parse_pois(_success([_poi(address=[])]))[0]

        self.assertEqual(place.address, "")

    def test_malformed_payloads_yield_nothing(self) -> None:
        for payload in ({}, {"pois": "nope"}, {"pois": [None, 5]}, []):
            with self.subTest(payload=payload):
                self.assertEqual(list(parse_pois(payload)), [])

    def test_partial_failure_keeps_the_usable_rows(self) -> None:
        places = parse_pois(_success([_poi(), _poi(id="", name="无名"), _poi(id="B2", name="第二家")]))

        self.assertEqual([place.provider_id for place in places], ["B000A7BD6C", "B2"])

    def test_opening_state_is_not_claimed(self) -> None:
        place = parse_pois(_success([_poi()]))[0]

        self.assertIsNone(place.open_now)


class StatusTest(unittest.TestCase):
    def test_success_status_passes(self) -> None:
        check_status({"status": "1"})

    def test_error_status_raises_with_the_reason(self) -> None:
        with self.assertRaises(AmapError) as context:
            check_status({"status": "0", "info": "INVALID_USER_KEY", "infocode": "10001"})

        self.assertIn("INVALID_USER_KEY", str(context.exception))
        self.assertIn("10001", str(context.exception))

    def test_non_object_is_refused(self) -> None:
        with self.assertRaises(AmapError):
            check_status([])


class BuildUrlTest(unittest.TestCase):
    def _provider(self, **overrides) -> AmapPlaceProvider:
        payload = {
            "credentials": AmapCredentials(key="test-key"),
            "http_get": lambda url: _success([]),
        }
        payload.update(overrides)
        return AmapPlaceProvider(**payload)

    def _params(self, url: str) -> dict:
        return {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}

    def test_url_targets_the_around_endpoint(self) -> None:
        url = self._provider().build_url(_coordinates_start())

        self.assertTrue(url.startswith(AROUND_URL))

    def test_location_is_longitude_first(self) -> None:
        params = self._params(self._provider().build_url(_coordinates_start()))

        self.assertEqual(params["location"], "114.361200,30.541200")

    def test_restaurant_types_are_used_when_no_keywords(self) -> None:
        params = self._params(self._provider().build_url(_coordinates_start()))

        self.assertEqual(params["types"], "050000")
        self.assertNotIn("keywords", params)

    def test_keywords_replace_the_type_filter(self) -> None:
        params = self._params(
            self._provider(keywords="米线").build_url(_coordinates_start())
        )

        self.assertEqual(params["keywords"], "米线")
        self.assertNotIn("types", params)

    def test_radius_is_capped(self) -> None:
        provider = self._provider(radius_meters=999999)
        params = self._params(provider.build_url(_coordinates_start()))

        self.assertEqual(params["radius"], "50000")

    def test_offset_is_capped_at_the_service_limit(self) -> None:
        params = self._params(self._provider().build_url(_coordinates_start(), limit=100))

        self.assertEqual(params["offset"], "25")

    def test_extensions_are_requested_for_the_business_block(self) -> None:
        params = self._params(self._provider().build_url(_coordinates_start()))

        self.assertEqual(params["extensions"], "all")
        self.assertEqual(params["sortrule"], "distance")


class SearchTest(unittest.TestCase):
    def test_search_returns_parsed_places(self) -> None:
        calls: list[str] = []

        def http_get(url: str) -> dict:
            calls.append(url)
            return _success([_poi()])

        provider = AmapPlaceProvider(AmapCredentials(key="k"), http_get=http_get)
        places = provider.search_nearby(_coordinates_start(), limit=5)

        self.assertEqual(len(places), 1)
        self.assertEqual(len(calls), 1)

    def test_limit_is_applied_to_the_result(self) -> None:
        provider = AmapPlaceProvider(
            AmapCredentials(key="k"),
            http_get=lambda url: _success([_poi(id="a", name="A"), _poi(id="b", name="B")]),
        )

        places = provider.search_nearby(_coordinates_start(), limit=1)

        self.assertEqual(len(places), 1)

    def test_api_error_is_not_reported_as_an_empty_search(self) -> None:
        provider = AmapPlaceProvider(
            AmapCredentials(key="k"),
            http_get=lambda url: {"status": "0", "info": "INVALID_USER_KEY", "infocode": "10001"},
        )
        with self.assertRaises(AmapError):
            provider.search_nearby(_coordinates_start())

    def test_start_point_without_coordinates_is_refused(self) -> None:
        provider = AmapPlaceProvider(AmapCredentials(key="k"), http_get=lambda url: _success([]))

        with self.assertRaises(AmapError) as context:
            provider.search_nearby(StartPoint(text="示例大学", precision=StartPrecision.LANDMARK))

        self.assertIn("坐标", str(context.exception))

    def test_coarse_start_point_is_refused(self) -> None:
        provider = AmapPlaceProvider(AmapCredentials(key="k"), http_get=lambda url: _success([]))

        with self.assertRaises(AmapError):
            provider.search_nearby(StartPoint(text="武汉", precision=StartPrecision.CITY))

    def test_no_request_is_made_when_the_start_point_is_refused(self) -> None:
        calls: list[str] = []

        def http_get(url: str) -> dict:
            calls.append(url)
            return _success([])

        provider = AmapPlaceProvider(AmapCredentials(key="k"), http_get=http_get)
        with self.assertRaises(AmapError):
            provider.search_nearby(StartPoint(text="武汉", precision=StartPrecision.CITY))

        self.assertEqual(calls, [])


class GeocodeTest(unittest.TestCase):
    def test_geocode_returns_a_coordinate_start_point(self) -> None:
        payload = {
            "status": "1",
            "geocodes": [
                {
                    "formatted_address": "湖北省武汉市洪山区示例大学",
                    "location": "114.361200,30.541200",
                }
            ],
        }

        start = parse_geocode(payload)

        self.assertIsNotNone(start)
        self.assertEqual(start.precision, StartPrecision.COORDINATE)
        self.assertAlmostEqual(start.latitude, 30.5412)
        self.assertAlmostEqual(start.longitude, 114.3612)
        self.assertTrue(start.is_usable_for_nearby)

    def test_empty_results_yield_nothing(self) -> None:
        self.assertIsNone(parse_geocode({"status": "1", "geocodes": []}))
        self.assertIsNone(parse_geocode({"status": "1"}))

    def test_unparseable_location_is_skipped(self) -> None:
        payload = {"status": "1", "geocodes": [{"location": ""}, {"location": "bad"}]}

        self.assertIsNone(parse_geocode(payload))

    def test_a_later_valid_match_is_used(self) -> None:
        payload = {
            "status": "1",
            "geocodes": [
                {"location": ""},
                {"formatted_address": "示例大学", "location": "114.1,30.1"},
            ],
        }

        start = parse_geocode(payload)

        self.assertEqual(start.text, "示例大学")

    def test_geocoder_url_targets_the_geocode_endpoint(self) -> None:
        geocoder = AmapGeocoder(AmapCredentials(key="k"), http_get=lambda url: {})

        url = geocoder.build_url("示例大学")

        self.assertTrue(url.startswith(GEOCODE_URL))
        self.assertIn("address=", url)

    def test_geocoder_rejects_an_empty_address(self) -> None:
        geocoder = AmapGeocoder(AmapCredentials(key="k"), http_get=lambda url: {})

        with self.assertRaises(AmapError):
            geocoder.build_url("   ")

    def test_geocoder_raises_on_an_error_response(self) -> None:
        geocoder = AmapGeocoder(
            AmapCredentials(key="k"),
            http_get=lambda url: {"status": "0", "info": "INVALID_USER_KEY"},
        )

        with self.assertRaises(AmapError):
            geocoder.geocode("示例大学")

    def test_city_is_included_when_configured(self) -> None:
        geocoder = AmapGeocoder(
            AmapCredentials(key="k"), http_get=lambda url: {}, city="武汉"
        )

        self.assertIn("city=", geocoder.build_url("示例大学"))


if __name__ == "__main__":
    unittest.main()
