"""A run-time silence switch, so a misbehaving bot can be stopped without a restart.

The plan asks for a silence control on the minimum admin surface, and G3 requires
it before any wider trial. The reason it matters on a real account is concrete: if
the bot starts answering the wrong things at 3am, the operator needs one action
that takes effect immediately. Editing configuration and restarting is not that
action, and neither is killing the process, because then nobody can turn it back
on remotely.

Design notes:

- Silence blocks ordinary replies. It does not block an administrator's silence
  command, because a switch that cannot be turned off from the same channel is a
  trap.
- The state is process-wide and in memory. A restart returns to the configured
  default, which is stated rather than implied: an operator who silences the bot
  and then redeploys should expect it back on, and should know that.
- Enabling silence also reports why, so the log explains a quiet bot later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

SILENCE_COMMAND = "静默"


@dataclass
class SilenceState:
    """Process-wide silence, with the reason and time it was set."""

    silent: bool = False
    changed_at: datetime | None = None
    reason: str = ""
    changes: int = 0

    def enable(self, reason: str = "", *, now: datetime | None = None) -> bool:
        """Silence the bot. Returns False when it was already silent."""
        already = self.silent
        self.silent = True
        self.reason = str(reason or "").strip()
        self.changed_at = now or datetime.now()
        self.changes += 1
        return not already

    def disable(self, *, now: datetime | None = None) -> bool:
        """Resume the bot. Returns False when it was already speaking."""
        already_awake = not self.silent
        self.silent = False
        self.reason = ""
        self.changed_at = now or datetime.now()
        self.changes += 1
        return not already_awake

    def clear(self) -> None:
        self.silent = False
        self.changed_at = None
        self.reason = ""
        self.changes = 0

    def describe(self) -> str:
        if not self.silent:
            return "未静默：机器人会正常回复。"
        when = f"{self.changed_at:%Y-%m-%d %H:%M:%S}" if self.changed_at else "未知时间"
        reason = f"，原因：{self.reason}" if self.reason else ""
        return f"已静默（{when} 起{reason}）：机器人不会回复普通消息，但仍可执行 /{SILENCE_COMMAND}。"


SILENCE = SilenceState()


def is_silent(state: SilenceState | None = None) -> bool:
    return (state or SILENCE).silent


def should_reply(settings: Any, state: SilenceState | None = None) -> bool:
    """Whether an ordinary reply may be produced.

    Configuration can start the process silent; the run-time switch can silence it
    later. Either one is enough to stay quiet.
    """
    if is_silent(state):
        return False
    return not bool(getattr(settings, "silence_default", False))


def describe_effective(settings: Any, state: SilenceState | None = None) -> str:
    """The state an operator actually cares about, including the config default."""
    active = state or SILENCE
    if active.silent:
        return active.describe()
    if bool(getattr(settings, "silence_default", False)):
        return "配置为启动即静默：机器人不会回复，除非用 /静默 off 打开。"
    return active.describe()


def parse_silence_args(args: str) -> str:
    """Normalise a silence subcommand to on/off/status/unknown.

    Accepted spellings include the obvious Chinese and English ones, because an
    operator reaching for this switch should not have to guess the exact word.
    """
    text = str(args or "").strip().lower()
    if not text:
        return "status"
    if text in {"on", "off"}:
        return text
    if text in {"1", "true", "yes", "开", "开启", "打开", "静默"}:
        return "on"
    if text in {"0", "false", "no", "关", "关闭", "解除", "恢复"}:
        return "off"
    if text in {"status", "状态", "查看"}:
        return "status"
    return "unknown"
