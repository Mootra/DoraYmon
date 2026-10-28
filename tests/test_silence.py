"""The silence switch: one action that stops a live bot from speaking."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from doraymon.config import Settings
from doraymon.context import BotContext
from doraymon.router import route_message, route_natural_message
from plugins import silence as silence_plugin
from services.account_session import AccountSession
from services.silence import SILENCE, parse_silence_args, should_reply


def _context(
    *,
    args: str = "",
    user_openid: str = "admin-a",
    admin_openids: list[str] | None = None,
    silence_default: bool = False,
) -> BotContext:
    return BotContext(
        settings=Settings(
            command_prefix="/",
            admin_openids=admin_openids if admin_openids is not None else ["admin-a"],
            silence_default=silence_default,
            chat_history_enabled=False,
        ),
        started_at=datetime.now(),
        user_openid=user_openid,
        raw_content=f"/静默 {args}".strip(),
        command="静默",
        args=args,
    )


class SilenceStateTest(unittest.TestCase):
    def setUp(self) -> None:
        SILENCE.clear()
        self.addCleanup(SILENCE.clear)

    def test_default_is_not_silent(self) -> None:
        self.assertFalse(SILENCE.silent)
        self.assertIn("未静默", SILENCE.describe())

    def test_enabling_and_disabling_report_whether_anything_changed(self) -> None:
        self.assertTrue(SILENCE.enable(reason="test"))
        self.assertFalse(SILENCE.enable(reason="again"))
        self.assertTrue(SILENCE.disable())
        self.assertFalse(SILENCE.disable())

    def test_describe_records_the_time_and_reason(self) -> None:
        SILENCE.enable(reason="半夜在乱回", now=datetime(2026, 10, 19, 3, 0, 0))

        described = SILENCE.describe()

        self.assertIn("已静默", described)
        self.assertIn("2026-10-19 03:00:00", described)
        self.assertIn("半夜在乱回", described)

    def test_clear_resets_everything(self) -> None:
        SILENCE.enable(reason="x")
        SILENCE.clear()

        self.assertFalse(SILENCE.silent)
        self.assertEqual(SILENCE.changes, 0)
        self.assertIsNone(SILENCE.changed_at)


class ShouldReplyTest(unittest.TestCase):
    def setUp(self) -> None:
        SILENCE.clear()
        self.addCleanup(SILENCE.clear)

    def test_normal_settings_reply(self) -> None:
        self.assertTrue(should_reply(Settings()))

    def test_runtime_silence_blocks_replies(self) -> None:
        SILENCE.enable()

        self.assertFalse(should_reply(Settings()))

    def test_config_default_silence_blocks_replies(self) -> None:
        """A process may start silent and stay silent until opened."""
        self.assertFalse(should_reply(Settings(silence_default=True)))

    def test_opening_the_switch_beats_the_config_default(self) -> None:
        """The run-time switch is the operator's escape from a silent start."""
        settings = Settings(silence_default=True)
        SILENCE.disable()

        self.assertFalse(should_reply(settings))
        self.assertIn("配置为启动即静默", __import__(
            "services.silence", fromlist=["describe_effective"]
        ).describe_effective(settings))


class ParseArgsTest(unittest.TestCase):
    def test_recognised_spellings(self) -> None:
        cases = {
            "": "status",
            "on": "on",
            "off": "off",
            "开启": "on",
            "打开": "on",
            "关闭": "off",
            "解除": "off",
            "状态": "status",
            "ON": "on",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_silence_args(text), expected)

    def test_unknown_subcommand_is_reported(self) -> None:
        self.assertEqual(parse_silence_args("maybe"), "unknown")


class SilencePluginTest(unittest.TestCase):
    def setUp(self) -> None:
        SILENCE.clear()
        self.addCleanup(SILENCE.clear)

    def test_a_non_admin_is_refused(self) -> None:
        reply = silence_plugin.handle(_context(user_openid="stranger"))

        self.assertIn("没有权限", reply)
        self.assertFalse(SILENCE.silent)

    def test_an_admin_can_silence_and_resume(self) -> None:
        on = silence_plugin.handle(_context(args="on"))
        self.assertIn("已静默", on)
        self.assertTrue(SILENCE.silent)

        off = silence_plugin.handle(_context(args="off"))
        self.assertIn("已恢复", off)
        self.assertFalse(SILENCE.silent)

    def test_repeat_commands_say_so_instead_of_pretending(self) -> None:
        silence_plugin.handle(_context(args="on"))

        again = silence_plugin.handle(_context(args="on"))

        self.assertIn("已经是静默状态", again)

    def test_status_reports_the_state(self) -> None:
        self.assertIn("未静默", silence_plugin.handle(_context(args="status")))

    def test_an_unknown_subcommand_prints_usage(self) -> None:
        self.assertIn("用法", silence_plugin.handle(_context(args="maybe")))

    def test_no_user_identity_is_refused(self) -> None:
        self.assertIn("没有权限", silence_plugin.handle(_context(user_openid="")))


class RouterGateTest(unittest.TestCase):
    def setUp(self) -> None:
        SILENCE.clear()
        self.addCleanup(SILENCE.clear)

    def test_ordinary_commands_are_skipped_while_silent(self) -> None:
        SILENCE.enable()
        context = _context()
        context.raw_content = "/ping"
        context.command = ""

        reply = asyncio.run(route_message(context))

        self.assertEqual(reply, "")

    def test_the_silence_command_still_works_while_silent(self) -> None:
        """Otherwise the bot could only be un-silenced by a restart."""
        SILENCE.enable()
        context = _context(args="off")

        reply = asyncio.run(route_message(context))

        self.assertIn("已恢复", reply)
        self.assertFalse(SILENCE.silent)

    def test_natural_language_is_skipped_while_silent(self) -> None:
        SILENCE.enable()
        context = _context()
        context.raw_content = "中午吃什么"

        reply = asyncio.run(route_natural_message(context))

        self.assertEqual(reply, "")

    def test_natural_language_still_works_when_not_silent(self) -> None:
        context = _context()
        context.raw_content = "中午吃什么"

        with patch(
            "doraymon.router._run_handler", new=AsyncMock(return_value="推荐结果")
        ):
            reply = asyncio.run(route_natural_message(context))

        self.assertEqual(reply, "推荐结果")


class AccountSessionSilenceTest(unittest.TestCase):
    """A silenced bot must not even route, let alone send."""

    def setUp(self) -> None:
        SILENCE.clear()
        self.addCleanup(SILENCE.clear)

    def test_a_silenced_session_sends_nothing(self) -> None:
        SILENCE.enable()
        sent: list[str] = []

        class Transport:
            async def send_action(self, action):
                sent.append(action.action)
                return {"status": "ok", "retcode": 0, "data": {}}

        session = AccountSession(
            settings=Settings(qqbot_appid="bot-1", chat_history_enabled=False),
            transport=Transport(),
            bot_account="20002",
        )
        from services.onebot_adapter import from_onebot_event
        from tests.onebot_server import private_message_event

        event = from_onebot_event(
            private_message_event(user_id=10001, message_id=1), bot_account="20002"
        )
        self.assertIsNotNone(event)

        result = asyncio.run(session.handle_event(event, counted=True))

        self.assertIsNone(result)
        self.assertEqual(sent, [])
        self.assertEqual(session.stats.replied, 0)


if __name__ == "__main__":
    unittest.main()
