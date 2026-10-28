from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from contextvars import ContextVar
from pathlib import Path

from doraymon.config import load_settings


_runtime_data_dir: ContextVar[Path | None] = ContextVar("runtime_data_dir", default=None)


@contextmanager
def use_data_directory(path: Path):
    """Scope local tools to an explicit database directory, including worker threads."""
    token = _runtime_data_dir.set(path.resolve())
    try:
        yield
    finally:
        _runtime_data_dir.reset(token)


def _data_dir() -> Path:
    override = _runtime_data_dir.get()
    if override is not None:
        override.mkdir(parents=True, exist_ok=True)
        return override
    settings = load_settings()
    settings.ensure_runtime_dirs()
    return settings.data_path


def get_db_path(name: str) -> Path:
    safe_name = name if name.endswith((".db", ".sqlite")) else f"{name}.db"
    return _data_dir() / safe_name


def get_connection(name: str) -> sqlite3.Connection:
    connection = sqlite3.connect(get_db_path(name))
    connection.row_factory = sqlite3.Row
    return connection


def init_all_tables() -> None:
    with closing(get_connection("doraymon")) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_openid TEXT PRIMARY KEY,
                nickname TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sign_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_openid TEXT NOT NULL,
                sign_date TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_openid, sign_date)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS todos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_openid TEXT NOT NULL,
                content TEXT NOT NULL,
                is_done INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS meal_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_key TEXT NOT NULL,
                shop_id TEXT NOT NULL,
                item_id TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_meal_feedback_session_kind
            ON meal_feedback (session_key, kind)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS food_preferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_openid TEXT NOT NULL,
                preference_text TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_openid, preference_text)
            )
            """
        )
        connection.commit()
