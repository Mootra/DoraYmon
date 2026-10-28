"""A source-linked, branch-level city catalog, compatible with campus shop fields."""

import hashlib
import json
import re
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .catalog_rankings import RankingIndex, ranking_coverage

APPLICATION_ID = 0x44534352
SCHEMA_VERSION = 3

AREA_NAMES = {
    "Wanda Plaza/Qiushui Park": "红谷滩",
    "The Mixc/Qianhu University Town": "红谷滩/前湖",
    "Tengwang Pavilion/Wanshou Palace": "东湖/万寿宫",
    "Bayi Square/Wangfujing": "八一广场周边",
    "High-Tech Development Zone&East Nanchang Commercial Area": "高新/城东",
    "Hengmao Dream Times Square": "青山湖/梦时代",
    "Xihu wanda plaza/Zoo/Xinhongcheng market": "西湖万达/动物园/新洪城",
    "Nanchang Lvde Guoji Bolan Center": "绿地国博周边",
    "The North of Nanchang Higher Education Mega Center": "昌北高校区",
    "Yaohu University Town/Jiangxi Olympic Sports Center": "高新/瑶湖",
    "Xianghu Scenic Area": "象湖景区周边",
    "Xinjian Central District": "新建城区",
}
_AREA_NAMES = {key.casefold(): value for key, value in AREA_NAMES.items()}


def canonical_area(value: str) -> str:
    return _AREA_NAMES.get(value.casefold(), value)


def normalized(value: str) -> str:
    return re.sub(r"[\s()（）·,，.。/\\\-]", "", value).casefold()


CATEGORY_NAMES = {
    "Snacks": "小吃",
    "Quick bites": "快餐/简餐",
    "Jiangxi cuisine": "赣菜/家常菜",
    "Bakery": "甜品/烘焙",
    "Cafes": "咖啡/饮品",
    "Coffee shop": "咖啡/饮品",
    "Teahouses": "茶饮/饮品",
    "Hotpot": "火锅",
    "Western cuisine": "西餐",
    "Barbecue": "烧烤",
    "Buffet": "自助餐",
    "Cantonese cuisine": "粤菜",
    "Japanese": "日料",
    "Japanese cuisine": "日料",
    "Sichuan cuisine": "川菜",
    "Drinks": "饮品",
    "Desserts": "甜品",
    "Ice cream": "冰淇淋",
    "Noodles": "面食",
    "Seafood": "海鲜",
    "Korean cuisine": "韩餐",
    "Hunan cuisine": "湘菜",
    "Innovative cuisine": "创意菜",
    "Jiangzhe cuisine": "江浙菜",
    "Southeast Asian cuisine": "东南亚菜",
    "Farmhouse dishes": "农家菜",
    "Bars/bistros": "酒吧/小酒馆",
    "Beijing cuisine": "京菜",
    "Xinjiang cuisine": "新疆菜",
    "Fusion cuisine": "融合菜",
    "Northeastern Chinese cuisine": "东北菜",
    "Vegetarian": "素食",
    "Hakka cuisine": "客家菜",
    "Guizhou cuisine": "贵州菜",
}
_CATEGORY_NAMES = {key.casefold(): value for key, value in CATEGORY_NAMES.items()}


def canonical_category(value: str) -> str:
    value = value.strip()
    return _CATEGORY_NAMES.get(value.casefold(), value or "类别待核对")


def normalize_classification(record: dict, previous: dict) -> dict:
    record = dict(record)
    original = str(record.get("category") or "类别待核对")
    if canonical_category(original) == "类别待核对" and previous.get("category") not in {
        None,
        "",
        "类别待核对",
    }:
        original = previous["category"]
        for key in ("source_category", "category_basis", "food_group"):
            if key in previous:
                record[key] = previous[key]
    record["category"] = canonical_category(original)
    if original != record["category"]:
        record["source_category"] = original
    category_tags = (
        list(
            dict.fromkeys(p.strip() for p in re.split(r"[/、，,]", record["category"]) if p.strip())
        )
        if record["category"] != "类别待核对"
        else []
    )
    old_tags = set(previous.get("category_tags", [])) | set(record.get("category_tags", []))
    record["tags"] = list(
        dict.fromkeys(
            [
                *(tag for tag in previous.get("tags", []) if tag not in old_tags),
                *(tag for tag in record.get("tags", []) if tag not in old_tags),
                *category_tags,
            ]
        )
    )
    record["category_tags"] = category_tags
    return record


def food_group(name: str, category: str) -> str:
    text = (name + " " + category).casefold()
    if (
        any(word in text for word in ("奶茶", "茶饮", "咖啡", "果汁", "酸奶", "柠檬茶", "饮品"))
        or any(brand.casefold() in text for brand in BRANDS[:19])
        or re.search(r"\b(?:coffee|starbucks|tea|cafes?|teahouses?|drinks)\b", text)
    ):
        return "饮品"
    if any(
        word in text
        for word in (
            "蛋糕",
            "糕点",
            "糕饼",
            "烘焙",
            "甜品",
            "甜点",
            "bakery",
            "dessert",
            "ice cream",
        )
    ):
        return "甜品烘焙"
    if any(word in text for word in ("粉", "面", "简餐", "快餐", "汉堡", "quick bites", "noodle")):
        return "粉面简餐"
    if any(word in text for word in ("小吃", "卤", "炸串", "烧烤", "snack", "barbecue")):
        return "小吃"
    return "正餐"


BRANDS = (
    "蜜雪冰城",
    "瑞幸咖啡",
    "星巴克",
    "库迪咖啡",
    "幸运咖",
    "茶百道",
    "古茗",
    "霸王茶姬",
    "沪上阿姨",
    "益禾堂",
    "喜茶",
    "奈雪的茶",
    "一点点",
    "1点点",
    "甜啦啦",
    "CoCo都可",
    "书亦烧仙草",
    "柠季",
    "爷爷不泡茶",
    "肯德基",
    "麦当劳",
    "汉堡王",
    "必胜客",
    "海底捞",
    "周真真",
    "老三样",
    "堂瓦里",
    "谭十三",
    "刘老三",
    "回家吃饭",
    "小罗子汤店",
    "千百味",
    "柴米油盐",
    "母舅坐上",
    "炉忆",
    "合味原",
    "上座云间",
    "名厨世家",
    "如可",
    "食在鲜",
    "鲟阳渔村",
    "浔阳宴",
    "宴请江熙",
    "叶满厨",
    "UH祐禾",
    "川西坝子",
    "曾记羊羊虾庄",
    "鑫郭氏",
    "西塔老太太",
    "丰屋烧烤",
    "牛造的场",
    "三毛烧烤",
    "串美滋",
    "巷林烤肉",
    "破店肥哈",
    "东方一串",
    "就恰烤肉",
    "姜虎东白丁",
    "酒拾烤肉",
)


def brand_of(name: str) -> str:
    return next((brand for brand in BRANDS if brand in name), "")


def merge_rankings(*groups: list[dict]) -> list[dict]:
    merged = {}
    for group in groups:
        for ranking in group:
            key = (str(ranking.get("rankingId")), str(ranking.get("rankingNum")))
            merged[key] = {**merged.get(key, {}), **ranking}
    return list(merged.values())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class CatalogStore:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db:
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            version = (
                db.execute("PRAGMA application_id").fetchone()[0],
                db.execute("PRAGMA user_version").fetchone()[0],
            )
            if tables:
                if version != (APPLICATION_ID, SCHEMA_VERSION):
                    raise ValueError(
                        "Existing database is not a city catalog; it will not be overwritten."
                    )
                return
            db.executescript(
                """CREATE TABLE shops (
                id TEXT PRIMARY KEY, display_name TEXT NOT NULL, category TEXT NOT NULL,
                name_complete INTEGER NOT NULL, campus_scope TEXT, scope_basis TEXT NOT NULL,
                city TEXT NOT NULL, area_name TEXT NOT NULL, campus_id TEXT NOT NULL,
                listed_location TEXT NOT NULL, name_norm TEXT NOT NULL, address_norm TEXT NOT NULL,
                payload_json TEXT NOT NULL);
                CREATE TABLE external_ids (source_key TEXT PRIMARY KEY,
                    shop_id TEXT NOT NULL REFERENCES shops(id));
                CREATE INDEX shop_location ON shops(city, campus_id, area_name);
                CREATE INDEX shop_identity ON shops(city,name_norm,address_norm);
                """
            )
            db.execute(f"PRAGMA application_id={APPLICATION_ID}")
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            db.commit()

    def import_records(self, records: list[dict[str, Any]]) -> int:
        self.initialize()
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("PRAGMA foreign_keys=ON")
            for incoming in records:
                record = dict(incoming)
                name = str(record.get("display_name", "")).strip()
                city = str(record.get("city", "")).strip()
                source_key = str(record.get("source_key", "")).strip()
                if not name or not city or not source_key or not record.get("sources"):
                    raise ValueError(
                        "Every branch needs a name, city, source key and source evidence."
                    )
                address = str(record.get("listed_location", "")).strip()
                row = db.execute(
                    "SELECT shop_id FROM external_ids WHERE source_key=?", (source_key,)
                ).fetchone()
                if row is None and address and record.get("location_status") != "needs_review":
                    row = db.execute(
                        "SELECT id FROM shops WHERE city=? AND name_norm=? AND address_norm=?",
                        (city, normalized(name), normalized(address)),
                    ).fetchone()
                shop_id = row[0] if row else hashlib.sha256(source_key.encode()).hexdigest()
                old = db.execute("SELECT payload_json FROM shops WHERE id=?", (shop_id,)).fetchone()
                previous = json.loads(old[0]) if old else {}
                record = {**previous, **record, "id": shop_id}
                record["amap_id"] = record.get("amap_id") or previous.get("amap_id")
                sources = {
                    source.get("id") or source.get("url") or source.get("publisher"): source
                    for source in previous.get("sources", [])
                }
                sources.update(
                    {
                        source.get("id") or source.get("url") or source.get("publisher"): source
                        for source in record["sources"]
                    }
                )
                record["sources"] = list(sources.values())
                record["rankings"] = merge_rankings(
                    previous.get("rankings", []), record.get("rankings", [])
                )
                record["aliases"] = list(
                    filter(
                        None,
                        dict.fromkeys(
                            [
                                *previous.get("aliases", []),
                                *record.get("aliases", []),
                                previous.get("display_name", ""),
                            ]
                        ),
                    )
                )
                record["baseline"] = bool(previous.get("baseline") or record.get("baseline"))
                record.setdefault("category", "类别待核对")
                record = normalize_classification(record, previous)
                record.setdefault("name_complete", True)
                record.setdefault("campus_scope", None)
                record.setdefault("scope_basis", "按公开来源所列城市与位置记录")
                record.setdefault("area_name", "")
                original_area = record["area_name"]
                record["area_name"] = canonical_area(original_area)
                if record["area_name"] != original_area:
                    record.setdefault("source_area_name", original_area)
                record.setdefault("campus_id", "")
                record.setdefault("activity_status", "not_checked")
                record.setdefault("brand", brand_of(name))
                record.setdefault("food_group", food_group(name, record["category"]))
                record.setdefault(
                    "location_status", "listed_address" if address else "needs_review"
                )
                db.execute(
                    """INSERT INTO shops VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name,
                    category=excluded.category,name_complete=excluded.name_complete,
                    campus_scope=excluded.campus_scope,scope_basis=excluded.scope_basis,
                    city=excluded.city,area_name=excluded.area_name,campus_id=excluded.campus_id,
                    listed_location=excluded.listed_location,name_norm=excluded.name_norm,
                    address_norm=excluded.address_norm,payload_json=excluded.payload_json""",
                    (
                        shop_id,
                        name,
                        record["category"],
                        int(record["name_complete"]),
                        record["campus_scope"],
                        record["scope_basis"],
                        city,
                        record["area_name"],
                        record["campus_id"],
                        address,
                        normalized(name),
                        normalized(address),
                        json.dumps(record, ensure_ascii=False),
                    ),
                )
                db.execute("INSERT OR REPLACE INTO external_ids VALUES(?,?)", (source_key, shop_id))
            return len(records)

    def rows(self, city: str = "") -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            query = "SELECT payload_json FROM shops"
            values = ()
            if city:
                query += " WHERE city=?"
                values = (city,)
            return [json.loads(row[0]) for row in db.execute(query, values)]

    def export(
        self, destination: Path, tasks: list[dict] | None = None, *, complete: bool = False
    ) -> dict:
        shops = self.rows("南昌")
        index = RankingIndex.read(self.path.with_name("ranking-index.json"))
        coverage, hot = ranking_coverage(shops, index)
        drinks = [row for row in shops if row["food_group"] == "饮品"]
        drink_brands = list(dict.fromkeys("1点点" if b == "一点点" else b for b in BRANDS[:19]))
        brand_coverage = []
        for brand in drink_brands:
            rows = [
                row
                for row in drinks
                if ("1点点" if row.get("brand") == "一点点" else row.get("brand")) == brand
            ]
            brand_coverage.append(
                {
                    "brand": brand,
                    "imported": len(rows),
                    "areas": dict(Counter(row.get("area_name") or "待核对" for row in rows)),
                    "expected_total": None,
                    "roster_verified": False,
                    "updated_at": max((r.get("checked_at") or "" for r in rows), default=""),
                }
            )
        coverage.update(
            {
                "drink_brand_coverage": brand_coverage,
                "drink_area_coverage": dict(
                    Counter(row.get("area_name") or "待核对" for row in drinks)
                ),
                "drink_brands_without_records": [
                    row["brand"] for row in brand_coverage if not row["imported"]
                ],
                "drink_source_status": (
                    "咖啡与烘焙公开目录已读完，品牌与区域覆盖待核对"
                    if complete
                    or all(
                        any(
                            t.get("source_kind") == kind and t["status"] == "completed"
                            for t in tasks or []
                        )
                        for kind in ("cafes", "bakeries")
                    )
                    else "公开目录与分店覆盖尚未完成核对"
                ),
                "source_directory_complete": complete,
                "all_branches_verified": False,
            }
        )
        value = {
            "schema_version": 3,
            "generated_at": datetime.now(UTC).isoformat(),
            "scope": "南昌主城区（包含红谷滩和瑶湖）",
            "shops": shops,
            "coverage": coverage,
            "hot_candidates": hot[: coverage["hot_target"]],
            "ranking_candidates": hot,
            "tasks": [
                task
                for task in tasks or []
                if task.get("source_kind") not in {"ranking", "brand_coverage"}
            ]
            + index.tasks()
            + [
                {
                    "source_kind": "brand_coverage",
                    "title": "饮品分店 · " + row["brand"],
                    "status": "needs_review",
                    "imported": row["imported"],
                    "needs_review": None,
                    "updated_at": row["updated_at"],
                }
                for row in brand_coverage
            ],
        }
        write_json(destination, value)
        return value
