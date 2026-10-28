from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from services.group_dialogue import GroupDialogueContext
    from services.message_contract import InputPart

from doraymon.config import Settings
from doraymon.identity import PLATFORM_QQ_BOT


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
    # Identity namespace: which platform and which bot account saw this message.
    # Defaults keep the official QQ Bot entry working without changes.
    platform: str = PLATFORM_QQ_BOT
    bot_account: str = ""
    background_tasks: set[Any] | None = None
    addressed_to_bot: bool = False
    group_dialogue: GroupDialogueContext | None = None
    # Original message boundaries when the account session coalesced a short
    # burst.  Kept as metadata so plugins can render the combined text without
    # losing quote or message identity.
    input_parts: tuple[InputPart, ...] = ()
