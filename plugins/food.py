from __future__ import annotations

import logging
import re
import time
from dataclasses import replace

from doraymon.context import BotContext
from services.blocking_io import BlockingIOBusy, LOOKUP_IO
from services.food_recommend_service import recommend_food
from services.intent_service import is_nearby_food_query
from services.meal_catalog_loader import load_catalog
from services.nearby_lookup import lookup_for, provider_is_configured
from services.meal_session import (
    ANOTHER_MARKERS,
    BUDGET_PATTERN,
    CLOSER_MARKERS,
    INTENT_CHEAPER,
    MEAL_STATES,
    MIN_RADIUS_METERS,
    RADIUS_STEP_METERS,
    MealDecisionSession,
    extract_start_point,
    parse_meal_turn,
)
from services.nearby_meal_service import render_selection
from services.recommendation_types import Shop, StartPoint, StartPrecision
from services.pending_reply import stage_reply
from services.session_history import generate_revision, read_history, reply_allowed
logger = logging.getLogger(__name__)

from storage.food_preference_store import (
    add_food_preference,
    list_food_preferences,
    remove_food_preference,
)


MAX_PREFERENCE_LENGTH = 100
MAX_PREFERENCE_COUNT = 20

DETAIL_REQUEST = re.compile(
    r"^(?:(?:第([1-5一二三四五])家|这家|那家|它)(?:的)?)?"
    r"(?:(?:给我|发我|发个|发|看看|看下|来个|给个|请))?"
    r"(?:地图(?:链接)?|导航(?:链接)?|地址|位置|怎么走|在哪[里儿]?)"
    r"(?:发我|给我|呢|吗|吧)?[？?！!。\s]*$"
)
LIST_REQUEST = re.compile(
    r"(?:还有哪些(?:店|饭店|餐厅|吃的|选择)?|多给几个|多推荐几家|列几家|列几个|列出来|换一批)"
    r"[？?！!。\s]*$"
)


async def handle(context: BotContext) -> str:
    read_history(context, context.args)
    revision = generate_revision(context)

    # A curated catalogue with real places takes priority: it can support a
    # claim about a nearby shop, which the sample dish-name catalogue cannot.
    grounded = await _grounded_reply(context, revision)
    if not reply_allowed(context, revision):
        return ""
    if grounded is not None:
        return stage_reply(context, context.args or "吃什么", grounded, revision)

    preferences = (
        list_food_preferences(context.user_openid) if context.user_openid else []
    )
    recommendation = recommend_food(context.args, preferences)
    return stage_reply(context, context.args or "吃什么", recommendation.text, revision)


def _meal_session(context: BotContext) -> MealDecisionSession | None:
    """Build a decision session, or None when no curated catalogue is loaded."""
    directory = str(getattr(context.settings, "meal_catalog_dir", "") or "").strip()
    if not directory:
        return None
    result = load_catalog(directory)
    if result.is_empty:
        return None
    return MealDecisionSession(catalog=result.catalog)


async def _grounded_reply(context: BotContext, revision: int | None) -> str | None:
    """Use the explicit place, ongoing decision, then the configured default."""
    session_key = _session_key(context)
    explicit_start = extract_start_point(context.args)
    ongoing = MEAL_STATES.get(session_key) if session_key else None
    use_default = context.settings.default_location and provider_is_configured(context.settings)
    if (
        explicit_start is None
        and ongoing is None
        and not is_nearby_food_query(context.args)
        and not use_default
    ):
        return None

    start = explicit_start or (ongoing.start if ongoing else None)
    if start is None and context.settings.default_location:
        start = StartPoint(context.settings.default_location, precision=StartPrecision.LANDMARK)
    if start is None:
        return "想查哪个地点附近？给我一个校区、地标或地址就行。"

    try:
        session = (
            await LOOKUP_IO.run(_meal_session, context)
            if context.settings.meal_catalog_dir else None
        )
    except BlockingIOBusy:
        return "附近查询正忙，请稍后再试。"
    if not reply_allowed(context, revision):
        return ""
    if session is None or session_key is None:
        return await _provider_only_reply(context, start, session_key, revision)

    detail = DETAIL_REQUEST.fullmatch(context.args.strip())
    if detail is not None and ongoing is not None:
        item = next((item for item in session.catalog.items() if item.item_id == ongoing.current_item_id), None)
        shop = session.catalog.shop_by_id(item.shop_id) if item is not None else None
        if shop is None:
            return "还没有选中的店，先说说想在哪附近吃。"
        return _place_details((shop,), (shop.shop_id,), detail)

    outcome = session.handle(
        session_key, context.args,
        start=explicit_start or (start if ongoing is None else None),
    )
    if outcome.states_a_fact:
        _persist_fact(session, session_key, outcome)
    return render_selection(outcome.selection, outcome.state.start)


def _persist_fact(session: MealDecisionSession, session_key, outcome) -> None:
    """Store what the user said, and apply the part that affects this decision.

    Persisting is best effort: a storage problem must not turn a working
    acknowledgement into an error. A rejection also joins this session's exclusion
    list, so the next suggestion does not repeat the place the user just refused.
    """
    from services.recommendation_types import FeedbackKind
    from storage.meal_feedback_store import record_feedback

    feedback = outcome.feedback
    if feedback is None:
        return
    session_id = "|".join(str(part) for part in (session_key or ()))
    if not record_feedback(feedback, session_key=session_id):
        logger.warning("用餐反馈未能写入存储，本次只做了口头确认")
    if feedback.kind is FeedbackKind.REJECTED:
        current = session.store.get(session_key)
        if current is not None:
            session.store.update(current.exclude(feedback.shop_id, reason="用户表示不想去"))


async def _provider_only_reply(
    context: BotContext, start: StartPoint, session_key, revision: int | None,
) -> str:
    """Return map facts, retaining the search place for the next turn."""
    radius = max(MIN_RADIUS_METERS, context.settings.nearby_provider_radius_meters)
    state = MEAL_STATES.get(session_key) if session_key else None
    previous_choice = state.candidates[0] if state and state.candidates else ""
    detail = DETAIL_REQUEST.fullmatch(context.args.strip())
    list_requested = bool(LIST_REQUEST.search(context.args))
    closer_requested = any(marker in context.args for marker in CLOSER_MARKERS)
    turn = parse_meal_turn(context.args, default_intent="")
    if session_key:
        if state is None:
            state = MEAL_STATES.start(session_key, start).with_radius(radius)
        elif state.start is None or not state.start.same_place_as(start):
            state = state.with_start(start).with_radius(radius)
        budget = BUDGET_PATTERN.search(context.args)
        if budget is not None:
            state = state.with_budget(float(budget.group(1)))
        if turn.party_size is not None:
            state = state.with_party_size(turn.party_size)
        if closer_requested:
            state = state.with_radius(max(MIN_RADIUS_METERS, (state.radius_meters or radius) - RADIUS_STEP_METERS))
        if any(marker in context.args for marker in ANOTHER_MARKERS):
            for shop_id in state.candidates:
                state = state.exclude(shop_id, reason="本轮换店")
        state = MEAL_STATES.update(state)
        radius = state.radius_meters or radius

    if not provider_is_configured(context.settings) or (
        context.settings.nearby_provider == "amap" and not context.settings.nearby_provider_key
    ):
        return f"按{start.text}附近找。高德还没接通，暂时拿不到真实店铺信息。"

    lookup = lookup_for(
        replace(context.settings, nearby_provider_radius_meters=radius)
    ).new_turn()
    started = time.monotonic()
    try:
        shops = await LOOKUP_IO.run(
            lookup.find_shops, start, limit=25 if state and state.excluded else 5,
        )
    except BlockingIOBusy:
        return "附近查询正忙，请稍后再试。"
    if not reply_allowed(context, revision):
        return ""
    fetched = len(shops)
    shops = tuple(
        shop for shop in shops
        if (state is None or not state.is_excluded(shop.shop_id))
        and (shop.distance_meters is None or shop.distance_meters <= radius)
        and (not closer_requested or shop.distance_meters is not None)
    )
    if detail is not None:
        return _place_details(shops, state.candidates if state is not None else (), detail)
    shops = shops[:3 if list_requested else 1]
    logger.info(
        "附近查询：地点=%s 半径=%s米 高德返回=%d家 采用=%d家 耗时=%.0fms",
        start.text,
        radius,
        fetched,
        len(shops),
        (time.monotonic() - started) * 1000,
    )
    if state is not None:
        MEAL_STATES.update(state.with_candidates(tuple(shop.shop_id for shop in shops)))
    if not shops:
        notes = lookup.describe_notes()
        return f"{start.text}附近：" + (notes or "这次没有查到符合条件的店。")
    lines = [f"{start.text}附近："]
    if closer_requested and shops[0].shop_id == previous_choice:
        lines.append("这次没找到比上次更近的店。")
    if list_requested:
        lines.extend(f"{index}. {_place_summary(shop)}" for index, shop in enumerate(shops, 1))
    else:
        lines.append("先看看" + _place_summary(shops[0]))
    if lookup.resolved_start is not None and lookup.resolved_start.text != start.text:
        lines.append(f"距离起点：{lookup.resolved_start.text}")
    if state is not None and state.budget_cny is not None:
        lines.append(f"预算按{state.budget_cny:g}元保留；缺少菜单价，还不能确认够不够。")
    if state is not None and state.party_size is not None:
        lines.append(f"{state.party_size}人用餐的份量尚未核实。")
    if turn.intent == INTENT_CHEAPER:
        lines.append("没有菜单价，暂时没法比较哪家更便宜。")
    lines.append("来源：高德地图。菜单、现价和营业状态尚未核实。")
    return "\n".join(lines)


def _place_summary(shop: Shop) -> str:
    return (
        shop.name
        + (f"（直线距离约{shop.distance_meters}米）" if shop.distance_meters is not None else "")
        + (f" {shop.address}" if shop.address else "")
    )


def _place_details(shops: tuple[Shop, ...], candidates: tuple[str, ...], request: re.Match[str]) -> str:
    if not candidates:
        return "还没有选中的店，先说说想在哪附近吃。"
    number = request.group(1)
    if number is None:
        index = 1
    elif number.isdigit():
        index = int(number)
    else:
        index = "一二三四五".index(number) + 1
    if index > len(candidates):
        return "上次给的候选里没有这家，可以先换一批。"
    selected_id = candidates[index - 1]
    shop = next((shop for shop in shops if shop.shop_id == selected_id), None)
    if shop is None:
        return "上次那家店的地点信息暂时取不到，可以重新查一下。"
    lines = [_place_summary(shop)]
    if any(word in request.group(0) for word in ("地图", "导航", "怎么走")):
        lines.append(shop.map_url or "这家店暂时没有可用的地图链接。")
    elif not shop.address:
        lines.append("这家店的详细地址尚未核实。")
    return "\n".join(lines)


def is_follow_up(context: BotContext, text: str) -> bool:
    session_key = _session_key(context)
    if session_key is None or MEAL_STATES.get(session_key) is None:
        return False
    if DETAIL_REQUEST.fullmatch(text.strip()) or LIST_REQUEST.search(text):
        return True
    if any(marker in text for marker in ("附近", "周边", "一带")):
        return is_nearby_food_query(text)
    return extract_start_point(text) is not None or bool(parse_meal_turn(text, default_intent="").intent)


def _session_key(context: BotContext):
    from services.session_history import get_session_key

    return get_session_key(context)


def handle_remember_preference(context: BotContext) -> str:
    if not context.user_openid:
        return "无法识别用户 openid，暂时不能保存口味。"

    preference = context.args.strip()
    if not preference:
        return "请告诉我要记住什么，例如：/记住口味 我不吃香菜"
    if len(preference) > MAX_PREFERENCE_LENGTH:
        return f"口味描述请控制在 {MAX_PREFERENCE_LENGTH} 个字符以内。"
    if len(list_food_preferences(context.user_openid)) >= MAX_PREFERENCE_COUNT:
        return f"最多保存 {MAX_PREFERENCE_COUNT} 条口味，请先删除不需要的记录。"

    created = add_food_preference(context.user_openid, preference)
    if not created:
        return "这条口味已经记住了。"
    return f"记住了：{preference}"


def handle_my_preferences(context: BotContext) -> str:
    if not context.user_openid:
        return "无法识别用户 openid，暂时不能查看口味。"

    preferences = list_food_preferences(context.user_openid)
    if not preferences:
        return "你还没有保存口味。可以发送：/记住口味 我喜欢辣"

    lines = ["你保存的口味："]
    lines.extend(f"{index}. {preference}" for index, preference in enumerate(preferences, 1))
    return "\n".join(lines)


def handle_forget_preference(context: BotContext) -> str:
    if not context.user_openid:
        return "无法识别用户 openid，暂时不能删除口味。"

    preference = context.args.strip()
    if not preference:
        return "请输入要忘记的完整口味，例如：/忘记口味 我不吃香菜"

    removed = remove_food_preference(context.user_openid, preference)
    if not removed:
        return "没有找到完全相同的口味记录，请先发送 /我的口味 查看。"
    return f"已经忘记：{preference}"
