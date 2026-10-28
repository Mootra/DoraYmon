from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Change = Literal["none", "another", "closer", "map", "cancel"]
OutcomeKind = Literal[
    "places", "map", "empty", "need_location", "need_search", "unavailable", "cancelled"
]
SourceName = Literal["catalog", "meituan", "xiaohongshu", "dianping"]


def utc_now() -> datetime:
    return datetime.now(UTC)


class MenuItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    name: str = Field(min_length=1, max_length=100)
    kind: Literal["dish", "package"] = "dish"
    specification: str = Field(default="", max_length=200)
    preparation: str = Field(default="", max_length=200)
    flavor: str = Field(default="", max_length=200)
    portion: str = Field(default="", max_length=200)
    price_yuan: float | None = Field(default=None, ge=0, le=100000)
    service_mode: Literal["dine_in", "delivery", "unknown"] = "unknown"


class Evidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: SourceName
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(default="", max_length=2400)
    url: str = Field(default="", max_length=1500)
    observed_at: AwareDatetime
    published_at: AwareDatetime | None = None
    branch_confirmed: bool = False
    kind: Literal["menu", "listing", "experience"] = "experience"
    source_id: str = Field(default="", max_length=120)
    items: tuple[MenuItem, ...] = Field(default=(), max_length=20)
    needs_review: bool = False

    @model_validator(mode="after")
    def _menu_items(self):
        if self.items and self.kind != "menu":
            raise ValueError("Structured menu items require menu evidence")
        if not self.text.strip() and not self.items:
            raise ValueError("Evidence requires source text or structured menu items")
        return self


class MenuEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    amap_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(default="", max_length=2400)
    source_url: str = Field(default="", max_length=1500)
    observed_at: AwareDatetime
    published_at: AwareDatetime | None = None
    source: SourceName = "catalog"
    source_id: str = Field(default="", max_length=120)
    items: tuple[MenuItem, ...] = Field(default=(), max_length=20)


class MenuCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[MenuEntry] = Field(max_length=500)


class SourceCoverage(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: SourceName | Literal["library"]
    status: Literal["used", "partial", "not_configured", "unavailable", "no_match"]
    reason: str = ""


class Place(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    address: str = ""
    distance: int | None = Field(default=None, ge=0)
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)
    city: str = ""
    category: str = ""
    tags: tuple[str, ...] = ()
    rating: str = ""
    average_cost_yuan: float | None = Field(default=None, ge=0)


class MealCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    place: Place
    evidence: tuple[Evidence, ...] = Field(default=(), max_length=8)


class MealReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal["doraymon.meal.v1"] = "doraymon.meal.v1"
    created_at: AwareDatetime = Field(default_factory=utc_now)
    candidates: tuple[MealCandidate, ...] = Field(max_length=5)
    coverage: tuple[SourceCoverage, ...] = ()


class MealRequest(BaseModel):
    """One tool call as filled in by the MaiBot planner."""

    model_config = ConfigDict(frozen=True)

    location: str = Field(default="", max_length=60)
    budget_yuan: float | None = Field(default=None, gt=0, le=10000)
    change: Change = "none"
    count: int = Field(default=1, ge=1, le=5)
    requirements: str | None = Field(default=None, max_length=200)


class MealState(BaseModel):
    """The current meal decision of one chat member; replaced, never mutated."""

    model_config = ConfigDict(frozen=True)

    location: str = Field(min_length=1)
    radius: int = Field(ge=200)
    budget_yuan: float | None = Field(default=None, gt=0)
    excluded: frozenset[str] = frozenset()
    selected: tuple[Place, ...] = ()
    requirements: str = ""
    report: MealReport | None = None


class MealOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: OutcomeKind
    state: MealState | None
    places: tuple[Place, ...] = ()
    detail: str = ""
