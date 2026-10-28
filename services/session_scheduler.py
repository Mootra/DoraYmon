"""Bounded FIFO per storage session, with concurrency across sessions."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable, Hashable
from typing import TypeVar

T = TypeVar("T")
DEFAULT_MAX_CONCURRENT = 4
DEFAULT_MAX_PENDING = 64
DEFAULT_MAX_PER_SESSION = 8


class ScheduleRejected(RuntimeError):
    """Admission failed before any handler or model was started."""


class SessionScheduler:
    def __init__(
        self, *, max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        max_pending: int = DEFAULT_MAX_PENDING,
        max_per_session: int = DEFAULT_MAX_PER_SESSION,
    ) -> None:
        if min(max_concurrent, max_pending, max_per_session) < 1:
            raise ValueError("scheduler limits must be positive")
        self.max_pending = max_pending
        self.max_per_session = max_per_session
        self._slots = asyncio.Semaphore(max_concurrent)
        self._locks: dict[Hashable, asyncio.Lock] = {}
        self._tasks: dict[Hashable, set[asyncio.Task]] = defaultdict(set)
        self._pending = 0
        self._closed = False

    @property
    def pending(self) -> int:
        return self._pending

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def session_keys(self) -> tuple[Hashable, ...]:
        return tuple(self._tasks)

    async def run(
        self, key: Hashable, work: Callable[[], Awaitable[T]], *, interrupt: bool = False,
        prepare: Callable[[], Awaitable[None]] | None = None,
    ) -> T:
        if self._closed:
            raise ScheduleRejected("closed")
        # Reserve bounded admission for controls so a full generation queue
        # cannot prevent a clear. Queue the control before yielding to ensure
        # newer messages wait behind it rather than racing the actual clear.
        if self._pending >= self.max_pending * (2 if interrupt else 1):
            raise ScheduleRejected("queue_full")
        if not interrupt and len(self._tasks.get(key, ())) >= self.max_per_session:
            raise ScheduleRejected("session_queue_full")
        if interrupt:
            for old in tuple(self._tasks.get(key, ())):
                old.cancel()
        lock = self._locks.setdefault(key, asyncio.Lock())

        async def execute() -> T:
            # Take the session lock before a global slot: queued turns from one
            # slow session must not occupy every slot. asyncio.Lock is FIFO and
            # cancelling a waiter cannot let its successor bypass the owner.
            async with lock:
                if interrupt:
                    return await work()
                if prepare is not None:
                    # Input coalescing retains FIFO admission but must not use a
                    # generation slot while waiting for a short supplement.
                    await prepare()
                async with self._slots:
                    return await work()

        task = asyncio.create_task(execute(), name="session-turn")
        self._pending += 1
        self._tasks[key].add(task)
        task.add_done_callback(lambda done: self._release(key, done))
        return await task

    def _release(self, key: Hashable, task: asyncio.Task) -> None:
        self._pending -= 1
        bucket = self._tasks[key]
        bucket.discard(task)
        if not bucket:
            del self._tasks[key]
            self._locks.pop(key, None)

    async def cancel_session(self, key: Hashable) -> None:
        tasks = tuple(self._tasks.get(key, ()))
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def cancel_all(self) -> None:
        current = asyncio.current_task()
        tasks = tuple(task for bucket in self._tasks.values() for task in bucket if task is not current)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        self._closed = True
        await self.cancel_all()
