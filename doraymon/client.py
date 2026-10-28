from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import botpy

from doraymon.config import Settings
from doraymon.identity import PLATFORM_QQ_BOT
from doraymon.router import route_incoming_message

logger = logging.getLogger(__name__)
from services.message_contract import MessageEvent, from_botpy_message
from services.reply_budget import REPLY_BUDGET, quota_key
from services.reply_delivery import (
    MAX_REPLY_LENGTH, DEFAULT_MAX_REPLY_SEGMENTS, DEFAULT_MAX_REPLY_TOTAL_CHARS,
    split_reply,
)
from services.pending_reply import commit_delivered_reply, reply_is_current
from services.send_result import DELIVERED, UNKNOWN, SEND_LOG, SendResult, classify_send_error
from services.session_history import get_session_key
from services.session_revisions import SESSION_REVISIONS


class BotpyMessageValidator:
    """Does the botpy-specific check for whether a message is the bot's own.

    ``owner`` is the client, because only the client knows the logged-in robot's
    openid. The check is a separate object so tests and other entry points can
    replace it without touching the message handling path.
    """

    def is_from_self(self, owner: Any, message: Any, bot_account: str) -> bool:
        author = getattr(message, "author", None)
        author_id = str(
            getattr(author, "member_openid", "") or getattr(author, "id", "") or ""
        )
        robot_openid = str(getattr(getattr(owner, "robot", None), "id", "") or "")
        return bool(author_id and robot_openid and author_id == robot_openid)


DEFAULT_VALIDATOR = BotpyMessageValidator()


class MyClient(botpy.Client):
    def __init__(
        self,
        settings: Settings,
        *args: Any,
        validator: Any = None,
        **kwargs: Any,
    ) -> None:
        if "intents" not in kwargs:
            kwargs["intents"] = botpy.Intents(public_messages=True)
        if "is_sandbox" not in kwargs:
            kwargs["is_sandbox"] = settings.qqbot_sandbox
        super().__init__(*args, **kwargs)
        self.settings = settings
        self.started_at = datetime.now()
        self.validator = validator or BotpyMessageValidator()

    async def on_ready(self) -> None:
        logger.info("DoraYmon 已登录：%s", getattr(self.robot, "name", "unknown"))

    async def on_c2c_message_create(self, message: Any) -> None:
        await self._handle_message(message, is_at_message=False)

    async def on_group_at_message_create(self, message: Any) -> None:
        await self._handle_message(message, is_at_message=True)

    async def on_group_message_create(self, message: Any) -> None:
        await self._handle_message(message, is_at_message=False)

    async def _handle_message(self, message: Any, *, is_at_message: bool) -> None:
        """One inbound path for private and group messages, via the contract."""
        bot_account = self._get_bot_account()
        validator = getattr(self, "validator", None) or DEFAULT_VALIDATOR
        if validator.is_from_self(self, message, bot_account):
            return

        event = from_botpy_message(
            message, bot_account=bot_account, platform=PLATFORM_QQ_BOT,
            is_at_message=is_at_message,
        )
        if not event.text.strip():
            return
        if event.is_group and not event.should_consider_for_reply():
            # An unaddressed group message only counts when it is an explicit command.
            if not event.text.strip().startswith(self.settings.command_prefix):
                return

        context = event.to_bot_context(self.settings, self.started_at)
        fallback = "" if event.is_group else "chat"
        reply = await route_incoming_message(context, fallback_command=fallback)
        if not reply:
            return

        scope_type = "group" if event.is_group else "private"
        decision = self._budget_decision(event, scope_type)
        if not decision.allowed:
            logger.info("按被动回复窗口跳过发送：%s", decision.describe())
            return

        # Sending is bounded up front: a long answer must not spend the whole
        # quota and then discover it cannot finish.
        all_segments = self._split_reply(reply)
        segments = all_segments[: decision.remaining]
        if len(segments) < len(all_segments):
            logger.warning(
                "回复超出被动回复额度，本次只发送 %d 段（%s）",
                len(segments),
                decision.describe(),
            )

        revision = SESSION_REVISIONS.begin(event.session_key)
        delivered: list[str] = []
        try:
            for msg_seq, segment in enumerate(segments, start=1):
                if not reply_is_current(reply) or not SESSION_REVISIONS.is_current(event.session_key, revision):
                    break
                result = await self._send_reply(context, message, segment, msg_seq=msg_seq)
                if result is not None and result.success:
                    delivered.append(segment)
                    self._record_budget_use(event, scope_type)
                    continue
                if result is not None:
                    logger.warning(
                        "回复分段发送失败，停止后续分段：%s（剩余 %d 段未发送）",
                        result.describe(), len(segments) - msg_seq,
                    )
                break
        finally:
            commit_delivered_reply(reply, "".join(delivered))

    def _budget_decision(self, event: MessageEvent, scope_type: str):
        """How many messages may still be sent for this inbound message."""
        from services.reply_budget import ReplyBudgetDecision, REASON_ALLOWED

        if not getattr(self.settings, "qqbot_reply_window_enabled", True):
            return ReplyBudgetDecision(
                allowed=True,
                reason=REASON_ALLOWED,
                remaining=DEFAULT_MAX_REPLY_SEGMENTS,
                limit=DEFAULT_MAX_REPLY_SEGMENTS,
                window_seconds=0,
                detail="被动回复窗口限制已关闭",
            )
        return REPLY_BUDGET.check(
            self._budget_key(event),
            started_at=datetime.now(timezone.utc),
            scope_type=scope_type,
        )

    def _budget_key(self, event: MessageEvent) -> str:
        """Scope the quota to this bot, conversation and message.

        A message id alone is not unique across senders, so two users quoting the
        same id must not share one reply quota.
        """
        scope = event.raw_group_id or event.raw_user_id
        return quota_key(
            f"{event.platform}:{event.bot_account}:{scope}",
            event.message_id,
        )

    def _record_budget_use(self, event: MessageEvent, scope_type: str) -> None:
        if not getattr(self.settings, "qqbot_reply_window_enabled", True):
            return
        REPLY_BUDGET.record(
            self._budget_key(event),
            started_at=datetime.now(timezone.utc),
            scope_type=scope_type,
        )

    async def _send_reply(
        self,
        context: Any,
        message: Any,
        content: str,
        *,
        msg_seq: int,
    ) -> SendResult:
        if context.group_openid:
            return await self._send_group_reply(context, message, content, msg_seq=msg_seq)
        return await self._send_private_reply(context, message, content, msg_seq=msg_seq)

    async def _send_private_reply(
        self, context: Any, message: Any, content: str, *, msg_seq: int = 1,
    ) -> SendResult:
        try:
            await self.api.post_c2c_message(
                openid=context.user_openid,
                msg_type=0,
                msg_id=getattr(message, "id", ""),
                content=content,
                msg_seq=msg_seq,
            )
        except asyncio.CancelledError as error:
            self._record_send(context, msg_seq, error)
            raise
        except Exception as error:
            logger.exception("发送私聊消息失败")
            return self._record_send(context, msg_seq, error)
        return self._record_send(context, msg_seq, None)

    async def _send_group_reply(
        self, context: Any, message: Any, content: str, *, msg_seq: int = 1,
    ) -> SendResult:
        try:
            await self.api.post_group_message(
                group_openid=context.group_openid,
                msg_type=0,
                msg_id=getattr(message, "id", ""),
                content=content,
                msg_seq=msg_seq,
            )
        except asyncio.CancelledError as error:
            self._record_send(context, msg_seq, error)
            raise
        except Exception as error:
            logger.exception("发送群消息失败")
            return self._record_send(context, msg_seq, error)
        return self._record_send(context, msg_seq, None)

    def _record_send(
        self, context: Any, msg_seq: int, error: BaseException | None,
    ) -> SendResult:
        """Record what actually happened; an accepted message cannot be revoked."""
        result = SendResult(
            scope_type="group" if context.group_openid else "private",
            session_key=get_session_key(context),
            segment_index=msg_seq,
            success=error is None,
            recorded_at=datetime.now(),
            error_label="" if error is None else classify_send_error(error),
            outcome=DELIVERED if error is None else UNKNOWN,
        )
        SEND_LOG.record(result)
        return result

    def _get_bot_account(self) -> str:
        """Stable identifier for this bot account, used for identity namespacing."""
        robot_id = str(getattr(getattr(self, "robot", None), "id", "") or "").strip()
        if robot_id:
            return robot_id
        return str(getattr(self.settings, "qqbot_appid", "") or "").strip()

    def _split_reply(self, content: str) -> list[str]:
        return split_reply(
            content,
            max_segments=getattr(self.settings, "reply_max_segments", DEFAULT_MAX_REPLY_SEGMENTS),
            max_total_chars=getattr(
                self.settings, "reply_max_total_chars", DEFAULT_MAX_REPLY_TOTAL_CHARS
            ),
        )
