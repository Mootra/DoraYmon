"""Bounded metadata-only idempotence journal, preserved across backend restarts."""

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path


class AdminJournal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS actions ("
                "id TEXT PRIMARY KEY, digest TEXT, created REAL, payload TEXT)"
            )
            # A crash after persisting intent may have happened after QQ accepted the send.
            for key, payload in db.execute("SELECT id,payload FROM actions").fetchall():
                value = json.loads(payload)
                if value["state"] == "running":
                    value.update(state="uncertain", message="后台重启前的操作结果需要在 QQ 核对。")
                    db.execute(
                        "UPDATE actions SET payload=? WHERE id=?",
                        (json.dumps(value, ensure_ascii=False), key),
                    )
        path.chmod(0o600)

    def get(self, key: str) -> tuple[str, dict] | None:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT digest,payload FROM actions WHERE id=?", (key,)).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def save(self, digest: str, value: dict) -> None:
        payload = {key: item for key, item in value.items() if key != "data"}
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "INSERT INTO actions VALUES(?,?,?,?) ON CONFLICT(id) "
                "DO UPDATE SET payload=excluded.payload",
                (value["request_id"], digest, time.time(), json.dumps(payload)),
            )
            db.execute("DELETE FROM actions WHERE created<?", (time.time() - 7 * 86400,))
            db.execute(
                "DELETE FROM actions WHERE id IN (SELECT id FROM actions "
                "ORDER BY created DESC LIMIT -1 OFFSET 2048)"
            )

    def history(self) -> list[dict]:
        with closing(sqlite3.connect(self.path)) as db:
            return [
                json.loads(row[0])
                for row in db.execute("SELECT payload FROM actions ORDER BY created DESC LIMIT 100")
            ]
