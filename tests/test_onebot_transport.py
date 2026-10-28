"""P1a: WebSocket transport behaviour, exercised without a real socket."""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import unittest

import aiohttp

from services.onebot_adapter import OneBotAction
from services.onebot_transport import (
    OneBotTransportError,
    OneBotWebSocketTransport,
    build_ws_url,
    reconnect_delay,
    redact_secrets,
)


class FakeFrame:
    def __init__(self, frame_type, data=None) -> None:
        self.type = frame_type
        self.data = data


class FakeWebSocket:
    """Stands in for aiohttp's client websocket."""

    def __init__(self, frames=None, *, hang_when_empty: bool = False) -> None:
        self.sent: list[str] = []
        self.closed = False
        self.hang_when_empty = hang_when_empty
        self._frames: asyncio.Queue = asyncio.Queue()
        for frame in frames or ():
            self._frames.put_nowait(frame)
        self.exception = None

    async def send_str(self, payload: str) -> None:
        self.sent.append(payload)

    async def receive(self):
        if self._frames.empty():
            if self.hang_when_empty:
                # A healthy but currently silent connection.
                await asyncio.sleep(3600)
            # Otherwise the stream has ended.
            return FakeFrame(aiohttp.WSMsgType.CLOSED)
        return await self._frames.get()

    def push(self, frame: FakeFrame) -> None:
        self._frames.put_nowait(frame)

    async def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, ws: FakeWebSocket) -> None:
        self.ws = ws
        self.closed = False
        self.connect_kwargs: dict = {}
        self.connect_calls = 0

    async def ws_connect(self, url, **kwargs):
        self.connect_calls += 1
        self.connect_kwargs = {"url": url, **kwargs}
        return self.ws

    async def close(self) -> None:
        self.closed = True


def _transport_with(ws: FakeWebSocket, **kwargs) -> tuple[OneBotWebSocketTransport, FakeSession]:
    session = FakeSession(ws)
    transport = OneBotWebSocketTransport(
        ws_url="ws://127.0.0.1:3001",
        session_factory=lambda: session,
        **kwargs,
    )
    return transport, session


class UrlBuildingTest(unittest.TestCase):
    def test_plain_url_is_returned_unchanged(self) -> None:
        self.assertEqual(build_ws_url("ws://127.0.0.1:3001"), "ws://127.0.0.1:3001")

    def test_token_is_appended_as_a_query_parameter(self) -> None:
        self.assertEqual(
            build_ws_url("ws://127.0.0.1:3001", "secret"),
            "ws://127.0.0.1:3001?access_token=secret",
        )

    def test_existing_query_is_preserved(self) -> None:
        self.assertEqual(
            build_ws_url("ws://127.0.0.1:3001?foo=1", "secret"),
            "ws://127.0.0.1:3001?foo=1&access_token=secret",
        )

    def test_token_is_percent_encoded(self) -> None:
        self.assertIn("access_token=a%2Fb", build_ws_url("ws://x", "a/b"))

    def test_empty_url_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_ws_url("   ")


class ReconnectDelayTest(unittest.TestCase):
    def test_delay_grows_then_is_capped(self) -> None:
        delays = [reconnect_delay(attempt, base_seconds=1, factor=2, max_seconds=8) for attempt in range(6)]

        self.assertEqual(delays, [1.0, 2.0, 4.0, 8.0, 8.0, 8.0])

    def test_negative_attempt_is_treated_as_the_first(self) -> None:
        self.assertEqual(reconnect_delay(-5, base_seconds=1, factor=2, max_seconds=8), 1.0)


class ConnectionTest(unittest.TestCase):
    def test_connect_marks_connected_and_notifies(self) -> None:
        ws = FakeWebSocket()
        connected: list[bool] = []
        transport, session = _transport_with(ws, on_connected=lambda: connected.append(True))

        asyncio.run(transport.connect())

        self.assertTrue(transport.connected)
        self.assertEqual(connected, [True])
        self.assertEqual(session.connect_calls, 1)
        self.assertEqual(session.connect_kwargs["url"], "ws://127.0.0.1:3001")

    def test_close_notifies_and_clears_state(self) -> None:
        ws = FakeWebSocket()
        disconnected: list[bool] = []
        transport, session = _transport_with(ws, on_disconnected=lambda: disconnected.append(True))
        asyncio.run(transport.connect())

        asyncio.run(transport.close())

        self.assertFalse(transport.connected)
        self.assertEqual(disconnected, [True])
        self.assertTrue(session.closed)

    def test_token_reaches_the_connection_url(self) -> None:
        ws = FakeWebSocket()
        session = FakeSession(ws)
        transport = OneBotWebSocketTransport(
            ws_url="ws://127.0.0.1:3001", token="secret", session_factory=lambda: session
        )

        asyncio.run(transport.connect())

        self.assertEqual(session.connect_kwargs["url"], "ws://127.0.0.1:3001?access_token=secret")


class SendActionTest(unittest.TestCase):
    def test_action_is_sent_and_response_resolved_by_echo(self) -> None:
        ws = FakeWebSocket()
        transport, _ = _transport_with(ws)

        async def run() -> dict:
            await transport.connect()
            # The response arrives over the socket, so a reader must be running
            # for the pending action to be resolved.
            reader = asyncio.ensure_future(transport.listen_once(timeout=1))
            sender = asyncio.ensure_future(
                transport.send_action(OneBotAction(action="get_login_info", params={}, echo="e1"))
            )
            await asyncio.sleep(0)
            ws.push(
                FakeFrame(
                    aiohttp.WSMsgType.TEXT,
                    json.dumps({"status": "ok", "retcode": 0, "echo": "e1"}),
                )
            )
            response = await sender
            await reader
            return response

        response = asyncio.run(run())

        self.assertEqual(response["retcode"], 0)
        sent = json.loads(ws.sent[0])
        self.assertEqual(sent["action"], "get_login_info")
        self.assertEqual(sent["echo"], "e1")

    def test_sending_while_disconnected_raises(self) -> None:
        ws = FakeWebSocket()
        transport, _ = _transport_with(ws)

        with self.assertRaises(OneBotTransportError):
            asyncio.run(transport.send_action(OneBotAction(action="get_login_info")))

    def test_missing_response_times_out_rather_than_reporting_success(self) -> None:
        ws = FakeWebSocket()
        transport, _ = _transport_with(ws, action_timeout=0.01)

        async def run() -> None:
            await transport.connect()
            await transport.send_action(OneBotAction(action="get_login_info", echo="e1"))

        with self.assertRaises(OneBotTransportError):
            asyncio.run(run())

        self.assertEqual(transport.pending_actions, 0)

    def test_closing_fails_pending_actions(self) -> None:
        ws = FakeWebSocket()
        transport, _ = _transport_with(ws)

        async def run() -> None:
            await transport.connect()
            task = asyncio.ensure_future(
                transport.send_action(OneBotAction(action="get_login_info", echo="e1"))
            )
            await asyncio.sleep(0)
            await transport.close()
            await task

        with self.assertRaises(OneBotTransportError):
            asyncio.run(run())


class DispatchTest(unittest.TestCase):
    def _transport(self, ws=None, **kwargs):
        return _transport_with(ws or FakeWebSocket(), **kwargs)[0]

    def test_message_event_is_dispatched_to_the_handler(self) -> None:
        seen: list[dict] = []
        transport = self._transport(on_event=seen.append)

        kind = asyncio.run(
            transport.dispatch_frame(
                FakeFrame(aiohttp.WSMsgType.TEXT, json.dumps({"post_type": "message", "user_id": 1}))
            )
        )

        self.assertEqual(kind, "event")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["post_type"], "message")

    def test_heartbeat_updates_the_timestamp(self) -> None:
        transport = self._transport()

        kind = asyncio.run(
            transport.dispatch_frame(
                FakeFrame(
                    aiohttp.WSMsgType.TEXT,
                    json.dumps({"post_type": "meta_event", "meta_event_type": "heartbeat"}),
                )
            )
        )

        self.assertEqual(kind, "heartbeat")
        self.assertIsInstance(transport.last_heartbeat_at, datetime)

    def test_other_meta_events_are_ignored(self) -> None:
        transport = self._transport()

        kind = asyncio.run(
            transport.dispatch_frame(
                FakeFrame(
                    aiohttp.WSMsgType.TEXT,
                    json.dumps({"post_type": "meta_event", "meta_event_type": "lifecycle"}),
                )
            )
        )

        self.assertEqual(kind, "ignored")

    def test_unparseable_frame_is_ignored(self) -> None:
        transport = self._transport()

        self.assertEqual(
            asyncio.run(transport.dispatch_frame(FakeFrame(aiohttp.WSMsgType.TEXT, "{not json"))),
            "ignored",
        )

    def test_binary_frame_is_decoded(self) -> None:
        seen: list[dict] = []
        transport = self._transport(on_event=seen.append)

        kind = asyncio.run(
            transport.dispatch_frame(
                FakeFrame(aiohttp.WSMsgType.BINARY, b'{"post_type": "message", "user_id": 2}')
            )
        )

        self.assertEqual(kind, "event")
        self.assertEqual(seen[0]["user_id"], 2)

    def test_close_frame_marks_disconnected(self) -> None:
        disconnected: list[bool] = []
        transport = self._transport(on_disconnected=lambda: disconnected.append(True))

        kind = asyncio.run(transport.dispatch_frame(FakeFrame(aiohttp.WSMsgType.CLOSED)))

        self.assertEqual(kind, "closed")
        self.assertEqual(disconnected, [True])

    def test_error_frame_raises(self) -> None:
        transport = self._transport()

        with self.assertRaises(OneBotTransportError):
            asyncio.run(transport.dispatch_frame(FakeFrame(aiohttp.WSMsgType.ERROR)))

    def test_untracked_response_is_ignored(self) -> None:
        """After a reconnect the caller has already given up on that echo."""
        transport = self._transport()

        kind = asyncio.run(
            transport.dispatch_frame(
                FakeFrame(aiohttp.WSMsgType.TEXT, json.dumps({"status": "ok", "echo": "stale"}))
            )
        )

        self.assertEqual(kind, "ignored")

    def test_event_without_a_handler_is_not_an_error(self) -> None:
        transport = self._transport()

        kind = asyncio.run(
            transport.dispatch_frame(
                FakeFrame(aiohttp.WSMsgType.TEXT, json.dumps({"post_type": "message"}))
            )
        )

        self.assertEqual(kind, "event")

    def test_async_event_handler_failure_does_not_break_the_listener(self) -> None:
        async def failing(payload):
            raise RuntimeError("handler boom")

        transport = self._transport(on_event=failing)

        async def run() -> str:
            kind = await transport.dispatch_frame(
                FakeFrame(aiohttp.WSMsgType.TEXT, json.dumps({"post_type": "message"}))
            )
            # Let the dispatched task run so its failure is logged, not raised.
            await asyncio.sleep(0.01)
            return kind

        self.assertEqual(asyncio.run(run()), "event")


class ListenOnceTest(unittest.TestCase):
    """A closed or silent connection must be reported, not read forever."""

    def test_closed_frame_is_reported_as_closed(self) -> None:
        ws = FakeWebSocket(frames=[FakeFrame(aiohttp.WSMsgType.CLOSED)])
        transport, _ = _transport_with(ws)
        asyncio.run(transport.connect())

        self.assertEqual(asyncio.run(transport.listen_once()), "closed")

    def test_silent_connection_reports_idle_instead_of_blocking(self) -> None:
        ws = FakeWebSocket(hang_when_empty=True)
        transport, _ = _transport_with(ws)
        asyncio.run(transport.connect())

        async def run() -> str:
            # Nothing will ever arrive; the read bound makes this return.
            return await asyncio.wait_for(transport.listen_once(timeout=0.01), timeout=2)

        self.assertEqual(asyncio.run(run()), "idle")

    def test_event_frame_is_dispatched_by_listen_once(self) -> None:
        ws = FakeWebSocket(
            frames=[FakeFrame(aiohttp.WSMsgType.TEXT, json.dumps({"post_type": "message"}))]
        )
        seen: list[dict] = []
        transport, _ = _transport_with(ws, on_event=seen.append)
        asyncio.run(transport.connect())

        self.assertEqual(asyncio.run(transport.listen_once(timeout=0.01)), "event")
        self.assertEqual(len(seen), 1)


class RunForeverTest(unittest.TestCase):
    def test_reconnects_after_a_lost_connection(self) -> None:
        first = FakeWebSocket(frames=[FakeFrame(aiohttp.WSMsgType.CLOSED)])
        second = FakeWebSocket()

        class SwitchingSession:
            def __init__(self) -> None:
                self.calls = 0
                self.closed = False

            async def ws_connect(self, url, **kwargs):
                self.calls += 1
                return first if self.calls == 1 else second

            async def close(self) -> None:
                self.closed = True

        session = SwitchingSession()
        transport = OneBotWebSocketTransport(
            ws_url="ws://127.0.0.1:3001",
            session_factory=lambda: session,
        )

        async def run() -> None:
            task = asyncio.ensure_future(transport.run_forever(max_attempts=2))
            # First connect ends immediately; the retry connects again.
            await asyncio.sleep(0.05)
            await transport.close()
            await asyncio.wait_for(task, timeout=2)

        asyncio.run(run())

        self.assertGreaterEqual(session.calls, 2)

    def test_idle_connection_triggers_a_reconnect(self) -> None:
        """A silent connection must not be waited on forever."""
        idle_then_closed = FakeWebSocket(hang_when_empty=True)
        healthy = FakeWebSocket(
            frames=[FakeFrame(aiohttp.WSMsgType.TEXT, json.dumps({"post_type": "message"}))]
        )

        class SwitchingSession:
            def __init__(self) -> None:
                self.calls = 0
                self.closed = False

            async def ws_connect(self, url, **kwargs):
                self.calls += 1
                return idle_then_closed if self.calls == 1 else healthy

            async def close(self) -> None:
                self.closed = True

        session = SwitchingSession()
        transport = OneBotWebSocketTransport(
            ws_url="ws://127.0.0.1:3001",
            session_factory=lambda: session,
            heartbeat_timeout=0.02,
            action_timeout=0.05,
        )

        async def run() -> int:
            task = asyncio.ensure_future(transport.run_forever(max_attempts=3))
            await asyncio.sleep(0.15)
            transport.request_stop()
            await asyncio.wait_for(task, timeout=2)
            return session.calls

        calls = asyncio.run(run())

        self.assertGreaterEqual(calls, 2)

    def test_max_attempts_stops_the_loop(self) -> None:
        class FailingSession:
            def __init__(self) -> None:
                self.calls = 0
                self.closed = False

            async def ws_connect(self, url, **kwargs):
                self.calls += 1
                raise ConnectionError("refused")

            async def close(self) -> None:
                self.closed = True

        session = FailingSession()
        transport = OneBotWebSocketTransport(
            ws_url="ws://127.0.0.1:3001",
            session_factory=lambda: session,
        )

        asyncio.run(transport.run_forever(max_attempts=2))

        self.assertEqual(session.calls, 2)
        self.assertEqual(transport.reconnect_attempts, 2)

    def test_request_stop_ends_the_loop_promptly(self) -> None:
        """Shutdown must not wait out a reconnect backoff."""

        class FailingSession:
            def __init__(self) -> None:
                self.calls = 0
                self.closed = False

            async def ws_connect(self, url, **kwargs):
                self.calls += 1
                raise ConnectionError("refused")

            async def close(self) -> None:
                self.closed = True

        session = FailingSession()
        transport = OneBotWebSocketTransport(
            ws_url="ws://127.0.0.1:3001",
            session_factory=lambda: session,
        )

        async def run() -> float:
            loop = asyncio.get_running_loop()
            started = loop.time()
            task = asyncio.ensure_future(
                transport.run_forever(
                    reconnect_base_seconds=30.0, reconnect_max_seconds=30.0
                )
            )
            await asyncio.sleep(0.02)
            transport.request_stop()
            await asyncio.wait_for(task, timeout=2)
            return loop.time() - started

        elapsed = asyncio.run(run())

        self.assertLess(elapsed, 1.0)
        self.assertEqual(session.calls, 1)

    def test_should_stop_reflects_the_stop_request(self) -> None:
        ws = FakeWebSocket()
        transport, _ = _transport_with(ws)

        self.assertFalse(transport.should_stop())
        transport.request_stop()
        self.assertTrue(transport.should_stop())


class SecretRedactionTest(unittest.TestCase):
    """Credentials must not reach the log, and the token travels in the URL."""

    def test_a_query_token_is_redacted_and_the_rest_is_kept(self) -> None:
        text = redact_secrets(
            "Connection timeout to host ws://h:3001/?access_token=s3cret&x=1"
        )

        self.assertNotIn("s3cret", text)
        self.assertIn("access_token=***", text)
        self.assertIn("x=1", text)

    def test_a_bearer_header_is_redacted(self) -> None:
        text = redact_secrets("Authorization: Bearer s3cret")

        self.assertNotIn("s3cret", text)
        self.assertIn("Bearer ***", text)

    def test_a_failing_reconnect_does_not_log_the_token(self) -> None:
        """The real regression: every retry used to write the token to disk."""

        class FailingSession:
            def __init__(self) -> None:
                self.closed = False

            async def ws_connect(self, url, **kwargs):
                # aiohttp embeds the full URL, token included, in this error.
                raise OSError(f"Connection timeout to host {url}")

            async def close(self) -> None:
                self.closed = True

        session = FailingSession()
        transport = OneBotWebSocketTransport(
            ws_url="ws://127.0.0.1:3001",
            token="s3cret-token",
            session_factory=lambda: session,
        )

        with self.assertLogs("services.onebot_transport", level="WARNING") as captured:
            asyncio.run(transport.run_forever(max_attempts=1))

        logged = "\n".join(captured.output)
        self.assertIn("连接中断", logged)
        self.assertIn("access_token=***", logged)
        self.assertNotIn("s3cret-token", logged)


if __name__ == "__main__":
    unittest.main()
