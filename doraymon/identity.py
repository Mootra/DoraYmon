"""Platform and bot-account identity namespace shared by every entry point.

An official QQ Bot openid, a real QQ number, and a Feishu open_id are three
different identity systems. They must never be treated as the same person,
group, or session, even when two strings happen to be equal. This module makes
that separation explicit instead of relying on every caller to remember it.

Storage keeps its historical ``(scope_type, scope_openid, user_openid)`` tuple.
Identity is expressed by *prefixing* the openid fields with
``"<platform>:<bot_account>:"``:

- Legacy rows written before namespacing keep their bare values and stay
  readable only through the bare (un-namespaced) accessors. They are not
  silently reinterpreted as belonging to a platform.
- New rows always carry a namespace, so two platforms with the same numeric id
  can never share a session.

This module deliberately imports nothing from the rest of the bot so that the
context layer can depend on it without a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass

PLATFORM_QQ_BOT = "qqbot"
PLATFORM_QQ_ACCOUNT = "qqaccount"
PLATFORM_FEISHU = "feishu"
KNOWN_PLATFORMS = (PLATFORM_QQ_BOT, PLATFORM_QQ_ACCOUNT, PLATFORM_FEISHU)

SCOPE_PRIVATE = "private"
SCOPE_GROUP = "group"

_SEPARATOR = ":"

# A session key is the storage-level identity tuple:
# (scope_type, scope_openid, user_openid). Both openid fields already carry the
# platform namespace, so the tuple alone is a complete address.
SessionKey = tuple[str, str, str]


@dataclass(frozen=True)
class IdentityNamespace:
    """One platform plus one bot account running on it."""

    platform: str = PLATFORM_QQ_BOT
    bot_account: str = ""

    def __post_init__(self) -> None:
        platform = str(self.platform or "").strip().lower()
        if not platform:
            raise ValueError("platform must not be empty")
        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "bot_account", str(self.bot_account or "").strip())

    @property
    def prefix(self) -> str:
        return f"{self.platform}{_SEPARATOR}{self.bot_account}{_SEPARATOR}"

    def qualify(self, raw_id: str) -> str:
        """Return the namespaced form of one raw platform identifier."""
        normalized = str(raw_id or "").strip()
        if not normalized:
            raise ValueError("raw_id must not be empty")
        if is_namespaced(normalized):
            return normalized
        return f"{self.prefix}{normalized}"

    def matches(self, qualified_id: str) -> bool:
        return str(qualified_id or "").startswith(self.prefix)

    def session_key(
        self,
        raw_user_id: str,
        raw_group_id: str = "",
    ) -> SessionKey | None:
        """Build the storage session key for one message, or None if unusable.

        Private scope isolates by user; group scope isolates by ``group`` plus
        the speaking user, so one member's questions never mix with another
        member's view of the same group.
        """
        user = str(raw_user_id or "").strip()
        if not user:
            return None
        group = str(raw_group_id or "").strip()
        qualified_user = self.qualify(user)
        if group:
            return (SCOPE_GROUP, self.qualify(group), qualified_user)
        return (SCOPE_PRIVATE, qualified_user, qualified_user)


def namespace_for(
    platform: str = PLATFORM_QQ_BOT,
    bot_account: str = "",
) -> IdentityNamespace:
    return IdentityNamespace(platform=platform, bot_account=bot_account)


def is_namespaced(identifier: str) -> bool:
    """True when a stored identifier already carries a platform namespace."""
    parts = str(identifier or "").split(_SEPARATOR, 2)
    return len(parts) == 3 and parts[0].lower() in KNOWN_PLATFORMS


def split_namespace(identifier: str) -> tuple[str, str, str]:
    """Split a namespaced identifier into ``(platform, bot_account, raw_id)``."""
    normalized = str(identifier or "")
    if not is_namespaced(normalized):
        return ("", "", normalized)
    platform, bot_account, raw_id = normalized.split(_SEPARATOR, 2)
    return (platform.lower(), bot_account, raw_id)
