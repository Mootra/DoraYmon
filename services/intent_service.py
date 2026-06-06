from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class IntentMatch:
    name: str
    confidence: float


FOOD_INTENT = "food"

FOOD_DECISION_PATTERNS = (
    re.compile(
        r"(?:^|[，,。！？?])"
        r"(?:我|我们)?"
        r"(?:今天|今晚|中午|早上|现在|等会|待会|一会儿)?"
        r"(?:早饭|早餐|午饭|午餐|中饭|晚饭|晚餐|夜宵|宵夜)?"
        r"(?:想|要|该|应该|准备|打算)?"
        r"(?:吃什么|吃啥|吃点什么|吃点啥)"
    ),
    re.compile(r"(?:外卖).{0,6}(?:点什么|点啥|吃什么|吃啥)"),
    re.compile(r"(?:点|叫)(?:什么|啥)(?:外卖)?"),
    re.compile(r"(?:不知道|不清楚|纠结).{0,8}(?:吃什么|吃啥|点什么|点啥)"),
    re.compile(r"(?:推荐|建议|安排).{0,8}(?:吃的|食物|午饭|晚饭|夜宵|外卖)"),
    re.compile(r"(?:午饭|午餐|中饭|晚饭|晚餐|夜宵|宵夜).{0,6}(?:怎么解决|怎么安排|来点什么|来点啥)"),
)

FOOD_HISTORY_PATTERNS = (
    re.compile(r"(?:刚刚|刚才|已经|刚).{0,4}(?:吃完|吃了)"),
    re.compile(r"(?:你|他|她).{0,3}(?:吃什么了|吃啥了|吃了什么|吃了啥)"),
    re.compile(r"(?:昨天|前天|上次).{0,6}(?:吃什么|吃啥|吃了什么|吃了啥)"),
)


def detect_intent(content: str) -> IntentMatch | None:
    normalized = re.sub(r"\s+", "", (content or "").strip().lower())
    if not normalized:
        return None

    if any(pattern.search(normalized) for pattern in FOOD_HISTORY_PATTERNS):
        return None

    if any(pattern.search(normalized) for pattern in FOOD_DECISION_PATTERNS):
        return IntentMatch(name=FOOD_INTENT, confidence=1.0)

    return None
