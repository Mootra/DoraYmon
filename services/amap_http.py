"""HTTP plumbing for the Amap provider: the only part that touches the network.

Kept separate from the mapping so the parsing stays testable offline, and kept
small on purpose: one GET, a timeout, and an honest failure. A lookup that cannot
reach Amap must report nothing found rather than degrade into a guess.

``requests`` is already a project dependency, so no new one is introduced.

One session is reused for the whole process: a fresh ``requests.get`` pays a new
TLS handshake every time, measured at 3.6-5.1 s from this host to
``restapi.amap.com`` — right on top of the old 5 s timeout, which produced
intermittent read timeouts. The same request over a reused connection measured
0.38-0.41 s.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import requests

from services.amap_provider import AmapCredentials, AmapError
from services.nearby_lookup import NearbyLookup
from services.http_sessions import ThreadLocalSessions

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_RADIUS_METERS = 1500

_SESSIONS = ThreadLocalSessions()


def shared_session() -> requests.Session:
    """One HTTP session per worker thread, created on first use.

    Reuse is the point: the handshake dominates the request cost from this host,
    so paying it once per process turns a borderline 5 s call into a ~0.4 s one.
    """
    return _SESSIONS.get()


def reset_shared_session() -> None:
    """Drop the cached session, for tests and for recovering from a dead pool."""
    _SESSIONS.reset()


def default_http_get(url: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Any:
    """The default requester, kept separate so tests can replace this seam."""
    return shared_session().get(url, timeout=timeout)


def make_http_get(
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    get: Callable[..., Any] | None = None,
) -> Callable[[str], dict[str, Any]]:
    """A JSON GET that raises ``AmapError`` instead of returning junk.

    With no ``get`` injected the shared session is resolved at call time, not at
    construction, so a lookup built earlier still follows a session reset.
    """
    requester = get or default_http_get

    def http_get(url: str) -> dict[str, Any]:
        response = requester(url, timeout=timeout)
        status = getattr(response, "status_code", 200)
        if status != 200:
            raise AmapError(f"高德请求返回 HTTP {status}")
        try:
            payload = response.json()
        except ValueError as error:
            raise AmapError(f"高德响应不是 JSON：{error}") from error
        if not isinstance(payload, dict):
            raise AmapError("高德响应不是对象")
        return payload

    return http_get


def build_amap_lookup(
    settings: Any,
    *,
    http_get: Callable[[str], dict[str, Any]] | None = None,
    geocoder_factory: Callable[..., Any] | None = None,
    lookup_factory: Callable[..., NearbyLookup] | None = None,
) -> NearbyLookup:
    """Build a lookup backed by Amap, or an empty one when unconfigured.

    The geocoder is attached so a start point given as text is resolved to
    coordinates before the around search, which requires them.
    """
    from services.amap_provider import AmapGeocoder, AmapPlaceProvider

    key = str(getattr(settings, "nearby_provider_key", "") or "").strip()
    if not key:
        lookup = NearbyLookup()
        reason = (
            "已选择高德作为地点服务，但缺少 BOT_NEARBY_PROVIDER_KEY，无法查询附近店铺"
            "（也不会用样例数据代替真实结果）"
        )
        lookup.provider_unavailable_reason = reason
        lookup.notes.append(reason)
        return lookup

    credentials = AmapCredentials(key=key)
    requester = http_get or make_http_get()
    city = str(getattr(settings, "nearby_provider_city", "") or "").strip()
    radius = int(getattr(settings, "nearby_provider_radius_meters", DEFAULT_RADIUS_METERS) or 0)

    lookup_kwargs: dict[str, Any] = {
        "places": AmapPlaceProvider(
            credentials,
            http_get=requester,
            radius_meters=radius or DEFAULT_RADIUS_METERS,
        )
    }
    lookup = (lookup_factory or NearbyLookup)(**lookup_kwargs)
    geocoder_cls = geocoder_factory or AmapGeocoder
    lookup.geocoder = geocoder_cls(credentials, http_get=requester, city=city)
    return lookup
