"""Feishu long-connection transport: receive events, send actions.

Two properties of Feishu shape this class, which is why it is not the OneBot
transport under another name:

1. **Three seconds.** Feishu redelivers an event whose handler has not returned
   within three seconds. Answering means routing and then running the model,
   which takes far longer, so the event handler must never await the session: it
   hands the payload to a queue and returns. The adapter's duplicate window is
   the second line of defence for anything that is still redelivered.

2. **No inbound address.** The SDK opens the connection outbound, so there is no
   port to expose, no callback URL and no request signature to verify. That is
   the point of this channel — it replaces an account a third party can log out
   with an app identity that cannot be.

The SDK is reached through ``EventSource``, so the transport is verifiable
offline with a fake and the dependency stays out of every other entry point's
import path. Sending goes through ``services.feishu_api``, which uses
``requests`` and therefore runs in a thread: a stalled reply must not stop the
workers and spend the three-second budget with them.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import threading
from typing import Any, Awaitable, Callable, Protocol
from services.blocking_io import BoundedExecutor

from services.reconnect import (
    DEFAULT_RECONNECT_BASE_SECONDS,
    DEFAULT_RECONNECT_MAX_SECONDS,
    reconnect_delay,
)

logger = logging.getLogger(__name__)

DEFAULT_WORKER_COUNT = 1
# Generous enough for a burst after a reconnect, small enough that a stalled
# session cannot grow the process without bound.
DEFAULT_MAX_QUEUED_EVENTS = 256
DEFAULT_MAX_EVENT_TASKS = 64
FEISHU_IO = BoundedExecutor(thread_name_prefix="feishu-io")


class FeishuTransportError(Exception):
    """The long connection could not be established, or was lost."""


class EventSource(Protocol):
    """The slice of the Feishu SDK this transport drives."""

    def start(
        self,
        *,
        on_payload: Callable[[Any], bool],
        on_ready: Callable[[], None] | None = None,
    ) -> None:
        """Open the long connection and block until it closes.

        ``on_payload`` is called for every inbound event and must return without
        waiting for anything: Feishu redelivers an event whose handler has not
        returned within three seconds, and answering takes far longer.

        ``on_ready`` must be called once the connection is actually established,
        never before: the session uses that signal to tell a first connect from a
        reconnect.
        """

    def stop(self) -> None:
        """Ask the connection to close. Must be safe to call from any thread."""


class FeishuLongConnectionTransport:
    """Queues inbound events and sends actions through the Feishu API."""

    def __init__(
        self,
        *,
        event_source: EventSource,
        sender: Any,
        on_event: Callable[[Any], Awaitable[None]],
        on_connected: Callable[[], None] | None = None,
        on_disconnected: Callable[[], None] | None = None,
        worker_count: int = DEFAULT_WORKER_COUNT,
        max_queued_events: int = DEFAULT_MAX_QUEUED_EVENTS,
        max_event_tasks: int = DEFAULT_MAX_EVENT_TASKS,
    ) -> None:
        self.event_source = event_source
        self.sender = sender
        self.on_event = on_event
        self.on_connected = on_connected
        self.on_disconnected = on_disconnected
        self.worker_count = max(1, int(worker_count))
        self.max_queued_events = max(1, int(max_queued_events))
        self.max_event_tasks = max(1, int(max_event_tasks))

        # Counters exist so a silent bot can be explained after a real
        # deployment: accepted vs refused is the difference between "Feishu
        # never called us" and "we dropped what it sent".
        self.accepted = 0
        self.refused = 0
        self.handled = 0
        self.failures = 0
        self.reconnect_attempts = 0
        self.ready = False

        self._queue: asyncio.Queue[Any] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = asyncio.Event()
        self._workers: list[asyncio.Task] = []
        self._closed = False
        self._event_tasks: set[asyncio.Task] = set()
        self._admission_lock = threading.Lock()
        self._reserved = 0
        self._event_slots = asyncio.Semaphore(self.max_event_tasks)

    # -- inbound ------------------------------------------------------------

    @property
    def queued(self) -> int:
        """Accepted but not yet handled. Diagnostic; nothing waits on this."""
        return self._queue.qsize() if self._queue is not None else 0

    def submit(self, payload: Any) -> bool:
        """Queue one inbound event and return immediately.

        This is the three-second contract, and the SDK calls it from its own
        thread. A full queue *refuses and counts* the event instead of blocking:
        blocking here makes Feishu redeliver, which trades a visible drop for a
        silent duplicate.
        """
        loop = self._loop
        queue = self._queue
        if self._closed or loop is None or queue is None:
            self.refused += 1
            return False
        with self._admission_lock:
            # Include callbacks not yet enqueued: an SDK burst must not create
            # an unbounded call_soon_threadsafe backlog in front of the queue.
            if self._reserved >= self.max_queued_events:
                self.refused += 1
                return False
            self._reserved += 1
        try:
            loop.call_soon_threadsafe(self._enqueue, payload)
        except RuntimeError:
            # The loop is already closed: shutdown won the race.
            self.refused += 1
            self._release_reservation()
            return False
        return True

    def _release_reservation(self) -> None:
        with self._admission_lock:
            self._reserved -= 1

    def _enqueue(self, payload: Any) -> None:
        """Runs on the event loop, so the counters need no lock."""
        queue = self._queue
        if queue is None or self._closed:
            self.refused += 1
            self._release_reservation()
            return
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            self.refused += 1
            self._release_reservation()
            logger.warning("飞书事件队列已满，本条事件被丢弃（累计丢弃 %d 条）", self.refused)
            return
        self.accepted += 1

    async def _worker(self, index: int) -> None:
        queue = self._queue
        if queue is None:  # pragma: no cover - run_forever always builds it
            return
        while True:
            await self._event_slots.acquire()
            try:
                payload = await queue.get()
            except BaseException:
                self._event_slots.release()
                raise
            self._release_reservation()
            task = asyncio.create_task(self._handle_event(payload))
            self._event_tasks.add(task)
            task.add_done_callback(lambda done: self._event_finished(done, queue))

    def _event_finished(self, task: asyncio.Task, queue: asyncio.Queue) -> None:
        self._event_tasks.discard(task)
        self._event_slots.release()
        queue.task_done()

    async def _handle_event(self, payload: Any) -> None:
        try:
            await self.on_event(payload)
            self.handled += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failures += 1
            logger.exception("飞书事件处理失败")

    async def drain(self) -> None:
        """Wait until every event submitted so far has been handled.

        Used by tests and by shutdown. The event loop never waits here — that
        would reintroduce the thing this class exists to avoid.
        """
        if self._queue is None:
            return
        # ``submit`` schedules the enqueue rather than performing it, so without
        # this a drain right after a submit would see an empty queue and return
        # as if the work were already done.
        await asyncio.sleep(0)
        await self._queue.join()

    # -- outbound -----------------------------------------------------------

    async def send_action(self, action: Any) -> dict[str, Any]:
        return await FEISHU_IO.run(self.sender.send_action, action)

    # -- lifecycle ----------------------------------------------------------

    def _notify_ready_from_source(self) -> None:
        """Called by the source from its own thread; marshal onto the loop."""
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._mark_ready)
        except RuntimeError:
            pass

    def _mark_ready(self) -> None:
        if self._closed:
            return
        self.ready = True
        if self.on_connected is not None:
            self.on_connected()

    def _mark_lost(self) -> None:
        self.ready = False
        if self.on_disconnected is not None:
            self.on_disconnected()

    def request_stop(self) -> None:
        """Ask the run loop to finish. Safe to call from a signal handler.

        Idempotent, and that matters: a signal handler, an explicit shutdown and
        the entry's own ``finally`` can all reach here, and a long connection
        that is told to close three times is a connection whose SDK ends up
        being asked to do contradictory things.
        """
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        try:
            self.event_source.stop()
        except Exception:
            logger.warning("请求关闭飞书长连接时出错", exc_info=True)

    def should_stop(self) -> bool:
        return self._stop.is_set()

    async def close(self) -> None:
        self.request_stop()
        await self._shutdown_workers()

    async def _shutdown_workers(self) -> None:
        workers, self._workers = self._workers, []
        for worker in workers:
            worker.cancel()
        for worker in workers:
            try:
                await worker
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("飞书事件 worker 退出时出错", exc_info=True)
        tasks = tuple(self._event_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._queue is not None:
            while not self._queue.empty():
                self._queue.get_nowait()
                self._release_reservation()
                self._queue.task_done()

    async def run_forever(
        self,
        *,
        max_attempts: int | None = None,
        reconnect_base_seconds: float = DEFAULT_RECONNECT_BASE_SECONDS,
        reconnect_max_seconds: float = DEFAULT_RECONNECT_MAX_SECONDS,
    ) -> None:
        """Open the long connection, keep it open, and reconnect with backoff.

        The SDK reconnects on its own; this loop is for the case where it gives
        up entirely, which is reported as ``start`` returning without a stop
        having been requested.
        """
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=self.max_queued_events)
        self._workers = [
            asyncio.create_task(self._worker(index), name=f"feishu-worker-{index}")
            for index in range(self.worker_count)
        ]
        attempt = 0
        try:
            while not self.should_stop():
                try:
                    await self._loop.run_in_executor(
                        None,
                        functools.partial(
                            self.event_source.start,
                            on_payload=self.submit,
                            on_ready=self._notify_ready_from_source,
                        ),
                    )
                    # A readiness callback scheduled by the source's own thread
                    # may still be queued behind us; letting it land keeps
                    # "connected" from being reported after "disconnected".
                    await asyncio.sleep(0)
                    if self.should_stop():
                        break
                    # ``start`` returned without being asked to: the SDK gave up
                    # on the connection.
                    raise FeishuTransportError("飞书长连接已断开")
                except Exception as error:
                    if self.should_stop():
                        break
                    self.reconnect_attempts += 1
                    delay = reconnect_delay(
                        attempt,
                        base_seconds=reconnect_base_seconds,
                        max_seconds=reconnect_max_seconds,
                    )
                    logger.warning("飞书长连接中断（%s），%.1f 秒后重试", error, delay)
                    attempt += 1
                    if max_attempts is not None and attempt >= max_attempts:
                        logger.error("飞书长连接重连次数已达上限，停止")
                        return
                    # Waiting on the stop signal keeps shutdown immediate even in
                    # the middle of a long backoff.
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        pass
                finally:
                    # Exactly once per attempt, however it ended: while we are
                    # between attempts the link is not up. Marking it in the
                    # individual branches instead reported a loss twice, because
                    # the normal-return path raises into the handler below it.
                    self._mark_lost()
        finally:
            self.request_stop()
            await self._shutdown_workers()
