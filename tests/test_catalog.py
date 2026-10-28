import json

import httpx
import pytest

from doraymon.catalog import CatalogStore, food_group, write_json
from doraymon.catalog_control import CollectionControl
from doraymon.catalog_legacy import legacy_records
from doraymon.catalog_sources import PublicCollector, branch_from_detail, collect


def branch(key="a", address="合成路1号", city="南昌"):
    return {
        "source_key": key,
        "display_name": "合成同名店",
        "city": city,
        "listed_location": address,
        "sources": [
            {
                "id": key,
                "url": "https://example.com/" + key,
                "checked_at": "2026-10-20T12:00:00+00:00",
            }
        ],
    }


def html(state):
    value = {"props": {"pageProps": {"initialState": state}}}
    return '<script id="__NEXT_DATA__">' + json.dumps(value) + "</script>"


def test_branch_identity_keeps_separate_addresses_and_unites_exact_source_identity(tmp_path):
    store = CatalogStore(tmp_path / "catalog.sqlite3")
    store.import_records([branch(), branch("b", "合成路2号"), branch("c", city="荆州")])
    assert len(store.rows()) == 3
    store.import_records([branch("same-location"), branch("a", "合成路更新的1号")])
    assert len(store.rows()) == 3
    first = next(r for r in store.rows("南昌") if r["source_key"] == "a")
    assert len(first["sources"]) == 2


def test_unknown_database_is_preserved(tmp_path):
    import sqlite3

    path = tmp_path / "other.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE unrelated(value TEXT)")
        db.execute("INSERT INTO unrelated VALUES('keep')")
    with pytest.raises(ValueError):
        CatalogStore(path).import_records([branch()])
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM unrelated").fetchone()[0] == "keep"


def test_directory_refresh_preserves_a_separately_reviewed_suspension(tmp_path):
    store = CatalogStore(tmp_path / "catalog.sqlite3")
    record = branch()
    record.update(
        amap_id="SYNTHETIC_MAP_ID",
        activity_status="source_reports_suspended",
        activity_basis={"source_url": "https://example.com/status", "checked_at": "2026-10-20"},
    )
    store.import_records([record])
    store.import_records([{**branch(), "amap_id": None}])
    [refreshed] = store.rows()
    assert refreshed["activity_status"] == "source_reports_suspended"
    assert refreshed["activity_basis"] == record["activity_basis"]
    assert refreshed["amap_id"] == "SYNTHETIC_MAP_ID"


def test_rankings_preserve_individual_provenance_and_do_not_fabricate_top100(tmp_path):
    store = CatalogStore(tmp_path / "catalog.sqlite3")
    a = branch()
    a["rankings"] = [{"rankingId": 1, "rankingNum": 2, "checked_at": "2026-10-20"}]
    store.import_records([a])
    a["rankings"] = [{"rankingId": 2, "rankingNum": 3, "checked_at": "2026-10-21"}]
    store.import_records([a])
    value = store.export(tmp_path / "catalog.json", complete=True)
    assert value["coverage"]["hot_total"] == 1
    assert value["coverage"]["hot_pending"] == 99
    assert len(value["coverage"]["ranking_sources"]) == 2
    assert not value["coverage"]["all_branches_verified"]
    assert {r["checked_at"] for r in value["shops"][0]["rankings"]} == {"2026-10-20", "2026-10-21"}


def test_public_branch_scope_and_source_dishes_do_not_become_map_or_menu_facts():
    listing = {"cuisineName": "Jiangxi cuisine"}
    detail = {
        "poiData": {
            "poiId": 1,
            "cityId": 175,
            "secondName": "合成店（瑶湖店）",
            "poiAddress": "紫阳大道合成1号",
        },
        "recommendFoodData": {"foods": [{"originalName": "合成汤"}]},
    }
    row = branch_from_detail(
        listing, detail, "https://www.trip.com/synthetic", "body", "2026-10-20T12:00:00+00:00"
    )
    assert row["display_name"] == "合成店（瑶湖店）"
    assert row["area_name"] == "高新/瑶湖"
    assert row.get("amap_id") is None
    assert "完整菜单未核实" in row["menu_text"]
    detail["poiData"]["poiAddress"] = "南昌县莲塘合成路1号"
    assert branch_from_detail(listing, detail, "url", "body", "date") is None


def test_native_source_address_is_preferred_and_the_display_address_is_kept():
    detail = {
        "poiData": {
            "poiId": 1,
            "restaurantId": 2,
            "cityId": 175,
            "secondName": "合成店",
            "poiAddress": "1 Synthetic Road",
            "translatedLocalAddress": "东湖区合成路1号",
        }
    }
    row = branch_from_detail({}, detail, "https://www.trip.com/synthetic", "body", "date")
    assert row["listed_location"] == "东湖区合成路1号"
    assert row["sources"][0]["original_address"] == "1 Synthetic Road"
    assert row["sources"][0]["local_address"] == "东湖区合成路1号"
    assert row["sources"][0]["restaurant_id"] == "2"
    detail["poiData"]["translatedLocalAddress"] = "南昌县莲塘合成路1号"
    assert branch_from_detail({}, detail, "url", "body", "date") is None


@pytest.mark.asyncio
async def test_forced_detail_refresh_keeps_reviewed_map_identity_and_archives_cache(tmp_path):
    detail = {
        "poiData": {
            "poiId": 1,
            "cityId": 175,
            "secondName": "合成店",
            "poiAddress": "Synthetic Road",
            "translatedLocalAddress": "东湖区合成路1号",
        }
    }
    calls = []

    def serve(request):
        calls.append(str(request.url))
        return httpx.Response(200, text=html(detail))

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        collector = PublicCollector(tmp_path, client, delay=0)
        collector.cache.mkdir()
        old = branch("ctrip:1", "Synthetic Road")
        old.update(amap_id="SYNTHETIC_MAP_ID", activity_status="source_reports_suspended")
        collector.store.import_records([old])
        path = collector.cache / "branch-1.json"
        write_json(path, {"record": old, "checked_at": "2026-10-20T12:00:00+00:00"})
        old_cache = path.read_bytes()
        write_json(
            tmp_path / "detail-outcomes.json", {"previous": "outside_scope_or_missing_address"}
        )
        await collector.details({"1": {"poiId": 1, "jumpUrl": "/synthetic/"}}, 1, refresh=True)
        [row] = collector.store.rows()
        assert len(calls) == 1 and row["listed_location"] == "东湖区合成路1号"
        assert row["amap_id"] == "SYNTHETIC_MAP_ID"
        assert row["activity_status"] == "source_reports_suspended"
        [snapshot] = (collector.cache / "versions").glob("*.json")
        assert snapshot.read_bytes() == old_cache
        assert json.loads((tmp_path / "detail-outcomes.json").read_text())["previous"] == (
            "outside_scope_or_missing_address"
        )


@pytest.mark.asyncio
async def test_refresh_rejects_unknown_ids_before_writing_or_fetching(tmp_path):
    write_json(tmp_path / "source-listings.json", [{"poiId": 1}])
    write_json(tmp_path / "catalog.json", {"shops": [], "coverage": {}})
    before = (tmp_path / "catalog.json").read_bytes()
    with pytest.raises(ValueError, match="captured directory"):
        await collect(tmp_path, 0, 1, (), refresh_ids=(2,))
    assert not (tmp_path / "city-candidates.sqlite3").exists()
    assert (tmp_path / "catalog.json").read_bytes() == before


def test_drink_category_does_not_match_english_steak():
    assert food_group("Steak House", "Western cuisine") == "正餐"
    assert food_group("蜜雪冰城（合成店）", "Bakery") == "饮品"
    assert food_group("合成咖啡", "Cafes") == "饮品"


def test_area_aliases_share_one_indexed_region_and_keep_the_source_label(tmp_path):
    store = CatalogStore(tmp_path / "catalog.sqlite3")
    row = branch()
    row["area_name"] = "YAOHU UNIVERSITY TOWN/JIANGXI OLYMPIC SPORTS CENTER"
    store.import_records([row])
    [saved] = store.rows()
    assert saved["area_name"] == "高新/瑶湖"
    assert saved["source_area_name"] == row["area_name"]
    import sqlite3

    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT area_name FROM shops").fetchone()[0] == "高新/瑶湖"


def test_drink_counts_leave_the_full_brand_roster_and_missing_total_unverified(tmp_path):
    store = CatalogStore(tmp_path / "catalog.sqlite3")
    row = branch()
    row.update(brand="蜜雪冰城", food_group="饮品", area_name="高新/瑶湖")
    store.import_records([row, {**row, "source_key": "other-city", "city": "荆州"}])
    value = store.export(tmp_path / "catalog.json", complete=True)
    brands = {row["brand"]: row for row in value["coverage"]["drink_brand_coverage"]}
    assert brands["蜜雪冰城"]["imported"] == 1
    assert brands["蜜雪冰城"]["areas"] == {"高新/瑶湖": 1}
    assert brands["蜜雪冰城"]["expected_total"] is None
    assert not brands["蜜雪冰城"]["roster_verified"]
    assert "古茗" in value["coverage"]["drink_brands_without_records"]
    task = next(t for t in value["tasks"] if t["title"] == "饮品分店 · 古茗")
    assert task["status"] == "needs_review" and task["needs_review"] is None


@pytest.mark.asyncio
async def test_directory_resume_keeps_actual_fetch_date_and_repeated_pages_are_pending(tmp_path):
    rows = [
        {
            "poiId": 1,
            "cityId": 175,
            "jumpUrl": "/restaurant/synthetic-1/",
            "rankings": [
                {"rankingId": 1, "rankingNum": 1, "rankingDesc": "No. 1 of Synthetic Restaurants"}
            ],
        }
    ]
    calls = []

    def serve(request):
        calls.append(str(request.url))
        return httpx.Response(200, text=html({"resultList": rows, "pageTotal": 2}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        collector = PublicCollector(tmp_path, client, delay=0)
        collector.store.initialize()
        collector.cache.mkdir()
        write_json(
            collector.cache / "directory-restaurants-1.json",
            {
                "rows": rows,
                "url": "https://www.trip.com/restaurant/nanchang-175/",
                "checked_at": "2026-10-20T12:00:00+00:00",
                "total": 2,
            },
        )
        listings = {}
        assert not await collector.directory("restaurants", 0, listings)
        assert len(calls) == 1
        assert listings["1"]["rankings"][0]["checked_at"] == "2026-10-20T12:00:00+00:00"
        assert collector.tasks[0]["status"] == "blocked"
        write_json(tmp_path / "collection-control.json", {"paused": True})
        assert collector.control_state() == "paused"


@pytest.mark.asyncio
async def test_collection_pause_does_not_start_a_process_and_resume_uses_fixed_service(tmp_path):
    calls = []

    async def run():
        calls.append("fixed collector")
        return True

    control = CollectionControl(tmp_path, runner=run)
    with pytest.raises(ValueError):
        await control.execute("arbitrary shell command")
    assert not calls
    assert (await control.execute("pause"))["ok"]
    assert not calls
    assert json.loads((tmp_path / "collection-control.json").read_text())["paused"]
    assert (await control.execute("resume"))["ok"]
    assert len(calls) == 1
    assert not json.loads((tmp_path / "collection-control.json").read_text())["paused"]


@pytest.mark.asyncio
async def test_published_directory_ends_before_its_total_without_scanning_empty_pages(tmp_path):
    calls = []

    def serve(request):
        calls.append(str(request.url))
        state = (
            {"resultList": [{"poiId": 1, "cityId": 175, "jumpUrl": "/synthetic/"}], "pageTotal": 90}
            if len(calls) == 1
            else {"resultList": [], "isPostEnd": True}
        )
        return httpx.Response(200, text=html(state))

    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
        collector = PublicCollector(tmp_path, client, delay=0)
        collector.store.initialize()
        collector.cache.mkdir()
        assert not await collector.directory("restaurants", 0, {})
        assert len(calls) == 2
        assert collector.tasks[0]["status"] == "partial"
        assert collector.tasks[0]["source_total"] == 90
        assert collector.tasks[0]["pending_entries"] == 89
        assert collector.tasks[0]["needs_review"] == 0


def test_legacy_import_retains_source_date_and_does_not_export_local_archive_paths():
    value = {
        "exported_at": "2026-10-21T12:00:00+00:00",
        "shops": [
            {
                "id": "synthetic",
                "display_name": "合成咖啡",
                "category": "咖啡",
                "sources": [
                    {
                        "source_id": "synthetic-source",
                        "platform": "合成截图",
                        "archive_path": "private-local-file",
                        "received_date": "2026-10-20",
                    }
                ],
            }
        ],
    }
    [row] = legacy_records(value, city="南昌", campus_id="jxnu_yaohu", area_name="高新/瑶湖")
    assert row["sources"][0]["checked_at"] == "2026-10-20"
    assert row["checked_at"] == "2026-10-21T12:00:00+00:00"
    assert "private-local-file" not in json.dumps(row)
    assert row["baseline"] and row["location_status"] == "needs_review"
