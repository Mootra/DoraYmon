from urllib.parse import quote

import aiohttp
from cachetools import TTLCache

from .models import Place

BASE_URL = "https://restapi.amap.com/v3/"
# 高德 POI 分类：050000 为餐饮服务。
FOOD_TYPES = "050000"


class AmapError(Exception):
    """A lookup failure with a stable code; never carries the key or the request URL."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class AmapClient:
    def __init__(
        self,
        http: aiohttp.ClientSession,
        key: str,
        *,
        timeout_seconds: float = 8,
        cache_seconds: int = 300,
    ):
        if not key:
            raise ValueError("amap key must not be empty")
        self._http, self._key = http, key
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        # Only public POI results are cached; per-person conditions live in MealState.
        self._cache: TTLCache = TTLCache(maxsize=128, ttl=cache_seconds)

    async def _get(self, endpoint: str, **params: str) -> dict:
        try:
            async with self._http.get(
                BASE_URL + endpoint,
                params={"key": self._key, **params},
                timeout=self._timeout,
            ) as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as error:
            # Exception text may include the request URL with the key, so only the code escapes.
            raise AmapError("network") from error
        if not isinstance(payload, dict) or payload.get("status") != "1":
            raise AmapError("request_failed")
        return payload

    async def nearby(self, location: str, radius: int) -> tuple[Place, ...]:
        key = location, radius
        if key in self._cache:
            return self._cache[key]
        geocoded = await self._get("geocode/geo", address=location)
        geocodes = geocoded.get("geocodes") or []
        if not geocodes:
            raise AmapError("location_not_found")
        if len({g.get("location") for g in geocodes}) > 1:
            raise AmapError("location_ambiguous")
        around = await self._get(
            "place/around",
            location=geocodes[0]["location"],
            types=FOOD_TYPES,
            radius=str(radius),
            sortrule="distance",
            offset="25",
            extensions="all",
        )
        places = []
        for poi in around.get("pois") or []:
            longitude, latitude = poi["location"].split(",")
            distance = poi.get("distance")
            address = poi.get("address")
            business = poi.get("biz_ext")
            business = business if isinstance(business, dict) else {}
            cost = business.get("cost")
            try:
                average_cost = float(cost) if cost not in (None, "", []) else None
                if average_cost is not None and not 0 <= average_cost < 100000:
                    average_cost = None
            except (TypeError, ValueError):
                average_cost = None
            places.append(
                Place(
                    id=poi["id"],
                    name=poi["name"],
                    address=address if isinstance(address, str) else "",
                    distance=None if distance in (None, "", []) else distance,
                    longitude=longitude,
                    latitude=latitude,
                    city=poi.get("cityname") if isinstance(poi.get("cityname"), str) else "",
                    category=poi.get("type") if isinstance(poi.get("type"), str) else "",
                    tags=tuple(
                        tag.strip()
                        for tag in (
                            poi.get("tag") if isinstance(poi.get("tag"), str) else ""
                        ).split(",")
                        if tag.strip()
                    )[:12],
                    rating=(
                        str(business["rating"])
                        if business.get("rating") not in (None, "", [])
                        else ""
                    ),
                    average_cost_yuan=average_cost,
                )
            )
        found = tuple(p for p in places if p.distance is not None and p.distance <= radius)
        self._cache[key] = found
        return found


def map_url(place: Place) -> str:
    return (
        f"https://uri.amap.com/marker?position={place.longitude},{place.latitude}"
        f"&name={quote(place.name, safe='')}&src=DoraYmon&coordinate=gaode&callnative=0"
    )
