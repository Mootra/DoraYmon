"""Access policy for the text-channel entries.

The generic default remains closed. QQ's entry explicitly selects normal chat or
an allowlist; Feishu continues to supply its own closed access scope.

Both lists are compared against raw platform identifiers — a QQ number, a Feishu
open_id, a chat id. Nothing here knows which of those it is, which is why one
policy serves two protocols.

This module has no I/O and no configuration reading, so every branch is testable
offline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.message_contract import MessageEvent

DECISION_ALLOWED = "allowed"
DECISION_GROUP_MISSING = "group_not_allowed"
DECISION_USER_MISSING = "user_not_allowed"
DECISION_EMPTY_ALLOWLIST = "empty_allowlist"


def as_id_list(value: Any) -> list[str]:
    """Coerce an allowlist into a list of ids.

    A bare string would iterate into single characters, which silently produces an
    allowlist of digits: ``"30003"`` becomes ``{"3", "0"}``. That looks like a
    configured allowlist while allowing nothing real, so a string is split on
    commas here rather than trusted.
    """
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(item).strip() for item in (value or []) if str(item).strip()]


@dataclass(frozen=True)
class AccessDecision:
    """Why one event may or may not be handled."""

    allowed: bool
    reason: str
    detail: str = ""

    def describe(self) -> str:
        if self.allowed:
            return "允许"
        return {
            DECISION_GROUP_MISSING: "该群不在允许列表",
            DECISION_USER_MISSING: "该用户不在允许列表",
            DECISION_EMPTY_ALLOWLIST: "允许列表为空，账号入口默认拒绝",
        }.get(self.reason, self.reason)


class AccountAccessPolicy:
    """Decides whether a channel event is in scope."""

    def __init__(
        self,
        *,
        allowed_groups: list[str] | tuple[str, ...] = (),
        allowed_users: list[str] | tuple[str, ...] = (),
        allow_all_groups: bool = False,
        allow_all_users: bool = False,
    ) -> None:
        self.allowed_groups = _normalize(allowed_groups)
        self.allowed_users = _normalize(allowed_users)
        self.allow_all_groups = bool(allow_all_groups)
        self.allow_all_users = bool(allow_all_users)

    @property
    def has_any_allowlist(self) -> bool:
        return bool(
            self.allowed_groups
            or self.allowed_users
            or self.allow_all_groups
            or self.allow_all_users
        )

    def decide(self, event: MessageEvent) -> AccessDecision:
        if not self.has_any_allowlist:
            return AccessDecision(False, DECISION_EMPTY_ALLOWLIST)

        if event.is_group:
            if not self.allow_all_groups and event.raw_group_id not in self.allowed_groups:
                return AccessDecision(False, DECISION_GROUP_MISSING, event.raw_group_id)
            if self.allow_all_users or event.raw_user_id in self.allowed_users:
                return AccessDecision(True, DECISION_ALLOWED)
            # A whitelisted group still only admits listed people.
            if not self.allowed_users:
                return AccessDecision(True, DECISION_ALLOWED)
            return AccessDecision(False, DECISION_USER_MISSING, event.raw_user_id)

        if self.allow_all_users or event.raw_user_id in self.allowed_users:
            return AccessDecision(True, DECISION_ALLOWED)
        return AccessDecision(False, DECISION_USER_MISSING, event.raw_user_id)

    def is_allowed(self, event: MessageEvent) -> bool:
        return self.decide(event).allowed


def _normalize(values: list[str] | tuple[str, ...]) -> frozenset[str]:
    if isinstance(values, str):
        values = [values]
    return frozenset(str(value).strip() for value in values if str(value).strip())
