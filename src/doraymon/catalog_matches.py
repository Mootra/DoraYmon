"""Apply explicit source-entry decisions without merging branches by fuzzy names."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from .catalog import CatalogStore, brand_of, food_group, write_json
from .catalog_rankings import RankingIndex, ranking_observation
from .catalog_sources import scope_area


class BranchDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["match", "import", "hold"]
    shop_id: str | None = None
    basis: str = Field(min_length=1, max_length=1000)
    urls: tuple[str, ...] = ()

    @model_validator(mode="after")
    def exact_target(self):
        if (self.action == "match") != bool(self.shop_id):
            raise ValueError("Only a match decision takes an existing branch ID")
        return self


def apply_matches(folder: Path, decisions: dict) -> dict:
    decisions = TypeAdapter(dict[str, BranchDecision]).validate_python(decisions)
    index_path = folder / "ranking-index.json"
    index = RankingIndex.read(index_path)
    store = CatalogStore(folder / "city-candidates.sqlite3")
    shops = {shop["id"]: shop for shop in store.rows("南昌")}
    entries = {}
    for source in index.sources:
        for entry in source.entries:
            entries.setdefault(entry.id, []).append((source, entry))
    records = []
    checked = datetime.now(UTC)
    for key, decision in decisions.items():
        if key not in entries:
            raise ValueError("Decision does not refer to a captured source entry")
        observations = entries[key]
        source, entry = observations[-1]
        if decision.action == "hold":
            continue
        if entry.in_scope is False:
            raise ValueError("An outside-scope branch cannot enter the current city selection")
        if decision.action == "match":
            if decision.shop_id not in shops:
                raise ValueError("Match target is not an existing Nanchang branch")
            record = dict(shops[decision.shop_id])
            if not record.get("brand") and brand_of(record["display_name"]):
                record["brand"] = brand_of(record["display_name"])
                record["brand_basis"] = "公开门店名包含品牌名"
        else:
            address = entry.address or entry.place_address
            if entry.in_scope is not True or not address:
                raise ValueError("A new branch needs an in-scope published address")
            display_name = (
                entry.place_name
                if entry.place_activity == "not_checked" and entry.place_name
                else entry.name
            )
            category = (
                entry.place_category.rsplit(";", 1)[-1]
                if entry.place_category.startswith("餐饮服务;")
                else "类别待核对"
            )
            record = {
                "source_key": entry.id,
                "display_name": display_name,
                "city": "南昌",
                "listed_location": address,
                "area_name": scope_area(entry.name, address, ""),
                "category": category,
                "food_group": food_group(display_name, category),
                "brand": brand_of(display_name),
                "brand_basis": "公开门店名包含品牌名" if brand_of(display_name) else "",
                "scope_basis": entry.scope_basis,
                "location_status": "listed_address",
                "activity_status": "not_checked",
                "checked_at": source.checked_at.isoformat(),
                "tags": [source.publisher + "推荐"],
            }
        if entry.place_activity != "not_checked":
            record["activity_status"] = entry.place_activity
            record["activity_basis"] = {
                "source_url": entry.place_url,
                "source_name": entry.place_name,
                "checked_at": entry.place_checked_at.isoformat(),
                "published_date": None,
            }
        if entry.id.startswith("amap:") and entry.place_checked_at:
            amap_id = entry.id.removeprefix("amap:")
            if record.get("amap_id") and record["amap_id"] != amap_id:
                raise ValueError("Existing and reviewed map place identities conflict")
            record["amap_id"] = amap_id
        record["sources"] = [
            {
                "id": source.id,
                "publisher": source.publisher,
                "title": source.title,
                "url": source.url,
                "checked_at": source.checked_at.isoformat(),
                "published_date": source.published_date.isoformat()
                if source.published_date
                else None,
                "page_updated_date": source.page_updated_date.isoformat()
                if source.page_updated_date
                else None,
                "edition": source.edition,
                "entry_name": entry.name,
                "entry_address": entry.address,
                "source_order": entry.position,
                "original_rank": entry.rank,
                "recommended_dishes": list(entry.recommended_dishes),
                "access": "public_page",
                "body_sha256": source.body_sha256,
                "place_url": entry.place_url,
                "place_name": entry.place_name,
                "place_address": entry.place_address,
                "place_checked_at": entry.place_checked_at.isoformat()
                if entry.place_checked_at
                else None,
            }
            for source, entry in observations
        ]
        record["rankings"] = [ranking_observation(source, entry) for source, entry in observations]
        record["aliases"] = list(dict.fromkeys([*record.get("aliases", []), entry.name]))
        records.append(record)
    if records:
        store.import_records(records)
    imported = {row["source_key"]: row["id"] for row in store.rows("南昌")}
    for source_index, source in enumerate(index.sources):
        updated = []
        for entry in source.entries:
            decision = decisions.get(entry.id)
            if decision:
                values = {
                    "match_basis": decision.basis,
                    "match_urls": decision.urls,
                    "match_checked_at": checked,
                    "review_status": "hold" if decision.action == "hold" else "matched",
                }
                if decision.action != "hold":
                    values["shop_id"] = (
                        decision.shop_id if decision.action == "match" else imported[entry.id]
                    )
                    values["in_scope"] = True
                    values["scope_basis"] = (
                        entry.scope_basis if entry.in_scope else "已按公开资料匹配到当前主城区分店"
                    )
                else:
                    values["shop_id"] = None
                entry = entry.model_copy(update=values)
            updated.append(entry)
        index.sources[source_index] = source.model_copy(update={"entries": tuple(updated)})
    write_json(index_path, index.model_dump(mode="json"))
    return {
        "matched": sum(d.action == "match" for d in decisions.values()),
        "imported": sum(d.action == "import" for d in decisions.values()),
        "held": sum(d.action == "hold" for d in decisions.values()),
    }
