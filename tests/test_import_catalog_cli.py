"""The import CLI is the operator-facing path, so it gets its own coverage."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.import_catalog import main
from services.meal_catalog_loader import load_catalog


def _run(argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with patch("sys.argv", ["import_catalog", *argv]), redirect_stdout(buffer):
        code = main()
    return code, buffer.getvalue()


class ImportCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.target = self.root / "catalog"
        self.shops = self.root / "shops.csv"
        self.items = self.root / "items.csv"
        self.shops.write_text(
            "shop_id,name,address,distance_meters\n"
            "s1,示例兰州拉面,示例路 1 号,320\n",
            encoding="utf-8",
        )
        self.items.write_text(
            "item_id,shop_id,name,composition,portion,flavours,flavour_detail,price_cny\n"
            "i1,s1,牛肉拉面,拉面;牛肉,大碗约 600 克,清汤牛肉汤底,面条偏硬辣子另给,18\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _import_shops(self) -> None:
        code, _ = _run(
            [
                "--from", str(self.shops),
                "--dir", str(self.target),
                "--source", "upload:menu.csv",
                "--apply",
            ]
        )
        self.assertEqual(code, 0)

    def test_missing_file_is_reported(self) -> None:
        code, output = _run(["--from", str(self.root / "nope.csv"), "--dir", str(self.target)])

        self.assertEqual(code, 1)
        self.assertIn("找不到待导入文件", output)

    def test_preview_does_not_write(self) -> None:
        code, output = _run(
            ["--from", str(self.shops), "--dir", str(self.target), "--source", "upload:menu.csv"]
        )

        self.assertEqual(code, 0)
        self.assertIn("预览", output)
        self.assertFalse((self.target / "meal_catalog.json").exists())

    def test_apply_writes_the_catalogue(self) -> None:
        self._import_shops()

        loaded = load_catalog(self.target)
        self.assertEqual(loaded.catalog.shop_count, 1)
        self.assertFalse(loaded.problems)

    def test_items_file_uses_shops_already_in_the_catalogue(self) -> None:
        """Shops and items arrive as separate files in normal use."""
        self._import_shops()

        code, output = _run(
            [
                "--from", str(self.items),
                "--dir", str(self.target),
                "--source", "upload:menu.csv",
                "--apply",
            ]
        )

        self.assertEqual(code, 0)
        loaded = load_catalog(self.target)
        self.assertEqual(loaded.catalog.item_count, 1)
        self.assertTrue(loaded.catalog.items()[0].is_decision_ready())
        self.assertIn("餐点 1 个", output)

    def test_items_without_their_shops_are_refused_with_a_reason(self) -> None:
        code, output = _run(
            [
                "--from", str(self.items),
                "--dir", str(self.target),
                "--source", "upload:menu.csv",
            ]
        )

        self.assertEqual(code, 1)
        self.assertIn("没有可用记录", output)
        self.assertIn("没有的店铺", output)

    def test_missing_default_source_is_explained(self) -> None:
        """A table with no source column needs one, and must say so."""
        code, output = _run(["--from", str(self.shops), "--dir", str(self.target)])

        self.assertEqual(code, 1)
        self.assertIn("缺少来源", output)
        self.assertIn("--source", output)

    def test_bad_default_source_spec_is_reported(self) -> None:
        code, output = _run(
            ["--from", str(self.shops), "--dir", str(self.target), "--source", "telepathy:abc"]
        )

        self.assertEqual(code, 1)
        self.assertIn("不认识的来源类型", output)

    def test_backup_is_created_before_overwriting(self) -> None:
        self._import_shops()
        changed = self.root / "shops2.csv"
        changed.write_text(
            "shop_id,name,address,distance_meters\ns1,示例兰州拉面（改名）,示例路 1 号,320\n",
            encoding="utf-8",
        )

        code, output = _run(
            [
                "--from", str(changed),
                "--dir", str(self.target),
                "--source", "upload:menu.csv",
                "--apply",
            ]
        )

        self.assertEqual(code, 0)
        self.assertIn("已备份", output)
        backups = list(self.target.glob("meal_catalog.backup-*.json"))
        self.assertEqual(len(backups), 1)
        # The backup holds the pre-import version.
        self.assertIn("示例兰州拉面", backups[0].read_text(encoding="utf-8"))

    def test_reapplying_the_same_file_makes_no_changes(self) -> None:
        self._import_shops()

        code, output = _run(
            ["--from", str(self.shops), "--dir", str(self.target), "--source", "upload:menu.csv"]
        )

        self.assertEqual(code, 0)
        self.assertIn("没有需要写入的改动", output)

    def test_json_import_still_works(self) -> None:
        json_file = self.root / "incoming.json"
        json_file.write_text(
            '{"shops": [{"shop_id": "j1", "name": "JSON 店", '
            '"source": {"kind": "upload", "reference": "x"}}], "items": []}',
            encoding="utf-8",
        )

        code, _ = _run(
            ["--from", str(json_file), "--dir", str(self.target), "--apply"]
        )

        self.assertEqual(code, 0)
        self.assertIsNotNone(load_catalog(self.target).catalog.shop_by_id("j1"))


if __name__ == "__main__":
    unittest.main()
