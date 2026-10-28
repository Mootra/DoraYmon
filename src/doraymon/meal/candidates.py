"""Read the campus shop catalog and draw choices for one meal decision."""

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

CampusScope = Literal["校内", "校外", "不限"]
CATEGORY_TERMS = {
    "粉面": ("粉", "面"),
    "米饭": ("饭",),
    "家常菜": ("家常", "炒菜", "小炒", "赣菜"),
    "炸鸡汉堡": ("炸鸡", "汉堡", "鸡排", "鸡柳", "炸物"),
    "小吃": ("小吃", "炸串", "关东煮", "卤味", "包点", "早餐", "鸭脖", "鸭货"),
    "麻辣烫香锅": ("麻辣烫", "麻辣拌", "麻辣香锅"),
    "饮品": ("茶饮", "咖啡", "饮品"),
    "奶茶": ("茶饮", "奶茶", "蜜雪冰城", "茶百道", "古茗", "霸王茶姬", "沪上阿姨", "喜茶"),
    "咖啡": ("咖啡", "瑞幸", "星巴克", "库迪", "幸运咖"),
    "甜品": ("蛋糕", "冰淇淋", "甜品", "甜点", "烘焙", "面包"),
    "日料": ("日料", "日式", "寿司", "鳗鱼"),
}
DEFAULT_LOCATION = "江西师范大学瑶湖校区"
LOCATION_ALIASES = {
    DEFAULT_LOCATION: (DEFAULT_LOCATION, "江西师范大学", "师大瑶湖", "瑶湖校区"),
    "瑶湖": ("瑶湖", "瑶湖片区", "南昌瑶湖"),
    "南昌": ("南昌", "南昌市", "南昌主城区"),
    "红谷滩": ("红谷滩", "红谷滩区", "南昌红谷滩"),
    "前湖": ("前湖", "前湖大学城"),
    "长江大学东校区": ("长江大学东校区", "长大东校区", "长江大学东区"),
    "荆州": ("荆州", "荆州市"),
}


def canonical_location(value: str, previous: str = "") -> str:
    value = value.strip()
    if value in {"长江大学", "长大"}:
        if previous == "长江大学东校区":
            return previous
        raise CandidateError("ambiguous_campus")
    for name, aliases in LOCATION_ALIASES.items():
        if value in aliases:
            return name
    raise CandidateError("unsupported_location")


class CampusRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    campus_scope: CampusScope | None = None
    category: str | None = Field(default=None, max_length=40)
    location: str | None = Field(default=None, max_length=80)
    count: int = Field(default=1, ge=1, le=3)
    change: Literal["none", "another", "reset", "cancel"] = "none"


class CampusShop(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=200)
    campus_scope: Literal["校内", "校外"] | None
    scope_basis: str = Field(max_length=300)
    city: str = ""
    area_name: str = ""
    listed_location: str = ""
    source_urls: tuple[str, ...] = ()
    checked_at: str = ""


@dataclass(frozen=True)
class CampusState:
    campus_scope: CampusScope = "不限"
    category: str = ""
    seen: tuple[str, ...] = ()
    location: str = DEFAULT_LOCATION


@dataclass(frozen=True)
class CampusResult:
    state: CampusState
    shops: tuple[CampusShop, ...]
    total: int


class CandidateError(Exception):
    """A stable catalog error code; source paths do not cross the tool boundary."""


class CampusCatalog:
    def __init__(self, path: Path, *, default_location: str = DEFAULT_LOCATION):
        self.path = path
        self.default_location = default_location

    def pick(self, state: CampusState | None, request: CampusRequest) -> CampusResult:
        state = state or CampusState(location=self.default_location)
        location = canonical_location(request.location or state.location, state.location)
        scope = request.campus_scope or state.campus_scope
        category = request.category.strip() if request.category is not None else state.category
        if (scope, category, location) != (
            state.campus_scope,
            state.category,
            state.location,
        ) or request.change == "reset":
            state = CampusState(campus_scope=scope, category=category, location=location)
        clauses, values = ["name_complete=1"], []
        if scope != "不限":
            clauses.append("campus_scope=?")
            values.append(scope)
        try:
            with closing(
                sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
            ) as connection:
                connection.row_factory = sqlite3.Row
                application, version = (
                    connection.execute("PRAGMA application_id").fetchone()[0],
                    connection.execute("PRAGMA user_version").fetchone()[0],
                )
                if application != 0x44534352 or version not in {2, 3}:
                    raise CandidateError("unsupported_catalog")
                columns = "id, display_name, category, campus_scope, scope_basis"
                category_expression = "category"
                if version == 2:
                    if location not in {DEFAULT_LOCATION, "瑶湖", "南昌"}:
                        clauses.append("0=1")
                else:
                    columns += ",city,area_name,listed_location,payload_json"
                    city = "荆州" if location in {"荆州", "长江大学东校区"} else "南昌"
                    clauses.append("city=?")
                    values.append(city)
                    if location in {DEFAULT_LOCATION, "瑶湖"}:
                        clauses.append("(campus_id='jxnu_yaohu' OR instr(area_name,'瑶湖')>0)")
                    elif location == "长江大学东校区":
                        clauses.append("campus_id='yangtze_east'")
                    elif location in {"红谷滩", "前湖"}:
                        clauses.append("instr(area_name,?)>0")
                        values.append(location)
                    clauses.append(
                        "COALESCE(json_extract(payload_json,'$.activity_status'),'') "
                        "NOT IN ('historical','closed',"
                        "'source_reports_suspended','source_reports_closed')"
                    )
                    category_expression += (
                        " || ' ' || display_name || ' ' || "
                        "COALESCE(json_extract(payload_json,'$.food_group'),'')"
                    )
                if category:
                    terms = CATEGORY_TERMS.get(category, (category,))
                    clauses.append(
                        "(" + " OR ".join(f"instr({category_expression}, ?)>0" for _ in terms) + ")"
                    )
                    values.extend(terms)
                where = " AND ".join(clauses)
                total = connection.execute(
                    f"SELECT COUNT(*) FROM shops WHERE {where}", values
                ).fetchone()[0]
                excluded = ""
                if state.seen:
                    excluded = " AND id NOT IN (" + ",".join("?" for _ in state.seen) + ")"
                rows = connection.execute(
                    f"SELECT {columns} "
                    f"FROM shops WHERE {where}{excluded} ORDER BY random() LIMIT ?",
                    (*values, *state.seen, request.count),
                ).fetchall()
                entries = []
                for row in rows:
                    entry = dict(row)
                    if version == 3:
                        data = json.loads(entry.pop("payload_json"))
                        entry["checked_at"] = data.get("checked_at", "")
                        entry["source_urls"] = tuple(
                            source["url"]
                            for source in data.get("sources", [])
                            if isinstance(source, dict)
                            and str(source.get("url", "")).startswith(("https://", "http://"))
                        )[:3]
                    entries.append(CampusShop.model_validate(entry))
                shops = tuple(entries)
        except (OSError, sqlite3.Error, ValidationError) as error:
            raise CandidateError("catalog_unavailable") from error
        seen = (*state.seen, *(shop.id for shop in shops))[-256:]
        return CampusResult(CampusState(scope, category, seen, location), shops, total)
