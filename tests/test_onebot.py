import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import nonebot
import pytest
from nonebot.adapters.onebot.v11 import (
    Adapter,
    Bot,
    GroupMessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.message import handle_event


@pytest.fixture(scope="module")
def onebot():
    nonebot.init(driver="~aiohttp", _env_file=None, environment="test", onebot_ws_urls=[])
    driver = nonebot.get_driver()
    driver.register_adapter(Adapter)
    assert nonebot.load_plugin("doraymon.onebot") is not None
    return importlib.import_module("doraymon.onebot")


def native(text="/ping", group=False, message_id=1):
    fields = {
        "time": 1,
        "self_id": 202,
        "post_type": "message",
        "user_id": 101,
        "sub_type": "normal" if group else "friend",
        "message_id": message_id,
        "message": [{"type": "text", "data": {"text": text}}],
        "original_message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text,
        "font": 0,
        "sender": {"user_id": 101, "nickname": "合成用户"},
    }
    if group:
        return GroupMessageEvent.model_validate(fields | {"message_type": "group", "group_id": 303})
    return PrivateMessageEvent.model_validate(fields | {"message_type": "private"})


async def test_framework_dispatches_real_onebot_event(onebot, app, monkeypatch):
    app.settings.allowed_users = {"101"}
    monkeypatch.setattr(onebot, "application", app)
    adapter = nonebot.get_adapter(Adapter)
    bot = Bot(adapter, "202")
    send = AsyncMock(return_value={"message_id": 9001})
    monkeypatch.setattr(bot, "send", send)
    await handle_event(bot, native())
    assert send.await_count == 1
    segments = send.call_args.args[1]
    assert [(s.type, s.data) for s in segments] == [
        ("reply", {"id": "1"}),
        ("text", {"text": "pong"}),
    ]


async def test_send_uses_literal_text_and_requires_receipt(
    onebot, app, clients, store, monkeypatch
):
    app.settings.allowed_users = {"101"}
    monkeypatch.setattr(onebot, "application", app)
    clients.complete.return_value = "[CQ:at,qq=999]"
    bot = SimpleNamespace(send=AsyncMock(return_value={"message_id": 1}))
    message = native("hello")
    await onebot.receive(bot, message)
    assert bot.send.call_args.args[1][-1].type == "text"
    assert bot.send.call_args.args[1][-1].data["text"] == "[CQ:at,qq=999]"
    assert len(await store.history(onebot.incoming(message).session)) == 1
    bot.send.return_value = {}
    next_message = native("second", message_id=2)
    await onebot.receive(bot, next_message)
    assert len(await store.history(onebot.incoming(message).session)) == 1


async def test_lifecycle_opens_and_closes_database_and_http(onebot, settings, monkeypatch):
    settings.allowed_users = {"101"}
    monkeypatch.setattr(onebot, "Settings", lambda: settings)
    await onebot.startup()
    app = onebot.application
    http = app.clients.http
    bot = SimpleNamespace(send=AsyncMock(return_value={"message_id": 1}))
    try:
        await onebot.receive(bot, native())
        assert bot.send.call_args.args[1][-1].data["text"] == "pong"
    finally:
        await onebot.shutdown()
    assert onebot.application is None
    assert http.closed


def test_adapter_only_keeps_quote_identifier(onebot):
    event = native("问题", group=True)
    event.original_message.insert(0, MessageSegment.reply(123))
    normalized = onebot.incoming(event)
    assert normalized.quote_id == "123"
    assert normalized.text == "问题"
    assert normalized.group_id == "303"
