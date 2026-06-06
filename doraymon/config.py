from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_yaml_config() -> dict[str, Any]:
    config_path = PROJECT_ROOT / "config.yaml"
    if not config_path.exists():
        return {}

    with config_path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}
    if not isinstance(data, dict):
        return {}
    return data


def _nested(config: dict[str, Any], path: str, default: Any = "") -> Any:
    current: Any = config
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def _env_or_config(env_name: str, config: dict[str, Any], path: str, default: Any = "") -> Any:
    value = os.getenv(env_name)
    if value is not None:
        return value
    return _nested(config, path, default)


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_openid_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    qqbot_appid: str = ""
    qqbot_secret: str = ""
    qqbot_sandbox: bool = True
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_temperature: float = 0.7
    command_prefix: str = "/"
    admin_openids: list[str] = field(default_factory=list)
    food_natural_trigger_enabled: bool = True
    log_level: str = "INFO"
    data_dir: str = "data"
    log_dir: str = "logs"

    @property
    def data_path(self) -> Path:
        return PROJECT_ROOT / self.data_dir

    @property
    def log_path(self) -> Path:
        return PROJECT_ROOT / self.log_dir

    def ensure_runtime_dirs(self) -> None:
        self.data_path.mkdir(parents=True, exist_ok=True)
        self.log_path.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")
    config = _load_yaml_config()

    return Settings(
        qqbot_appid=str(_env_or_config("QQBOT_APPID", config, "qqbot.appid", "")).strip(),
        qqbot_secret=str(_env_or_config("QQBOT_SECRET", config, "qqbot.secret", "")).strip(),
        qqbot_sandbox=_to_bool(_env_or_config("QQBOT_SANDBOX", config, "qqbot.sandbox", True)),
        deepseek_api_key=str(_env_or_config("DEEPSEEK_API_KEY", config, "deepseek.api_key", "")).strip(),
        deepseek_base_url=str(
            _env_or_config("DEEPSEEK_BASE_URL", config, "deepseek.base_url", "https://api.deepseek.com")
        ).rstrip("/"),
        deepseek_model=str(_env_or_config("DEEPSEEK_MODEL", config, "deepseek.model", "deepseek-v4-flash")).strip(),
        deepseek_temperature=_to_float(
            _env_or_config("DEEPSEEK_TEMPERATURE", config, "deepseek.temperature", 0.7),
            0.7,
        ),
        command_prefix=str(_env_or_config("BOT_COMMAND_PREFIX", config, "bot.command_prefix", "/")).strip() or "/",
        admin_openids=_to_openid_list(_env_or_config("BOT_ADMIN_OPENIDS", config, "bot.admin_openids", "")),
        food_natural_trigger_enabled=_to_bool(
            _env_or_config(
                "BOT_ENABLE_FOOD_NATURAL_TRIGGER",
                config,
                "food.natural_trigger",
                True,
            )
        ),
        log_level=str(_env_or_config("LOG_LEVEL", config, "log.level", "INFO")).strip() or "INFO",
        data_dir=str(_env_or_config("DATA_DIR", config, "paths.data_dir", "data")).strip() or "data",
        log_dir=str(_env_or_config("LOG_DIR", config, "paths.log_dir", "logs")).strip() or "logs",
    )
