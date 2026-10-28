from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from doraymon.client import MyClient
from doraymon.config import Settings, load_settings
from services.reply_budget import REPLY_BUDGET
from services.send_result import SEND_LOG
from services.session_history import get_session_key


class ClientConfigTest(unittest.TestCase):
    @patch("doraymon.config.load_dotenv")
    @patch("doraymon.config._load_yaml_config", return_value={})
    @patch.dict("os.environ", {}, clear=True)
    def test_default_location_and_configuration_overrides(self, config_mock, dotenv_mock) -> None:
        self.assertEqual(load_settings().default_location, "江西师范大学瑶湖校区")
        config_mock.return_value = {"bot": {"default_location": "示例大学北校区"}}
        self.assertEqual(load_settings().default_location, "示例大学北校区")
        with patch.dict("os.environ", {"BOT_DEFAULT_LOCATION": "示例车站"}):
            self.assertEqual(load_settings().default_location, "示例车站")

    @patch("doraymon.config.load_dotenv")
    @patch("doraymon.config._load_yaml_config", return_value={})
    @patch.dict(
        "os.environ",
        {
            "BOT_CHAT_CONTEXT_MAX_CHARS": "4321",
            "BOT_CHAT_CONTEXT_SUMMARY_MAX_CHARS": "987",
            "BOT_CHAT_CONTEXT_TTL_MINUTES": "45",
            "BOT_CHAT_SUMMARY_RETENTION_DAYS": "9",
            "BOT_REPLY_MAX_SEGMENTS": "2",
            "BOT_REPLY_MAX_TOTAL_CHARS": "3500",
        },
    )
    def test_chat_context_budget_is_loaded_from_environment(
        self,
        config_mock,
        dotenv_mock,
    ) -> None:
        settings = load_settings()

        self.assertEqual(settings.chat_context_max_chars, 4321)
        self.assertEqual(settings.chat_context_summary_max_chars, 987)
        self.assertEqual(settings.chat_context_ttl_minutes, 45)
        self.assertEqual(settings.chat_summary_retention_days, 9)
        self.assertEqual(settings.reply_max_segments, 2)
        self.assertEqual(settings.reply_max_total_chars, 3500)

    @patch("doraymon.client.botpy.Client.__init__", return_value=None)
    def test_sandbox_setting_is_forwarded_to_botpy_client(self, client_init_mock) -> None:
        for sandbox_enabled in (True, False):
            with self.subTest(sandbox_enabled=sandbox_enabled):
                MyClient(Settings(qqbot_sandbox=sandbox_enabled))

                self.assertEqual(
                    client_init_mock.call_args.kwargs["is_sandbox"],
                    sandbox_enabled,
                )

    @patch("doraymon.client.botpy.Client.__init__", return_value=None)
    def test_explicit_botpy_sandbox_argument_keeps_priority(self, client_init_mock) -> None:
        MyClient(Settings(qqbot_sandbox=True), is_sandbox=False)

        self.assertFalse(client_init_mock.call_args.kwargs["is_sandbox"])


class ClientMessageContractTest(unittest.TestCase):
    """The official entry point must route through the shared message contract."""

    def setUp(self) -> None:
        SEND_LOG.clear()
        REPLY_BUDGET.clear()

    def tearDown(self) -> None:
        SEND_LOG.clear()
        REPLY_BUDGET.clear()

    def _client(self) -> MyClient:
        client = object.__new__(MyClient)
        client.settings = Settings(qqbot_appid="bot-1", command_prefix="/")
        client.started_at = datetime.now()
        client.api = SimpleNamespace(
            post_c2c_message=AsyncMock(),
            post_group_message=AsyncMock(),
        )
        return client

    def _group_message(self, content: str, *, with_mention: bool) -> SimpleNamespace:
        return SimpleNamespace(
            id="m-1",
            content=content,
            group_openid="group-a",
            author=SimpleNamespace(member_openid="user-a"),
            mentions=[SimpleNamespace(id="bot-1")] if with_mention else [],
            attachments=[],
            message_reference=None,
        )

    def test_unaddressed_group_message_is_ignored(self) -> None:
        client = self._client()

        async def run() -> None:
            with patch(
                "doraymon.client.route_incoming_message",
                new=AsyncMock(return_value="不该发出"),
            ) as router:
                await client._handle_message(
                    self._group_message("随便聊聊", with_mention=False),
                    is_at_message=False,
                )
                router.assert_not_called()

        asyncio.run(run())
        client.api.post_group_message.assert_not_awaited()

    def test_unaddressed_group_command_is_routed(self) -> None:
        client = self._client()
        seen = {}

        async def capture(context, fallback_command=""):
            seen["context"] = context
            return "命令回复"

        async def run() -> None:
            with patch("doraymon.client.route_incoming_message", new=capture):
                await client._handle_message(
                    self._group_message("/help", with_mention=False),
                    is_at_message=False,
                )

        asyncio.run(run())
        self.assertEqual(seen["context"].command or seen["context"].raw_content, "/help")
        self.assertEqual(seen["context"].platform, "qqbot")
        self.assertEqual(seen["context"].bot_account, "bot-1")
        self.assertEqual(seen["context"].group_openid, "group-a")

    def test_addressed_group_message_is_routed_with_identity(self) -> None:
        client = self._client()
        seen = {}

        async def capture(context, fallback_command=""):
            seen["context"] = context
            seen["fallback"] = fallback_command
            return "群回复"

        async def run() -> None:
            with patch("doraymon.client.route_incoming_message", new=capture):
                await client._handle_message(
                    self._group_message("<@!bot-1> 你好", with_mention=True),
                    is_at_message=True,
                )

        asyncio.run(run())
        self.assertTrue(seen["context"].is_at_message)
        self.assertEqual(seen["fallback"], "")
        self.assertEqual(
            get_session_key(seen["context"]),
            ("group", "qqbot:bot-1:group-a", "qqbot:bot-1:user-a"),
        )

    def test_private_message_falls_back_to_chat(self) -> None:
        client = self._client()
        seen = {}
        message = SimpleNamespace(
            id="m-2",
            content="你好",
            author=SimpleNamespace(user_openid="user-a"),
            mentions=[],
            attachments=[],
            message_reference=None,
        )

        async def capture(context, fallback_command=""):
            seen["fallback"] = fallback_command
            seen["context"] = context
            return "私聊回复"

        async def run() -> None:
            with patch("doraymon.client.route_incoming_message", new=capture):
                await client._handle_message(message, is_at_message=False)

        asyncio.run(run())
        self.assertEqual(seen["fallback"], "chat")
        self.assertEqual(
            get_session_key(seen["context"]),
            ("private", "qqbot:bot-1:user-a", "qqbot:bot-1:user-a"),
        )

    def test_own_message_is_ignored(self) -> None:
        client = self._client()
        client.validator = SimpleNamespace(is_from_self=lambda owner, message, account: True)

        async def run() -> None:
            with patch(
                "doraymon.client.route_incoming_message",
                new=AsyncMock(return_value="不该发出"),
            ) as router:
                await client._handle_message(
                    self._group_message("<@!bot-1> 你好", with_mention=True),
                    is_at_message=True,
                )
                router.assert_not_called()

        asyncio.run(run())

    def test_empty_text_is_ignored(self) -> None:
        client = self._client()
        message = SimpleNamespace(
            id="m-3",
            content="   ",
            author=SimpleNamespace(user_openid="user-a"),
            mentions=[],
            attachments=[],
            message_reference=None,
        )

        async def run() -> None:
            with patch(
                "doraymon.client.route_incoming_message",
                new=AsyncMock(return_value="不该发出"),
            ) as router:
                await client._handle_message(message, is_at_message=False)
                router.assert_not_called()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
