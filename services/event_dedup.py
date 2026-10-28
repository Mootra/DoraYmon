"""Bounded memory of recently handled inbound event ids.

Every entry point can be handed the same message twice: OneBot redelivers after
a reconnect, and Feishu redelivers over its long connection when a handler does
not finish in time. Answering a replay twice looks like the bot talking to
itself, so the window lives here instead of inside one protocol's adapter.

The window is bounded, which is sufficient for a first version and is stated as
such rather than implied to be exact.
"""

from __future__ import annotations

from collections import deque

DEFAULT_MAX_SEEN_EVENTS = 512


class DuplicateEventTracker:
    """Bounded memory of recently handled event ids."""

    def __init__(self, max_events: int = DEFAULT_MAX_SEEN_EVENTS) -> None:
        self.max_events = max(1, int(max_events))
        self._seen: set[str] = set()
        self._order: deque[str] = deque()

    def seen_before(self, event_id: str) -> bool:
        normalized = str(event_id or "").strip()
        if not normalized:
            return False
        return normalized in self._seen

    def remember(self, event_id: str) -> bool:
        """Record an event id. Returns False when it was already known."""
        normalized = str(event_id or "").strip()
        if not normalized:
            return True
        if normalized in self._seen:
            return False
        self._seen.add(normalized)
        self._order.append(normalized)
        while len(self._order) > self.max_events:
            self._seen.discard(self._order.popleft())
        return True

    def admit(self, event_id: str) -> bool:
        """True when this event should be handled (first sighting only)."""
        return self.remember(event_id)

    def __len__(self) -> int:
        return len(self._seen)

    def clear(self) -> None:
        self._seen.clear()
        self._order.clear()
