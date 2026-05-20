from __future__ import annotations

import asyncio

from doraymon.context import BotContext
from services.deepseek_service import DeepSeekService


async def handle(context: BotContext) -> str:
    prompt = context.args.strip()
    if not prompt:
        return "请在 /chat 后面输入要发送给 DeepSeek 的内容，例如：/chat 你好"

    service = DeepSeekService(context.settings)
    return await asyncio.to_thread(service.chat, prompt)
