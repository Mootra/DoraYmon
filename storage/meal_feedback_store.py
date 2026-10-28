"""Store explicit dining feedback: what the user said, not what we inferred.

The plan separates four facts that are easy to confuse: a recommendation, a
rejection, a save, and actually eating somewhere. ``RecommendationFeedback``
already models that distinction, but nothing in the application ever produced one,
so the bot could not record the only kind of feedback that matters most: the user
telling it what happened.

Rules this module keeps, both taken from the plan:

- **Recommending is not eating.** Nothing is recorded as eaten unless the user says
  so. A recommendation that goes unanswered leaves no trace here, because there is
  nothing to record.
- **Rejecting is not disliking.** "不想去这家" records a rejection. It excludes the
  place from the current decision and is remembered as a rejection, never as a
  taste preference. Turning one refusal into a permanent dislike would be inventing
  a fact about the user.

Storage follows the existing convention: a failed write returns False and a failed
read returns empty rather than raising, because a storage problem must not turn a
working conversation into an error reply.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import logging
from sqlite3 import OperationalError

from services.recommendation_types import FeedbackKind, RecommendationFeedback
from storage.db import get_connection

logger = logging.getLogger(__name__)

TABLE = "meal_feedback"
MAX_ITEMS_PER_READ = 200


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def record_feedback(feedback: RecommendationFeedback, *, session_key: str = "") -> bool:
    """Persist one explicit statement. Returns False when it could not be stored."""
    if feedback is None or not str(feedback.shop_id or "").strip():
        return False
    if not isinstance(feedback.kind, FeedbackKind):
        return False
    # Opening the connection is inside the guard as well: an unusable database
    # raises there too, and a storage problem must not break the conversation.
    try:
        with closing(get_connection("doraymon")) as connection:
            connection.execute(
                f"""
                INSERT INTO {TABLE}
                    (session_key, shop_id, item_id, kind, reason, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(session_key or ""),
                    str(feedback.shop_id).strip(),
                    str(feedback.item_id or "").strip(),
                    feedback.kind.value,
                    str(feedback.reason or "").strip(),
                    (feedback.recorded_at or _now()).strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            connection.commit()
            return True
    except OperationalError:
        logger.exception("保存用餐反馈失败：数据库结构不可用")
        return False


def _rows(session_key: str, kind: FeedbackKind, limit: int) -> list[tuple]:
    """Read stored rows, treating an unavailable table as "nothing stored"."""
    try:
        with closing(get_connection("doraymon")) as connection:
            return list(
                connection.execute(
                    f"""
                    SELECT shop_id, item_id, reason, recorded_at
                    FROM {TABLE}
                    WHERE session_key = ? AND kind = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (str(session_key or ""), kind.value, max(1, int(limit))),
                ).fetchall()
            )
    except OperationalError:
        return []


def eaten_shop_ids(session_key: str, *, limit: int = MAX_ITEMS_PER_READ) -> tuple[str, ...]:
    """Places the user said they actually ate at."""
    return tuple(
        str(row[0]) for row in _rows(session_key, FeedbackKind.EATEN, limit) if row[0]
    )


def rejected_shop_ids(session_key: str, *, limit: int = MAX_ITEMS_PER_READ) -> tuple[str, ...]:
    """Places the user said they did not want."""
    return tuple(
        str(row[0]) for row in _rows(session_key, FeedbackKind.REJECTED, limit) if row[0]
    )


def forget_session(session_key: str) -> int:
    """Remove this session's feedback, returning how many rows went.

    Used by the clear command: if a user clears the conversation, the bot should not
    keep acting on statements from it.
    """
    try:
        with closing(get_connection("doraymon")) as connection:
            cursor = connection.execute(
                f"DELETE FROM {TABLE} WHERE session_key = ?", (str(session_key or ""),)
            )
            connection.commit()
            return int(cursor.rowcount or 0)
    except OperationalError:
        logger.exception("清除用餐反馈失败：数据库结构不可用")
        return 0


def summarise(session_key: str) -> str:
    """A short line for /status style reporting, without quoting user text."""
    eaten = eaten_shop_ids(session_key)
    rejected = rejected_shop_ids(session_key)
    if not eaten and not rejected:
        return "本次会话没有记录用餐反馈"
    return f"本次会话记录：吃过 {len(eaten)} 家，不想去 {len(rejected)} 家"
