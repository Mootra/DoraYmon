"""P2a: a menu spreadsheet must import without silently losing rows."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from services.catalog_table import (
    TABLE_ITEMS,
    TABLE_SHOPS,
    TABLE_UNKNOWN,
    XlsxUnavailable,
    detect_table,
    list_sheets,
    parse_items,
    parse_shops,
    parse_source_spec,
    parse_table,
    read_table,
)
from services.recommendation_types import SourceKind, SourceRef


def _source(reference: str = "default") -> SourceRef:
    return SourceRef(
        kind=SourceKind.UPLOAD,
        reference=reference,
        fetched_at=datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc),
    )


class TableTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.directory = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _write(self, name: str, text: str) -> Path:
        path = self.directory / name
        path.write_text(text, encoding="utf-8")
        return path


class DetectTableTest(unittest.TestCase):
    def test_item_headers_are_recognised(self) -> None:
        self.assertEqual(detect_table(["item_id", "shop_id", "name"]), TABLE_ITEMS)

    def test_shop_headers_are_recognised(self) -> None:
        self.assertEqual(detect_table(["shop_id", "name", "address"]), TABLE_SHOPS)

    def test_unknown_headers_are_reported(self) -> None:
        self.assertEqual(detect_table(["foo", "bar"]), TABLE_UNKNOWN)

    def test_header_case_and_spacing_are_tolerated(self) -> None:
        self.assertEqual(detect_table([" Item_ID ", "Shop_ID", "NAME"]), TABLE_ITEMS)


class ReadTableTest(TableTestCase):
    def test_csv_is_read_into_rows(self) -> None:
        path = self._write("shops.csv", "shop_id,name\ns1,示例店\n")

        read = read_table(path)

        self.assertEqual(read.table, TABLE_SHOPS)
        self.assertEqual(read.rows[0]["name"], "示例店")

    def test_utf8_bom_is_tolerated(self) -> None:
        """Excel writes a BOM; it must not become part of the first header."""
        path = self._write("shops.csv", "\ufeffshop_id,name\ns1,示例店\n")

        read = read_table(path)

        self.assertIn("shop_id", read.rows[0])

    def test_tsv_is_supported(self) -> None:
        path = self._write("shops.tsv", "shop_id\tname\ns1\t示例店\n")

        read = read_table(path)

        self.assertEqual(read.rows[0]["shop_id"], "s1")

    def test_blank_lines_are_skipped_not_rejected(self) -> None:
        path = self._write("shops.csv", "shop_id,name\ns1,示例店\n\n,\n")

        read = read_table(path)

        self.assertEqual(len(read.rows), 1)

    def test_unsupported_suffix_is_reported(self) -> None:
        path = self._write("shops.pdf", "nope")

        read = read_table(path)

        self.assertTrue(read.problems)
        self.assertIn("不支持的格式", read.problems[0].detail)

    def test_missing_file_is_reported(self) -> None:
        read = read_table(self.directory / "absent.csv")

        self.assertTrue(read.problems)
        self.assertIn("找不到文件", read.problems[0].detail)

    def test_file_without_a_header_is_reported(self) -> None:
        path = self._write("empty.csv", "")

        read = read_table(path)

        self.assertTrue(read.problems)

    def test_xlsx_without_openpyxl_says_so(self) -> None:
        """The optional reader must explain itself, not fail obscurely."""
        path = self._write("menu.xlsx", "not really a workbook")

        try:
            import openpyxl  # noqa: F401
        except ImportError:
            with self.assertRaises(XlsxUnavailable) as context:
                read_table(path)
            self.assertIn("openpyxl", str(context.exception))
        else:
            self.skipTest("openpyxl is installed; the optional path is available")

    def test_list_sheets_without_openpyxl_says_so(self) -> None:
        path = self._write("menu.xlsx", "nope")

        try:
            import openpyxl  # noqa: F401
        except ImportError:
            with self.assertRaises(XlsxUnavailable):
                list_sheets(path)
        else:
            self.skipTest("openpyxl is installed")


class ParseShopsTest(unittest.TestCase):
    def test_shop_row_becomes_a_shop(self) -> None:
        rows = [
            {
                "shop_id": "s1",
                "name": "示例兰州拉面",
                "address": "示例路 1 号",
                "distance_meters": "320",
                "open_now": "是",
                "source_reference": "menu-2026-10.csv",
                "source_kind": "upload",
            }
        ]

        result = parse_shops(rows)

        self.assertEqual(len(result.shops), 1)
        shop = result.shops[0]
        self.assertEqual(shop.name, "示例兰州拉面")
        self.assertEqual(shop.distance_meters, 320)
        self.assertTrue(shop.open_now)
        self.assertEqual(shop.source.kind, SourceKind.UPLOAD)

    def test_default_source_is_used_when_the_row_has_none(self) -> None:
        rows = [{"shop_id": "s1", "name": "示例店"}]

        result = parse_shops(rows, default_source=_source("upload:batch"))

        self.assertEqual(len(result.shops), 1)
        self.assertEqual(result.shops[0].source.reference, "upload:batch")

    def test_row_without_any_source_is_refused(self) -> None:
        rows = [{"shop_id": "s1", "name": "示例店"}]

        result = parse_shops(rows)

        self.assertEqual(result.shops, [])
        self.assertEqual(len(result.problems), 1)
        self.assertIn("缺少来源", result.problems[0].detail)

    def test_missing_name_is_refused_with_its_line_number(self) -> None:
        rows = [{"shop_id": "s1", "name": ""}]

        result = parse_shops(rows)

        self.assertEqual(result.shops, [])
        self.assertEqual(result.problems[0].row_number, 2)
        self.assertEqual(result.problems[0].field, "name")

    def test_unknown_source_kind_is_refused(self) -> None:
        rows = [
            {
                "shop_id": "s1",
                "name": "示例店",
                "source_reference": "x",
                "source_kind": "telepathy",
            }
        ]

        result = parse_shops(rows)

        self.assertEqual(result.shops, [])
        self.assertIn("不认识的来源类型", result.problems[0].detail)

    def test_type_declared_without_a_reference_is_refused(self) -> None:
        rows = [{"shop_id": "s1", "name": "示例店", "source_kind": "upload"}]

        result = parse_shops(rows)

        self.assertEqual(result.shops, [])
        self.assertIn("没有来源标识", result.problems[0].detail)

    def test_unparseable_distance_is_unknown_not_zero(self) -> None:
        rows = [{"shop_id": "s1", "name": "示例店", "distance_meters": "很远"}]

        result = parse_shops(rows, default_source=_source())

        self.assertIsNone(result.shops[0].distance_meters)

    def test_a_bad_row_does_not_discard_the_good_ones(self) -> None:
        rows = [
            {"shop_id": "s1", "name": "好店"},
            {"shop_id": "s2", "name": ""},
            {"shop_id": "s3", "name": "另一家好店"},
        ]

        result = parse_shops(rows, default_source=_source())

        self.assertEqual([shop.shop_id for shop in result.shops], ["s1", "s3"])
        self.assertEqual(len(result.problems), 1)


class ParseItemsTest(unittest.TestCase):
    def _row(self, **overrides) -> dict[str, str]:
        row = {
            "item_id": "i1",
            "shop_id": "s1",
            "name": "牛肉拉面",
            "composition": "拉面;牛肉;香菜",
            "portion": "大碗约 600 克",
            "flavours": "清汤牛肉汤底;面条偏硬",
            "flavour_detail": "辣子另给",
            "price_cny": "18",
            "price_basis": "menu",
        }
        row.update(overrides)
        return row

    def test_item_row_becomes_an_item(self) -> None:
        result = parse_items([self._row()], default_source=_source())

        self.assertEqual(len(result.items), 1)
        item = result.items[0]
        self.assertEqual(item.composition, ("拉面", "牛肉", "香菜"))
        self.assertEqual(item.portion, "大碗约 600 克")
        self.assertEqual(item.menu_prices[0].amount_cny, 18.0)

    def test_composition_and_portion_get_a_source_from_the_row(self) -> None:
        """A spreadsheet cell has no source of its own."""
        result = parse_items([self._row()], default_source=_source())

        item = result.items[0]
        self.assertIsNotNone(item.composition_source)
        self.assertIsNotNone(item.portion_source)

    def test_flavours_and_details_pair_up_by_position(self) -> None:
        result = parse_items([self._row()], default_source=_source())

        flavours = result.items[0].flavours
        self.assertEqual(flavours[0].detail, "辣子另给")
        self.assertEqual(flavours[1].detail, "")

    def test_price_range_is_supported(self) -> None:
        result = parse_items(
            [self._row(price_cny="", price_min_cny="12", price_max_cny="22")],
            default_source=_source(),
        )

        price = result.items[0].prices[0]
        self.assertEqual((price.min_cny, price.max_cny), (12.0, 22.0))

    def test_per_person_basis_is_preserved(self) -> None:
        result = parse_items(
            [self._row(price_basis="per_person")], default_source=_source()
        )

        item = result.items[0]
        self.assertEqual(item.menu_prices, ())
        self.assertIn("菜单价格", item.missing_evidence())

    def test_invalid_basis_is_refused_with_a_reason(self) -> None:
        result = parse_items(
            [self._row(price_basis="guess")], default_source=_source()
        )

        self.assertEqual(result.items[0].prices, ())
        self.assertIn("不合法", result.problems[0].detail)

    def test_item_without_a_price_has_no_price(self) -> None:
        result = parse_items(
            [self._row(price_cny="", price_basis="")], default_source=_source()
        )

        self.assertEqual(result.items[0].prices, ())
        self.assertEqual(result.problems, [])

    def test_missing_shop_id_is_refused(self) -> None:
        result = parse_items([self._row(shop_id="")], default_source=_source())

        self.assertEqual(result.items, [])
        self.assertEqual(result.problems[0].field, "shop_id")

    def test_multi_value_separator_tolerates_spaces_and_empties(self) -> None:
        result = parse_items(
            [self._row(composition=" 拉面 ; ; 牛肉 ")],
            default_source=_source(),
        )

        self.assertEqual(result.items[0].composition, ("拉面", "牛肉"))

    def test_suitable_meals_are_parsed(self) -> None:
        result = parse_items(
            [self._row(suitable_meals="午餐;晚餐")], default_source=_source()
        )

        self.assertEqual(result.items[0].suitable_meals, ("午餐", "晚餐"))


class ParseTableTest(TableTestCase):
    def test_shop_table_is_detected_from_its_headers(self) -> None:
        path = self._write("shops.csv", "shop_id,name,address\ns1,示例店,示例路 1 号\n")

        result = parse_table(path, default_source=_source())

        self.assertEqual(len(result.shops), 1)
        self.assertEqual(result.items, [])

    def test_item_table_is_detected_from_its_headers(self) -> None:
        path = self._write(
            "items.csv",
            "item_id,shop_id,name,price_cny\ni1,s1,牛肉拉面,18\n",
        )

        result = parse_table(path, default_source=_source())

        self.assertEqual(len(result.items), 1)

    def test_undetectable_table_is_refused_with_an_explanation(self) -> None:
        path = self._write("weird.csv", "foo,bar\n1,2\n")

        result = parse_table(path, default_source=_source())

        self.assertEqual(result.record_count, 0)
        self.assertTrue(any("无法判断" in problem.detail for problem in result.problems))

    def test_problems_from_reading_are_carried_through(self) -> None:
        result = parse_table(self.directory / "absent.csv")

        self.assertTrue(result.problems)
        self.assertIn("找不到文件", result.problems[0].detail)

    def test_summary_counts_records_and_refusals(self) -> None:
        path = self._write("shops.csv", "shop_id,name\ns1,示例店\ns2,\n")

        summary = parse_table(path, default_source=_source()).summary()

        self.assertIn("店铺 1 条", summary)
        self.assertIn("被拒 1 条", summary)

    def test_describe_problems_includes_line_numbers(self) -> None:
        path = self._write("shops.csv", "shop_id,name\ns1,示例店\ns2,\n")

        described = parse_table(path, default_source=_source()).describe_problems()

        self.assertIn("第 3 行", described)


class SourceSpecTest(unittest.TestCase):
    def test_spec_is_parsed(self) -> None:
        source = parse_source_spec("upload:2026-10-menu.csv")

        self.assertEqual(source.kind, SourceKind.UPLOAD)
        self.assertEqual(source.reference, "2026-10-menu.csv")

    def test_empty_spec_yields_nothing(self) -> None:
        self.assertIsNone(parse_source_spec(""))
        self.assertIsNone(parse_source_spec("   "))

    def test_missing_reference_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            parse_source_spec("upload:")

    def test_unknown_kind_is_refused_and_lists_the_options(self) -> None:
        with self.assertRaises(ValueError) as context:
            parse_source_spec("telepathy:abc")

        self.assertIn("不认识的来源类型", str(context.exception))
        self.assertIn("menu", str(context.exception))


class EndToEndTableImportTest(TableTestCase):
    def test_two_csv_files_feed_the_importer(self) -> None:
        """The whole point: spreadsheets become a catalogue through the importer."""
        from services.meal_catalog_import import CatalogImporter
        from services.nearby_meal_service import MealCatalog

        shops = self._write(
            "shops.csv",
            "shop_id,name,address,distance_meters\ns1,示例兰州拉面,示例路 1 号,320\n",
        )
        items = self._write(
            "items.csv",
            # The flavour column must carry real description: a bare six-character
            # phrase is below the contract's informativeness bar on purpose.
            "item_id,shop_id,name,composition,portion,flavours,flavour_detail,price_cny\n"
            "i1,s1,牛肉拉面,拉面;牛肉,大碗约 600 克,清汤牛肉汤底,面条偏硬且辣子另给,18\n",
        )
        source = _source("upload:menu-2026-10")

        shop_result = parse_table(shops, default_source=source)
        item_result = parse_table(items, default_source=source)
        catalog = MealCatalog()
        importer = CatalogImporter(catalog)
        preview = importer.preview(
            shops=tuple(shop_result.shops), items=tuple(item_result.items)
        )
        importer.apply(preview)

        self.assertEqual(catalog.shop_count, 1)
        item = catalog.items()[0]
        self.assertTrue(item.is_decision_ready())
        self.assertFalse(shop_result.problems)
        self.assertFalse(item_result.problems)


if __name__ == "__main__":
    unittest.main()
