"""Passive reply window and quota for the official QQ Bot API.

The official platform does not allow a bot to speak freely. A reply must be tied
to a message the user just sent, inside a window, and only a few replies are
allowed per window:

- private (C2C): within 60 minutes of the triggering message, up to 4 messages;
- group: within 5 minutes, up to 5 messages.

Each segment of a long reply counts as one message, so a three-segment answer
consumes three of the four private replies. Exceeding the quota or answering
after the window is not "slow", it is refused by the platform, so the bot must
stop on its own rather than discover it through failures.

This module is pure bookkeeping: no I/O, no clock beyond an injectable one, so
every boundary is testable offline.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Checked against the official platform documentation rather than recalled:
#   private (C2C): "被动消息有效时间 60 分钟，每个消息最多回复 4 次"
#   group:         "被动消息（回复类）有效时间为 5 分钟，每个消息最多回复 5 次"
# https://bot.qq.com/wiki/develop/api-v2/server-inter/message/overview.html
# https://bot.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_messages.post.html
# The same doc states that resending an identical msg_id + msg_seq fails, while
# incrementing msg_seq is how one inbound message may be answered more than once,
# which is exactly why each segment is sent with its own sequence number.
PRIVATE_WINDOW_SECONDS = 60 * 60
PRIVATE_MAX_REPLIES = 4
GROUP_WINDOW_SECONDS = 5 * 60
GROUP_MAX_REPLIES = 5

SCOPE_PRIVATE = "private"
SCOPE_GROUP = "group"

REASON_ALLOWED = "allowed"
REASON_QUOTA_EXHAUSTED = "quota_exhausted"
REASON_WINDOW_EXPIRED = "window_expired"
REASON_UNKNOWN_SCOPE = "unknown_scope"

MAX_TRACKED_SESSIONS = 500


@dataclass(frozen=True)
class ReplyBudgetDecision:
    """Whether one more message may be sent for this inbound message."""

    allowed: bool
    reason: str
    remaining: int
    limit: int
    window_seconds: int
    detail: str = ""

    def describe(self) -> str:
        if self.allowed:
            return f"还可以回复 {self.remaining} 条（窗口 {self.window_seconds} 秒，上限 {self.limit} 条）"
        if self.reason == REASON_QUOTA_EXHAUSTED:
            return (
                f"本次消息的回复条数已用满（窗口 {self.window_seconds} 秒内最多 {self.limit} 条），"
                "需要用户再发一条消息"
            )
        if self.reason == REASON_WINDOW_EXPIRED:
            return f"已超过 {self.window_seconds} 秒的被动回复窗口，需要用户再发一条消息"
        return self.detail or "当前消息无法回复"


def quota_key(scope: str, message_id: str) -> str:
    """Composite key for one inbound message.

    OneBot and the official platform both report small integer message ids, so an
    id is only unique inside its own conversation. Without the scope, a group
    message and a private message with the same numeric id would share a quota.
    Empty identifiers are returned unchanged so the budget can ignore them.
    """
    normalized_id = str(message_id or "").strip()
    if not normalized_id:
        return ""
    return f"{str(scope or '').strip()}:{normalized_id}"


def limits_for(scope_type: str) -> tuple[int, int] | None:
    """Return ``(window_seconds, max_replies)`` for a scope, or None if unknown."""
    normalized = str(scope_type or "").strip().lower()
    if normalized == SCOPE_PRIVATE:
        return PRIVATE_WINDOW_SECONDS, PRIVATE_MAX_REPLIES
    if normalized == SCOPE_GROUP:
        return GROUP_WINDOW_SECONDS, GROUP_MAX_REPLIES
    return None


def time_left_in_window(
    window_start: datetime,
    window_seconds: int,
    now: datetime | None = None,
) -> float:
    """Seconds remaining in the window; negative once it has passed."""
    moment = now or datetime.now(timezone.utc)
    return (window_start + timedelta(seconds=int(window_seconds)) - moment).total_seconds()


class PassiveReplyBudget:
    """Tracks how many messages were spent answering each inbound message.

    The key is the triggering message id, so a fresh user message always starts a
    fresh window. Entries are bounded; the oldest is evicted first.
    """

    def __init__(self, max_sessions: int = MAX_TRACKED_SESSIONS) -> None:
        self.max_sessions = max(1, int(max_sessions))
        self._lock = threading.Lock()
        self._windows: dict[str, dict] = {}

    # -- inspection ---------------------------------------------------------

    def check(
        self,
        message_id: str,
        *,
        started_at: datetime,
        scope_type: str,
        sent_count: int = 0,
        now: datetime | None = None,
    ) -> ReplyBudgetDecision:
        """Whether one more message may be sent for this inbound message."""
        limits = limits_for(scope_type)
        if limits is None:
            return ReplyBudgetDecision(
                allowed=False,
                reason=REASON_UNKNOWN_SCOPE,
                remaining=0,
                limit=0,
                window_seconds=0,
                detail=f"无法识别会话类型：{scope_type}",
            )
        window_seconds, limit = limits
        if time_left_in_window(started_at, window_seconds, now) <= 0:
            return ReplyBudgetDecision(
                allowed=False,
                reason=REASON_WINDOW_EXPIRED,
                remaining=0,
                limit=limit,
                window_seconds=window_seconds,
            )
        spent = self._spent(message_id)
        remaining = max(0, limit - max(spent, int(sent_count)))
        if remaining <= 0:
            return ReplyBudgetDecision(
                allowed=False,
                reason=REASON_QUOTA_EXHAUSTED,
                remaining=0,
                limit=limit,
                window_seconds=window_seconds,
            )
        return ReplyBudgetDecision(
            allowed=True,
            reason=REASON_ALLOWED,
            remaining=remaining,
            limit=limit,
            window_seconds=window_seconds,
        )

    def remaining(
        self,
        message_id: str,
        *,
        started_at: datetime,
        scope_type: str,
        now: datetime | None = None,
    ) -> int:
        decision = self.check(
            message_id, started_at=started_at, scope_type=scope_type, now=now
        )
        return decision.remaining

    def spent(self, message_id: str) -> int:
        return self._spent(message_id)

    # -- accounting ---------------------------------------------------------

    def record(
        self,
        message_id: str,
        *,
        started_at: datetime,
        scope_type: str,
        now: datetime | None = None,
    ) -> None:
        """Account for one delivered message."""
        normalized = str(message_id or "").strip()
        if not normalized:
            return
        moment = now or datetime.now(timezone.utc)
        with self._lock:
            entry = self._windows.get(normalized)
            if entry is None:
                entry = {
                    "spent": 0,
                    "started_at": started_at,
                    "scope_type": str(scope_type or "").strip().lower(),
                    "last_used_at": moment,
                }
                self._windows[normalized] = entry
                self._evict_if_needed()
            entry["spent"] = int(entry["spent"]) + 1
            entry["last_used_at"] = moment

    def forget(self, message_id: str) -> None:
        with self._lock:
            self._windows.pop(str(message_id or "").strip(), None)

    def prune(self, *, now: datetime | None = None) -> int:
        """Drop windows that can no longer be used."""
        moment = now or datetime.now(timezone.utc)
        with self._lock:
            stale = []
            for message_id, entry in self._windows.items():
                limits = limits_for(entry["scope_type"])
                if limits is None:
                    stale.append(message_id)
                    continue
                window_seconds, _limit = limits
                if time_left_in_window(entry["started_at"], window_seconds, moment) <= 0:
                    stale.append(message_id)
            for message_id in stale:
                self._windows.pop(message_id, None)
            return len(stale)

    def __len__(self) -> int:
        with self._lock:
            return len(self._windows)

    def clear(self) -> None:
        with self._lock:
            self._windows.clear()

    # -- internals ----------------------------------------------------------

    def _spent(self, message_id: str) -> int:
        normalized = str(message_id or "").strip()
        if not normalized:
            return 0
        with self._lock:
            entry = self._windows.get(normalized)
            return int(entry["spent"]) if entry else 0

    def _evict_if_needed(self) -> None:
        while len(self._windows) > self.max_sessions:
            oldest = min(
                self._windows,
                key=lambda key: self._windows[key]["last_used_at"],
            )
            self._windows.pop(oldest, None)


# Process-wide budget shared by the official entry point.
REPLY_BUDGET = PassiveReplyBudget()
