import pytest

from doraymon.models import Passage


async def test_turns_are_atomic_bounded_and_idempotent(store, event):
    key = event().session
    for i in range(5):
        await store.append(key, str(i), f"q{i}", f"a{i}")
    await store.append(key, "4", "different", "different")
    assert [(t.question, t.answer) for t in await store.history(key)] == [
        ("q2", "a2"),
        ("q3", "a3"),
        ("q4", "a4"),
    ]
    assert await store.history(event(user_id="bob").session) == []


async def test_clear_only_owns_current_namespace(store, event):
    private = event().session
    group = event(group_id="g1").session
    other = event(bot_id="other").session
    for key in (private, group, other):
        await store.append(key, "1", "question", "answer")
    await store.clear(private)
    assert await store.history(private) == []
    assert len(await store.history(group)) == len(await store.history(other)) == 1


async def test_expired_turns_do_not_reach_prompt(store, event):
    key = event().session
    await store.append(key, "1", "expired", "expired")
    await store.db.execute("UPDATE turns SET created=0")
    await store.db.commit()
    assert await store.history(key) == []
    await store.append(key, "2", "new", "new-answer")
    assert [(t.question, t.answer) for t in await store.history(key)] == [("new", "new-answer")]


async def test_recent_activity_keeps_complete_earlier_turns(store, event):
    key = event().session
    await store.append(key, "1", "earlier", "earlier-answer")
    await store.append(key, "2", "recent", "recent-answer")
    await store.db.execute("UPDATE turns SET created=0 WHERE event='1'")
    await store.db.commit()
    assert [t.question for t in await store.history(key)] == ["earlier", "recent"]


async def test_knowledge_permissions_precede_top_k(store, event):
    denied = [Passage(source=f"private-{i}", body="合成知识 测试问题 测试问题") for i in range(8)]
    await store.replace_knowledge("user:bot:bob", denied)
    await store.replace_knowledge(
        "public", [Passage(source="public", body="合成知识的测试问题说明")]
    )
    await store.replace_knowledge(
        "user:bot:alice", [Passage(source="mine", body="合成知识的测试问题解答")]
    )
    await store.replace_knowledge(
        "group:bot:g1", [Passage(source="group", body="合成知识的测试问题群规则")]
    )
    assert {p.source for p in await store.search("测试问题", event().scopes)} == {"public", "mine"}
    assert {p.source for p in await store.search("测试问题", event(group_id="g1").scopes)} == {
        "public",
        "group",
    }


@pytest.mark.parametrize("query", ["火星没有匹配", '" OR scope:*', "%%", "_x"])
async def test_unknown_or_operator_queries_do_not_expose_data(store, event, query):
    await store.replace_knowledge(
        "public", [Passage(source="manual", body="部署时需要创建虚拟环境")]
    )
    await store.replace_knowledge("user:bot:bob", [Passage(source="secret", body=query)])
    assert await store.search(query, event().scopes) == []


async def test_two_character_chinese_and_index_replacement(store, event):
    await store.replace_knowledge("public", [Passage(source="first", body="菜单在资料目录")])
    assert [p.source for p in await store.search("菜单", event().scopes)] == ["first"]
    await store.replace_knowledge("public", [Passage(source="second", body="菜单更新")])
    assert [p.source for p in await store.search("菜单", event().scopes)] == ["second"]
