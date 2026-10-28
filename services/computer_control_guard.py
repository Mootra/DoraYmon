"""Yield claimed private messages to the local computer bridge while its lease is live."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time


def claimed_by_computer_bridge(payload: dict) -> bool:
    lease_path = os.environ.get("BOT_PC_BRIDGE_LEASE_PATH")
    if not lease_path or payload.get("message_type") != "private":
        return False
    try:
        lease = json.loads(Path(lease_path).read_text(encoding="utf-8"))
        if lease["expires"] <= time.time():
            return False
    except (OSError, ValueError, KeyError, TypeError):
        return False
    message = payload.get("message", [])
    if isinstance(message, list):
        text = "".join(item.get("data", {}).get("text", "")
                       for item in message if item.get("type") == "text").strip()
    else:
        text = str(payload.get("raw_message", "")).strip()
    if re.fullmatch(r"连接电脑\s+[0-9a-fA-F]{16}", text):
        return True
    if str(payload.get("user_id", "")) != lease.get("owner_id"):
        return False
    return bool(lease.get("active") or re.match(
        r"^(电脑|菜单|用\s*(?:codex|dsh)|切(?:换)?到\s*(?:codex|dsh)|退出电脑|(?:codex|dsh|c|d)(?:$|[，,:：\s]))", text, re.I,
    ))
