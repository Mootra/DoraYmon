"""Resumable anonymous public-directory collection, separate from the chat backend."""

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import time
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from .catalog import (
    CatalogStore,
    brand_of,
    canonical_area,
    canonical_category,
    food_group,
    merge_rankings,
    write_json,
)


class NextData(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "__NEXT_DATA__":
            self.active = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)


def next_state(html: str) -> dict:
    parser = NextData()
    parser.feed(html)
    data = json.loads("".join(parser.parts))
    return data["props"]["pageProps"]["initialState"]


OUTSIDE_TERMS = (
    "安义",
    "进贤",
    "南昌县",
    "莲塘",
    "梅岭",
    "湾里",
    "昌北机场",
    "昌北国际机场",
    "小蓝",
    "向塘",
    "象湖新城",
    "厚田",
    "流湖",
    "松湖",
    "石埠",
    "西山镇",
    "乐化",
)
DIRECTORIES = {"restaurants": "", "cafes": "cafes-e1/", "bakeries": "bakeries-e1/"}
DIRECTORY_NAMES = {"restaurants": "餐饮", "cafes": "咖啡", "bakeries": "烘焙"}


def category_from_listing(listing: dict) -> tuple[str, dict]:
    value = str(listing.get("cuisineName") or "").strip()
    url = str(listing.get("directory_url") or "")
    basis = {"source_url": url, "checked_at": listing.get("directory_checked_at", "")}
    if value:
        return canonical_category(value), {**basis, "kind": "source_category", "value": value}
    parts = urlsplit(url)
    if (
        parts.scheme == "https"
        and parts.netloc == "www.trip.com"
        and not parts.query
        and not parts.fragment
        and re.fullmatch(r"/restaurant/nanchang-175/(cafes-e1|bakeries-e1)/(?:\d+/)?", parts.path)
    ):
        kind = "cafes" if "/cafes-e1/" in parts.path else "bakeries"
        category = "咖啡/饮品" if kind == "cafes" else "甜品/烘焙"
        return category, {**basis, "kind": "source_directory", "directory": kind}
    return "类别待核对", {}


def scope_area(name: str, address: str, zone: str) -> str:
    if "瑶湖" in name + address or "紫阳大道" in address or "天祥大道" in address:
        return "高新/瑶湖"
    for district in ("红谷滩", "东湖区", "西湖区", "青山湖", "青云谱", "高新区", "新建区"):
        if district in address:
            return district
    return canonical_area(zone)


def branch_from_detail(
    listing: dict, detail: dict, url: str, html: str, checked: str
) -> dict | None:
    poi = detail.get("poiData") or {}
    original_address = str(poi.get("poiAddress") or "").strip()
    local_address = str(poi.get("translatedLocalAddress") or "").strip()
    address = local_address or original_address
    if str(poi.get("cityId")) != "175":
        return None
    zone = str(poi.get("zoneName") or listing.get("zoneName") or "")
    if any(word in zone.lower() for word in ("airport", "meiling")) or any(
        word in address for word in OUTSIDE_TERMS
    ):
        return None
    name = str(poi.get("secondName") or poi.get("poiName") or listing.get("poiName") or "")
    if not name.strip() or not address:
        return None
    category, category_basis = category_from_listing(listing)
    source = {
        "id": hashlib.sha256(url.encode()).hexdigest(),
        "publisher": "Trip.com / 携程",
        "title": name,
        "url": url,
        "published_date": None,
        "checked_at": checked,
        "access": "public_page",
        "body_sha256": hashlib.sha256(html.encode()).hexdigest(),
        "original_address": original_address,
        "local_address": local_address,
        "poi_id": str(poi["poiId"]),
        "restaurant_id": str(poi.get("restaurantId") or ""),
    }
    foods = (detail.get("recommendFoodData") or {}).get("foods") or []
    dishes = list(
        dict.fromkeys(
            str(food.get("originalName") or "")
            for food in foods
            if isinstance(food, dict) and food.get("originalName")
        )
    )
    rankings = []
    for original in listing.get("rankings") or []:
        ranking = dict(original)
        ranking.setdefault("source_url", listing.get("directory_url", url))
        ranking.setdefault("checked_at", listing.get("directory_checked_at", checked))
        ranking.setdefault("published_date", None)
        rankings.append(ranking)
    return {
        "source_key": "ctrip:" + str(poi["poiId"]),
        "display_name": name,
        "aliases": [str(poi.get("poiName") or ""), str(poi.get("englishName") or "")],
        "city": "南昌",
        "area_name": scope_area(name, address, zone),
        "listed_location": address,
        "category": category,
        "source_category": str(listing.get("cuisineName") or ""),
        "category_basis": category_basis,
        "food_group": food_group(name, category),
        "brand": brand_of(name),
        "brand_basis": "公开门店名包含品牌名" if brand_of(name) else "",
        "location_status": "listed_address",
        "sources": [source],
        "checked_at": checked,
        "scope_basis": "来源标注南昌；剔除资料明确列出的外围地区，地址与城区边界仍可复核",
        "rankings": rankings,
        "recommended_dishes": dishes,
        "menu_text": "来源推荐菜（完整菜单未核实）：" + "、".join(dishes[:20]) if dishes else "",
    }


class PublicCollector:
    def __init__(self, folder: Path, client: httpx.AsyncClient, *, delay: float = 0.45):
        self.folder, self.client, self.delay = folder, client, delay
        self.store = CatalogStore(folder / "city-candidates.sqlite3")
        self.cache = folder / "public-pages"
        self.tasks: list[dict] = []
        self._request_lock = asyncio.Lock()
        self._last_request = 0.0

    def control_state(self) -> str:
        from .catalog_control import CollectionControl

        return "paused" if CollectionControl(self.folder).is_paused() else "running"

    def publish(self, *, complete: bool = False, state: str = "running") -> dict:
        value = self.store.export(self.folder / "catalog.json", self.tasks, complete=complete)
        value["collection_control"] = True
        value["collection"] = {
            "state": self.control_state() if state == "running" else state,
            "pid": os.getpid(),
            "heartbeat": datetime.now(UTC).isoformat(),
        }
        write_json(self.folder / "catalog.json", value)
        return value

    async def checkpoint(self) -> None:
        while self.control_state() == "paused":
            self.publish(state="paused")
            await asyncio.sleep(1)

    async def fetch(self, url: str) -> tuple[str, str]:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname != "www.trip.com":
            raise ValueError("Unpublished source host")
        for attempt in range(3):
            await self.checkpoint()
            async with self._request_lock:
                wait = self.delay - (time.monotonic() - self._last_request)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_request = time.monotonic()
            try:
                response = await self.client.get(url)
                response.raise_for_status()
                if response.url.host != "www.trip.com":
                    raise ValueError("Source redirected outside the public directory")
                return response.text, datetime.now(UTC).isoformat()
            except httpx.HTTPError:
                if attempt == 2:
                    raise
                await asyncio.sleep(2 ** (attempt + 1))
        raise RuntimeError("Public fetch did not finish")

    async def directory(self, kind: str, pages: int, listings: dict) -> bool:
        base = "https://www.trip.com/restaurant/nanchang-175/" + DIRECTORIES[kind]
        page, limit, successful, signatures = 1, pages or 100, True, set()
        task = {
            "title": f"公开目录 · {DIRECTORY_NAMES[kind]}",
            "source_kind": kind,
            "status": "running",
            "imported": 0,
            "needs_review": 0,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        self.tasks.append(task)
        while page <= limit:
            await self.checkpoint()
            url = base + (f"{page}/" if page > 1 else "")
            path = self.cache / f"directory-{kind}-{page}.json"
            try:
                if path.exists():
                    cached = json.loads(path.read_text(encoding="utf-8"))
                else:
                    html, checked = await self.fetch(url)
                    state = next_state(html)
                    rows = state["resultList"]
                    if rows == [] and state.get("isPostEnd") is True:
                        break
                    if not isinstance(rows, list) or not rows:
                        raise ValueError("No published directory rows")
                    fields = (
                        "poiId",
                        "cityId",
                        "poiName",
                        "jumpUrl",
                        "zoneName",
                        "cuisineName",
                        "rankings",
                        "rangeNum",
                    )
                    cached = {
                        "rows": [{k: r[k] for k in fields if k in r} for r in rows],
                        "checked_at": checked,
                        "total": state.get("pageTotal"),
                        "url": url,
                    }
                    write_json(path, cached)
                rows = cached["rows"]
                if page == 1:
                    task["source_total"] = int(cached.get("total") or len(rows))
                if page == 1 and not pages:
                    limit = min(
                        100, max(1, math.ceil(int(cached.get("total") or len(rows)) / len(rows)))
                    )
                signature = tuple(str(row.get("poiId")) for row in rows)
                if signature in signatures:
                    task["needs_review"] += 1
                    successful = False
                    break
                signatures.add(signature)
                for raw in rows:
                    if str(raw.get("cityId")) != "175" or not raw.get("jumpUrl"):
                        continue
                    row = {
                        **raw,
                        "directory_url": cached["url"],
                        "directory_checked_at": cached["checked_at"],
                    }
                    row["rankings"] = [
                        {
                            **ranking,
                            "source_url": cached["url"],
                            "checked_at": cached["checked_at"],
                            "published_date": None,
                            "list_title": re.sub(
                                r"^(?:No\. \d+ of |Selected for )",
                                "",
                                ranking.get("rankingDesc", ""),
                            ),
                        }
                        for ranking in row.get("rankings", [])
                    ]
                    key = str(row["poiId"])
                    previous = listings.get(key, {})
                    row["rankings"] = merge_rankings(
                        previous.get("rankings", []), row.get("rankings", [])
                    )
                    listings[key] = row
                task["imported"] += len(rows)
                task["updated_at"] = datetime.now(UTC).isoformat()
                print(f"{kind} page {page}/{limit}; unique entries {len(listings)}", flush=True)
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                successful = False
                task["needs_review"] += 1
                print(f"{kind} page {page}: needs review", flush=True)
            self.publish()
            page += 1
        pending = max(0, task.get("source_total", 0) - task["imported"])
        task["pending_entries"] = pending
        task["status"] = "blocked" if not successful else "partial" if pending else "completed"
        if successful and pending:
            task["message"] = (
                f"来源列出 {task['source_total']} 条，公开页面可读取 {task['imported']} 条；"
                f"其余 {pending} 条尚未核对。"
            )
        successful = successful and not pending
        return successful

    async def details(self, listings: dict, concurrency: int, *, refresh: bool = False) -> None:
        task = {
            "title": "指定门店来源重读" if refresh else "门店地址与来源核对",
            "source_kind": "details_refresh" if refresh else "details",
            "status": "running",
            "imported": 0,
            "needs_review": 0,
            "excluded": 0,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        self.tasks.append(task)
        checked = 0
        semaphore = asyncio.Semaphore(concurrency)
        outcome_path = self.folder / "detail-outcomes.json"
        outcomes: dict[str, str] = (
            json.loads(outcome_path.read_text(encoding="utf-8"))
            if refresh and outcome_path.exists()
            else {}
        )

        async def read_detail(listing):
            nonlocal checked
            async with semaphore:
                await self.checkpoint()
                key = str(listing["poiId"])
                path = self.cache / f"branch-{key}.json"
                try:
                    if path.exists() and not refresh:
                        saved = json.loads(path.read_text(encoding="utf-8"))
                        record = saved.get("record")
                    else:
                        url = urljoin("https://www.trip.com", listing["jumpUrl"])
                        html, fetched = await self.fetch(url)
                        record = branch_from_detail(listing, next_state(html), url, html, fetched)
                        if path.exists():
                            old = path.read_bytes()
                            archive = self.cache / "versions"
                            archive.mkdir(exist_ok=True)
                            snapshot = (
                                archive / f"branch-{key}-{hashlib.sha256(old).hexdigest()}.json"
                            )
                            snapshot.write_bytes(old)
                        write_json(path, {"record": record, "checked_at": fetched})
                    if record:
                        record["rankings"] = merge_rankings(
                            record.get("rankings", []), listing.get("rankings", [])
                        )
                        self.store.import_records([record])
                        task["imported"] += 1
                        outcomes[key] = "imported"
                    else:
                        task["excluded"] += 1
                        outcomes[key] = "outside_scope_or_missing_address"
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    task["needs_review"] += 1
                    outcomes[key] = "fetch_or_parse_failed"
                checked += 1
                task["updated_at"] = datetime.now(UTC).isoformat()
                if checked % 15 == 0:
                    self.publish()
                    write_json(self.folder / "detail-outcomes.json", outcomes)
                    print(
                        f"details {checked}/{len(listings)}; imported {task['imported']}; "
                        f"review {task['needs_review']}; excluded {task['excluded']}",
                        flush=True,
                    )

        await asyncio.gather(*(read_detail(listing) for listing in listings.values()))
        task["status"] = "completed" if not task["needs_review"] else "blocked"
        write_json(self.folder / "detail-outcomes.json", outcomes)


async def collect(
    folder: Path,
    pages: int,
    concurrency: int,
    kinds: tuple[str, ...],
    *,
    refresh_ids: tuple[int, ...] = (),
) -> None:
    await asyncio.to_thread(folder.mkdir, parents=True, exist_ok=True)
    listings, previous = {}, {}
    if refresh_ids:
        captured = json.loads((folder / "source-listings.json").read_text(encoding="utf-8"))
        by_id = {str(row["poiId"]): row for row in captured}
        if any(str(key) not in by_id for key in refresh_ids):
            raise ValueError("Refresh IDs must come from this catalog's captured directory")
        listings = {str(key): by_id[str(key)] for key in refresh_ids}
        previous = json.loads((folder / "catalog.json").read_text(encoding="utf-8"))
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(25, connect=15), follow_redirects=True
    ) as client:
        collector = PublicCollector(folder, client)
        collector.store.initialize()
        collector.cache.mkdir(exist_ok=True)
        try:
            if refresh_ids:
                collector.tasks = [
                    task
                    for task in previous.get("tasks", [])
                    if task.get("source_kind") not in {"ranking", "details_refresh"}
                ]
            else:
                for kind in kinds:
                    await collector.directory(kind, pages, listings)
                write_json(folder / "source-listings.json", list(listings.values()))
            await collector.details(listings, concurrency, refresh=bool(refresh_ids))
            complete = (
                previous.get("coverage", {}).get("source_directory_complete", False)
                if refresh_ids
                else all(t["status"] == "completed" for t in collector.tasks)
            )
            from .catalog_rankings import RankingIndex
            from .catalog_rankings_sources import collect as collect_rankings

            if not refresh_ids:
                await collect_rankings(
                    folder, ("specialty50", "amap", "bbq20"), checkpoint=collector.checkpoint
                )
            tasks = collector.tasks + RankingIndex.read(folder / "ranking-index.json").tasks()
            state = "completed" if all(t["status"] == "completed" for t in tasks) else "partial"
            if any(t["status"] == "blocked" for t in tasks):
                state = "blocked"
            result = collector.publish(complete=complete, state=state)
            print(
                f"saved {len(result['shops'])} Nanchang branches; "
                f"ranked {result['coverage']['hot_total']}",
                flush=True,
            )
        except BaseException:
            collector.publish(state="interrupted")
            raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--pages", type=int, default=0, help="0 follows the published page count")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument(
        "--refresh-ids", type=int, nargs="+", help="Re-read up to 100 captured Trip.com place IDs"
    )
    parser.add_argument(
        "--directories", nargs="+", choices=tuple(DIRECTORIES), default=list(DIRECTORIES)
    )
    args = parser.parse_args()
    if not 0 <= args.pages <= 100 or not 1 <= args.concurrency <= 4:
        parser.error("Invalid public collection limits")
    if args.refresh_ids and (
        len(args.refresh_ids) > 100 or any(key <= 0 for key in args.refresh_ids)
    ):
        parser.error("Choose 1 to 100 positive captured place IDs")
    import fcntl

    args.folder.mkdir(parents=True, exist_ok=True)
    with (args.folder / "collection.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("This catalog already has a running collector.") from None
        asyncio.run(
            collect(
                args.folder,
                args.pages,
                args.concurrency,
                tuple(args.directories),
                refresh_ids=tuple(args.refresh_ids or ()),
            )
        )


if __name__ == "__main__":
    main()
