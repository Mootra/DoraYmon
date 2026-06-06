from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from doraymon.config import load_settings


def _data_dir() -> Path:
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
