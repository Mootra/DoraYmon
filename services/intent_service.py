from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class IntentMatch:
    name: str
    confidence: float


FOOD_INTENT = "food"

# These are explicit directions, not a guess from the occurrence of food words.
# In particular a recipe request must win over "吃什么" or an old POI decision.
_CHAT_DIRECTION = re.compile(
    r"(?:只是|就是|只想|就想)(?:想|和你|跟你)?(?:聊聊|聊天|说说话)"
    r"|(?:陪我|和我|跟我)(?:聊聊|聊天|说说话)"
    r"|(?:不用|不要|别)(?:再)?(?:帮我)?(?:找店|查店|查地图|推荐店铺|推荐餐馆|推荐餐厅)"
    r"|(?:不找店了|不出去吃了|不点外卖了)"
)
_COOK_DIRECTION = re.compile(
    r"(?:自己|在家|在宿舍)(?:做饭|做菜|做点吃的|煮饭)"
    r"|(?:帮我|给我)(?:想(?:点|个)?菜|推荐(?:个|道|点)?家常菜|想(?:个|点)?菜谱)"
    r"|(?:做什么菜|做点什么菜|有什么菜谱|怎么做这道菜)"
)
_NEGATED_COOK = re.compile(r"(?:不想|不打算|不准备|不用|不要|不再|不|别)(?:再|要)?(?:自己|在家|在宿舍)(?:做饭|做菜|做点吃的|煮饭)")
_RESUME_NEARBY = re.compile(r"(?:还是|改成|那就|现在|不过)(?:想|要)?(?:出去吃|找店|查店|点外卖|推荐餐馆|推荐餐厅)")
_STOP_TEXT = re.compile(
    r"^(?:没问你|没叫你|不是问你|不用(?:你)?回[复答](?:了)?|不用了|别回[复答](?:了)?"
    r"|别接话(?:了)?|别插话(?:了)?|别说了|不聊了|先不聊了|停止回复"
    r"|取消(?:了|吧)?|算了|先停一下|停止|不用找了)[。！!，,\s]*$"
)


def redirects_meal_to_chat(content: str) -> bool:
    """Whether an explicit chat/cooking direction supersedes nearby dining."""
    text = re.sub(r"[ \t]+", "", str(content or ""))
    chat = list(_CHAT_DIRECTION.finditer(text))
    # Remove explicit negations before looking for positive cooking requests.
    cooking_text = _NEGATED_COOK.sub(lambda match: " " * len(match.group()), text)
    cooking = list(_COOK_DIRECTION.finditer(cooking_text))
    changes = chat + cooking
    if not changes:
        return False
    last_change = max(match.end() for match in changes)
    return not any(match.start() >= last_change for match in _RESUME_NEARBY.finditer(text))


def is_stop_text(content: str) -> bool:
    return bool(_STOP_TEXT.fullmatch(str(content or "").strip()))

NEARBY_FOOD_PATTERN = re.compile(
    r"(?:附近|周边|一带).{0,10}"
    r"(?:吃什么|吃啥|吃点啥|吃点什么|有什么吃的|有啥吃的|有什么好吃的|有啥好吃的"
    r"|哪里吃|哪家好吃|餐厅|饭店|餐馆|小吃|美食|外卖|奶茶|烧烤|火锅|快餐)"
    r"|(?:附近|周边)(?:有)?推荐(?:的)?(?:店|餐厅|饭店)?(?:吗|么)?[？?！!。\s]*$"
    r"|^(?:推荐|建议)(?:一个|一家|个|家)?(?:附近|周边)的?[？?！!。\s]*$"
    r"|(?:餐厅|饭店|餐馆|小吃|美食).{0,8}(?:附近|周边)"
)

# “地点 + 附近”这类不带餐食词的说法，例如“江西师范大学附近”“学校附近”“附近有什么”。
# 只在整句以附近/周边（可带“有什么/推荐/吃的”一类收尾词）结束时才算找饭吃；
# “学校附近租房贵吗”“附近有什么图书馆”“附近适合散步吗”仍走普通聊天。
# 把一个问题误判成点餐，和漏掉点餐一样是错的，所以这里只认收尾形态。
BARE_NEARBY_PATTERN = re.compile(
    r"(?:附近|周边|一带)"
    r"(?:有什么|有啥|有哪些|有什么店|有啥店|哪些店|有什么吃的|有啥吃的|推荐|好吃的|吃的)?"
    r"[？?！!。.，,、~～\s]*$"
)


def is_nearby_food_query(content: str) -> bool:
    if redirects_meal_to_chat(content):
        return False
    normalized = re.sub(r"\s+", "", content or "")
    return bool(
        NEARBY_FOOD_PATTERN.search(normalized) or BARE_NEARBY_PATTERN.search(normalized)
    )


FOOD_DECISION_PATTERNS = (
    NEARBY_FOOD_PATTERN,
    BARE_NEARBY_PATTERN,
    re.compile(
        r"(?:^|[，,。！？?])"
        r"(?:我|我们)?"
        r"(?:今天|今晚|中午|早上|现在|等会|待会|一会儿)?"
        r"(?:早饭|早餐|午饭|午餐|中饭|晚饭|晚餐|夜宵|宵夜)?"
        r"(?:想|要|该|应该|准备|打算)?"
        r"(?:吃什么|吃啥|吃点什么|吃点啥)"
    ),
    re.compile(r"(?:外卖).{0,6}(?:点什么|点啥|吃什么|吃啥)"),
    re.compile(r"(?:点|叫)(?:什么|啥)外卖"),
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
    if not normalized or redirects_meal_to_chat(content):
        return None

    if any(pattern.search(normalized) for pattern in FOOD_HISTORY_PATTERNS):
        return None

    if any(pattern.search(normalized) for pattern in FOOD_DECISION_PATTERNS):
        return IntentMatch(name=FOOD_INTENT, confidence=1.0)

    return None
