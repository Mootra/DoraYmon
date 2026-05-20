from __future__ import annotations

from storage.db import get_connection


def add_todo(user_openid: str, content: str) -> int:
    with get_connection("doraymon") as connection:
        cursor = connection.execute(
            "INSERT INTO todos (user_openid, content) VALUES (?, ?)",
            (user_openid, content),
        )
        connection.commit()
        return int(cursor.lastrowid)


def list_todos(user_openid: str) -> list[str]:
    with get_connection("doraymon") as connection:
        rows = connection.execute(
            """
            SELECT content
            FROM todos
            WHERE user_openid = ? AND is_done = 0
            ORDER BY id DESC
            LIMIT 20
            """,
            (user_openid,),
        ).fetchall()
    return [str(row["content"]) for row in rows]
