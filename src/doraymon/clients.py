from urllib.parse import quote

import aiohttp
from cachetools import TTLCache
from pydantic import BaseModel, Field

from .config import Settings
from .models import Place


class CompletionMessage(BaseModel):
    content: str = Field(min_length=1)


class Choice(BaseModel):
    message: CompletionMessage
    finish_reason: str


class Completion(BaseModel):
    choices: list[Choice] = Field(min_length=1)


class Clients:
    def __init__(self, settings: Settings, http: aiohttp.ClientSession):
        self.settings, self.http = settings, http
        self.places: TTLCache = TTLCache(maxsize=128, ttl=300)

    async def complete(self, messages: list[dict[str, str]]) -> str:
        if not self.settings.model_key.get_secret_value():
            raise ValueError("model_key_missing")
        async with self.http.post(
            self.settings.model_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + self.settings.model_key.get_secret_value()},
            json={
                "model": self.settings.model_name,
                "messages": messages,
                "max_tokens": self.settings.model_max_tokens,
                "temperature": 0.7,
            },
        ) as response:
            response.raise_for_status()
            completion = Completion.model_validate(await response.json())
        choice = completion.choices[0]
        text = choice.message.content.strip()
        if not text:
            raise ValueError("model_empty_reply")
        if choice.finish_reason == "length":
            text += "\n[生成达到长度上限，回答未完成]"
        return text

    async def _amap(self, endpoint: str, **params) -> dict:
        if not self.settings.amap_key.get_secret_value():
            raise ValueError("amap_key_missing")
        async with self.http.get(
            "https://restapi.amap.com/v3/" + endpoint,
            params={"key": self.settings.amap_key.get_secret_value(), **params},
        ) as response:
            response.raise_for_status()
            payload = await response.json()
        if payload.get("status") != "1":
            raise ValueError("amap_request_failed")
        return payload

    async def nearby(self, location: str, radius: int) -> tuple[Place, ...]:
        key = location, radius
        if key in self.places:
            return self.places[key]
        geocoded = await self._amap("geocode/geo", address=location)
        geocodes = geocoded.get("geocodes", [])
        if not geocodes:
            raise ValueError("location_not_found")
        if len({g["location"] for g in geocodes}) > 1:
            raise ValueError("location_ambiguous")
        around = await self._amap(
            "place/around",
            location=geocodes[0]["location"],
            types="050000",
            radius=str(radius),
            sortrule="distance",
            offset="25",
            extensions="base",
        )
        places = []
        for poi in around.get("pois", []):
            lon, lat = poi["location"].split(",")
            distance = poi.get("distance")
            places.append(
                Place(
                    id=poi["id"],
                    name=poi["name"],
                    address=poi["address"] if isinstance(poi.get("address"), str) else "",
                    distance=None if distance in (None, "", []) else distance,
                    longitude=lon,
                    latitude=lat,
                )
            )
        found = tuple(p for p in places if p.distance is not None and p.distance <= radius)
        self.places[key] = found
        return found


def map_url(place: Place) -> str:
    return (
        f"https://uri.amap.com/marker?position={place.longitude},{place.latitude}"
        f"&name={quote(place.name, safe='')}&src=DoraYmon&coordinate=gaode&callnative=0"
    )
