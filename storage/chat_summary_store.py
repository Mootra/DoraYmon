from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from storage import chat_history_store


@dataclass(frozen=True)
class ChatSummary:
    scope_type: str
    scope_openid: str
    user_openid: str
    covered_through_id: int
    summary: str
    created_at: str
    expires_at: str


def init_chat_summary_table() -> None:
    with closing(chat_history_store.get_connection("doraymon")) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_summaries (
                scope_type TEXT NOT NULL,
                scope_openid TEXT NOT NULL,
                user_openid TEXT NOT NULL,
                covered_through_id INTEGER NOT NULL,
                summary TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                expires_at TEXT NOT NULL,
                PRIMARY KEY (scope_type, scope_openid, user_openid)
            )
            """
        )
        connection.commit()


def get_summary(
    scope_type: str,
    scope_openid: str,
    user_openid: str,
) -> ChatSummary | None:
    session = _normalize_session(scope_type, scope_openid, user_openid)
    with closing(chat_history_store.get_connection("doraymon")) as connection:
        row = connection.execute(
            """
            SELECT scope_type, scope_openid, user_openid,
                   covered_through_id, summary, created_at, expires_at
            FROM chat_summaries
            WHERE scope_type = ? AND scope_openid = ? AND user_openid = ?
              AND expires_at > datetime('now')
            """,
            session,
        ).fetchone()
        if row is not None:
            return _row_to_summary(row)

        connection.execute(
            """
            DELETE FROM chat_summaries
            WHERE scope_type = ? AND scope_openid = ? AND user_openid = ?
              AND expires_at <= datetime('now')
            """,
            session,
        )
        connection.commit()
    return None


def save_summary_if_current(
    scope_type: str,
    scope_openid: str,
    user_openid: str,
    expected_last_message_id: int,
    summary: str,
    retention_days: int = 7,
) -> bool:
    init_chat_summary_table()
    session = _normalize_session(scope_type, scope_openid, user_openid)
    normalized_summary = str(summary or "").strip()
    if not normalized_summary:
        return False

    now = datetime.now(timezone.utc).replace(microsecond=0)
    expires_at = now + timedelta(days=max(1, int(retention_days)))
    with closing(chat_history_store.get_connection("doraymon")) as connection:
        # Hold the writer lock across validation and write so a concurrent clear
        # or new turn cannot invalidate the check before the summary is stored.
        connection.execute("BEGIN IMMEDIATE")
        current_row = connection.execute(
            """
            SELECT COALESCE(MAX(id), 0) AS last_message_id
            FROM chat_messages
            WHERE scope_type = ? AND scope_openid = ? AND user_openid = ?
            """,
            session,
        ).fetchone()
        current_last_message_id = int(current_row["last_message_id"] or 0)
        if current_last_message_id <= 0 or current_last_message_id != int(expected_last_message_id):
            return False

        connection.execute(
            """
            INSERT INTO chat_summaries (
                scope_type, scope_openid, user_openid,
                covered_through_id, summary, created_at, expires_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope_type, scope_openid, user_openid) DO UPDATE SET
                covered_through_id = excluded.covered_through_id,
                summary = excluded.summary,
                created_at = excluded.created_at,
                expires_at = excluded.expires_at
            """,
            (*session, current_last_message_id, normalized_summary,
             now.strftime("%Y-%m-%d %H:%M:%S"),
             expires_at.strftime("%Y-%m-%d %H:%M:%S")),
        )
        connection.commit()
    return True


def clear_summary(scope_type: str, scope_openid: str, user_openid: str) -> int:
    init_chat_summary_table()
    session = _normalize_session(scope_type, scope_openid, user_openid)
    with closing(chat_history_store.get_connection("doraymon")) as connection:
        cursor = connection.execute(
            """
            DELETE FROM chat_summaries
            WHERE scope_type = ? AND scope_openid = ? AND user_openid = ?
            """,
            session,
        )
        connection.commit()
        return int(cursor.rowcount)


def _normalize_session(
    scope_type: str,
    scope_openid: str,
    user_openid: str,
) -> tuple[str, str, str]:
    normalized_scope = str(scope_type or "").strip()
    if normalized_scope not in {"private", "group"}:
        raise ValueError("scope_type must be private or group")
    normalized_user = str(user_openid or "").strip()
    if not normalized_user:
        raise ValueError("user_openid must not be empty")
    normalized_scope_openid = str(scope_openid or "").strip()
    if normalized_scope == "private" and not normalized_scope_openid:
        normalized_scope_openid = normalized_user
    if not normalized_scope_openid:
        raise ValueError("scope_openid must not be empty")
    return normalized_scope, normalized_scope_openid, normalized_user


def _row_to_summary(row) -> ChatSummary:
    return ChatSummary(
        scope_type=str(row["scope_type"]),
        scope_openid=str(row["scope_openid"]),
        user_openid=str(row["user_openid"]),
        covered_through_id=int(row["covered_through_id"]),
        summary=str(row["summary"]),
        created_at=str(row["created_at"]),
        expires_at=str(row["expires_at"]),
    )
