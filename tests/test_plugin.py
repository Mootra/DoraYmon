import asyncio
import base64
import importlib.util
import logging
from importlib.metadata import version

import pytest
from helpers import REPO_ROOT, FakeLookup, make_campus_catalog, make_place

from doraymon.meal import MealReport, MealService

PLUGIN_DIR = REPO_ROOT / "plugins" / "doraymon_meal"


def load_plugin_module():
    spec = importlib.util.spec_from_file_location("doraymon_meal_plugin", PLUGIN_DIR / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeSend:
    def __init__(self):
        self.texts: list[tuple[str, str]] = []
        self.files = []
        self.result = {"sent": True, "message_id": "delivered-1"}

    async def text(self, text, stream_id, *, return_details=False):
        assert return_details
        self.texts.append((text, stream_id))
        return self.result

    async def custom(self, kind, payload, stream_id, *, return_details=False):
        assert kind == "file" and return_details
        self.files.append((payload, stream_id))
        return self.result


class FakeMessage:
    """按 SDK 的实际契约返回：找到就是消息字典，找不到是 None。"""

    async def get_by_id(self, message_id, *, stream_id):
        if (stream_id, message_id) not in {("s1", "m1"), ("s1", "m2"), ("s1", "m3"), ("s2", "m1")}:
            return None
        return {
            "message_info": {
                "user_info": {
                    "user_id": "30002" if message_id == "m2" else "30001",
                    "user_nickname": "同名群友",
                }
            }
        }


class FakeContext:
    def __init__(self):
        self.send = FakeSend()
        self.message = FakeMessage()
        self.logger = logging.getLogger("doraymon-meal-test")


def configure(plugin, **values):
    config = plugin.get_default_config()
    config["plugin"].update(values)
    plugin.set_plugin_config(config)


@pytest.fixture
async def plugin():
    plugin = load_plugin_module().create_plugin()
    configure(plugin, default_location="合成校区")
    plugin._set_context(FakeContext())
    await plugin.on_load()
    yield plugin
    await plugin.on_unload()


def use_lookup(plugin, *places):
    plugin._service = MealService(FakeLookup(places), default_location="合成校区")


async def test_tool_returns_facts_for_the_planner(plugin):
    use_lookup(plugin, make_place(1), make_place(2))
    result = await plugin.nearby_food(msg_id="m1", stream_id="s1")
    assert result["success"] is True
    assert "合成餐馆1" in result["content"] and "合成路1号" in result["content"]
    assert "合成校区" in result["content"]


async def test_another_and_map_follow_the_same_member(plugin):
    use_lookup(plugin, make_place(1), make_place(2))
    await plugin.nearby_food(msg_id="m1", stream_id="s1")
    changed = await plugin.nearby_food(change="another", msg_id="m3", stream_id="s1")
    assert "合成餐馆2" in changed["content"]
    mapped = await plugin.nearby_food(change="map", msg_id="m1", stream_id="s1")
    assert mapped["stop_after_execution"] is True
    [(text, stream)] = plugin.ctx.send.texts
    assert stream == "s1"
    assert text.startswith("合成餐馆2\n合成路2号\nhttps://uri.amap.com/marker")


async def test_state_is_separate_per_stream_and_per_member_with_the_same_nickname(plugin):
    use_lookup(plugin, make_place(1), make_place(2))
    await plugin.nearby_food(msg_id="m1", stream_id="s1")
    await plugin.nearby_food(change="another", msg_id="m1", stream_id="s1")
    other = await plugin.nearby_food(change="another", msg_id="m2", stream_id="s1")
    elsewhere = await plugin.nearby_food(msg_id="m1", stream_id="s2")
    assert "合成餐馆1" in other["content"]
    assert "合成餐馆1" in elsewhere["content"]


@pytest.mark.parametrize("stream,msg", [("", "m1"), ("s1", ""), ("s1", "unknown")])
async def test_missing_or_unavailable_message_does_not_share_state(plugin, stream, msg):
    use_lookup(plugin, make_place(1))
    result = await plugin.nearby_food(msg_id=msg, stream_id=stream)
    assert result["success"] is False
    assert plugin._states == {}
    assert plugin._service.lookup.calls == []


@pytest.mark.parametrize("fields", [{"count": 9}, {"count": 0}, {"budget_yuan": 0}])
async def test_invalid_arguments_are_reported(plugin, fields):
    use_lookup(plugin, make_place(1))
    result = await plugin.nearby_food(msg_id="m1", stream_id="s1", **fields)
    assert result["success"] is False


async def test_without_amap_key_the_tool_says_it_cannot_look_up(plugin):
    result = await plugin.nearby_food(msg_id="m1", stream_id="s1")
    assert result["success"] is False
    assert "高德 key" in result["error"]


async def test_disabled_plugin_refuses(plugin):
    configure(plugin, enabled=False)
    result = await plugin.nearby_food(msg_id="m1", stream_id="s1")
    assert result["success"] is False


async def test_campus_pick_runs_without_a_map_key_and_keeps_filters_per_member(plugin, tmp_path):
    catalog = make_campus_catalog(tmp_path)
    configure(plugin, candidate_database_path=str(catalog.path))
    await plugin.on_config_update("self", {}, "1.0.0")
    first = await plugin.campus_food(
        msg_id="m1", stream_id="s1", campus_scope="校内", category="粉面", count=3
    )
    assert first["success"]
    assert "合成粉店" in first["content"] and "合成面店" in first["content"]
    exhausted = await plugin.campus_food(msg_id="m3", stream_id="s1", change="another")
    assert "都选过了" in exhausted["content"]
    other = await plugin.campus_food(
        msg_id="m2", stream_id="s1", campus_scope="校内", category="粉面", count=3
    )
    assert "合成粉店" in other["content"] and "合成面店" in other["content"]
    assert plugin._http is None and plugin.ctx.send.texts == []


async def test_campus_followups_serialize_and_cancel_resets_only_that_member(plugin, tmp_path):
    plugin._campus_catalog = make_campus_catalog(tmp_path)
    await plugin.campus_food(msg_id="m1", stream_id="s1", campus_scope="校内", category="粉面")
    await plugin.campus_food(msg_id="m2", stream_id="s1", campus_scope="校外", category="咖啡")
    await asyncio.gather(
        *(plugin.campus_food(msg_id="m1", stream_id="s1", change="another") for _ in range(2))
    )
    assert set(plugin._campus_states[("s1", "30001")].seen) == {"a", "b"}
    await plugin.campus_food(msg_id="m1", stream_id="s1", change="cancel")
    assert ("s1", "30001") not in plugin._campus_states
    assert plugin._campus_states[("s1", "30002")].seen == ("f",)
    restarted = await plugin.campus_food(
        msg_id="m1", stream_id="s1", campus_scope="校内", category="粉面", count=3
    )
    assert "合成粉店" in restarted["content"] and "合成面店" in restarted["content"]


async def test_campus_pick_clears_an_earlier_map_selection_and_reload_clears_decisions(
    plugin, tmp_path
):
    catalog = make_campus_catalog(tmp_path)
    configure(plugin, candidate_database_path=str(catalog.path))
    await plugin.on_config_update("self", {}, "1.0.0")
    use_lookup(plugin, make_place(1))
    await plugin.nearby_food(msg_id="m1", stream_id="s1")
    await plugin.campus_food(msg_id="m1", stream_id="s1")
    assert plugin._states == {} and plugin._campus_states
    mapped = await plugin.nearby_food(msg_id="m1", stream_id="s1", change="map")
    assert "还没有推荐过店" in mapped["content"] and plugin.ctx.send.texts == []
    await plugin.on_config_update("self", {}, "1.0.0")
    assert plugin._campus_states == {}


async def test_campus_identity_arguments_and_unavailable_catalog_are_reported(plugin, tmp_path):
    plugin._campus_catalog = make_campus_catalog(tmp_path)
    missing = await plugin.campus_food(msg_id="unknown", stream_id="s1")
    assert not missing["success"] and plugin._campus_states == {}
    invalid = await plugin.campus_food(msg_id="m1", stream_id="s1", campus_scope="附近")
    assert not invalid["success"]
    plugin._campus_catalog = type(plugin._campus_catalog)(tmp_path / "missing.sqlite3")
    unavailable = await plugin.campus_food(msg_id="m1", stream_id="s1")
    assert not unavailable["success"] and "读不到店铺清单" in unavailable["error"]


def test_identity_lookup_accepts_current_and_legacy_sdk_shapes():
    module = load_plugin_module()
    message = {"message_info": {"user_info": {"user_id": "30001"}}}
    assert module._message_from_lookup(message) == message
    assert module._message_from_lookup({"success": True, "message": message}) == message
    assert module._message_from_lookup(None) is None
    assert module._message_from_lookup({"success": True, "message": None}) is None
    assert module._message_from_lookup({"success": False, "error": "capability denied"}) is None


def test_campus_tool_is_registered_with_the_locked_sdk():
    plugin = load_plugin_module().create_plugin()
    [tool] = [c for c in plugin.get_components() if c.get("name") == "campus_food"]
    parameters = {p["name"]: p for p in tool["metadata"]["parameters"]}
    assert parameters["msg_id"]["required"] is True
    assert parameters["campus_scope"]["enum_values"] == ["校内", "校外", "不限"]


@pytest.mark.parametrize(
    "delivery", [{"sent": False, "message_id": None}, {"sent": True, "message_id": None}]
)
async def test_unconfirmed_map_delivery_is_not_reported_as_sent_or_retried(plugin, delivery):
    use_lookup(plugin, make_place(1))
    await plugin.nearby_food(msg_id="m1", stream_id="s1")
    plugin.ctx.send.result = delivery
    result = await plugin.nearby_food(change="map", msg_id="m1", stream_id="s1")
    assert result["success"] is False
    assert "发送未获平台确认" in result["error"]
    assert len(plugin.ctx.send.texts) == 1


async def test_concurrent_follow_ups_preserve_exclusions(plugin):
    class YieldingLookup(FakeLookup):
        async def nearby(self, *args):
            await asyncio.sleep(0)
            return await super().nearby(*args)

    plugin._service = MealService(
        YieldingLookup([make_place(n) for n in range(1, 4)]), default_location="合成校区"
    )
    await plugin.nearby_food(msg_id="m1", stream_id="s1")
    replies = await asyncio.gather(
        *(plugin.nearby_food(change="another", msg_id="m1", stream_id="s1") for _ in range(2))
    )
    assert "合成餐馆2" in replies[0]["content"]
    assert "合成餐馆3" in replies[1]["content"]


def test_tool_declares_message_identity_and_optional_search_parameters():
    plugin = load_plugin_module().create_plugin()
    [tool] = [c for c in plugin.get_components() if c.get("name") == "nearby_food"]
    parameters = {p["name"]: p for p in tool["metadata"]["parameters"]}
    assert parameters["msg_id"]["required"] is True
    for name in ("location", "budget_yuan", "change", "count"):
        assert parameters[name]["required"] is False


async def test_config_reload_waits_for_inflight_lookup_then_discards_its_state(plugin):
    started, release = asyncio.Event(), asyncio.Event()

    class WaitingLookup(FakeLookup):
        async def nearby(self, *args):
            started.set()
            await release.wait()
            return await super().nearby(*args)

    plugin._service = MealService(WaitingLookup([make_place(1)]), default_location="合成校区")
    querying = asyncio.create_task(plugin.nearby_food(msg_id="m1", stream_id="s1"))
    async with asyncio.timeout(1):
        await started.wait()
    configure(plugin, default_location="新起点")
    reloading = asyncio.create_task(plugin.on_config_update("self", {}, "1.0.0"))
    try:
        await asyncio.sleep(0)
        assert not reloading.done()
    finally:
        release.set()
        await asyncio.gather(querying, reloading)
    assert plugin._states == {}
    assert plugin._service.default_location == "新起点"


def test_manifest_is_valid_for_the_locked_maibot(tmp_path, monkeypatch):
    monkeypatch.setenv("MAIBOT_INSTANCE_DIR", str(tmp_path / "instance"))
    from src.plugin_runtime.runner.manifest_validator import PluginManifest, VersionComparator

    manifest = PluginManifest.model_validate_json(
        (PLUGIN_DIR / "_manifest.json").read_text("utf-8")
    )
    for name, bounds in (
        ("maibot", manifest.host_application),
        ("maibot-plugin-sdk", manifest.sdk),
    ):
        assert VersionComparator.is_in_range(version(name), bounds.min_version, bounds.max_version)[
            0
        ]
    assert {"send.text", "send.custom", "message.get_by_id"} <= set(manifest.capabilities)


async def test_slow_member_does_not_block_another_member(plugin):
    entered, release = asyncio.Event(), asyncio.Event()

    class WaitingLookup(FakeLookup):
        async def nearby(self, location, radius):
            if location == "慢起点":
                entered.set()
                await release.wait()
            return await super().nearby(location, radius)

    plugin._service = MealService(WaitingLookup([make_place(1)]), default_location="合成校区")
    slow = asyncio.create_task(plugin.nearby_food(location="慢起点", msg_id="m1", stream_id="s1"))
    try:
        async with asyncio.timeout(1):
            await entered.wait()
            other = await plugin.nearby_food(msg_id="m2", stream_id="s1")
        assert other["success"] and not slow.done()
    finally:
        release.set()
        await slow


@pytest.mark.parametrize("replacement", ["cancel", "none"])
async def test_cancel_or_new_search_stops_old_lookup_and_cleans_up_progress(plugin, replacement):
    entered, cleaned, progress_sent = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class WaitingLookup(FakeLookup):
        async def nearby(self, location, radius):
            if location == "慢起点":
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
            return await super().nearby(location, radius)

    original_send = plugin.ctx.send.text

    async def send(text, *args, **kwargs):
        result = await original_send(text, *args, **kwargs)
        progress_sent.set()
        return result

    plugin.ctx.send.text = send
    plugin._progress_delay = 0
    plugin._progress_interval = 0
    plugin._service = MealService(WaitingLookup([make_place(1)]), default_location="合成校区")
    slow = asyncio.create_task(plugin.nearby_food(location="慢起点", msg_id="m1", stream_id="s1"))
    async with asyncio.timeout(1):
        await entered.wait()
        await progress_sent.wait()
        result = await plugin.nearby_food(
            change=replacement, location="新起点", msg_id="m3", stream_id="s1"
        )
        old = await slow
    assert cleaned.is_set() and old["stop_after_execution"]
    assert result["success"]
    assert plugin._jobs == {} and plugin._member_locks == {}
    sent_count = len(plugin.ctx.send.texts)
    await asyncio.sleep(0)
    assert len(plugin.ctx.send.texts) == sent_count
    if replacement == "cancel":
        assert plugin._states == {}
    else:
        assert plugin._states[("s1", "30001")].location == "新起点"


async def test_export_uses_same_snapshot_and_does_not_requery_or_share_members(plugin):
    use_lookup(plugin, make_place(1))
    await plugin.nearby_food(msg_id="m1", stream_id="s1", requirements="少辣")
    stored = plugin._states[("s1", "30001")].report
    unavailable = await plugin.meal_export(msg_id="m2", stream_id="s1")
    assert not unavailable["success"] and plugin.ctx.send.files == []
    result = await plugin.meal_export(msg_id="m1", stream_id="s1", format="json")
    assert result["success"] and result["stop_after_execution"]
    [(payload, stream)] = plugin.ctx.send.files
    assert stream == "s1" and payload["name"].endswith(".json")
    content = base64.b64decode(payload["url"].removeprefix("base64://"))
    assert payload["size"] == len(content)
    assert MealReport.model_validate_json(content) == stored
    assert len(plugin._service.lookup.calls) == 1
    assert "少辣" not in content.decode()


@pytest.mark.parametrize(
    "delivery", [{"sent": False, "message_id": None}, {"sent": True, "message_id": None}]
)
async def test_export_does_not_retry_or_report_unconfirmed_delivery(plugin, delivery):
    use_lookup(plugin, make_place(1))
    await plugin.nearby_food(msg_id="m1", stream_id="s1")
    plugin.ctx.send.result = delivery
    result = await plugin.meal_export(msg_id="m1", stream_id="s1")
    assert not result["success"] and "未获平台确认" in result["error"]
    assert len(plugin.ctx.send.files) == 1


async def test_export_file_payload_survives_locked_host_conversion(plugin, tmp_path, monkeypatch):
    monkeypatch.setenv("MAIBOT_INSTANCE_DIR", str(tmp_path / "instance"))
    from src.common.data_models.message_component_data_model import FileComponent

    use_lookup(plugin, make_place(1))
    await plugin.nearby_food(msg_id="m1", stream_id="s1")
    await plugin.meal_export(msg_id="m1", stream_id="s1")
    [(payload, _)] = plugin.ctx.send.files
    forwarded = FileComponent.from_payload(payload).to_payload()
    assert forwarded["url"] == payload["url"]
    assert forwarded["name"] == payload["name"]


async def test_database_configuration_and_member_cache_use_actual_message_identity(
    plugin, tmp_path
):
    class Source:
        searches = 0

        def __call__(self, *args, **kwargs):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        async def call(self, name, arguments):
            if name == "dianping_search":
                self.searches += 1
                return {"items": [{"id": "shop-1", "name": "合成餐馆1"}]}
            return {"name": "合成餐馆1", "rating": f"4.{self.searches}"}

    source = Source()
    configure(
        plugin,
        meal_database_path=str(tmp_path / "meal.sqlite3"),
        dianping_mcp_url="https://dp.test/mcp",
    )
    await plugin.on_config_update("self", {}, "1.0.0")
    plugin._service.lookup = FakeLookup((make_place(1).model_copy(update={"city": "合成市"}),))
    plugin._service.sources.client_factory = source
    first = await plugin.nearby_food(msg_id="m1", stream_id="s1")
    other = await plugin.nearby_food(msg_id="m2", stream_id="s1")
    again = await plugin.nearby_food(msg_id="m3", stream_id="s1")
    assert "平台评分：4.1" in first["content"]
    assert "平台评分：4.2" in other["content"]
    assert "平台评分：4.1" in again["content"] and source.searches == 2
    assert plugin.ctx.send.texts == []
