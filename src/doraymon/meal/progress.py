"""Bounded stage messages for one retrieval task."""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress


class SearchProgress:
    def __init__(
        self,
        send: Callable[[str], Awaitable[bool]],
        *,
        delay: float = 2,
        interval: float = 8,
        limit: int = 3,
        send_timeout: float = 3,
    ):
        self._send = send
        self._delay, self._interval = delay, interval
        self._limit, self._send_timeout = limit, send_timeout
        self._stage = ""
        self._changed = asyncio.Event()
        self._task: asyncio.Task | None = None

    def stage(self, text: str) -> None:
        if text != self._stage:
            self._stage = text
            self._changed.set()

    async def __aenter__(self):
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *exc):
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        await asyncio.sleep(self._delay)
        previous = ""
        for _ in range(self._limit):
            while not self._stage or self._stage == previous:
                self._changed.clear()
                await self._changed.wait()
            current = self._stage
            try:
                async with asyncio.timeout(self._send_timeout):
                    confirmed = await self._send(current)
            except (TimeoutError, OSError):
                return
            if not confirmed:
                return
            previous = current
            await asyncio.sleep(self._interval)
