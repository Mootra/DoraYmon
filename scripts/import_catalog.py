"""Preview or apply a catalogue import from the command line.

The plan's import flow is: upload -> preview valid and invalid rows -> confirm ->
publish. This is the operator-facing half of that: it shows exactly what an
import would change, and only writes when asked, so a bad file is caught before
it reaches the live catalogue.

Usage:
    .venv/bin/python -m scripts.import_catalog --from incoming.json
    .venv/bin/python -m scripts.import_catalog --from incoming.json --apply
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import shutil
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.catalog_table import (
    XlsxUnavailable,
    parse_source_spec,
    parse_table,
)
from services.meal_catalog_import import CatalogImporter
from services.meal_catalog_loader import CATALOG_FILENAME, catalog_path, load_catalog, save_catalog
from services.nearby_meal_service import MealCatalog

TABLE_SUFFIXES = (".csv", ".tsv", ".xlsx")


def _load_json_incoming(path: Path) -> tuple[MealCatalog, list[str]]:
    """Read a JSON candidate through the same strict loader as the live one."""
    with tempfile.TemporaryDirectory(prefix="catalog-import-") as temp:
        target = Path(temp) / CATALOG_FILENAME
        target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        result = load_catalog(temp)
        return result.catalog, result.problems


def _load_table_incoming(
    path: Path,
    *,
    table: str,
    sheet: str,
    default_source: str,
    known_shops: tuple = (),
) -> tuple[MealCatalog, list[str]]:
    """Read a spreadsheet candidate into a catalogue, naming every refusal.

    Shops and items usually arrive as two separate files, so an items table
    normally references shops that already live in the catalogue rather than in
    the same file. ``known_shops`` carries those in: without them every item in an
    items file would be rejected as referencing an unknown shop. They are held as
    references only, so the catalogue stays able to resolve an item's shop.
    """
    source_ref = parse_source_spec(default_source)
    parsed = parse_table(path, table=table, sheet=sheet, default_source=source_ref)
    problems = [problem.describe() for problem in parsed.problems]

    catalogue = MealCatalog()
    for shop in known_shops:
        catalogue.add_shop(shop)
    for shop in parsed.shops:
        catalogue.add_shop(shop)

    for item in parsed.items:
        if catalogue.shop_by_id(item.shop_id) is None:
            problems.append(
                f"餐点 {item.item_id} 引用了目录里没有的店铺 {item.shop_id}"
            )
            continue
        catalogue.add_item(item)
    return catalogue, problems


def main() -> int:
    parser = argparse.ArgumentParser(description="门店/餐点目录导入（默认只预览）")
    parser.add_argument(
        "--from",
        dest="source",
        type=Path,
        required=True,
        help="待导入文件：JSON 目录，或店铺/餐点表格（.csv/.tsv/.xlsx）",
    )
    parser.add_argument("--dir", type=Path, default=PROJECT_ROOT / "data" / "meal_catalog", help="现有目录所在目录")
    parser.add_argument("--apply", action="store_true", help="确认写入；缺省只预览")
    parser.add_argument(
        "--source",
        dest="default_source",
        default="",
        help="表格缺来源列时的默认来源，写成 类型:标识，例如 upload:menu-2026-10.csv",
    )
    parser.add_argument(
        "--table",
        choices=("", "shops", "items"),
        default="",
        help="表格类型；缺省按表头自动判断",
    )
    parser.add_argument("--sheet", default="", help="XLSX 工作表名；缺省用第一个")
    args = parser.parse_args()

    if not args.source.exists():
        print(f"找不到待导入文件：{args.source}")
        return 1

    current = load_catalog(args.dir)
    suffix = args.source.suffix.lower()
    if suffix in TABLE_SUFFIXES:
        try:
            incoming, problems = _load_table_incoming(
                args.source,
                table=args.table,
                sheet=args.sheet,
                default_source=args.default_source,
                known_shops=tuple(current.catalog.shops()),
            )
        except XlsxUnavailable as error:
            print(str(error))
            return 1
        except ValueError as error:
            print(f"无法导入：{error}")
            return 1
    else:
        incoming, problems = _load_json_incoming(args.source)

    if problems:
        print(f"待导入文件有 {len(problems)} 处问题，这些记录不会进入目录：")
        for problem in problems:
            print(f"  - {problem}")

    if incoming.shop_count == 0 and incoming.item_count == 0:
        print("待导入文件没有可用记录，未做任何改动。")
        if suffix in TABLE_SUFFIXES and not args.default_source:
            print(
                "提示：表格若没有 source_reference 列，需要给一个默认来源，"
                "例如 --source upload:menu-2026-10.csv"
            )
        return 1

    importer = CatalogImporter(current.catalog)
    preview = importer.preview(shops=incoming.shops(), items=incoming.items())
    print(f"预览：{preview.summary()}")
    for record in preview.additions:
        print(f"  新增：{record.record_id}")
    for record in preview.replacements:
        print(f"  覆盖：{record.record_id}")
    if preview.issues:
        print("被拒绝的行：")
        for line in preview.describe_issues().splitlines():
            print(f"  {line}")

    if not preview.is_applicable:
        print("没有需要写入的改动。")
        return 0

    if not args.apply:
        print(f"这是预览，未写入。确认后加 --apply 写入 {catalog_path(args.dir)}。")
        return 0

    # Back up before writing: an in-memory batch record dies with this process,
    # so a durable copy of the previous catalogue is what actually makes the
    # import reversible.
    backup = _backup_existing(args.dir, preview.batch_id)
    batch = importer.apply(preview)
    save_catalog(current.catalog, args.dir)
    print(f"已写入：批次 {batch.batch_id}")
    print(f"  新增店铺 {len(batch.added_shops)} 家，餐点 {len(batch.added_items)} 个")
    print(f"  覆盖店铺 {len(batch.replaced_shops)} 家，餐点 {len(batch.replaced_items)} 个")
    print(f"  目录现为：{current.catalog.shop_count} 家店，{current.catalog.item_count} 个餐点")
    if backup is not None:
        print(f"  导入前的目录已备份：{backup}")
        print(f"  撤回：把该文件复制回 {catalog_path(args.dir)}")
    else:
        print("  此前没有目录文件，撤回即删除本次生成的目录文件。")
    return 0


def _backup_existing(directory: Path, batch_id: str) -> Path | None:
    """Copy the current catalogue aside, so this import can be undone."""
    current_file = catalog_path(directory)
    if not current_file.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = directory / f"{current_file.stem}.backup-{stamp}-{batch_id}.json"
    shutil.copy2(current_file, backup)
    return backup


if __name__ == "__main__":
    raise SystemExit(main())
