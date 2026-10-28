import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import aiosqlite

from .models import Passage, Turn

BEIJING = timezone(timedelta(hours=8))  # sign-in days follow Beijing time


@dataclass(frozen=True)
class SignResult:
    created: bool
    total_days: int


@dataclass(frozen=True)
class SignSummary:
    total_days: int
    last_sign_date: str


class Store:
    def __init__(
        self,
        path: Path,
        *,
        max_turns: int = 12,
        ttl: int = 43200,
        clock: Callable[[], datetime] | None = None,
    ):
        self.path, self.max_turns, self.ttl = path, max_turns, ttl
        self.lock = asyncio.Lock()
        self.clock = clock or (lambda: datetime.now(UTC))

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path)
        try:
            await self.db.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA busy_timeout=5000;
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY, session TEXT NOT NULL, event TEXT NOT NULL,
                    question TEXT NOT NULL, answer TEXT NOT NULL, created REAL NOT NULL,
                    UNIQUE(session, event)
                );
                CREATE INDEX IF NOT EXISTS turns_session ON turns(session, id);
                CREATE VIRTUAL TABLE IF NOT EXISTS knowledge USING fts5(
                    scope UNINDEXED, source UNINDEXED, body, tokenize='trigram'
                );
                CREATE TABLE IF NOT EXISTS food_preferences (
                    session TEXT NOT NULL, preference TEXT NOT NULL, id INTEGER NOT NULL,
                    PRIMARY KEY(session, preference)
                );
                CREATE TABLE IF NOT EXISTS sign_records (
                    session TEXT NOT NULL, sign_date TEXT NOT NULL,
                    PRIMARY KEY(session, sign_date)
                );
            """)
        except BaseException:
            await self.db.close()
            raise

    async def close(self) -> None:
        await self.db.close()

    async def history(self, session: str) -> list[Turn]:
        async with self.lock:
            async with self.db.execute(
                "SELECT question, answer FROM turns WHERE session=? "
                "AND (SELECT MAX(created) FROM turns WHERE session=?)>? "
                "ORDER BY id DESC LIMIT ?",
                (session, session, time.time() - self.ttl, self.max_turns),
            ) as cursor:
                rows = await cursor.fetchall()
        return [Turn(question=q, answer=a) for q, a in reversed(rows)]

    async def append(self, session: str, event: str, question: str, answer: str) -> None:
        async with self.lock:
            try:
                await self.db.execute(
                    "DELETE FROM turns WHERE session IN "
                    "(SELECT session FROM turns GROUP BY session HAVING MAX(created)<?)",
                    (time.time() - self.ttl,),
                )
                await self.db.execute(
                    "INSERT INTO turns(session,event,question,answer,created) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(session,event) DO NOTHING",
                    (session, event, question, answer, time.time()),
                )
                await self.db.execute(
                    "DELETE FROM turns WHERE session=? AND id NOT IN "
                    "(SELECT id FROM turns WHERE session=? ORDER BY id DESC LIMIT ?)",
                    (session, session, self.max_turns),
                )
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise

    async def clear(self, session: str) -> None:
        async with self.lock:
            await self.db.execute("DELETE FROM turns WHERE session=?", (session,))
            await self.db.commit()

    async def food_preferences(self, session: str) -> list[str]:
        async with self.lock:
            async with self.db.execute(
                "SELECT preference FROM food_preferences WHERE session=? ORDER BY id",
                (session,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def add_food_preference(
        self, session: str, preference: str, *, limit: int
    ) -> Literal["added", "exists", "full"]:
        async with self.lock:
            try:
                await self.db.execute("BEGIN IMMEDIATE")
                async with self.db.execute(
                    "SELECT preference,id FROM food_preferences WHERE session=?", (session,)
                ) as cursor:
                    saved = await cursor.fetchall()
                if any(text == preference for text, _ in saved):
                    result = "exists"
                elif len(saved) >= limit:
                    result = "full"
                else:
                    sequence = max((number for _, number in saved), default=0) + 1
                    await self.db.execute(
                        "INSERT INTO food_preferences(session,preference,id) VALUES(?,?,?)",
                        (session, preference, sequence),
                    )
                    result = "added"
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise
        return result

    async def remove_food_preference(self, session: str, preference: str) -> bool:
        async with self.lock:
            try:
                cursor = await self.db.execute(
                    "DELETE FROM food_preferences WHERE session=? AND preference=?",
                    (session, preference),
                )
                removed = cursor.rowcount > 0
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise
        return removed

    async def sign_today(self, session: str) -> SignResult:
        async with self.lock:
            day = self.clock().astimezone(BEIJING).date().isoformat()
            try:
                cursor = await self.db.execute(
                    "INSERT INTO sign_records(session,sign_date) VALUES(?,?) "
                    "ON CONFLICT(session,sign_date) DO NOTHING",
                    (session, day),
                )
                created = cursor.rowcount > 0
                async with self.db.execute(
                    "SELECT COUNT(*) FROM sign_records WHERE session=?", (session,)
                ) as rows:
                    total = int((await rows.fetchone())[0])
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise
        return SignResult(created=created, total_days=total)

    async def sign_summary(self, session: str) -> SignSummary:
        async with self.lock:
            async with self.db.execute(
                "SELECT COUNT(*), MAX(sign_date) FROM sign_records WHERE session=?",
                (session,),
            ) as cursor:
                total, last = await cursor.fetchone()
        return SignSummary(total_days=total, last_sign_date=last or "")

    async def replace_knowledge(self, scope: str, passages: list[Passage]) -> None:
        async with self.lock:
            try:
                await self.db.execute("DELETE FROM knowledge WHERE scope=?", (scope,))
                await self.db.executemany(
                    "INSERT INTO knowledge(scope,source,body) VALUES(?,?,?)",
                    [(scope, p.source, p.body) for p in passages],
                )
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise

    async def search(self, query: str, scopes: tuple[str, str]) -> list[Passage]:
        # Quoted terms cannot become FTS operators. Permissions precede ranking and LIMIT.
        query = query.strip()[:200]
        if len(query) < 2:
            return []
        async with self.lock:
            if len(query) == 2:
                escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                sql = (
                    "SELECT source,body FROM knowledge WHERE scope IN (?,?) "
                    "AND body LIKE ? ESCAPE '\\' LIMIT 3"
                )
                params = (*scopes, f"%{escaped}%")
            else:
                terms = dict.fromkeys(query[i : i + 3] for i in range(min(len(query) - 2, 40)))
                match = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
                sql = (
                    "SELECT source,body FROM knowledge WHERE knowledge MATCH ? "
                    "AND scope IN (?,?) ORDER BY bm25(knowledge) LIMIT 3"
                )
                params = (match, *scopes)
            async with self.db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
        return [Passage(source=s, body=b) for s, b in rows]
