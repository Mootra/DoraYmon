"""Who may operate the controls, matched the same way on both entries.

The official entry and the account entry identify the same person differently: the
official one reports an openid, the account entry reports a QQ number, and the
stored identity is namespaced as ``<platform>:<bot>:<user>``. An operator writing
``BOT_ADMIN_OPENIDS`` cannot reasonably be expected to know which form is in play,
and getting it wrong is not a cosmetic problem: a list that matches nobody means the
silence switch cannot be operated, which is the one control needed when a live
account misbehaves.

So an entry counts as an administrator when it matches any form of the person:
the bare id, the namespaced session identity, or the conversation key. Whitespace
and surrounding ``<>`` are ignored, because both are easy to paste in by accident.
"""

from __future__ import annotations

from typing import Any

from services.session_history import get_session_key


def _normalize(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1].strip()
    return text


def admin_identifiers(context: Any) -> tuple[str, ...]:
    """Every form of this speaker an allowlist might legitimately contain."""
    candidates = [
        _normalize(getattr(context, "user_openid", "")),
        _normalize(getattr(context, "group_openid", "")),
    ]
    # An unusable identity must not raise here: this runs on every admin command,
    # and a crash would replace a clear refusal with an error reply. A context with
    # no platform simply has no namespaced form to match.
    try:
        session_key = get_session_key(context)
    except Exception:
        session_key = None
    if session_key:
        candidates.extend(_normalize(part) for part in session_key)
    return tuple(dict.fromkeys(item for item in candidates if item))


def is_admin(context: Any) -> bool:
    """Whether this speaker is on the administrator list.

    An empty list means nobody, deliberately: controls that guard a live account
    must not be open to everyone by default.
    """
    allowed = {
        _normalize(item)
        for item in (getattr(getattr(context, "settings", None), "admin_openids", []) or [])
    }
    allowed.discard("")
    if not allowed:
        return False
    return any(identifier in allowed for identifier in admin_identifiers(context))


def refusal_message() -> str:
    return "你没有权限使用管理员命令。"
