from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CATALOG_PATH = PROJECT_ROOT / "data" / "food_catalog.example.json"


@dataclass(frozen=True)
class FoodItem:
    name: str
    tags: tuple[str, ...]
    moods: tuple[str, ...]
    flavors: tuple[str, ...]
    scenes: tuple[str, ...]
    budget_min: int
    budget_max: int


@dataclass(frozen=True)
class FoodConditions:
    budget_max: int | None
    moods: tuple[str, ...]
    flavors: tuple[str, ...]
    scenes: tuple[str, ...]
    desired_tags: tuple[str, ...]
    remembered_flavors: tuple[str, ...] = ()
    remembered_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class FoodRecommendation:
    food_name: str
    reason: str

    @property
    def text(self) -> str:
        return f"今天吃{self.food_name}。{self.reason}"


MOOD_KEYWORDS = {
    "累": ("累", "疲惫", "没精神", "不想动"),
    "烦躁": ("烦", "烦躁", "生气", "压力大"),
    "低落": ("低落", "难过", "不开心", "心情不好"),
    "开心": ("开心", "高兴", "庆祝", "心情好"),
}

FLAVOR_KEYWORDS = {
    "辣": ("辣", "重辣", "麻辣"),
    "微辣": ("微辣", "一点辣"),
    "清淡": ("清淡", "淡一点", "不油"),
    "不辣": ("不辣", "不要辣", "不能吃辣"),
    "重口": ("重口", "味道重"),
    "咸香": ("咸香",),
}

SCENE_KEYWORDS = {
    "早餐": ("早餐", "早饭"),
    "午饭": ("午饭", "午餐", "中饭"),
    "晚饭": ("晚饭", "晚餐"),
    "夜宵": ("夜宵", "宵夜"),
}

TAG_KEYWORDS = {
    "热乎": ("热的", "热乎", "暖和"),
    "管饱": ("管饱", "顶饱", "很饿"),
    "少油": ("少油", "不要太油", "不油腻"),
    "负担不重": ("轻一点", "负担小", "负担不重"),
    "出餐快": ("快一点", "赶时间", "出餐快"),
}


def _as_text_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def load_food_catalog(catalog_path: Path = DEFAULT_CATALOG_PATH) -> list[FoodItem]:
    with catalog_path.open("r", encoding="utf-8") as file:
        raw_items = json.load(file)

    if not isinstance(raw_items, list):
        raise ValueError("食物库格式错误：根节点必须是列表")

    items: list[FoodItem] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue

        name = str(raw_item.get("name", "")).strip()
        if not name:
            continue

        items.append(
            FoodItem(
                name=name,
                tags=_as_text_tuple(raw_item.get("tags")),
                moods=_as_text_tuple(raw_item.get("moods")),
                flavors=_as_text_tuple(raw_item.get("flavors")),
                scenes=_as_text_tuple(raw_item.get("scenes")),
                budget_min=int(raw_item.get("budget_min", 0)),
                budget_max=int(raw_item.get("budget_max", 0)),
            )
        )

    if not items:
        raise ValueError("食物库中没有可用食物")
    return items


def _match_labels(query: str, keyword_groups: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    return tuple(
        label
        for label, keywords in keyword_groups.items()
        if any(keyword in query for keyword in keywords)
    )


def _parse_flavors(query: str) -> tuple[str, ...]:
    flavors = list(_match_labels(query, FLAVOR_KEYWORDS))
    if "不辣" in flavors:
        flavors = [flavor for flavor in flavors if flavor not in {"辣", "微辣"}]
    return tuple(flavors)


def parse_food_conditions(
    query: str,
    preferences: list[str] | tuple[str, ...] = (),
) -> FoodConditions:
    normalized = (query or "").strip().lower()
    preference_query = " ".join(preferences).strip().lower()
    budget_match = re.search(r"(\d+)\s*(?:元)?\s*(?:以内|以下|左右)", normalized)
    budget_max = int(budget_match.group(1)) if budget_match else None
    current_flavors = _parse_flavors(normalized)
    remembered_flavors = _parse_flavors(preference_query)
    current_tags = _match_labels(normalized, TAG_KEYWORDS)
    remembered_tags = _match_labels(preference_query, TAG_KEYWORDS)

    return FoodConditions(
        budget_max=budget_max,
        moods=_match_labels(normalized, MOOD_KEYWORDS),
        flavors=current_flavors or remembered_flavors,
        scenes=_match_labels(normalized, SCENE_KEYWORDS),
        desired_tags=current_tags or remembered_tags,
        remembered_flavors=remembered_flavors if not current_flavors else (),
        remembered_tags=remembered_tags if not current_tags else (),
    )


def _score_food(item: FoodItem, conditions: FoodConditions) -> int:
    score = 0
    score += 4 * len(set(item.moods) & set(conditions.moods))
    score += 4 * len(set(item.flavors) & set(conditions.flavors))
    score += 3 * len(set(item.scenes) & set(conditions.scenes))
    score += 3 * len(set(item.tags) & set(conditions.desired_tags))

    if conditions.budget_max is not None and item.budget_max <= conditions.budget_max:
        score += 2
    return score


def _build_reason(item: FoodItem, conditions: FoodConditions) -> str:
    reasons: list[str] = []

    matched_moods = set(item.moods) & set(conditions.moods)
    if "累" in matched_moods:
        reasons.append("你说今天比较累，它热乎省事")
    elif "烦躁" in matched_moods:
        reasons.append("它满足感强，不需要再费心纠结")
    elif "低落" in matched_moods:
        reasons.append("它温和热乎，吃起来比较有安慰感")
    elif "开心" in matched_moods:
        reasons.append("它更有满足感，适合心情好的时候")

    matched_flavors = set(item.flavors) & set(conditions.flavors)
    if matched_flavors:
        matched_flavor = next(iter(matched_flavors))
        if matched_flavor in conditions.remembered_flavors:
            reasons.append(f"也符合你保存的{matched_flavor}口味")
        else:
            reasons.append(f"口味也符合你想要的{matched_flavor}")

    matched_tags = set(item.tags) & set(conditions.desired_tags)
    if matched_tags:
        matched_tag = next(iter(matched_tags))
        if matched_tag in conditions.remembered_tags:
            reasons.append(f"也符合你保存的{matched_tag}偏好")
        else:
            reasons.append(f"也符合你想要的{matched_tag}")

    if conditions.budget_max is not None:
        reasons.append(f"通常能控制在{conditions.budget_max}元以内")

    if not reasons:
        reasons.append(f"它热乎稳妥，预算通常在{item.budget_min}到{item.budget_max}元")

    return "，".join(reasons) + "。"


def recommend_food(
    query: str = "",
    preferences: list[str] | tuple[str, ...] = (),
    catalog_path: Path = DEFAULT_CATALOG_PATH,
) -> FoodRecommendation:
    conditions = parse_food_conditions(query, preferences)
    items = load_food_catalog(catalog_path)

    if conditions.budget_max is not None:
        affordable_items = [
            item for item in items if item.budget_min <= conditions.budget_max
        ]
        if affordable_items:
            items = affordable_items

    best_item = max(items, key=lambda item: _score_food(item, conditions))
    return FoodRecommendation(
        food_name=best_item.name,
        reason=_build_reason(best_item, conditions),
    )
