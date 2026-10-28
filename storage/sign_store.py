from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from sqlite3 import IntegrityError

from storage.db import get_connection


@dataclass(frozen=True)
class SignResult:
    created: bool
    total_days: int


@dataclass(frozen=True)
class SignSummary:
    total_days: int
    last_sign_date: str


def sign_today(user_openid: str) -> SignResult:
    today = date.today().isoformat()
    created = True

    with get_connection("doraymon") as connection:
        try:
            connection.execute(
                "INSERT INTO sign_records (user_openid, sign_date) VALUES (?, ?)",
                (user_openid, today),
            )
            connection.commit()
        except IntegrityError:
            created = False

        total_days = connection.execute(
            "SELECT COUNT(*) FROM sign_records WHERE user_openid = ?",
            (user_openid,),
        ).fetchone()[0]

    return SignResult(created=created, total_days=int(total_days))


def get_sign_summary(user_openid: str) -> SignSummary:
    with get_connection("doraymon") as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS total_days, MAX(sign_date) AS last_sign_date
            FROM sign_records
            WHERE user_openid = ?
            """,
            (user_openid,),
        ).fetchone()

    return SignSummary(total_days=int(row["total_days"] or 0), last_sign_date=row["last_sign_date"] or "")
