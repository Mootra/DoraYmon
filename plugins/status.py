from __future__ import annotations

from datetime import datetime

from doraymon.context import BotContext


def handle(context: BotContext) -> str:
    uptime = datetime.now() - context.started_at
    mode = "沙箱模式" if context.settings.qqbot_sandbox else "正式模式"
    return "\n".join(
        [
            "DoraYmon 运行中",
            f"启动时间：{context.started_at:%Y-%m-%d %H:%M:%S}",
            f"运行时长：{str(uptime).split('.')[0]}",
            f"当前模式：{mode}",
        ]
    )
