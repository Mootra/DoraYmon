"""Persist reply controls and use the SDK's temporary group-frequency adjustment."""

import asyncio
import json
import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import tomlkit
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .admin import QQ_ID
from .ops.overlay import write_text_atomic

logger = logging.getLogger(__name__)


class ReplySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    at_reply: bool = True
    normal_frequency: float = Field(default=0.02, ge=0.001, le=1)
    conversation_frequency: float = Field(default=0.8, ge=0.001, le=1)
    conversation_minutes: float = Field(default=5, ge=0, le=60)

    @model_validator(mode="after")
    def check_frequencies(self):
        if self.conversation_minutes and self.conversation_frequency < self.normal_frequency:
            raise ValueError("连续对话活跃度不能低于平时活跃度。")
        return self


GroupId = Annotated[str, Field(pattern=QQ_ID)]


class ReplyConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    defaults: ReplySettings = Field(default_factory=ReplySettings)
    groups: dict[GroupId, ReplySettings] = Field(default_factory=dict)

    def for_group(self, group_id: str) -> ReplySettings:
        return self.groups.get(group_id, self.defaults)


class GroupReplyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    group_id: GroupId
    settings: ReplySettings


class ReplySettingsStore:
    def __init__(self, path: Path, bot_config: Path, overlay: Path):
        self.path, self.bot_config, self.overlay = path, bot_config, overlay

    def read(self) -> ReplyConfiguration:
        data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if "defaults" in data or "groups" in data:
            return ReplyConfiguration.model_validate(data)
        defaults = ReplySettings.model_validate(data)
        return ReplyConfiguration(
            defaults=defaults, groups={group: defaults for group in self.group_ids()}
        )

    def group_ids(self) -> list[str]:
        policy = self.bot_config.with_name("adapter_policy.toml")
        if not policy.exists():
            return []
        data = tomllib.loads(policy.read_text(encoding="utf-8"))
        return list(data.get("defaults", {}).get("group", {}).get("allow_ids", []))

    def save(self, group_id: str, settings: ReplySettings) -> None:
        update = GroupReplyUpdate(group_id=group_id, settings=settings)
        saved = self.read()
        groups = {**saved.groups, update.group_id: update.settings}
        configuration = ReplyConfiguration(defaults=saved.defaults, groups=groups)
        changes = {
            "inevitable_at_reply": False,
            "mentioned_bot_reply": False,
            "enable_talk_value_rules": True,
        }
        originals = {
            path: path.read_text(encoding="utf-8") if path.exists() else None
            for path in (self.bot_config, self.overlay, self.path)
        }
        document = tomlkit.parse(originals[self.bot_config])
        timing = document["chat"]["reply_timing"]
        if not changes.keys() <= timing.keys():
            raise ValueError("当前 MaiBot 配置不支持这些回复参数。")
        # Disabled rules are historical configuration, not current group settings.
        rules = (
            list(timing.get("talk_value_rules", [])) if timing["enable_talk_value_rules"] else []
        )
        rules = [
            rule
            for rule in rules
            if not (
                rule.get("rule_type") == "group"
                and rule.get("platform", "") in {"", "qq"}
                and rule.get("item_id") in groups
            )
        ]
        rules.extend(
            {
                "platform": "qq",
                "item_id": group,
                "rule_type": "group",
                "time": "*",
                "value": value.normal_frequency,
            }
            for group, value in sorted(groups.items())
        )
        changes["talk_value_rules"] = rules
        timing.update(changes)
        overlay = tomlkit.parse(originals[self.overlay] or "")
        overlay.setdefault("chat", tomlkit.table()).setdefault(
            "reply_timing", tomlkit.table()
        ).update(changes)
        contents = {
            self.bot_config: tomlkit.dumps(document),
            self.overlay: tomlkit.dumps(overlay),
            self.path: json.dumps(configuration.model_dump(), ensure_ascii=False, indent=2),
        }
        written = []
        try:
            for path, content in contents.items():
                if originals[path] is not None:
                    backup = path.with_name(path.name + ".reply-backup")
                    write_text_atomic(backup, originals[path])
                    backup.chmod(0o600)
                write_text_atomic(path, content)
                written.append(path)
        except OSError:
            for path in reversed(written):
                if originals[path] is None:
                    path.unlink(missing_ok=True)
                else:
                    write_text_atomic(path, originals[path])
            raise


@dataclass
class ActiveConversation:
    previous_adjust: float
    timer: asyncio.Task


class ReplyPolicy:
    def __init__(self, store: ReplySettingsStore, config, frequency, *, sleep=asyncio.sleep):
        self.store, self.config, self.frequency = store, config, frequency
        self.settings = store.read()
        self.active: dict[str, ActiveConversation] = {}
        self._sleep = sleep
        self._lock = asyncio.Lock()

    async def read(self) -> dict:
        saved = self.store.read()
        at_reply, mentioned_reply, enabled, rules = await asyncio.gather(
            *(
                self.config.get("chat.reply_timing." + key)
                for key in (
                    "inevitable_at_reply",
                    "mentioned_bot_reply",
                    "enable_talk_value_rules",
                    "talk_value_rules",
                )
            )
        )
        groups = []
        for group_id in sorted(set(self.store.group_ids()) | saved.groups.keys()):
            settings = saved.for_group(group_id)
            applied = (
                settings == self.settings.for_group(group_id)
                and not at_reply
                and not mentioned_reply
                and enabled
                and any(
                    rule.get("platform") == "qq"
                    and rule.get("rule_type") == "group"
                    and rule.get("item_id") == group_id
                    and rule.get("time") == "*"
                    and rule.get("value") == settings.normal_frequency
                    for rule in rules
                )
            )
            groups.append(
                {"group_id": group_id, "settings": settings.model_dump(), "applied": applied}
            )
        return {"groups": groups}

    async def save(self, data: dict) -> dict:
        update = GroupReplyUpdate.model_validate(data)
        self.store.save(update.group_id, update.settings)
        return {"ok": True, "applied": False}

    async def activate(self, session_id: str, group_id: str) -> None:
        settings = self.settings.for_group(group_id)
        if not settings.conversation_minutes:
            return
        async with self._lock:
            adjust = await self.frequency.get_adjust(session_id)
            current = await self.frequency.get_current_talk_value(session_id)
            # A deliberately silent stream must stay silent.
            if adjust <= 0 or current <= 0:
                return
            existing = self.active.get(session_id)
            previous = existing.previous_adjust if existing else adjust
            value = max(previous, settings.conversation_frequency / (current / adjust))
            if value <= previous:
                return
            if not existing and len(self.active) >= 128:
                await self._restore(next(iter(self.active)))
            if not await self.frequency.set_adjust(session_id, value):
                raise RuntimeError("连续对话活跃度未能应用。")
            if existing:
                existing.timer.cancel()
            task = asyncio.create_task(self._expire(session_id, settings.conversation_minutes))
            self.active[session_id] = ActiveConversation(previous, task)
            task.add_done_callback(self._timer_finished)

    @staticmethod
    def _timer_finished(task: asyncio.Task) -> None:
        if not task.cancelled() and (error := task.exception()):
            logger.error("连续对话计时恢复失败", exc_info=error)

    async def _restore(self, session_id: str) -> None:
        entry = self.active[session_id]
        if not await self.frequency.set_adjust(session_id, entry.previous_adjust):
            raise RuntimeError("平时活跃度未能恢复。")
        if entry.timer is not asyncio.current_task():
            entry.timer.cancel()
        del self.active[session_id]

    async def _expire(self, session_id: str, minutes: float) -> None:
        await self._sleep(minutes * 60)
        async with self._lock:
            await self._restore(session_id)

    async def close(self) -> None:
        async with self._lock:
            for session_id in list(self.active):
                await self._restore(session_id)
