"""P2a: preview before applying, and roll back exactly."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from services.meal_catalog_import import (
    ACTION_ADD,
    ACTION_REPLACE,
    ACTION_UNCHANGED,
    CatalogImporter,
)
from services.nearby_meal_service import MealCatalog
from services.recommendation_types import (
    FlavourNote,
    MealItem,
    PriceRecord,
    Shop,
    SourceKind,
    SourceRef,
)


_FIXED_TIME = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


def _source(reference: str = "src-1") -> SourceRef:
    """A source with a fixed timestamp, so equality is meaningful in tests."""
    return SourceRef(
        kind=SourceKind.UPLOAD, reference=reference, fetched_at=_FIXED_TIME
    )


def _shop(shop_id: str = "s1", name: str = "示例店", distance: int = 300) -> Shop:
    return Shop(
        shop_id=shop_id,
        name=name,
        source=_source(f"upload/{shop_id}"),
        address="示例路 1 号",
        distance_meters=distance,
    )


def _item(item_id: str = "i1", shop_id: str = "s1", name: str = "套餐", price: int = 18) -> MealItem:
    return MealItem(
        item_id=item_id,
        shop_id=shop_id,
        name=name,
        source=_source(f"upload/{item_id}"),
        composition=("主料", "配菜"),
        composition_source=_source(f"upload/{item_id}#composition"),
        portion="一份",
        portion_source=_source(f"upload/{item_id}#portion"),
        flavours=(FlavourNote(description="清汤底，面条偏硬", source=_source()),),
        prices=(PriceRecord(basis="menu", source=_source(), amount_cny=price),),
    )


class PreviewTest(unittest.TestCase):
    def test_preview_does_not_mutate_the_catalogue(self) -> None:
        catalog = MealCatalog()
        importer = CatalogImporter(catalog)

        preview = importer.preview(shops=(_shop(),), items=(_item(),))

        self.assertTrue(preview.is_applicable)
        self.assertEqual(catalog.shop_count, 0)
        self.assertEqual(catalog.item_count, 0)
        self.assertEqual(importer.batches, ())

    def test_new_records_are_planned_as_additions(self) -> None:
        importer = CatalogImporter(MealCatalog())

        preview = importer.preview(shops=(_shop(),), items=(_item(),))

        self.assertEqual([record.action for record in preview.records], [ACTION_ADD, ACTION_ADD])
        self.assertIn("新增 2 条", preview.summary())

    def test_identical_records_are_reported_as_unchanged(self) -> None:
        catalog = MealCatalog(shops=(_shop(),), items=(_item(),))
        importer = CatalogImporter(catalog)

        preview = importer.preview(shops=(_shop(),), items=(_item(),))

        self.assertEqual(
            [record.action for record in preview.records],
            [ACTION_UNCHANGED, ACTION_UNCHANGED],
        )
        self.assertFalse(preview.is_applicable)

    def test_changed_records_are_planned_as_replacements(self) -> None:
        catalog = MealCatalog(shops=(_shop(),), items=(_item(price=18),))
        importer = CatalogImporter(catalog)

        preview = importer.preview(items=(_item(price=22),))

        self.assertEqual(preview.records[0].action, ACTION_REPLACE)
        self.assertIn("覆盖 1 条", preview.summary())

    def test_item_for_an_unknown_shop_is_refused(self) -> None:
        importer = CatalogImporter(MealCatalog())

        preview = importer.preview(shops=(_shop("s1"),), items=(_item("i9", "missing"),))

        self.assertEqual(len(preview.issues), 1)
        self.assertIn("不存在的店铺", preview.issues[0].detail)
        self.assertIn("第 2 行", preview.describe_issues())

    def test_item_may_reference_a_shop_from_the_same_import(self) -> None:
        importer = CatalogImporter(MealCatalog())

        preview = importer.preview(shops=(_shop("new-shop"),), items=(_item("i1", "new-shop"),))

        self.assertEqual(preview.issues, [])
        self.assertEqual(len(preview.additions), 2)

    def test_row_numbers_account_for_the_offset(self) -> None:
        importer = CatalogImporter(MealCatalog())

        preview = importer.preview(items=(_item("i1", "missing"),), row_offset=5)

        self.assertEqual(preview.issues[0].row_index, 5)

    def test_batch_ids_are_unique_and_prefixed(self) -> None:
        importer = CatalogImporter(MealCatalog())

        first = importer.preview(shops=(_shop(),))
        second = importer.preview(shops=(_shop(),))

        self.assertNotEqual(first.batch_id, second.batch_id)
        self.assertTrue(first.batch_id.startswith("import-"))


class ApplyTest(unittest.TestCase):
    def test_apply_adds_records(self) -> None:
        catalog = MealCatalog()
        importer = CatalogImporter(catalog)

        preview = importer.preview(shops=(_shop(),), items=(_item(),))
        batch = importer.apply(preview)

        self.assertEqual(catalog.shop_count, 1)
        self.assertEqual(catalog.item_count, 1)
        self.assertEqual(batch.added_shops, ("s1",))
        self.assertEqual(batch.added_items, ("i1",))
        self.assertIn(batch.batch_id, importer.batches)

    def test_apply_replaces_and_remembers_the_previous_version(self) -> None:
        catalog = MealCatalog(shops=(_shop(),), items=(_item(price=18),))
        importer = CatalogImporter(catalog)

        preview = importer.preview(items=(_item(price=22),))
        batch = importer.apply(preview)

        self.assertEqual(catalog.items()[0].menu_prices[0].amount_cny, 22)
        self.assertEqual(len(batch.replaced_items), 1)
        self.assertEqual(batch.replaced_items[0][1].menu_prices[0].amount_cny, 18)

    def test_apply_refuses_a_preview_with_nothing_to_do(self) -> None:
        catalog = MealCatalog(shops=(_shop(),))
        importer = CatalogImporter(catalog)

        preview = importer.preview(shops=(_shop(),))
        self.assertFalse(preview.is_applicable)

        with self.assertRaises(ValueError):
            importer.apply(preview)

    def test_apply_ignores_unchanged_records_inside_a_batch(self) -> None:
        catalog = MealCatalog(shops=(_shop(),), items=(_item(price=18),))
        importer = CatalogImporter(catalog)

        # One changed item and one identical item in the same import.
        preview = importer.preview(shops=(_shop(),), items=(_item(price=22),))
        batch = importer.apply(preview)

        self.assertEqual(batch.added_shops, ())
        self.assertEqual(len(batch.replaced_items), 1)

    def test_applying_a_partial_preview_only_applies_the_changes(self) -> None:
        catalog = MealCatalog(shops=(_shop(),), items=(_item(price=18),))
        importer = CatalogImporter(catalog)

        preview = importer.preview(shops=(_shop(),), items=(_item(price=22),))
        importer.apply(preview)

        self.assertEqual(catalog.shop_count, 1)
        self.assertEqual(catalog.items()[0].menu_prices[0].amount_cny, 22)


class RollbackTest(unittest.TestCase):
    def test_rollback_removes_what_the_batch_added(self) -> None:
        catalog = MealCatalog()
        importer = CatalogImporter(catalog)
        batch = importer.apply(importer.preview(shops=(_shop(),), items=(_item(),)))

        importer.rollback(batch.batch_id)

        self.assertEqual(catalog.shop_count, 0)
        self.assertEqual(catalog.item_count, 0)
        self.assertEqual(importer.batches, ())

    def test_rollback_restores_what_the_batch_replaced(self) -> None:
        catalog = MealCatalog(shops=(_shop(),), items=(_item(price=18),))
        importer = CatalogImporter(catalog)
        batch = importer.apply(importer.preview(items=(_item(price=22),)))

        importer.rollback(batch.batch_id)

        self.assertEqual(catalog.items()[0].menu_prices[0].amount_cny, 18)

    def test_rollback_does_not_touch_other_batches(self) -> None:
        catalog = MealCatalog()
        importer = CatalogImporter(catalog)
        first = importer.apply(importer.preview(shops=(_shop("s1"),)))
        second = importer.apply(importer.preview(shops=(_shop("s2", "第二家"),)))

        importer.rollback(first.batch_id)

        self.assertIsNone(catalog.shop_by_id("s1"))
        self.assertIsNotNone(catalog.shop_by_id("s2"))
        self.assertEqual(importer.batches, (second.batch_id,))

    def test_rollback_of_an_unknown_batch_is_refused(self) -> None:
        importer = CatalogImporter(MealCatalog())

        with self.assertRaises(KeyError):
            importer.rollback("import-nope")

    def test_rollback_removes_items_that_belonged_to_a_removed_shop(self) -> None:
        """Removing a shop must not leave orphaned items behind."""
        catalog = MealCatalog()
        importer = CatalogImporter(catalog)
        batch = importer.apply(importer.preview(shops=(_shop(),), items=(_item(),)))

        importer.rollback(batch.batch_id)

        self.assertEqual(catalog.items(), ())

    def test_a_batch_can_only_be_rolled_back_once(self) -> None:
        catalog = MealCatalog()
        importer = CatalogImporter(catalog)
        batch = importer.apply(importer.preview(shops=(_shop(),)))

        importer.rollback(batch.batch_id)

        with self.assertRaises(KeyError):
            importer.rollback(batch.batch_id)


class CatalogueRemovalTest(unittest.TestCase):
    def test_removing_a_shop_removes_its_items(self) -> None:
        catalog = MealCatalog(shops=(_shop("s1"), _shop("s2", "第二家")), items=(_item("i1", "s1"),))

        removed = catalog.remove_shop("s1")

        self.assertTrue(removed)
        self.assertEqual(catalog.shop_count, 1)
        self.assertEqual(catalog.item_count, 0)

    def test_removing_an_unknown_shop_reports_false(self) -> None:
        self.assertFalse(MealCatalog().remove_shop("missing"))

    def test_bulk_removal_ignores_duplicates(self) -> None:
        catalog = MealCatalog(shops=(_shop("s1"),), items=(_item("i1", "s1"),))

        self.assertEqual(catalog.remove_items(["i1", "i1"]), 1)


if __name__ == "__main__":
    unittest.main()
