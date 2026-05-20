from __future__ import annotations

from doraymon.context import BotContext


def handle(context: BotContext) -> str:
    prefix = context.settings.command_prefix
    return "\n".join(
        [
            "DoraYmon 可用命令：",
            f"{prefix}help - 查看命令列表",
            f"{prefix}ping - 测试机器人是否在线",
            f"{prefix}status - 查看运行状态",
            f"{prefix}chat 你好 - 调用 DeepSeek 聊天",
            f"{prefix}天气 南昌 - 天气功能占位",
            f"{prefix}今日运势 - 运势功能占位",
            f"{prefix}签到 - 每日签到",
            f"{prefix}我的签到 - 查看签到统计",
            f"{prefix}todo - 待办功能占位",
            f"{prefix}admin status - 管理员状态命令",
        ]
    )
