"""Ingress stays bounded without making the reader wait for a model."""
from __future__ import annotations

import asyncio
import unittest

from services.feishu_transport import FeishuLongConnectionTransport
from services.onebot_transport import OneBotWebSocketTransport
from tests.test_feishu_transport import FakeEventSource, RecordingSender, _settle


class TransportBackpressureTest(unittest.IsolatedAsyncioTestCase):
    async def test_onebot_bounds_tasks_and_close_cancels_every_handler(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def handle(payload):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        transport = OneBotWebSocketTransport(ws_url="ws://synthetic", on_event=handle, max_event_tasks=1)
        transport._schedule_event({"id": "first"})
        await started.wait()
        transport._schedule_event({"id": "second"})
        self.assertEqual(transport.pending_events, 1)
        self.assertEqual(transport.refused_events, 1)
        await transport.close()
        self.assertTrue(cancelled.is_set())
        self.assertEqual(transport.pending_events, 0)

    async def test_feishu_single_dispatch_worker_does_not_serialize_sessions(self):
        first_started, release, second_done = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def handle(payload):
            if payload == "slow":
                first_started.set()
                await release.wait()
            else:
                second_done.set()

        transport = FeishuLongConnectionTransport(
            event_source=FakeEventSource(), sender=RecordingSender(), on_event=handle,
            worker_count=1, max_event_tasks=2,
        )
        run = asyncio.create_task(transport.run_forever())
        try:
            await _settle(lambda: transport.ready)
            self.assertTrue(transport.submit("slow"))
            await first_started.wait()
            self.assertTrue(transport.submit("fast"))
            await asyncio.wait_for(second_done.wait(), 1)
            self.assertFalse(release.is_set())
            release.set()
            await transport.drain()
            self.assertEqual(transport.handled, 2)
        finally:
            await transport.close()
            await run

    async def test_feishu_reserves_queue_capacity_before_callbacks_run(self):
        transport = FeishuLongConnectionTransport(
            event_source=FakeEventSource(), sender=RecordingSender(),
            on_event=lambda payload: asyncio.sleep(0), max_queued_events=2,
        )
        transport._loop = asyncio.get_running_loop()
        transport._queue = asyncio.Queue(maxsize=2)
        self.assertEqual([transport.submit(i) for i in range(10)], [True, True] + [False] * 8)
        await asyncio.sleep(0)
        self.assertEqual(transport.queued, 2)
        await transport.close()
        await transport.drain()
        self.assertEqual(transport._reserved, 0)

    async def test_multiple_feishu_dispatchers_share_the_inflight_limit(self):
        started = asyncio.Event()
        calls = []

        async def handle(payload):
            calls.append(payload)
            started.set()
            await asyncio.Event().wait()

        transport = FeishuLongConnectionTransport(
            event_source=FakeEventSource(), sender=RecordingSender(), on_event=handle,
            worker_count=4, max_event_tasks=1,
        )
        run = asyncio.create_task(transport.run_forever())
        try:
            await _settle(lambda: transport.ready)
            for i in range(8):
                self.assertTrue(transport.submit(i))
            await started.wait()
            await asyncio.sleep(0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(transport._event_tasks), 1)
        finally:
            await transport.close()
            await run
        await transport.drain()
        self.assertEqual(transport._event_tasks, set())
