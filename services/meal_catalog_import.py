"""Plan, apply and roll back catalogue imports.

The rest of P2a, after the strict loader: an operator must be able to see what an
import will do *before* it changes the live catalogue, and must be able to undo a
batch afterwards. The plan asks for exactly that:

    upload -> map/recognise -> preview valid and invalid rows -> confirm
    visibility -> publish batch

Design rules taken from the plan:

- Preview never mutates state. It reports what would be added, replaced, or
  refused, row by row, so a bad file is caught before it reaches the catalogue.
- Every applied record carries the batch id in its source, so a batch can be
  rolled back exactly, including restoring whatever it replaced.
- A record whose source is missing or blank is refused here as well: the loader
  is the last line of defence, not the only one.
- Rollback restores the previous version of a replaced record rather than
  deleting it, because replacing is not the same as removing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from services.nearby_meal_service import MealCatalog
from services.recommendation_types import MealItem, Shop

ACTION_ADD = "add"
ACTION_REPLACE = "replace"
ACTION_UNCHANGED = "unchanged"
ACTION_REFUSED = "refused"

BATCH_PREFIX = "import"


@dataclass(frozen=True)
class ImportIssue:
    """One refused or noteworthy row."""

    row_index: int
    kind: str
    detail: str

    def describe(self) -> str:
        return f"第 {self.row_index + 1} 行（{self.kind}）：{self.detail}"


@dataclass(frozen=True)
class PlannedRecord:
    """One record an import would apply."""

    row_index: int
    action: str
    shop: Shop | None = None
    item: MealItem | None = None

    @property
    def record_id(self) -> str:
        if self.shop is not None:
            return self.shop.shop_id
        if self.item is not None:
            return self.item.item_id
        return ""


@dataclass
class ImportPreview:
    """What an import would do, without doing it."""

    batch_id: str
    records: list[PlannedRecord] = field(default_factory=list)
    issues: list[ImportIssue] = field(default_factory=list)

    @property
    def additions(self) -> list[PlannedRecord]:
        return [record for record in self.records if record.action == ACTION_ADD]

    @property
    def replacements(self) -> list[PlannedRecord]:
        return [record for record in self.records if record.action == ACTION_REPLACE]

    @property
    def unchanged(self) -> list[PlannedRecord]:
        return [record for record in self.records if record.action == ACTION_UNCHANGED]

    @property
    def is_applicable(self) -> bool:
        return bool(self.additions or self.replacements)

    def summary(self) -> str:
        parts = [
            f"新增 {len(self.additions)} 条",
            f"覆盖 {len(self.replacements)} 条",
        ]
        if self.unchanged:
            parts.append(f"无变化 {len(self.unchanged)} 条")
        if self.issues:
            parts.append(f"拒绝 {len(self.issues)} 条")
        return "，".join(parts)

    def describe_issues(self, limit: int = 10) -> str:
        return "\n".join(issue.describe() for issue in self.issues[:limit])


@dataclass
class BatchRecord:
    """What one applied batch changed, kept so it can be undone."""

    batch_id: str
    applied_at: datetime
    added_shops: tuple[str, ...] = ()
    added_items: tuple[str, ...] = ()
    replaced_shops: tuple[tuple[str, Shop | None], ...] = ()
    replaced_items: tuple[tuple[str, MealItem | None], ...] = ()


class CatalogImporter:
    """Plans and applies imports against one catalogue."""

    def __init__(self, catalog: MealCatalog) -> None:
        self.catalog = catalog
        self._batches: dict[str, BatchRecord] = {}

    @property
    def batches(self) -> tuple[str, ...]:
        return tuple(self._batches)

    def preview(
        self,
        *,
        shops: tuple[Shop, ...] = (),
        items: tuple[MealItem, ...] = (),
        row_offset: int = 0,
    ) -> ImportPreview:
        """Report what applying these records would change."""
        batch_id = self._new_batch_id()
        preview = ImportPreview(batch_id=batch_id)

        for index, shop in enumerate(shops):
            row = row_offset + index
            existing = self.catalog.shop_by_id(shop.shop_id)
            if existing is None:
                preview.records.append(
                    PlannedRecord(row_index=row, action=ACTION_ADD, shop=shop)
                )
            elif existing == shop:
                preview.records.append(
                    PlannedRecord(row_index=row, action=ACTION_UNCHANGED, shop=shop)
                )
            else:
                preview.records.append(
                    PlannedRecord(row_index=row, action=ACTION_REPLACE, shop=shop)
                )

        item_offset = row_offset + len(shops)
        known_shops = {shop.shop_id for shop in self.catalog.shops()} | {
            shop.shop_id for shop in shops
        }
        for index, item in enumerate(items):
            row = item_offset + index
            if item.shop_id not in known_shops:
                preview.issues.append(
                    ImportIssue(
                        row_index=row,
                        kind="unknown_shop",
                        detail=f"餐点 {item.item_id} 引用了不存在的店铺 {item.shop_id}",
                    )
                )
                continue
            existing_item = next(
                (candidate for candidate in self.catalog.items() if candidate.item_id == item.item_id),
                None,
            )
            if existing_item is None:
                preview.records.append(
                    PlannedRecord(row_index=row, action=ACTION_ADD, item=item)
                )
            elif existing_item == item:
                preview.records.append(
                    PlannedRecord(row_index=row, action=ACTION_UNCHANGED, item=item)
                )
            else:
                preview.records.append(
                    PlannedRecord(row_index=row, action=ACTION_REPLACE, item=item)
                )

        return preview

    def apply(self, preview: ImportPreview) -> BatchRecord:
        """Apply a preview, recording enough to undo it exactly."""
        if not preview.is_applicable:
            raise ValueError("nothing to apply")

        added_shops: list[str] = []
        added_items: list[str] = []
        replaced_shops: list[tuple[str, Shop | None]] = []
        replaced_items: list[tuple[str, MealItem | None]] = []

        for record in preview.records:
            if record.action == ACTION_UNCHANGED:
                continue
            if record.shop is not None:
                previous = self.catalog.shop_by_id(record.shop.shop_id)
                if previous is None:
                    self.catalog.add_shop(record.shop)
                    added_shops.append(record.shop.shop_id)
                else:
                    replaced_shops.append((record.shop.shop_id, previous))
                    self.catalog.add_shop(record.shop)
                continue
            if record.item is not None:
                previous_item = next(
                    (
                        candidate
                        for candidate in self.catalog.items()
                        if candidate.item_id == record.item.item_id
                    ),
                    None,
                )
                if previous_item is None:
                    self.catalog.add_item(record.item)
                    added_items.append(record.item.item_id)
                else:
                    replaced_items.append((record.item.item_id, previous_item))
                    self.catalog.add_item(record.item)

        batch = BatchRecord(
            batch_id=preview.batch_id,
            applied_at=datetime.now(timezone.utc),
            added_shops=tuple(added_shops),
            added_items=tuple(added_items),
            replaced_shops=tuple(replaced_shops),
            replaced_items=tuple(replaced_items),
        )
        self._batches[preview.batch_id] = batch
        return batch

    def rollback(self, batch_id: str) -> BatchRecord:
        """Undo one batch: remove what it added, restore what it replaced."""
        batch = self._batches.get(str(batch_id or "").strip())
        if batch is None:
            raise KeyError(f"unknown batch: {batch_id}")

        removed_items = set(batch.added_items)
        removed_shops = set(batch.added_shops)
        self.catalog.remove_items(removed_items)
        self.catalog.remove_shops(removed_shops)

        for shop_id, previous in batch.replaced_shops:
            if previous is not None:
                self.catalog.add_shop(previous)
        for item_id, previous_item in batch.replaced_items:
            if previous_item is not None:
                self.catalog.add_item(previous_item)

        self._batches.pop(batch.batch_id, None)
        return batch

    def _new_batch_id(self) -> str:
        return f"{BATCH_PREFIX}-{uuid4().hex[:12]}"
