from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass

from storage.db import get_connection


DEFAULT_MAX_CONTENT_LENGTH = 1000
VALID_SCOPE_TYPES = {"private", "group"}
VALID_ROLES = {"user", "assistant"}
SENSITIVE_MARKERS = (
    "QQBOT_APPID=",
    "QQBOT_SECRET=",
    "DEEPSEEK_API_KEY=",
    "Authorization: Bearer ",
    "api_key:",
    "secret:",
)


@dataclass(frozen=True)
class ChatMessage:
    id: int
    scope_type: str
    scope_openid: str
    user_openid: str
    role: str
    content: str
    created_at: str


def init_chat_history_table() -> None:
    with closing(get_connection("doraymon")) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_type TEXT NOT NULL,
                scope_openid TEXT NOT NULL,
                user_openid TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_chat_messages_session
            ON chat_messages (scope_type, scope_openid, user_openid, id)
            """
        )
        connection.commit()


def save_message(
    scope_type: str,
    scope_openid: str,
    user_openid: str,
    role: str,
    content: str,
    max_content_length: int = DEFAULT_MAX_CONTENT_LENGTH,
) -> ChatMessage:
    normalized_scope_type = _normalize_scope_type(scope_type)
    normalized_user_openid = _require_text(user_openid, "user_openid")
    normalized_scope_openid = _normalize_scope_openid(
        normalized_scope_type,
        scope_openid,
        normalized_user_openid,
    )
    normalized_role = _normalize_role(role)
    normalized_content = _normalize_content(content, max_content_length)

    with closing(get_connection("doraymon")) as connection:
        cursor = connection.execute(
            """
            INSERT INTO chat_messages (
                scope_type,
                scope_openid,
                user_openid,
                role,
                content
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                normalized_scope_type,
                normalized_scope_openid,
                normalized_user_openid,
                normalized_role,
                normalized_content,
            ),
        )
        connection.commit()
        row = connection.execute(
            """
            SELECT id, scope_type, scope_openid, user_openid, role, content, created_at
            FROM chat_messages
            WHERE id = ?
            """,
            (cursor.lastrowid,),
        ).fetchone()

    return _row_to_message(row)


def list_recent_messages(
    scope_type: str,
    scope_openid: str,
    user_openid: str,
    limit: int,
) -> list[ChatMessage]:
    if limit <= 0:
        return []

    normalized_scope_type = _normalize_scope_type(scope_type)
    normalized_user_openid = _require_text(user_openid, "user_openid")
    normalized_scope_openid = _normalize_scope_openid(
        normalized_scope_type,
        scope_openid,
        normalized_user_openid,
    )

    with closing(get_connection("doraymon")) as connection:
        rows = connection.execute(
            """
            SELECT id, scope_type, scope_openid, user_openid, role, content, created_at
            FROM chat_messages
            WHERE scope_type = ?
              AND scope_openid = ?
              AND user_openid = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (
                normalized_scope_type,
                normalized_scope_openid,
                normalized_user_openid,
                int(limit),
            ),
        ).fetchall()

    return [_row_to_message(row) for row in reversed(rows)]


def clear_messages(scope_type: str, scope_openid: str, user_openid: str) -> int:
    normalized_scope_type = _normalize_scope_type(scope_type)
    normalized_user_openid = _require_text(user_openid, "user_openid")
    normalized_scope_openid = _normalize_scope_openid(
        normalized_scope_type,
        scope_openid,
        normalized_user_openid,
    )

    with closing(get_connection("doraymon")) as connection:
        cursor = connection.execute(
            """
            DELETE FROM chat_messages
            WHERE scope_type = ?
              AND scope_openid = ?
              AND user_openid = ?
            """,
            (
                normalized_scope_type,
                normalized_scope_openid,
                normalized_user_openid,
            ),
        )
        connection.commit()
        return int(cursor.rowcount)


def count_messages(scope_type: str, scope_openid: str, user_openid: str) -> int:
    normalized_scope_type = _normalize_scope_type(scope_type)
    normalized_user_openid = _require_text(user_openid, "user_openid")
    normalized_scope_openid = _normalize_scope_openid(
        normalized_scope_type,
        scope_openid,
        normalized_user_openid,
    )

    with closing(get_connection("doraymon")) as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS message_count
            FROM chat_messages
            WHERE scope_type = ?
              AND scope_openid = ?
              AND user_openid = ?
            """,
            (
                normalized_scope_type,
                normalized_scope_openid,
                normalized_user_openid,
            ),
        ).fetchone()

    return int(row["message_count"] or 0)


def _normalize_scope_type(scope_type: str) -> str:
    normalized = _require_text(scope_type, "scope_type")
    if normalized not in VALID_SCOPE_TYPES:
        raise ValueError("scope_type must be private or group")
    return normalized


def _normalize_role(role: str) -> str:
    normalized = _require_text(role, "role")
    if normalized not in VALID_ROLES:
        raise ValueError("role must be user or assistant")
    return normalized


def _normalize_scope_openid(
    scope_type: str,
    scope_openid: str,
    user_openid: str,
) -> str:
    if scope_type == "private" and not str(scope_openid or "").strip():
        return user_openid
    return _require_text(scope_openid, "scope_openid")


def _normalize_content(content: str, max_content_length: int) -> str:
    normalized = str(content or "").strip()
    if not normalized:
        raise ValueError("content must not be empty")
    if _contains_sensitive_marker(normalized):
        raise ValueError("content contains sensitive configuration markers")
    if max_content_length <= 0:
        raise ValueError("max_content_length must be positive")
    return normalized[:max_content_length]


def _contains_sensitive_marker(content: str) -> bool:
    lower_content = content.lower()
    if ".env" in lower_content or "config.yaml" in lower_content:
        return True
    return any(marker.lower() in lower_content for marker in SENSITIVE_MARKERS)


def _require_text(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _row_to_message(row) -> ChatMessage:
    return ChatMessage(
        id=int(row["id"]),
        scope_type=str(row["scope_type"]),
        scope_openid=str(row["scope_openid"]),
        user_openid=str(row["user_openid"]),
        role=str(row["role"]),
        content=str(row["content"]),
        created_at=str(row["created_at"]),
    )
