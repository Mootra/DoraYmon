import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from doraymon.commands import STORAGE_ERROR, local_reply
from doraymon.store import BEIJING, Store

PREFERENCES_HEADING = "你保存的口味："
NOT_FOUND = "没有找到完全相同的口味记录，请先发送 /我的口味 查看。"
FULL = "最多保存 20 条口味，请先删除不需要的记录。"
TOO_LONG = "口味描述请控制在 100 个字符以内。"
SIGNED_NOW = "签到成功！这是你第 {} 次签到。"
SIGNED_BEFORE = "今天已经签到过啦。累计签到 {} 天。"
SIGN_SUMMARY = "你的累计签到：{} 天。最近签到：{}。"


def at(year, month, day, hour=12):
    return datetime(year, month, day, hour, tzinfo=UTC)


@pytest.fixture
def food(event):
    def make(text, **kwargs):
        return event("/记住口味 " + text, **kwargs)

    return make


async def test_store_keeps_first_seen_order_and_ignores_duplicates(store, food):
    session = food("不吃香菜").session
    assert await store.food_preferences(session) == []
    assert await store.add_food_preference(session, "不吃香菜", limit=20) == "added"
    assert await store.add_food_preference(session, "不吃香菜", limit=20) == "exists"
    assert await store.add_food_preference(session, "喜欢辣", limit=20) == "added"
    assert await store.food_preferences(session) == ["不吃香菜", "喜欢辣"]


async def test_store_limits_each_session_and_deletes_exact_match(store, food):
    session = food("x").session
    for index in range(20):
        assert await store.add_food_preference(session, f"合成口味{index}", limit=20) == "added"
    assert await store.add_food_preference(session, "合成口味20", limit=20) == "full"
    assert len(await store.food_preferences(session)) == 20
    assert not await store.remove_food_preference(session, "合成口味21")
    assert len(await store.food_preferences(session)) == 20
    assert await store.remove_food_preference(session, "合成口味0")
    assert await store.food_preferences(session) == [f"合成口味{i}" for i in range(1, 20)]


async def test_store_isolation_between_member_group_and_bot(store, food):
    private = food("不吃香菜")
    others = [
        food("不吃香菜", group_id="g1"),
        food("不吃香菜", group_id="g2"),
        food("不吃香菜", user_id="bob"),
        food("不吃香菜", bot_id="other"),
    ]
    for item in (private, *others):
        assert await store.add_food_preference(item.session, "不吃香菜", limit=20) == "added"
    await store.remove_food_preference(private.session, "不吃香菜")
    assert await store.food_preferences(private.session) == []
    for item in others:
        assert await store.food_preferences(item.session) == ["不吃香菜"]


async def test_remember_trims_edges_and_keeps_inner_spacing(store, food):
    assert await local_reply(food("  不吃香菜  "), store, None) == "记住了：不吃香菜"
    assert await local_reply(food("我有空格的 口味"), store, None) == "记住了：我有空格的 口味"
    assert await store.food_preferences(food("x").session) == ["不吃香菜", "我有空格的 口味"]


async def test_remember_rejects_empty_and_oversized(store, food):
    empty = await local_reply(food("   "), store, None)
    assert empty == "请告诉我要记住什么，例如：/记住口味 我不吃香菜"
    assert await local_reply(food("甲" * 101), store, None) == TOO_LONG
    assert await store.food_preferences(food("x").session) == []
    assert await local_reply(food("甲" * 100), store, None) == "记住了：" + "甲" * 100
    assert len((await store.food_preferences(food("x").session))[0]) == 100


async def test_remember_reports_full_session_without_growing(store, food):
    for index in range(20):
        await store.add_food_preference(food("x").session, f"合成口味{index}", limit=20)
    assert await local_reply(food("合成口味20"), store, None) == FULL
    assert len(await store.food_preferences(food("x").session)) == 20


@pytest.mark.parametrize("count", [1, 20])
async def test_duplicate_preference_is_reported_before_or_at_limit(store, event, count):
    for index in range(count):
        await store.add_food_preference(event().session, f"口味{index}", limit=20)
    assert await local_reply(event("/记住口味 口味0"), store, None) == "已经记住了：口味0"
    assert await store.food_preferences(event().session) == [f"口味{i}" for i in range(count)]


async def test_preference_order_survives_clock_adjustment(store, event, monkeypatch):
    ticks = iter([300, 200])
    monkeypatch.setattr("doraymon.store.time.time_ns", lambda: next(ticks))
    await local_reply(event("/记住口味 甲"), store, None)
    await local_reply(event("/记住口味 乙"), store, None)
    assert await store.food_preferences(event().session) == ["甲", "乙"]


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
async def test_programming_error_is_not_reported_as_storage_failure(
    store, event, monkeypatch, error_type
):
    def invalid_clock():
        raise error_type("invalid_clock")

    monkeypatch.setattr(store, "clock", invalid_clock)
    with pytest.raises(error_type, match="invalid_clock"):
        await local_reply(event("/签到"), store, None)


async def test_competing_connections_keep_preference_limit(store, event):
    session = event().session
    for index in range(19):
        await store.add_food_preference(session, f"口味{index}", limit=20)
    other = Store(store.path)
    await other.open()
    try:
        results = await asyncio.gather(
            store.add_food_preference(session, "甲", limit=20),
            other.add_food_preference(session, "乙", limit=20),
        )
        assert sorted(results) == ["added", "full"]
        preferences = await store.food_preferences(session)
        assert len(preferences) == 20
        assert len({"甲", "乙"} & set(preferences)) == 1
    finally:
        await other.close()


async def test_list_numbers_entries_in_insertion_order(store, event):
    assert await local_reply(event("/记住口味 甲"), store, None) == "记住了：甲"
    assert await local_reply(event("/记住口味 乙"), store, None) == "记住了：乙"
    listed = await local_reply(event("/我的口味"), store, None)
    assert listed == PREFERENCES_HEADING + "\n" + "1. 甲" + "\n" + "2. 乙"


async def test_forget_only_removes_exact_text(store, event):
    await local_reply(event("/记住口味 不吃香菜"), store, None)
    assert await local_reply(event("/忘记口味 不吃香"), store, None) == NOT_FOUND
    assert await local_reply(event("/忘记口味   "), store, None) == (
        "请输入要忘记的完整口味，例如：/忘记口味 我不吃香菜"
    )
    assert await store.food_preferences(event().session) == ["不吃香菜"]
    assert await local_reply(event("/忘记口味 不吃香菜"), store, None) == "已经忘记：不吃香菜"
    assert await store.food_preferences(event().session) == []


async def test_clear_context_keeps_preferences_and_sign_ins(store, event):
    await local_reply(event("/记住口味 不吃香菜"), store, None)
    await local_reply(event("/签到"), store, None)
    await store.append(event().session, "m1", "问题", "回答")
    await store.clear(event().session)
    assert await store.history(event().session) == []
    assert await store.food_preferences(event().session) == ["不吃香菜"]
    assert (await store.sign_summary(event().session)).total_days == 1


async def test_sign_deduplicates_same_day_and_counts_days(store, event):
    session = event().session
    first = await local_reply(event("/签到"), store, None)
    assert first == SIGNED_NOW.format(1)
    assert await local_reply(event("/签到", message_id="2"), store, None) == SIGNED_BEFORE.format(1)
    assert (await store.sign_summary(session)).total_days == 1


async def test_concurrent_same_day_sign_creates_one_record(store, event):
    session = event().session
    results = await asyncio.gather(
        store.sign_today(session), store.sign_today(session), store.sign_today(session)
    )
    assert sum(1 for result in results if result.created) == 1
    assert {result.total_days for result in results} == {1}
    assert (await store.sign_summary(session)).total_days == 1


async def test_sign_crosses_days_and_reports_latest(store, event):
    session = event().session
    store.clock = lambda: at(2026, 3, 1)
    assert (await store.sign_today(session)).created
    assert not (await store.sign_today(session)).created
    store.clock = lambda: at(2026, 3, 2)
    assert (await store.sign_today(session)).created
    summary = await store.sign_summary(session)
    assert summary.total_days == 2
    assert summary.last_sign_date == "2026-03-02"
    assert await local_reply(event("/我的签到"), store, None) == SIGN_SUMMARY.format(
        2, "2026-03-02"
    )


async def test_empty_sign_summary_is_zero_and_no_date(settings, event):
    fresh = Store(settings.database, clock=lambda: at(2026, 3, 1))
    await fresh.open()
    try:
        assert await local_reply(event("/我的签到"), fresh, None) == SIGN_SUMMARY.format(0, "暂无")
    finally:
        await fresh.close()


async def test_sign_day_follows_beijing_time(settings, event):
    fresh = Store(settings.database, clock=lambda: at(2026, 3, 1, 17))
    await fresh.open()
    try:
        assert (await fresh.sign_today(event().session)).created
        assert (await fresh.sign_summary(event().session)).last_sign_date == "2026-03-02"
        assert at(2026, 3, 1, 17).astimezone(BEIJING).date().isoformat() == "2026-03-02"
    finally:
        await fresh.close()


async def test_sign_isolation_between_member_group_and_bot(store, event):
    private = event()
    others = [
        event(group_id="g1"),
        event(group_id="g2"),
        event(user_id="bob"),
        event(bot_id="other"),
    ]
    store.clock = lambda: at(2026, 3, 1)
    for item in (private, *others):
        assert (await store.sign_today(item.session)).created
    store.clock = lambda: at(2026, 3, 2)
    assert (await store.sign_today(private.session)).created
    assert (await store.sign_summary(private.session)).total_days == 2
    for item in others:
        assert (await store.sign_summary(item.session)).total_days == 1


@pytest.mark.parametrize(
    "text",
    [
        "/记住口味 甲",
        "/我的口味",
        "/忘记口味 甲",
        "/签到",
        "/我的签到",
    ],
)
async def test_closed_database_is_reported_not_hidden(settings, event, text):
    broken = Store(settings.database, clock=lambda: at(2026, 3, 1))
    await broken.open()
    await broken.close()
    assert await local_reply(event(text), broken, None) == STORAGE_ERROR
    with pytest.raises(ValueError, match="no active connection"):
        await broken.food_preferences(event().session)
    with pytest.raises(ValueError, match="no active connection"):
        await broken.sign_summary(event().session)


@pytest.mark.parametrize(
    "text", ["/记住口味 甲", "/我的口味", "/忘记口味 甲", "/签到", "/我的签到"]
)
async def test_sqlite_failure_is_reported_without_fabricating_records(store, event, text):
    await store.db.executescript("DROP TABLE food_preferences; DROP TABLE sign_records;")
    assert await local_reply(event(text), store, None) == STORAGE_ERROR
    with pytest.raises(aiosqlite.OperationalError):
        await store.food_preferences(event().session)


async def test_saved_records_survive_reopening(settings, event):
    saved = Store(settings.database, clock=lambda: at(2026, 3, 1))
    await saved.open()
    try:
        await local_reply(event("/记住口味 不吃香菜"), saved, None)
        await local_reply(event("/签到"), saved, None)
    finally:
        await saved.close()
    restored = Store(settings.database)
    await restored.open()
    try:
        assert await restored.food_preferences(event().session) == ["不吃香菜"]
        summary = await restored.sign_summary(event().session)
        assert (summary.total_days, summary.last_sign_date) == (1, "2026-03-01")
    finally:
        await restored.close()


async def test_application_runs_local_commands_without_model(app, clients, store, event):
    send = AsyncMock()
    assert await app.handle(event("/记住口味 不吃香菜"), send)
    assert await app.handle(event("/签到", message_id="2"), send)
    assert send.call_args_list[0].args[0] == "记住了：不吃香菜"
    assert send.call_args_list[1].args[0] == SIGNED_NOW.format(1)
    clients.complete.assert_not_awaited()
    clients.nearby.assert_not_awaited()
    assert await store.food_preferences(event().session) == ["不吃香菜"]
    assert (await store.sign_summary(event().session)).total_days == 1


async def test_application_clear_context_keeps_saved_records(app, clients, store, event):
    await app.handle(event("/记住口味 不吃香菜"), AsyncMock())
    await app.handle(event("/签到", message_id="2"), AsyncMock())
    await store.append(event().session, "prior", "旧问题", "旧回答")
    send = AsyncMock()
    assert await app.handle(event("/清空上下文", message_id="3"), send)
    assert await store.history(event().session) == []
    assert await store.food_preferences(event().session) == ["不吃香菜"]
    assert (await store.sign_summary(event().session)).total_days == 1
    clients.complete.assert_not_awaited()


async def test_help_lists_every_local_command(store, event):
    text = await local_reply(event("/help"), store, None)
    assert text is not None
    for command in ("/记住口味", "/我的口味", "/忘记口味", "/签到", "/我的签到"):
        assert command in text


async def test_application_concurrent_sign_reports_single_new_record(app, store, event):
    first, second = await asyncio.gather(
        app.handle(event("/签到"), AsyncMock()),
        app.handle(event("/签到", message_id="2"), AsyncMock()),
    )
    assert first and second
    assert (await store.sign_summary(event().session)).total_days == 1


async def test_saved_preferences_never_enter_the_model_prompt(app, clients, store, event):
    await app.handle(event("/记住口味 不吃香菜"), AsyncMock())
    send = AsyncMock()
    assert await app.handle(event("今晚聊点什么", message_id="2"), send)
    prompt = clients.complete.call_args.args[0]
    assert all("不吃香菜" not in message["content"] for message in prompt)
