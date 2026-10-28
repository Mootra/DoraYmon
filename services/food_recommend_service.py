from __future__ import annotations

import json
import random
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
        return f"可以试试{self.food_name}。{self.reason}" if self.food_name else self.reason


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
    "不辣": ("不辣", "不要辣", "不能吃辣", "不吃辣"),
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
    elif "微辣" in flavors:
        flavors = [flavor for flavor in flavors if flavor != "辣"]
    return tuple(flavors)


def parse_food_conditions(
    query: str,
    preferences: list[str] | tuple[str, ...] = (),
) -> FoodConditions:
    normalized = (query or "").strip().lower()
    preference_query = " ".join(preferences).strip().lower()
    budget_match = re.search(r"(\d+)\s*(?:元|块钱|块)?\s*(?:以内|以下|左右)", normalized)
    if budget_match is None:
        budget_match = re.search(r"预算\s*(\d+)", normalized)
    if budget_match is None:
        budget_match = re.search(r"(?:只有|最多|不超过)\s*(\d+)\s*(?:元|块钱|块)", normalized)
    budget_max = int(budget_match.group(1)) if budget_match else None
    current_flavors = _parse_flavors(normalized)
    remembered_flavors = _parse_flavors(preference_query)
    current_tags = _match_labels(normalized, TAG_KEYWORDS)
    remembered_tags = _match_labels(preference_query, TAG_KEYWORDS)

    return FoodConditions(
        budget_max=budget_max,
        moods=tuple(mood for mood in _match_labels(normalized, MOOD_KEYWORDS)
                    if mood != "开心" or not any(word in normalized for word in ("不开心", "心情不好"))),
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
        reasons.append("累了一天，这顿就吃得省心一点")
    elif "烦躁" in matched_moods:
        reasons.append("这顿吃得满足一点，少纠结一件事")
    elif "低落" in matched_moods:
        reasons.append("来点合口味的，慢慢吃")
    elif "开心" in matched_moods:
        reasons.append("正好吃顿喜欢的，庆祝一下")

    matched_flavors = set(item.flavors) & set(conditions.flavors)
    if matched_flavors:
        matched_flavor = next(flavor for flavor in conditions.flavors if flavor in matched_flavors)
        if matched_flavor in conditions.remembered_flavors:
            reasons.append(f"按你保存的{matched_flavor}口味选的")
        else:
            reasons.append(f"口味可以选{matched_flavor}的")

    matched_tags = set(item.tags) & set(conditions.desired_tags)
    if matched_tags:
        matched_tag = next(tag for tag in conditions.desired_tags if tag in matched_tags)
        reasons.append({"热乎": "想吃热的，这个合适", "管饱": "饿了的话，这份比较顶饱",
                        "少油": "可以选少油的做法", "负担不重": "想吃轻一点可以选它",
                        "出餐快": "赶时间可以优先看看这类"}.get(matched_tag, matched_tag))

    if conditions.budget_max is not None:
        reasons.append(f"样例价格约{item.budget_min}～{item.budget_max}元，实际以店家为准")
        if item.budget_max > conditions.budget_max:
            reasons.append(f"可能超过你的{conditions.budget_max}元预算，点之前先确认价格")

    if not reasons:
        if "热乎" in item.tags:
            reasons.append("来一份热乎的，慢慢吃")
        elif "管饱" in item.tags:
            reasons.append("想吃顿顶饱的可以选它")
        elif "清爽" in item.tags:
            reasons.append("吃起来清爽，适合想换换口味的时候")
        else:
            reasons.append("这顿可以换个口味")

    return "。".join(reasons) + "。"


def recommend_food(
    query: str = "",
    preferences: list[str] | tuple[str, ...] = (),
    catalog_path: Path = DEFAULT_CATALOG_PATH,
) -> FoodRecommendation:
    conditions = parse_food_conditions(query, preferences)
    items = load_food_catalog(catalog_path)

    # Explicit restrictions must not lose to mood/scene scores.
    items = [item for item in items
             if set(conditions.flavors).issubset(item.flavors)
             and set(conditions.desired_tags).issubset(item.tags)
             and (not conditions.scenes or set(conditions.scenes) & set(item.scenes))]

    if conditions.budget_max is not None:
        affordable_items = [
            item for item in items if item.budget_min <= conditions.budget_max
        ]
        within_budget = [item for item in affordable_items if item.budget_max <= conditions.budget_max]
        items = within_budget or affordable_items

    if not items:
        return FoodRecommendation("", "这几个条件下，现有食物清单里还没找到合适的。可以调整预算或补充想吃的种类，忌口照常保留。")

    best_score = max(_score_food(item, conditions) for item in items)
    best_item = random.choice([item for item in items if _score_food(item, conditions) == best_score])
    return FoodRecommendation(
        food_name=best_item.name,
        reason=_build_reason(best_item, conditions),
    )
