from __future__ import annotations

from contextlib import closing
from sqlite3 import IntegrityError

from storage.db import get_connection


def add_food_preference(user_openid: str, preference_text: str) -> bool:
    normalized = preference_text.strip()
    if not normalized:
        return False

    with closing(get_connection("doraymon")) as connection:
        try:
            connection.execute(
                """
                INSERT INTO food_preferences (user_openid, preference_text)
                VALUES (?, ?)
                """,
                (user_openid, normalized),
            )
            connection.commit()
            return True
        except IntegrityError:
            return False


def list_food_preferences(user_openid: str) -> list[str]:
    with closing(get_connection("doraymon")) as connection:
        rows = connection.execute(
            """
            SELECT preference_text
            FROM food_preferences
            WHERE user_openid = ?
            ORDER BY id ASC
            """,
            (user_openid,),
        ).fetchall()

    return [str(row["preference_text"]) for row in rows]


def remove_food_preference(user_openid: str, preference_text: str) -> bool:
    normalized = preference_text.strip()
    if not normalized:
        return False

    with closing(get_connection("doraymon")) as connection:
        cursor = connection.execute(
            """
            DELETE FROM food_preferences
            WHERE user_openid = ? AND preference_text = ?
            """,
            (user_openid, normalized),
        )
        connection.commit()
        return cursor.rowcount > 0
