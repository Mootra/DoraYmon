"""The official platform limits are hard boundaries, so they get exact tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from doraymon.client import MyClient
from doraymon.config import Settings
from services.reply_budget import (
    GROUP_MAX_REPLIES,
    GROUP_WINDOW_SECONDS,
    PRIVATE_MAX_REPLIES,
    PRIVATE_WINDOW_SECONDS,
    REASON_ALLOWED,
    REASON_QUOTA_EXHAUSTED,
    REASON_UNKNOWN_SCOPE,
    REASON_WINDOW_EXPIRED,
    PassiveReplyBudget,
    REPLY_BUDGET,
    limits_for,
    quota_key,
    time_left_in_window,
)
from services.send_result import SEND_LOG


class LimitsTest(unittest.TestCase):
    def test_private_limits_match_the_platform(self) -> None:
        self.assertEqual(limits_for("private"), (PRIVATE_WINDOW_SECONDS, PRIVATE_MAX_REPLIES))
        self.assertEqual(PRIVATE_WINDOW_SECONDS, 3600)
        self.assertEqual(PRIVATE_MAX_REPLIES, 4)

    def test_group_limits_match_the_platform(self) -> None:
        self.assertEqual(limits_for("group"), (GROUP_WINDOW_SECONDS, GROUP_MAX_REPLIES))
        self.assertEqual(GROUP_WINDOW_SECONDS, 300)
        self.assertEqual(GROUP_MAX_REPLIES, 5)

    def test_unknown_scope_has_no_limits(self) -> None:
        self.assertIsNone(limits_for("guild"))
        self.assertIsNone(limits_for(""))

    def test_case_and_whitespace_are_tolerated(self) -> None:
        self.assertEqual(limits_for(" Group "), (GROUP_WINDOW_SECONDS, GROUP_MAX_REPLIES))


class WindowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.budget = PassiveReplyBudget()
        self.now = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)

    def test_fresh_window_allows_the_full_quota(self) -> None:
        decision = self.budget.check(
            "m1", started_at=self.now, scope_type="private", now=self.now
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.remaining, PRIVATE_MAX_REPLIES)
        self.assertEqual(decision.limit, PRIVATE_MAX_REPLIES)
        self.assertEqual(decision.reason, REASON_ALLOWED)

    def test_private_window_closes_after_exactly_60_minutes(self) -> None:
        just_inside = self.now + timedelta(seconds=PRIVATE_WINDOW_SECONDS - 1)

        allowed = self.budget.check("m1", started_at=self.now, scope_type="private", now=just_inside)
        expired = self.budget.check(
            "m1",
            started_at=self.now,
            scope_type="private",
            now=self.now + timedelta(seconds=PRIVATE_WINDOW_SECONDS),
        )

        self.assertTrue(allowed.allowed)
        self.assertFalse(expired.allowed)
        self.assertEqual(expired.reason, REASON_WINDOW_EXPIRED)

    def test_group_window_closes_after_exactly_5_minutes(self) -> None:
        just_inside = self.now + timedelta(seconds=GROUP_WINDOW_SECONDS - 1)

        allowed = self.budget.check("m1", started_at=self.now, scope_type="group", now=just_inside)
        expired = self.budget.check(
            "m1",
            started_at=self.now,
            scope_type="group",
            now=self.now + timedelta(seconds=GROUP_WINDOW_SECONDS),
        )

        self.assertTrue(allowed.allowed)
        self.assertFalse(expired.allowed)

    def test_time_left_is_negative_after_the_window(self) -> None:
        past = self.now - timedelta(seconds=10)

        self.assertLess(time_left_in_window(past, 5, self.now), 0)

    def test_unknown_scope_is_refused(self) -> None:
        decision = self.budget.check("m1", started_at=self.now, scope_type="guild", now=self.now)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, REASON_UNKNOWN_SCOPE)


class QuotaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.budget = PassiveReplyBudget()
        self.now = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)

    def _spend(self, message_id: str, count: int, scope: str = "private") -> None:
        for _ in range(count):
            self.budget.record(
                message_id, started_at=self.now, scope_type=scope, now=self.now
            )

    def test_spending_the_whole_quota_blocks_further_sends(self) -> None:
        self._spend("m1", PRIVATE_MAX_REPLIES)

        decision = self.budget.check("m1", started_at=self.now, scope_type="private", now=self.now)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, REASON_QUOTA_EXHAUSTED)
        self.assertEqual(decision.remaining, 0)

    def test_remaining_counts_down_one_per_message(self) -> None:
        expected = PRIVATE_MAX_REPLIES
        for index in range(PRIVATE_MAX_REPLIES):
            decision = self.budget.check(
                "m1", started_at=self.now, scope_type="private", now=self.now
            )
            self.assertEqual(decision.remaining, expected - index)
            self._spend("m1", 1)

        self.assertFalse(
            self.budget.check("m1", started_at=self.now, scope_type="private", now=self.now).allowed
        )

    def test_group_quota_is_separate_from_private(self) -> None:
        """The same numeric message id can exist in both a group and a private chat."""
        group_key = quota_key("group:555", "m1")
        private_key = quota_key("private:67890", "m1")
        self._spend(group_key, GROUP_MAX_REPLIES, scope="group")

        blocked = self.budget.check(
            group_key, started_at=self.now, scope_type="group", now=self.now
        )
        private = self.budget.check(
            private_key, started_at=self.now, scope_type="private", now=self.now
        )

        self.assertFalse(blocked.allowed)
        self.assertTrue(private.allowed)

    def test_quota_key_combines_scope_and_message(self) -> None:
        self.assertEqual(quota_key("private:67890", "1001"), "private:67890:1001")
        self.assertEqual(quota_key("private:67890", ""), "")
        self.assertNotEqual(quota_key("group:555", "1"), quota_key("private:67890", "1"))

    def test_a_new_user_message_starts_a_fresh_quota(self) -> None:
        self._spend("m1", PRIVATE_MAX_REPLIES)

        decision = self.budget.check("m2", started_at=self.now, scope_type="private", now=self.now)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.remaining, PRIVATE_MAX_REPLIES)

    def test_spent_is_reported(self) -> None:
        self._spend("m1", 2)

        self.assertEqual(self.budget.spent("m1"), 2)
        self.assertEqual(self.budget.spent("unknown"), 0)

    def test_air_quota_is_not_consumed_by_an_empty_id(self) -> None:
        self.budget.record("", started_at=self.now, scope_type="private", now=self.now)

        self.assertEqual(len(self.budget), 0)

    def test_windows_stay_bounded(self) -> None:
        budget = PassiveReplyBudget(max_sessions=3)
        for index in range(10):
            budget.record(f"m{index}", started_at=self.now, scope_type="private", now=self.now)

        self.assertLessEqual(len(budget), 3)

    def test_prune_drops_only_closed_windows(self) -> None:
        self.budget.record("old", started_at=self.now, scope_type="group", now=self.now)
        self.budget.record("recent", started_at=self.now, scope_type="private", now=self.now)

        removed = self.budget.prune(now=self.now + timedelta(minutes=6))

        self.assertEqual(removed, 1)
        self.assertEqual(len(self.budget), 1)

    def test_forget_removes_one_window(self) -> None:
        self._spend("m1", 1)

        self.budget.forget("m1")

        self.assertEqual(self.budget.spent("m1"), 0)

    def test_describe_explains_both_refusals(self) -> None:
        self._spend("m1", PRIVATE_MAX_REPLIES)
        exhausted = self.budget.check("m1", started_at=self.now, scope_type="private", now=self.now)
        expired = self.budget.check(
            "m2",
            started_at=self.now,
            scope_type="private",
            now=self.now + timedelta(hours=2),
        )

        self.assertIn("需要用户再发一条消息", exhausted.describe())
        self.assertIn("被动回复窗口", expired.describe())


class ClientBudgetTest(unittest.TestCase):
    """The client must stop sending instead of being refused by the platform."""

    def setUp(self) -> None:
        SEND_LOG.clear()
        REPLY_BUDGET.clear()

    def tearDown(self) -> None:
        SEND_LOG.clear()
        REPLY_BUDGET.clear()

    def _client(self, **overrides) -> MyClient:
        settings = Settings(
            qqbot_appid="bot-1",
            reply_max_segments=3,
            reply_max_total_chars=4000,
            **overrides,
        )
        client = object.__new__(MyClient)
        client.settings = settings
        client.started_at = datetime.now()
        client.api = SimpleNamespace(
            post_c2c_message=AsyncMock(),
            post_group_message=AsyncMock(),
        )
        return client

    def _message(self, message_id: str = "m-1") -> SimpleNamespace:
        return SimpleNamespace(
            id=message_id,
            content="你好",
            author=SimpleNamespace(user_openid="user-a"),
            mentions=[],
            attachments=[],
            message_reference=None,
        )

    def test_long_reply_is_capped_by_the_remaining_quota(self) -> None:
        client = self._client()
        # Spend three of the four private replies first.
        for _ in range(3):
            REPLY_BUDGET.record(
                "qqbot:bot-1:user-a:m-1",
                started_at=datetime.now(timezone.utc),
                scope_type="private",
            )
        reply = "第一段。" + ("内容" * 1100) + "\n\n第二段。" + ("更多" * 1000)

        async def run() -> None:
            with patch(
                "doraymon.client.route_incoming_message",
                new=AsyncMock(return_value=reply),
            ):
                await client._handle_message(self._message(), is_at_message=False)

        asyncio.run(run())

        self.assertEqual(client.api.post_c2c_message.await_count, 1)

    def test_exhausted_quota_sends_nothing(self) -> None:
        client = self._client()
        for _ in range(4):
            REPLY_BUDGET.record(
                "qqbot:bot-1:user-a:m-1",
                started_at=datetime.now(timezone.utc),
                scope_type="private",
            )

        async def run() -> None:
            with patch(
                "doraymon.client.route_incoming_message",
                new=AsyncMock(return_value="不该发出"),
            ):
                await client._handle_message(self._message(), is_at_message=False)

        asyncio.run(run())

        client.api.post_c2c_message.assert_not_awaited()

    def test_successful_sends_consume_the_quota(self) -> None:
        client = self._client()

        async def run() -> None:
            with patch(
                "doraymon.client.route_incoming_message",
                new=AsyncMock(return_value="简短回复"),
            ):
                await client._handle_message(self._message(), is_at_message=False)

        asyncio.run(run())

        self.assertEqual(REPLY_BUDGET.spent("qqbot:bot-1:user-a:m-1"), 1)

    def test_failed_sends_do_not_consume_the_quota(self) -> None:
        client = self._client()
        client.api.post_c2c_message = AsyncMock(side_effect=ConnectionError("down"))

        async def run() -> None:
            with patch(
                "doraymon.client.route_incoming_message",
                new=AsyncMock(return_value="简短回复"),
            ):
                await client._handle_message(self._message(), is_at_message=False)

        asyncio.run(run())

        self.assertEqual(REPLY_BUDGET.spent("qqbot:bot-1:user-a:m-1"), 0)

    def test_window_can_be_disabled_by_configuration(self) -> None:
        client = self._client(qqbot_reply_window_enabled=False)
        for _ in range(10):
            REPLY_BUDGET.record(
                "qqbot:bot-1:user-a:m-1",
                started_at=datetime.now(timezone.utc),
                scope_type="private",
            )

        async def run() -> None:
            with patch(
                "doraymon.client.route_incoming_message",
                new=AsyncMock(return_value="仍然发出"),
            ):
                await client._handle_message(self._message(), is_at_message=False)

        asyncio.run(run())

        self.assertEqual(client.api.post_c2c_message.await_count, 1)
        self.assertEqual(REPLY_BUDGET.spent("qqbot:bot-1:user-a:m-1"), 10)

    def test_group_and_private_quotas_do_not_share(self) -> None:
        client = self._client()
        for _ in range(4):
            REPLY_BUDGET.record(
                "qqbot:bot-1:user-a:m-1",
                started_at=datetime.now(timezone.utc),
                scope_type="private",
            )
        group_message = SimpleNamespace(
            id="m-9",
            content="@bot 你好",
            group_openid="group-a",
            author=SimpleNamespace(member_openid="user-a"),
            mentions=[SimpleNamespace(id="bot-1")],
            attachments=[],
            message_reference=None,
        )

        async def run() -> None:
            with patch(
                "doraymon.client.route_incoming_message",
                new=AsyncMock(return_value="群回复"),
            ):
                await client._handle_message(group_message, is_at_message=True)

        asyncio.run(run())

        self.assertEqual(client.api.post_group_message.await_count, 1)

    def test_budget_key_includes_bot_and_conversation(self) -> None:
        """A message id is not unique across senders."""
        client = self._client()
        event = SimpleNamespace(
            platform="qqbot", bot_account="bot-1", raw_group_id="", raw_user_id="user-a",
            message_id="m-1",
        )

        self.assertEqual(client._budget_key(event), "qqbot:bot-1:user-a:m-1")


if __name__ == "__main__":
    unittest.main()
