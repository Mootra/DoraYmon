"""Explicit, owner-only QQ operations shared by desktop and chat commands."""

import asyncio
import hashlib
import json
import shlex
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from cachetools import TTLCache
from pydantic import BaseModel, ConfigDict, Field

from .admin_store import AdminJournal

Action = Literal[
    "private", "mention", "friend_request", "approve_friend", "friends", "groups", "members"
]
QQ_ID = r"^[1-9][0-9]{4,11}$"
HELP = (
    "/私聊 QQ号 内容\n/群消息 群号 QQ号 内容\n/好友列表\n/群列表\n"
    "/群成员 群号\n/好友申请 请求标识 同意或拒绝\n/机器人状态\n#sl"
)


class AdminRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Action
    target: str = Field(default="", max_length=128)
    group: str = Field(default="", max_length=32)
    text: str = Field(default="", max_length=2000)
    approve: bool = True
    request_id: str = Field(default_factory=lambda: str(uuid4()), min_length=8, max_length=80)

    def validate_target(self) -> None:
        import re

        if self.action in {"private", "mention", "friend_request"}:
            if not re.fullmatch(QQ_ID, self.target):
                raise ValueError("请输入完整的目标 QQ 号。")
        if self.action in {"mention", "members"}:
            if not re.fullmatch(QQ_ID, self.group):
                raise ValueError("请输入完整的群号。")
        if self.action in {"private", "mention"} and not self.text.strip():
            raise ValueError("消息内容不能为空。")
        if self.action == "approve_friend" and not self.target.strip():
            raise ValueError("需要好友申请的请求标识。")


def sender_from_message(message: Any) -> str:
    """Use the host's original message, never a caller-supplied owner parameter."""
    if not isinstance(message, dict):
        return ""
    info = message.get("message_info") or {}
    user = info.get("user_info") or {}
    platform = message.get("platform") or user.get("platform") or info.get("platform")
    return str(user.get("user_id") or "") if platform == "qq" else ""


def parse_command(text: str) -> AdminRequest | None:
    parts = shlex.split(text.strip(), posix=False)
    if not parts:
        return None
    command = parts[0].lstrip("/")
    rest = parts[1:]
    if command == "私聊" and len(rest) >= 2:
        return AdminRequest(action="private", target=rest[0], text=" ".join(rest[1:]))
    if command == "群消息" and len(rest) >= 3:
        return AdminRequest(
            action="mention", group=rest[0], target=rest[1], text=" ".join(rest[2:])
        )
    if command == "加好友" and rest:
        return AdminRequest(action="friend_request", target=rest[0], text=" ".join(rest[1:]))
    if command == "好友申请" and len(rest) == 2 and rest[1] in {"同意", "拒绝"}:
        return AdminRequest(action="approve_friend", target=rest[0], approve=rest[1] == "同意")
    if command == "好友列表" and not rest:
        return AdminRequest(action="friends")
    if command == "群列表" and not rest:
        return AdminRequest(action="groups")
    if command == "群成员" and len(rest) == 1:
        return AdminRequest(action="members", group=rest[0])
    return None


def confirmed_message_id(response: dict[str, Any]) -> str:
    data = response.get("data")
    value = str(data.get("message_id") or "").strip() if isinstance(data, dict) else ""
    return value if value not in {"", "0", "-1", "None"} else ""


class AdminService:
    def __init__(
        self,
        owner: str,
        call: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
        *,
        journal_path: Path | None = None,
    ) -> None:
        self.owner = owner
        self.call = call
        self._lock = asyncio.Lock()
        self._results: TTLCache = TTLCache(maxsize=512, ttl=3600)
        self._history: deque = deque(maxlen=100)
        self._last_write = 0.0
        self._journal = AdminJournal(journal_path) if journal_path else None

    def authorized(self, actor: str) -> bool:
        return bool(self.owner) and actor == self.owner

    @property
    def capabilities(self) -> dict[str, bool]:
        # SnowLuma 2.2.0 publishes handling received requests, not requesting new friends.
        return {"private": True, "mention": True, "approve_friend": True, "friend_request": False}

    def history(self) -> list[dict[str, Any]]:
        return (
            self._journal.history()
            if self._journal
            else [
                {k: v for k, v in item.items() if k != "data"} for item in reversed(self._history)
            ]
        )

    async def execute(self, actor: str, request: AdminRequest) -> dict[str, Any]:
        if not self.authorized(actor):
            raise PermissionError("此操作仅管理员可用。")
        request.validate_target()
        digest = hashlib.sha256(
            json.dumps(request.model_dump(exclude={"request_id"}), sort_keys=True).encode()
        ).hexdigest()
        async with self._lock:
            cached = self._results.get(request.request_id)
            if cached is None and self._journal:
                cached = self._journal.get(request.request_id)
            if cached:
                if cached[0] != digest:
                    raise ValueError("请求标识已用于另一项操作。")
                return dict(cached[1])
            result = {
                "request_id": request.request_id,
                "action": request.action,
                "target": request.target or request.group,
                "state": "running",
                "time": datetime.now(UTC).isoformat(),
            }
            self._results[request.request_id] = (digest, result)
            self._history.append(result)
            if self._journal:
                self._journal.save(digest, result)
        try:
            if request.action == "friend_request":
                result.update(
                    state="unsupported",
                    message="当前 SnowLuma 2.2.0 未提供主动申请加好友接口。可处理对方发来的申请。",
                )
                if self._journal:
                    self._journal.save(digest, result)
                return dict(result)
            action, params = self._operation(request)
            if request.action in {"private", "mention", "approve_friend"}:
                async with self._lock:
                    wait = max(0.0, 0.5 - (time.monotonic() - self._last_write))
                    if wait:
                        await asyncio.sleep(wait)
                    self._last_write = time.monotonic()
            response = await self.call(action, params)
            if response.get("status") != "ok" or response.get("retcode", 0) != 0:
                result.update(
                    state="failed", message="QQ 接入未接受这项操作，请检查目标和接入状态。"
                )
            elif request.action in {"private", "mention"}:
                message_id = confirmed_message_id(response)
                result.update(
                    state="confirmed" if message_id else "uncertain",
                    message="消息已发送。" if message_id else "发送结果尚未确认，请先在 QQ 核对。",
                    message_id=message_id,
                )
            elif request.action == "approve_friend":
                result.update(state="confirmed", message="好友申请已处理。")
            else:
                result.update(state="confirmed", data=self._list_rows(request.action, response))
        except (TimeoutError, OSError, RuntimeError):
            result.update(state="uncertain", message="接入响应未确认，请先核对 QQ，避免重复执行。")
        except asyncio.CancelledError:
            result.update(state="uncertain", message="操作中断，执行结果需要核对。")
            if self._journal:
                self._journal.save(digest, result)
            raise
        if self._journal:
            self._journal.save(digest, result)
        return dict(result)

    @staticmethod
    def _operation(request: AdminRequest) -> tuple[str, dict[str, Any]]:
        if request.action == "private":
            return "send_msg", {
                "message_type": "private",
                "user_id": int(request.target),
                "message": [{"type": "text", "data": {"text": request.text}}],
            }
        if request.action == "mention":
            return "send_msg", {
                "message_type": "group",
                "group_id": int(request.group),
                "message": [
                    {"type": "at", "data": {"qq": request.target}},
                    {"type": "text", "data": {"text": " " + request.text}},
                ],
            }
        if request.action == "approve_friend":
            return "set_friend_add_request", {"flag": request.target, "approve": request.approve}
        if request.action == "friends":
            return "get_friend_list", {}
        if request.action == "groups":
            return "get_group_list", {}
        return "get_group_member_list", {"group_id": int(request.group)}

    @staticmethod
    def _list_rows(action: str, response: dict[str, Any]) -> list[dict[str, str]]:
        rows = response.get("data")
        if not isinstance(rows, list):
            return []
        return [
            {
                "id": str(row.get("group_id") if action == "groups" else row.get("user_id")),
                "name": str(row.get("group_name") if action == "groups" else row.get("nickname")),
                "remark": str(row.get("remark") or row.get("card") or ""),
            }
            for row in rows[:5000]
            if isinstance(row, dict)
        ]
