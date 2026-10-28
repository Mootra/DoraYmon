"""Self-imposed account throttling keeps a real account from looking like spam."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest

from doraymon.account_entry import build_throttle
from doraymon.config import Settings
from services.account_session import SKIP_THROTTLED, AccountSession
from services.account_throttle import (
    ACCOUNT_THROTTLE,
    ConversationThrottle,
    DISABLED,
    REASON_ALLOWED,
    REASON_COOLDOWN,
    REASON_RATE_LIMITED,
    ThrottlePolicy,
    policy_for_scope,
)
from services.send_result import SEND_LOG
from tests.test_account_session import FakeTransport, _settings
from tests.test_onebot_adapter import BOT, group_payload


class PolicyTest(unittest.TestCase):
    def test_disabled_policy_is_off(self) -> None:
        self.assertFalse(DISABLED.is_enabled)
        self.assertFalse(ThrottlePolicy().is_enabled)

    def test_either_limit_enables_the_policy(self) -> None:
        self.assertTrue(ThrottlePolicy(cooldown_seconds=1).is_enabled)
        self.assertTrue(ThrottlePolicy(max_replies=1).is_enabled)

    def test_invalid_values_are_rejected(self) -> None:
        for payload in (
            {"cooldown_seconds": -1},
            {"window_seconds": 0},
            {"max_replies": -1},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    ThrottlePolicy(**payload)

    def test_group_policy_applies_only_to_groups(self) -> None:
        group = ThrottlePolicy(cooldown_seconds=5)

        self.assertIs(policy_for_scope(scope_type="group", group_policy=group), group)
        self.assertIs(
            policy_for_scope(scope_type="private", group_policy=group),
            DISABLED,
        )


class CheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.throttle = ConversationThrottle()
        self.now = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)

    def _policy(self, **overrides) -> ThrottlePolicy:
        payload = {"cooldown_seconds": 10.0, "window_seconds": 60.0, "max_replies": 3}
        payload.update(overrides)
        return ThrottlePolicy(**payload)

    def test_first_reply_is_allowed(self) -> None:
        decision = self.throttle.check("g1", self._policy(), now=self.now)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, REASON_ALLOWED)

    def test_cooldown_blocks_until_it_elapses(self) -> None:
        policy = self._policy()
        self.throttle.record("g1", now=self.now)

        during = self.throttle.check("g1", policy, now=self.now + timedelta(seconds=9))
        after = self.throttle.check("g1", policy, now=self.now + timedelta(seconds=10))

        self.assertFalse(during.allowed)
        self.assertEqual(during.reason, REASON_COOLDOWN)
        self.assertAlmostEqual(during.retry_after_seconds, 1.0, places=3)
        self.assertTrue(after.allowed)

    def test_rate_limit_blocks_after_the_configured_count(self) -> None:
        policy = self._policy(cooldown_seconds=0.0, max_replies=2)
        self.throttle.record("g1", now=self.now)
        self.throttle.record("g1", now=self.now + timedelta(seconds=1))

        blocked = self.throttle.check("g1", policy, now=self.now + timedelta(seconds=2))

        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason, REASON_RATE_LIMITED)
        # The earliest send leaves the window at 60 seconds.
        self.assertAlmostEqual(blocked.retry_after_seconds, 58.0, places=3)

    def test_rate_limit_clears_once_the_window_slides(self) -> None:
        policy = self._policy(cooldown_seconds=0.0, max_replies=1)
        self.throttle.record("g1", now=self.now)

        blocked = self.throttle.check("g1", policy, now=self.now + timedelta(seconds=30))
        allowed = self.throttle.check("g1", policy, now=self.now + timedelta(seconds=61))

        self.assertFalse(blocked.allowed)
        self.assertTrue(allowed.allowed)

    def test_conversations_are_independent(self) -> None:
        policy = self._policy()
        self.throttle.record("g1", now=self.now)

        other_group = self.throttle.check("g2", policy, now=self.now)
        other_private = self.throttle.check("u1", policy, now=self.now)

        self.assertTrue(other_group.allowed)
        self.assertTrue(other_private.allowed)

    def test_disabled_policy_never_blocks(self) -> None:
        for _ in range(50):
            self.throttle.record("g1", now=self.now)

        self.assertTrue(self.throttle.check("g1", DISABLED, now=self.now).allowed)

    def test_empty_key_is_never_blocked(self) -> None:
        policy = self._policy()
        self.throttle.record("", now=self.now)

        self.assertTrue(self.throttle.check("", policy, now=self.now).allowed)
        self.assertEqual(len(self.throttle), 0)

    def test_tracking_stays_bounded(self) -> None:
        throttle = ConversationThrottle(max_conversations=2)
        for index in range(10):
            throttle.record(f"g{index}", now=self.now)

        self.assertLessEqual(len(throttle), 2)

    def test_prune_drops_idle_conversations(self) -> None:
        self.throttle.record("old", now=self.now)
        self.throttle.record("recent", now=self.now + timedelta(minutes=10))

        removed = self.throttle.prune(keep_seconds=300, now=self.now + timedelta(minutes=11))

        self.assertEqual(removed, 1)
        self.assertEqual(len(self.throttle), 1)

    def test_describe_explains_both_refusals(self) -> None:
        policy = self._policy(cooldown_seconds=30.0, max_replies=0)
        self.throttle.record("g1", now=self.now)
        cooling = self.throttle.check("g1", policy, now=self.now + timedelta(seconds=5))

        rate_policy = self._policy(cooldown_seconds=0.0, max_replies=1)
        self.throttle.record("g2", now=self.now)
        rated = self.throttle.check("g2", rate_policy, now=self.now + timedelta(seconds=5))

        self.assertIn("冷却", cooling.describe())
        self.assertIn("上限", rated.describe())


class SessionThrottleTest(unittest.TestCase):
    def setUp(self) -> None:
        SEND_LOG.clear()
        ACCOUNT_THROTTLE.clear()

    def tearDown(self) -> None:
        SEND_LOG.clear()
        ACCOUNT_THROTTLE.clear()

    def _session(self, transport=None, **kwargs) -> AccountSession:
        return AccountSession(
            settings=_settings(),
            transport=transport or FakeTransport(),
            bot_account=BOT,
            throttle=ACCOUNT_THROTTLE,
            **kwargs,
        )

    def _patch_router(self, reply: str):
        from unittest.mock import AsyncMock, patch

        return patch(
            "services.account_session.route_incoming_message",
            new=AsyncMock(return_value=reply),
        )

    def test_second_group_reply_is_throttled_by_cooldown(self) -> None:
        transport = FakeTransport()
        session = self._session(transport, group_throttle=ThrottlePolicy(cooldown_seconds=30))

        with self._patch_router("回复"):
            asyncio.run(session.handle_payload(group_payload(message_id=1)))
            asyncio.run(session.handle_payload(group_payload(message_id=2)))

        self.assertEqual(len(transport.actions), 1)
        self.assertEqual(session.stats.skipped.get(SKIP_THROTTLED), 1)

    def test_private_chat_is_not_throttled_by_the_group_policy(self) -> None:
        from tests.test_onebot_adapter import private_payload

        transport = FakeTransport()
        session = self._session(transport, group_throttle=ThrottlePolicy(cooldown_seconds=30))

        with self._patch_router("回复"):
            asyncio.run(session.handle_payload(private_payload(message_id=1)))
            asyncio.run(session.handle_payload(private_payload(message_id=2)))

        self.assertEqual(len(transport.actions), 2)

    def test_rate_limit_stops_a_flood(self) -> None:
        transport = FakeTransport()
        session = self._session(
            transport,
            group_throttle=ThrottlePolicy(cooldown_seconds=0.0, max_replies=2),
        )

        with self._patch_router("回复"):
            for message_id in range(5):
                asyncio.run(session.handle_payload(group_payload(message_id=message_id)))

        self.assertEqual(len(transport.actions), 2)
        self.assertEqual(session.stats.skipped.get(SKIP_THROTTLED), 3)

    def test_failed_send_does_not_consume_the_throttle(self) -> None:
        transport = FakeTransport(retcode=100)
        session = self._session(
            transport,
            group_throttle=ThrottlePolicy(cooldown_seconds=30),
        )

        with self._patch_router("回复"):
            asyncio.run(session.handle_payload(group_payload(message_id=1)))
            asyncio.run(session.handle_payload(group_payload(message_id=2)))

        # Nothing was delivered, so the second attempt is not blocked.
        self.assertEqual(len(transport.actions), 2)
        self.assertIsNone(session.stats.skipped.get(SKIP_THROTTLED))

    def test_default_settings_throttle_groups(self) -> None:
        throttle = build_throttle(Settings())

        self.assertTrue(throttle.is_enabled)
        self.assertEqual(throttle.cooldown_seconds, 0)
        self.assertGreater(throttle.max_replies, 0)
        self.assertEqual(Settings().qq_account_group_participation_cooldown_seconds, 30)
        self.assertEqual(Settings().qq_account_group_participation_max_replies, 2)

    def test_throttle_can_be_disabled_by_configuration(self) -> None:
        throttle = build_throttle(
            Settings(
                qq_account_group_cooldown_seconds=0.0,
                qq_account_group_max_replies=0,
            )
        )

        self.assertFalse(throttle.is_enabled)


if __name__ == "__main__":
    unittest.main()
