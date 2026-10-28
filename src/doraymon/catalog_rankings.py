"""Published source lists and their independently reviewable branch matches."""

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RankingEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    address: str = Field(default="", max_length=500)
    position: int = Field(ge=1)
    rank: int | None = Field(default=None, ge=1)
    shop_id: str | None = None
    match_basis: str = ""
    match_urls: tuple[str, ...] = ()
    match_checked_at: datetime | None = None
    review_status: Literal["unreviewed", "matched", "hold"] = "unreviewed"
    in_scope: bool | None = None
    scope_basis: str = ""
    place_url: str = ""
    place_name: str = ""
    place_address: str = ""
    place_category: str = ""
    place_checked_at: datetime | None = None
    place_activity: Literal["not_checked", "source_reports_suspended", "source_reports_closed"] = (
        "not_checked"
    )
    recommended_dishes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def check_match(self):
        if self.shop_id and not self.match_basis:
            raise ValueError("A branch match requires its evidence basis")
        return self


class RankingSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    publisher: str = Field(min_length=1, max_length=100)
    url: str
    kind: Literal["ranked", "curated", "award"]
    checked_at: datetime
    published_date: date | None = None
    page_updated_date: date | None = None
    edition: str = ""
    body_sha256: str = Field(default="", pattern=r"^(?:[0-9a-f]{64})?$")
    declared_total: int | None = Field(default=None, ge=1)
    complete: bool = False
    entries: tuple[RankingEntry, ...] = Field(max_length=2000)

    @field_validator("checked_at")
    @classmethod
    def aware_time(cls, value):
        if value.tzinfo is None:
            raise ValueError("Source checks need a timezone")
        return value

    @field_validator("url")
    @classmethod
    def public_url(cls, value):
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
        ):
            raise ValueError("A public source URL is required")
        return value

    @model_validator(mode="after")
    def unique_entries(self):
        if len({entry.id for entry in self.entries}) != len(self.entries):
            raise ValueError("A source list contains duplicate entry identities")
        if self.complete and self.declared_total != len(self.entries):
            raise ValueError("A complete list must account for every declared entry")
        return self


class RankingCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str
    status: Literal["completed", "partial", "blocked"]
    attempted_at: datetime
    message: str = ""


class RankingIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    sources: list[RankingSource] = Field(default_factory=list)
    checks: dict[str, RankingCheck] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_sources(self):
        if len({source.id for source in self.sources}) != len(self.sources):
            raise ValueError("A ranking index contains duplicate source identities")
        return self

    @classmethod
    def read(cls, path: Path):
        return cls.model_validate_json(path.read_text(encoding="utf-8")) if path.exists() else cls()

    def tasks(self) -> list[dict]:
        result = []
        by_url = {source.url: source for source in self.sources}
        for url in dict.fromkeys([*by_url, *self.checks]):
            source, check = by_url.get(url), self.checks.get(url)
            count = len(source.entries) if source else 0
            pending = max(0, (source.declared_total or count) - count) if source else 0
            result.append(
                {
                    "source_kind": "ranking",
                    "source_url": url,
                    "title": source.title
                    if source
                    else "公开名单 · " + str(urlsplit(url).hostname),
                    "status": check.status
                    if check
                    else "completed"
                    if source.complete
                    else "partial",
                    "imported": count,
                    "pending_entries": pending,
                    "needs_review": 1 if check and check.status == "blocked" else 0,
                    "updated_at": (check.attempted_at if check else source.checked_at).isoformat(),
                    "message": check.message if check else "",
                }
            )
        return result


def entry_id(name: str, address: str) -> str:
    identity = json.dumps([name.strip(), address.strip()], ensure_ascii=False)
    return "published:" + hashlib.sha256(identity.encode()).hexdigest()


def ranking_observation(source: RankingSource, entry: RankingEntry) -> dict:
    return {
        "rankingId": source.id,
        "rankingNum": entry.rank,
        "source_order": entry.position,
        "list_kind": source.kind,
        "list_title": source.title,
        "publisher": source.publisher,
        "source_url": source.url,
        "checked_at": source.checked_at.isoformat(),
        "published_date": source.published_date.isoformat() if source.published_date else None,
        "page_updated_date": source.page_updated_date.isoformat()
        if source.page_updated_date
        else None,
        "edition": source.edition,
        "itemTotalCnt": source.declared_total,
    }


def ranking_coverage(
    shops: list[dict], index: RankingIndex, *, target: int = 100
) -> tuple[dict, list]:
    by_id = {shop["id"]: shop for shop in shops}
    indexed = {source.id for source in index.sources}
    candidates, sources = {}, {}
    for shop in shops:
        observations = [
            row for row in shop.get("rankings") or [] if str(row["rankingId"]) not in indexed
        ]
        if not observations:
            continue
        candidates["shop:" + shop["id"]] = {
            "id": "shop:" + shop["id"],
            "name": shop["display_name"],
            "address": shop.get("listed_location", ""),
            "shop_id": shop["id"],
            "match_basis": "既有门店保留的上榜记录",
            "activity_status": shop.get("activity_status", "not_checked"),
            "rankings": list(observations),
        }
        for observation in observations:
            key = str(observation["rankingId"])
            sources[key] = {
                "id": observation["rankingId"],
                "title": observation.get("list_title") or observation.get("rankingDesc"),
                "publisher": observation.get("publisher")
                or urlsplit(observation.get("source_url", "")).hostname,
                "url": observation.get("source_url"),
                "checked_at": observation.get("checked_at"),
                "published_date": observation.get("published_date"),
                "item_count": observation.get("itemTotalCnt"),
            }
    for source in index.sources:
        sources[source.id] = {
            "id": source.id,
            "title": source.title,
            "publisher": source.publisher,
            "url": source.url,
            "checked_at": source.checked_at.isoformat(),
            "published_date": source.published_date.isoformat() if source.published_date else None,
            "page_updated_date": source.page_updated_date.isoformat()
            if source.page_updated_date
            else None,
            "edition": source.edition,
            "item_count": source.declared_total,
            "captured_count": len(source.entries),
            "complete": source.complete,
        }
        for entry in source.entries:
            if entry.in_scope is not True:
                continue
            matched = entry.shop_id if entry.shop_id in by_id else None
            key = "shop:" + matched if matched else entry.id
            candidate = candidates.setdefault(
                key,
                {
                    "id": key,
                    "name": entry.name,
                    "address": entry.address,
                    "place_address": entry.place_address,
                    "place_name": entry.place_name,
                    "place_url": entry.place_url,
                    "activity_status": by_id[matched].get("activity_status", entry.place_activity)
                    if matched
                    else entry.place_activity,
                    "shop_id": matched,
                    "match_basis": entry.match_basis or "分店尚未匹配",
                    "match_urls": list(entry.match_urls),
                    "match_checked_at": entry.match_checked_at.isoformat()
                    if entry.match_checked_at
                    else None,
                    "review_status": "matched" if matched else entry.review_status,
                    "rankings": [],
                },
            )
            observation = ranking_observation(source, entry)
            candidate["rankings"] = [
                row for row in candidate["rankings"] if str(row["rankingId"]) != source.id
            ] + [observation]

    def priority(candidate):
        observations = candidate["rankings"]
        publishers = {
            str(sources[str(row["rankingId"])].get("publisher") or row["rankingId"])
            for row in observations
        }
        ranks = [row.get("rankingNum") for row in observations if row.get("rankingNum")]
        return (
            -len(publishers),
            -len({str(row["rankingId"]) for row in observations}),
            min(ranks, default=9999),
            candidate["name"],
        )

    ordered = sorted(candidates.values(), key=priority)
    selected = ordered[:target]
    included = sum(candidate["shop_id"] is not None for candidate in selected)
    return {
        "hot_target": target,
        "hot_total": len(selected),
        "hot_included": included,
        "hot_missing": len(selected) - included,
        "hot_pending": max(0, target - len(selected)),
        "hot_missing_before": sum(
            not by_id.get(c["shop_id"], {}).get("baseline") for c in selected
        ),
        "hot_ids": [c["shop_id"] for c in selected if c["shop_id"]],
        "ranking_candidate_total": len(ordered),
        "ranking_missing": sum(c["shop_id"] is None for c in ordered),
        "ranking_sources": list(sources.values()),
        "ranking_scope_pending": sum(e.in_scope is None for s in index.sources for e in s.entries),
    }, ordered


class PublicPlaceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    url: str
    name: str = Field(min_length=1, max_length=200)
    address: str = Field(min_length=1, max_length=500)
    category: str = ""
    readable: Literal[True] = True
    checked_at: datetime

    @model_validator(mode="after")
    def official_identity(self):
        parts = urlsplit(self.url)
        if (
            parts.scheme != "https"
            or parts.hostname != "www.amap.com"
            or parts.query
            or parts.fragment
            or parts.username
            or parts.password
            or not parts.path.startswith("/place/")
            or self.id != "amap:" + parts.path.removeprefix("/place/")
        ):
            raise ValueError("Place evidence must retain the official published place identity")
        if self.checked_at.tzinfo is None:
            raise ValueError("Place checks need a timezone")
        return self
