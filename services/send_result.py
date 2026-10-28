"""Delivery outcome contract for outbound QQ messages.

A reply is only "sent" when the platform API call actually returned. The bot
cannot revoke a message that was already accepted, so the honest contract is to
record what happened per segment instead of assuming success:

- ``SendResult`` describes one delivery attempt, including a short error label
  that is safe to show a user (no stack, no request headers, no credentials).
- ``SendLog`` keeps the most recent results per session so an operator can ask
  why the bot looked silent, without storing message bodies.

The log is bounded and in memory; it is diagnostic state, not chat history.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime

from doraymon.identity import SessionKey

MAX_RESULTS_PER_SESSION = 20

ERROR_TIMEOUT = "请求超时"
ERROR_CONNECTION = "网络连接失败"
ERROR_HTTP_FORMAT = "接口返回 {status}"
ERROR_UNKNOWN = "未分类的发送错误"
DELIVERED = "delivered"
REJECTED = "rejected"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class SendResult:
    scope_type: str
    session_key: SessionKey | None
    segment_index: int
    success: bool
    recorded_at: datetime
    error_label: str = ""
    outcome: str = ""
    event_id: str = ""

    @property
    def delivery_status(self) -> str:
        return self.outcome or (DELIVERED if self.success else REJECTED)

    @property
    def scope_label(self) -> str:
        return "群聊" if self.scope_type == "group" else "私聊"

    def describe(self) -> str:
        if self.success:
            return f"第 {self.segment_index} 段已提交给 QQ"
        if self.delivery_status == UNKNOWN:
            return f"第 {self.segment_index} 段投递结果未知（{self.error_label}）"
        return f"第 {self.segment_index} 段发送失败（{self.error_label}）"


def classify_send_error(error: BaseException) -> str:
    """Return a short, safe label for one delivery failure."""
    status = getattr(error, "status", None) or getattr(error, "status_code", None)
    if isinstance(status, int) and status > 0:
        return ERROR_HTTP_FORMAT.format(status=status)
    if isinstance(error, (TimeoutError,)):
        return ERROR_TIMEOUT
    if isinstance(error, (ConnectionError, OSError)):
        return ERROR_CONNECTION
    if error.__class__.__name__ in {"TimeoutError", "asyncio.TimeoutError", "CancelledError"}:
        return ERROR_TIMEOUT
    return ERROR_UNKNOWN


class SendLog:
    """Bounded per-session record of recent delivery outcomes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._results: dict[SessionKey, deque[SendResult]] = {}

    def record(self, result: SendResult) -> None:
        if result.session_key is None:
            return
        with self._lock:
            bucket = self._results.get(result.session_key)
            if bucket is None:
                bucket = deque(maxlen=MAX_RESULTS_PER_SESSION)
                self._results[result.session_key] = bucket
            bucket.append(result)

    def results_for(self, session: SessionKey) -> tuple[SendResult, ...]:
        with self._lock:
            return tuple(self._results.get(session, ()))

    def last_failure(self, session: SessionKey) -> SendResult | None:
        with self._lock:
            for result in reversed(self._results.get(session, ())):
                if not result.success:
                    return result
        return None

    def counts_for(self, session: SessionKey) -> tuple[int, int]:
        """Return ``(delivered_segments, failed_segments)`` for one session."""
        with self._lock:
            bucket = tuple(self._results.get(session, ()))
        delivered = sum(1 for result in bucket if result.success)
        return delivered, sum(1 for result in bucket if not result.success)

    def forget(self, session: SessionKey) -> None:
        with self._lock:
            self._results.pop(session, None)

    def clear(self) -> None:
        with self._lock:
            self._results.clear()


# Process-wide log shared by the QQ client and diagnostic commands.
SEND_LOG = SendLog()
