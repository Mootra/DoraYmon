"""Anonymous published-list collection; unmatched branches remain explicit review work."""

import argparse
import asyncio
import hashlib
import json
import os
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import httpx
from pydantic import TypeAdapter

from .catalog import CatalogStore, normalized, write_json
from .catalog_control import CollectionControl
from .catalog_rankings import (
    PublicPlaceEvidence,
    RankingCheck,
    RankingEntry,
    RankingIndex,
    RankingSource,
    entry_id,
)
from .catalog_sources import OUTSIDE_TERMS

BDB_URL = "https://nc.bendibao.com/meishi/202426/85069.shtm"
BDB_BBQ_URL = "https://nc.bendibao.com/meishi/2023811/82099.shtm"
AMAP_BASE = "https://www.amap.com/ranking/nanchang/food"
AMAP_DISTRICTS = ("", "360102", "360103", "360104", "360111", "360112", "360113")


class PublicPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.documents = []
        self.place_links = []
        self._skip = 0
        self._ld = False
        self._script_parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style", "head"}:
            self._skip += 1
        if tag == "script":
            self._ld = attrs.get("type") == "application/ld+json"
            self._script_parts = []
        if tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")
        if tag == "a" and "poi-card" in attrs.get("class", "").split():
            href = attrs.get("href", "")
            if re.fullmatch(r"/place/[A-Za-z0-9]+", href):
                self.place_links.append(href)

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"}:
            self._skip = max(0, self._skip - 1)
        if tag == "script" and self._ld:
            self.documents.append(json.loads("".join(self._script_parts)))
            self._ld = False
        if tag in {"p", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._ld:
            self._script_parts.append(data)
        if not self._skip:
            self.parts.append(data)

    @property
    def lines(self):
        return [line.strip() for line in "".join(self.parts).splitlines() if line.strip()]


def address_scope(name: str, address: str) -> tuple[bool | None, str]:
    boundary_text = address or "".join(re.findall(r"[（(]([^）)]+)[）)]", name))
    if any(term in boundary_text for term in OUTSIDE_TERMS):
        return False, "来源名称或地址明确位于当前南昌主城区范围之外"
    if not address:
        return None, "来源未给出可核对的分店地址"
    return True, "南昌专页列示地址；未出现已明确排除的外围地区，边界仍可复核"


def _bdb_entries(html: str, count: int) -> tuple[tuple[RankingEntry, ...], str | None]:
    page = PublicPage()
    page.feed(html)
    found = {}
    lines = page.lines
    for i, line in enumerate(lines):
        match = re.fullmatch(r"(\d+)[.．、]\s*(.+)", line)
        if not match or not 1 <= int(match[1]) <= count:
            continue
        position, name = int(match[1]), match[2].strip()
        following = lines[i + 1 : i + 4]
        address = next(
            (re.sub(r"^地址[：:]\s*", "", x) for x in following if re.match(r"^地址[：:]", x)), ""
        )
        if not address:
            continue
        dishes = next((x for x in following if "菜品推荐：" in x), "")
        dishes = re.sub(r"^.*?菜品推荐[：:]", "", dishes).rstrip("）)")
        in_scope, basis = address_scope(name, address)
        entry = RankingEntry(
            id=entry_id(name, address),
            name=name,
            address=address,
            position=position,
            in_scope=in_scope,
            scope_basis=basis,
            recommended_dishes=tuple(filter(None, re.split(r"[、，,]", dishes))),
        )
        if position in found and found[position] != entry:
            raise ValueError("Conflicting copies of the published list")
        found[position] = entry
    if set(found) != set(range(1, count + 1)):
        raise ValueError(f"The {count}-branch article is incomplete or its layout changed")
    stamp = next(
        (
            re.search(r"\b(20\d\d-\d\d-\d\d)\s+\d\d:\d\d", line)
            for line in lines
            if re.search(r"\b20\d\d-\d\d-\d\d\s+\d\d:\d\d", line)
        ),
        None,
    )
    return tuple(found[i] for i in range(1, count + 1)), stamp[1] if stamp else None


def parse_bdb_specialty(html: str, checked_at: datetime) -> RankingSource:
    entries, stamp = _bdb_entries(html, 50)
    return RankingSource(
        id="bdb:specialty50:85069",
        title="南昌赣菜美食餐饮门店推荐（50家特色菜门店）",
        publisher="南昌本地宝",
        url=BDB_URL,
        kind="curated",
        checked_at=checked_at,
        page_updated_date=stamp,
        declared_total=50,
        complete=True,
        entries=entries,
    )


def parse_bdb_bbq(html: str, checked_at: datetime) -> RankingSource:
    entries, stamp = _bdb_entries(html, 20)
    return RankingSource(
        id="bdb:bbq-popularity20:82099",
        title="2023南昌热门烧烤门店人气榜（前20家）",
        publisher="南昌本地宝",
        url=BDB_BBQ_URL,
        kind="ranked",
        checked_at=checked_at,
        published_date="2023-08-11",
        page_updated_date=stamp,
        edition="2023",
        declared_total=20,
        complete=True,
        entries=tuple(entry.model_copy(update={"rank": entry.position}) for entry in entries),
    )


def parse_amap_list(html: str, url: str, checked_at: datetime) -> RankingSource:
    page = PublicPage()
    page.feed(html)
    lists = [
        doc for doc in page.documents if isinstance(doc, dict) and doc.get("@type") == "ItemList"
    ]
    if len(lists) != 1:
        raise ValueError("No single published Amap list found")
    data = lists[0]
    entries = data.get("itemListElement", [])
    if not entries or len(entries) != len(page.place_links):
        raise ValueError("Published list and place links do not match")
    rows = []
    for item, href in zip(entries, page.place_links, strict=True):
        place = item["item"]
        name, address = place["name"], place.get("address", "")
        in_scope, basis = address_scope(name, address)
        if url != AMAP_BASE and in_scope is None:
            in_scope, basis = True, "来源明确列在当前范围内的城区分榜，分店地址仍待补充"
        rows.append(
            RankingEntry(
                id="amap:" + href.rsplit("/", 1)[-1],
                name=name,
                address=address,
                position=item["position"],
                in_scope=in_scope,
                scope_basis=basis,
                place_url=urljoin("https://www.amap.com", href),
            )
        )
    declared = re.search(r"本榜单共有\s*(\d+)\s*个地点", " ".join(page.lines))
    total = int(declared[1]) if declared else data.get("numberOfItems")
    return RankingSource(
        id="amap:nanchang:food:" + url.removeprefix(AMAP_BASE).strip("/"),
        title=data["name"],
        publisher="高德地图",
        url=url,
        kind="award",
        checked_at=checked_at,
        edition="2025" if "2025" in data["name"] else "",
        declared_total=total,
        complete=len(rows) == total,
        entries=tuple(rows),
    )


def match_exact(
    source: RankingSource, shops: list[dict], previous: RankingSource | None = None
) -> RankingSource:
    rows = []
    reviewed = {entry.id: entry for entry in previous.entries} if previous else {}
    shop_ids = {shop["id"] for shop in shops}
    for entry in source.entries:
        old = reviewed.get(entry.id)
        unchanged = old and old.name == entry.name and old.address == entry.address
        if unchanged and entry.in_scope is not False and old.place_address:
            entry = entry.model_copy(
                update={
                    name: getattr(old, name)
                    for name in (
                        "place_name",
                        "place_address",
                        "place_category",
                        "place_checked_at",
                        "place_activity",
                        "in_scope",
                        "scope_basis",
                    )
                }
            )
        if (
            unchanged
            and entry.in_scope is not False
            and (old.review_status == "hold" or old.shop_id in shop_ids)
        ):
            rows.append(
                entry.model_copy(
                    update={
                        "shop_id": old.shop_id,
                        "match_basis": old.match_basis,
                        "match_urls": old.match_urls,
                        "match_checked_at": old.match_checked_at,
                        "review_status": old.review_status,
                        "in_scope": old.in_scope,
                        "scope_basis": old.scope_basis,
                    }
                )
            )
            continue
        candidates = [
            shop
            for shop in shops
            if entry.address
            and normalized(shop["display_name"]) == normalized(entry.name)
            and normalized(shop.get("listed_location", "")) == normalized(entry.address)
        ]
        if len(candidates) == 1:
            entry = entry.model_copy(
                update={
                    "shop_id": candidates[0]["id"],
                    "match_basis": "完整店名和完整地址均与已存分店一致",
                    "review_status": "matched",
                }
            )
        rows.append(entry)
    return source.model_copy(update={"entries": tuple(rows)})


def apply_place_evidence(folder: Path, values: dict) -> dict:
    facts = TypeAdapter(dict[str, PublicPlaceEvidence]).validate_python(values)
    path = folder / "ranking-index.json"
    index = RankingIndex.read(path)
    count = 0
    suspended = set()
    for i, source in enumerate(index.sources):
        updated = []
        for entry in source.entries:
            fact = facts.get(entry.id)
            if fact:
                if fact.id != entry.id or fact.url != entry.place_url:
                    raise ValueError("Public evidence does not match the captured place identity")
                in_scope, basis = address_scope(fact.name, fact.address)
                if "南昌" not in fact.address:
                    in_scope, basis = None, "公开地点地址未明确南昌归属，需继续核对"
                status = "not_checked"
                if "暂停营业" in fact.name:
                    status = "source_reports_suspended"
                    suspended.add(entry.id)
                elif any(label in fact.name for label in ("已停业", "已关闭", "永久关闭")):
                    status = "source_reports_closed"
                entry = entry.model_copy(
                    update={
                        "place_name": fact.name,
                        "place_address": fact.address,
                        "place_category": fact.category,
                        "place_checked_at": fact.checked_at,
                        "place_activity": status,
                        "in_scope": in_scope,
                        "scope_basis": "高德公开门店页所列地址；" + basis,
                    }
                )
                count += 1
            updated.append(entry)
        index.sources[i] = source.model_copy(update={"entries": tuple(updated)})
    write_json(path, index.model_dump(mode="json"))
    return {"updated_entries": count, "suspension_labels": len(suspended)}


def save_source(folder: Path, source: RankingSource | None, url: str, error: str = "") -> None:
    path = folder / "ranking-index.json"
    index = RankingIndex.read(path)
    if source:
        index.sources = [row for row in index.sources if row.id != source.id] + [source]
    index.checks[url] = RankingCheck(
        url=url,
        status="blocked" if error else "completed" if source.complete else "partial",
        attempted_at=datetime.now(UTC),
        message=error,
    )
    write_json(path, index.model_dump(mode="json"))


def publish(folder: Path, *, state: str | None = None) -> None:
    previous = (
        json.loads((folder / "catalog.json").read_text(encoding="utf-8"))
        if (folder / "catalog.json").exists()
        else {}
    )
    store = CatalogStore(folder / "city-candidates.sqlite3")
    value = store.export(
        folder / "catalog.json",
        previous.get("tasks", []),
        complete=previous.get("coverage", {}).get("source_directory_complete", False),
    )
    for key in ("collection", "collection_control"):
        if key in previous:
            value[key] = previous[key]
    if state:
        value["collection_control"] = True
        value["collection"] = {
            "state": state,
            "pid": os.getpid(),
            "heartbeat": datetime.now(UTC).isoformat(),
        }
    write_json(folder / "catalog.json", value)


async def collect(
    folder: Path,
    kinds: tuple[str, ...],
    *,
    checkpoint: Callable[[], Awaitable[None]] | None = None,
) -> bool:
    await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
    cache = folder / "public-rankings"
    await asyncio.to_thread(cache.mkdir, exist_ok=True)
    store = CatalogStore(folder / "city-candidates.sqlite3")
    shops = await asyncio.to_thread(store.rows, "南昌")
    requests = []
    if "specialty50" in kinds:
        requests.append(("specialty50", BDB_URL))
    if "bbq20" in kinds:
        requests.append(("bbq20", BDB_BBQ_URL))
    if "amap" in kinds:
        requests.extend(
            ("amap", AMAP_BASE + ("/" + code if code else "")) for code in AMAP_DISTRICTS
        )
    successful = True
    control = CollectionControl(folder)
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        for kind, url in requests:
            if checkpoint:
                await checkpoint()
            else:
                while await asyncio.to_thread(control.is_paused):
                    await asyncio.to_thread(publish, folder, state="paused")
                    await asyncio.sleep(1)
            await asyncio.to_thread(publish, folder, state="running")
            try:
                response = await client.get(url)
                response.raise_for_status()
                if response.url.host not in {"nc.bendibao.com", "www.amap.com"}:
                    raise ValueError("Public source redirected to a different host")
                checked = datetime.now(UTC)
                if kind == "amap":
                    source = parse_amap_list(response.text, url, checked)
                elif kind == "bbq20":
                    source = parse_bdb_bbq(response.text, checked)
                else:
                    source = parse_bdb_specialty(response.text, checked)
                source = source.model_copy(
                    update={"body_sha256": hashlib.sha256(response.content).hexdigest()}
                )
                previous = await asyncio.to_thread(RankingIndex.read, folder / "ranking-index.json")
                previous_source = next(
                    (row for row in previous.sources if row.id == source.id), None
                )
                source = match_exact(source, shops, previous_source)
                successful = successful and source.complete
                key = hashlib.sha256(url.encode()).hexdigest() + "-" + source.body_sha256
                await asyncio.to_thread((cache / (key + ".html")).write_bytes, response.content)
                await asyncio.to_thread(
                    write_json, cache / (key + ".json"), source.model_dump(mode="json")
                )
                await asyncio.to_thread(save_source, folder, source, url)
                print(
                    json.dumps(
                        {
                            "source": source.id,
                            "entries": len(source.entries),
                            "matched": sum(e.shop_id is not None for e in source.entries),
                            "scope_pending": sum(e.in_scope is None for e in source.entries),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            except (httpx.HTTPError, ValueError) as error:
                successful = False
                message = (
                    f"公开页面读取失败（HTTP {error.response.status_code}）。"
                    if isinstance(error, httpx.HTTPStatusError)
                    else "公开页面读取或解析未完成，已保留上次来源资料。"
                )
                await asyncio.to_thread(save_source, folder, None, url, message)
                print(
                    json.dumps({"url": url, "status": "blocked", "reason": type(error).__name__}),
                    flush=True,
                )
            await asyncio.to_thread(publish, folder)
            await asyncio.sleep(0.75)
    tasks = RankingIndex.read(folder / "ranking-index.json").tasks()
    state = (
        "blocked"
        if any(t["status"] == "blocked" for t in tasks)
        else ("completed" if successful else "partial")
    )
    await asyncio.to_thread(publish, folder, state=state)
    return successful


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument(
        "--decisions",
        type=Path,
        help="Apply explicit branch review decisions without fetching sources",
    )
    parser.add_argument(
        "--sources",
        choices=("specialty50", "amap", "bbq20"),
        nargs="+",
        default=["specialty50", "amap", "bbq20"],
    )
    parser.add_argument(
        "--place-evidence", type=Path, help="Apply captured official public place facts"
    )
    args = parser.parse_args()
    if args.decisions and args.place_evidence:
        parser.error("Choose source decisions or place evidence for one operation")
    import fcntl

    args.folder.mkdir(parents=True, exist_ok=True)
    with (args.folder / "collection.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("This catalog already has a running collector.") from None
        if args.decisions:
            from .catalog_matches import apply_matches

            result = apply_matches(
                args.folder, json.loads(args.decisions.read_text(encoding="utf-8"))
            )
            publish(args.folder)
            print(json.dumps(result, ensure_ascii=False))
        elif args.place_evidence:
            payload = json.loads(args.place_evidence.read_text(encoding="utf-8"))
            print(
                json.dumps(apply_place_evidence(args.folder, payload["places"]), ensure_ascii=False)
            )
            publish(args.folder)
        else:
            raise SystemExit(0 if asyncio.run(collect(args.folder, tuple(args.sources))) else 1)


if __name__ == "__main__":
    main()
