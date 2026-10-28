import pytest

from doraymon.__main__ import index
from doraymon.models import Incoming
from doraymon.store import Store


async def test_index_utf8_documents_and_scope(settings, tmp_path):
    folder = tmp_path / "knowledge"
    folder.mkdir()
    (folder / "手册.md").write_text("菜单说明\n合成测试资料", encoding="utf-8")
    assert await index(folder, "user:bot:alice", settings) == 1
    store = Store(settings.database)
    await store.open()
    try:
        event = Incoming(bot_id="bot", user_id="alice", message_id="1", text="菜单")
        assert [p.source for p in await store.search("菜单", event.scopes)] == ["手册.md"]
        assert await store.search("菜单", event.model_copy(update={"group_id": "g1"}).scopes) == []
    finally:
        await store.close()


async def test_index_validates_entire_input_before_replacing(settings, tmp_path):
    folder = tmp_path / "knowledge"
    folder.mkdir()
    (folder / "手册.md").write_text("有效合成文档", encoding="utf-8")
    (folder / "无效.txt").write_bytes(b"\xff")
    with pytest.raises(UnicodeDecodeError):
        await index(folder, "public", settings)
    assert not settings.database.exists()
