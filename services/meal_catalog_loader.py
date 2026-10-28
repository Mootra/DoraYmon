"""Load a curated shop and meal catalogue into the recommendation contracts.

The first version of P2a: read a JSON catalogue, validate every record against the
contracts, and refuse anything that cannot support a claim. The plan's hard rule
is that a recommendation may not invent a shop, item, price or portion, so the
loader is strict on purpose:

- A shop without a source is rejected, because the bot could not say where the
  fact came from.
- An item referencing an unknown shop is rejected, since it could not be shown
  to belong to a real place.
- Composition or portion without its own source is rejected by the contract
  itself.
- A price without a basis and source is rejected, and a per-person figure is
  never promoted to a menu price.
- An unreadable or invalid file yields an empty catalogue and a list of problems
  rather than a partial one that looks complete.

Import, batches, visibility and rollback arrive in the rest of P2a; this covers
the file-to-contract step that the rest builds on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.nearby_meal_service import MealCatalog
from services.recommendation_types import (
    FlavourNote,
    MealItem,
    PriceRecord,
    Shop,
    SourceKind,
    SourceRef,
    Visibility,
)

CATALOG_FILENAME = "meal_catalog.json"
DEFAULT_CATALOG_DIR = Path("data") / "meal_catalog"


@dataclass
class CatalogLoadResult:
    """What was loaded, and every record that was refused."""

    catalog: MealCatalog
    problems: list[str] = field(default_factory=list)
    source_path: Path | None = None

    @property
    def is_empty(self) -> bool:
        return self.catalog.item_count == 0

    @property
    def has_problems(self) -> bool:
        return bool(self.problems)

    def summary(self) -> str:
        parts = [f"店铺 {self.catalog.shop_count} 家，餐点 {self.catalog.item_count} 个"]
        if self.problems:
            parts.append(f"被拒绝的记录 {len(self.problems)} 条")
        return "；".join(parts)


def catalog_path(directory: Path | str) -> Path:
    return Path(directory) / CATALOG_FILENAME


def load_catalog(directory: Path | str) -> CatalogLoadResult:
    """Load the catalogue from ``directory``, refusing invalid records."""
    path = catalog_path(directory)
    if not path.exists():
        return CatalogLoadResult(
            catalog=MealCatalog(),
            problems=[f"目录里没有 {CATALOG_FILENAME}"],
            source_path=path,
        )

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return CatalogLoadResult(
            catalog=MealCatalog(),
            problems=[f"目录文件无法解析：{error}"],
            source_path=path,
        )

    if not isinstance(raw, dict):
        return CatalogLoadResult(
            catalog=MealCatalog(),
            problems=["目录文件根节点必须是对象"],
            source_path=path,
        )

    problems: list[str] = []
    shops: list[Shop] = []
    for index, record in enumerate(raw.get("shops") or []):
        shop = _parse_shop(record, index, problems)
        if shop is not None:
            shops.append(shop)

    catalog = MealCatalog(shops=tuple(shops))
    for index, record in enumerate(raw.get("items") or []):
        item = _parse_item(record, index, problems)
        if item is None:
            continue
        if catalog.shop_by_id(item.shop_id) is None:
            problems.append(f"餐点 #{index} 引用了不存在的店铺：{item.shop_id}")
            continue
        catalog.add_item(item)

    return CatalogLoadResult(catalog=catalog, problems=problems, source_path=path)


def _source_from(raw: Any, *, default_reference: str = "") -> SourceRef | None:
    """Build a source, or None when the record cannot be attributed."""
    if isinstance(raw, str):
        raw = {"kind": raw, "reference": default_reference}
    if not isinstance(raw, dict):
        return None
    kind_text = str(raw.get("kind", "") or "").strip().lower()
    raw_reference = str(raw.get("reference", "") or "").strip()
    if "reference" in raw and not raw_reference:
        # An explicitly blank reference is a data problem, not a request to use
        # a placeholder: inventing one would make an unattributed fact look
        # attributed.
        return None
    reference = raw_reference or default_reference
    if not reference:
        return None
    try:
        kind = SourceKind(kind_text) if kind_text else SourceKind.UPLOAD
    except ValueError:
        kind = SourceKind.UNKNOWN
    fetched_raw = str(raw.get("fetched_at", "") or "").strip()
    fetched_at = _parse_time(fetched_raw)
    visibility_raw = str(raw.get("visibility", "") or "").strip().lower()
    try:
        visibility = Visibility(visibility_raw) if visibility_raw else Visibility.PRIVATE
    except ValueError:
        visibility = Visibility.PRIVATE
    try:
        return SourceRef(
            kind=kind,
            reference=reference,
            fetched_at=fetched_at,
            visibility=visibility,
            batch_id=str(raw.get("batch_id", "") or ""),
        )
    except ValueError:
        return None


def _parse_time(raw: str) -> datetime:
    if raw:
        for pattern in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(raw, pattern)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    # An undated record is still attributed, just not timestamped.
    return datetime.now(timezone.utc).replace(microsecond=0)


def _parse_shop(raw: Any, index: int, problems: list[str]) -> Shop | None:
    if not isinstance(raw, dict):
        problems.append(f"店铺 #{index} 不是对象")
        return None
    shop_id = str(raw.get("shop_id", "") or "").strip()
    name = str(raw.get("name", "") or "").strip()
    if not shop_id or not name:
        problems.append(f"店铺 #{index} 缺少 shop_id 或 name")
        return None
    source = _source_from(raw.get("source"), default_reference=f"catalog:shop:{shop_id}")
    if source is None:
        problems.append(f"店铺 #{index}（{name}）没有可用来源")
        return None
    try:
        return Shop(
            shop_id=shop_id,
            name=name,
            source=source,
            address=str(raw.get("address", "") or ""),
            map_url=str(raw.get("map_url", "") or ""),
            distance_meters=(
                int(raw["distance_meters"]) if raw.get("distance_meters") is not None else None
            ),
            open_now=raw.get("open_now") if isinstance(raw.get("open_now"), bool) else None,
        )
    except (ValueError, TypeError) as error:
        problems.append(f"店铺 #{index}（{name}）无法使用：{error}")
        return None


def _parse_item(raw: Any, index: int, problems: list[str]) -> MealItem | None:
    if not isinstance(raw, dict):
        problems.append(f"餐点 #{index} 不是对象")
        return None
    item_id = str(raw.get("item_id", "") or "").strip()
    shop_id = str(raw.get("shop_id", "") or "").strip()
    name = str(raw.get("name", "") or "").strip()
    if not item_id or not shop_id or not name:
        problems.append(f"餐点 #{index} 缺少 item_id、shop_id 或 name")
        return None
    source = _source_from(raw.get("source"), default_reference=f"catalog:item:{item_id}")
    if source is None:
        problems.append(f"餐点 #{index}（{name}）没有可用来源")
        return None

    composition = tuple(
        str(part).strip() for part in (raw.get("composition") or []) if str(part).strip()
    )
    composition_source = None
    if composition:
        composition_source = _source_from(
            raw.get("composition_source"), default_reference=f"catalog:item:{item_id}#composition"
        )
        if composition_source is None:
            problems.append(f"餐点 #{index}（{name}）有组成但没有组成来源")
            return None

    portion = str(raw.get("portion", "") or "").strip()
    portion_source = None
    if portion:
        portion_source = _source_from(
            raw.get("portion_source"), default_reference=f"catalog:item:{item_id}#portion"
        )
        if portion_source is None:
            problems.append(f"餐点 #{index}（{name}）有份量但没有份量来源")
            return None

    flavours: list[FlavourNote] = []
    for flavour_index, flavour_raw in enumerate(raw.get("flavours") or []):
        if not isinstance(flavour_raw, dict):
            problems.append(f"餐点 #{index} 的口味 #{flavour_index} 不是对象")
            continue
        description = str(flavour_raw.get("description", "") or "").strip()
        if not description:
            problems.append(f"餐点 #{index} 的口味 #{flavour_index} 缺少描述")
            continue
        flavour_source = _source_from(
            flavour_raw.get("source"),
            default_reference=f"catalog:item:{item_id}#flavour{flavour_index}",
        )
        if flavour_source is None:
            problems.append(f"餐点 #{index} 的口味 #{flavour_index} 没有来源")
            continue
        flavours.append(
            FlavourNote(
                description=description,
                detail=str(flavour_raw.get("detail", "") or ""),
                source=flavour_source,
            )
        )

    prices: list[PriceRecord] = []
    for price_index, price_raw in enumerate(raw.get("prices") or []):
        if not isinstance(price_raw, dict):
            problems.append(f"餐点 #{index} 的价格 #{price_index} 不是对象")
            continue
        price_source = _source_from(
            price_raw.get("source"),
            default_reference=f"catalog:item:{item_id}#price{price_index}",
        )
        if price_source is None:
            problems.append(f"餐点 #{index} 的价格 #{price_index} 没有来源")
            continue
        try:
            prices.append(
                PriceRecord(
                    basis=str(price_raw.get("basis", "") or "").strip(),
                    source=price_source,
                    amount_cny=_optional_float(price_raw.get("amount_cny")),
                    min_cny=_optional_float(price_raw.get("min_cny")),
                    max_cny=_optional_float(price_raw.get("max_cny")),
                )
            )
        except ValueError as error:
            problems.append(f"餐点 #{index} 的价格 #{price_index} 不合法：{error}")

    try:
        return MealItem(
            item_id=item_id,
            shop_id=shop_id,
            name=name,
            source=source,
            composition=composition,
            composition_source=composition_source,
            portion=portion,
            portion_source=portion_source,
            flavours=tuple(flavours),
            prices=tuple(prices),
            suitable_meals=tuple(
                str(value).strip() for value in (raw.get("suitable_meals") or []) if str(value).strip()
            ),
        )
    except ValueError as error:
        problems.append(f"餐点 #{index}（{name}）无法使用：{error}")
        return None


def save_catalog(catalog: MealCatalog, directory: Path | str) -> Path:
    """Write a catalogue back in the format the loader reads.

    The round trip matters: an import that cannot be persisted is not an import.
    Saving is atomic-ish in the sense that the file is written in one call, so a
    crash cannot leave a half-written catalogue that would load as "some records
    are missing".
    """
    target = catalog_path(directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "shops": [_shop_to_payload(shop) for shop in catalog.shops()],
        "items": [_item_to_payload(item) for item in catalog.items()],
    }
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return target


def _source_to_payload(source: SourceRef) -> dict[str, Any]:
    return {
        "kind": source.kind.value,
        "reference": source.reference,
        "fetched_at": source.fetched_at.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "visibility": source.visibility.value,
        "batch_id": source.batch_id,
    }


def _shop_to_payload(shop: Shop) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "shop_id": shop.shop_id,
        "name": shop.name,
        "source": _source_to_payload(shop.source),
    }
    if shop.address:
        payload["address"] = shop.address
    if shop.map_url:
        payload["map_url"] = shop.map_url
    if shop.distance_meters is not None:
        payload["distance_meters"] = int(shop.distance_meters)
    if shop.open_now is not None:
        payload["open_now"] = bool(shop.open_now)
    return payload


def _item_to_payload(item: MealItem) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "item_id": item.item_id,
        "shop_id": item.shop_id,
        "name": item.name,
        "source": _source_to_payload(item.source),
    }
    if item.composition:
        payload["composition"] = list(item.composition)
        if item.composition_source is not None:
            payload["composition_source"] = _source_to_payload(item.composition_source)
    if item.portion:
        payload["portion"] = item.portion
        if item.portion_source is not None:
            payload["portion_source"] = _source_to_payload(item.portion_source)
    if item.flavours:
        payload["flavours"] = [
            {
                "description": note.description,
                "detail": note.detail,
                "source": _source_to_payload(note.source),
            }
            for note in item.flavours
        ]
    if item.prices:
        payload["prices"] = [_price_to_payload(price) for price in item.prices]
    if item.suitable_meals:
        payload["suitable_meals"] = list(item.suitable_meals)
    return payload


def _price_to_payload(price: PriceRecord) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "basis": price.basis,
        "source": _source_to_payload(price.source),
    }
    if price.amount_cny is not None:
        payload["amount_cny"] = price.amount_cny
    if price.min_cny is not None:
        payload["min_cny"] = price.min_cny
    if price.max_cny is not None:
        payload["max_cny"] = price.max_cny
    return payload


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
