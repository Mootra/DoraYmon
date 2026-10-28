import asyncio
from unittest.mock import AsyncMock

import pytest

from doraymon.models import Passage


async def test_confirmed_chat_writes_exact_sent_turn(app, clients, store, event):
    send = AsyncMock()
    assert await app.handle(event("问题"), send)
    send.assert_awaited_once_with("合成回答")
    turns = await store.history(event().session)
    assert [(t.question, t.answer) for t in turns] == [("问题", "合成回答")]
    assert clients.complete.call_args.args[0][-1] == {"role": "user", "content": "问题"}


@pytest.mark.parametrize("failure_at", [1, 2])
async def test_failed_or_partial_delivery_never_becomes_history(
    app, clients, store, event, failure_at
):
    clients.complete.return_value = "甲" * 2000
    send = AsyncMock(
        side_effect=[RuntimeError("send")] if failure_at == 1 else [None, TimeoutError()]
    )
    assert not await app.handle(event(), send)
    assert await store.history(event().session) == []
    assert event().session not in app.policy.active


async def test_duplicate_does_not_generate_or_send_twice(app, clients, event):
    send = AsyncMock()
    assert await app.handle(event(), send)
    assert not await app.handle(event(), send)
    clients.complete.assert_awaited_once()
    send.assert_awaited_once()


async def test_denied_user_is_never_observed_or_sent_to_model(app, clients, event):
    denied = event(user_id="stranger", group_id="g1")
    send = AsyncMock()
    assert not await app.handle(denied, send)
    assert not app.policy.rooms
    clients.complete.assert_not_awaited()
    send.assert_not_awaited()


async def test_clear_interrupts_generation_and_next_input_sees_empty_history(
    app, clients, store, event
):
    entered, hold = asyncio.Event(), asyncio.Event()

    async def generate(messages):
        entered.set()
        await hold.wait()
        return "不能送达的旧回答"

    clients.complete.side_effect = generate
    send = AsyncMock()
    first = asyncio.create_task(app.handle(event("旧问题"), send))
    await entered.wait()
    assert await app.handle(event("/清空上下文", message_id="clear"), send)
    assert not await first
    assert await store.history(event().session) == []
    clients.complete.side_effect = None
    clients.complete.return_value = "新回答"
    assert await app.handle(event("新问题", message_id="new"), send)
    assert [c.args[0] for c in send.call_args_list] == ["当前会话已清空。", "新回答"]
    assert [(t.question, t.answer) for t in await store.history(event().session)] == [
        ("新问题", "新回答")
    ]


async def test_same_session_fifo_and_other_session_progress(app, clients, event):
    entered, release, other_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    order = []

    async def generate(messages):
        text = messages[-1]["content"]
        if text == "first":
            entered.set()
            await release.wait()
        if text == "other":
            other_done.set()
        order.append(text)
        return text + "-reply"

    clients.complete.side_effect = generate
    send = AsyncMock()
    first = asyncio.create_task(app.handle(event("first"), send))
    await entered.wait()
    second = asyncio.create_task(app.handle(event("second", message_id="2"), send))
    other = asyncio.create_task(app.handle(event("other", user_id="bob", message_id="3"), send))
    await asyncio.wait_for(other_done.wait(), 1)
    release.set()
    assert all(await asyncio.gather(first, second, other))
    assert order == ["other", "first", "second"]


async def test_natural_meal_followups_and_location_are_scoped(app, clients, event):
    send = AsyncMock()
    for index, text in enumerate(
        ["吃什么", "换一个", "15元以内", "再近点", "地图", "换到南昌站附近"]
    ):
        assert await app.handle(event(text, message_id=str(index)), send)
    assert app.meals[event().session].location == "南昌站"
    assert event(user_id="bob").session not in app.meals
    assert "合成乙店" in send.call_args_list[1].args[0]
    assert "不能确认" in send.call_args_list[2].args[0]
    assert "uri.amap.com/marker" in send.call_args_list[4].args[0]
    assert clients.nearby.call_args.args == ("南昌站", 1500)
    clients.complete.assert_not_awaited()


async def test_cooking_direction_overrides_previous_budget_branch(app, clients, event):
    send = AsyncMock()
    await app.handle(event("吃什么"), send)
    assert await app.handle(event("只是聊聊，自己做饭，15元以内", message_id="2"), send)
    assert event().session not in app.meals
    clients.complete.assert_awaited_once()
    clients.nearby.assert_awaited_once()


async def test_explicit_topic_reset_discards_old_history_before_generation(
    app, clients, store, event
):
    await store.append(event().session, "prior", "旧问题", "旧回答")
    send = AsyncMock()
    assert await app.handle(event("换个话题，聊聊音乐"), send)
    prompt = clients.complete.call_args.args[0]
    assert [m["content"] for m in prompt[1:]] == ["换个话题，聊聊音乐"]
    assert len(await store.history(event().session)) == 1


async def test_strict_knowledge_empty_does_not_call_model(app, clients, event):
    send = AsyncMock()
    assert await app.handle(event("/知识问 火星问题"), send)
    assert "没有检索到" in send.call_args.args[0]
    clients.complete.assert_not_awaited()


async def test_strict_knowledge_uses_scoped_sources(app, clients, store, event):
    app.settings.knowledge_enabled = True
    await store.replace_knowledge(
        "public", [Passage(source="manual.md", body="如何部署的合成说明")]
    )
    clients.complete.return_value = "根据资料[1]进行配置。"
    send = AsyncMock()
    assert await app.handle(event("/知识问 如何部署"), send)
    assert send.call_args.args[0].endswith("来源：manual.md")
    assert "只根据本轮" in clients.complete.call_args.args[0][0]["content"]


async def test_service_failure_is_explicit_and_not_saved(app, clients, store, event):
    clients.complete.side_effect = TimeoutError("synthetic")
    send = AsyncMock()
    assert not await app.handle(event(), send)
    assert "失败" in send.call_args.args[0]
    assert await store.history(event().session) == []


async def test_reply_limit_matches_exact_sent_and_stored_text(app, clients, store, event):
    clients.complete.return_value = "甲" * 5000
    send = AsyncMock()
    assert await app.handle(event(), send)
    text = "".join(c.args[0] for c in send.call_args_list)
    assert len(text) <= app.settings.reply_chars
    assert text.endswith("[回复超出发送上限]")
    assert (await store.history(event().session))[0].answer == text


async def test_global_silence_is_admin_only_and_can_be_released(app, clients, event):
    send = AsyncMock()
    assert not await app.handle(event("/静默 开", user_id="bob"), send)
    assert "管理员" in send.call_args.args[0]
    assert await app.handle(event("/静默 开", message_id="admin"), send)
    assert not await app.handle(event("你好", message_id="silenced"), send)
    assert await app.handle(event("/静默 关", message_id="resume"), send)
    assert await app.handle(event("你好", message_id="next"), send)
    clients.complete.assert_awaited_once()
