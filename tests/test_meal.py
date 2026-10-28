import pytest
from helpers import FakeLookup, make_place
from pydantic import ValidationError

from doraymon.meal import AmapError, MealRequest, MealService, describe, map_message


def service(lookup=None, *, default="江西师范大学瑶湖校区", **kwargs) -> MealService:
    return MealService(lookup, default_location=default, **kwargs)


async def test_first_request_searches_default_location():
    lookup = FakeLookup([make_place(1), make_place(2)])
    outcome = await service(lookup).handle(None, MealRequest())
    assert outcome.kind == "places"
    assert outcome.places == (make_place(1),)
    assert lookup.calls == [("江西师范大学瑶湖校区", 1500)]


async def test_another_excludes_what_was_already_suggested():
    lookup = FakeLookup([make_place(1), make_place(2), make_place(3)])
    meal = service(lookup)
    first = await meal.handle(None, MealRequest())
    second = await meal.handle(first.state, MealRequest(change="another"))
    third = await meal.handle(second.state, MealRequest(change="another"))
    assert [o.places[0].id for o in (first, second, third)] == ["p1", "p2", "p3"]
    assert third.state.excluded == {"p1", "p2"}


async def test_closer_shrinks_radius_but_never_below_minimum():
    lookup = FakeLookup([make_place(1)])
    meal = service(lookup, radius=900)
    outcome = await meal.handle(None, MealRequest())
    for _ in range(3):
        outcome = await meal.handle(outcome.state, MealRequest(change="closer"))
    assert [radius for _, radius in lookup.calls] == [900, 400, 200, 200]


async def test_new_location_starts_a_fresh_decision():
    lookup = FakeLookup([make_place(1), make_place(2)])
    meal = service(lookup)
    first = await meal.handle(None, MealRequest(budget_yuan=15))
    first = await meal.handle(first.state, MealRequest(change="another"))
    moved = await meal.handle(first.state, MealRequest(location="南昌站"))
    assert moved.state.location == "南昌站"
    assert moved.state.excluded == frozenset()
    assert moved.state.budget_yuan is None
    assert moved.places == (make_place(1),)


async def test_repeating_the_same_location_keeps_the_decision():
    lookup = FakeLookup([make_place(1), make_place(2)])
    meal = service(lookup)
    first = await meal.handle(None, MealRequest(location="南昌站", budget_yuan=20))
    again = await meal.handle(first.state, MealRequest(location="南昌站", change="another"))
    assert again.places == (make_place(2),)
    assert again.state.budget_yuan == 20


async def test_count_lists_several_places():
    lookup = FakeLookup([make_place(n) for n in range(1, 8)])
    outcome = await service(lookup).handle(None, MealRequest(count=3))
    assert [p.id for p in outcome.places] == ["p1", "p2", "p3"]


async def test_map_needs_an_earlier_suggestion_and_does_not_query_again():
    lookup = FakeLookup([make_place(1)])
    meal = service(lookup)
    early = await meal.handle(None, MealRequest(change="map"))
    assert early.kind == "need_search"
    found = await meal.handle(None, MealRequest())
    mapped = await meal.handle(found.state, MealRequest(change="map"))
    assert mapped.kind == "map"
    assert mapped.places == (make_place(1),)
    assert len(lookup.calls) == 1


async def test_without_a_lookup_it_reports_unavailable():
    outcome = await service(None).handle(None, MealRequest())
    assert (outcome.kind, outcome.detail) == ("unavailable", "key_missing")
    assert "不要凭记忆" in describe(outcome)


async def test_lookup_errors_become_unavailable_with_their_code():
    lookup = FakeLookup(error=AmapError("location_ambiguous"))
    outcome = await service(lookup).handle(None, MealRequest(location="中山路"))
    assert (outcome.kind, outcome.detail) == ("unavailable", "location_ambiguous")
    assert "补充城市" in describe(outcome)


async def test_empty_when_every_candidate_was_excluded():
    lookup = FakeLookup([make_place(1)])
    meal = service(lookup)
    first = await meal.handle(None, MealRequest())
    second = await meal.handle(first.state, MealRequest(change="another"))
    assert second.kind == "empty"
    assert "没有查到其他餐饮店" in describe(second)


async def test_no_default_location_asks_for_one():
    outcome = await service(FakeLookup(), default="").handle(None, MealRequest())
    assert outcome.kind == "need_location"


async def test_description_keeps_facts_budget_and_missing_items():
    lookup = FakeLookup([make_place(1, distance=141), make_place(2, distance=None)])
    outcome = await service(lookup).handle(None, MealRequest(count=2, budget_yuan=15))
    text = describe(outcome)
    assert "合成餐馆1" in text and "合成路1号" in text and "直线约 141 米" in text
    assert "合成餐馆2" in text and "合成路2号" in text and "距离未知" in text
    assert "用户预算 15 元" in text and "只有对应菜单价证据" in text
    assert "菜单、现价、营业状态和步行距离都没有核实" in text


def test_map_message_has_name_address_and_link():
    message = map_message(make_place(1))
    assert message.splitlines()[:2] == ["合成餐馆1", "合成路1号"]
    assert "https://uri.amap.com/marker?position=116.02,28.67" in message


async def test_requirements_persist_until_explicitly_updated_or_cleared():
    meal = service(FakeLookup([make_place(1), make_place(2)]))
    first = await meal.handle(None, MealRequest(requirements="少辣，适合聊天"))
    again = await meal.handle(first.state, MealRequest(change="another"))
    assert again.state.requirements == "少辣，适合聊天"
    cleared = await meal.handle(again.state, MealRequest(requirements=""))
    assert cleared.state.requirements == ""


async def test_cancel_discards_state_without_another_lookup():
    lookup = FakeLookup([make_place(1)])
    meal = service(lookup)
    first = await meal.handle(None, MealRequest())
    stopped = await meal.handle(first.state, MealRequest(change="cancel"))
    assert stopped.kind == "cancelled" and stopped.state is None
    assert len(lookup.calls) == 1


@pytest.mark.parametrize(
    "fields",
    [
        {"count": 0},
        {"count": 6},
        {"budget_yuan": 0},
        {"change": "farther"},
        {"location": "长" * 61},
    ],
)
def test_requests_reject_values_the_tool_does_not_offer(fields):
    with pytest.raises(ValidationError):
        MealRequest(**fields)
