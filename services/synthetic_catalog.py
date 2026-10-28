"""Synthetic meal catalog for offline contract and replay tests.

Nothing here is a claim about a real shop. These records exist so the contract
machinery (evidence gaps, price basis, decision readiness) can be exercised
without a map, a search API or a real uploaded menu.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.recommendation_types import (
    FlavourNote,
    MealItem,
    PriceRecord,
    Shop,
    SourceKind,
    SourceRef,
)

_FETCHED_AT = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


def _source(kind: SourceKind, reference: str, *, age_days: int = 0) -> SourceRef:
    return SourceRef(
        kind=kind,
        reference=reference,
        fetched_at=_FETCHED_AT - timedelta(days=age_days),
    )


def synthetic_shops() -> tuple[Shop, ...]:
    """Three nearby places: one complete, one POI-only, one stale."""
    return (
        Shop(
            shop_id="shop-noodle",
            name="示例兰州拉面（合成）",
            source=_source(SourceKind.MAP, "synthetic-map/noodle"),
            address="示例路 1 号（合成）",
            map_url="https://example.invalid/shop-noodle",
            latitude=30.5412,
            longitude=114.3612,
            distance_meters=320,
            open_now=True,
        ),
        Shop(
            shop_id="shop-poi-only",
            name="示例快餐（合成，仅 POI）",
            source=_source(SourceKind.MAP, "synthetic-map/poi-only"),
            address="示例路 9 号（合成）",
            distance_meters=800,
        ),
        Shop(
            shop_id="shop-stale",
            name="示例米线（合成，菜单过期）",
            source=_source(SourceKind.MAP, "synthetic-map/stale"),
            address="示例路 20 号（合成）",
            distance_meters=1200,
        ),
    )


def synthetic_items() -> tuple[MealItem, ...]:
    """One decision-ready item, plus items with deliberate evidence gaps."""
    return (
        MealItem(
            item_id="item-beef-noodle",
            shop_id="shop-noodle",
            name="牛肉拉面（合成）",
            source=_source(SourceKind.MENU, "synthetic-menu/noodle#beef"),
            composition=("手工拉面", "牛肉片", "清汤", "香菜"),
            composition_source=_source(SourceKind.MENU, "synthetic-menu/noodle#beef"),
            portion="大碗，约 600 克",
            portion_source=_source(SourceKind.REVIEW, "synthetic-review/noodle-9"),
            flavours=(
                FlavourNote(
                    description="清汤牛肉汤底，面条偏硬",
                    detail="辣子单独提供，可按口味添加",
                    source=_source(SourceKind.REVIEW, "synthetic-review/noodle-9"),
                ),
            ),
            prices=(PriceRecord(basis="menu", source=_source(SourceKind.MENU, "synthetic-menu/noodle#beef"), amount_cny=18),),
            suitable_meals=("午餐", "晚餐"),
        ),
        MealItem(
            item_id="item-poi-guess",
            shop_id="shop-poi-only",
            name="招牌套餐（合成，无菜单依据）",
            source=_source(SourceKind.MAP, "synthetic-map/poi-only"),
            prices=(PriceRecord(basis="per_person", source=_source(SourceKind.MAP, "synthetic-map/poi-only"), amount_cny=25),),
        ),
        MealItem(
            item_id="item-stale-mixian",
            shop_id="shop-stale",
            name="砂锅米线（合成，菜单已过期）",
            source=_source(SourceKind.MENU, "synthetic-menu/stale#mixian", age_days=400),
            composition=("米线", "骨汤", "配菜"),
            composition_source=_source(SourceKind.MENU, "synthetic-menu/stale#mixian", age_days=400),
            prices=(PriceRecord(basis="menu", source=_source(SourceKind.MENU, "synthetic-menu/stale#mixian", age_days=400), min_cny=12, max_cny=22),),
        ),
    )


def item_by_id(item_id: str) -> MealItem | None:
    return next((item for item in synthetic_items() if item.item_id == item_id), None)


def shop_by_id(shop_id: str) -> Shop | None:
    return next((shop for shop in synthetic_shops() if shop.shop_id == shop_id), None)


def items_for_shop(shop_id: str) -> tuple[MealItem, ...]:
    return tuple(item for item in synthetic_items() if item.shop_id == shop_id)
