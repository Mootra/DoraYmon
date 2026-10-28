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


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
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
    # The official platform only accepts a bounded number of replies per inbound
    # message; enforcing it locally avoids refused sends.
    qqbot_reply_window_enabled: bool = True
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_temperature: float = 0.7
    command_prefix: str = "/"
    default_location: str = "江西师范大学瑶湖校区"
    admin_openids: list[str] = field(default_factory=list)
    # Start silent and stay silent until an administrator says otherwise. Useful
    # for a first deployment where the operator wants to watch traffic before
    # letting the bot speak.
    silence_default: bool = False
    # Daily model budget, in tokens. Tokens are reported by the API and therefore
    # measurable; a money cap would need a price list that changes without notice.
    daily_token_cap: int = 0
    # Optional, for display only: a configured unit price yields an estimate that
    # is labelled as one.
    model_price_per_million_cny: float = 0.0
    food_natural_trigger_enabled: bool = True
    # Where the curated shop/meal catalogue lives. Empty means the rule-based
    # sample catalogue answers instead.
    meal_catalog_dir: str = "data/meal_catalog"
    # Optional map/search provider for nearby lookups. Empty keeps the bot fully
    # offline: it then answers only from the curated catalogue.
    nearby_provider: str = ""
    nearby_provider_key: str = ""
    nearby_provider_city: str = ""
    nearby_provider_radius_meters: int = 1500
    chat_history_enabled: bool = True
    chat_history_limit: int = 40
    chat_history_max_content_length: int = 4000
    chat_context_max_chars: int = 6000
    chat_context_summary_max_chars: int = 1200
    chat_context_ttl_minutes: int = 720
    chat_summary_retention_days: int = 7
    reply_max_segments: int = 3
    reply_max_total_chars: int = 4000
    rag_enabled: bool = False
    rag_top_k: int = 3
    rag_tokenizer: str = "trigram"
    rag_max_context_chars: int = 6000
    rag_chunk_max_chars: int = 800
    rag_chunk_overlap_chars: int = 100
    knowledge_dir: str = "resources/knowledge"
    # QQ account entry (OneBot v11 / NapCat). Disabled by default: this reaches a
    # real QQ account, so it must be switched on deliberately.
    qq_account_enabled: bool = False
    qq_account_ws_url: str = "ws://127.0.0.1:3001"
    qq_account_token: str = ""
    qq_account_self_id: str = ""
    qq_account_access_mode: str = "chat"
    qq_account_groups: list[str] = field(default_factory=list)
    qq_account_friends: list[str] = field(default_factory=list)
    qq_account_group_reply_mode: str = "reply"
    # Direct requests and ordinary participation have independent limits.
    qq_account_group_participation_enabled: bool = True
    qq_account_bot_names: list[str] = field(default_factory=lambda: ["DoraYmon", "哆啦A梦", "哆啦", "小哆"])
    qq_account_group_participation_cooldown_seconds: float = 30.0
    qq_account_group_participation_max_replies: int = 2
    qq_account_group_cooldown_seconds: float = 0.0
    qq_account_group_max_replies: int = 8
    qq_account_group_rate_window_seconds: float = 60.0
    # Short same-speaker bursts are coalesced before routing.  Zero disables
    # the wait while retaining the explicit maximum bound.
    qq_account_input_merge_wait_seconds: float = 0.35
    qq_account_input_merge_max_wait_seconds: float = 1.5
    # Feishu entry (self-built app over the official long connection). Disabled by
    # default: it needs real app credentials, so it must be switched on
    # deliberately.
    feishu_enabled: bool = False
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    # The bot's own open_id. Required: without it a group message cannot be
    # recognised as addressed to the bot, and staying quiet is the safer failure.
    feishu_bot_open_id: str = ""
    feishu_chats: list[str] = field(default_factory=list)
    feishu_users: list[str] = field(default_factory=list)
    feishu_group_reply_mode: str = "reply"
    # Self-imposed group throttling, on by default for the same reason as the
    # account entry: answering everything in a busy group looks like spam.
    feishu_group_cooldown_seconds: float = 3.0
    feishu_group_max_replies: int = 8
    feishu_group_rate_window_seconds: float = 60.0
    # feishu.cn or larksuite.com: the two deployments do not share a host, and
    # sending to the wrong one fails with what looks like a bad secret.
    feishu_domain: str = "feishu"
    feishu_timeout_seconds: float = 10.0
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
        qqbot_reply_window_enabled=_to_bool(
            _env_or_config(
                "QQBOT_REPLY_WINDOW_ENABLED",
                config,
                "qqbot.reply_window_enabled",
                True,
            )
        ),
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
        default_location=str(
            _env_or_config("BOT_DEFAULT_LOCATION", config, "bot.default_location", "江西师范大学瑶湖校区")
        ).strip(),
        admin_openids=_to_openid_list(_env_or_config("BOT_ADMIN_OPENIDS", config, "bot.admin_openids", "")),
        silence_default=_to_bool(
            _env_or_config("BOT_SILENCE", config, "bot.silence", False)
        ),
        daily_token_cap=_to_int(
            _env_or_config("BOT_DAILY_TOKEN_CAP", config, "bot.daily_token_cap", 0), 0
        ),
        model_price_per_million_cny=_to_float(
            _env_or_config(
                "BOT_MODEL_PRICE_PER_MILLION_CNY",
                config,
                "bot.model_price_per_million_cny",
                0.0,
            ),
            0.0,
        ),
        food_natural_trigger_enabled=_to_bool(
            _env_or_config(
                "BOT_ENABLE_FOOD_NATURAL_TRIGGER",
                config,
                "food.natural_trigger",
                True,
            )
        ),
        nearby_provider=str(
            _env_or_config("BOT_NEARBY_PROVIDER", config, "food.nearby_provider", "")
        ).strip().lower(),
        nearby_provider_key=str(
            _env_or_config("BOT_NEARBY_PROVIDER_KEY", config, "food.nearby_provider_key", "")
        ).strip(),
        nearby_provider_city=str(
            _env_or_config("BOT_NEARBY_PROVIDER_CITY", config, "food.nearby_provider_city", "")
        ).strip(),
        nearby_provider_radius_meters=_to_int(
            _env_or_config(
                "BOT_NEARBY_PROVIDER_RADIUS_METERS",
                config,
                "food.nearby_provider_radius_meters",
                1500,
            ),
            1500,
        ),
        meal_catalog_dir=str(
            _env_or_config(
                "BOT_MEAL_CATALOG_DIR",
                config,
                "food.catalog_dir",
                "data/meal_catalog",
            )
        ).strip(),
        chat_history_enabled=_to_bool(
            _env_or_config(
                "BOT_ENABLE_CHAT_HISTORY",
                config,
                "chat.history_enabled",
                True,
            )
        ),
        chat_history_limit=_to_int(
            _env_or_config("BOT_CHAT_HISTORY_LIMIT", config, "chat.history_limit", 40),
            40,
        ),
        chat_history_max_content_length=_to_int(
            _env_or_config(
                "BOT_CHAT_HISTORY_MAX_CONTENT_LENGTH",
                config,
                "chat.history_max_content_length",
                4000,
            ),
            4000,
        ),
        chat_context_max_chars=_to_int(
            _env_or_config(
                "BOT_CHAT_CONTEXT_MAX_CHARS",
                config,
                "chat.context_max_chars",
                6000,
            ),
            6000,
        ),
        chat_context_summary_max_chars=_to_int(
            _env_or_config(
                "BOT_CHAT_CONTEXT_SUMMARY_MAX_CHARS",
                config,
                "chat.context_summary_max_chars",
                1200,
            ),
            1200,
        ),
        chat_context_ttl_minutes=_to_int(
            _env_or_config(
                "BOT_CHAT_CONTEXT_TTL_MINUTES",
                config,
                "chat.context_ttl_minutes",
                720,
            ),
            720,
        ),
        chat_summary_retention_days=_to_int(
            _env_or_config(
                "BOT_CHAT_SUMMARY_RETENTION_DAYS",
                config,
                "chat.summary_retention_days",
                7,
            ),
            7,
        ),
        reply_max_segments=_to_int(
            _env_or_config("BOT_REPLY_MAX_SEGMENTS", config, "reply.max_segments", 3),
            3,
        ),
        reply_max_total_chars=_to_int(
            _env_or_config("BOT_REPLY_MAX_TOTAL_CHARS", config, "reply.max_total_chars", 4000),
            4000,
        ),
        rag_enabled=_to_bool(
            _env_or_config("BOT_ENABLE_RAG", config, "rag.enabled", False)
        ),
        rag_top_k=_to_int(
            _env_or_config("BOT_RAG_TOP_K", config, "rag.top_k", 3),
            3,
        ),
        rag_tokenizer=str(
            _env_or_config("BOT_RAG_TOKENIZER", config, "rag.tokenizer", "trigram")
        ).strip().lower()
        or "trigram",
        rag_max_context_chars=_to_int(
            _env_or_config(
                "BOT_RAG_MAX_CONTEXT_CHARS",
                config,
                "rag.max_context_chars",
                6000,
            ),
            6000,
        ),
        rag_chunk_max_chars=_to_int(
            _env_or_config(
                "BOT_RAG_CHUNK_MAX_CHARS",
                config,
                "rag.chunk_max_chars",
                800,
            ),
            800,
        ),
        rag_chunk_overlap_chars=_to_int(
            _env_or_config(
                "BOT_RAG_CHUNK_OVERLAP_CHARS",
                config,
                "rag.chunk_overlap_chars",
                100,
            ),
            100,
        ),
        knowledge_dir=str(
            _env_or_config(
                "BOT_KNOWLEDGE_DIR",
                config,
                "rag.knowledge_dir",
                "resources/knowledge",
            )
        ).strip()
        or "resources/knowledge",
        qq_account_enabled=_to_bool(
            _env_or_config(
                "BOT_QQ_ACCOUNT_ENABLED", config, "qq_account.enabled", False
            )
        ),
        qq_account_ws_url=str(
            _env_or_config(
                "BOT_QQ_ACCOUNT_WS_URL",
                config,
                "qq_account.ws_url",
                "ws://127.0.0.1:3001",
            )
        ).strip()
        or "ws://127.0.0.1:3001",
        qq_account_token=str(
            _env_or_config("BOT_QQ_ACCOUNT_TOKEN", config, "qq_account.token", "")
        ).strip(),
        qq_account_self_id=str(
            _env_or_config("BOT_QQ_ACCOUNT_SELF_ID", config, "qq_account.self_id", "")
        ).strip(),
        qq_account_access_mode=str(
            _env_or_config("BOT_QQ_ACCOUNT_ACCESS_MODE", config, "qq_account.access_mode", "chat")
        ).strip().lower(),
        qq_account_group_participation_enabled=_to_bool(
            _env_or_config("BOT_QQ_ACCOUNT_GROUP_PARTICIPATION_ENABLED", config, "qq_account.group_participation_enabled", True)
        ),
        qq_account_bot_names=_to_openid_list(
            _env_or_config("BOT_QQ_ACCOUNT_BOT_NAMES", config, "qq_account.bot_names", "DoraYmon,哆啦A梦,哆啦,小哆")
        ),
        qq_account_group_participation_cooldown_seconds=_to_float(
            _env_or_config("BOT_QQ_ACCOUNT_GROUP_PARTICIPATION_COOLDOWN_SECONDS", config, "qq_account.group_participation_cooldown_seconds", 30.0), 30.0,
        ),
        qq_account_group_participation_max_replies=_to_int(
            _env_or_config("BOT_QQ_ACCOUNT_GROUP_PARTICIPATION_MAX_REPLIES", config, "qq_account.group_participation_max_replies", 2), 2,
        ),
        qq_account_groups=_to_openid_list(
            _env_or_config("BOT_QQ_ACCOUNT_GROUPS", config, "qq_account.groups", "")
        ),
        qq_account_friends=_to_openid_list(
            _env_or_config("BOT_QQ_ACCOUNT_FRIENDS", config, "qq_account.friends", "")
        ),
        qq_account_group_cooldown_seconds=_to_float(
            _env_or_config(
                "BOT_QQ_ACCOUNT_GROUP_COOLDOWN_SECONDS",
                config,
                "qq_account.group_cooldown_seconds",
                0.0,
            ),
            0.0,
        ),
        qq_account_group_max_replies=_to_int(
            _env_or_config(
                "BOT_QQ_ACCOUNT_GROUP_MAX_REPLIES",
                config,
                "qq_account.group_max_replies",
                8,
            ),
            8,
        ),
        qq_account_group_rate_window_seconds=_to_float(
            _env_or_config(
                "BOT_QQ_ACCOUNT_GROUP_RATE_WINDOW_SECONDS",
                config,
                "qq_account.group_rate_window_seconds",
                60.0,
            ),
            60.0,
        ),
        qq_account_input_merge_wait_seconds=_to_float(
            _env_or_config(
                "BOT_QQ_ACCOUNT_INPUT_MERGE_WAIT_SECONDS",
                config,
                "qq_account.input_merge_wait_seconds",
                0.35,
            ),
            0.35,
        ),
        qq_account_input_merge_max_wait_seconds=_to_float(
            _env_or_config(
                "BOT_QQ_ACCOUNT_INPUT_MERGE_MAX_WAIT_SECONDS",
                config,
                "qq_account.input_merge_max_wait_seconds",
                1.5,
            ),
            1.5,
        ),
        qq_account_group_reply_mode=str(
            _env_or_config(
                "BOT_QQ_ACCOUNT_GROUP_REPLY_MODE",
                config,
                "qq_account.group_reply_mode",
                "reply",
            )
        ).strip().lower()
        or "reply",
        feishu_enabled=_to_bool(
            _env_or_config("BOT_FEISHU_ENABLED", config, "feishu.enabled", False)
        ),
        feishu_app_id=str(
            _env_or_config("BOT_FEISHU_APP_ID", config, "feishu.app_id", "")
        ).strip(),
        feishu_app_secret=str(
            _env_or_config("BOT_FEISHU_APP_SECRET", config, "feishu.app_secret", "")
        ).strip(),
        feishu_bot_open_id=str(
            _env_or_config("BOT_FEISHU_BOT_OPEN_ID", config, "feishu.bot_open_id", "")
        ).strip(),
        feishu_chats=_to_openid_list(
            _env_or_config("BOT_FEISHU_CHATS", config, "feishu.chats", "")
        ),
        feishu_users=_to_openid_list(
            _env_or_config("BOT_FEISHU_USERS", config, "feishu.users", "")
        ),
        feishu_group_cooldown_seconds=_to_float(
            _env_or_config(
                "BOT_FEISHU_GROUP_COOLDOWN_SECONDS",
                config,
                "feishu.group_cooldown_seconds",
                3.0,
            ),
            3.0,
        ),
        feishu_group_max_replies=_to_int(
            _env_or_config(
                "BOT_FEISHU_GROUP_MAX_REPLIES",
                config,
                "feishu.group_max_replies",
                8,
            ),
            8,
        ),
        feishu_group_rate_window_seconds=_to_float(
            _env_or_config(
                "BOT_FEISHU_GROUP_RATE_WINDOW_SECONDS",
                config,
                "feishu.group_rate_window_seconds",
                60.0,
            ),
            60.0,
        ),
        feishu_group_reply_mode=str(
            _env_or_config(
                "BOT_FEISHU_GROUP_REPLY_MODE",
                config,
                "feishu.group_reply_mode",
                "reply",
            )
        ).strip().lower()
        or "reply",
        feishu_domain=str(
            _env_or_config("BOT_FEISHU_DOMAIN", config, "feishu.domain", "feishu")
        ).strip().lower()
        or "feishu",
        feishu_timeout_seconds=_to_float(
            _env_or_config(
                "BOT_FEISHU_TIMEOUT_SECONDS",
                config,
                "feishu.timeout_seconds",
                10.0,
            ),
            10.0,
        ),
        log_level=str(_env_or_config("LOG_LEVEL", config, "log.level", "INFO")).strip() or "INFO",
        data_dir=str(_env_or_config("DATA_DIR", config, "paths.data_dir", "data")).strip() or "data",
        log_dir=str(_env_or_config("LOG_DIR", config, "paths.log_dir", "logs")).strip() or "logs",
    )
