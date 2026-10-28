"""Provider-agnostic map and web lookup for nearby meal data.

The plan's P2b: resolve a start point, find real nearby shops, look up menu and
page material, and keep the source and time of every fact. The providers here are
protocols with fake implementations, so the *policy* half of P2b is complete and
testable offline while the concrete HTTP clients arrive later.

Policy rules from the plan, enforced at this layer:

- A place provider returns shops; it never returns items. What a shop sells needs
  a menu or page source, so a POI alone can never become a recommendation.
- A per-capita figure from a map source stays a per-capita figure.
- Every fetched record carries its provider reference and fetch time.
- Lookups are bounded: a maximum number of search calls and page reads per turn,
  and a budget that stops further fetching and reports what is missing instead of
  silently exhausting itself.
- Search is best-effort: a provider failure degrades to "no results", never to an
  invented one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import threading
from typing import Any, Callable, Protocol

from services.recommendation_types import (
    MealItem,
    PriceRecord,
    Shop,
    SourceKind,
    SourceRef,
    StartPoint,
)

DEFAULT_MAX_SEARCHES = 2
DEFAULT_MAX_PAGE_READS = 2
DEFAULT_CACHE_TTL_MINUTES = 60
# 地理编码结果的本地上限；超过就丢最早的，避免长跑进程无限增长。
MAX_GEOCODE_CACHE_ENTRIES = 32
MAX_LOOKUP_CACHE_ENTRIES = 16
DEFAULT_MAX_CACHE_ENTRIES = 128


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


@dataclass(frozen=True)
class ProviderPlace:
    """One place as a provider reports it. Deliberately not a Shop yet."""

    provider_id: str
    name: str
    address: str = ""
    map_url: str = ""
    distance_meters: int | None = None
    per_person_cny: float | None = None
    open_now: bool | None = None

    @property
    def has_location(self) -> bool:
        return bool(self.address or self.map_url) or self.distance_meters is not None


@dataclass(frozen=True)
class ProviderPage:
    """One fetched page or menu excerpt."""

    url: str
    title: str = ""
    text: str = ""
    published_at: datetime | None = None


class PlaceProvider(Protocol):
    """Finds real places near a start point."""

    def search_nearby(self, start: StartPoint, *, limit: int = 5) -> list[ProviderPlace]:
        ...


class PageProvider(Protocol):
    """Fetches a page or menu by reference."""

    def fetch(self, reference: str) -> ProviderPage | None:
        ...


@dataclass
class LookupBudget:
    """Bounds how much fetching one turn may do."""

    max_searches: int = DEFAULT_MAX_SEARCHES
    max_page_reads: int = DEFAULT_MAX_PAGE_READS
    searches_used: int = 0
    page_reads_used: int = 0

    def __post_init__(self) -> None:
        if self.max_searches < 0 or self.max_page_reads < 0:
            raise ValueError("budgets must not be negative")

    @property
    def searches_left(self) -> int:
        return max(0, self.max_searches - self.searches_used)

    @property
    def page_reads_left(self) -> int:
        return max(0, self.max_page_reads - self.page_reads_used)

    @property
    def exhausted(self) -> bool:
        return self.searches_left <= 0 and self.page_reads_left <= 0

    def spend_search(self) -> bool:
        if self.searches_left <= 0:
            return False
        self.searches_used += 1
        return True

    def spend_page_read(self) -> bool:
        if self.page_reads_left <= 0:
            return False
        self.page_reads_used += 1
        return True

    def describe(self) -> str:
        return (
            f"可搜索 {self.searches_left} 次、可读页面 {self.page_reads_left} 次"
        )


@dataclass
class NearbyLookup:
    """Aggregates the providers with a cache and a per-turn budget."""

    places: PlaceProvider | None = None
    pages: PageProvider | None = None
    budget: LookupBudget = field(default_factory=LookupBudget)
    cache_ttl_minutes: int = DEFAULT_CACHE_TTL_MINUTES
    max_cache_entries: int = DEFAULT_MAX_CACHE_ENTRIES
    notes: list[str] = field(default_factory=list)
    # Optional: resolves a text start point to coordinates. Without it a text-only
    # start point cannot be used for an around search.
    geocoder: Any = None
    # Why no provider is usable, when one was named in configuration. Keeps the
    # generic "no place service configured" message from hiding a missing key.
    provider_unavailable_reason: str = ""
    resolved_start: StartPoint | None = field(default=None, init=False)
    _place_cache: dict[str, tuple[datetime, tuple[Shop, ...]]] = field(
        default_factory=dict, repr=False
    )
    _page_cache: dict[str, tuple[datetime, ProviderPage | None]] = field(
        default_factory=dict, repr=False
    )
    _page_refs: dict[str, SourceRef] = field(default_factory=dict, repr=False)
    # Text -> coordinates, so a repeated question about the same place does not
    # pay a second geocode. Only successes are stored.
    _geocode_cache: dict[str, tuple[datetime, StartPoint]] = field(
        default_factory=dict, repr=False
    )
    _cache_lock: Any = field(default_factory=threading.RLock, repr=False)

    def new_turn(self) -> NearbyLookup:
        """Independent notes/budget with shared, bounded, locked caches.

        Providers are immutable request configuration. No turn may reset the
        mutable state another worker is still using.
        """
        return NearbyLookup(
            places=self.places, pages=self.pages, geocoder=self.geocoder,
            budget=LookupBudget(self.budget.max_searches, self.budget.max_page_reads),
            cache_ttl_minutes=self.cache_ttl_minutes,
            max_cache_entries=self.max_cache_entries,
            provider_unavailable_reason=self.provider_unavailable_reason,
            _place_cache=self._place_cache, _page_cache=self._page_cache,
            _page_refs=self._page_refs, _geocode_cache=self._geocode_cache,
            _cache_lock=self._cache_lock,
        )

    def begin_turn(self) -> None:
        """Reset the per-turn state of a reused lookup.

        Notes and the fetch budget describe one turn: without this reset a reused
        lookup both leaks last turn's explanations into this turn's reply and
        refuses the second search with "本轮搜索次数已用完". The caches are
        deliberately left alone — they are the reason the object is reused.
        """
        self.notes.clear()
        self.budget.searches_used = 0
        self.budget.page_reads_used = 0
        self.resolved_start = None

    # -- places -------------------------------------------------------------

    def resolve_start(self, start: StartPoint) -> StartPoint:
        """Return a coordinate start point, geocoding when only text is known.

        A geocode failure leaves the start point unchanged, so the caller's
        "not usable for nearby" path reports the reason instead of the provider
        being asked to search from nowhere.
        """
        if start.has_coordinates or self.geocoder is None:
            return start
        key = str(start.text or "").strip()
        cached = self._read_cached(self._geocode_cache, key)
        if cached is not None:
            return cached[1]
        try:
            resolved = self.geocoder.geocode(key)
        except Exception:
            # Not cached on purpose: a timeout is transient, and storing the
            # failure would make the place unqueryable for the whole TTL.
            self.notes.append(f"无法定位“{key}”，本轮不做附近搜索")
            return start
        if resolved is None:
            self.notes.append(f"没有查到“{key}”的位置，本轮不做附近搜索")
            return start
        self._remember_geocode(key, resolved)
        return resolved

    def _remember_geocode(self, text: str, resolved: StartPoint) -> None:
        self._write_cached(
            self._geocode_cache, text, resolved, capacity=MAX_GEOCODE_CACHE_ENTRIES,
        )

    def find_shops(self, start: StartPoint, *, limit: int = 5) -> tuple[Shop, ...]:
        """Return real shops near the start point, or nothing.

        A coarser start point than a landmark cannot anchor a nearby search, so it
        is refused here as well as in the selection layer.
        """
        start = self.resolve_start(start)
        self.resolved_start = start
        if not start.is_usable_for_nearby:
            self.notes.append(
                f"“{start.text}”范围太大，无法作为附近搜索的起点"
            )
            return ()
        if self.places is None:
            self.notes.append(
                self.provider_unavailable_reason
                or "没有配置地点服务，无法查找附近店铺"
            )
            return ()

        cached = self._read_cached(self._place_cache, self._cache_key(start, limit))
        if cached is not None:
            return cached[1]

        if not self.budget.spend_search():
            self.notes.append("本轮搜索次数已用完，未继续查找附近店铺")
            return ()

        try:
            found = self.places.search_nearby(start, limit=limit)
        except Exception:
            # Best effort: a provider failure must not become a fabricated place.
            self.notes.append("地点服务请求失败，本轮没有查到附近店铺")
            return ()

        shops: list[Shop] = []
        for place in found:
            if not place.has_location:
                continue
            shops.append(self._to_shop(place))
        result = tuple(shops)
        self._write_cached(self._place_cache, self._cache_key(start, limit), result)
        return result

    @staticmethod
    def _map_reference(provider_id: str) -> str:
        """Prefix a provider id with its source kind exactly once.

        Providers are inconsistent about whether their own ids already carry a
        prefix, and ``map:map:B1`` in a citation is both ugly and harder to trace.
        """
        normalized = str(provider_id or "").strip()
        if normalized.startswith(f"{SourceKind.MAP.value}:"):
            return normalized
        return f"{SourceKind.MAP.value}:{normalized}"

    @staticmethod
    def _cache_key(start: StartPoint, limit: int) -> str:
        """Keyed by start point and requested size.

        The cached tuple is already truncated to ``limit``. A "换一家" turn asks
        for 25 so it can exclude what was already shown, and being served the
        cached 5 would leave nothing to show.
        """
        where = (
            f"{start.latitude:.6f},{start.longitude:.6f}"
            if start.has_coordinates
            else start.text.strip()
        )
        return f"{where}|{max(0, int(limit))}"

    def _to_shop(self, place: ProviderPlace) -> Shop:
        source = SourceRef(
            kind=SourceKind.MAP,
            reference=self._map_reference(place.provider_id),
            fetched_at=_now(),
        )
        return Shop(
            shop_id=str(place.provider_id),
            name=place.name,
            source=source,
            address=place.address,
            map_url=place.map_url,
            distance_meters=place.distance_meters,
            open_now=place.open_now,
        )

    def price_from_place(self, place: ProviderPlace) -> PriceRecord | None:
        """A map per-capita figure, kept as a per-capita figure.

        The plan is explicit that this cannot stand in for a menu price, so the
        basis is preserved rather than upgraded.
        """
        if place.per_person_cny is None:
            return None
        return PriceRecord(
            basis="per_person",
            source=SourceRef(
                kind=SourceKind.MAP,
                reference=f"map:{place.provider_id}#per_person",
                fetched_at=_now(),
            ),
            amount_cny=float(place.per_person_cny),
        )

    # -- pages --------------------------------------------------------------

    def fetch_page(self, reference: str) -> ProviderPage | None:
        """Fetch one page, bounded and cached. None means "not obtained"."""
        normalized = str(reference or "").strip()
        if not normalized:
            return None
        cached = self._read_cached(self._page_cache, normalized)
        if cached is not None:
            return cached[1]
        if self.pages is None:
            self.notes.append("没有配置页面服务，无法读取资料")
            return None
        if not self.budget.spend_page_read():
            self.notes.append("本轮页面读取次数已用完，未继续补全资料")
            return None
        try:
            page = self.pages.fetch(normalized)
        except Exception:
            self.notes.append("页面读取失败，本条资料视为未取得")
            page = None
        with self._cache_lock:
            self._write_cached(self._page_cache, normalized, page)
            if page is not None and normalized in self._page_cache:
                self._page_refs[normalized] = SourceRef(
                    kind=SourceKind.WEB, reference=normalized, fetched_at=_now()
                )
            else:
                self._page_refs.pop(normalized, None)
        return page

    def source_for_page(self, reference: str) -> SourceRef | None:
        """The attribution for a page that was actually fetched."""
        key = str(reference or "").strip()
        with self._cache_lock:
            self._prune_cache(self._page_cache)
            return self._page_refs.get(key)

    def items_from_page(self, page: ProviderPage, shop_id: str) -> tuple[MealItem, ...]:
        """No generic extraction is attempted here.

        Turning arbitrary page text into items needs a real parser plus review
        (the plan's "draft then confirm" step), so this reports honestly instead
        of guessing at menu structure.
        """
        if page is None:
            return ()
        self.notes.append("页面资料还需要人工核对后才能成为餐点记录")
        return ()

    # -- internals ----------------------------------------------------------

    def _prune_cache(self, cache: dict) -> None:
        for key, (fetched_at, _) in tuple(cache.items()):
            if self._is_expired(fetched_at):
                cache.pop(key, None)
        if cache is self._page_cache:
            for key in tuple(self._page_refs):
                if key not in cache:
                    self._page_refs.pop(key, None)

    def _read_cached(self, cache: dict, key: str):
        with self._cache_lock:
            self._prune_cache(cache)
            return cache.get(key)

    def _write_cached(self, cache: dict, key: str, value: Any, *, capacity=None) -> None:
        with self._cache_lock:
            self._prune_cache(cache)
            cache.pop(key, None)
            cache[key] = (_now(), value)
            bound = self.max_cache_entries if capacity is None else capacity
            while len(cache) > max(0, int(bound)):
                removed = next(iter(cache))
                cache.pop(removed)
                if cache is self._page_cache:
                    self._page_refs.pop(removed, None)

    def _is_expired(self, fetched_at: datetime) -> bool:
        if self.cache_ttl_minutes <= 0:
            return True
        return _now() - fetched_at > timedelta(minutes=self.cache_ttl_minutes)

    def describe_notes(self, limit: int = 5) -> str:
        return "\n".join(self.notes[:limit])

    def debug_state(self) -> dict[str, Any]:
        return {
            "shops_cached": len(self._place_cache),
            "pages_cached": len(self._page_cache),
            "geocodes_cached": len(self._geocode_cache),
            "budget": self.budget.describe(),
            "notes": len(self.notes),
        }


SUPPORTED_PROVIDERS = ("amap",)


def build_lookup(settings: Any) -> NearbyLookup:
    """Build a lookup from configuration.

    An unknown provider name yields a lookup with no place provider plus an
    explicit note. That is deliberate: falling back to a fake provider would put
    invented shops in front of a user, which is the one outcome the plan forbids.
    Importing the Amap client lazily keeps a bot with no provider configured from
    importing the HTTP stack at all.
    """
    name = str(getattr(settings, "nearby_provider", "") or "").strip().lower()
    if not name:
        return NearbyLookup()
    if name in SUPPORTED_PROVIDERS:
        from services.amap_http import build_amap_lookup

        return build_amap_lookup(settings)
    lookup = NearbyLookup()
    reason = (
        f"不认识的地点服务“{name}”，本轮不会查到附近的店"
        f"（支持：{'、'.join(SUPPORTED_PROVIDERS)}）"
    )
    # Both places on purpose: the reason has to survive begin_turn(), and the
    # note explains it on the turn that built the lookup.
    lookup.provider_unavailable_reason = reason
    lookup.notes.append(reason)
    return lookup


def provider_is_configured(settings: Any) -> bool:
    return bool(str(getattr(settings, "nearby_provider", "") or "").strip())


_LOOKUP_CACHE: dict[tuple[str, str, str, int], NearbyLookup] = {}
_LOOKUP_CACHE_LOCK = threading.Lock()


def lookup_for(
    settings: Any,
    *,
    factory: Callable[[Any], NearbyLookup] | None = None,
) -> NearbyLookup:
    """The process-wide lookup template for this configuration.

    Building one per message threw the place and geocode caches away every turn,
    so each nearby question paid geocoding plus an around search again. Call
    ``new_turn()`` before concurrent use: notes and budget belong to one query.
    """
    key = _lookup_cache_key(settings)
    with _LOOKUP_CACHE_LOCK:
        lookup = _LOOKUP_CACHE.get(key)
        if lookup is None:
            lookup = (factory or build_lookup)(settings)
            _LOOKUP_CACHE[key] = lookup
            while len(_LOOKUP_CACHE) > MAX_LOOKUP_CACHE_ENTRIES:
                _LOOKUP_CACHE.pop(next(iter(_LOOKUP_CACHE)))
        return lookup


def _lookup_cache_key(settings: Any) -> tuple[str, str, str, int]:
    """What shapes the request: provider, credential, city and radius.

    The credential is part of the key so a lookup built for one key is never
    reused after the key changes. It is held in memory only, never logged.
    """
    return (
        str(getattr(settings, "nearby_provider", "") or "").strip().lower(),
        str(getattr(settings, "nearby_provider_key", "") or "").strip(),
        str(getattr(settings, "nearby_provider_city", "") or "").strip(),
        int(getattr(settings, "nearby_provider_radius_meters", 0) or 0),
    )


def reset_lookup_cache() -> None:
    """Drop cached lookups (tests, and after a configuration reload)."""
    with _LOOKUP_CACHE_LOCK:
        _LOOKUP_CACHE.clear()
