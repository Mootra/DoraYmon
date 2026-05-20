from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from doraymon.config import Settings


@dataclass
class BotContext:
    settings: Settings
    started_at: datetime
    user_openid: str = ""
    group_openid: str = ""
    raw_content: str = ""
    command: str = ""
    args: str = ""
    is_at_message: bool = False
    message: Any = None
