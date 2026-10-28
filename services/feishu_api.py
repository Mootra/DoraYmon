"""HTTP plumbing for the Feishu API: the only part of the channel that sends.

Receiving a Feishu message needs the official long connection, which is a
WebSocket protocol with its own framing. *Sending* does not: it is four
documented HTTPS calls, and ``requests`` is already a project dependency, so the
sending half adds no dependency and can be verified offline against a fake
requester — the same shape ``services/amap_http.py`` established.

    POST /open-apis/auth/v3/tenant_access_token/internal
    POST /open-apis/im/v1/messages
    POST /open-apis/im/v1/messages/{message_id}/reply
    GET  /open-apis/im/v1/messages/{message_id}
    GET  /open-apis/bot/v3/info

Three things about this API shape the code:

- **HTTP 200 is not success.** A refusal arrives as ``200`` with a non-zero
  ``code`` in the body, so the body decides. The response is returned undecoded
  for exactly that reason: the adapter reads ``code`` itself, and only the
  transport-level failures (no HTTP response, non-200, not JSON) are raised.
- **The tenant token expires.** It is cached with the lifetime the API reports
  and refreshed with a margin, because a reply that fails at minute 119 of a
  two-hour token would look like a random outage.
- **The app secret never leaves this module.** It is sent once per token
  refresh and never logged or put in an exception message.
"""

from __future__ import annotations

import logging
import time
import threading
from typing import Any, Callable
from urllib.parse import quote

import requests
from services.http_sessions import ThreadLocalSessions

from services.feishu_adapter import (
    ACTION_BOT_INFO,
    ACTION_CREATE_MESSAGE,
    ACTION_GET_MESSAGE,
    ACTION_REPLY_MESSAGE,
    MSG_TYPE_TEXT,
    RECEIVE_ID_CHAT,
    RECEIVE_ID_OPEN,
)

logger = logging.getLogger(__name__)

FEISHU_BASE_URL = "https://open.feishu.cn/open-apis"
LARK_BASE_URL = "https://open.larksuite.com/open-apis"

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_TOKEN_LIFETIME_SECONDS = 7200.0
# Refresh this long before the reported expiry rather than after a failure.
TOKEN_REFRESH_MARGIN_SECONDS = 300.0
MIN_TOKEN_LIFETIME_SECONDS = 60.0

RECEIVE_ID_TYPES = (RECEIVE_ID_OPEN, RECEIVE_ID_CHAT, "user_id", "union_id", "email")

_TOKEN_PATH = "/auth/v3/tenant_access_token/internal"
_MESSAGES_PATH = "/im/v1/messages"
_BOT_INFO_PATH = "/bot/v3/info"

_SESSIONS = ThreadLocalSessions()


class FeishuApiError(Exception):
    """The Feishu API could not be reached, or answered something unusable.

    Carries ``status`` when a real HTTP status was involved, so
    ``classify_send_error`` can label the failure instead of falling back to
    "unclassified".
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        if status is not None:
            self.status = int(status)


def shared_session() -> requests.Session:
    """One HTTP session per worker thread, created on first use.

    Reuse matters here for the same reason it did for Amap: the TLS handshake
    dominates the cost of a single call, and this channel makes one call per
    reply segment.
    """
    return _SESSIONS.get()


def reset_shared_session() -> None:
    """Drop the cached session, for tests and for recovering from a dead pool."""
    _SESSIONS.reset()


def default_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Any:
    """The default requester, kept separate so tests can replace this seam."""
    return shared_session().request(
        method,
        url,
        headers=headers,
        params=params,
        json=json,
        timeout=timeout,
    )


def _decode(response: Any) -> dict[str, Any]:
    """Turn one HTTP response into a decoded body, or raise.

    A non-zero business ``code`` is deliberately *not* raised here: it is part of
    the body the adapter inspects, and turning it into an exception would hide
    the difference between "Feishu refused this call" and "Feishu was
    unreachable".
    """
    status = getattr(response, "status_code", 200)
    try:
        payload = response.json()
    except ValueError as error:
        raise FeishuApiError(f"飞书响应不是 JSON（HTTP {status}）", status=status) from error
    if not isinstance(payload, dict):
        raise FeishuApiError(f"飞书响应不是对象（HTTP {status}）", status=status)
    if status != 200:
        raise FeishuApiError(
            f"飞书请求返回 HTTP {status}", status=status, code=payload.get("code")
        )
    return payload


def _required(params: dict[str, Any], name: str) -> str:
    value = str(params.get(name, "") or "").strip()
    if not value:
        raise FeishuApiError(f"飞书动作缺少 {name}")
    return value


class FeishuApi:
    """Sends Feishu actions over HTTPS, with a cached tenant token."""

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        base_url: str = FEISHU_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        request: Callable[..., Any] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.app_id = str(app_id or "").strip()
        self.app_secret = str(app_secret or "").strip()
        self.base_url = str(base_url or FEISHU_BASE_URL).rstrip("/")
        self.timeout = float(timeout)
        self._request = request or default_request
        self._clock = clock or time.monotonic
        self._token = ""
        self._token_expires_at = 0.0
        self.token_refreshes = 0
        self._token_lock = threading.Lock()

    # -- token --------------------------------------------------------------

    @property
    def cached_token(self) -> str:
        """The current token, or "" when none is cached. Never triggers a call."""
        return self._token if self._clock() < self._token_expires_at else ""

    def tenant_token(self) -> str:
        """Return a valid tenant token, fetching one when the cache is stale."""
        with self._token_lock:
            return self._tenant_token()

    def _tenant_token(self) -> str:
        cached = self.cached_token
        if cached:
            return cached
        if not self.app_id or not self.app_secret:
            raise FeishuApiError("缺少飞书 App ID 或 App Secret，无法获取 tenant token")

        response = self._request(
            "POST",
            f"{self.base_url}{_TOKEN_PATH}",
            headers={"Content-Type": "application/json; charset=utf-8"},
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=self.timeout,
        )
        payload = _decode(response)
        code = payload.get("code")
        if code not in (None, 0):
            # The message is the API's own wording; the secret is never in it.
            raise FeishuApiError(
                f"飞书拒绝了应用凭证（code={code}）：{payload.get('msg') or ''}".strip(),
                code=code,
            )
        token = str(payload.get("tenant_access_token", "") or "").strip()
        if not token:
            raise FeishuApiError("飞书没有返回 tenant_access_token")

        lifetime = _token_lifetime(payload.get("expire"))
        self._token = token
        self._token_expires_at = self._clock() + lifetime
        self.token_refreshes += 1
        logger.info("飞书 tenant token 已获取，%.0f 秒后刷新", lifetime)
        return token

    # -- calls --------------------------------------------------------------

    def _call(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self._request(
            method,
            url,
            headers={
                "Authorization": f"Bearer {self.tenant_token()}",
                "Content-Type": "application/json; charset=utf-8",
            },
            params=params,
            json=json,
            timeout=self.timeout,
        )
        return _decode(response)

    def send_action(self, action: Any) -> dict[str, Any]:
        """Perform one action built by the adapter and return the decoded body.

        The body is returned as-is, including a non-zero ``code``: the adapter
        turns that into a send failure, and keeping the decision there is what
        lets the session stay protocol-agnostic.
        """
        name = str(getattr(action, "name", "") or "")
        params = dict(getattr(action, "params", {}) or {})

        if name == ACTION_CREATE_MESSAGE:
            receive_id_type = _required(params, "receive_id_type")
            if receive_id_type not in RECEIVE_ID_TYPES:
                raise FeishuApiError(f"不支持的 receive_id_type：{receive_id_type}")
            return self._call(
                "POST",
                f"{self.base_url}{_MESSAGES_PATH}",
                params={"receive_id_type": receive_id_type},
                json={
                    "receive_id": _required(params, "receive_id"),
                    "msg_type": str(params.get("msg_type") or MSG_TYPE_TEXT),
                    "content": str(params.get("content") or ""),
                },
            )

        if name == ACTION_REPLY_MESSAGE:
            message_id = quote(_required(params, "message_id"), safe="")
            return self._call(
                "POST",
                f"{self.base_url}{_MESSAGES_PATH}/{message_id}/reply",
                json={
                    "msg_type": str(params.get("msg_type") or MSG_TYPE_TEXT),
                    "content": str(params.get("content") or ""),
                },
            )

        if name == ACTION_GET_MESSAGE:
            message_id = quote(_required(params, "message_id"), safe="")
            return self._call("GET", f"{self.base_url}{_MESSAGES_PATH}/{message_id}")

        if name == ACTION_BOT_INFO:
            return self._call("GET", f"{self.base_url}{_BOT_INFO_PATH}")

        raise FeishuApiError(f"未知的飞书动作：{name or '(空)'}")


def _token_lifetime(expire: Any) -> float:
    """Seconds to cache the token for, with the refresh margin already applied.

    An unparseable or absurdly short ``expire`` is not trusted to be long: the
    floor keeps a bad value from producing a token that is stale on arrival.
    """
    try:
        reported = float(expire)
    except (TypeError, ValueError):
        reported = DEFAULT_TOKEN_LIFETIME_SECONDS
    return max(MIN_TOKEN_LIFETIME_SECONDS, reported - TOKEN_REFRESH_MARGIN_SECONDS)


def build_feishu_api(settings: Any, *, request: Callable[..., Any] | None = None) -> FeishuApi:
    """Build the API client from configuration.

    ``domain`` selects the Feishu or Lark endpoint; the two deployments do not
    share a host, and sending to the wrong one fails with a credential error
    that looks like a bad secret.
    """
    domain = str(getattr(settings, "feishu_domain", "feishu") or "feishu").strip().lower()
    base_url = LARK_BASE_URL if domain == "lark" else FEISHU_BASE_URL
    return FeishuApi(
        app_id=str(getattr(settings, "feishu_app_id", "") or ""),
        app_secret=str(getattr(settings, "feishu_app_secret", "") or ""),
        base_url=base_url,
        timeout=float(getattr(settings, "feishu_timeout_seconds", DEFAULT_TIMEOUT_SECONDS) or 0)
        or DEFAULT_TIMEOUT_SECONDS,
        request=request,
    )
