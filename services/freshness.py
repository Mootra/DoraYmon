"""Freshness of a fact, and how stale material is presented.

The plan's P3 names a staleness check. ``SourceRef.is_stale`` existed and was
tested, but nothing in the application ever called it, so a menu price taken from a
year-old page was rendered with exactly the same confidence as one fetched this
morning. That is the failure this module exists to prevent.

Two principles, both taken from the plan:

- A stale record is **not deleted**. The price was real when it was recorded, and
  dropping it would turn a possibly-useful answer into no answer.
- A stale record is **never presented as current**. The age is stated, with the
  date, so the reader can judge it. "¥18（2025-03-01 的资料，可能已变）" tells the
  truth; a bare "¥18" does not.

Age limits are per fact type, because they go out of date at different rates: a
shop's location is stable for a long time, a menu price is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from services.recommendation_types import Shop, SourceRef, SourceKind

# How long each kind of fact may be presented without an age warning.
DEFAULT_MAX_AGE_DAYS = 180

MAX_AGE_DAYS_BY_KIND: dict[SourceKind, int] = {
    # A location or a mapped place barely changes.
    SourceKind.MAP: 365,
    # A menu price changes without notice.
    SourceKind.MENU: 90,
    # A page is a page; treat it like a menu.
    SourceKind.WEB: 90,
    # Opening hours and similar merchant details drift slowly.
    SourceKind.MERCHANT: 180,
    # A review is a dated opinion; it does not expire, but it is also not a fact
    # about today, so it ages at the same rate as a page.
    SourceKind.REVIEW: 90,
    # An operator's own upload is refreshed by hand, so allow longer.
    SourceKind.UPLOAD: 180,
}


def max_age_days_for(kind: SourceKind | str) -> int:
    """The age limit for a source kind, defaulting rather than failing."""
    if isinstance(kind, SourceKind):
        return MAX_AGE_DAYS_BY_KIND.get(kind, DEFAULT_MAX_AGE_DAYS)
    try:
        return MAX_AGE_DAYS_BY_KIND.get(SourceKind(str(kind or "").strip().lower()), DEFAULT_MAX_AGE_DAYS)
    except ValueError:
        return DEFAULT_MAX_AGE_DAYS


@dataclass(frozen=True)
class Freshness:
    """What is known about how old a fact is."""

    source: SourceRef
    age_days: int | None
    limit_days: int
    stale: bool

    @property
    def fetched_on(self) -> str:
        return self.source.fetched_at.strftime("%Y-%m-%d")

    def describe(self) -> str:
        """A short suffix for a rendered fact, or "" when it is current."""
        if not self.stale:
            return ""
        if self.age_days is None:
            return f"（{self.fetched_on} 的资料，可能已变）"
        return f"（{self.fetched_on} 的资料，已 {self.age_days} 天，可能已变）"

    def as_note(self) -> str:
        """A full sentence for the notes section."""
        if not self.stale:
            return ""
        age = f"{self.age_days} 天前" if self.age_days is not None else "较早"
        return (
            f"以下资料是 {self.fetched_on} 取得的（{age}），"
            f"超过 {self.limit_days} 天的保鲜期，价格和菜单可能已经变了。"
        )


def assess(source: SourceRef | None, *, now: datetime | None = None) -> Freshness | None:
    """Judge one source. None means there is nothing to judge."""
    if source is None:
        return None
    moment = now or datetime.now(timezone.utc)
    limit = max_age_days_for(source.kind)
    fetched = source.fetched_at
    if fetched.tzinfo is None:
        # A naive timestamp is compared as UTC rather than guessed at locally.
        fetched = fetched.replace(tzinfo=timezone.utc)
    age_days = max(0, (moment - fetched).days)
    return Freshness(
        source=source,
        age_days=age_days,
        limit_days=limit,
        stale=age_days > limit,
    )


def assess_item(item: Any, *, now: datetime | None = None) -> Freshness | None:
    """Judge the material behind one meal item, using the oldest concern.

    The price is what a user acts on, so when any part of an item is stale the
    item is stale; reporting the newest part would hide the problem.
    """
    if item is None:
        return None
    candidates: list[Freshness] = []
    for source in _item_sources(item):
        judged = assess(source, now=now)
        if judged is not None and judged.stale:
            candidates.append(judged)
    if not candidates:
        return assess(getattr(item, "source", None), now=now)
    # The oldest stale source is the most conservative statement.
    return max(candidates, key=lambda freshness: freshness.age_days or 0)


def _item_sources(item: Any) -> list[SourceRef]:
    sources: list[SourceRef] = []
    primary = getattr(item, "source", None)
    if isinstance(primary, SourceRef):
        sources.append(primary)
    for price in getattr(item, "prices", ()) or ():
        source = getattr(price, "source", None)
        if isinstance(source, SourceRef):
            sources.append(source)
    return sources


def assess_shop(shop: Shop | None, *, now: datetime | None = None) -> Freshness | None:
    """Judge a shop's own material. Its location rarely goes stale."""
    if shop is None:
        return None
    return assess(getattr(shop, "source", None), now=now)


def stale_note(*freshnesses: Freshness | None) -> str:
    """One sentence for the notes section, or "" when everything is current."""
    stale = [freshness for freshness in freshnesses if freshness is not None and freshness.stale]
    if not stale:
        return ""
    worst = max(stale, key=lambda freshness: freshness.age_days or 0)
    return worst.as_note()
