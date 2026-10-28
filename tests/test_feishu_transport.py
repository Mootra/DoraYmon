"""The Feishu transport: the three-second contract, and nothing blocking.

Feishu redelivers an event whose handler has not returned within three seconds,
so the property under test here is not "does it deliver" but "does the event
handler ever wait for the model". Everything else follows from that.
"""

from __future__ import annotations

import asyncio
import threading
import unittest

from services.feishu_adapter import ACTION_BOT_INFO, FeishuAction
from services.feishu_transport import (
    FeishuLongConnectionTransport,
)


class FakeEventSource:
    """Stands in for the SDK's long connection.

    With no ``behaviour``, ``start`` blocks until ``stop`` is called — which is
    what a healthy connection looks like from the transport's point of view. A
    ``behaviour`` that returns immediately models the SDK giving up.
    """

    def __init__(self, *, announce_ready: bool = True, behaviour=None) -> None:
        self.starts = 0
        self.stops = 0
        self.thread_ids: list[int] = []
        self._announce_ready = announce_ready
        self._behaviour = behaviour
        self._release = threading.Event()
        self.payload_sink = None

    def start(self, *, on_payload=None, on_ready=None):
        self.starts += 1
        self.thread_ids.append(threading.get_ident())
        self.payload_sink = on_payload
        if on_ready is not None and self._announce_ready:
            on_ready()
        if self._behaviour is not None:
            self._behaviour(self)
            return
        self._release.wait(timeout=5)

    def stop(self):
        self.stops += 1
        self._release.set()


class RecordingSender:
    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.actions = []
        self.thread_ids: list[int] = []
        self.raises = raises

    def send_action(self, action):
        self.actions.append(action)
        self.thread_ids.append(threading.get_ident())
        if self.raises is not None:
            raise self.raises
        return {"code": 0, "msg": "ok", "data": {}}


async def _settle(predicate, *, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition was not met in time")
        await asyncio.sleep(0.005)


def _transport(*, source=None, sender=None, on_event=None, **kwargs):
    return FeishuLongConnectionTransport(
        event_source=source or FakeEventSource(),
        sender=sender or RecordingSender(),
        on_event=on_event or (lambda payload: asyncio.sleep(0)),
        **kwargs,
    )


class AckDisciplineTest(unittest.TestCase):
    def test_submit_returns_before_the_handler_finishes(self) -> None:
        """The event handler must never wait for the model."""

        async def scenario() -> None:
            release = asyncio.Event()
            handled: list[str] = []

            async def on_event(payload):
                await release.wait()
                handled.append(payload)

            transport = _transport(on_event=on_event)
            run = asyncio.create_task(transport.run_forever())
            await _settle(lambda: transport.ready)

            self.assertTrue(transport.submit("first"))
            await _settle(lambda: transport.queued == 0)
            # Queued and being handled, but submit already returned.
            self.assertEqual(handled, [])

            release.set()
            await asyncio.wait_for(transport.drain(), timeout=2)
            self.assertEqual(handled, ["first"])

            transport.request_stop()
            await asyncio.wait_for(run, timeout=5)

        asyncio.run(scenario())

    def test_a_handled_event_is_counted(self) -> None:
        async def scenario() -> None:
            transport = _transport()
            run = asyncio.create_task(transport.run_forever())
            await _settle(lambda: transport.ready)

            self.assertTrue(transport.submit("x"))
            await asyncio.wait_for(transport.drain(), timeout=2)

            self.assertEqual(transport.accepted, 1)
            self.assertEqual(transport.handled, 1)
            self.assertEqual(transport.refused, 0)
            self.assertEqual(transport.failures, 0)

            transport.request_stop()
            await asyncio.wait_for(run, timeout=5)

        asyncio.run(scenario())

    def test_submit_before_the_connection_is_refused(self) -> None:
        """No queue means no connection; refusing beats buffering forever."""
        transport = _transport()
        self.assertFalse(transport.submit("x"))
        self.assertEqual(transport.refused, 1)
        self.assertEqual(transport.accepted, 0)

    def test_submit_after_close_is_refused(self) -> None:
        async def scenario() -> None:
            transport = _transport()
            run = asyncio.create_task(transport.run_forever())
            await _settle(lambda: transport.ready)

            await transport.close()
            self.assertFalse(transport.submit("x"))
            self.assertEqual(transport.refused, 1)

            await asyncio.wait_for(run, timeout=5)

        asyncio.run(scenario())

    def test_two_events_for_a_one_slot_queue_refuse_the_second(self) -> None:
        """Blocking here would make Feishu redeliver, hiding the drop."""

        async def scenario() -> None:
            release = asyncio.Event()

            async def on_event(payload):
                await release.wait()

            transport = _transport(on_event=on_event, max_queued_events=1)
            run = asyncio.create_task(transport.run_forever())
            await _settle(lambda: transport.ready)

            # Both submits land before the loop runs, so nothing has consumed
            # anything yet: one fits, the other is refused rather than waited on.
            self.assertTrue(transport.submit("a"))
            self.assertFalse(transport.submit("b"))
            await _settle(lambda: transport.accepted + transport.refused == 2)

            self.assertEqual(transport.accepted, 1)
            self.assertEqual(transport.refused, 1)

            release.set()
            transport.request_stop()
            await asyncio.wait_for(run, timeout=5)

        asyncio.run(scenario())

    def test_submit_refuses_at_once_when_the_queue_is_already_full(self) -> None:
        """The refusal happens on the caller's thread, before any waiting."""

        async def scenario() -> None:
            release = asyncio.Event()

            async def on_event(payload):
                await release.wait()

            transport = _transport(on_event=on_event, max_queued_events=1, max_event_tasks=1)
            run = asyncio.create_task(transport.run_forever())
            await _settle(lambda: transport.ready)

            self.assertTrue(transport.submit("a"))
            await _settle(lambda: transport.accepted == 1)
            await _settle(lambda: transport.queued == 0)  # worker took it

            self.assertTrue(transport.submit("b"))
            await _settle(lambda: transport.queued == 1)

            self.assertFalse(transport.submit("c"))
            self.assertEqual(transport.refused, 1)
            self.assertEqual(transport.accepted, 2)

            release.set()
            transport.request_stop()
            await asyncio.wait_for(run, timeout=5)

        asyncio.run(scenario())

    def test_a_failing_handler_does_not_end_the_worker(self) -> None:
        async def scenario() -> None:
            attempts: list[str] = []

            async def on_event(payload):
                attempts.append(payload)
                if payload == "bad":
                    raise RuntimeError("boom")

            transport = _transport(on_event=on_event)
            run = asyncio.create_task(transport.run_forever())
            await _settle(lambda: transport.ready)

            transport.submit("bad")
            await asyncio.wait_for(transport.drain(), timeout=2)
            transport.submit("good")
            await asyncio.wait_for(transport.drain(), timeout=2)

            self.assertEqual(attempts, ["bad", "good"])
            self.assertEqual(transport.failures, 1)
            self.assertEqual(transport.handled, 1)

            transport.request_stop()
            await asyncio.wait_for(run, timeout=5)

        asyncio.run(scenario())

    def test_the_source_is_given_the_transport_queue_as_its_sink(self) -> None:
        """The SDK callback path is the transport's own ``submit``."""

        async def scenario() -> None:
            source = FakeEventSource()
            transport = _transport(source=source)
            run = asyncio.create_task(transport.run_forever())
            await _settle(lambda: transport.ready)

            self.assertIsNotNone(source.payload_sink)
            self.assertTrue(source.payload_sink("from-source"))
            await asyncio.wait_for(transport.drain(), timeout=2)
            self.assertEqual(transport.handled, 1)

            transport.request_stop()
            await asyncio.wait_for(run, timeout=5)

        asyncio.run(scenario())

    def test_two_workers_share_the_queue(self) -> None:
        async def scenario() -> None:
            release = asyncio.Event()
            started: list[str] = []

            async def on_event(payload):
                started.append(payload)
                await release.wait()

            transport = _transport(on_event=on_event, worker_count=2)
            run = asyncio.create_task(transport.run_forever())
            await _settle(lambda: transport.ready)

            transport.submit("a")
            transport.submit("b")
            await _settle(lambda: len(started) == 2)
            self.assertEqual(sorted(started), ["a", "b"])
            self.assertEqual(transport.queued, 0)

            release.set()
            transport.request_stop()
            await asyncio.wait_for(run, timeout=5)

        asyncio.run(scenario())


class SendTest(unittest.TestCase):
    def test_send_runs_the_blocking_sender_off_the_loop(self) -> None:
        """``requests`` blocks; a stalled reply must not stop the workers."""

        async def scenario() -> None:
            sender = RecordingSender()
            transport = _transport(sender=sender)
            loop_thread = threading.get_ident()

            result = await transport.send_action(
                FeishuAction(name=ACTION_BOT_INFO, params={})
            )

            self.assertEqual(result["code"], 0)
            self.assertEqual(len(sender.thread_ids), 1)
            self.assertNotEqual(sender.thread_ids[0], loop_thread)
            self.assertEqual(threading.get_ident(), loop_thread)

        asyncio.run(scenario())

    def test_a_sender_failure_propagates_unchanged(self) -> None:
        async def scenario() -> None:
            transport = _transport(sender=RecordingSender(raises=ConnectionError("down")))
            with self.assertRaises(ConnectionError):
                await transport.send_action(FeishuAction(name=ACTION_BOT_INFO, params={}))

        asyncio.run(scenario())


class LifecycleTest(unittest.TestCase):
    def test_connected_is_reported_only_when_the_source_is_ready(self) -> None:
        async def scenario() -> None:
            events: list[str] = []
            source = FakeEventSource(announce_ready=False)
            transport = _transport(
                source=source, on_connected=lambda: events.append("connected")
            )
            run = asyncio.create_task(transport.run_forever())
            await _settle(lambda: source.starts == 1)
            await asyncio.sleep(0.05)

            self.assertEqual(events, [])
            self.assertFalse(transport.ready)

            transport.request_stop()
            await asyncio.wait_for(run, timeout=5)

        asyncio.run(scenario())

    def test_connected_is_reported_once_the_source_says_so(self) -> None:
        async def scenario() -> None:
            events: list[str] = []
            transport = _transport(on_connected=lambda: events.append("connected"))
            run = asyncio.create_task(transport.run_forever())
            await _settle(lambda: transport.ready)
            self.assertEqual(events, ["connected"])

            transport.request_stop()
            await asyncio.wait_for(run, timeout=5)

        asyncio.run(scenario())

    def test_the_source_runs_off_the_event_loop(self) -> None:
        """The SDK blocks; blocking the loop would stop every worker."""

        async def scenario() -> None:
            source = FakeEventSource()
            transport = _transport(source=source)
            run = asyncio.create_task(transport.run_forever())
            await _settle(lambda: transport.ready)

            self.assertNotEqual(source.thread_ids[0], threading.get_ident())

            transport.request_stop()
            await asyncio.wait_for(run, timeout=5)

        asyncio.run(scenario())

    def test_request_stop_closes_the_source_and_the_run_loop(self) -> None:
        async def scenario() -> None:
            events: list[str] = []
            source = FakeEventSource()
            transport = _transport(
                source=source, on_disconnected=lambda: events.append("disconnected")
            )
            run = asyncio.create_task(transport.run_forever())
            await _settle(lambda: transport.ready)

            transport.request_stop()
            await asyncio.wait_for(run, timeout=5)

            self.assertEqual(source.stops, 1)
            self.assertTrue(transport.should_stop())
            self.assertFalse(transport.ready)
            self.assertEqual(events, ["disconnected"])
            # A requested stop is not a reconnect.
            self.assertEqual(source.starts, 1)

        asyncio.run(scenario())

    def test_the_run_loop_reconnects_with_bounded_attempts(self) -> None:
        async def scenario() -> None:
            source = FakeEventSource(behaviour=lambda fake: None)
            transport = _transport(source=source)
            await asyncio.wait_for(
                transport.run_forever(
                    max_attempts=3,
                    reconnect_base_seconds=0.01,
                    reconnect_max_seconds=0.02,
                ),
                timeout=5,
            )
            self.assertEqual(source.starts, 3)
            self.assertEqual(transport.reconnect_attempts, 3)

        asyncio.run(scenario())

    def test_a_lost_connection_is_reported_before_reconnecting(self) -> None:
        async def scenario() -> None:
            events: list[str] = []
            source = FakeEventSource(behaviour=lambda fake: None)
            transport = _transport(
                source=source,
                on_connected=lambda: events.append("connected"),
                on_disconnected=lambda: events.append("disconnected"),
            )
            await asyncio.wait_for(
                transport.run_forever(
                    max_attempts=2,
                    reconnect_base_seconds=0.01,
                    reconnect_max_seconds=0.02,
                ),
                timeout=5,
            )
            self.assertEqual(events, ["connected", "disconnected", "connected", "disconnected"])
            self.assertFalse(transport.ready)

        asyncio.run(scenario())

    def test_shutdown_is_immediate_during_a_backoff(self) -> None:
        """Waiting on the stop signal keeps a 60 s backoff from delaying exit."""

        async def scenario() -> None:
            source = FakeEventSource(behaviour=lambda fake: None)
            transport = _transport(source=source)
            run = asyncio.create_task(
                transport.run_forever(
                    reconnect_base_seconds=30.0, reconnect_max_seconds=60.0
                )
            )
            # First failure schedules a 30 s wait; stopping must not wait it out.
            await _settle(lambda: transport.reconnect_attempts == 1)
            transport.request_stop()
            await asyncio.wait_for(run, timeout=5)
            self.assertEqual(source.starts, 1)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
