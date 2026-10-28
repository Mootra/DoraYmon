from contextlib import AsyncExitStack

import aiohttp
from nonebot import get_driver, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent, MessageSegment

from .app import Application
from .clients import Clients
from .config import Settings
from .models import Incoming
from .store import Store

driver = get_driver()
stack = AsyncExitStack()
application: Application | None = None
messages = on_message(priority=5, block=False)


@driver.on_startup
async def startup() -> None:
    global application
    settings = Settings()
    store = Store(
        settings.database, max_turns=settings.history_turns, ttl=settings.history_ttl_seconds
    )
    try:
        await store.open()
        stack.push_async_callback(store.close)
        http = await stack.enter_async_context(
            aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=35),
                trust_env=False,
            )
        )
        application = Application(settings, store, Clients(settings, http))
        stack.push_async_callback(application.close)
    except BaseException:
        await stack.aclose()
        raise


@driver.on_shutdown
async def shutdown() -> None:
    global application
    await stack.aclose()
    application = None


def incoming(event: MessageEvent) -> Incoming:
    segments = event.original_message
    mentions = frozenset(str(s.data["qq"]) for s in segments if s.type == "at")
    quote_id = (
        str(event.reply.message_id)
        if event.reply
        else next(
            (str(s.data["id"]) for s in segments if s.type == "reply"),
            "",
        )
    )
    return Incoming(
        bot_id=str(event.self_id),
        user_id=str(event.user_id),
        message_id=str(event.message_id),
        group_id=str(event.group_id) if isinstance(event, GroupMessageEvent) else None,
        text=event.get_plaintext().strip(),
        addressed=event.to_me,
        mentions=mentions,
        quote_id=quote_id,
        has_attachment=any(s.type not in {"text", "at", "reply"} for s in segments),
    )


@messages.handle()
async def receive(bot: Bot, event: MessageEvent) -> None:
    if application is None:
        raise RuntimeError("application_not_started")
    normalized = incoming(event)

    async def send(text: str) -> None:
        # MessageSegment.text keeps model text and CQ-looking input as literal text.
        receipt = await bot.send(
            event,
            MessageSegment.reply(event.message_id) + MessageSegment.text(text),
        )
        if not isinstance(receipt, dict) or type(receipt.get("message_id")) is not int:
            raise ValueError("delivery_not_confirmed")
        application.policy.observe(
            normalized.model_copy(
                update={
                    "user_id": normalized.bot_id,
                    "message_id": str(receipt["message_id"]),
                    "text": text,
                    "quote_id": normalized.message_id,
                }
            )
        )

    await application.handle(normalized, send)
