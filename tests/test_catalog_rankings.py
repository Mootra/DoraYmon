import json
from datetime import UTC, datetime

import httpx
import pytest

from doraymon import catalog_rankings_sources
from doraymon.catalog import CatalogStore, write_json
from doraymon.catalog_matches import apply_matches
from doraymon.catalog_rankings import RankingEntry, RankingIndex, RankingSource, ranking_coverage
from doraymon.catalog_rankings_sources import (
    AMAP_BASE,
    address_scope,
    apply_place_evidence,
    match_exact,
    parse_amap_list,
    parse_bdb_bbq,
    parse_bdb_specialty,
)

CHECKED = datetime(2026, 10, 20, 12, tzinfo=UTC)


def source(entries, **kwargs):
    return RankingSource(
        id="synthetic-list",
        title="合成公开推荐",
        publisher="合成来源",
        url="https://example.com/list",
        kind="curated",
        checked_at=CHECKED,
        entries=tuple(entries),
        **kwargs,
    )


def entry(key="a", **kwargs):
    return RankingEntry(id=key, name="合成店", position=1, in_scope=True, **kwargs)


def test_unmatched_list_entry_is_a_real_coverage_gap_until_matched(tmp_path):
    store = CatalogStore(tmp_path / "catalog.sqlite3")
    store.import_records(
        [
            {
                "source_key": "synthetic-shop",
                "display_name": "合成店",
                "city": "南昌",
                "listed_location": "合成路1号",
                "sources": [{"id": "directory"}],
            }
        ]
    )
    shop = store.rows()[0]
    index = RankingIndex(
        sources=[source([entry(address="合成路1号"), entry("b", address="合成路2号")])]
    )
    write_json(tmp_path / "ranking-index.json", index.model_dump(mode="json"))
    value = store.export(tmp_path / "catalog.json")
    assert value["coverage"]["hot_total"] == 2
    assert value["coverage"]["hot_included"] == 0
    assert value["coverage"]["hot_missing"] == 2
    assert value["coverage"]["hot_pending"] == 98
    matched = match_exact(index.sources[0], [shop])
    coverage, candidates = ranking_coverage([shop], RankingIndex(sources=[matched]))
    assert coverage["hot_included"] == 1 and coverage["hot_missing"] == 1
    assert len(candidates) == 2
    assert candidates[0]["rankings"][0]["rankingNum"] is None
    assert matched.checked_at == CHECKED


def test_matching_requires_both_full_name_and_full_address():
    original = source([entry(address="合成路1号")])
    shops = [{"id": "wrong-branch", "display_name": "合成店", "listed_location": "合成路2号"}]
    assert match_exact(original, shops).entries[0].shop_id is None
    shops += [{"id": "right-branch", "display_name": "合成店", "listed_location": "合成路1号"}]
    assert match_exact(original, shops).entries[0].shop_id == "right-branch"


def specialty_html(count=50):
    return "<p>2026-06-17 13:35</p>" + "".join(
        f"<p>{i}.合成店{i}</p><p>地址：东湖区合成路{i}号</p><p>（菜品推荐：合成汤、合成菜）</p>"
        for i in range(1, count + 1)
    )


def test_article_responsive_copies_do_not_duplicate_branches_or_invent_rank_and_date():
    article = specialty_html()
    result = parse_bdb_specialty(article + article, CHECKED)
    assert len(result.entries) == 50 and result.complete
    assert result.published_date is None
    assert result.page_updated_date.isoformat() == "2026-06-17"
    assert result.entries[0].rank is None
    assert result.entries[0].position == 1
    assert result.entries[0].recommended_dishes == ("合成汤", "合成菜")
    with pytest.raises(ValueError, match="incomplete"):
        parse_bdb_specialty(specialty_html(49), CHECKED)


def test_bbq_list_keeps_its_original_year_and_rank_when_read_later():
    html = specialty_html(20).replace("2026-06-17", "2023-08-11")
    captured = parse_bdb_bbq(html + html, CHECKED)
    assert captured.edition == "2023" and captured.published_date.isoformat() == "2023-08-11"
    assert captured.checked_at == CHECKED
    assert len(captured.entries) == 20 and captured.entries[0].rank == 1
    assert captured.complete and captured.kind == "ranked"
    with pytest.raises(ValueError, match="incomplete"):
        parse_bdb_bbq(specialty_html(19), CHECKED)


def test_public_amap_excerpt_keeps_declared_total_and_original_edition():
    body = json.dumps(
        {
            "@type": "ItemList",
            "name": "合成2025年度榜单",
            "numberOfItems": 1,
            "itemListElement": [{"position": 1, "item": {"name": "合成店", "address": ""}}],
        },
        ensure_ascii=False,
    )
    html = '<script type="application/ld+json">' + body + "</script>"
    html += '<a class="poi-card" href="/place/SYNTHETIC1">合成店</a>'
    html += "<p>本榜单共有 5 个地点，当前仅展示前 1 个</p>"
    result = parse_amap_list(html, AMAP_BASE, CHECKED)
    assert result.edition == "2025" and result.declared_total == 5
    assert not result.complete and result.entries[0].in_scope is None
    coverage, _ = ranking_coverage([], RankingIndex(sources=[result]))
    assert coverage["hot_total"] == 0 and coverage["ranking_scope_pending"] == 1
    district = parse_amap_list(html, AMAP_BASE + "/360113", CHECKED)
    assert district.entries[0].in_scope is True


def test_source_scope_uses_address_instead_of_a_brand_named_after_an_outside_town():
    assert address_scope("向塘小馆（第一街区店）", "红谷滩区合成路1号")[0] is True
    assert address_scope("合成店", "南昌县莲塘合成路1号")[0] is False
    assert address_scope("合成店（莲塘店）", "")[0] is False


def test_cross_list_matches_count_one_branch_and_source_ids_are_unique():
    shop = {"id": "branch", "display_name": "合成店", "listed_location": "合成路1号"}
    first = source([entry(shop_id="branch", match_basis="合成核对依据")])
    second = first.model_copy(update={"id": "second-list", "publisher": "另一来源"})
    coverage, candidates = ranking_coverage([shop], RankingIndex(sources=[first, second]))
    assert coverage["hot_total"] == coverage["hot_included"] == 1
    assert len(candidates[0]["rankings"]) == 2
    with pytest.raises(ValueError, match="duplicate source"):
        RankingIndex(sources=[first, first])


def test_candidate_limit_does_not_hide_an_unmatched_entry_in_the_full_source_pool():
    shops = [{"id": str(i), "display_name": "合成店"} for i in range(100)]
    captured = [entry(str(i), shop_id=str(i), match_basis="合成完整分店依据") for i in range(100)]
    captured.append(RankingEntry(id="pending", name="最后一店", position=101, in_scope=True))
    coverage, candidates = ranking_coverage(shops, RankingIndex(sources=[source(captured)]))
    assert coverage["hot_included"] == 100 and coverage["hot_missing"] == 0
    assert coverage["ranking_candidate_total"] == len(candidates) == 101
    assert coverage["ranking_missing"] == 1
    assert candidates[-1]["id"] == "pending" and candidates[-1]["shop_id"] is None


def test_one_place_in_several_lists_retains_every_source_when_imported(tmp_path):
    first = source([entry(address="东湖区合成路1号")])
    second = first.model_copy(update={"id": "second-list", "publisher": "另一来源"})
    path = tmp_path / "ranking-index.json"
    write_json(path, RankingIndex(sources=[first, second]).model_dump(mode="json"))
    apply_matches(tmp_path, {"a": {"action": "import", "basis": "合成分店逐项核对"}})
    [shop] = CatalogStore(tmp_path / "city-candidates.sqlite3").rows()
    assert {s["id"] for s in shop["sources"]} == {"synthetic-list", "second-list"}
    assert {r["rankingId"] for r in shop["rankings"]} == {"synthetic-list", "second-list"}
    assert len({e.shop_id for s in RankingIndex.read(path).sources for e in s.entries}) == 1


def test_reviewed_import_retains_source_order_and_a_hold_reopens_the_mapping_gap(tmp_path):
    store = CatalogStore(tmp_path / "city-candidates.sqlite3")
    original = source([entry(address="东湖区合成路1号")])
    write_json(
        tmp_path / "ranking-index.json", RankingIndex(sources=[original]).model_dump(mode="json")
    )
    result = apply_matches(tmp_path, {"a": {"action": "import", "basis": "合成公开分店地址核对"}})
    assert result == {"matched": 0, "imported": 1, "held": 0}
    row = store.rows("南昌")[0]
    assert row["listed_location"] == "东湖区合成路1号"
    assert row["sources"][0]["original_rank"] is None
    assert row["sources"][0]["source_order"] == 1
    assert row["activity_status"] == "not_checked"
    index = RankingIndex.read(tmp_path / "ranking-index.json")
    assert ranking_coverage([row], index)[0]["hot_included"] == 1
    apply_matches(tmp_path, {"a": {"action": "hold", "basis": "两份来源的分店地址出现冲突"}})
    index = RankingIndex.read(tmp_path / "ranking-index.json")
    coverage, candidates = ranking_coverage(store.rows(), index)
    assert coverage["hot_total"] == 1 and coverage["hot_included"] == 0
    assert coverage["hot_missing"] == 1
    assert candidates[0]["review_status"] == "hold"
    assert len(store.rows()) == 1


def test_invalid_batch_decision_leaves_catalog_and_index_untouched(tmp_path):
    original = source([entry(address="东湖区合成路1号"), entry("b", address="东湖区合成路2号")])
    path = tmp_path / "ranking-index.json"
    write_json(path, RankingIndex(sources=[original]).model_dump(mode="json"))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="existing Nanchang"):
        apply_matches(
            tmp_path,
            {
                "a": {"action": "import", "basis": "合成核对"},
                "b": {"action": "match", "shop_id": "missing", "basis": "不存在的目标"},
            },
        )
    assert path.read_bytes() == before
    assert not CatalogStore(tmp_path / "city-candidates.sqlite3").rows()


def test_refetch_preserves_reviewed_mapping_only_while_branch_fields_are_unchanged():
    shop = {"id": "existing", "display_name": "合成分店的另一名称", "listed_location": "合成路1号"}
    previous = source(
        [
            entry(
                address="合成路1号",
                shop_id="existing",
                match_basis="已核对两名称对应同一门牌",
                review_status="matched",
                place_address="东湖区合成路1号",
                place_name="合成分店",
                place_activity="source_reports_suspended",
                place_checked_at=CHECKED,
            )
        ]
    )
    fresh = source([entry(address="合成路1号")])
    preserved = match_exact(fresh, [shop], previous).entries[0]
    assert preserved.shop_id == "existing"
    assert preserved.place_activity == "source_reports_suspended"
    assert preserved.place_checked_at == CHECKED
    changed = source([entry(address="合成路2号")])
    assert match_exact(changed, [shop], previous).entries[0].shop_id is None


def test_place_evidence_keeps_original_list_address_and_suspension_label(tmp_path):
    captured = entry(
        "amap:SYNTHETIC1", address="", place_url="https://www.amap.com/place/SYNTHETIC1"
    )
    original = source([captured])
    path = tmp_path / "ranking-index.json"
    write_json(path, RankingIndex(sources=[original]).model_dump(mode="json"))
    result = apply_place_evidence(
        tmp_path,
        {
            captured.id: {
                "id": captured.id,
                "url": captured.place_url,
                "name": "合成店(暂停营业)",
                "address": "南昌市东湖区合成路1号",
                "category": "餐饮服务;中餐厅",
                "checked_at": CHECKED.isoformat(),
            }
        },
    )
    assert result["suspension_labels"] == 1
    updated = RankingIndex.read(path).sources[0].entries[0]
    assert updated.address == "" and updated.place_address == "南昌市东湖区合成路1号"
    assert updated.place_activity == "source_reports_suspended"
    apply_matches(
        tmp_path, {captured.id: {"action": "import", "basis": "合成店名与具体门牌已核对"}}
    )
    row = CatalogStore(tmp_path / "city-candidates.sqlite3").rows()[0]
    assert row["amap_id"] == "SYNTHETIC1"
    assert row["activity_status"] == "source_reports_suspended"
    assert row["activity_basis"]["source_url"] == captured.place_url


def test_place_evidence_rejects_a_different_official_place_identity(tmp_path):
    captured = entry("amap:SYNTHETIC1", place_url="https://www.amap.com/place/SYNTHETIC1")
    path = tmp_path / "ranking-index.json"
    write_json(path, RankingIndex(sources=[source([captured])]).model_dump(mode="json"))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        apply_place_evidence(
            tmp_path,
            {
                captured.id: {
                    "id": captured.id,
                    "url": "https://www.amap.com/place/SYNTHETIC2",
                    "name": "合成店",
                    "address": "南昌市东湖区合成路1号",
                    "checked_at": CHECKED.isoformat(),
                }
            },
        )
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_blocked_refetch_preserves_source_and_reviewed_mapping(tmp_path, monkeypatch):
    previous = source([entry(shop_id="saved", match_basis="已有核对依据")])
    path = tmp_path / "ranking-index.json"
    write_json(path, RankingIndex(sources=[previous]).model_dump(mode="json"))
    checkpoints = []

    async def checkpoint():
        checkpoints.append("before request")

    async def sleep(_):
        pass

    def respond(request):
        return httpx.Response(403, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(catalog_rankings_sources.httpx, "AsyncClient", lambda **_: client)
    monkeypatch.setattr(catalog_rankings_sources.asyncio, "sleep", sleep)
    assert not await catalog_rankings_sources.collect(
        tmp_path, ("specialty50",), checkpoint=checkpoint
    )
    index = RankingIndex.read(path)
    assert checkpoints == ["before request"]
    assert index.sources[0] == previous
    assert index.checks[catalog_rankings_sources.BDB_URL].status == "blocked"
    catalog = json.loads((tmp_path / "catalog.json").read_text())
    assert catalog["collection"]["state"] == "blocked"
    task = next(
        t for t in catalog["tasks"] if t.get("source_url") == catalog_rankings_sources.BDB_URL
    )
    assert task["needs_review"] == 1
