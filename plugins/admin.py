from __future__ import annotations

from doraymon.context import BotContext


def handle(context: BotContext) -> str:
    if not context.user_openid or context.user_openid not in context.settings.admin_openids:
        return "你没有权限使用管理员命令。"

    subcommand = context.args.strip().split(" ", 1)[0]
    if subcommand == "status":
        return "管理员状态：权限校验正常。危险命令未开放。"

    return "管理员命令占位。当前支持：/admin status"
