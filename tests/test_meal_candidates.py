import sqlite3

import pytest
from helpers import make_campus_catalog

from doraymon.catalog import CatalogStore
from doraymon.meal import CampusCatalog, CampusRequest, describe_campus
from doraymon.meal.candidates import CandidateError


def test_scope_and_category_groups_keep_unknown_locations_and_partial_names_out(tmp_path):
    catalog = make_campus_catalog(tmp_path)
    original = catalog.path.read_bytes()
    result = catalog.pick(None, CampusRequest(campus_scope="校内", category="粉面", count=3))
    assert {shop.id for shop in result.shops} == {"a", "b"}
    assert result.total == 2
    assert catalog.path.read_bytes() == original
    unrestricted = catalog.pick(None, CampusRequest(category="粉面", count=3))
    assert {shop.id for shop in unrestricted.shops} == {"a", "b", "g"}
    assert "归属待确认" in describe_campus(unrestricted)


def test_follow_up_keeps_filters_and_draws_each_choice_once_until_reset(tmp_path):
    catalog = make_campus_catalog(tmp_path)
    first = catalog.pick(None, CampusRequest(campus_scope="校内", category="粉面"))
    second = catalog.pick(first.state, CampusRequest(change="another"))
    assert first.shops[0].id != second.shops[0].id
    exhausted = catalog.pick(second.state, CampusRequest(change="another"))
    assert exhausted.total == 2 and exhausted.shops == ()
    assert "都选过了" in describe_campus(exhausted)
    restarted = catalog.pick(exhausted.state, CampusRequest(change="reset"))
    assert len(restarted.shops) == 1 and restarted.state.category == "粉面"


def test_changed_filters_start_a_decision_and_empty_category_clears_the_filter(tmp_path):
    catalog = make_campus_catalog(tmp_path)
    first = catalog.pick(None, CampusRequest(campus_scope="校内", category="咖啡"))
    changed = catalog.pick(first.state, CampusRequest(campus_scope="校外"))
    assert [shop.id for shop in changed.shops] == ["f"]
    broadened = catalog.pick(changed.state, CampusRequest(category="", count=3))
    assert {shop.id for shop in broadened.shops} == {"e", "f"}


def test_unmatched_category_is_not_silently_broadened(tmp_path):
    result = make_campus_catalog(tmp_path).pick(
        None, CampusRequest(campus_scope="校外", category="%' OR 1=1--")
    )
    assert result.total == 0 and result.shops == ()
    assert "没有符合" in describe_campus(result)


def test_missing_catalog_is_not_created_and_unknown_schema_is_preserved(tmp_path):
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(CandidateError, match="catalog_unavailable"):
        CampusCatalog(missing).pick(None, CampusRequest())
    assert not missing.exists()
    other = tmp_path / "other.sqlite3"
    with sqlite3.connect(other) as connection:
        connection.execute("CREATE TABLE preserved(value TEXT)")
        connection.execute("INSERT INTO preserved VALUES('keep')")
    original = other.read_bytes()
    with pytest.raises(CandidateError, match="unsupported_catalog"):
        CampusCatalog(other).pick(None, CampusRequest())
    assert other.read_bytes() == original


def city_catalog(tmp_path):
    store = CatalogStore(tmp_path / "cities.sqlite3")
    records = []
    for key, city, area, campus in (
        ("yaohu", "南昌", "高新/瑶湖", "jxnu_yaohu"),
        ("honggutan", "南昌", "红谷滩", ""),
        ("yangtze", "荆州", "长江大学周边", "yangtze_east"),
    ):
        records.append(
            {
                "source_key": key,
                "display_name": "合成咖啡（" + key + "）",
                "city": city,
                "area_name": area,
                "campus_id": campus,
                "category": "咖啡/饮品",
                "campus_scope": "校外",
                "listed_location": "合成路1号",
                "sources": [{"url": "https://example.com/" + key}],
            }
        )
    store.import_records(records)
    return CampusCatalog(store.path)


def test_city_campus_and_area_never_mix_and_switching_clears_seen_ids(tmp_path):
    catalog = city_catalog(tmp_path)
    initial = catalog.pick(None, CampusRequest(category="饮品", count=3))
    assert len(initial.shops) == 1 and initial.shops[0].city == "南昌"
    assert initial.shops[0].area_name == "高新/瑶湖"
    other = catalog.pick(initial.state, CampusRequest(location="长大东校区"))
    assert len(other.shops) == 1 and other.shops[0].city == "荆州"
    assert initial.shops[0].id not in other.state.seen
    exhausted = catalog.pick(other.state, CampusRequest(change="another"))
    assert exhausted.state.location == "长江大学东校区" and not exhausted.shops
    city = catalog.pick(exhausted.state, CampusRequest(location="南昌", count=3))
    assert len(city.shops) == 2 and all(s.city == "南昌" for s in city.shops)
    area = catalog.pick(city.state, CampusRequest(location="红谷滩区"))
    assert len(area.shops) == 1 and area.shops[0].area_name == "红谷滩"


def test_ambiguous_campus_requires_clarification_and_legacy_other_city_returns_empty(tmp_path):
    catalog = city_catalog(tmp_path)
    with pytest.raises(CandidateError, match="ambiguous_campus"):
        catalog.pick(None, CampusRequest(location="长江大学"))
    established = catalog.pick(None, CampusRequest(location="长江大学东校区"))
    assert catalog.pick(established.state, CampusRequest(location="长江大学")).state.location == (
        "长江大学东校区"
    )
    legacy = make_campus_catalog(tmp_path)
    assert not legacy.pick(None, CampusRequest(location="长江大学东校区")).shops


def test_source_reported_suspension_and_closure_are_retained_but_not_drawn(tmp_path):
    store = CatalogStore(tmp_path / "availability.sqlite3")
    for i, status in enumerate(
        ("not_checked", "source_reports_suspended", "source_reports_closed")
    ):
        store.import_records(
            [
                {
                    "source_key": f"synthetic-{i}",
                    "display_name": f"合成店{i}",
                    "city": "南昌",
                    "listed_location": f"合成路{i}号",
                    "activity_status": status,
                    "sources": [{"id": f"source-{i}", "url": "https://example.com/branches"}],
                }
            ]
        )
    result = CampusCatalog(store.path).pick(None, CampusRequest(location="南昌", count=3))
    assert result.total == 1
    assert [shop.display_name for shop in result.shops] == ["合成店0"]
    assert len(store.rows()) == 3
