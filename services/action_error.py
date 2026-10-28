"""Protocol-neutral outbound action failure.

A send that the platform refused is a different event from a send that never
reached the network, and both are different from a send whose outcome is
unknown. Recording the refusal matters for diagnosis, but the *reason* is
platform-specific: OneBot answers with ``retcode``, Feishu with ``code``.

The session therefore only ever sees this shape. ``label`` names the protocol so
a log line is readable, and ``status`` exists so ``classify_send_error`` can
report a real HTTP status when the failure happened at that layer.
"""

from __future__ import annotations

from typing import Any


class ActionError(Exception):
    """An endpoint refused one outbound action."""

    def __init__(
        self,
        label: str,
        code: Any = None,
        message: Any = "",
        status: int | None = None,
    ) -> None:
        super().__init__(f"{label} error {code}")
        self.label = str(label)
        self.code = code
        self.action_message = str(message or "")
        if status is not None:
            self.status = int(status)
