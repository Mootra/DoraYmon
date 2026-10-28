"""The account path over a real WebSocket, not a mock.

Everything else about the account path is unit-tested against fakes. These tests
run the real transport against a real OneBot server on a real socket, because the
things most likely to be wrong at 3am on a live account are the things a fake
cannot show: the URL the transport builds, the token header, the action frames it
puts on the wire, and whether a second identical event produces a second reply.

The reply text is canned via a patched route: what is under test is the channel,
not the model.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from doraymon.config import Settings
from services.account_session import AccountSession
from services.account_throttle import ACCOUNT_THROTTLE, ConversationThrottle
from services.onebot_transport import OneBotWebSocketTransport
from services.reply_budget import REPLY_BUDGET
from services.session_revisions import SESSION_REVISIONS
from tests.onebot_server import (
    OneBotTestServer,
    group_message_event,
    private_message_event,
)

REPLY_TEXT = "这是测试回复"


async def _canned_route(self, event):  # noqa: ANN001
    return REPLY_TEXT


class RealSocketTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        REPLY_BUDGET.clear()
        ACCOUNT_THROTTLE.clear()
        SESSION_REVISIONS.clear()
        self.server = OneBotTestServer()
        await self.server.start()
        self.transport: OneBotWebSocketTransport | None = None
        self.session: AccountSession | None = None
        self._reader_task: asyncio.Task | None = None

    async def asyncTearDown(self) -> None:
        if self.transport is not None:
            self.transport.request_stop()
            await self.transport.close()
        task = getattr(self, "_reader_task", None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
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
            "qq_account_self_id": "20002",
            "qq_account_ws_url": self.server.url,
            "qq_account_groups": "30003",
            "qq_account_friends": "",
        }
        payload.update(overrides)
        return Settings(**payload)

    async def _connect(self, settings: Settings | None = None) -> AccountSession:
        """Wire a real transport to a real session and connect it."""
        resolved = settings or self._settings()
        self.session = AccountSession(
            settings=resolved,
            transport=None,  # replaced below once the transport exists
            bot_account="20002",
            group_reply_mode="reply",
            group_throttle=None,
            private_throttle=None,
            throttle=ConversationThrottle(),
        )
        self.transport = OneBotWebSocketTransport(
            ws_url=self.server.url,
            token=self.server.token,
            on_event=self.session.handle_payload,
            on_connected=self.session.on_connected,
            on_disconnected=self.session.on_disconnected,
            connect_timeout=5.0,
            action_timeout=5.0,
            heartbeat_timeout=5.0,
        )
        self.session.transport = self.transport
        # The reader has to be running for action responses to be seen: the
        # transport resolves a pending action from inside its read loop.
        self._reader_task = asyncio.ensure_future(
            self.transport.run_forever(max_attempts=1, reconnect_base_seconds=0.05)
        )
        # The reader only reconnects on a dead link, so give the first frame a
        # moment: without one the transport treats the peer as silent.
        if not await self.server.wait_for_connection(timeout=5.0):
            self.fail("the test server never saw a connection")
        await self._wait_for_session_connected()
        return self.session

    async def _wait_for_session_connected(self, timeout: float = 5.0) -> None:
        """Wait until the session is marked connected, or fail with a reason."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self.session is not None and self.session.connected:
                return
            await asyncio.sleep(0.02)
        self.fail(
            "the session never reported a live connection "
            f"(transport connected={getattr(self.transport, 'connected', None)})"
        )


class ChannelTest(RealSocketTestCase):
    async def test_transport_connects_over_a_real_socket(self) -> None:
        await self._connect()

        self.assertEqual(self.server.connections, 1)
        self.assertTrue(self.session.connected)

    async def test_private_message_produces_a_real_send_action(self) -> None:
        session = await self._connect()

        with patch.object(AccountSession, "_route", _canned_route):
            event = await session.handle_payload(private_message_event())
            # Let the send settle on the wire.
            action = await self.server.wait_for_action("send_private_msg")

        self.assertIsNotNone(event)
        self.assertIsNotNone(action, f"no send happened; saw {self.server.action_names()}")
        self.assertEqual(action.params.get("user_id"), 10001)
        self.assertIn(REPLY_TEXT, str(action.params.get("message")))

    async def test_group_message_with_mention_produces_a_send_action(self) -> None:
        session = await self._connect()

        with patch.object(AccountSession, "_route", _canned_route):
            await session.handle_payload(group_message_event())
            action = await self.server.wait_for_action("send_group_msg")

        self.assertIsNotNone(action, f"no send happened; saw {self.server.action_names()}")
        self.assertEqual(action.params.get("group_id"), 30003)

    async def test_group_message_without_a_mention_is_not_answered(self) -> None:
        session = await self._connect()

        with patch.object(AccountSession, "_route", _canned_route):
            await session.handle_payload(group_message_event(at_self=False))
            await asyncio.sleep(0.15)

        self.assertEqual(self.server.actions, [])

    async def test_every_action_carries_a_unique_echo(self) -> None:
        session = await self._connect()

        with patch.object(AccountSession, "_route", _canned_route):
            await session.handle_payload(private_message_event(message_id=11))
            await self.server.wait_for_action("send_private_msg")
            await session.handle_payload(private_message_event(message_id=12))
            await asyncio.sleep(0.15)

        echoes = [item.echo for item in self.server.actions]
        self.assertTrue(echoes)
        self.assertEqual(len(echoes), len(set(echoes)))

    async def test_the_bot_does_not_answer_its_own_message(self) -> None:
        session = await self._connect()

        with patch.object(AccountSession, "_route", _canned_route):
            await session.handle_payload(
                private_message_event(user_id=20002, message_id=21)
            )
            await asyncio.sleep(0.15)

        self.assertEqual(self.server.actions, [])

    async def test_a_duplicate_event_is_not_answered_twice(self) -> None:
        """OneBot can redeliver; a second reply would be visible to the user."""
        session = await self._connect()
        payload = private_message_event(message_id=31)

        with patch.object(AccountSession, "_route", _canned_route):
            await session.handle_payload(payload)
            await self.server.wait_for_action("send_private_msg")
            await session.handle_payload(dict(payload))
            await asyncio.sleep(0.15)

        self.assertEqual(len(self.server.actions_named("send_private_msg")), 1)

    async def test_non_message_events_are_ignored(self) -> None:
        session = await self._connect()

        with patch.object(AccountSession, "_route", _canned_route):
            for payload in (
                {"post_type": "meta_event", "meta_event_type": "heartbeat"},
                {"post_type": "notice", "notice_type": "group_increase"},
                {"post_type": "request", "request_type": "friend"},
            ):
                await session.handle_payload(payload)
            await asyncio.sleep(0.1)

        self.assertEqual(self.server.actions, [])

    async def test_a_rejected_status_is_not_reported_as_delivered(self) -> None:
        """The server returns a failure; the bot must record a failure."""
        self.server.responses["send_private_msg"] = {
            "status": "failed",
            "retcode": 100,
            "msg": "SEND_FAILED",
            "data": {},
        }
        session = await self._connect()

        with patch.object(AccountSession, "_route", _canned_route):
            await session.handle_payload(private_message_event(message_id=41))
            await self.server.wait_for_action("send_private_msg")
            await asyncio.sleep(0.15)

        self.assertGreaterEqual(session.stats.segments_failed, 1)
        self.assertEqual(session.stats.segments_sent, 0)


if __name__ == "__main__":
    unittest.main()


class CrossMachineReachabilityTest(unittest.IsolatedAsyncioTestCase):
    """The bot listens beyond loopback, so a Windows-side client can connect.

    Windows reaches WSL2 through the localhost forwarding rule, which is verified
    from the Windows side rather than assumed. What is checked here is the half that
    lives in this repo: the listener accepts a non-loopback bind, and the address
    the self-check prints for a remote client is a real address of this machine.
    """

    async def test_the_server_can_bind_beyond_loopback(self) -> None:
        server = OneBotTestServer(host="0.0.0.0")
        await server.start()
        try:
            self.assertNotIn(server.url, ("", "ws://0.0.0.0:0"))
            # The port must be a real assigned port, not the requested 0.
            self.assertGreater(server.port, 0)
        finally:
            await server.stop()

    async def test_a_loopback_client_still_reaches_a_wildcard_bind(self) -> None:
        """Binding 0.0.0.0 must not break the local case."""
        server = OneBotTestServer(host="0.0.0.0")
        await server.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    def test_the_printed_remote_address_is_a_real_machine_address(self) -> None:
        from scripts.verify_account_channel import _reachable_addresses

        addresses = _reachable_addresses(3001)

        # Every candidate must be usable verbatim by a client: a wildcard or a
        # loopback address would be useless to point another machine at.
        for address in addresses:
            with self.subTest(address=address):
                self.assertTrue(address.startswith("ws://"), address)
                host = address[len("ws://") :].rsplit(":", 1)[0]
                self.assertNotEqual(host, "0.0.0.0", address)
                self.assertFalse(host.startswith("127."), address)

    def test_the_localhost_guidance_is_offered_for_a_windows_client(self) -> None:
        """The WSL2 localhost forwarding path is the one that actually works here."""
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch

        from scripts import verify_account_channel as module

        async def fake_wait(timeout):
            return False

        server = MagicMockServer()
        buffer = io.StringIO()
        with patch.object(module, "OneBotTestServer", return_value=server), \
             patch.object(module, "load_settings", side_effect=RuntimeError("stop here")):
            with redirect_stdout(buffer):
                try:
                    asyncio.run(module.run(3001, "", "0.0.0.0"))
                except RuntimeError:
                    pass
        output = buffer.getvalue()
        self.assertIn("ws://127.0.0.1:3001", output)
        self.assertIn("Windows", output)


class MagicMockServer:
    """Minimal stand-in that records the bind it was asked for."""

    def __init__(self) -> None:
        self.connections = 0
        self.url = "ws://0.0.0.0:3001"

    async def start(self) -> str:
        return self.url

    async def stop(self) -> None:
        return None

    async def wait_for_connection(self, timeout: float = 5.0) -> bool:
        return False
