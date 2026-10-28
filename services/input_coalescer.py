"""Bounded short-input bursts admitted in the session scheduler's FIFO order.

Only the latest still-pending batch can accept more input. Waiting happens under
the session lock, before taking a model concurrency slot. Quote/recipient changes
seal the preceding batch; controls discard it through the scheduler's interrupt.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace

from services.message_contract import MessageEvent


@dataclass(eq=False)
class InputBatch:
    events: list[MessageEvent]
    wait_seconds: float
    opened_at: float
    last_at: float
    deadline: float
    sealed: bool = False
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    waited_seconds: float = 0.0

    @property
    def ready_at(self) -> float:
        return min(self.deadline, self.last_at + self.wait_seconds)

    def seal(self) -> None:
        self.sealed = True
        self.changed.set()

    async def wait(self) -> None:
        started = asyncio.get_running_loop().time()
        try:
            while not self.sealed:
                delay = self.ready_at - asyncio.get_running_loop().time()
                if delay <= 0:
                    break
                self.changed.clear()
                try:
                    await asyncio.wait_for(self.changed.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.waited_seconds = asyncio.get_running_loop().time() - started
            self.seal()

    def combined_event(self) -> MessageEvent:
        if len(self.events) == 1:
            return self.events[0]
        # Newline separators preserve the user's code and message order. The
        # envelope quotes the latest original message; structured parts retain
        # every original ID, timestamp and quote for the prompt and diagnostics.
        mentions = {mention.raw_id: mention for event in self.events for mention in event.mentions}
        return replace(
            self.events[-1],
            text="\n".join(event.text for event in self.events),
            mentions_bot=any(event.mentions_bot for event in self.events),
            mentions=tuple(mentions.values()),
            input_parts=tuple(part for event in self.events for part in event.original_input_parts()),
        )


class InputCoalescer:
    def __init__(
        self, *, wait_seconds: float = 0.35, max_wait_seconds: float = 1.5,
        max_messages: int = 8, max_chars: int = 4000, max_message_chars: int = 1000,
    ) -> None:
        # Invalid/huge configuration must not create an unbounded wait. The
        # configured maximum remains authoritative even when below the quiet gap.
        self.wait_seconds = min(5.0, max(0.0, float(wait_seconds)))
        self.max_wait_seconds = min(5.0, max(0.0, float(max_wait_seconds)))
        self.max_messages = max(1, max_messages)
        self.max_chars = max(1, max_chars)
        self.max_message_chars = max(1, max_message_chars)
        self._pending: dict[object, InputBatch] = {}

    @staticmethod
    def same_branch(first: MessageEvent, second: MessageEvent) -> bool:
        def target(event: MessageEvent):
            quote = event.quote
            return (
                quote.message_id if quote else "",
                tuple(sorted(mention.raw_id for mention in event.mentions
                             if not mention.is_bot and mention.raw_id != "all")),
            )
        return first.session_key == second.session_key and target(first) == target(second)

    def pending_event(self, event: MessageEvent) -> MessageEvent | None:
        batch = self._pending.get(event.session_key)
        if batch is not None and self.same_branch(batch.events[-1], event):
            return batch.combined_event()
        return None

    def admit(self, event: MessageEvent, *, mergeable: bool = True) -> tuple[InputBatch, bool]:
        now = asyncio.get_running_loop().time()
        eligible = (mergeable and not event.attachments and bool(event.normalized_text)
                    and len(event.text) <= self.max_message_chars
                    and self.wait_seconds > 0 and self.max_wait_seconds > 0)
        previous = self._pending.get(event.session_key)
        if previous is not None:
            if (eligible and not previous.sealed and now < previous.ready_at
                    and self.same_branch(previous.events[-1], event)
                    and len(previous.events) < self.max_messages
                    and sum(len(old.text) for old in previous.events) + len(event.text) + len(previous.events) <= self.max_chars):
                previous.events.append(event)
                previous.last_at = now
                previous.changed.set()
                if len(previous.events) >= self.max_messages:
                    previous.seal()
                return previous, False
            previous.seal()
        batch = InputBatch(
            events=[event], wait_seconds=self.wait_seconds,
            opened_at=now, last_at=now, deadline=now + self.max_wait_seconds,
            sealed=not eligible,
        )
        self._pending[event.session_key] = batch
        return batch, True

    def retire(self, batch: InputBatch) -> None:
        key = batch.events[0].session_key
        if self._pending.get(key) is batch:
            self._pending.pop(key, None)

    def discard(self, session_key: object) -> None:
        batch = self._pending.pop(session_key, None)
        if batch is not None:
            batch.seal()

    def clear(self) -> None:
        for batch in self._pending.values():
            batch.seal()
        self._pending.clear()
