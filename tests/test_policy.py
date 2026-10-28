import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from doraymon.policy import Policy


@pytest.mark.parametrize("text", ["谢谢", "今天天气不错", "有人打游戏吗"])
async def test_group_social_text_stays_quiet(app, clients, event, text):
    send = AsyncMock()
    assert not await app.handle(event(text, group_id="g1"), send)
    clients.complete.assert_not_awaited()
    send.assert_not_awaited()


async def test_group_recipient_and_member_continuation(app, clients, event):
    send = AsyncMock()
    first = event("帮我解释协程", group_id="g1")
    assert await app.handle(first, send)
    assert await app.handle(event("再具体一点", group_id="g1", message_id="2"), send)
    assert not await app.handle(
        event("我也一样", user_id="bob", group_id="g1", message_id="3"), send
    )
    assert not await app.handle(
        event("帮我看看", group_id="g1", message_id="4", mentions={"bob"}), send
    )
    assert clients.complete.await_count == 2


async def test_group_supplement_before_first_reply_is_merged(app, clients, event):
    app.flow.gap = 0.01
    send = AsyncMock()
    assert all(
        await asyncio.gather(
            app.handle(event("帮我解释异步任务", group_id="g1"), send),
            app.handle(event("举一个简单例子", group_id="g1", message_id="2"), send),
        )
    )
    clients.complete.assert_awaited_once()
    request = clients.complete.call_args.args[0][-1]["content"]
    assert json.loads(request.partition("\n")[2]) == [
        {"message_id": "m1", "text": "帮我解释异步任务"},
        {"message_id": "2", "text": "举一个简单例子"},
    ]
    send.assert_awaited_once_with("合成回答")


async def test_named_bot_can_answer_when_another_member_is_mentioned(app, clients, event):
    send = AsyncMock()
    assert await app.handle(event("小哆，帮我解释这段做法", group_id="g1", mentions={"bob"}), send)
    clients.complete.assert_awaited_once()
    send.assert_awaited_once_with("合成回答")


@pytest.mark.parametrize("closing", ["谢谢", "先这样", "晚安"])
async def test_closing_ends_automatic_followup(app, clients, event, closing):
    send = AsyncMock()
    assert await app.handle(event("/chat 解释协程", group_id="g1"), send)
    assert not await app.handle(event(closing, group_id="g1", message_id="closing"), send)
    assert not await app.handle(event("我去上课了", group_id="g1", message_id="later"), send)
    assert await app.handle(event("帮我再解释线程", group_id="g1", message_id="help"), send)
    assert clients.complete.await_count == 2


async def test_closing_during_generation_keeps_the_branch_closed(app, clients, event):
    entered, hold = asyncio.Event(), asyncio.Event()

    async def generate(messages):
        entered.set()
        await hold.wait()
        return "旧回答"

    clients.complete.side_effect = generate
    send = AsyncMock()
    pending = asyncio.create_task(app.handle(event("帮我解释协程", group_id="g1"), send))
    await entered.wait()
    try:
        assert not await app.handle(event("谢谢", group_id="g1", message_id="closing"), send)
        hold.set()
        assert not await pending
        send.assert_not_awaited()
        assert not await app.handle(event("我去上课了", group_id="g1", message_id="later"), send)
        clients.complete.side_effect = None
        assert await app.handle(event("小哆，帮我解释线程", group_id="g1", message_id="help"), send)
    finally:
        hold.set()
        await pending


@pytest.mark.parametrize("boundary", [{"mentions": {"bob"}}, {"quote_id": "other"}])
async def test_pending_branch_does_not_capture_messages_to_other_members(
    app, clients, event, boundary
):
    app.flow.gap = 0.01
    send = AsyncMock()
    assert await asyncio.gather(
        app.handle(event("帮我解释协程", group_id="g1"), send),
        app.handle(event("举个例子", group_id="g1", message_id="2", **boundary), send),
    ) == [True, False]
    clients.complete.assert_awaited_once()
    assert clients.complete.call_args.args[0][-1]["content"] == "帮我解释协程"


async def test_local_stop_affects_only_that_member(app, event):
    send = AsyncMock()
    assert await app.handle(event("/chat 你好", group_id="g1"), send)
    assert await app.handle(event("别接话了", group_id="g1", message_id="stop"), send)
    assert not await app.handle(event("帮我看看", group_id="g1", message_id="later"), send)
    assert await app.handle(
        event("/chat 你好", group_id="g1", user_id="bob", message_id="bob"), send
    )
    assert await app.handle(
        event("帮我看看", group_id="g1", addressed=True, message_id="direct"), send
    )


async def test_ambient_limit_does_not_consume_direct_capacity(settings, event):
    policy = Policy(settings)
    message = event("求助", group_id="g1")
    assert await policy.admit(message, "ambient")
    assert not await policy.admit(message, "ambient")
    assert await policy.admit(message, "direct")


async def test_bridge_lease_yields_only_bound_private_user(app, clients, event, tmp_path):
    lease = tmp_path / "lease.json"
    lease.write_text(
        json.dumps({"expires": 9999999999, "owner_id": "alice", "active": True}), encoding="utf-8"
    )
    app.settings.bridge_lease = lease
    send = AsyncMock()
    assert not await app.handle(event("电脑"), send)
    assert await app.handle(event("你好", user_id="bob"), send)
    assert clients.complete.await_count == 1


def test_quote_never_imports_private_or_other_room_text(settings, event):
    policy = Policy(settings)
    policy.observe(event("private secret"))
    policy.observe(event("other room secret", group_id="g2", message_id="2"))
    same = event("hi", group_id="g1", quote_id="2")
    assert policy.context(same) == []
    assert policy.reason(same) == ""


async def test_group_nearby_request_and_private_quote_are_admitted(app, event):
    send = AsyncMock()
    assert await app.handle(event("附近有什么好吃的", group_id="g1"), send)
    assert "合成甲店" in send.call_args.args[0]
    assert await app.handle(event("问题", quote_id="unknown", message_id="2"), send)
