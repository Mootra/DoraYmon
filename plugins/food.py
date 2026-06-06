from __future__ import annotations

from doraymon.context import BotContext
from services.food_recommend_service import recommend_food
from storage.food_preference_store import (
    add_food_preference,
    list_food_preferences,
    remove_food_preference,
)


MAX_PREFERENCE_LENGTH = 100
MAX_PREFERENCE_COUNT = 20


def handle(context: BotContext) -> str:
    preferences = (
        list_food_preferences(context.user_openid) if context.user_openid else []
    )
    recommendation = recommend_food(context.args, preferences)
    return recommendation.text


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
