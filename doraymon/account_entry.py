"""QQ account (OneBot) entry point.

Wires an OneBot WebSocket transport to the account session: access policy,
routing, replies and the connection lifecycle. Kept separate from ``main.py`` so
the official bot and the account entry can run independently, each with its own
configuration and data directory, as the plan requires.

Nothing here is started unless the account entry is explicitly enabled, because
it drives a real QQ account.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from services.account_policy import AccountAccessPolicy, as_id_list as _as_id_list
from services.computer_control_guard import claimed_by_computer_bridge
from services.account_throttle import ThrottlePolicy
from services.account_session import (
    SKIP_DUPLICATE,
    SKIP_NOT_ADAPTED,
    SKIP_SELF,
    AccountSession,
)
from services.onebot_transport import OneBotWebSocketTransport
from services.group_dialogue import DEFAULT_BOT_NAMES, GroupDialogue

logger = logging.getLogger(__name__)


def build_policy(settings: Any) -> AccountAccessPolicy:
    """QQ chat admits incoming people; allowlist retains the closed test mode."""
    groups = _as_id_list(getattr(settings, "qq_account_groups", []))
    friends = _as_id_list(getattr(settings, "qq_account_friends", []))
    chat_mode = getattr(settings, "qq_account_access_mode", "chat") == "chat"
    return AccountAccessPolicy(
        allowed_groups=groups,
        allowed_users=friends,
        allow_all_groups=chat_mode and not groups,
        allow_all_users=chat_mode,
    )


def build_throttle(settings: Any) -> ThrottlePolicy:
    """Self-imposed group limits, read from configuration."""
    return ThrottlePolicy(
        cooldown_seconds=float(
            getattr(settings, "qq_account_group_cooldown_seconds", 0.0) or 0.0
        ),
        window_seconds=float(
            getattr(settings, "qq_account_group_rate_window_seconds", 60.0) or 60.0
        ),
        max_replies=int(getattr(settings, "qq_account_group_max_replies", 8) or 0),
    )


def validate_account_settings(settings: Any) -> list[str]:
    """Return configuration problems that must block startup."""
    problems: list[str] = []
    if getattr(settings, "qq_account_access_mode", "chat") not in {"chat", "allowlist"}:
        problems.append("BOT_QQ_ACCOUNT_ACCESS_MODE 必须为 chat 或 allowlist")
    if not str(getattr(settings, "qq_account_self_id", "") or "").strip():
        problems.append("缺少 BOT_QQ_ACCOUNT_SELF_ID（机器人 QQ 号），无法区分身份")
    url = str(getattr(settings, "qq_account_ws_url", "") or "").strip()
    if not url:
        problems.append("缺少 BOT_QQ_ACCOUNT_WS_URL（OneBot WebSocket 地址）")
    elif not url.startswith(("ws://", "wss://")):
        problems.append("BOT_QQ_ACCOUNT_WS_URL 必须以 ws:// 或 wss:// 开头")
    if not build_policy(settings).has_any_allowlist:
        problems.append(
            "允许列表为空：请设置 BOT_QQ_ACCOUNT_GROUPS（允许的群号）"
            "和/或 BOT_QQ_ACCOUNT_FRIENDS（允许的私聊 QQ 号）"
        )
    return problems


class AccountRunner:
    """Owns one account session and its transport."""

    def __init__(self, *, settings: Any, transport: Any = None) -> None:
        self.settings = settings
        self.policy = build_policy(settings)
        self.session = AccountSession(
            settings=settings,
            transport=transport,  # replaced below once the real transport exists
            bot_account=str(getattr(settings, "qq_account_self_id", "") or ""),
            group_reply_mode=str(
                getattr(settings, "qq_account_group_reply_mode", "reply") or "reply"
            ),
            group_throttle=build_throttle(settings),
            group_dialogue=GroupDialogue(
                enabled=bool(getattr(settings, "qq_account_group_participation_enabled", True)),
                bot_names=getattr(settings, "qq_account_bot_names", DEFAULT_BOT_NAMES),
            ),
            participation_throttle=ThrottlePolicy(
                cooldown_seconds=float(getattr(settings, "qq_account_group_participation_cooldown_seconds", 30.0)),
                max_replies=int(getattr(settings, "qq_account_group_participation_max_replies", 2)),
                window_seconds=60.0,
            ),
        )
        self.transport = transport or OneBotWebSocketTransport(
            ws_url=str(getattr(settings, "qq_account_ws_url", "") or ""),
            token=str(getattr(settings, "qq_account_token", "") or ""),
            on_event=self._on_event,
            on_connected=self._on_connected,
            on_disconnected=self._on_disconnected,
        )
        # The session sends through the real transport.
        self.session.transport = self.transport

    async def _on_event(self, payload: dict[str, Any]) -> None:
        await self.handle_payload(payload)

    def _on_connected(self) -> None:
        if self.session.connected:
            self.session.on_reconnected()
        else:
            self.session.on_connected()

    def _on_disconnected(self) -> None:
        self.session.on_disconnected()

    async def handle_payload(self, payload: dict[str, Any]) -> None:
        """Apply access policy, then hand the event to the session.

        Adaptation happens exactly once: the policy needs the adapted event, and
        re-adapting would consume the duplicate window twice.
        """
        if claimed_by_computer_bridge(payload):
            return
        self.session.stats.received += 1
        if self.session.adapter.is_self_message(payload):
            self.session.stats.note_skip(SKIP_SELF)
            return
        event_id = self.session.adapter.event_id_of(payload)
        if event_id and self.session.adapter.duplicates.seen_before(event_id):
            self.session.stats.note_skip(SKIP_DUPLICATE)
            return

        event = self.session.adapter.adapt(payload)
        if event is None:
            self.session.stats.note_skip(SKIP_NOT_ADAPTED)
            return

        decision = self.policy.decide(event)
        if not decision.allowed:
            self.session.stats.note_skip(decision.reason)
            logger.info("QQ 账号消息被访问策略拒绝：%s", decision.describe())
            return

        await self.session.handle_event(event, counted=True)

    async def run(self, *, max_attempts: int | None = None) -> None:
        await self.transport.run_forever(max_attempts=max_attempts)

    async def stop(self) -> None:
        await self.transport.close()
        await self.session.close()


async def run_account_entry(settings: Any) -> int:
    """Validate configuration and run the account entry until cancelled."""
    problems = validate_account_settings(settings)
    if problems:
        for problem in problems:
            logger.error("QQ 账号入口配置问题：%s", problem)
        return 1

    runner = AccountRunner(settings=settings)
    throttle = runner.session.group_throttle
    if throttle.is_enabled:
        throttle_text = (
            f"冷却 {throttle.cooldown_seconds:g} 秒，"
            f"{throttle.window_seconds:g} 秒内最多 {throttle.max_replies} 条"
        )
    else:
        throttle_text = "未启用"
    logger.info(
        "QQ 账号入口启动：账号=%s 允许群=%d 允许好友=%d 访问模式=%s 群范围=%s 私聊范围=%s 普通群接话=%s 群回复模式=%s 直接请求节流=%s",
        settings.qq_account_self_id,
        len(runner.policy.allowed_groups),
        len(runner.policy.allowed_users),
        getattr(settings, "qq_account_access_mode", "chat"),
        "已加入的群" if runner.policy.allow_all_groups else "群列表",
        "所有来信" if runner.policy.allow_all_users else "好友列表",
        runner.session.group_dialogue.enabled,
        settings.qq_account_group_reply_mode,
        throttle_text,
    )
    try:
        await runner.run()
    finally:
        await runner.stop()
    return 0


def main(settings: Any) -> int:
    return asyncio.run(run_account_entry(settings))
