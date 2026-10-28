"""Read-only source adapters; source text remains attributed, not promoted to menu facts."""

import asyncio
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import ValidationError

from .evidence import catalog_records, evidence_key, review_record, source_text
from .evidence import public_url as public_url
from .library import LibraryError, MealLibrary
from .models import (
    Evidence,
    MealCandidate,
    MealReport,
    MenuCatalog,
    Place,
    SourceCoverage,
    SourceName,
    utc_now,
)
from .models import (
    MenuEntry as MenuEntry,
)

SOURCE_LABELS = {
    "catalog": "商家资料",
    "meituan": "美团",
    "xiaohongshu": "小红书",
    "dianping": "大众点评",
    "library": "本地资料库",
}
READ_TOOLS = {"search_feeds", "get_feed_detail", "dianping_search", "dianping_shop"}


def _normalized(text: str) -> str:
    return re.sub(r"[\W_]", "", text).lower()


def _same_branch(place: Place, name: str, address: str) -> bool:
    left, right = _normalized(place.address), _normalized(address)
    return (
        _normalized(place.name) == _normalized(name)
        and min(len(left), len(right)) >= 6
        and (left in right or right in left)
    )


def _published(value: Any) -> datetime | None:
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        published = datetime.fromtimestamp(value / 1000 if value > 10**11 else value, UTC)
        return published if published <= utc_now() else None
    except (ValueError, OverflowError, OSError):
        return None


class SourceError(Exception):
    """Only a stable public code crosses the adapter boundary."""


class McpTools:
    def __init__(
        self, url: str, *, timeout: float = 40, transport: httpx.AsyncBaseTransport | None = None
    ):
        parsed = urlsplit(url)
        if not parsed.hostname:
            raise ValueError("MCP endpoint requires a hostname")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("MCP endpoint must not contain credentials or query parameters")
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("MCP endpoint requires HTTPS or loopback HTTP")
        self.url, self.timeout, self.transport = url, timeout, transport
        self._stack = None
        self.session: ClientSession | None = None

    async def __aenter__(self):
        from contextlib import AsyncExitStack

        self._stack = AsyncExitStack()
        try:
            http = await self._stack.enter_async_context(
                httpx.AsyncClient(timeout=self.timeout, transport=self.transport)
            )
            read, write, _ = await self._stack.enter_async_context(
                streamable_http_client(self.url, http_client=http)
            )
            self.session = await self._stack.enter_async_context(ClientSession(read, write))
            await self.session.initialize()
            return self
        except BaseException:
            await self._stack.aclose()
            raise

    async def __aexit__(self, *exc):
        assert self._stack is not None
        return await self._stack.__aexit__(*exc)

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in READ_TOOLS:
            raise ValueError("This adapter exposes only its declared read tools")
        assert self.session is not None
        result = await self.session.call_tool(name, arguments)
        if result.isError:
            raise SourceError("tool_failed")
        payload = result.structuredContent
        if payload is None:
            text = "\n".join(c.text for c in result.content if c.type == "text")
            try:
                payload = json.loads(text)
            except (json.JSONDecodeError, ValueError) as error:
                raise SourceError("invalid_response") from error
        if not isinstance(payload, dict):
            raise SourceError("invalid_response")
        return payload


def _read_catalog(path: Path) -> str:
    with path.open("rb") as stream:
        raw = stream.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("Menu catalog exceeds size limit")
    return raw.decode("utf-8")


class ResearchSources:
    def __init__(
        self,
        *,
        catalog_path: Path | None = None,
        xiaohongshu_url: str = "",
        dianping_url: str = "",
        timeout: float = 40,
        client_factory: Callable[..., Any] = McpTools,
        library: MealLibrary | None = None,
        cache_hours: float = 24,
    ):
        self.catalog_path = catalog_path
        self.urls = {"xiaohongshu": xiaohongshu_url, "dianping": dianping_url}
        self.timeout, self.client_factory = timeout, client_factory
        self.library, self.max_age = library, timedelta(hours=cache_hours)
        self._locks = {source: asyncio.Lock() for source in self.urls}

    @property
    def configured(self) -> bool:
        return self.catalog_path is not None or any(self.urls.values())

    async def enrich(
        self,
        places: tuple[Place, ...],
        requirements: str,
        stage: Callable[[str], None],
        *,
        scope: str | None = None,
    ) -> MealReport:
        scope = sha256(f"{scope}\0{requirements}".encode()).hexdigest() if scope else None
        cached = {}
        library_error = False
        saved = 0
        if self.library is not None:
            stage("正在查已有的餐饮资料。")
            try:
                cached = await asyncio.to_thread(
                    self.library.lookup, tuple(p.id for p in places), scope=scope
                )
            except (LibraryError, OSError):
                library_error = True
        pending = {
            source: tuple(
                p
                for p in places
                if not any(
                    item.source == source and not review_record(item, self.max_age).needs_review
                    for item in cached.get(p.id, ())
                )
            )
            for source in self.urls
        }
        if self.catalog_path is not None or any(self.urls[s] and v for s, v in pending.items()):
            stage(f"找到 {len(places)} 家候选，正在补充菜品和用餐信息。")
        replies = await asyncio.gather(
            self._catalog(places),
            *(
                self._remote(source, url, pending[source], requirements)
                for source, url in self.urls.items()
            ),
        )
        if any(evidence for _, evidence in replies):
            stage("资料找到了，正在整理来源和缺项。")
        if self.library is not None and not library_error:
            try:
                saved += await asyncio.to_thread(self.library.save, replies[0][1], places=places)
                if scope:
                    private = {}
                    for _, mapping in replies[1:]:
                        for amap_id, items in mapping.items():
                            private.setdefault(amap_id, []).extend(items)
                    if private:
                        saved += await asyncio.to_thread(self.library.save, private, scope=scope)
            except (LibraryError, OSError, ValueError):
                library_error = True
        candidates = []
        for place in places:
            merged = {evidence_key(item): item for item in cached.get(place.id, ())}
            for _, mapping in replies:
                for item in mapping.get(place.id, ()):
                    key = evidence_key(item)
                    if key not in merged or item.observed_at >= merged[key].observed_at:
                        merged[key] = item
            evidence = sorted(
                (review_record(item, self.max_age) for item in merged.values()),
                key=lambda item: (not item.needs_review, item.kind == "menu", item.observed_at),
                reverse=True,
            )[:8]
            candidates.append(
                MealCandidate(
                    place=place,
                    evidence=tuple(evidence),
                )
            )
        coverage = [c for c, _ in replies]
        if self.library is not None:
            coverage.append(
                SourceCoverage(
                    source="library",
                    status=("partial" if cached else "unavailable")
                    if library_error
                    else ("used" if cached or saved else "no_match"),
                    reason="library_unavailable" if library_error else "",
                )
            )
        return MealReport(candidates=tuple(candidates), coverage=tuple(coverage))

    async def _catalog(self, places: tuple[Place, ...]):
        if self.catalog_path is None:
            return SourceCoverage(source="catalog", status="not_configured"), {}
        try:
            raw = await asyncio.to_thread(_read_catalog, self.catalog_path)
            catalog = MenuCatalog.model_validate_json(raw)
            ids = {p.id for p in places}
            found = {
                key: items[:4] for key, items in catalog_records(catalog).items() if key in ids
            }
            status = "used" if found else "no_match"
            return SourceCoverage(source="catalog", status=status), found
        except (OSError, UnicodeError, ValidationError, ValueError):
            return SourceCoverage(
                source="catalog", status="unavailable", reason="invalid_catalog"
            ), {}

    async def _remote(
        self, source: SourceName, url: str, places: tuple[Place, ...], requirements: str
    ):
        if not url:
            return SourceCoverage(source=source, status="not_configured"), {}
        if not places:
            return SourceCoverage(source=source, status="used", reason="library_cache"), {}
        found: dict[str, list[Evidence]] = {}
        try:
            async with asyncio.timeout(self.timeout):
                async with (
                    self._locks[source],
                    self.client_factory(url, timeout=self.timeout) as client,
                ):
                    for place in places:
                        if source == "xiaohongshu":
                            items = await self._xiaohongshu(client, place, requirements)
                        else:
                            items = await self._dianping(client, place)
                        if items:
                            found[place.id] = items
            return SourceCoverage(source=source, status="used" if found else "no_match"), found
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # SDK transports can raise nested task-group errors. Neither their text nor
            # arbitrary server error messages may reach the planner or an export.
            code = str(error) if isinstance(error, SourceError) else "source_unavailable"
            return SourceCoverage(
                source=source, status="partial" if found else "unavailable", reason=code
            ), found

    async def _xiaohongshu(self, client, place: Place, requirements: str) -> list[Evidence]:
        query = " ".join(p for p in (place.city, place.name, requirements) if p)
        payload = await client.call("search_feeds", {"keyword": query})
        feeds = payload.get("feeds")
        if not isinstance(feeds, list):
            raise SourceError("invalid_response")
        result = []
        for feed in feeds[:5]:
            if not isinstance(feed, dict):
                continue
            card = feed.get("noteCard") or {}
            title = source_text(card.get("displayTitle"), 200)
            note_id, token = feed.get("id"), feed.get("xsecToken")
            if not isinstance(note_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{6,80}", note_id):
                continue
            if not isinstance(token, str) or not token:
                continue
            detail = await client.call(
                "get_feed_detail",
                {"feed_id": note_id, "xsec_token": token, "load_all_comments": False},
            )
            data = detail.get("data", detail)
            note = data.get("note", {}) if isinstance(data, dict) else {}
            text = source_text(note.get("desc"))
            if _normalized(place.name) not in _normalized(title + text) or not text:
                continue
            result.append(
                Evidence(
                    source="xiaohongshu",
                    title=source_text(note.get("title"), 200) or title or place.name,
                    text=text,
                    url=f"https://www.xiaohongshu.com/explore/{note_id}",
                    observed_at=utc_now(),
                    published_at=_published(note.get("time")),
                    source_id=note_id,
                    branch_confirmed=False,
                )
            )
            break
        return result

    async def _dianping(self, client, place: Place) -> list[Evidence]:
        if not place.city:
            return []
        payload = await client.call(
            "dianping_search", {"keyword": place.name, "city": place.city, "limit": 3}
        )
        if payload.get("error"):
            raise SourceError("tool_failed")
        shops = payload.get("items")
        if not isinstance(shops, list):
            raise SourceError("invalid_response")
        result = []
        for shop in shops[:3]:
            if not isinstance(shop, dict):
                continue
            if _normalized(source_text(shop.get("name"))) != _normalized(place.name):
                continue
            shop_id = shop.get("id")
            if not isinstance(shop_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", shop_id):
                continue
            detail = await client.call("dianping_shop", {"shop_id": shop_id})
            if detail.get("error"):
                raise SourceError("tool_failed")
            name = source_text(detail.get("name"), 200)
            if _normalized(name) != _normalized(place.name):
                continue
            address = source_text(detail.get("address"), 300)
            details = [
                f"{label}：{value}"
                for key, label in (
                    ("category", "品类"),
                    ("opening_hours", "资料中的营业时间"),
                    ("rating", "平台评分"),
                    ("price_range", "平台人均参考"),
                    ("description", "门店介绍"),
                )
                if (value := source_text(str(detail[key]), 300) if detail.get(key) else "")
            ]
            if not details:
                continue
            result.append(
                Evidence(
                    source="dianping",
                    title=name or place.name,
                    text="；".join(details)[:2400],
                    url=f"https://www.dianping.com/shop/{shop_id}",
                    observed_at=utc_now(),
                    branch_confirmed=_same_branch(place, name, address),
                    kind="listing",
                    source_id=shop_id,
                )
            )
            break
        return result
