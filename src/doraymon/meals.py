import re

from .clients import Clients, map_url
from .config import Settings
from .models import Meal

BUDGET = re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块)")
LOCATION = re.compile(r"(?:换到|换位置|我在|在)?([^，,。！？?\n]{2,60}?)(?:附近|周边)")
CHANGE_LOCATION = re.compile(r"(?:换位置|换到|我在)[\s：:]*([^，,。！？?\n]{2,60})")
DECISION = re.compile(
    r"吃什么|吃啥|点什么外卖|推荐.{0,6}(?:餐厅|饭店|吃的)"
    r"|附近.{0,10}(?:好吃的|吃的|餐厅|饭店)|附近(?:有啥|有什么|推荐)?[？?。]*$"
)
FOLLOWUP = re.compile(
    r"换(?:一个|一家|一批)|再近点|近一点|便宜|\d+\s*(?:元|块)|地图|导航|地址|换位置|换到"
)
REDIRECT = re.compile(r"只是聊聊|不找店|不想找店|自己做饭|自己做菜|聊点别的|换个话题")


def leaves_meal(text: str) -> bool:
    cooked = re.sub(r"(?:不想|不打算|不要|不用|不)(?:再)?自己做[饭菜]", "", text)
    redirects = list(REDIRECT.finditer(cooked))
    resume = list(re.finditer(r"还是(?:想|要)?(?:出去吃|找店)|改成出去吃", cooked))
    return bool(redirects) and (not resume or redirects[-1].end() > resume[-1].start())


def wants_meal(text: str, current: Meal | None) -> bool:
    if leaves_meal(text) or re.search(r"(?:昨天|刚刚|刚才|上次).{0,6}吃|[你他她]吃什么了", text):
        return False
    return bool(DECISION.search(text) or (current and FOLLOWUP.search(text)))


async def recommend(
    text: str,
    current: Meal | None,
    clients: Clients,
    settings: Settings,
) -> tuple[str, Meal | None]:
    match = LOCATION.search(text) or CHANGE_LOCATION.search(text)
    location = match.group(1).strip() if match else ""
    location = re.sub(r"^(?:请|帮我)?(?:查一下|找一下|看看|我在|在|换到|换位置)\s*", "", location)
    state = current or Meal(location=settings.default_location, radius=settings.radius_meters)
    if location and location != state.location:
        state = Meal(location=location, radius=settings.radius_meters)
    if not state.location:
        return "给我一个校区、地标或地址，我再查附近的店。", current
    if re.search(r"换位置[？?。\s]*$", text):
        return "想换到哪里？给我一个校区、地标或地址。", current
    changes = {}
    if budget := BUDGET.search(text):
        amount = float(budget.group(1))
        if amount <= 0:
            return "预算需要大于 0 元。", current
        changes["budget"] = amount
    if "再近点" in text or "近一点" in text:
        changes["radius"] = max(200, state.radius - 500)
    if re.search(r"换一个|换一家|换一批", text):
        changes["excluded"] = state.excluded | {p.id for p in state.selected}
    state = state.model_copy(update=changes)
    if re.search(r"地图|导航|地址", text) and state.selected:
        place = state.selected[0]
        return f"{place.name}\n{place.address}\n{map_url(place)}", state
    if not settings.amap_key.get_secret_value():
        return f"按{state.location}附近找。地图尚未配置，暂时无法核实真实店铺。", state
    found = await clients.nearby(state.location, state.radius)
    candidates = tuple(p for p in found if p.id not in state.excluded)
    selected = candidates[: 3 if re.search(r"几家|一批|列出来|还有哪些", text) else 1]
    state = state.model_copy(update={"selected": selected})
    if not selected:
        return f"{state.location}附近这次没有查到符合范围的其他店。", state
    lines = [f"{state.location}附近："]
    for place in selected:
        distance = f"（直线距离约{place.distance}米）" if place.distance is not None else ""
        lines.append(f"{place.name}{distance} {place.address}".rstrip())
    if state.budget is not None:
        lines.append(f"预算按{state.budget:g}元保留；缺少菜单价，还不能确认够不够。")
    if "便宜" in text:
        lines.append("缺少菜单价，暂时无法比较哪家更便宜。")
    lines.append("来源：高德地图。菜单、现价、营业状态和步行距离尚未核实。")
    return "\n".join(lines), state
