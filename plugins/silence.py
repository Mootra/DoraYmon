"""Administrator silence switch: stop replying without a restart."""

from __future__ import annotations

from doraymon.context import BotContext
from services.admin_access import is_admin, refusal_message
from services.silence import SILENCE, parse_silence_args


def handle(context: BotContext) -> str:
    """Turn ordinary replies on or off, admin only.

    Deliberately reachable while silenced: a switch that cannot be turned off
    through the same channel would require a restart to recover from, which
    defeats the point of having it.
    """
    if not is_admin(context):
        return refusal_message()

    action = parse_silence_args(context.args)
    if action == "on":
        changed = SILENCE.enable(reason="管理员开启静默")
        if changed:
            return "已静默：机器人现在不会回复普通消息。用 /静默 off 恢复。"
        return "已经是静默状态。用 /静默 off 恢复。"
    if action == "off":
        changed = SILENCE.disable()
        if changed:
            return "已恢复：机器人会正常回复。"
        return "当前没有静默，机器人本来就在回复。"
    if action == "status":
        return SILENCE.describe()

    return "用法：/静默 on｜off｜status（也可以用 开启/关闭/状态）"
