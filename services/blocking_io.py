"""Bounded thread work whose capacity survives cancellation of its awaiter."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial
import threading


class BlockingIOBusy(RuntimeError):
    delivery_unknown = False


class BoundedExecutor:
    def __init__(self, *, max_workers: int = 4, thread_name_prefix: str = "nearby-io") -> None:
        self._slots = threading.BoundedSemaphore(max_workers)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=thread_name_prefix,
        )

    async def run(self, function, /, *args, **kwargs):
        # Do not let repeated clear/cancel operations fill an executor's hidden
        # unbounded queue. Running requests keep their slot until they finish.
        if not self._slots.acquire(blocking=False):
            raise BlockingIOBusy("blocking I/O capacity exhausted")
        try:
            work = partial(function, *args, **kwargs)
            future = self._executor.submit(copy_context().run, work)
        except BaseException:
            self._slots.release()
            raise
        future.add_done_callback(lambda _: self._slots.release())
        return await asyncio.wrap_future(future)


LOOKUP_IO = BoundedExecutor()
MODEL_IO = BoundedExecutor(thread_name_prefix="model-io")
