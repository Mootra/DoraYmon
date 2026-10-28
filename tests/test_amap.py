import aiohttp
import pytest

from doraymon.meal import AmapClient, AmapError
from doraymon.meal.amap import BASE_URL, FOOD_TYPES

KEY = "secret-amap-key"
GEOCODE_OK = {"status": "1", "geocodes": [{"location": "116.02,28.67"}]}


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        return None

    async def json(self, content_type=None):
        return self.payload


class FakeHttp:
    def __init__(self, **responses):
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None):
        endpoint = url.removeprefix(BASE_URL).replace("/", "_")
        self.calls.append((endpoint, params))
        response = self.responses[endpoint]
        if isinstance(response, Exception):
            raise response
        return FakeResponse(response)


def poi(number, distance):
    return {
        "id": f"B0{number}",
        "name": f"合成餐馆{number}",
        "address": f"合成路{number}号" if number % 2 else [],
        "distance": distance,
        "location": "116.03,28.68",
    }


async def test_nearby_parses_pois_and_drops_those_outside_the_radius():
    around = {"status": "1", "pois": [poi(1, "141"), poi(2, "900"), poi(3, "2000"), poi(4, "")]}
    http = FakeHttp(geocode_geo=GEOCODE_OK, place_around=around)
    places = await AmapClient(http, KEY).nearby("江西师范大学瑶湖校区", 1500)
    assert [(p.id, p.distance, p.address) for p in places] == [
        ("B01", 141, "合成路1号"),
        ("B02", 900, ""),
    ]
    _, params = http.calls[1]
    assert params["types"] == FOOD_TYPES
    assert params["radius"] == "1500"


async def test_results_are_cached_per_location_and_radius():
    http = FakeHttp(geocode_geo=GEOCODE_OK, place_around={"status": "1", "pois": []})
    client = AmapClient(http, KEY)
    await client.nearby("南昌站", 1500)
    await client.nearby("南昌站", 1500)
    await client.nearby("南昌站", 1000)
    assert [endpoint for endpoint, _ in http.calls].count("geocode_geo") == 2


@pytest.mark.parametrize(
    ("geocode", "code"),
    [
        ({"status": "1", "geocodes": []}, "location_not_found"),
        (
            {"status": "1", "geocodes": [{"location": "1,1"}, {"location": "2,2"}]},
            "location_ambiguous",
        ),
        ({"status": "0", "info": "INVALID_USER_KEY"}, "request_failed"),
    ],
)
async def test_geocode_problems_have_stable_codes(geocode, code):
    with pytest.raises(AmapError) as caught:
        await AmapClient(FakeHttp(geocode_geo=geocode), KEY).nearby("中山路", 1500)
    assert caught.value.code == code


async def test_network_errors_do_not_leak_the_key():
    failure = aiohttp.ClientConnectionError(f"{BASE_URL}geocode/geo?key={KEY}")
    with pytest.raises(AmapError) as caught:
        await AmapClient(FakeHttp(geocode_geo=failure), KEY).nearby("南昌站", 1500)
    assert caught.value.code == "network"
    assert KEY not in str(caught.value)


def test_empty_key_is_rejected():
    with pytest.raises(ValueError):
        AmapClient(FakeHttp(), "")


async def test_amap_additional_fields_remain_platform_references():
    item = {
        **poi(1, "141"),
        "cityname": "合成市",
        "type": "餐饮服务;中餐厅",
        "tag": "面食,清淡",
        "biz_ext": {"rating": "4.2", "cost": "25"},
    }
    http = FakeHttp(geocode_geo=GEOCODE_OK, place_around={"status": "1", "pois": [item]})
    [place] = await AmapClient(http, KEY).nearby("合成起点", 1500)
    assert http.calls[1][1]["extensions"] == "all"
    assert place.city == "合成市" and place.category == "餐饮服务;中餐厅"
    assert place.tags == ("面食", "清淡") and place.rating == "4.2"
    assert place.average_cost_yuan == 25


@pytest.mark.parametrize("cost", [[], "暂无", "NaN", "-1"])
async def test_missing_or_invalid_reference_cost_does_not_break_lookup(cost):
    item = {**poi(1, "141"), "tag": [], "biz_ext": {"cost": cost}}
    http = FakeHttp(geocode_geo=GEOCODE_OK, place_around={"status": "1", "pois": [item]})
    [place] = await AmapClient(http, KEY).nearby("合成起点", 1500)
    assert place.average_cost_yuan is None and place.tags == ()
