from __future__ import annotations

from contextlib import closing
import logging
from sqlite3 import IntegrityError, OperationalError

from storage.db import get_connection

logger = logging.getLogger(__name__)


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
        except OperationalError:
            # A missing table is a storage problem, not a reason to break the
            # conversation. Nothing is stored and the caller is told plainly.
            logger.exception("保存口味失败：数据库结构不可用")
            return False


def list_food_preferences(user_openid: str) -> list[str]:
    """Return stored preferences; an unavailable table means none are stored.

    Reading must not raise: this is called on every food request, so a storage
    problem would otherwise turn a working command into an error reply.
    """
    try:
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
    except OperationalError:
        logger.warning("读取口味失败：数据库结构不可用，按没有保存口味处理")
        return []

    return [str(row["preference_text"]) for row in rows]


def remove_food_preference(user_openid: str, preference_text: str) -> bool:
    normalized = preference_text.strip()
    if not normalized:
        return False

    with closing(get_connection("doraymon")) as connection:
        try:
            cursor = connection.execute(
                """
                DELETE FROM food_preferences
                WHERE user_openid = ? AND preference_text = ?
                """,
                (user_openid, normalized),
            )
            connection.commit()
            return cursor.rowcount > 0
        except OperationalError:
            logger.exception("删除口味失败：数据库结构不可用")
            return False
