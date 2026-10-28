"""A minimal NapCat stand-in: OneBot v11 forward WebSocket server for replaying scenarios.

It implements only what the official MaiBot QQ adapter (v1.1.8) calls during a chat: profile
detection, login info, member and group lookups, message lookup and message sending. Any other
action is answered as unsupported and listed in ``unsupported`` so gaps show up in reports.
"""

import asyncio
import contextlib
import itertools
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aiohttp import WSMsgType, web

Chat = tuple[str, str]  # ("group", group_id) or ("private", user_id)


@dataclass(frozen=True)
class BotMessage:
    at: float
    chat: Chat
    message_id: int
    text: str
    quote: str | None


class FakeNapCat:
    def __init__(
        self,
        *,
        bot_id: str = "10001",
        nickname: str = "DoraYmon",
        token: str = "",
        heartbeat_seconds: float = 5.0,
    ):
        self.bot_id, self.nickname, self.token = bot_id, nickname, token
        self.heartbeat_seconds = heartbeat_seconds
        self.groups: dict[str, str] = {}
        self.members: dict[str, str] = {}
        self.messages: dict[int, dict[str, Any]] = {}
        self.sent: list[BotMessage] = []
        self.unsupported: set[str] = set()
        self._ids = itertools.count(1000)
        self._clients: set[web.WebSocketResponse] = set()
        self._ready = asyncio.Event()
        self._runner: web.AppRunner | None = None

    async def start(self, host: str = "127.0.0.1", port: int = 3901) -> int:
        app = web.Application()
        app.router.add_get("/{tail:.*}", self._websocket)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port)
        await site.start()
        return site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    async def stop(self) -> None:
        for client in tuple(self._clients):
            await client.close()
        if self._runner is not None:
            await self._runner.cleanup()

    async def wait_ready(self, seconds: float) -> None:
        """Ready means the adapter finished its bootstrap by asking who the bot is."""
        async with asyncio.timeout(seconds):
            await self._ready.wait()

    def add_member(self, user_id: str, nickname: str) -> None:
        self.members[user_id] = nickname

    def add_group(self, group_id: str, name: str) -> None:
        self.groups[group_id] = name

    def last_bot_message(self, chat: Chat) -> int | None:
        return next((m.message_id for m in reversed(self.sent) if m.chat == chat), None)

    async def push_message(
        self,
        chat: Chat,
        user_id: str,
        text: str,
        *,
        mention_bot: bool = False,
        mention_user: str | None = None,
        quote: int | None = None,
    ) -> int:
        if not self._clients:
            raise RuntimeError("回放连接已断开，消息未发送")
        segments: list[dict[str, Any]] = []
        if quote is not None:
            segments.append({"type": "reply", "data": {"id": str(quote)}})
        mentions = [self.bot_id] if mention_bot else []
        mentions += [mention_user] if mention_user else []
        segments += [{"type": "at", "data": {"qq": target}} for target in mentions]
        segments.append({"type": "text", "data": {"text": f" {text}" if mentions else text}})
        event = self._store(chat, user_id, segments)
        await self._broadcast(event)
        return event["message_id"]

    def _store(self, chat: Chat, user_id: str, segments: list[dict[str, Any]]) -> dict[str, Any]:
        message_id = next(self._ids)
        kind, target = chat
        sender = {"user_id": int(user_id), "nickname": self._nickname(user_id)}
        event: dict[str, Any] = {
            "time": int(time.time()),
            "self_id": int(self.bot_id),
            "post_type": "message",
            "message_type": kind,
            "sub_type": "normal" if kind == "group" else "friend",
            "message_id": message_id,
            "real_id": message_id,
            "message_seq": message_id,
            "user_id": int(user_id),
            "message": segments,
            "message_format": "array",
            "raw_message": render_segments(segments, self._nickname),
            "font": 14,
            "sender": sender,
        }
        if kind == "group":
            event["group_id"] = int(target)
            event["group_name"] = self.groups.get(target, "")
            sender.update(card="", role="member")
        self.messages[message_id] = event
        return event

    def _nickname(self, user_id: str) -> str:
        return self.nickname if user_id == self.bot_id else self.members.get(user_id, user_id)

    async def _broadcast(self, payload: Mapping[str, Any]) -> None:
        for client in tuple(self._clients):
            await client.send_json(payload)

    def _authorized(self, request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        return header == f"Bearer {self.token}" or request.query.get("access_token") == self.token

    async def _websocket(self, request: web.Request) -> web.WebSocketResponse:
        if self.token and not self._authorized(request):
            raise web.HTTPUnauthorized()
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        self._clients.add(socket)
        beat = asyncio.create_task(self._heartbeat(socket))
        try:
            await socket.send_json(self._meta("lifecycle", sub_type="connect"))
            async for message in socket:
                if message.type == WSMsgType.TEXT:
                    await self._answer(socket, message.data)
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
            self._clients.discard(socket)
            if not self._clients:
                self._ready.clear()
        return socket

    def _meta(self, kind: str, **fields: Any) -> dict[str, Any]:
        return {
            "time": int(time.time()),
            "self_id": int(self.bot_id),
            "post_type": "meta_event",
            "meta_event_type": kind,
            **fields,
        }

    async def _heartbeat(self, socket: web.WebSocketResponse) -> None:
        while not socket.closed:
            await asyncio.sleep(self.heartbeat_seconds)
            status = {"online": True, "good": True}
            interval = int(self.heartbeat_seconds * 1000)
            await socket.send_json(self._meta("heartbeat", status=status, interval=interval))

    async def _answer(self, socket: web.WebSocketResponse, raw: str) -> None:
        try:
            request = json.loads(raw)
        except ValueError:
            return
        if not isinstance(request, dict) or not isinstance(request.get("action"), str):
            return
        action, params = request["action"], request.get("params") or {}
        handler = getattr(self, f"_action_{action}", None)
        if handler is None:
            self.unsupported.add(action)
            response = {"status": "failed", "retcode": 1404, "data": None, "message": action}
        else:
            response = {"status": "ok", "retcode": 0, "data": handler(params)}
        if "echo" in request:
            response["echo"] = request["echo"]
        await socket.send_json(response)

    def _action_get_version_info(self, params: dict) -> dict:
        return {"app_name": "NapCat.Onebot", "app_version": "replay", "protocol_version": "v11"}

    def _action_get_login_info(self, params: dict) -> dict:
        self._ready.set()
        return {"user_id": int(self.bot_id), "nickname": self.nickname}

    def _action_get_status(self, params: dict) -> dict:
        return {"online": True, "good": True}

    def _action_get_group_info(self, params: dict) -> dict:
        group_id = str(params.get("group_id", ""))
        name = self.groups.get(group_id, "")
        return {
            "group_id": int(group_id or 0),
            "group_name": name,
            "member_count": len(self.members),
        }

    def _action_get_group_list(self, params: dict) -> list[dict]:
        return [self._action_get_group_info({"group_id": group_id}) for group_id in self.groups]

    def _action_get_group_member_info(self, params: dict) -> dict:
        user_id = str(params.get("user_id", ""))
        return {
            "group_id": int(params.get("group_id") or 0),
            "user_id": int(user_id or 0),
            "nickname": self._nickname(user_id),
            "card": "",
            "role": "member",
        }

    def _action_get_group_member_list(self, params: dict) -> list[dict]:
        group_id = params.get("group_id")
        users = [*self.members, self.bot_id]
        return [
            self._action_get_group_member_info({"group_id": group_id, "user_id": u}) for u in users
        ]

    def _action_get_friend_list(self, params: dict) -> list[dict]:
        return [{"user_id": int(u), "nickname": n, "remark": ""} for u, n in self.members.items()]

    def _action_get_stranger_info(self, params: dict) -> dict:
        user_id = str(params.get("user_id", ""))
        return {"user_id": int(user_id or 0), "nickname": self._nickname(user_id)}

    def _action_get_msg(self, params: dict) -> dict | None:
        return self.messages.get(int(params.get("message_id") or 0))

    def _action_send_group_msg(self, params: dict) -> dict:
        return self._record(("group", str(params.get("group_id", ""))), params.get("message"))

    def _action_send_private_msg(self, params: dict) -> dict:
        return self._record(("private", str(params.get("user_id", ""))), params.get("message"))

    def _action_send_msg(self, params: dict) -> dict:
        if params.get("message_type") == "group" or params.get("group_id"):
            return self._action_send_group_msg(params)
        return self._action_send_private_msg(params)

    def _action_delete_msg(self, params: dict) -> None:
        self.messages.pop(int(params.get("message_id") or 0), None)

    def _record(self, chat: Chat, message: Any) -> dict:
        segments = (
            message
            if isinstance(message, list)
            else [{"type": "text", "data": {"text": str(message or "")}}]
        )
        event = self._store(chat, self.bot_id, segments)
        quote = next((str(s["data"].get("id")) for s in segments if s.get("type") == "reply"), None)
        loop_time = asyncio.get_running_loop().time()
        text = render_segments(segments, self._nickname)
        self.sent.append(BotMessage(loop_time, chat, event["message_id"], text, quote))
        return {"message_id": event["message_id"]}


def render_segments(segments: list[dict[str, Any]], nickname: Any) -> str:
    """Plain-text view of OneBot segments for transcripts and reply-length metrics."""
    parts = []
    for segment in segments:
        kind, data = segment.get("type"), segment.get("data") or {}
        if kind == "text":
            parts.append(str(data.get("text", "")))
        elif kind == "at":
            parts.append(f"@{nickname(str(data.get('qq', '')))} ")
        elif kind != "reply":
            parts.append(
                {"face": "[表情]", "image": "[图片]", "record": "[语音]"}.get(kind, f"[{kind}]")
            )
    return "".join(parts).strip()
