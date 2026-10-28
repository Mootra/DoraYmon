"""Shared recommendation and evidence contracts for nearby meal decisions.

The product requirement is narrow and strict: recommend a *real nearby place*
plus *specific items* with enough evidence about composition, portion, flavour
and price for someone to decide. That means the data layer must be able to say
"I do not know" out loud, and it must never let a missing fact be filled in by
a guess.

Design rules encoded here:

- Every fact carries a ``SourceRef``: where it came from, when it was obtained,
  and how it is scoped. A fact without a source cannot be constructed.
- ``None`` means unknown. It is never rendered as ``0``, "free", or a default.
- A map POI's per-capita figure is not a menu price, and a POI alone does not
  prove what a shop sells.
- Recommended is not the same as eaten. Feedback is recorded separately.

This module is deliberately free of network, database and model access so the
whole contract can be exercised offline with synthetic data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


class SourceKind(str, Enum):
    """Where one fact came from."""

    MENU = "menu"
    MERCHANT = "merchant"
    REVIEW = "review"
    MAP = "map"
    WEB = "web"
    UPLOAD = "upload"
    USER = "user"
    UNKNOWN = "unknown"


class Visibility(str, Enum):
    PRIVATE = "private"
    GROUP = "group"
    PUBLIC = "public"


@dataclass(frozen=True)
class SourceRef:
    """Provenance for one fact or one record."""

    kind: SourceKind
    reference: str
    fetched_at: datetime
    visibility: Visibility = Visibility.PRIVATE
    batch_id: str = ""

    def __post_init__(self) -> None:
        if not str(self.reference or "").strip():
            raise ValueError("source reference must not be empty")
        if self.fetched_at is None:
            raise ValueError("source fetched_at must not be empty")

    @property
    def is_verifiable(self) -> bool:
        """A source the user could in principle check themselves."""
        return self.kind is not SourceKind.UNKNOWN and bool(self.reference.strip())

    def is_stale(self, max_age_days: int, now: datetime | None = None) -> bool:
        """True when this fact is older than the accepted freshness window.

        Menu coverage goes out of date; a stale record is not deleted, but it
        must not be presented as current either.
        """
        if max_age_days is None or int(max_age_days) <= 0:
            return False
        moment = now or _now()
        age = moment - self.fetched_at
        return age > timedelta(days=int(max_age_days))

    def describe(self) -> str:
        return f"{self.kind.value}:{self.reference}"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


# ---------------------------------------------------------------------------
# Places, items, evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PriceRecord:
    """A price or price range, always with its basis and source.

    ``amount_cny`` may stay ``None`` for a range whose bounds are unknown; the
    contract never converts unknown into zero.
    """

    basis: str  # "menu" | "per_person" | "estimate"
    source: SourceRef
    amount_cny: float | None = None
    min_cny: float | None = None
    max_cny: float | None = None

    VALID_BASES = ("menu", "per_person", "estimate")

    def __post_init__(self) -> None:
        if self.basis not in self.VALID_BASES:
            raise ValueError(f"price basis must be one of {self.VALID_BASES}")
        for name in ("amount_cny", "min_cny", "max_cny"):
            value = getattr(self, name)
            if value is not None and float(value) < 0:
                raise ValueError(f"{name} must not be negative")
        if self.amount_cny is None and self.min_cny is None and self.max_cny is None:
            raise ValueError("a price record must carry at least one known amount")
        if (
            self.min_cny is not None
            and self.max_cny is not None
            and float(self.min_cny) > float(self.max_cny)
        ):
            raise ValueError("price range minimum must not exceed maximum")

    @property
    def is_menu_price(self) -> bool:
        """True only for a menu price, which is what a budget check may use."""
        return self.basis == "menu"

    def is_within_budget(self, budget_cny: float | None) -> bool | None:
        """Return None when the price cannot decide the question."""
        if budget_cny is None:
            return None
        if not self.is_menu_price:
            return None
        if self.max_cny is not None:
            return float(self.max_cny) <= float(budget_cny)
        if self.amount_cny is not None:
            return float(self.amount_cny) <= float(budget_cny)
        return None

    def describe(self) -> str:
        if self.amount_cny is not None:
            return f"¥{self.amount_cny:g}（{self.basis}）"
        if self.min_cny is not None and self.max_cny is not None:
            return f"¥{self.min_cny:g}-{self.max_cny:g}（{self.basis}）"
        if self.min_cny is not None:
            return f"¥{self.min_cny:g} 起（{self.basis}）"
        return f"¥{self.max_cny:g} 以内（{self.basis}）"


@dataclass(frozen=True)
class FlavourNote:
    """A flavour description that is attributed rather than asserted."""

    description: str
    source: SourceRef
    detail: str = ""

    def __post_init__(self) -> None:
        if not str(self.description or "").strip():
            raise ValueError("flavour note must not be empty")

    @property
    def is_informative(self) -> bool:
        """A note that actually says something beyond generic praise."""
        text = f"{self.description}{self.detail}"
        return len(text.strip()) >= 8


@dataclass(frozen=True)
class Shop:
    """A real place. Coordinates and hours may be unknown."""

    shop_id: str
    name: str
    source: SourceRef
    address: str = ""
    map_url: str = ""
    latitude: float | None = None
    longitude: float | None = None
    # Straight-line distance as reported by the map source; not a walking route.
    distance_meters: int | None = None
    open_now: bool | None = None

    def __post_init__(self) -> None:
        if not str(self.shop_id or "").strip():
            raise ValueError("shop_id must not be empty")
        if not str(self.name or "").strip():
            raise ValueError("shop name must not be empty")
        if self.distance_meters is not None and int(self.distance_meters) < 0:
            raise ValueError("distance_meters must not be negative")

    @property
    def has_location(self) -> bool:
        return bool(self.address or self.map_url) or (
            self.latitude is not None and self.longitude is not None
        )

    @property
    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None


@dataclass(frozen=True)
class MealItem:
    """A specific orderable item: a dish, set meal or stall combination.

    Composition and portion are separate facts and may come from different
    records, so each carries its own source. Missing means unknown; the
    contract never lets a model invent a weight or a serving count.
    """

    item_id: str
    shop_id: str
    name: str
    source: SourceRef
    composition: tuple[str, ...] = ()
    composition_source: SourceRef | None = None
    portion: str = ""
    portion_source: SourceRef | None = None
    flavours: tuple[FlavourNote, ...] = ()
    prices: tuple[PriceRecord, ...] = ()
    suitable_meals: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.item_id or "").strip():
            raise ValueError("item_id must not be empty")
        if not str(self.shop_id or "").strip():
            raise ValueError("item shop_id must not be empty")
        if not str(self.name or "").strip():
            raise ValueError("item name must not be empty")
        if self.composition and self.composition_source is None:
            raise ValueError("composition requires a composition source")
        if self.portion and self.portion_source is None:
            raise ValueError("portion requires a portion source")

    @property
    def kind(self) -> str:
        """Distinguish a single dish from a set meal or stall combination."""
        if self.composition and len(self.composition) > 1:
            return "combo"
        return "single"

    @property
    def has_composition(self) -> bool:
        return bool(self.composition)

    @property
    def has_portion(self) -> bool:
        return bool(str(self.portion or "").strip())

    @property
    def has_informative_flavour(self) -> bool:
        return any(note.is_informative for note in self.flavours)

    @property
    def menu_prices(self) -> tuple[PriceRecord, ...]:
        return tuple(price for price in self.prices if price.is_menu_price)

    def missing_evidence(self) -> tuple[str, ...]:
        """What a user would still have to guess about this item."""
        gaps: list[str] = []
        if not self.has_composition:
            gaps.append("餐点组成")
        if not self.has_portion:
            gaps.append("份量或规格")
        if not self.has_informative_flavour:
            gaps.append("口味描述")
        if not self.menu_prices:
            gaps.append("菜单价格")
        return tuple(gaps)

    def is_decision_ready(self) -> bool:
        """Enough evidence to help choose, per the first-version standard."""
        return not self.missing_evidence()


# ---------------------------------------------------------------------------
# Recommendation result and feedback
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MealRecommendation:
    """One first-choice suggestion, plus what it could not establish."""

    shop: Shop
    item: MealItem | None
    source: SourceRef
    reason: str = ""
    alternatives: tuple[MealItem, ...] = ()
    missing: tuple[str, ...] = ()
    generated_at: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        if self.item is not None and self.item.shop_id != self.shop.shop_id:
            raise ValueError("recommended item must belong to the recommended shop")

    @property
    def is_grounded(self) -> bool:
        """True only when a specific item backs a real place."""
        if self.item is None:
            return False
        return self.source.is_verifiable and self.item.source.is_verifiable

    @property
    def is_decision_ready(self) -> bool:
        return self.item is not None and self.item.is_decision_ready()

    def describe_missing(self) -> str:
        if not self.missing:
            return ""
        return "、".join(self.missing)


class FeedbackKind(str, Enum):
    RECOMMENDED = "recommended"
    REJECTED = "rejected"
    SAVED = "saved"
    EATEN = "eaten"


@dataclass(frozen=True)
class RecommendationFeedback:
    """Recommendation, rejection, saving and eating are separate facts."""

    kind: FeedbackKind
    shop_id: str
    item_id: str = ""
    reason: str = ""
    recorded_at: datetime = field(default_factory=_now)

    @property
    def means_eaten(self) -> bool:
        return self.kind is FeedbackKind.EATEN


# ---------------------------------------------------------------------------
# Current meal decision state
# ---------------------------------------------------------------------------


class StartPrecision(str, Enum):
    LANDMARK = "landmark"  # school, business district, landmark
    CITY = "city"  # too coarse to call anything "nearby"
    COORDINATE = "coordinate"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class StartPoint:
    """Where this decision starts from."""

    text: str
    precision: StartPrecision = StartPrecision.UNKNOWN
    latitude: float | None = None
    longitude: float | None = None

    def __post_init__(self) -> None:
        if not str(self.text or "").strip():
            raise ValueError("start point text must not be empty")

    @property
    def is_usable_for_nearby(self) -> bool:
        """A city name is not a nearby search; it must be narrowed first."""
        return self.precision in (StartPrecision.LANDMARK, StartPrecision.COORDINATE)

    @property
    def has_coordinates(self) -> bool:
        """A coordinate lookup needs both numbers, not just a claimed precision."""
        return self.latitude is not None and self.longitude is not None

    def same_place_as(self, other: StartPoint) -> bool:
        """Identity of a start point, used to detect a real location change."""
        if self.precision is not other.precision:
            return False
        if self.precision is StartPrecision.COORDINATE:
            return (self.latitude, self.longitude) == (other.latitude, other.longitude)
        return self.text.strip() == other.text.strip()


@dataclass(frozen=True)
class MealState:
    """Bounded per-conversation state for one ongoing meal decision.

    Only the fields a follow-up may change are stored. A follow-up updates just
    the relevant field; changing the start point invalidates the candidate set
    because results from another area must not be reused.
    """

    session_key: tuple[str, str, str]
    start: StartPoint | None = None
    radius_meters: int | None = None
    budget_cny: float | None = None
    party_size: int | None = None
    current_item_id: str = ""
    candidates: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()
    exclusion_reasons: tuple[tuple[str, str], ...] = ()
    updated_at: datetime = field(default_factory=_now)
    valid_until: datetime | None = None

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.valid_until is None:
            return False
        moment = now or _now()
        return moment >= self.valid_until

    # -- partial updates: each returns a new state, never mutating in place --

    def with_start(self, start: StartPoint) -> "MealState":
        """Changing the start point drops candidates found for the old area."""
        if self.start is not None and self.start.same_place_as(start):
            return self._replace(start=start)
        return self._replace(
            start=start,
            candidates=(),
            current_item_id="",
            excluded=(),
            exclusion_reasons=(),
        )

    def with_budget(self, budget_cny: float | None) -> "MealState":
        return self._replace(budget_cny=budget_cny)

    def with_party_size(self, party_size: int | None) -> "MealState":
        if party_size is not None and int(party_size) < 1:
            raise ValueError("party_size must be at least 1")
        return self._replace(party_size=party_size)

    def with_radius(self, radius_meters: int | None) -> "MealState":
        if radius_meters is not None and int(radius_meters) < 1:
            raise ValueError("radius_meters must be positive")
        return self._replace(radius_meters=radius_meters)

    def with_candidates(self, candidates: tuple[str, ...]) -> "MealState":
        return self._replace(candidates=tuple(candidates))

    def with_current(self, item_id: str) -> "MealState":
        return self._replace(current_item_id=str(item_id or ""))

    def exclude(self, shop_id: str, reason: str = "") -> "MealState":
        """Exclude a shop for this decision only; not a permanent dislike."""
        normalized = str(shop_id or "").strip()
        if not normalized or normalized in self.excluded:
            return self
        reasons = self.exclusion_reasons + ((normalized, str(reason or "").strip()),)
        return self._replace(excluded=self.excluded + (normalized,), exclusion_reasons=reasons)

    def remaining_candidates(self) -> tuple[str, ...]:
        return tuple(c for c in self.candidates if c not in self.excluded)

    def is_excluded(self, shop_id: str) -> bool:
        return shop_id in self.excluded

    def reason_for(self, shop_id: str) -> str:
        for candidate, reason in self.exclusion_reasons:
            if candidate == shop_id:
                return reason
        return ""

    def _replace(self, **changes) -> "MealState":
        payload = {
            "session_key": self.session_key,
            "start": self.start,
            "radius_meters": self.radius_meters,
            "budget_cny": self.budget_cny,
            "party_size": self.party_size,
            "current_item_id": self.current_item_id,
            "candidates": self.candidates,
            "excluded": self.excluded,
            "exclusion_reasons": self.exclusion_reasons,
            "valid_until": self.valid_until,
            "updated_at": _now(),
        }
        payload.update(changes)
        return MealState(**payload)


# ---------------------------------------------------------------------------
# Bounded, in-memory state cache
# ---------------------------------------------------------------------------


class MealStateStore:
    """Bounded per-session meal state.

    Matching the plan, the first version is an in-memory cache: it is dropped on
    restart and expires on its own. It is not long-term memory, and nothing here
    is written to chat history.
    """

    def __init__(self, ttl_minutes: int = 60, max_sessions: int = 200) -> None:
        self.ttl_minutes = int(ttl_minutes)
        self.max_sessions = max(1, int(max_sessions))
        self._states: dict[tuple[str, str, str], MealState] = {}

    def _expiry(self) -> datetime | None:
        if self.ttl_minutes <= 0:
            return None
        return _now() + timedelta(minutes=self.ttl_minutes)

    def get(self, session_key: tuple[str, str, str]) -> MealState | None:
        state = self._states.get(session_key)
        if state is None:
            return None
        if state.is_expired():
            self._states.pop(session_key, None)
            return None
        return state

    def start(self, session_key: tuple[str, str, str], start: StartPoint) -> MealState:
        state = MealState(session_key=session_key, start=start, valid_until=self._expiry())
        self._store(state)
        return state

    def update(self, state: MealState) -> MealState:
        refreshed = state._replace(valid_until=self._expiry())
        self._store(refreshed)
        return refreshed

    def reset(self, session_key: tuple[str, str, str]) -> bool:
        return self._states.pop(session_key, None) is not None

    def forget(self, session_key: tuple[str, str, str]) -> None:
        self._states.pop(session_key, None)

    def prune(self) -> int:
        expired = [key for key, state in self._states.items() if state.is_expired()]
        for key in expired:
            self._states.pop(key, None)
        return len(expired)

    def clear(self) -> None:
        self._states.clear()

    def __len__(self) -> int:
        return len(self._states)

    def _store(self, state: MealState) -> None:
        if len(self._states) >= self.max_sessions and state.session_key not in self._states:
            # Drop the least recently updated session to stay bounded.
            oldest = min(self._states, key=lambda key: self._states[key].updated_at)
            self._states.pop(oldest, None)
        self._states[state.session_key] = state
