"""Read a spreadsheet-shaped catalogue: CSV today, XLSX when openpyxl exists.

Operators keep menus in a spreadsheet, not in JSON, so P2a's import needs a
tabular path. Two decisions shape this module:

- CSV is handled with the standard library, so the import works with no new
  dependency. XLSX is optional: if openpyxl is missing the reader says so instead
  of failing obscurely or, worse, importing nothing and reporting success.
- A row is never silently dropped. Every rejected row comes back with its line
  number and a reason, and the reason names the field, because a menu import that
  quietly loses rows is how a user ends up being told about a shop that has no
  items.

Column contract (header row required, case-insensitive, extra columns ignored):

    shops:  shop_id, name, address, distance_meters, map_url, open_now,
            source_kind, source_reference, source_fetched_at
    items:  item_id, shop_id, name, composition, portion,
            flavours, flavour_detail, price_cny, price_min_cny, price_max_cny,
            price_basis, suitable_meals,
            plus the same source_* columns

Multi-value cells are separated by ``;``. Source columns may be omitted entirely
when the importer is given a default source, which is what an operator uploading
their own menu means.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
import io
from pathlib import Path
from typing import Any, Iterable

from services.recommendation_types import (
    FlavourNote,
    MealItem,
    PriceRecord,
    Shop,
    SourceKind,
    SourceRef,
)

MULTI_VALUE_SEPARATOR = ";"
SUPPORTED_SUFFIXES = (".csv", ".tsv", ".xlsx")

TABLE_SHOPS = "shops"
TABLE_ITEMS = "items"
TABLE_UNKNOWN = "unknown"

SHOP_REQUIRED = ("shop_id", "name")
ITEM_REQUIRED = ("item_id", "shop_id", "name")

DEFAULT_PRICE_BASIS = "menu"


@dataclass(frozen=True)
class TableProblem:
    """One rejected row or field."""

    row_number: int
    field: str
    detail: str

    def describe(self) -> str:
        location = f"第 {self.row_number} 行" if self.row_number else "表头"
        return f"{location}（{self.field}）：{self.detail}"


@dataclass
class TableReadResult:
    """Rows read from a file, plus what could not be read."""

    rows: list[dict[str, str]] = field(default_factory=list)
    problems: list[TableProblem] = field(default_factory=list)
    table: str = TABLE_UNKNOWN
    sheet: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.rows


@dataclass
class TableParseResult:
    """Contracts built from rows, plus what was refused."""

    shops: list[Shop] = field(default_factory=list)
    items: list[MealItem] = field(default_factory=list)
    problems: list[TableProblem] = field(default_factory=list)

    @property
    def record_count(self) -> int:
        return len(self.shops) + len(self.items)

    def summary(self) -> str:
        parts = [f"店铺 {len(self.shops)} 条", f"餐点 {len(self.items)} 条"]
        if self.problems:
            parts.append(f"被拒 {len(self.problems)} 条")
        return "，".join(parts)

    def describe_problems(self, limit: int = 10) -> str:
        return "\n".join(problem.describe() for problem in self.problems[:limit])


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


class XlsxUnavailable(Exception):
    """XLSX was requested but the optional reader is not installed."""


def detect_table(headers: Iterable[str]) -> str:
    """Decide whether a header row describes shops or items."""
    normalized = {str(header or "").strip().lower() for header in headers}
    if {"item_id"} <= normalized or {"shop_id", "name"} <= normalized:
        if "item_id" in normalized:
            return TABLE_ITEMS
        return TABLE_SHOPS
    return TABLE_UNKNOWN


def read_table(path: Path | str, *, sheet: str = "") -> TableReadResult:
    """Read a CSV/TSV/XLSX file into rows keyed by header."""
    target = Path(path)
    suffix = target.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        return TableReadResult(
            problems=[TableProblem(0, "文件", f"不支持的格式：{suffix or '无扩展名'}")]
        )
    if not target.exists():
        return TableReadResult(problems=[TableProblem(0, "文件", f"找不到文件：{target}")])
    if suffix == ".xlsx":
        return _read_xlsx(target, sheet=sheet)
    return _read_delimited(target, delimiter="\t" if suffix == ".tsv" else ",")


def _read_delimited(path: Path, *, delimiter: str) -> TableReadResult:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as error:
        return TableReadResult(problems=[TableProblem(0, "文件", f"无法读取：{error}")])
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if reader.fieldnames is None:
        return TableReadResult(problems=[TableProblem(0, "表头", "文件没有表头")])
    headers = [str(name or "").strip().lower() for name in reader.fieldnames]
    rows: list[dict[str, str]] = []
    for _index, raw in enumerate(reader, start=2):
        row = {}
        for original, normalized in zip(reader.fieldnames, headers):
            row[normalized] = str(raw.get(original, "") or "").strip()
        if not any(row.values()):
            # A blank line is not a rejected record.
            continue
        rows.append(row)
    return TableReadResult(rows=rows, table=detect_table(headers))


def _read_xlsx(path: Path, *, sheet: str) -> TableReadResult:
    try:
        from openpyxl import load_workbook  # type: ignore
    except ImportError as error:
        raise XlsxUnavailable(
            "读取 XLSX 需要 openpyxl：pip install openpyxl；也可以先把表格另存为 CSV"
        ) from error

    workbook = load_workbook(filename=str(path), read_only=True, data_only=True)
    try:
        worksheet = workbook[sheet] if sheet else workbook[workbook.sheetnames[0]]
        rows_iter = worksheet.iter_rows(values_only=True)
        header_row = next(rows_iter, None)
        if header_row is None:
            return TableReadResult(
                problems=[TableProblem(0, "表头", "工作表是空的")], sheet=worksheet.title
            )
        headers = [str(value or "").strip().lower() for value in header_row]
        rows: list[dict[str, str]] = []
        for index, values in enumerate(rows_iter, start=2):
            row = {
                header: str(value).strip() if value is not None else ""
                for header, value in zip(headers, values)
                if header
            }
            if not any(row.values()):
                continue
            rows.append(row)
        return TableReadResult(
            rows=rows, table=detect_table(headers), sheet=worksheet.title
        )
    finally:
        workbook.close()


def list_sheets(path: Path | str) -> list[str]:
    try:
        from openpyxl import load_workbook  # type: ignore
    except ImportError as error:
        raise XlsxUnavailable(
            "读取 XLSX 需要 openpyxl：pip install openpyxl；也可以先把表格另存为 CSV"
        ) from error
    workbook = load_workbook(filename=str(Path(path)), read_only=True)
    try:
        return list(workbook.sheetnames)
    finally:
        workbook.close()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _split_values(raw: str) -> tuple[str, ...]:
    text = str(raw or "").strip()
    if not text:
        return ()
    return tuple(
        part.strip()
        for part in text.split(MULTI_VALUE_SEPARATOR)
        if part.strip()
    )


def _optional_int(raw: str) -> int | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _optional_float(raw: str) -> float | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _optional_bool(raw: str) -> bool | None:
    text = str(raw or "").strip().lower()
    if not text:
        return None
    if text in {"1", "true", "yes", "y", "是", "开"}:
        return True
    if text in {"0", "false", "no", "n", "否", "关"}:
        return False
    return None


def _source_from_row(
    row: dict[str, str],
    *,
    default_source: SourceRef | None,
    default_reference: str,
    row_number: int,
    problems: list[TableProblem],
) -> SourceRef | None:
    """Build a source from the row, or fall back to the supplied default."""
    reference = str(row.get("source_reference", "") or "").strip()
    kind_text = str(row.get("source_kind", "") or "").strip().lower()
    fetched_text = str(row.get("source_fetched_at", "") or "").strip()

    if not reference and not kind_text and not fetched_text:
        if default_source is not None:
            return default_source
        problems.append(
            TableProblem(
                row_number,
                "source_reference",
                f"缺少来源，且没有提供默认来源（如 --source upload:{default_reference}）",
            )
        )
        return None

    if not reference:
        problems.append(
            TableProblem(row_number, "source_reference", "写了来源类型但没有来源标识")
        )
        return None
    try:
        kind = SourceKind(kind_text) if kind_text else SourceKind.UPLOAD
    except ValueError:
        problems.append(
            TableProblem(row_number, "source_kind", f"不认识的来源类型：{kind_text}")
        )
        return None
    fetched_at = _parse_fetched_at(fetched_text)
    try:
        return SourceRef(kind=kind, reference=reference, fetched_at=fetched_at)
    except ValueError as error:
        problems.append(TableProblem(row_number, "source_reference", str(error)))
        return None


def _parse_fetched_at(raw: str) -> datetime:
    text = str(raw or "").strip()
    if not text:
        return datetime.now(timezone.utc).replace(microsecond=0)
    for pattern in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            parsed = datetime.strptime(text, pattern)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_shops(
    rows: list[dict[str, str]],
    *,
    default_source: SourceRef | None = None,
    row_offset: int = 0,
) -> TableParseResult:
    result = TableParseResult()
    for index, row in enumerate(rows):
        row_number = row_offset + index + 2
        missing = [field for field in SHOP_REQUIRED if not str(row.get(field, "")).strip()]
        if missing:
            result.problems.append(
                TableProblem(row_number, missing[0], "必填字段为空")
            )
            continue
        shop_id = str(row["shop_id"]).strip()
        source = _source_from_row(
            row,
            default_source=default_source,
            default_reference=shop_id,
            row_number=row_number,
            problems=result.problems,
        )
        if source is None:
            continue
        try:
            result.shops.append(
                Shop(
                    shop_id=shop_id,
                    name=str(row["name"]).strip(),
                    source=source,
                    address=str(row.get("address", "") or "").strip(),
                    map_url=str(row.get("map_url", "") or "").strip(),
                    distance_meters=_optional_int(row.get("distance_meters", "")),
                    open_now=_optional_bool(row.get("open_now", "")),
                )
            )
        except ValueError as error:
            result.problems.append(TableProblem(row_number, "shop_id", str(error)))
    return result


def parse_items(
    rows: list[dict[str, str]],
    *,
    default_source: SourceRef | None = None,
    row_offset: int = 0,
) -> TableParseResult:
    result = TableParseResult()
    for index, row in enumerate(rows):
        row_number = row_offset + index + 2
        missing = [field for field in ITEM_REQUIRED if not str(row.get(field, "")).strip()]
        if missing:
            result.problems.append(TableProblem(row_number, missing[0], "必填字段为空"))
            continue
        item_id = str(row["item_id"]).strip()
        shop_id = str(row["shop_id"]).strip()
        source = _source_from_row(
            row,
            default_source=default_source,
            default_reference=item_id,
            row_number=row_number,
            problems=result.problems,
        )
        if source is None:
            continue

        composition = _split_values(row.get("composition", ""))
        portion = str(row.get("portion", "") or "").strip()
        flavours = _build_flavours(row, source, row_number, result.problems)
        prices = _build_prices(row, row_number, result.problems)

        try:
            result.items.append(
                MealItem(
                    item_id=item_id,
                    shop_id=shop_id,
                    name=str(row["name"]).strip(),
                    source=source,
                    # Composition and portion reuse the item's source, because a
                    # spreadsheet cell has no source of its own and inventing a
                    # separate one would misattribute the fact.
                    composition=composition,
                    composition_source=source if composition else None,
                    portion=portion,
                    portion_source=source if portion else None,
                    flavours=flavours,
                    prices=prices,
                    suitable_meals=_split_values(row.get("suitable_meals", "")),
                )
            )
        except ValueError as error:
            result.problems.append(TableProblem(row_number, "item_id", str(error)))
    return result


def _build_flavours(
    row: dict[str, str],
    source: SourceRef,
    row_number: int,
    problems: list[TableProblem],
) -> tuple[FlavourNote, ...]:
    descriptions = _split_values(row.get("flavours", ""))
    if not descriptions:
        return ()
    details = _split_values(row.get("flavour_detail", ""))
    notes: list[FlavourNote] = []
    for index, description in enumerate(descriptions):
        notes.append(
            FlavourNote(
                description=description,
                detail=details[index] if index < len(details) else "",
                source=source,
            )
        )
    return tuple(notes)


def _build_prices(
    row: dict[str, str],
    row_number: int,
    problems: list[TableProblem],
) -> tuple[PriceRecord, ...]:
    amount = _optional_float(row.get("price_cny", ""))
    minimum = _optional_float(row.get("price_min_cny", ""))
    maximum = _optional_float(row.get("price_max_cny", ""))
    if amount is None and minimum is None and maximum is None:
        return ()
    basis = str(row.get("price_basis", "") or "").strip().lower() or DEFAULT_PRICE_BASIS
    try:
        record = PriceRecord(
            basis=basis,
            source=_price_source(row, row_number),
            amount_cny=amount,
            min_cny=minimum,
            max_cny=maximum,
        )
    except ValueError as error:
        problems.append(TableProblem(row_number, "price_cny", f"价格不合法：{error}"))
        return ()
    return (record,)


def _price_source(row: dict[str, str], row_number: int) -> SourceRef:
    """The price needs its own attribution, defaulting to the row's own source."""
    reference = (
        str(row.get("price_reference", "") or "").strip()
        or str(row.get("source_reference", "") or "").strip()
        or f"row:{row_number}"
    )
    return SourceRef(
        kind=SourceKind.MENU,
        reference=reference,
        fetched_at=_parse_fetched_at(row.get("source_fetched_at", "")),
    )


def parse_table(
    path: Path | str,
    *,
    table: str = "",
    sheet: str = "",
    default_source: SourceRef | None = None,
) -> TableParseResult:
    """Read and parse one table, auto-detecting whether it holds shops or items."""
    read = read_table(path, sheet=sheet)
    result = TableParseResult(problems=list(read.problems))
    if read.is_empty:
        return result
    kind = table or read.table
    if kind == TABLE_SHOPS:
        parsed = parse_shops(read.rows, default_source=default_source)
    elif kind == TABLE_ITEMS:
        parsed = parse_items(read.rows, default_source=default_source)
    else:
        result.problems.append(
            TableProblem(
                1,
                "表头",
                "无法判断这是店铺表还是餐点表：店铺表需要 shop_id/name，"
                "餐点表需要 item_id/shop_id/name",
            )
        )
        return result
    result.shops.extend(parsed.shops)
    result.items.extend(parsed.items)
    result.problems.extend(parsed.problems)
    return result


def parse_source_spec(spec: str) -> SourceRef | None:
    """Parse ``kind:reference`` into a default source, or None when empty."""
    text = str(spec or "").strip()
    if not text:
        return None
    kind_text, _, reference = text.partition(":")
    kind_text = kind_text.strip().lower()
    reference = reference.strip()
    if not reference:
        raise ValueError("默认来源需要写成 类型:标识，例如 upload:2026-10-menu.csv")
    try:
        kind = SourceKind(kind_text) if kind_text else SourceKind.UPLOAD
    except ValueError as error:
        raise ValueError(
            f"不认识的来源类型：{kind_text}（可用：{'、'.join(k.value for k in SourceKind)}）"
        ) from error
    return SourceRef(
        kind=kind, reference=reference, fetched_at=datetime.now(timezone.utc).replace(microsecond=0)
    )
