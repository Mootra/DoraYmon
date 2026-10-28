"""Versioned source records in SQLite; locations still come from the map lookup."""

import json
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import closing
from datetime import UTC
from hashlib import sha256
from pathlib import Path

from pydantic import ValidationError

from .evidence import clean_evidence, evidence_key
from .models import Evidence, Place

APPLICATION_ID = 0x444F5241
SCHEMA_VERSION = 1
SCHEMA = (
    "CREATE TABLE branches (amap_id TEXT PRIMARY KEY, poi_json TEXT)",
    """CREATE TABLE records (
        id INTEGER PRIMARY KEY,
        amap_id TEXT NOT NULL REFERENCES branches(amap_id),
        scope TEXT NOT NULL,
        source TEXT NOT NULL,
        source_key TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        payload TEXT NOT NULL,
        UNIQUE(amap_id, scope, source, source_key, content_hash)
    )""",
    "CREATE INDEX records_lookup ON records(amap_id, scope, source, source_key, observed_at DESC)",
)


class LibraryError(Exception):
    """A stable code, without filesystem paths or source contents."""


class MealLibrary:
    def __init__(self, path: Path, *, timeout: float = 2):
        self.path, self.timeout = path, timeout

    def _connect(self, *, readonly=False):
        database = self.path.resolve().as_uri() + "?mode=ro" if readonly else self.path
        connection = sqlite3.connect(
            database,
            timeout=self.timeout,
            uri=readonly,
            autocommit=sqlite3.LEGACY_TRANSACTION_CONTROL,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _check(connection) -> None:
        application = connection.execute("PRAGMA application_id").fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if (application, version) != (APPLICATION_ID, SCHEMA_VERSION):
            raise LibraryError("unsupported_library")

    def initialize(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                application = connection.execute("PRAGMA application_id").fetchone()[0]
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                tables = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
                if application == 0 and version == 0 and not tables:
                    for statement in SCHEMA:
                        connection.execute(statement)
                    connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                else:
                    self._check(connection)
            self.path.chmod(0o600)
        except (OSError, sqlite3.Error) as error:
            raise LibraryError("library_unavailable") from error

    def save(
        self,
        records: Mapping[str, Iterable[Evidence]],
        *,
        places: tuple[Place, ...] = (),
        scope: str = "",
    ) -> int:
        prepared = [
            (amap_id, clean_evidence(item)) for amap_id, items in records.items() for item in items
        ]
        self.initialize()
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                self._check(connection)
                for place in places:
                    poi = place.model_dump_json(exclude={"distance"})
                    connection.execute(
                        """INSERT INTO branches(amap_id, poi_json) VALUES(?, ?)
                        ON CONFLICT(amap_id) DO UPDATE SET poi_json=excluded.poi_json""",
                        (place.id, poi),
                    )
                inserted = 0
                for amap_id, item in prepared:
                    connection.execute(
                        "INSERT OR IGNORE INTO branches(amap_id) VALUES(?)", (amap_id,)
                    )
                    payload = item.model_dump_json(exclude={"needs_review"})
                    key = json.dumps(evidence_key(item)[1:], ensure_ascii=False)
                    cursor = connection.execute(
                        """INSERT OR IGNORE INTO records
                        (amap_id, scope, source, source_key, observed_at, content_hash, payload)
                        VALUES(?, ?, ?, ?, ?, ?, ?)""",
                        (
                            amap_id,
                            scope,
                            item.source,
                            key,
                            item.observed_at.astimezone(UTC).isoformat(),
                            sha256(payload.encode()).hexdigest(),
                            payload,
                        ),
                    )
                    inserted += cursor.rowcount
            return inserted
        except (OSError, sqlite3.Error) as error:
            raise LibraryError("library_unavailable") from error

    def lookup(self, amap_ids: tuple[str, ...], *, scope: str | None = None):
        if not amap_ids or not self.path.exists():
            return {}
        scopes = ("", scope) if scope else ("",)
        slots = ",".join("?" for _ in amap_ids)
        owners = ",".join("?" for _ in scopes)
        try:
            with closing(self._connect(readonly=True)) as connection:
                self._check(connection)
                rows = connection.execute(
                    f"""WITH versions AS (
                        SELECT amap_id, payload, observed_at, id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY amap_id, source, source_key
                                   ORDER BY observed_at DESC, id DESC
                               ) AS latest
                        FROM records WHERE amap_id IN ({slots}) AND scope IN ({owners})
                    ), bounded AS (
                        SELECT amap_id, payload, observed_at, id, ROW_NUMBER() OVER (
                            PARTITION BY amap_id ORDER BY observed_at DESC, id DESC
                        ) AS position FROM versions WHERE latest=1
                    ) SELECT amap_id, payload FROM bounded WHERE position<=32
                    ORDER BY observed_at DESC, id DESC""",
                    (*amap_ids, *scopes),
                ).fetchall()
            result: dict[str, list[Evidence]] = {}
            for row in rows:
                result.setdefault(row["amap_id"], []).append(
                    Evidence.model_validate_json(row["payload"])
                )
            return result
        except (OSError, sqlite3.Error, ValidationError) as error:
            raise LibraryError("library_unavailable") from error

    def inspect(self, amap_id: str, *, scope: str | None = None, history=False) -> dict:
        if not self.path.exists():
            raise LibraryError("library_missing")
        try:
            with closing(self._connect(readonly=True)) as connection:
                self._check(connection)
                branch = connection.execute(
                    "SELECT poi_json FROM branches WHERE amap_id=?", (amap_id,)
                ).fetchone()
                place = json.loads(branch[0]) if branch and branch[0] else None
                if history:
                    rows = connection.execute(
                        """SELECT payload FROM records WHERE amap_id=? AND scope IN ('', ?)
                        ORDER BY observed_at DESC, id DESC LIMIT 100""",
                        (amap_id, scope or ""),
                    ).fetchall()
                    records = [Evidence.model_validate_json(row[0]) for row in rows]
                else:
                    records = self.lookup((amap_id,), scope=scope).get(amap_id, [])
            return {"amap_id": amap_id, "place": place, "records": records}
        except (OSError, sqlite3.Error, ValidationError, json.JSONDecodeError) as error:
            raise LibraryError("library_unavailable") from error
