"""P2a: the loader must refuse anything that cannot support a claim."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from services.meal_catalog_loader import (
    CATALOG_FILENAME,
    catalog_path,
    load_catalog,
    save_catalog,
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


def _shop(**overrides) -> dict:
    payload = {
        "shop_id": "s1",
        "name": "示例店",
        "address": "示例路 1 号",
        "distance_meters": 300,
        "source": {"kind": "map", "reference": "map/1", "fetched_at": "2026-10-19"},
    }
    payload.update(overrides)
    return payload


def _item(**overrides) -> dict:
    payload = {
        "item_id": "i1",
        "shop_id": "s1",
        "name": "示例套餐",
        "source": {"kind": "menu", "reference": "menu/1"},
        "composition": ["主料", "配菜"],
        "composition_source": {"kind": "menu", "reference": "menu/1"},
        "portion": "一份",
        "portion_source": {"kind": "review", "reference": "review/1"},
        "flavours": [
            {"description": "清汤底，面条偏硬", "source": {"kind": "review", "reference": "review/1"}}
        ],
        "prices": [{"basis": "menu", "amount_cny": 18, "source": {"kind": "menu", "reference": "menu/1"}}],
    }
    payload.update(overrides)
    return payload


def _shop_record() -> Shop:
    return Shop(
        shop_id="s1",
        name="示例店",
        source=SourceRef(
            kind=SourceKind.MAP,
            reference="map/1",
            fetched_at=datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc),
        ),
        address="示例路 1 号",
        distance_meters=300,
    )


def _item_record() -> MealItem:
    source = SourceRef(
        kind=SourceKind.MENU,
        reference="menu/1",
        fetched_at=datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc),
    )
    return MealItem(
        item_id="i1",
        shop_id="s1",
        name="示例套餐",
        source=source,
        composition=("主料", "配菜"),
        composition_source=source,
        portion="一份",
        portion_source=source,
        flavours=(FlavourNote(description="清汤底，面条偏硬", source=source),),
        prices=(PriceRecord(basis="menu", source=source, amount_cny=18),),
    )


class LoaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.directory = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _write(self, payload) -> None:
        path = self._directory_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(payload, str):
            path.write_text(payload, encoding="utf-8")
        else:
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _directory_file(self) -> Path:
        return catalog_path(self.directory)

    def test_missing_file_is_reported_not_silently_empty(self) -> None:
        result = load_catalog(self.directory)

        self.assertTrue(result.is_empty)
        self.assertTrue(result.has_problems)
        self.assertIn(CATALOG_FILENAME, result.problems[0])

    def test_valid_catalogue_loads(self) -> None:
        self._write({"shops": [_shop()], "items": [_item()]})

        result = load_catalog(self.directory)

        self.assertEqual(result.catalog.shop_count, 1)
        self.assertEqual(result.catalog.item_count, 1)
        self.assertFalse(result.has_problems)
        item = result.catalog.items()[0]
        self.assertTrue(item.is_decision_ready())

    def test_unparseable_file_is_refused(self) -> None:
        self._write("{not json")

        result = load_catalog(self.directory)

        self.assertTrue(result.is_empty)
        self.assertTrue(result.has_problems)

    def test_non_object_root_is_refused(self) -> None:
        self._write([1, 2, 3])

        result = load_catalog(self.directory)

        self.assertTrue(result.is_empty)
        self.assertIn("根节点", result.problems[0])

    def test_shop_without_a_source_is_refused(self) -> None:
        self._write({"shops": [_shop(source=None)], "items": []})

        result = load_catalog(self.directory)

        self.assertEqual(result.catalog.shop_count, 0)
        self.assertTrue(result.has_problems)

    def test_shop_without_a_reference_is_refused(self) -> None:
        self._write({"shops": [_shop(source={"kind": "map", "reference": "  "})], "items": []})

        result = load_catalog(self.directory)

        self.assertEqual(result.catalog.shop_count, 0)

    def test_item_referencing_an_unknown_shop_is_refused(self) -> None:
        self._write({"shops": [_shop()], "items": [_item(shop_id="missing")]})

        result = load_catalog(self.directory)

        self.assertEqual(result.catalog.item_count, 0)
        self.assertTrue(any("不存在的店铺" in problem for problem in result.problems))

    def test_item_without_a_source_is_refused(self) -> None:
        self._write({"shops": [_shop()], "items": [_item(source=None)]})

        result = load_catalog(self.directory)

        self.assertEqual(result.catalog.item_count, 0)

    def test_composition_without_its_own_source_is_refused(self) -> None:
        self._write({"shops": [_shop()], "items": [_item(composition_source=None)]})

        result = load_catalog(self.directory)

        self.assertEqual(result.catalog.item_count, 0)
        self.assertTrue(any("组成来源" in problem for problem in result.problems))

    def test_portion_without_its_own_source_is_refused(self) -> None:
        self._write({"shops": [_shop()], "items": [_item(portion_source=None)]})

        result = load_catalog(self.directory)

        self.assertEqual(result.catalog.item_count, 0)

    def test_price_without_a_source_is_refused(self) -> None:
        self._write(
            {
                "shops": [_shop()],
                "items": [_item(prices=[{"basis": "menu", "amount_cny": 18}])],
            }
        )

        result = load_catalog(self.directory)

        self.assertEqual(result.catalog.item_count, 1)
        self.assertEqual(result.catalog.items()[0].menu_prices, ())
        self.assertTrue(any("价格 #0 没有来源" in problem for problem in result.problems))

    def test_price_without_an_amount_is_refused(self) -> None:
        self._write(
            {
                "shops": [_shop()],
                "items": [_item(prices=[{"basis": "menu", "source": {"kind": "menu", "reference": "m"}}])],
            }
        )

        result = load_catalog(self.directory)

        self.assertEqual(result.catalog.items()[0].prices, ())
        self.assertTrue(result.has_problems)

    def test_per_person_price_stays_a_per_person_price(self) -> None:
        self._write(
            {
                "shops": [_shop()],
                "items": [
                    _item(
                        prices=[
                            {"basis": "per_person", "amount_cny": 25, "source": {"kind": "map", "reference": "map/人均"}}
                        ]
                    )
                ],
            }
        )

        result = load_catalog(self.directory)

        item = result.catalog.items()[0]
        self.assertEqual(item.menu_prices, ())
        self.assertIn("菜单价格", item.missing_evidence())

    def test_invalid_basis_is_reported(self) -> None:
        self._write(
            {
                "shops": [_shop()],
                "items": [_item(prices=[{"basis": "guess", "amount_cny": 1, "source": {"kind": "menu", "reference": "m"}}])],
            }
        )

        result = load_catalog(self.directory)

        self.assertTrue(any("不合法" in problem for problem in result.problems))

    def test_flavour_without_a_description_is_skipped(self) -> None:
        self._write(
            {
                "shops": [_shop()],
                "items": [_item(flavours=[{"source": {"kind": "review", "reference": "r"}}])],
            }
        )

        result = load_catalog(self.directory)

        self.assertEqual(result.catalog.items()[0].flavours, ())
        self.assertTrue(result.has_problems)

    def test_a_bad_record_does_not_discard_the_good_ones(self) -> None:
        self._write(
            {
                "shops": [_shop(), _shop(shop_id="bad", source=None), _shop(shop_id="s2", name="第二家")],
                "items": [_item(), _item(item_id="i2", shop_id="missing")],
            }
        )

        result = load_catalog(self.directory)

        self.assertEqual(result.catalog.shop_count, 2)
        self.assertEqual(result.catalog.item_count, 1)
        self.assertTrue(result.has_problems)

    def test_string_source_shorthand_is_accepted(self) -> None:
        self._write({"shops": [_shop(source="map")], "items": []})

        result = load_catalog(self.directory)

        self.assertEqual(result.catalog.shop_count, 1)

    def test_undated_source_is_still_attributed(self) -> None:
        self._write({"shops": [_shop(source={"kind": "map", "reference": "map/1"})], "items": []})

        result = load_catalog(self.directory)

        self.assertEqual(result.catalog.shop_count, 1)
        self.assertIsNotNone(result.catalog.shops()[0].source.fetched_at)

    def test_summary_reports_counts(self) -> None:
        self._write({"shops": [_shop()], "items": [_item()]})

        summary = load_catalog(self.directory).summary()

        self.assertIn("店铺 1 家", summary)
        self.assertIn("餐点 1 个", summary)

    def test_saved_catalogue_reloads_identically(self) -> None:
        """An import that cannot be persisted is not an import."""
        self._write({"shops": [_shop()], "items": [_item()]})
        loaded = load_catalog(self.directory)
        # Round-trip through a second directory so the original is untouched.
        other = Path(self._temp.name) / "roundtrip"

        save_catalog(loaded.catalog, other)
        reloaded = load_catalog(other)

        self.assertFalse(reloaded.has_problems)
        self.assertEqual(reloaded.catalog.shop_count, loaded.catalog.shop_count)
        self.assertEqual(reloaded.catalog.item_count, loaded.catalog.item_count)
        self.assertEqual(
            reloaded.catalog.items()[0].prices[0].amount_cny,
            loaded.catalog.items()[0].prices[0].amount_cny,
        )

    def test_save_preserves_evidence_details(self) -> None:
        catalogue = MealCatalog(
            shops=(_shop_record(),),
            items=(_item_record(),),
        )
        other = Path(self._temp.name) / "preserve"

        save_catalog(catalogue, other)
        reloaded = load_catalog(other)

        item = reloaded.catalog.items()[0]
        self.assertEqual(item.composition, ("主料", "配菜"))
        self.assertEqual(item.portion, "一份")
        self.assertTrue(item.has_informative_flavour)
        self.assertTrue(item.is_decision_ready())

    def test_saved_file_is_readable_json(self) -> None:
        other = Path(self._temp.name) / "readable"

        save_catalog(MealCatalog(shops=(_shop_record(),)), other)

        payload = json.loads(catalog_path(other).read_text(encoding="utf-8"))
        self.assertIn("shops", payload)
        self.assertEqual(payload["shops"][0]["shop_id"], "s1")

    def test_addressing_a_directory_without_the_file(self) -> None:
        other = self.directory / "nested"

        result = load_catalog(other)

        self.assertTrue(result.is_empty)


if __name__ == "__main__":
    unittest.main()
