import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .models import Incoming, InputPart

Sender = Callable[[str], Awaitable[None]]
Processor = Callable[[Incoming, Sender], Awaitable[bool]]


class BusyError(Exception):
    pass


@dataclass
class Request:
    event: Incoming
    send: Sender
    done: asyncio.Future
    control: Processor | None = None


@dataclass
class Lane:
    queue: asyncio.Queue
    task: asyncio.Task | None = None
    outstanding: list[Request] = field(default_factory=list)
    previous: asyncio.Task | None = None


class Conversations:
    def __init__(
        self,
        process: Processor,
        *,
        max_sessions: int = 128,
        queue_size: int = 8,
        gap: float = 0.35,
        window: float = 1.5,
    ):
        self.process, self.max_sessions, self.queue_size = process, max_sessions, queue_size
        self.gap, self.window = gap, window
        self.lanes: dict[str, Lane] = {}
        self.closed = False

    async def submit(
        self, event: Incoming, send: Sender, *, control: Processor | None = None
    ) -> bool:
        if self.closed:
            raise BusyError("closed")
        key = event.session
        lane = self.lanes.get(key)
        if control is not None and lane is not None:
            previous = lane.task
            if previous:
                previous.cancel()
            lane = self.lanes[key] = Lane(asyncio.Queue(self.queue_size), previous=previous)
        if lane is None:
            if len(self.lanes) >= self.max_sessions:
                raise BusyError("sessions_full")
            lane = self.lanes[key] = Lane(asyncio.Queue(self.queue_size))
        if len(lane.outstanding) >= self.queue_size:
            raise BusyError("session_full")
        request = Request(event, send, asyncio.get_running_loop().create_future(), control)
        lane.queue.put_nowait(request)
        lane.outstanding.append(request)
        if lane.task is None:
            lane.task = asyncio.create_task(self._run(key, lane))
        return await asyncio.shield(request.done)

    @staticmethod
    def mergeable(first: Incoming, second: Incoming) -> bool:
        return (
            not first.text.startswith("/")
            and not second.text.startswith("/")
            and not first.has_attachment
            and not second.has_attachment
            and len(first.text) <= 1000
            and len(second.text) <= 1000
            and first.quote_id == second.quote_id
            and first.mentions == second.mentions
        )

    async def _run(self, key: str, lane: Lane) -> None:
        carry = None
        try:
            if lane.previous:
                await asyncio.gather(lane.previous, return_exceptions=True)
            while carry is not None or not lane.queue.empty():
                first = carry or lane.queue.get_nowait()
                carry = None
                batch = [first]
                deadline = asyncio.get_running_loop().time() + self.window
                if not first.control and self.gap > 0 and self.mergeable(first.event, first.event):
                    while len(batch) < self.queue_size:
                        remaining = min(self.gap, deadline - asyncio.get_running_loop().time())
                        if remaining <= 0:
                            break
                        try:
                            nxt = await asyncio.wait_for(lane.queue.get(), remaining)
                        except TimeoutError:
                            break
                        total = sum(len(r.event.text) + 1 for r in batch) + len(nxt.event.text)
                        if (
                            nxt.control
                            or not self.mergeable(first.event, nxt.event)
                            or total > 4000
                        ):
                            carry = nxt
                            break
                        batch.append(nxt)
                event = batch[-1].event.model_copy(
                    update={
                        "text": "\n".join(r.event.text for r in batch),
                        "addressed": any(r.event.addressed for r in batch),
                        "parts": tuple(
                            InputPart(message_id=r.event.message_id, text=r.event.text)
                            for r in batch
                        ),
                    }
                )
                try:
                    async with asyncio.timeout(90):
                        delivered = await (first.control or self.process)(event, batch[-1].send)
                except Exception as error:
                    for request in batch:
                        if not request.done.done():
                            request.done.set_exception(error)
                else:
                    for request in batch:
                        if not request.done.done():
                            request.done.set_result(delivered)
                for request in batch:
                    lane.outstanding.remove(request)
        finally:
            for request in lane.outstanding:
                if not request.done.done():
                    request.done.set_result(False)
            if self.lanes.get(key) is lane:
                self.lanes.pop(key)

    async def cancel(self, key: str) -> None:
        lane = self.lanes.get(key)
        if lane and lane.task:
            lane.task.cancel()
            await asyncio.gather(lane.task, return_exceptions=True)

    async def close(self) -> None:
        self.closed = True
        tasks = [lane.task for lane in self.lanes.values() if lane.task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
