"""OneBot v11 WebSocket transport.

This is the only part of the account entry that performs I/O. It connects to an
OneBot implementation (NapCat), decodes inbound payloads, resolves action
responses by their echo field, and reconnects with a bounded backoff. Everything
it learns is handed to the session, which owns the policy; the transport owns
bytes, not behaviour.

Two details matter for correctness on a real account:

- A reply whose response never arrived has an *unknown* outcome. The transport
  raises instead of pretending, so the session records an unknown result rather
  than a success.
- Reconnects are not immediate. Hammering a just-restarted endpoint makes the
  outage longer, so the delay grows and is capped.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
import re
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import aiohttp

from services.onebot_adapter import OneBotAction
from services.reconnect import (
    DEFAULT_BACKOFF_FACTOR,
    DEFAULT_RECONNECT_BASE_SECONDS,
    DEFAULT_RECONNECT_MAX_SECONDS,
    reconnect_delay,
)

logger = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_ACTION_TIMEOUT_SECONDS = 30.0
DEFAULT_HEARTBEAT_TIMEOUT_SECONDS = 90.0
DEFAULT_MAX_EVENT_TASKS = 64
# The reconnect constants and ``reconnect_delay`` are imported above and
# re-exported here: the curve is protocol-neutral, and the Feishu transport
# needs the same one rather than a second copy of it.

# aiohttp puts the whole request URL into connection errors, and this transport's
# URL carries ``?access_token=...``; logging that message verbatim wrote the
# OneBot token to disk on every reconnect attempt. Redaction lives here so every
# log site uses one rule instead of each remembering to be careful.
_SECRET_QUERY_PATTERN = re.compile(
    r"(?i)([?&](?:access_token|token|api_key|apikey|key|secret|password)=)[^&\s'\"<>]*"
)
_SECRET_HEADER_PATTERN = re.compile(r"(?i)(authorization\s*:\s*bearer\s+)\S+")


def redact_secrets(value: Any) -> str:
    """Render ``value`` as text with credentials removed, for logging."""
    text = str(value)
    text = _SECRET_QUERY_PATTERN.sub(r"\1***", text)
    return _SECRET_HEADER_PATTERN.sub(r"\1***", text)


class OneBotTransportError(Exception):
    """The transport could not complete an action."""

    def __init__(self, message: str, *, delivery_unknown: bool = True) -> None:
        super().__init__(message)
        self.delivery_unknown = delivery_unknown


class OneBotAuthError(OneBotTransportError):
    """The endpoint rejected our token."""


def build_ws_url(base_url: str, token: str = "") -> str:
    """Append the access token the way OneBot implementations expect it."""
    url = str(base_url or "").strip()
    if not url:
        raise ValueError("ws url must not be empty")
    normalized = str(token or "").strip()
    if not normalized:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}access_token={quote(normalized, safe='')}"


class OneBotWebSocketTransport:
    """Bidirectional OneBot connection over aiohttp.

    ``send_action`` is only valid while connected; callers that need a reply use
    the session, which handles the disconnected case.
    """

    def __init__(
        self,
        *,
        ws_url: str,
        token: str = "",
        on_event: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        on_connected: Callable[[], None] | None = None,
        on_disconnected: Callable[[], None] | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        action_timeout: float = DEFAULT_ACTION_TIMEOUT_SECONDS,
        heartbeat_timeout: float = DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
        session_factory: Callable[[], Any] | None = None,
        max_event_tasks: int = DEFAULT_MAX_EVENT_TASKS,
    ) -> None:
        self.base_url = str(ws_url or "").strip()
        self.token = str(token or "").strip()
        self.on_event = on_event
        self.on_connected = on_connected
        self.on_disconnected = on_disconnected
        self.connect_timeout = float(connect_timeout)
        self.action_timeout = float(action_timeout)
        self.heartbeat_timeout = float(heartbeat_timeout)
        self._session_factory = session_factory or aiohttp.ClientSession
        self.max_event_tasks = max(1, int(max_event_tasks))
        self._event_tasks: set[asyncio.Task] = set()
        self.refused_events = 0

        self._session: Any = None
        self._ws: Any = None
        self._stop = asyncio.Event()
        self._reader: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._echo_counter = 0
        self._send_lock = asyncio.Lock()
        self._closed = False
        self.last_heartbeat_at: datetime | None = None
        self.reconnect_attempts = 0
        self.idle_reconnects = 0
        # Liveness is measured on frames, not on heartbeats: a peer that has
        # stopped sending anything at all is indistinguishable from a dead link.
        self._last_frame_at: float = 0.0

    @property
    def url(self) -> str:
        return build_ws_url(self.base_url, self.token)

    @property
    def connected(self) -> bool:
        return (
            self._ws is not None
            and not self._closed
            and not getattr(self._ws, "closed", False)
        )

    @property
    def pending_actions(self) -> int:
        return len(self._pending)

    @property
    def pending_events(self) -> int:
        return len(self._event_tasks)

    def seconds_since_last_frame(self) -> float:
        if not self._last_frame_at:
            return 0.0
        return max(0.0, asyncio.get_event_loop().time() - self._last_frame_at)

    # -- lifecycle ----------------------------------------------------------

    async def connect(self) -> None:
        if self._session is None:
            self._session = self._session_factory()
        self._ws = await self._session.ws_connect(
            self.url, timeout=aiohttp.ClientTimeout(total=self.connect_timeout)
        )
        logger.info("QQ 账号 OneBot 连接已建立：%s", self.base_url)
        if self.on_connected is not None:
            self.on_connected()

    def request_stop(self) -> None:
        """Ask the run loop to finish. Safe to call from a signal handler."""
        self._stop.set()

    def should_stop(self) -> bool:
        return self._closed or self._stop.is_set()

    async def close(self) -> None:
        self._closed = True
        self._stop.set()
        await self._cancel_reader()
        await self._cancel_event_tasks()
        self._fail_pending(OneBotTransportError("connection closed"))
        ws, self._ws = self._ws, None
        if ws is not None:
            await ws.close()
        session, self._session = self._session, None
        if session is not None:
            await session.close()
        if self.on_disconnected is not None:
            self.on_disconnected()

    # -- outbound -----------------------------------------------------------

    def _next_echo(self, action: OneBotAction) -> str:
        self._echo_counter += 1
        return action.echo or f"doraymon-{self._echo_counter}"

    async def send_action(self, action: OneBotAction) -> dict[str, Any]:
        """Send one action and wait for its response.

        Raises ``OneBotTransportError`` when the connection is gone or the
        response never arrives. An unknown outcome must never look like success.
        """
        if not self.connected:
            raise OneBotTransportError("not connected", delivery_unknown=False)
        echo = self._next_echo(action)
        payload = {"action": action.action, "params": dict(action.params), "echo": echo}
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[echo] = future
        try:
            async with self._send_lock:
                await self._ws.send_str(json.dumps(payload, ensure_ascii=False))
            return await asyncio.wait_for(future, timeout=self.action_timeout)
        except asyncio.TimeoutError as error:
            raise OneBotTransportError(f"action {action.action} timed out") from error
        finally:
            self._pending.pop(echo, None)

    # -- inbound ------------------------------------------------------------

    async def listen_once(self, *, timeout: float | None = None) -> str:
        """Read one frame and route it.

        Returns the frame disposition (``event``, ``heartbeat``, ``closed``, ...)
        or ``"idle"`` when the wait timed out. The caller decides whether a close
        or an idle connection should end the session; the transport does not
        loop on its own, because a closed socket would otherwise spin forever.
        """
        bound = self.heartbeat_timeout if timeout is None else float(timeout)
        # The read runs as its own task so a shutdown can cancel it instead of
        # waiting out the whole idle bound. With a real socket, closing the
        # connection also wakes the read; this covers transports that do not.
        self._reader = asyncio.ensure_future(self._ws.receive())
        try:
            message = await asyncio.wait_for(self._reader, timeout=bound)
        except asyncio.TimeoutError:
            return "idle"
        except asyncio.CancelledError:
            return "closed"
        finally:
            self._reader = None
        return await self.dispatch_frame(message)

    async def dispatch_frame(self, message: Any) -> str:
        """Route one already-received frame. Returns what it was.

        Kept separate from the socket so every branch is testable offline:
        ``event``, ``action_response``, ``heartbeat``, ``heartbeat_ack``,
        ``ignored`` or ``closed``.
        """
        frame_type = getattr(message, "type", None)
        if frame_type in (
            aiohttp.WSMsgType.TEXT,
            aiohttp.WSMsgType.BINARY,
            aiohttp.WSMsgType.PING,
            aiohttp.WSMsgType.PONG,
        ):
            self._last_frame_at = asyncio.get_running_loop().time()
        if frame_type == aiohttp.WSMsgType.TEXT:
            return self._handle_json(message.data)
        if frame_type == aiohttp.WSMsgType.ERROR:
            raise OneBotTransportError(f"websocket error: {getattr(self._ws, 'exception', None)}")
        if frame_type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
            self._fail_pending(OneBotTransportError("connection closed"))
            if self.on_disconnected is not None:
                self.on_disconnected()
            return "closed"
        if frame_type in (aiohttp.WSMsgType.BINARY,):
            return self._handle_json(self._decode_bytes(message.data))
        return "ignored"

    def _handle_json(self, raw: Any) -> str:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("收到无法解析的 OneBot 帧，已忽略")
            return "ignored"
        if not isinstance(payload, dict):
            return "ignored"

        echo = payload.get("echo")
        if echo is not None and str(echo) in self._pending:
            future = self._pending[str(echo)]
            if not future.done():
                future.set_result(payload)
            return "action_response"

        post_type = payload.get("post_type")
        if post_type == "meta_event":
            if str(payload.get("meta_event_type", "")) == "heartbeat":
                self.last_heartbeat_at = datetime.now()
                return "heartbeat"
            return "ignored"
        if post_type is not None:
            self._schedule_event(payload)
            return "event"
        if "status" in payload or "retcode" in payload:
            # A response whose echo we no longer track (for example after a
            # reconnect). Dropping it is correct: the caller already gave up.
            return "ignored"
        return "ignored"

    def _schedule_event(self, payload: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        if self.should_stop() or len(self._event_tasks) >= self.max_event_tasks:
            self.refused_events += 1
            logger.warning("OneBot 事件未入队：关闭或积压已达上限")
            return
        result = self.on_event(payload)
        if asyncio.iscoroutine(result):
            task = asyncio.ensure_future(result)
            self._event_tasks.add(task)
            task.add_done_callback(self._event_tasks.discard)
            task.add_done_callback(self._log_task_failure)

    async def _cancel_event_tasks(self) -> None:
        tasks = tuple(task for task in self._event_tasks if task is not asyncio.current_task())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _log_task_failure(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("处理 OneBot 事件失败：%s", redact_secrets(error))

    @staticmethod
    def _decode_bytes(data: Any) -> str:
        if isinstance(data, (bytes, bytearray)):
            return data.decode("utf-8", errors="replace")
        return str(data or "")

    def _peer_looks_dead(self) -> bool:
        if self.heartbeat_timeout <= 0:
            return False
        return self.seconds_since_last_frame() >= self.heartbeat_timeout

    async def _cancel_reader(self) -> None:
        """Stop an in-flight read so shutdown does not wait for the read bound."""
        reader, self._reader = self._reader, None
        if reader is None or reader.done():
            return
        reader.cancel()
        try:
            await reader
        except (asyncio.CancelledError, Exception):
            pass

    def _fail_pending(self, error: BaseException) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    # -- run loop -----------------------------------------------------------

    async def run_forever(
        self,
        *,
        max_attempts: int | None = None,
        reconnect_base_seconds: float = DEFAULT_RECONNECT_BASE_SECONDS,
        reconnect_max_seconds: float = DEFAULT_RECONNECT_MAX_SECONDS,
    ) -> None:
        try:
            await self._run_forever(
                max_attempts=max_attempts,
                reconnect_base_seconds=reconnect_base_seconds,
                reconnect_max_seconds=reconnect_max_seconds,
            )
        finally:
            await self._cancel_event_tasks()

    async def _run_forever(
        self,
        *,
        max_attempts: int | None = None,
        reconnect_base_seconds: float = DEFAULT_RECONNECT_BASE_SECONDS,
        reconnect_max_seconds: float = DEFAULT_RECONNECT_MAX_SECONDS,
    ) -> None:
        """Connect, listen, and reconnect with capped backoff until closed."""
        attempt = 0
        while not self.should_stop():
            try:
                await self.connect()
                attempt = 0
                self.reconnect_attempts = 0
                self._last_frame_at = asyncio.get_running_loop().time()
                while self.connected and not self.should_stop():
                    disposition = await self.listen_once()
                    if disposition == "closed":
                        # The peer closed the connection: leave the inner loop so
                        # the outer loop reconnects. Without this the closed
                        # socket is read again immediately and starves the loop.
                        break
                    if disposition == "idle" and self._peer_looks_dead():
                        # Nothing at all arrived for a whole heartbeat window, so
                        # treat the link as dead and reconnect rather than
                        # waiting on a connection that may never speak again.
                        self.idle_reconnects += 1
                        logger.warning(
                            "QQ 账号连接 %.0f 秒无任何帧，按失效处理并重连",
                            self.seconds_since_last_frame(),
                        )
                        break
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if self.should_stop():
                    break
                self.reconnect_attempts += 1
                delay = reconnect_delay(
                    attempt,
                    base_seconds=reconnect_base_seconds,
                    max_seconds=reconnect_max_seconds,
                )
                logger.warning(
                    "QQ 账号连接中断（%s），%.1f 秒后重试",
                    redact_secrets(error),
                    delay,
                )
                attempt += 1
                if max_attempts is not None and attempt >= max_attempts:
                    logger.error("QQ 账号重连次数已达上限，停止")
                    return
                # Waiting on the stop signal means shutdown is immediate even
                # in the middle of a long backoff.
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
            finally:
                await self._cancel_reader()
                self._ws = None
                self._fail_pending(OneBotTransportError("connection lost"))
