"""Select a grounded nearby meal from a curated catalogue.

This is the layer the plan asks for in P2a/P2c: given a start point and a user
request, pick *one* real shop plus a specific item, with the evidence behind it,
and say plainly what is still unknown. It never invents a price, a portion or a
shop.

It deliberately does not replace the existing rule-based recommender, which
answers from a sample dish-name catalogue and cannot support a claim about a
real nearby place. Both can coexist until the curated catalogue exists.

Honesty rules enforced here:

- A candidate without any menu price cannot be called affordable. If the user
  gave a budget and nothing has a menu price, the result reports that the budget
  could not be checked instead of guessing.
- An item whose evidence is incomplete is still recommendable, but the gaps are
  returned so the reply can state them; the plan forbids presenting a guess as a
  fact.
- A city-level start point is not a nearby search, so it is refused rather than
  silently treated as an area.

The catalogue is an in-memory collection, so the whole selection is testable
offline. Storage and imports arrive in P2a.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from services.freshness import assess, assess_item, assess_shop, stale_note
from services.recommendation_types import (
    MealItem,
    MealRecommendation,
    PriceRecord,
    Shop,
    SourceKind,
    SourceRef,
    StartPoint,
)

# Which start precisions may be used for a nearby search.
NEARBY_PRECISIONS = ("landmark", "coordinate")


@dataclass(frozen=True)
class MealRequest:
    """What the user asked for this round."""

    budget_cny: float | None = None
    party_size: int | None = None
    suitable_meal: str = ""
    flavours: tuple[str, ...] = ()
    max_distance_meters: int | None = None
    excluded_shop_ids: tuple[str, ...] = ()
    wants_complete_evidence: bool = False

    def __post_init__(self) -> None:
        if self.budget_cny is not None and float(self.budget_cny) <= 0:
            raise ValueError("budget must be positive")
        if self.party_size is not None and int(self.party_size) < 1:
            raise ValueError("party_size must be at least 1")


@dataclass(frozen=True)
class MealCandidate:
    """One ranked shop/item pair with the reasons it ranked there."""

    shop: Shop
    item: MealItem
    reason: str
    rank_score: int
    price_note: str = ""
    budget_checked: bool = False

    @property
    def missing_evidence(self) -> tuple[str, ...]:
        return self.item.missing_evidence()


@dataclass(frozen=True)
class MealSelection:
    """The outcome of one selection, including what could not be established."""

    recommendation: MealRecommendation | None
    alternatives: tuple[MealCandidate, ...] = ()
    missing: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    refused_reason: str = ""

    @property
    def is_usable(self) -> bool:
        return self.recommendation is not None


class NoNearbyStart(Exception):
    """The start point is too coarse to search nearby."""


class MealCatalog:
    """An in-memory set of curated shops and items."""

    def __init__(self, shops: tuple[Shop, ...] = (), items: tuple[MealItem, ...] = ()) -> None:
        self._shops: dict[str, Shop] = {shop.shop_id: shop for shop in shops}
        self._items: dict[str, MealItem] = {item.item_id: item for item in items}
        for item in self._items.values():
            if item.shop_id not in self._shops:
                raise ValueError(f"item {item.item_id} references an unknown shop")

    @property
    def shop_count(self) -> int:
        return len(self._shops)

    @property
    def item_count(self) -> int:
        return len(self._items)

    def shops(self) -> tuple[Shop, ...]:
        return tuple(self._shops.values())

    def items(self) -> tuple[MealItem, ...]:
        return tuple(self._items.values())

    def shop_by_id(self, shop_id: str) -> Shop | None:
        return self._shops.get(str(shop_id or "").strip())

    def items_for_shop(self, shop_id: str) -> tuple[MealItem, ...]:
        normalized = str(shop_id or "").strip()
        return tuple(item for item in self._items.values() if item.shop_id == normalized)

    def add_shop(self, shop: Shop) -> None:
        self._shops[shop.shop_id] = shop

    def add_item(self, item: MealItem) -> None:
        if item.shop_id not in self._shops:
            raise ValueError(f"item {item.item_id} references an unknown shop")
        self._items[item.item_id] = item

    def remove_shop(self, shop_id: str) -> bool:
        """Remove a shop and any items that belonged to it.

        Leaving orphaned items would break the invariant that every item has a
        real place, so they are removed together.
        """
        normalized = str(shop_id or "").strip()
        if normalized not in self._shops:
            return False
        self._shops.pop(normalized, None)
        for item_id in [
            item.item_id for item in self._items.values() if item.shop_id == normalized
        ]:
            self._items.pop(item_id, None)
        return True

    def remove_item(self, item_id: str) -> bool:
        return self._items.pop(str(item_id or "").strip(), None) is not None

    def remove_shops(self, shop_ids) -> int:
        return sum(1 for shop_id in set(shop_ids) if self.remove_shop(shop_id))

    def remove_items(self, item_ids) -> int:
        return sum(1 for item_id in set(item_ids) if self.remove_item(item_id))


def select_meal(
    catalog: MealCatalog,
    start: StartPoint,
    request: MealRequest | None = None,
) -> MealSelection:
    """Pick the best grounded option, or explain why nothing qualifies."""
    request = request or MealRequest()

    if not start.is_usable_for_nearby:
        return MealSelection(
            recommendation=None,
            refused_reason=(
                f"“{start.text}”范围太大，不足以推荐附近。"
                "请给一个学校、商圈、地标或具体位置。"
            ),
        )

    if catalog.item_count == 0:
        return MealSelection(
            recommendation=None,
            refused_reason="本地餐点资料还是空的，还不能给出有依据的附近推荐。",
        )

    candidates = [
        candidate
        for candidate in _build_candidates(catalog, request)
        if candidate is not None
    ]
    if not candidates:
        return MealSelection(
            recommendation=None,
            notes=("当前资料里没有符合这些条件的店铺或餐点。",),
            refused_reason="现有资料里没有能对应上的店铺和餐点，不编造推荐。",
        )

    notes: list[str] = []
    if request.budget_cny is not None:
        candidates, budget_notes = _apply_budget(candidates, request.budget_cny)
        notes.extend(budget_notes)
        if not candidates:
            return MealSelection(
                recommendation=None,
                notes=tuple(notes),
                refused_reason=notes[0] if notes else "现有资料里没有能确认在预算内的餐点。",
            )

    candidates.sort(key=lambda candidate: (-candidate.rank_score, candidate.shop.shop_id))
    best = candidates[0]
    if best.price_note and not any("无法确认" in note for note in notes):
        notes.append(best.price_note)

    recommendation = MealRecommendation(
        shop=best.shop,
        item=best.item,
        source=best.shop.source,
        reason=best.reason,
        alternatives=tuple(candidate.item for candidate in candidates[1:3]),
        missing=best.item.missing_evidence(),
    )
    return MealSelection(
        recommendation=recommendation,
        alternatives=tuple(candidates[1:3]),
        missing=best.item.missing_evidence(),
        notes=tuple(notes),
    )


def _apply_budget(
    candidates: list[MealCandidate],
    budget: float,
) -> tuple[list[MealCandidate], list[str]]:
    """Prefer options whose menu price is known to fit the budget.

    A known menu price over budget is disqualifying, not merely a penalty: the
    user stated a constraint and we know it is violated. When nothing priced
    fits, unpriced options are still offered, but the reply must say that the
    budget could not be verified instead of implying it fits.
    """
    within = [candidate for candidate in candidates if candidate.budget_checked
              and "超过" not in candidate.reason]
    if within:
        return within, []

    over_budget = [
        candidate for candidate in candidates
        if candidate.budget_checked and "超过" in candidate.reason
    ]
    unpriced = [candidate for candidate in candidates if not candidate.budget_checked]
    if unpriced:
        return unpriced, [
            f"现有资料里没有能确认在{budget:g}元以内的菜单价，"
            "下面这些价格未知，预算是否够无法确认。"
        ]
    if over_budget:
        return [], [f"现有资料里的菜单价都超过{budget:g}元。"]
    return candidates, []


def _build_candidates(catalog: MealCatalog, request: MealRequest) -> list[MealCandidate | None]:
    excluded = {str(value).strip() for value in request.excluded_shop_ids}
    candidates: list[MealCandidate | None] = []
    for item in catalog.items():
        if item.shop_id in excluded:
            continue
        shop = catalog.shop_by_id(item.shop_id)
        if shop is None:
            continue
        if not _matches_request(shop, item, request):
            continue
        score, reason, price_note, budget_checked = _score(shop, item, request)
        candidates.append(
            MealCandidate(
                shop=shop,
                item=item,
                reason=reason,
                rank_score=score,
                price_note=price_note,
                budget_checked=budget_checked,
            )
        )
    return candidates


def _matches_request(shop: Shop, item: MealItem, request: MealRequest) -> bool:
    if request.suitable_meal and request.suitable_meal not in item.suitable_meals:
        return False
    if request.max_distance_meters is not None:
        if shop.distance_meters is None:
            # An unknown distance cannot be claimed to be near.
            return False
        if int(shop.distance_meters) > int(request.max_distance_meters):
            return False
    if request.flavours:
        described = " ".join(
            f"{note.description}{note.detail}" for note in item.flavours
        )
        if not any(flavour in described for flavour in request.flavours):
            return False
    return True


def _score(
    shop: Shop,
    item: MealItem,
    request: MealRequest,
) -> tuple[int, str, str, bool]:
    score = 0
    reasons: list[str] = []
    price_note = ""
    budget_checked = False

    if item.is_decision_ready():
        score += 6
        reasons.append("餐点、份量和口味都有来源")
    else:
        missing = "、".join(item.missing_evidence())
        score -= len(item.missing_evidence())
        reasons.append(f"还缺{missing}")

    menu_prices = item.menu_prices
    if request.budget_cny is not None:
        verdict = _budget_verdict(item, request.budget_cny)
        if verdict is True:
            score += 4
            budget_checked = True
            reasons.append(f"菜单价在{request.budget_cny:g}元以内")
        elif verdict is False:
            score -= 4
            budget_checked = True
            reasons.append(f"菜单价超过{request.budget_cny:g}元")
        elif not any("无法确认" in note for note in ()):
            # No menu price: say so rather than implying it fits the budget.
            price_note = "现有资料里没有这家店的菜单价，预算是否够无法确认，不替你下结论。"
    elif menu_prices:
        score += 1
        reasons.append(f"菜单价{menu_prices[0].describe()}")

    if shop.distance_meters is not None:
        score += max(0, 3 - min(3, shop.distance_meters // 500))
        reasons.append(f"直线距离约{shop.distance_meters}米")
    else:
        reasons.append("距离未知")

    if item.has_informative_flavour:
        score += 2
    if item.composition:
        score += 1

    return score, "。".join(reasons) + "。", price_note, budget_checked


def _budget_verdict(item: MealItem, budget: float) -> bool | None:
    """True/False when a menu price decides it, None when it cannot."""
    verdicts = [
        price.is_within_budget(budget)
        for price in item.menu_prices
    ]
    decided = [verdict for verdict in verdicts if verdict is not None]
    if not decided:
        return None
    return any(decided)


def render_selection(
    selection: MealSelection,
    start: StartPoint,
    *,
    now: datetime | None = None,
) -> str:
    """Render one selection without asserting anything that is not known.

    ``now`` exists so an age statement can be pinned in a test. Without it the
    wording depends on the calendar, and a test asserting "400 days" starts failing
    the next day for a reason that has nothing to do with the code.
    """
    if not selection.is_usable:
        parts = [selection.refused_reason or "这次没有合适的选择。"]
        parts.extend(selection.notes)
        return "".join(parts)

    recommendation = selection.recommendation
    shop = recommendation.shop
    item = recommendation.item
    lines = [f"{shop.name}：{item.name}"]

    details: list[str] = []
    if shop.distance_meters is not None:
        details.append(f"直线距离约{shop.distance_meters}米")
    if shop.address:
        details.append(shop.address)
    if details:
        lines.append("（" + "，".join(details) + "）")

    if item.composition:
        lines.append("组成：" + "、".join(item.composition))
    if item.portion:
        lines.append(f"份量：{item.portion}")

    described = [note.description + (f"（{note.detail}）" if note.detail else "")
                 for note in item.flavours]
    if described:
        lines.append("口味：" + "；".join(described))

    if item.menu_prices:
        # The age travels with the price: a year-old figure presented bare reads
        # exactly like one fetched this morning.
        price_freshness = assess(item.menu_prices[0].source, now=now)
        suffix = price_freshness.describe() if price_freshness is not None else ""
        lines.append(
            "菜单价："
            + item.menu_prices[0].describe()
            + f"，来源 {item.menu_prices[0].source.kind.value}"
            + suffix
        )
    else:
        lines.append("菜单价：资料里还没有，不能确认多少钱")

    if selection.missing:
        lines.append("还缺：" + "、".join(selection.missing))
    note = stale_note(assess_item(item, now=now), assess_shop(shop, now=now))
    if note:
        lines.append(note)
    for extra in selection.notes:
        lines.append(extra)
    return "\n".join(lines)
