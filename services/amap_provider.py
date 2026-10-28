"""Amap (高德) POI provider: real nearby places, never menu items.

Implements ``PlaceProvider`` against the Amap Web service "周边搜索"
(``/v3/place/around``). Verified request parameters: ``key``, ``location``
"lng,lat", ``keywords``/``types``, ``radius`` up to 50000, ``offset`` up to 25,
``page``, ``extensions=all`` for the business block, ``sortrule=distance``.

Verified response shape: ``status`` is the string ``"1"`` on success, ``pois`` is
a list where ``distance`` is only present for around-search, ``type`` is a
semicolon-separated category path, ``location`` is "lng,lat", and
``biz_ext.cost`` is the per-capita spend. Amap returns numbers as strings, and
``biz_ext`` may be an empty list rather than an object, so parsing is defensive.

What this provider deliberately does not do: it never produces ``MealItem``s. A
POI says a shop exists and roughly what it costs per person; it cannot say what
the shop sells or what a dish costs. ``cost`` is therefore kept as a per-capita
figure and is never promoted to a menu price.

The HTTP call is injected, so the whole mapping is testable offline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlencode

from services.nearby_lookup import ProviderPlace
from services.recommendation_types import StartPoint, StartPrecision

AROUND_URL = "https://restapi.amap.com/v3/place/around"
DEFAULT_RADIUS_METERS = 1500
MAX_RADIUS_METERS = 50000
MAX_PAGE_SIZE = 25
# Amap's restaurant category code; 050000 is the parent "餐饮服务" class.
RESTAURANT_TYPES = "050000"

STATUS_OK = "1"


class AmapError(Exception):
    """The provider could not complete the request."""


@dataclass(frozen=True)
class AmapCredentials:
    key: str

    def __post_init__(self) -> None:
        if not str(self.key or "").strip():
            raise ValueError("Amap key must not be empty")


def _as_float(value: Any) -> float | None:
    """Amap returns numbers as strings; "" means unknown, not zero."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _as_int(value: Any) -> int | None:
    number = _as_float(value)
    return int(number) if number is not None else None


def _business_block(poi: dict[str, Any]) -> dict[str, Any]:
    """``biz_ext`` is sometimes an object and sometimes an empty list."""
    block = poi.get("biz_ext")
    return block if isinstance(block, dict) else {}


def _coordinates(poi: dict[str, Any]) -> tuple[float | None, float | None]:
    raw = str(poi.get("location", "") or "").strip()
    if not raw or "," not in raw:
        return (None, None)
    longitude, _, latitude = raw.partition(",")
    return (_as_float(longitude), _as_float(latitude))


def parse_pois(payload: dict[str, Any]) -> list[ProviderPlace]:
    """Convert an Amap response into provider places, dropping unusable rows."""
    if not isinstance(payload, dict):
        return []
    raw_pois = payload.get("pois")
    if not isinstance(raw_pois, list):
        return []

    places: list[ProviderPlace] = []
    for poi in raw_pois:
        if not isinstance(poi, dict):
            continue
        provider_id = str(poi.get("id", "") or "").strip()
        name = str(poi.get("name", "") or "").strip()
        if not provider_id or not name:
            # An unnamed or unidentified place cannot be attributed later.
            continue
        address = str(poi.get("address", "") or "").strip()
        if isinstance(poi.get("address"), list):
            address = ""
        longitude, latitude = _coordinates(poi)
        business = _business_block(poi)
        places.append(
            ProviderPlace(
                provider_id=provider_id,
                name=name,
                address=address,
                map_url=_map_url(poi, longitude, latitude),
                distance_meters=_as_int(poi.get("distance")),
                per_person_cny=_as_float(business.get("cost")),
                open_now=None,  # Amap does not state opening state here.
            )
        )
    return places


def _map_url(
    poi: dict[str, Any],
    longitude: float | None,
    latitude: float | None,
) -> str:
    """A link a user can open, built only from what the response actually gave."""
    if longitude is None or latitude is None:
        return ""
    name = str(poi.get("name", "") or "").strip()
    query = urlencode(
        {
            "position": f"{longitude:.6f},{latitude:.6f}",
            "name": name,
        }
    )
    return f"https://uri.amap.com/marker?{query}"


class AmapPlaceProvider:
    """``PlaceProvider`` backed by Amap's around search."""

    def __init__(
        self,
        credentials: AmapCredentials,
        *,
        http_get: Callable[[str], dict[str, Any]],
        radius_meters: int = DEFAULT_RADIUS_METERS,
        keywords: str = "",
        page_size: int = MAX_PAGE_SIZE,
    ) -> None:
        self.credentials = credentials
        self.http_get = http_get
        self.radius_meters = max(1, min(int(radius_meters), MAX_RADIUS_METERS))
        self.keywords = str(keywords or "").strip()
        self.page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))

    def search_nearby(self, start: StartPoint, *, limit: int = 5) -> list[ProviderPlace]:
        if not start.is_usable_for_nearby:
            raise AmapError(f"起点不足以做附近搜索：{start.text}")
        if not start.has_coordinates:
            raise AmapError(
                "高德周边搜索需要坐标，当前起点只有文字，需要先做地理编码"
            )
        url = self.build_url(start, limit=limit)
        payload = self.http_get(url)
        check_status(payload)
        return parse_pois(payload)[: max(0, int(limit))]

    def build_url(self, start: StartPoint, *, limit: int = 5) -> str:
        """The request URL, exposed so it can be asserted without a network."""
        offset = max(1, min(int(limit) or 1, self.page_size))
        params = {
            "key": self.credentials.key,
            "location": f"{start.longitude:.6f},{start.latitude:.6f}",
            "radius": str(self.radius_meters),
            "offset": str(offset),
            "page": "1",
            "extensions": "all",
            "sortrule": "distance",
        }
        if self.keywords:
            params["keywords"] = self.keywords
        else:
            params["types"] = RESTAURANT_TYPES
        return f"{AROUND_URL}?{urlencode(params)}"


GEOCODE_URL = "https://restapi.amap.com/v3/geocode/geo"


def parse_geocode(payload: dict[str, Any]) -> StartPoint | None:
    """Turn a geocode response into a coordinate start point, or None.

    Amap answers a geocode with one or more matches; a missing ``geocodes`` list
    or an unparseable location means the place could not be resolved, which must
    not be turned into a guess.
    """
    if not isinstance(payload, dict):
        return None
    matches = payload.get("geocodes")
    if not isinstance(matches, list) or not matches:
        return None
    for match in matches:
        if not isinstance(match, dict):
            continue
        name = str(match.get("formatted_address", "") or "").strip()
        longitude, latitude = _coordinates({"location": match.get("location")})
        if longitude is None or latitude is None:
            continue
        return StartPoint(
            text=name or "已定位的起点",
            precision=StartPrecision.COORDINATE,
            latitude=latitude,
            longitude=longitude,
        )
    return None


class AmapGeocoder:
    """Resolves a place name to coordinates through Amap's geocoding API."""

    def __init__(
        self,
        credentials: AmapCredentials,
        *,
        http_get: Callable[[str], dict[str, Any]],
        city: str = "",
    ) -> None:
        self.credentials = credentials
        self.http_get = http_get
        self.city = str(city or "").strip()

    def build_url(self, address: str) -> str:
        normalized = str(address or "").strip()
        if not normalized:
            raise AmapError("地址不能为空")
        params = {"key": self.credentials.key, "address": normalized}
        if self.city:
            params["city"] = self.city
        return f"{GEOCODE_URL}?{urlencode(params)}"

    def geocode(self, address: str) -> StartPoint | None:
        payload = self.http_get(self.build_url(address))
        check_status(payload)
        return parse_geocode(payload)


def check_status(payload: dict[str, Any]) -> None:
    """Raise on an Amap error response so the caller can degrade honestly."""
    if not isinstance(payload, dict):
        raise AmapError("响应不是对象")
    status = str(payload.get("status", "") or "").strip()
    if status != STATUS_OK:
        info = str(payload.get("info", "") or "").strip()
        infocode = str(payload.get("infocode", "") or "").strip()
        raise AmapError(f"高德返回错误：{info or 'unknown'}（{infocode or 'no code'}）")
