"""A real OneBot v11 WebSocket server, for tests and for live verification.

Mocks prove the orchestrator's logic; they cannot prove the transport works over
an actual socket. This harness speaks OneBot over a real aiohttp WebSocket, so a
test can drive the real ``OneBotWebSocketTransport`` and observe the action frames
the bot actually sends.

It doubles as the verification tool for a live run: point it at a port, connect
the bot, and watch what the bot would have sent while NapCat is not yet available.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
from typing import Any

from aiohttp import web


@dataclass
class RecordedAction:
    """One action frame the client sent, with the reply the server gave."""

    action: str
    params: dict[str, Any]
    echo: str


@dataclass
class OneBotTestServer:
    """An in-process OneBot endpoint that records what the bot sends."""

    host: str = "127.0.0.1"
    port: int = 0
    token: str = ""
    # Replies the server returns for each action, keyed by action name.
    responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    actions: list[RecordedAction] = field(default_factory=list)
    connections: int = 0
    rejected: int = 0
    _runner: Any = None
    _websockets: list[Any] = field(default_factory=list)
    _action_event: asyncio.Event = field(default_factory=asyncio.Event)
    _connected_event: asyncio.Event = field(default_factory=asyncio.Event)

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        # Resolve the port the OS actually assigned.
        for sock in site._server.sockets:  # type: ignore[union-attr]
            self.port = sock.getsockname()[1]
            break
        return self.url

    async def stop(self) -> None:
        for socket in list(self._websockets):
            await socket.close()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    # -- driving ------------------------------------------------------------

    async def wait_for_connection(self, timeout: float = 5.0) -> bool:
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_for_action(self, action: str = "", timeout: float = 5.0) -> RecordedAction | None:
        """Wait until an action (optionally of one name) has been recorded."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            match = next(
                (item for item in self.actions if not action or item.action == action), None
            )
            if match is not None:
                return match
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            self._action_event.clear()
            try:
                await asyncio.wait_for(self._action_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return None

    async def push_event(self, payload: dict[str, Any]) -> bool:
        """Send one event frame to every connected client."""
        if not self._websockets:
            return False
        text = json.dumps(payload, ensure_ascii=False)
        for socket in list(self._websockets):
            await socket.send_str(text)
        return True

    def action_names(self) -> list[str]:
        return [item.action for item in self.actions]

    def actions_named(self, action: str) -> list[RecordedAction]:
        return [item for item in self.actions if item.action == action]

    def clear(self) -> None:
        self.actions.clear()

    # -- internals ----------------------------------------------------------

    async def _handle(self, request: web.Request) -> web.WebSocketResponse:
        if self.token:
            header = request.headers.get("Authorization", "")
            if header != f"Bearer {self.token}":
                self.rejected += 1
                return web.Response(status=401)
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        self.connections += 1
        self._websockets.append(socket)
        self._connected_event.set()
        try:
            async for message in socket:
                if message.type is not web.WSMsgType.TEXT:
                    continue
                await self._handle_frame(socket, message.data)
        finally:
            if socket in self._websockets:
                self._websockets.remove(socket)
        return socket

    async def _handle_frame(self, socket: web.WebSocketResponse, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except ValueError:
            return
        if not isinstance(payload, dict):
            return
        action = str(payload.get("action", "") or "")
        params = payload.get("params")
        echo = str(payload.get("echo", "") or "")
        self.actions.append(
            RecordedAction(
                action=action,
                params=params if isinstance(params, dict) else {},
                echo=echo,
            )
        )
        self._action_event.set()
        reply = self.responses.get(action, {"status": "ok", "retcode": 0, "data": {}})
        await socket.send_str(
            json.dumps({**reply, "echo": echo}, ensure_ascii=False)
        )


def private_message_event(
    *,
    user_id: int = 10001,
    message: str = "你好",
    message_id: int = 1,
    self_id: int = 20002,
) -> dict[str, Any]:
    """A minimal private message event, shaped like a real OneBot push."""
    return {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": message_id,
        "user_id": user_id,
        "self_id": self_id,
        "time": 1760000000,
        "raw_message": message,
        "message": [{"type": "text", "data": {"text": message}}],
        "sender": {"user_id": user_id, "nickname": "测试用户"},
    }


def group_message_event(
    *,
    group_id: int = 30003,
    user_id: int = 10001,
    message: str = "你好",
    message_id: int = 2,
    self_id: int = 20002,
    at_self: bool = True,
) -> dict[str, Any]:
    """A group message event; ``at_self`` adds the mention segment."""
    segments: list[dict[str, Any]] = []
    if at_self:
        segments.append({"type": "at", "data": {"qq": str(self_id)}})
    segments.append({"type": "text", "data": {"text": message}})
    return {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": message_id,
        "group_id": group_id,
        "user_id": user_id,
        "self_id": self_id,
        "time": 1760000000,
        "raw_message": message,
        "message": segments,
        "sender": {"user_id": user_id, "nickname": "测试用户", "role": "member"},
    }
