from __future__ import annotations

from doraymon.context import BotContext
from storage.sign_store import get_sign_summary, sign_today


def handle(context: BotContext) -> str:
    if not context.user_openid:
        return "无法识别用户 openid，暂时不能签到。"

    result = sign_today(context.user_openid)
    if result.created:
        return f"签到成功！这是你第 {result.total_days} 次签到。"
    return f"今天已经签到过啦。累计签到 {result.total_days} 天。"


def handle_my_sign(context: BotContext) -> str:
    if not context.user_openid:
        return "无法识别用户 openid，暂时不能查询签到。"

    summary = get_sign_summary(context.user_openid)
    return f"你的累计签到：{summary.total_days} 天。最近签到：{summary.last_sign_date or '暂无'}。"
