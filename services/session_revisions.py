"""Session revision fence against in-flight generation and concurrent clears.

A reply is generated from context that was read before the model call. While
that call is in flight the user may clear the conversation, switch topics, or
let the session expire. In that case the answer was produced for a conversation
that no longer exists and must not be written back into history.

The fence works with a monotonic revision per session:

1. ``begin`` when a generation starts, capturing the current revision.
2. ``is_current`` after the model returns and before history is written.
3. ``bump`` whenever the user invalidates the conversation (clear, topic
   switch, expiry), which retires every generation started earlier.

Revisions live in memory only, matching the first-version bounded cache in the
plan: after a restart there are no in-flight generations to guard, so a fresh
counter cannot resurrect a stale reply.

Revisions do not claim that an already-sent QQ message can be recalled. That is
a separate delivery concern; a send result should record what actually happened.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from doraymon.identity import SessionKey


class SessionRevisions:
    """Monotonic per-session revision counters, safe for concurrent threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._revisions: dict[SessionKey, int] = {}

    def current(self, session: SessionKey) -> int:
        with self._lock:
            return self._revisions.get(session, 0)

    def begin(self, session: SessionKey) -> int:
        """Capture the revision a generation is about to work against."""
        return self.current(session)

    def bump(self, session: SessionKey) -> int:
        """Invalidate every generation started before this call."""
        with self._lock:
            revision = self._revisions.get(session, 0) + 1
            self._revisions[session] = revision
            return revision

    def is_current(self, session: SessionKey, revision: int) -> bool:
        with self._lock:
            return self._revisions.get(session, 0) == int(revision)

    def forget(self, session: SessionKey) -> None:
        """Drop one session's counter after it is fully retired."""
        with self._lock:
            self._revisions.pop(session, None)

    def clear(self) -> None:
        with self._lock:
            self._revisions.clear()


# Process-wide fence shared by plugins, summary tasks and command handlers.
SESSION_REVISIONS = SessionRevisions()


def begin_generation(session: SessionKey) -> int:
    return SESSION_REVISIONS.begin(session)


def invalidate_session(session: SessionKey) -> int:
    return SESSION_REVISIONS.bump(session)


def is_generation_current(session: SessionKey, revision: int) -> bool:
    return SESSION_REVISIONS.is_current(session, revision)
