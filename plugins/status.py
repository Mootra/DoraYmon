from __future__ import annotations

from datetime import datetime

from doraymon.context import BotContext
from services.model_usage import MODEL_USAGE
from services.send_result import SEND_LOG
from services.session_history import get_session_key


def handle(context: BotContext) -> str:
    uptime = datetime.now() - context.started_at
    mode = "沙箱模式" if context.settings.qqbot_sandbox else "正式模式"
    lines = [
        "DoraYmon 运行中",
        f"启动时间：{context.started_at:%Y-%m-%d %H:%M:%S}",
        f"运行时长：{str(uptime).split('.')[0]}",
        f"当前模式：{mode}",
    ]
    lines.extend(_delivery_lines(context))
    lines.append(MODEL_USAGE.describe())
    return "\n".join(lines)


def _delivery_lines(context: BotContext) -> list[str]:
    """Report what actually happened on the wire, without exposing message text."""
    session = get_session_key(context)
    if session is None:
        return ["本次运行发送情况：无法识别当前会话"]

    delivered, failed = SEND_LOG.counts_for(session)
    if delivered == 0 and failed == 0:
        return ["本次运行发送情况：本次启动后还没有发送记录"]

    lines = [f"本次运行发送情况：成功 {delivered} 段，失败 {failed} 段"]
    failure = SEND_LOG.last_failure(session)
    if failure is not None:
        lines.append(f"最近一次发送失败：{failure.error_label}")
    return lines
