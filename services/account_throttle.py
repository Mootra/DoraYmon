"""Outbound throttling for the QQ account entry.

An account entry has no platform quota, which cuts both ways: nothing stops the
bot from answering every message in a busy group, and a real account that does
that looks like spam to the platform's risk control. The group cooldown and rate
limit in the plan exist for exactly this.

Deliberately separate from ``reply_budget``: that one enforces a platform rule
the bot cannot negotiate, this one is a self-imposed courtesy limit that the
operator can tune or switch off. Private chat is throttled separately because a
one-to-one conversation is not spam in the same way a group is.

Pure bookkeeping with an injectable clock, so every boundary is testable offline.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

SCOPE_PRIVATE = "private"
SCOPE_GROUP = "group"

REASON_ALLOWED = "allowed"
REASON_COOLDOWN = "cooldown"
REASON_RATE_LIMITED = "rate_limited"

MAX_TRACKED_CONVERSATIONS = 500


@dataclass(frozen=True)
class ThrottleDecision:
    """Whether the account entry may speak in this conversation right now."""

    allowed: bool
    reason: str
    retry_after_seconds: float = 0.0
    detail: str = ""

    def describe(self) -> str:
        if self.allowed:
            return "允许发送"
        if self.reason == REASON_COOLDOWN:
            return f"距上次发言不足冷却时间，还需等待 {self.retry_after_seconds:.0f} 秒"
        if self.reason == REASON_RATE_LIMITED:
            return f"本窗口发言次数已达上限，还需等待 {self.retry_after_seconds:.0f} 秒"
        return self.detail or "当前不允许发送"


@dataclass(frozen=True)
class ThrottlePolicy:
    """Self-imposed limits for one conversation type.

    Both limits default to off: an operator must opt in, because the right value
    depends on the group. ``cooldown_seconds`` is the minimum gap between two
    replies; ``max_replies`` applies inside ``window_seconds``.
    """

    cooldown_seconds: float = 0.0
    window_seconds: float = 60.0
    max_replies: int = 0

    @property
    def is_enabled(self) -> bool:
        return self.cooldown_seconds > 0 or self.max_replies > 0

    def __post_init__(self) -> None:
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must not be negative")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if self.max_replies < 0:
            raise ValueError("max_replies must not be negative")


DISABLED = ThrottlePolicy()


class ConversationThrottle:
    """Per-conversation cooldown and sliding-window rate limit."""

    def __init__(self, *, max_conversations: int = MAX_TRACKED_CONVERSATIONS) -> None:
        self.max_conversations = max(1, int(max_conversations))
        self._lock = threading.Lock()
        self._history: dict[str, list[datetime]] = {}
        self._last_activity: dict[str, datetime] = {}

    def check(
        self,
        conversation_key: str,
        policy: ThrottlePolicy,
        *,
        now: datetime | None = None,
    ) -> ThrottleDecision:
        if not policy.is_enabled:
            return ThrottleDecision(allowed=True, reason=REASON_ALLOWED)
        key = self._normalize(conversation_key)
        if not key:
            return ThrottleDecision(allowed=True, reason=REASON_ALLOWED)

        moment = now or datetime.now(timezone.utc)
        sends = self._sends_within(key, policy.window_seconds, moment)

        if policy.max_replies > 0 and len(sends) >= policy.max_replies:
            oldest = min(sends)
            retry_after = (
                oldest + timedelta(seconds=policy.window_seconds) - moment
            ).total_seconds()
            return ThrottleDecision(
                allowed=False,
                reason=REASON_RATE_LIMITED,
                retry_after_seconds=max(0.0, retry_after),
            )

        if policy.cooldown_seconds > 0 and sends:
            latest = max(sends)
            elapsed = (moment - latest).total_seconds()
            if elapsed < policy.cooldown_seconds:
                return ThrottleDecision(
                    allowed=False,
                    reason=REASON_COOLDOWN,
                    retry_after_seconds=policy.cooldown_seconds - elapsed,
                )

        return ThrottleDecision(allowed=True, reason=REASON_ALLOWED)

    def record(
        self,
        conversation_key: str,
        *,
        now: datetime | None = None,
    ) -> None:
        key = self._normalize(conversation_key)
        if not key:
            return
        moment = now or datetime.now(timezone.utc)
        with self._lock:
            self._history.setdefault(key, []).append(moment)
            self._last_activity[key] = moment
            self._evict_if_needed()

    def prune(self, *, keep_seconds: float, now: datetime | None = None) -> int:
        """Drop conversations idle for longer than ``keep_seconds``."""
        moment = now or datetime.now(timezone.utc)
        cutoff = moment - timedelta(seconds=float(keep_seconds))
        with self._lock:
            stale = [key for key, seen in self._last_activity.items() if seen < cutoff]
            for key in stale:
                self._history.pop(key, None)
                self._last_activity.pop(key, None)
            return len(stale)

    def forget(self, conversation_key: str) -> None:
        key = self._normalize(conversation_key)
        with self._lock:
            self._history.pop(key, None)
            self._last_activity.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._history.clear()
            self._last_activity.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._last_activity)

    # -- internals ----------------------------------------------------------

    def _sends_within(
        self,
        key: str,
        window_seconds: float,
        now: datetime,
    ) -> list[datetime]:
        cutoff = now - timedelta(seconds=float(window_seconds))
        with self._lock:
            sends = [moment for moment in self._history.get(key, []) if moment > cutoff]
            if sends:
                self._history[key] = sends
            else:
                self._history.pop(key, None)
            return sends

    def _evict_if_needed(self) -> None:
        while len(self._last_activity) > self.max_conversations:
            oldest = min(self._last_activity, key=lambda key: self._last_activity[key])
            self._history.pop(oldest, None)
            self._last_activity.pop(oldest, None)

    @staticmethod
    def _normalize(conversation_key: str) -> str:
        return str(conversation_key or "").strip()


def policy_for_scope(
    *,
    scope_type: str,
    group_policy: ThrottlePolicy,
    private_policy: ThrottlePolicy = DISABLED,
) -> ThrottlePolicy:
    return group_policy if str(scope_type).strip().lower() == SCOPE_GROUP else private_policy


# Process-wide throttle shared by the account entry.
ACCOUNT_THROTTLE = ConversationThrottle()
