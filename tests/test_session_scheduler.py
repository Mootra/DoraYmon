from __future__ import annotations

import asyncio
import unittest

from services.session_scheduler import ScheduleRejected, SessionScheduler


class SessionSchedulerTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_middle_waiter_does_not_bypass_the_running_turn(self):
        scheduler = SessionScheduler(max_concurrent=2)
        release, started = asyncio.Event(), asyncio.Event()
        order = []

        async def first():
            started.set()
            await release.wait()
            order.append("first")

        async def later():
            order.append("later")

        one = asyncio.create_task(scheduler.run("same", first))
        await started.wait()
        two = asyncio.create_task(scheduler.run("same", later))
        three = asyncio.create_task(scheduler.run("same", later))
        await asyncio.sleep(0)
        two.cancel()
        await asyncio.gather(two, return_exceptions=True)
        self.assertEqual(order, [])
        release.set()
        await asyncio.gather(one, three)
        self.assertEqual(order, ["first", "later"])
        self.assertEqual(scheduler.pending, 0)

    async def test_per_session_and_global_admission_are_bounded(self):
        scheduler = SessionScheduler(max_concurrent=1, max_pending=2, max_per_session=1)
        release, started = asyncio.Event(), asyncio.Event()

        async def blocked():
            started.set()
            await release.wait()

        first = asyncio.create_task(scheduler.run("alice", blocked))
        await started.wait()
        with self.assertRaises(ScheduleRejected):
            await scheduler.run("alice", blocked)
        second = asyncio.create_task(scheduler.run("bob", blocked))
        await asyncio.sleep(0)
        with self.assertRaises(ScheduleRejected):
            await scheduler.run("carol", blocked)
        await scheduler.close()
        await asyncio.gather(first, second, return_exceptions=True)
        self.assertEqual(scheduler.pending, 0)
        with self.assertRaises(ScheduleRejected):
            await scheduler.run("alice", blocked)

    async def test_failed_turn_does_not_poison_following_turn(self):
        scheduler = SessionScheduler()

        async def fails():
            raise ValueError("synthetic failure")

        async def succeeds():
            return "ok"

        with self.assertRaises(ValueError):
            await scheduler.run("same", fails)
        self.assertEqual(await scheduler.run("same", succeeds), "ok")
