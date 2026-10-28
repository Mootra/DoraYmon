import json

import pytest

from doraymon.catalog import CatalogStore, write_json
from doraymon.catalog_consolidation import consolidate
from doraymon.catalog_sources import category_from_listing
from doraymon.meal import CampusCatalog, CampusRequest


def branch(key="ctrip:1", address="合成路1号", city="南昌", **fields):
    return {
        "source_key": key,
        "display_name": "合成门店",
        "city": city,
        "listed_location": address,
        "sources": [{"url": "https://example.com/" + key}],
        **fields,
    }


def test_category_translation_keeps_source_custom_tags_and_refreshes_derived_tags(tmp_path):
    store = CatalogStore(tmp_path / "catalog.sqlite3")
    store.import_records([branch(category="COFFEE SHOP", tags=["聚餐", "来源名单"])])
    first = store.rows()[0]
    assert first["category"] == "咖啡/饮品"
    assert first["source_category"] == "COFFEE SHOP"
    assert set(first["tags"]) == {"聚餐", "来源名单", "咖啡", "饮品"}
    store.import_records([branch(category="Teahouses")])
    updated = store.rows()[0]
    assert updated["id"] == first["id"]
    assert set(updated["tags"]) == {"聚餐", "来源名单", "茶饮", "饮品"}
    assert updated["category_tags"] == ["茶饮", "饮品"]
    store.import_records([branch(category="类别待核对", tags=[], food_group="正餐")])
    assert store.rows()[0]["category"] == "茶饮/饮品"
    assert set(store.rows()[0]["tags"]) == {"聚餐", "来源名单", "茶饮", "饮品"}
    assert store.rows()[0]["food_group"] == "饮品"


@pytest.mark.parametrize("category", ["炸鸡、汉堡", "汉堡/炸鸡，汉堡"])
def test_compound_source_category_produces_reusable_tags(tmp_path, category):
    store = CatalogStore(tmp_path / "catalog.sqlite3")
    store.import_records([branch(category=category)])
    row = store.rows()[0]
    assert row["category"] == category
    assert set(row["category_tags"]) == {"炸鸡", "汉堡"}


@pytest.mark.parametrize(
    "path,category",
    [("cafes-e1/3/", "咖啡/饮品"), ("bakeries-e1/", "甜品/烘焙"), ("", "类别待核对")],
)
def test_directory_classification_keeps_its_captured_evidence(path, category):
    listing = {
        "directory_url": "https://www.trip.com/restaurant/nanchang-175/" + path,
        "directory_checked_at": "2026-10-20T12:00:00+00:00",
    }
    actual, basis = category_from_listing(listing)
    assert actual == category
    if category != "类别待核对":
        assert basis["kind"] == "source_directory"
        assert basis["source_url"] == listing["directory_url"]
        assert basis["checked_at"] == listing["directory_checked_at"]
    listing["cuisineName"] = "Japanese cuisine"
    assert category_from_listing(listing)[0] == "日料"
    listing.pop("cuisineName")
    listing["directory_url"] = listing["directory_url"].replace("www.trip.com", "example.com")
    assert category_from_listing(listing) == ("类别待核对", {})


def test_consolidation_preview_and_apply_preserve_branch_and_collection_facts(tmp_path):
    store = CatalogStore(tmp_path / "city-candidates.sqlite3")
    store.import_records(
        [
            branch(activity_status="source_reports_suspended"),
            branch("ctrip:2", "合成路2号", category="Japanese cuisine"),
            branch("jingzhou", city="荆州"),
        ]
    )
    write_json(
        tmp_path / "source-listings.json",
        [
            {
                "poiId": 1,
                "directory_url": "https://www.trip.com/restaurant/nanchang-175/cafes-e1/",
                "directory_checked_at": "2026-10-20T12:00:00+00:00",
            }
        ],
    )
    snapshot = store.export(tmp_path / "catalog.json")
    snapshot["collection"] = {"state": "partial", "heartbeat": "2026-10-20T12:00:00+00:00"}
    snapshot["collection_control"] = True
    write_json(tmp_path / "catalog.json", snapshot)
    original_db = store.path.read_bytes()
    original_json = (tmp_path / "catalog.json").read_bytes()
    before = store.rows()
    preview = consolidate(tmp_path)
    assert preview["categories_from_directory"] == 1
    assert store.path.read_bytes() == original_db
    assert (tmp_path / "catalog.json").read_bytes() == original_json
    report = consolidate(tmp_path, apply=True)
    assert report["changed"] == 1
    after = store.rows()
    assert after[0]["food_group"] == "饮品"
    assert after[2] == before[2]
    for old, new in zip(before, after, strict=True):
        for key in ("id", "city", "display_name", "listed_location", "activity_status"):
            assert new.get(key) == old.get(key)
        assert all(source in new["sources"] for source in old["sources"])
    directory = after[0]["sources"][-1]
    assert directory["title"] == "咖啡目录"
    assert directory["checked_at"] == "2026-10-20T12:00:00+00:00"
    updated = json.loads((tmp_path / "catalog.json").read_text())
    assert updated["collection"] == snapshot["collection"]
    assert updated["collection_control"] is True
    assert consolidate(tmp_path)["changed"] == 0


def test_translated_city_category_is_available_to_the_chat_filter(tmp_path):
    store = CatalogStore(tmp_path / "catalog.sqlite3")
    store.import_records([branch(category="Japanese cuisine")])
    result = CampusCatalog(store.path).pick(None, CampusRequest(location="南昌", category="日料"))
    assert result.total == 1
    assert result.shops[0].category == "日料"
