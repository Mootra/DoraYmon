from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import botpy

from doraymon.config import Settings
from doraymon.context import BotContext
from doraymon.router import route_incoming_message

logger = logging.getLogger(__name__)
MAX_REPLY_LENGTH = 1800


class MyClient(botpy.Client):
    def __init__(self, settings: Settings, *args: Any, **kwargs: Any) -> None:
        if "intents" not in kwargs:
            kwargs["intents"] = botpy.Intents(public_messages=True)
        if "is_sandbox" not in kwargs:
            kwargs["is_sandbox"] = settings.qqbot_sandbox
        super().__init__(*args, **kwargs)
        self.settings = settings
        self.started_at = datetime.now()

    async def on_ready(self) -> None:
        logger.info("DoraYmon 已登录：%s", getattr(self.robot, "name", "unknown"))

    async def on_c2c_message_create(self, message: Any) -> None:
        await self._handle_private_message(message)

    async def _handle_private_message(self, message: Any) -> None:
        if self._is_from_self(message):
            return

        content = str(getattr(message, "content", "") or "").strip()
        if not content:
            return

        context = BotContext(
            settings=self.settings,
            started_at=self.started_at,
            user_openid=self._get_user_openid(message),
            group_openid="",
            raw_content=content,
            is_at_message=False,
            message=message,
        )

        reply = await route_incoming_message(context, fallback_command="chat")
        if reply:
            await self._send_private_reply(message, self._limit_reply(reply))

    async def _send_private_reply(self, message: Any, content: str) -> None:
        try:
            await self.api.post_c2c_message(
                openid=self._get_user_openid(message),
                msg_type=0,
                msg_id=getattr(message, "id", ""),
                content=content,
            )
        except Exception:
            logger.exception("发送私聊消息失败")

    async def on_group_at_message_create(self, message: Any) -> None:
        await self._handle_group_message(message, is_at_message=True)

    async def on_group_message_create(self, message: Any) -> None:
        await self._handle_group_message(message, is_at_message=False)

    async def _handle_group_message(self, message: Any, is_at_message: bool) -> None:
        if self._is_from_self(message):
            return

        content = str(getattr(message, "content", "") or "").strip()
        if not content:
            return

        if not is_at_message and not content.startswith(self.settings.command_prefix):
            return

        context = BotContext(
            settings=self.settings,
            started_at=self.started_at,
            user_openid=self._get_user_openid(message),
            group_openid=str(getattr(message, "group_openid", "") or ""),
            raw_content=content,
            is_at_message=is_at_message,
            message=message,
        )

        reply = await route_incoming_message(context)
        if reply:
            await self._send_group_reply(message, self._limit_reply(reply))

    async def _send_group_reply(self, message: Any, content: str) -> None:
        try:
            await self.api.post_group_message(
                group_openid=getattr(message, "group_openid", ""),
                msg_type=0,
                msg_id=getattr(message, "id", ""),
                content=content,
            )
        except Exception:
            logger.exception("发送群消息失败")

    def _is_from_self(self, message: Any) -> bool:
        author = getattr(message, "author", None)
        author_id = str(
            getattr(author, "member_openid", "") or getattr(author, "id", "") or ""
        )
        robot_openid = str(getattr(getattr(self, "robot", None), "id", "") or "")
        return bool(author_id and robot_openid and author_id == robot_openid)

    def _get_user_openid(self, message: Any) -> str:
        author = getattr(message, "author", None)
        return str(
            getattr(author, "member_openid", "")
            or getattr(author, "user_openid", "")
            or getattr(author, "id", "")
            or ""
        )

    def _limit_reply(self, content: str) -> str:
        if len(content) <= MAX_REPLY_LENGTH:
            return content
        return content[:MAX_REPLY_LENGTH] + "\n\n[回复过长，已截断]"
