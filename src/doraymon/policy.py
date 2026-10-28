import json
import re
import time
from collections import deque

from aiolimiter import AsyncLimiter
from cachetools import TTLCache

from .config import Settings
from .meals import wants_meal
from .models import Incoming

STOP = re.compile(r"^(没问你|不用回复了|不用回答了|别接话了|别说了|停止|算了)[。！!\s]*$")
HELP = re.compile(r"求助|请教|帮我|怎么|如何|为什么|是什么|吃什么|吃啥|推荐.{0,6}吃")
ACK = re.compile(
    r"^(谢谢|好的|好|嗯+|哦+|收到|明白|哈哈+|ok|先这样|回头聊|晚安|不聊了)"
    r"[啦了哈呀啊]?[。！!\s]*$",
    re.I,
)


class Policy:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.active: TTLCache = TTLCache(maxsize=1024, ttl=120)
        self.muted: TTLCache = TTLCache(maxsize=1024, ttl=120)
        self.rooms: TTLCache = TTLCache(maxsize=128, ttl=300)
        self.limits: TTLCache = TTLCache(maxsize=256, ttl=300)

    def allowed(self, event: Incoming) -> bool:
        cfg = self.settings
        if event.user_id == event.bot_id:
            return False
        if cfg.access_mode == "chat":
            return (
                not event.group_id or not cfg.allowed_groups or event.group_id in cfg.allowed_groups
            )
        if event.group_id:
            return event.group_id in cfg.allowed_groups and (
                not cfg.allowed_users or event.user_id in cfg.allowed_users
            )
        return event.user_id in cfg.allowed_users

    def bridge_claims(self, event: Incoming) -> bool:
        path = self.settings.bridge_lease
        if path is None or event.group_id:
            return False
        try:
            lease = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(lease, dict) or float(lease["expires"]) <= time.time():
                return False
        except (OSError, ValueError, KeyError, TypeError):
            return False
        if re.fullmatch(r"连接电脑\s+[0-9a-fA-F]{16}", event.text):
            return True
        return event.user_id == str(lease.get("owner_id")) and bool(
            lease.get("active")
            or re.match(
                r"^(电脑|菜单|用\s*(?:codex|dsh)|退出电脑|切(?:换)?到\s*(?:codex|dsh)"
                r"|(?:codex|dsh|c|d)(?:$|[，,:：\s]))",
                event.text,
                re.I,
            )
        )

    def observe(self, event: Incoming) -> None:
        if not event.group_id:
            return
        room = self.rooms.setdefault(event.room, deque(maxlen=24))
        room.append((time.monotonic(), event))

    def context(self, event: Incoming) -> list[dict]:
        now = time.monotonic()
        turns = []
        used = 0
        current_ids = {event.message_id, *(p.message_id for p in event.parts)}
        for seen, entry in reversed(self.rooms.get(event.room, ())):
            if now - seen > 300 or entry.message_id in current_ids:
                continue
            if used + len(entry.text) > 2000:
                break
            turns.append(
                {
                    "speaker": entry.user_id,
                    "message": entry.message_id,
                    "quote": entry.quote_id,
                    "text": entry.text,
                }
            )
            used += len(entry.text)
        return list(reversed(turns))

    def called(self, event: Incoming) -> bool:
        return event.addressed or any(
            re.match(r"^(?:@)?" + re.escape(name) + r"(?:$|[\s，,:：]|你|帮|请)", event.text, re.I)
            for name in self.settings.bot_names
        )

    def addressed_to_other(self, event: Incoming) -> bool:
        if not event.group_id or self.called(event):
            return False
        if event.mentions - {event.bot_id, "all"}:
            return True
        if event.quote_id:
            quoted = next(
                (
                    entry
                    for _, entry in self.rooms.get(event.room, ())
                    if entry.message_id == event.quote_id
                ),
                None,
            )
            return quoted is None or quoted.user_id != event.bot_id
        return False

    def reason(self, event: Incoming) -> str:
        if not event.group_id:
            return "direct"
        if self.called(event):
            return "direct"
        if self.addressed_to_other(event):
            return ""
        if event.text.startswith("/"):
            return "direct"
        if ACK.fullmatch(event.text):
            self.active.pop(event.session, None)
            return ""
        if event.session in self.muted:
            return ""
        if event.session in self.active:
            return "direct"
        return (
            "ambient"
            if self.settings.group_participation
            and (HELP.search(event.text) or wants_meal(event.text, None))
            else ""
        )

    async def admit(self, event: Incoming, reason: str) -> bool:
        if not event.group_id:
            return True
        key = event.room, reason
        limiter = self.limits.get(key)
        if limiter is None:
            limiter = self.limits[key] = (
                AsyncLimiter(1, 30) if reason == "ambient" else AsyncLimiter(8, 60)
            )
        if reason == "ambient" and not limiter.has_capacity():
            return False
        await limiter.acquire()
        return True

    def delivered(self, event: Incoming) -> None:
        if event.group_id:
            self.active[event.session] = True
            self.muted.pop(event.session, None)

    def stop(self, event: Incoming) -> None:
        self.active.pop(event.session, None)
        self.muted[event.session] = True
