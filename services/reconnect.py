"""Growing, capped reconnect backoff, shared by every long-lived connection.

OneBot reconnects with a fresh socket and Feishu reconnects inside its SDK, but
"wait longer each time, up to a ceiling" is the same shape for both, and two
copies of it would be two things to get wrong independently.
"""

from __future__ import annotations

DEFAULT_RECONNECT_BASE_SECONDS = 1.0
DEFAULT_RECONNECT_MAX_SECONDS = 60.0
DEFAULT_BACKOFF_FACTOR = 2.0


def reconnect_delay(
    attempt: int,
    *,
    base_seconds: float = DEFAULT_RECONNECT_BASE_SECONDS,
    factor: float = DEFAULT_BACKOFF_FACTOR,
    max_seconds: float = DEFAULT_RECONNECT_MAX_SECONDS,
) -> float:
    """Growing, capped backoff for reconnect attempt ``attempt`` (0-based)."""
    normalized_attempt = max(0, int(attempt))
    delay = float(base_seconds) * (float(factor) ** normalized_attempt)
    return float(min(delay, float(max_seconds)))
