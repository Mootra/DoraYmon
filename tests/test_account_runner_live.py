"""AccountRunner over a real socket: the access policy and throttle in situ.

The channel tests prove frames move. These prove the *policy* actually gates
them, with the real transport underneath: a group that is not on the allowlist
gets nothing, a group that is on the list is answered, a second message inside the
cooldown is not, and the rate window stops a flood.

This is the combination a live account runs, and it had only ever been exercised
with fakes.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from doraymon.account_entry import AccountRunner
from doraymon.config import Settings
from services.account_session import AccountSession
from services.account_throttle import ACCOUNT_THROTTLE
from services.reply_budget import REPLY_BUDGET
from services.session_revisions import SESSION_REVISIONS
from tests.onebot_server import (
    OneBotTestServer,
    group_message_event,
    private_message_event,
)

REPLY_TEXT = "策略测试回复"
ALLOWED_GROUP = 30003
ALLOWED_FRIEND = 10001
OTHER_GROUP = 40004
OTHER_FRIEND = 10009


async def _canned_route(self, event):  # noqa: ANN001
    return REPLY_TEXT


class RunnerTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        REPLY_BUDGET.clear()
        ACCOUNT_THROTTLE.clear()
        SESSION_REVISIONS.clear()
        self.server = OneBotTestServer()
        await self.server.start()
        self.runner: AccountRunner | None = None
        self._reader_task: asyncio.Task | None = None

    async def asyncTearDown(self) -> None:
        if self.runner is not None:
            self.runner.transport.request_stop()
            await self.runner.stop()
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except BaseException:
                pass
        await self.server.stop()
        REPLY_BUDGET.clear()
        ACCOUNT_THROTTLE.clear()
        SESSION_REVISIONS.clear()

    def _settings(self, **overrides) -> Settings:
        payload = {
            "qqbot_appid": "bot-1",
            "chat_history_enabled": False,
            "qq_account_enabled": True,
            "qq_account_access_mode": "allowlist",
            "qq_account_self_id": "20002",
            "qq_account_ws_url": self.server.url,
            # Settings holds lists; load_settings splits the comma form for us.
            "qq_account_groups": [str(ALLOWED_GROUP)],
            "qq_account_friends": [str(ALLOWED_FRIEND)],
            "qq_account_group_cooldown_seconds": 3.0,
            "qq_account_group_rate_window_seconds": 60.0,
            "qq_account_group_max_replies": 8,
        }
        payload.update(overrides)
        return Settings(**payload)

    async def _start(self, settings: Settings | None = None) -> AccountRunner:
        self.runner = AccountRunner(settings=settings or self._settings())
        self.runner.transport.connect_timeout = 5.0
        self.runner.transport.action_timeout = 5.0
        self.runner.transport.heartbeat_timeout = 5.0
        self._reader_task = asyncio.ensure_future(
            self.runner.run(max_attempts=1)
        )
        if not await self.server.wait_for_connection(timeout=5.0):
            self.fail("the test server never saw a connection")
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline:
            if self.runner.session.connected:
                break
            await asyncio.sleep(0.02)
        else:
            self.fail("the session never reported a live connection")
        return self.runner

    async def _send_and_settle(self, payload: dict, *, expect: bool) -> None:
        """Send one event and check whether *this* event produced a send.

        The comparison is on the action count, not on the whole list: earlier
        events in the same test have already recorded their own sends, and
        asserting an empty list would fail for the wrong reason while claiming to
        prove "nothing was sent".
        """
        before = len(self.server.actions)
        await self.runner.handle_payload(payload)
        if expect:
            action = await self.server.wait_for_action(timeout=5.0)
            self.assertIsNotNone(
                action, f"expected a reply; saw {self.server.action_names()}"
            )
            return
        await asyncio.sleep(0.25)
        self.assertEqual(
            len(self.server.actions),
            before,
            f"expected no reply; new actions: {self.server.action_names()[before:]}",
        )


class PolicyOverSocketTest(RunnerTestCase):
    async def test_an_allowed_group_mention_is_answered(self) -> None:
        runner = await self._start()

        with patch.object(AccountSession, "_route", _canned_route):
            await self._send_and_settle(
                group_message_event(
                    group_id=ALLOWED_GROUP, user_id=ALLOWED_FRIEND, message_id=101
                ),
                expect=True,
            )

        self.assertEqual(runner.session.stats.replied, 1)

    async def test_a_group_outside_the_allowlist_gets_nothing(self) -> None:
        """Default-deny is the whole point of the account entry."""
        runner = await self._start()

        with patch.object(AccountSession, "_route", _canned_route):
            await self._send_and_settle(
                group_message_event(
                    group_id=OTHER_GROUP, user_id=ALLOWED_FRIEND, message_id=102
                ),
                expect=False,
            )

        self.assertIn("group_not_allowed", runner.session.stats.skipped)

    async def test_an_allowed_friend_is_answered(self) -> None:
        runner = await self._start()

        with patch.object(AccountSession, "_route", _canned_route):
            await self._send_and_settle(
                private_message_event(user_id=ALLOWED_FRIEND, message_id=103),
                expect=True,
            )

        self.assertEqual(runner.session.stats.replied, 1)

    async def test_a_stranger_is_not_answered(self) -> None:
        runner = await self._start()

        with patch.object(AccountSession, "_route", _canned_route):
            await self._send_and_settle(
                private_message_event(user_id=OTHER_FRIEND, message_id=104),
                expect=False,
            )

        self.assertTrue(
            any("user" in reason or "allowlist" in reason for reason in runner.session.stats.skipped),
            runner.session.stats.skipped,
        )


class ThrottleOverSocketTest(RunnerTestCase):
    async def test_a_second_message_inside_the_cooldown_is_dropped(self) -> None:
        settings = self._settings(qq_account_group_cooldown_seconds=60.0)
        runner = await self._start(settings)

        with patch.object(AccountSession, "_route", _canned_route):
            await self._send_and_settle(
                group_message_event(group_id=ALLOWED_GROUP, message_id=201), expect=True
            )
            await self._send_and_settle(
                group_message_event(group_id=ALLOWED_GROUP, message_id=202), expect=False
            )

        self.assertEqual(runner.session.stats.replied, 1)
        self.assertTrue(
            any("throttl" in reason.lower() for reason in runner.session.stats.skipped),
            runner.session.stats.skipped,
        )

    async def test_the_rate_window_stops_a_flood_in_one_group(self) -> None:
        settings = self._settings(
            qq_account_group_cooldown_seconds=0.0,
            qq_account_group_max_replies=2,
            qq_account_group_rate_window_seconds=600.0,
        )
        runner = await self._start(settings)

        with patch.object(AccountSession, "_route", _canned_route):
            for index in range(4):
                await self.runner.handle_payload(
                    group_message_event(
                        group_id=ALLOWED_GROUP, message_id=300 + index
                    )
                )
                await asyncio.sleep(0.12)

        self.assertLessEqual(len(self.server.actions_named("send_group_msg")), 2)
        self.assertGreaterEqual(runner.session.stats.replied, 1)

    async def test_a_private_conversation_is_not_blocked_by_the_group_throttle(self) -> None:
        """The cooldown belongs to the group, not to the account."""
        settings = self._settings(qq_account_group_cooldown_seconds=600.0)
        runner = await self._start(settings)

        with patch.object(AccountSession, "_route", _canned_route):
            await self._send_and_settle(
                group_message_event(group_id=ALLOWED_GROUP, message_id=401), expect=True
            )
            await self._send_and_settle(
                private_message_event(user_id=ALLOWED_FRIEND, message_id=402), expect=True
            )

        self.assertEqual(runner.session.stats.replied, 2)


class DuplicateOverSocketTest(RunnerTestCase):
    async def test_a_replayed_event_is_not_answered_twice(self) -> None:
        runner = await self._start()
        payload = private_message_event(user_id=ALLOWED_FRIEND, message_id=501)

        with patch.object(AccountSession, "_route", _canned_route):
            await self._send_and_settle(payload, expect=True)
            await self._send_and_settle(dict(payload), expect=False)

        self.assertEqual(len(self.server.actions_named("send_private_msg")), 1)
        self.assertEqual(runner.session.stats.replied, 1)
        self.assertIn("duplicate", runner.session.stats.skipped)


if __name__ == "__main__":
    unittest.main()
