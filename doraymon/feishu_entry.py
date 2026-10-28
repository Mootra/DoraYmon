"""Feishu entry point.

Wires the long-connection transport to the shared message session: access policy,
routing, replies and the connection lifecycle. Kept separate from ``main.py`` and
from the QQ account entry so each runs with its own configuration and data
directory, as the plan requires.

Nothing here is started unless the entry is explicitly enabled, because it talks
to a real Feishu tenant with real credentials.

The event source is injected rather than constructed here. Every entry point must
be verifiable offline, and the SDK wiring is the one piece that cannot be — so it
stays behind ``services/feishu_sdk.py`` and this module never imports it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from doraymon.identity import PLATFORM_FEISHU
from services.account_policy import AccountAccessPolicy, as_id_list
from services.account_session import (
    SKIP_DUPLICATE,
    SKIP_NOT_ADAPTED,
    SKIP_SELF,
    AccountSession,
)
from services.account_throttle import ThrottlePolicy
from services.feishu_adapter import FeishuAdapter
from services.feishu_api import build_feishu_api
from services.feishu_transport import FeishuLongConnectionTransport

logger = logging.getLogger(__name__)


def build_policy(settings: Any) -> AccountAccessPolicy:
    """Build the allowlist: which conversations and which people are in scope."""
    return AccountAccessPolicy(
        allowed_groups=as_id_list(getattr(settings, "feishu_chats", [])),
        allowed_users=as_id_list(getattr(settings, "feishu_users", [])),
        # Open to everyone is never the default: this entry acts as the app, and
        # an app that answers strangers is a liability rather than a feature.
        allow_all_groups=False,
        allow_all_users=False,
    )


def build_throttle(settings: Any) -> ThrottlePolicy:
    """Self-imposed group limits, read from configuration."""
    return ThrottlePolicy(
        cooldown_seconds=float(
            getattr(settings, "feishu_group_cooldown_seconds", 3.0) or 0.0
        ),
        window_seconds=float(
            getattr(settings, "feishu_group_rate_window_seconds", 60.0) or 60.0
        ),
        max_replies=int(getattr(settings, "feishu_group_max_replies", 8) or 0),
    )


def validate_feishu_settings(settings: Any) -> list[str]:
    """Return configuration problems that must block startup."""
    problems: list[str] = []
    if not str(getattr(settings, "feishu_app_id", "") or "").strip():
        problems.append("缺少 BOT_FEISHU_APP_ID（飞书应用 App ID）")
    if not str(getattr(settings, "feishu_app_secret", "") or "").strip():
        problems.append("缺少 BOT_FEISHU_APP_SECRET（飞书应用 App Secret）")
    if not str(getattr(settings, "feishu_bot_open_id", "") or "").strip():
        # Without the bot's own open_id a group message cannot be recognised as
        # addressed to it. Guessing would be worse than refusing to start.
        problems.append(
            "缺少 BOT_FEISHU_BOT_OPEN_ID（机器人自己的 open_id）："
            "群聊里无法判断是否被 @，会静默无视所有群消息"
        )
    if not build_policy(settings).has_any_allowlist:
        problems.append(
            "允许列表为空：请设置 BOT_FEISHU_CHATS（允许的群 chat_id）"
            "和/或 BOT_FEISHU_USERS（允许的私聊 open_id）"
        )
    return problems


class FeishuRunner:
    """Owns one Feishu session, its transport and its API client."""

    def __init__(
        self,
        *,
        settings: Any,
        transport: Any = None,
        api: Any = None,
        event_source: Any = None,
    ) -> None:
        self.settings = settings
        self.policy = build_policy(settings)
        bot_open_id = str(getattr(settings, "feishu_bot_open_id", "") or "")

        self.api = api if api is not None else build_feishu_api(settings)
        self.session = AccountSession(
            settings=settings,
            transport=None,  # assigned below, once the real transport exists
            bot_account=bot_open_id,
            platform=PLATFORM_FEISHU,
            adapter=FeishuAdapter(bot_account=bot_open_id),
            group_reply_mode=str(
                getattr(settings, "feishu_group_reply_mode", "reply") or "reply"
            ),
            group_throttle=build_throttle(settings),
        )

        if transport is None:
            if event_source is None:
                raise ValueError(
                    "FeishuRunner 需要 event_source 或已构建的 transport："
                    "长连接的 SDK 装配在 services/feishu_sdk.py 里"
                )
            transport = FeishuLongConnectionTransport(
                event_source=event_source,
                sender=self.api,
                on_event=self._on_event,
                on_connected=self._on_connected,
                on_disconnected=self._on_disconnected,
            )
        self.transport = transport
        self.session.transport = transport

    async def _on_event(self, payload: Any) -> None:
        await self.handle_payload(payload)

    def _on_connected(self) -> None:
        if self.session.connected:
            self.session.on_reconnected()
        else:
            self.session.on_connected()

    def _on_disconnected(self) -> None:
        self.session.on_disconnected()

    async def handle_payload(self, payload: Any) -> None:
        """Apply access policy, then hand the event to the session.

        Adaptation happens exactly once: the policy needs the adapted event, and
        re-adapting would consume the duplicate window twice.
        """
        adapter = self.session.adapter
        self.session.stats.received += 1
        if adapter.is_self_message(payload):
            self.session.stats.note_skip(SKIP_SELF)
            return
        event_id = adapter.event_id_of(payload)
        if event_id and adapter.duplicates.seen_before(event_id):
            self.session.stats.note_skip(SKIP_DUPLICATE)
            return

        event = adapter.adapt(payload)
        if event is None:
            self.session.stats.note_skip(SKIP_NOT_ADAPTED)
            return

        decision = self.policy.decide(event)
        if not decision.allowed:
            self.session.stats.note_skip(decision.reason)
            logger.info("飞书消息被访问策略拒绝：%s", decision.describe())
            return

        await self.session.handle_event(event, counted=True)

    async def run(self, *, max_attempts: int | None = None) -> None:
        await self.transport.run_forever(max_attempts=max_attempts)

    async def stop(self) -> None:
        await self.transport.close()
        await self.session.close()


async def run_feishu_entry(
    settings: Any,
    *,
    event_source: Any = None,
    runner: Any = None,
) -> int:
    """Validate configuration and run the Feishu entry until cancelled.

    ``runner`` may be supplied already built, which is how the offline tests
    reach the stop path without a real connection.
    """
    problems = validate_feishu_settings(settings)
    if problems:
        for problem in problems:
            logger.error("飞书入口配置问题：%s", problem)
        return 1

    if runner is None:
        runner = FeishuRunner(settings=settings, event_source=event_source)
    throttle = runner.session.group_throttle
    if throttle.is_enabled:
        throttle_text = (
            f"冷却 {throttle.cooldown_seconds:g} 秒，"
            f"{throttle.window_seconds:g} 秒内最多 {throttle.max_replies} 条"
        )
    else:
        throttle_text = "未启用"
    logger.info(
        "飞书入口启动：机器人 open_id=%s 允许会话=%d 允许用户=%d 群回复模式=%s 群节流=%s",
        settings.feishu_bot_open_id,
        len(runner.policy.allowed_groups),
        len(runner.policy.allowed_users),
        settings.feishu_group_reply_mode,
        throttle_text,
    )
    try:
        await runner.run()
    finally:
        await runner.stop()
    return 0


def main(settings: Any, *, event_source: Any = None) -> int:
    return asyncio.run(run_feishu_entry(settings, event_source=event_source))
