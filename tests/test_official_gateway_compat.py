"""The pieces of the official path that only fail on a newer Python.

The official client worked up to login and no further could be checked without
credentials. These tests cover the parts that do not need them: the event loop
botpy demands before it will construct, and the WebSocket connect pattern botpy
uses to reach the gateway. Both are version-sensitive, and a failure in either
would look like a credential problem to whoever is deploying.

The connect pattern is reproduced exactly as botpy writes it rather than
paraphrased, because the point is to catch a dependency upgrade breaking that
specific call.
"""

from __future__ import annotations

import asyncio
from ssl import SSLContext
import unittest

from aiohttp import TCPConnector, ClientSession, web

from main import _prepare_event_loop


class BotpyConnectPatternTest(unittest.IsolatedAsyncioTestCase):
    """botpy's gateway connect, against a local server that speaks first."""

    async def asyncSetUp(self) -> None:
        app = web.Application()
        app.router.add_get("/", self._handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    async def asyncTearDown(self) -> None:
        await self.runner.cleanup()

    async def _handler(self, request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        # The real gateway opens with an op 10 hello.
        await socket.send_str('{"op":10,"d":{"heartbeat_interval":45000}}')
        async for _message in socket:
            pass
        return socket

    async def test_the_connect_pattern_still_works(self) -> None:
        """Exactly botpy/gateway.py's ClientSession(connector=TCPConnector(...))."""
        _prepare_event_loop()

        async with ClientSession(
            connector=TCPConnector(limit=10, ssl=SSLContext())
        ) as session:
            async with session.ws_connect(f"ws://127.0.0.1:{self.port}") as socket:
                message = await socket.receive()
                payload = message.json()

        self.assertEqual(payload["op"], 10)
        self.assertIn("heartbeat_interval", payload["d"])

    async def test_a_hello_frame_can_be_parsed_as_botpy_parses_it(self) -> None:
        """A hello must survive the JSON round trip botpy performs."""
        async with ClientSession() as session:
            async with session.ws_connect(f"ws://127.0.0.1:{self.port}") as socket:
                message = await socket.receive()

        self.assertEqual(int(payload_of(message)["d"]["heartbeat_interval"]), 45000)


def payload_of(message) -> dict:
    return message.json()


class EventLoopRequirementTest(unittest.TestCase):
    """botpy cannot be constructed without a current loop on this Python."""

    def setUp(self) -> None:
        # Other tests need a working loop, so whatever this leaves behind is
        # replaced with a usable one afterwards.
        self.addCleanup(_prepare_event_loop)

    def test_preparation_installs_a_usable_current_loop(self) -> None:
        """This is the failure botpy hits while constructing its client."""
        asyncio.set_event_loop(None)
        with self.assertRaises(RuntimeError):
            asyncio.get_event_loop()

        loop = _prepare_event_loop()

        self.assertIs(asyncio.get_event_loop(), loop)
        self.assertFalse(loop.is_closed())

    def test_botpy_client_construction_succeeds_after_preparation(self) -> None:
        """The concrete symptom: construction raised before any request was made."""
        import botpy

        asyncio.set_event_loop(None)
        _prepare_event_loop()

        # Same intents the real client builds, so this exercises the real path.
        client = botpy.Client(intents=botpy.Intents(public_messages=True))

        self.assertIsNotNone(client.loop)
        self.assertFalse(client.loop.is_closed())
