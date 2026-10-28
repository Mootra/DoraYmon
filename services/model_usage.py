"""Daily model-usage accounting and a hard cap.

G3 requires a daily cost ceiling before any wider trial, and section 9 lists
request usage on the admin surface. Two requests keep this honest:

- The cap is expressed in **tokens**, not money. Token counts are reported by the
  API, so they are measurable; a currency figure would need a price list that
  changes without notice, and a confidently wrong cost figure is worse for an
  operator than no figure. When a price is configured it is used only to display
  an estimate, and it is labelled as an estimate.
- Usage is counted per calendar day. The day boundary uses local time because an
  operator watching a budget thinks in their own day, and the boundary is
  recorded rather than implied.

The refusal is explicit. When the cap is reached the bot says so instead of
silently going quiet, because a bot that answers nothing and explains nothing is
indistinguishable from a broken one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import threading
from typing import Any

# Reported by the API; 0 when the provider did not include it.
DEFAULT_DAILY_TOKEN_CAP = 0
TOKENS_PER_MILLION = 1_000_000


@dataclass
class UsageRecord:
    """One model request's accounting."""

    at: datetime
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @property
    def measured(self) -> bool:
        """Whether the provider actually reported token counts."""
        return self.total_tokens > 0 or self.prompt_tokens > 0 or self.completion_tokens > 0


@dataclass
class CapDecision:
    """Whether another request may be made today."""

    allowed: bool
    reason: str = ""
    used_tokens: int = 0
    cap_tokens: int = 0

    def describe(self) -> str:
        if self.allowed:
            if self.cap_tokens <= 0:
                return f"今日已用 {self.used_tokens} tokens（未设置每日上限）"
            remaining = max(0, self.cap_tokens - self.used_tokens)
            return (
                f"今日已用 {self.used_tokens}/{self.cap_tokens} tokens，"
                f"剩余 {remaining}"
            )
        return (
            f"今日模型额度已用完（{self.used_tokens}/{self.cap_tokens} tokens），"
            "暂时不能生成新回答；明天自动恢复，或让管理员调高 BOT_DAILY_TOKEN_CAP。"
        )


@dataclass
class UsageLedger:
    """Process-wide per-day model usage."""

    daily_token_cap: int = DEFAULT_DAILY_TOKEN_CAP
    price_per_million_cny: float = 0.0
    _day: date | None = None
    _records: list[UsageRecord] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _refusals: int = 0

    def __post_init__(self) -> None:
        self.daily_token_cap = max(0, int(self.daily_token_cap or 0))
        self.price_per_million_cny = max(0.0, float(self.price_per_million_cny or 0.0))

    # -- day handling -------------------------------------------------------

    def _roll(self, now: datetime) -> None:
        today = now.date()
        if self._day != today:
            self._day = today
            self._records.clear()

    @property
    def day(self) -> date | None:
        return self._day

    # -- accounting ---------------------------------------------------------

    def check(self, *, now: datetime | None = None) -> CapDecision:
        moment = now or datetime.now()
        with self._lock:
            self._roll(moment)
            used = self.used_tokens
            cap = self.daily_token_cap
            allowed = cap <= 0 or used < cap
            if not allowed:
                self._refusals += 1
            return CapDecision(
                allowed=allowed,
                reason="" if allowed else "daily_cap_reached",
                used_tokens=used,
                cap_tokens=cap,
            )

    def record(
        self,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        model: str = "",
        now: datetime | None = None,
    ) -> UsageRecord:
        """Record one request. Missing totals are derived, never invented."""
        moment = now or datetime.now()
        prompt = max(0, int(prompt_tokens or 0))
        completion = max(0, int(completion_tokens or 0))
        total = max(0, int(total_tokens or 0)) or (prompt + completion)
        record = UsageRecord(
            at=moment,
            model=str(model or ""),
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
        )
        with self._lock:
            self._roll(moment)
            self._records.append(record)
        return record

    # -- reporting ----------------------------------------------------------

    @property
    def used_tokens(self) -> int:
        return sum(record.total_tokens for record in self._records)

    @property
    def request_count(self) -> int:
        return len(self._records)

    @property
    def refusals(self) -> int:
        return self._refusals

    @property
    def estimated_cny(self) -> float | None:
        """An estimate, or None when no price is configured.

        None rather than 0.0: "no price configured" and "this cost nothing" are
        different statements.
        """
        if self.price_per_million_cny <= 0:
            return None
        return self.used_tokens / TOKENS_PER_MILLION * self.price_per_million_cny

    def describe(self) -> str:
        if self.request_count == 0:
            base = "今日还没有模型请求记录"
        else:
            base = f"今日模型请求 {self.request_count} 次，共 {self.used_tokens} tokens"
        cap = self.daily_token_cap
        if cap > 0:
            base += f"（上限 {cap}）"
        estimate = self.estimated_cny
        if estimate is not None:
            # Labelled as an estimate because the price is configured, not asked for.
            base += f"；按配置单价估算约 ¥{estimate:.4f}（估算）"
        if self.refusals:
            base += f"；因额度用尽被拒 {self.refusals} 次"
        return base

    def reset(self) -> None:
        with self._lock:
            self._day = None
            self._records.clear()
            self._refusals = 0

    def token_usage_from(self, payload: dict[str, Any]) -> dict[str, int]:
        """Pull token counts out of an API response, defaulting to unknown."""
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if not isinstance(usage, dict):
            return {}

        def _int(value: Any) -> int:
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                return 0

        return {
            "prompt_tokens": _int(usage.get("prompt_tokens")),
            "completion_tokens": _int(usage.get("completion_tokens")),
            "total_tokens": _int(usage.get("total_tokens")),
        }


MODEL_USAGE = UsageLedger()


def configure_from_settings(settings: Any, ledger: UsageLedger | None = None) -> UsageLedger:
    """Apply configured limits to the ledger."""
    target = ledger or MODEL_USAGE
    target.daily_token_cap = max(
        0, int(getattr(settings, "daily_token_cap", DEFAULT_DAILY_TOKEN_CAP) or 0)
    )
    target.price_per_million_cny = max(
        0.0, float(getattr(settings, "model_price_per_million_cny", 0.0) or 0.0)
    )
    return target
