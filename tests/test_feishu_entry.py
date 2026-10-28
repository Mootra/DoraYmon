"""The Feishu entry: allowlist, validation, and the wiring to the shared session."""

from __future__ import annotations

import asyncio
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from doraymon.feishu_entry import (
    FeishuRunner,
    build_policy,
    build_throttle,
    run_feishu_entry,
    validate_feishu_settings,
)
from doraymon.identity import PLATFORM_FEISHU
from services.feishu_adapter import ACTION_CREATE_MESSAGE, FeishuAdapter
from tests.test_feishu_adapter import BOT, USER, private_payload


def _settings(**overrides) -> SimpleNamespace:
    values = {
        "command_prefix": "/",
        "feishu_enabled": True,
        "feishu_app_id": "cli_example",
        "feishu_app_secret": "secret",
        "feishu_bot_open_id": BOT,
        "feishu_chats": [],
        "feishu_users": [USER],
        "feishu_group_reply_mode": "reply",
        "feishu_group_cooldown_seconds": 3.0,
        "feishu_group_max_replies": 8,
        "feishu_group_rate_window_seconds": 60.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeTransport:
    """Records actions and events; stands in for the long-connection transport."""

    def __init__(self) -> None:
        self.actions = []
        self.submitted = []
        self.ran = False
        self.closed = False

    async def send_action(self, action):
        self.actions.append(action)
        return {"code": 0, "msg": "ok", "data": {}}

    def submit(self, payload) -> bool:
        self.submitted.append(payload)
        return True

    async def run_forever(self, *, max_attempts=None) -> None:
        self.ran = True

    async def close(self) -> None:
        self.closed = True


class FakeEventSource:
    def __init__(self) -> None:
        self.starts = 0

    def start(self, *, on_payload=None, on_ready=None):
        self.starts += 1
        if on_ready is not None:
            on_ready()
        if on_payload is not None:
            on_payload(private_payload())

    def stop(self):
        pass


class PolicyTest(unittest.TestCase):
    def test_lists_come_from_the_feishu_settings(self) -> None:
        policy = build_policy(_settings(feishu_chats="oc_a, oc_b", feishu_users=["ou_x"]))
        self.assertEqual(policy.allowed_groups, {"oc_a", "oc_b"})
        self.assertEqual(policy.allowed_users, {"ou_x"})

    def test_a_bare_string_is_split_not_iterated(self) -> None:
        """``"oc_a"`` must not become an allowlist of single characters."""
        policy = build_policy(_settings(feishu_chats="oc_a"))
        self.assertEqual(policy.allowed_groups, {"oc_a"})

    def test_an_empty_allowlist_denies_everyone(self) -> None:
        policy = build_policy(_settings(feishu_users=[]))
        self.assertFalse(policy.has_any_allowlist)

    def test_everyone_is_never_allowed_by_default(self) -> None:
        policy = build_policy(_settings(feishu_users=[USER]))
        self.assertFalse(policy.allow_all_users)
        self.assertFalse(policy.allow_all_groups)

    def test_throttle_reads_the_feishu_settings(self) -> None:
        throttle = build_throttle(
            _settings(
                feishu_group_cooldown_seconds=5.0,
                feishu_group_max_replies=2,
                feishu_group_rate_window_seconds=30.0,
            )
        )
        self.assertTrue(throttle.is_enabled)
        self.assertEqual(throttle.max_replies, 2)


class ValidationTest(unittest.TestCase):
    def test_a_complete_configuration_has_no_problems(self) -> None:
        self.assertEqual(validate_feishu_settings(_settings()), [])

    def test_the_app_id_is_required(self) -> None:
        problems = validate_feishu_settings(_settings(feishu_app_id="  "))
        self.assertTrue(any("BOT_FEISHU_APP_ID" in problem for problem in problems))

    def test_the_app_secret_is_required(self) -> None:
        problems = validate_feishu_settings(_settings(feishu_app_secret=""))
        self.assertTrue(any("BOT_FEISHU_APP_SECRET" in problem for problem in problems))

    def test_the_bot_open_id_is_required(self) -> None:
        """Without it a group message cannot be recognised as addressed."""
        problems = validate_feishu_settings(_settings(feishu_bot_open_id=""))
        self.assertTrue(any("BOT_FEISHU_BOT_OPEN_ID" in problem for problem in problems))

    def test_an_empty_allowlist_is_required_to_be_filled(self) -> None:
        problems = validate_feishu_settings(_settings(feishu_users=[]))
        self.assertTrue(any("允许列表为空" in problem for problem in problems))

    def test_every_problem_is_reported_at_once(self) -> None:
        problems = validate_feishu_settings(
            _settings(
                feishu_app_id="",
                feishu_app_secret="",
                feishu_bot_open_id="",
                feishu_users=[],
            )
        )
        self.assertEqual(len(problems), 4)


class RunnerTest(unittest.TestCase):
    def _runner(self, transport=None, **overrides) -> FeishuRunner:
        return FeishuRunner(
            settings=_settings(**overrides),
            transport=transport or FakeTransport(),
            api=FakeTransport(),
        )

    def test_the_session_speaks_feishu(self) -> None:
        runner = self._runner()
        self.assertEqual(runner.session.platform, PLATFORM_FEISHU)
        self.assertIsInstance(runner.session.adapter, FeishuAdapter)
        self.assertEqual(runner.session.channel_label, "飞书")
        self.assertEqual(runner.session.bot_account, BOT)

    def test_an_allowed_person_is_answered(self) -> None:
        transport = FakeTransport()
        runner = self._runner(transport)
        with patch(
            "services.account_session.route_incoming_message",
            new=AsyncMock(return_value="你好呀"),
        ):
            asyncio.run(runner.handle_payload(private_payload()))

        self.assertEqual(len(transport.actions), 1)
        action = transport.actions[0]
        self.assertEqual(action.name, ACTION_CREATE_MESSAGE)
        self.assertEqual(action.params["receive_id"], USER)
        self.assertEqual(json.loads(action.params["content"]), {"text": "你好呀"})

    def test_a_stranger_is_refused_and_counted(self) -> None:
        transport = FakeTransport()
        runner = self._runner(transport)
        with patch(
            "services.account_session.route_incoming_message",
            new=AsyncMock(return_value="不该发"),
        ):
            asyncio.run(runner.handle_payload(private_payload(open_id="ou_stranger")))

        self.assertEqual(transport.actions, [])
        self.assertEqual(runner.session.stats.skipped.get("user_not_allowed"), 1)

    def test_a_group_outside_the_allowlist_is_refused(self) -> None:
        from tests.test_feishu_adapter import group_payload

        transport = FakeTransport()
        runner = self._runner(transport, feishu_users=[])
        with patch(
            "services.account_session.route_incoming_message",
            new=AsyncMock(return_value="不该发"),
        ):
            asyncio.run(runner.handle_payload(group_payload()))
        self.assertEqual(transport.actions, [])
        self.assertEqual(runner.session.stats.skipped.get("empty_allowlist"), 1)

    def test_an_allowlisted_chat_with_no_user_list_admits_its_members(self) -> None:
        from tests.test_feishu_adapter import group_payload

        transport = FakeTransport()
        runner = self._runner(transport, feishu_users=[], feishu_chats=["oc_group"])
        with patch(
            "services.account_session.route_incoming_message",
            new=AsyncMock(return_value="群回复"),
        ):
            asyncio.run(runner.handle_payload(group_payload()))
        self.assertEqual(len(transport.actions), 1)

    def test_the_runners_own_message_is_ignored(self) -> None:
        transport = FakeTransport()
        runner = self._runner(transport)
        asyncio.run(runner.handle_payload(private_payload(open_id=BOT)))
        self.assertEqual(transport.actions, [])
        self.assertEqual(runner.session.stats.skipped.get("self_message"), 1)

    def test_a_runner_without_a_source_or_transport_refuses_to_be_built(self) -> None:
        with self.assertRaises(ValueError):
            FeishuRunner(settings=_settings(), api=FakeTransport())

    def test_an_injected_event_source_is_wired_into_the_transport(self) -> None:
        source = FakeEventSource()
        runner = FeishuRunner(settings=_settings(), api=FakeTransport(), event_source=source)
        self.assertIs(runner.transport.event_source, source)
        self.assertIs(runner.transport.sender, runner.api)


class BlockingEventSource:
    """A source that stays connected until it is told to stop.

    A source that returns immediately is not a failure case here but a *retry*
    case: the transport backs off and reconnects, for ever by design. Reaching
    the clean stop path therefore needs a connection that stays up.
    """

    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0
        self._release = threading.Event()

    def start(self, *, on_payload=None, on_ready=None):
        self.starts += 1
        if on_ready is not None:
            on_ready()
        self._release.wait(timeout=5)

    def stop(self):
        self.stops += 1
        self._release.set()


async def _settle(predicate, *, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition was not met in time")
        await asyncio.sleep(0.005)


class EntryTest(unittest.TestCase):
    def test_bad_configuration_stops_before_any_connection(self) -> None:
        source = BlockingEventSource()
        code = asyncio.run(
            run_feishu_entry(_settings(feishu_app_id=""), event_source=source)
        )
        self.assertEqual(code, 1)
        self.assertEqual(source.starts, 0)

    def test_a_healthy_connection_runs_until_stopped(self) -> None:
        async def scenario() -> None:
            source = BlockingEventSource()
            runner = FeishuRunner(
                settings=_settings(), api=FakeTransport(), event_source=source
            )
            task = asyncio.create_task(run_feishu_entry(_settings(), runner=runner))

            await _settle(lambda: source.starts == 1)
            self.assertEqual(source.stops, 0)

            await runner.stop()
            code = await asyncio.wait_for(task, timeout=5)

            self.assertEqual(code, 0)
            self.assertEqual(source.stops, 1)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
