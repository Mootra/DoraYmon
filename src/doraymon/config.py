from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DORAYMON_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    access_mode: Literal["allowlist", "chat"] = "allowlist"
    allowed_users: frozenset[str] = frozenset()
    allowed_groups: frozenset[str] = frozenset()
    admin_users: frozenset[str] = frozenset()
    database: Path = Path("data/runtime.sqlite3")
    bridge_lease: Path | None = None
    model_key: SecretStr = SecretStr("")
    model_url: str = "https://api.deepseek.com"
    model_name: str = "deepseek-v4-flash"
    model_max_tokens: int = Field(default=2048, ge=128, le=8192)
    amap_key: SecretStr = SecretStr("")
    default_location: str = "江西师范大学瑶湖校区"
    radius_meters: int = Field(default=1500, ge=200, le=50000)
    history_turns: int = Field(default=12, ge=1, le=50)
    history_ttl_seconds: int = Field(default=43200, ge=60)
    context_chars: int = Field(default=6000, ge=500, le=32000)
    reply_chars: int = Field(default=4000, ge=100, le=8000)
    model_concurrency: int = Field(default=4, ge=1, le=16)
    max_sessions: int = Field(default=128, ge=1, le=1024)
    session_queue: int = Field(default=8, ge=1, le=32)
    merge_gap: float = Field(default=0.35, ge=0, le=2)
    merge_window: float = Field(default=1.5, ge=0, le=5)
    group_participation: bool = True
    bot_names: tuple[str, ...] = ("DoraYmon", "哆啦A梦", "哆啦", "小哆")
    knowledge_enabled: bool = False
