import asyncio
import tomllib
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from doraymon.admin import AdminService
from doraymon.admin_http import create_admin_app
from doraymon.reply_policy import GroupReplyUpdate, ReplyPolicy, ReplySettings, ReplySettingsStore


@pytest.fixture
def store(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        '[bot]\nnickname="合成机器人"\n[chat.reply_timing]\n'
        "talk_value=1.0\ninevitable_at_reply=true\nmentioned_bot_reply=true\n"
        'enable_talk_value_rules=true\nmessage_debounce_mode="natural"\n'
        'talk_value_rules=[{platform="qq",item_id="40009",rule_type="group",'
        'time="*",value=0.4}]\n',
        encoding="utf-8",
    )
    (tmp_path / "adapter_policy.toml").write_text(
        '[defaults.group]\ndefault_action="block"\nallow_ids=["40001","40002"]\n',
        encoding="utf-8",
    )
    overlay = tmp_path / "local.toml"
    overlay.write_text('[bot]\nnickname="本机名称"\n', encoding="utf-8")
    return ReplySettingsStore(tmp_path / "reply.json", config, overlay)


def test_group_save_preserves_other_groups_config_and_access(store):
    before = tomllib.loads(store.bot_config.read_text())
    access = store.bot_config.with_name("adapter_policy.toml").read_bytes()
    settings = ReplySettings(at_reply=False, normal_frequency=0.1, conversation_frequency=0.6)
    store.save("40001", settings)
    saved = store.read()
    assert saved.for_group("40001") == settings
    assert saved.for_group("40002") == ReplySettings()
    after = tomllib.loads(store.bot_config.read_text())
    timing = after["chat"]["reply_timing"]
    assert timing["talk_value"] == 1.0
    assert not timing["inevitable_at_reply"]
    assert not timing["mentioned_bot_reply"]
    assert timing["enable_talk_value_rules"]
    assert timing["talk_value_rules"] == [
        *before["chat"]["reply_timing"]["talk_value_rules"],
        {"platform": "qq", "item_id": "40001", "rule_type": "group", "time": "*", "value": 0.1},
        {"platform": "qq", "item_id": "40002", "rule_type": "group", "time": "*", "value": 0.02},
    ]
    assert after["bot"] == before["bot"]
    assert timing["message_debounce_mode"] == "natural"
    overlay = tomllib.loads(store.overlay.read_text())
    assert overlay["chat"]["reply_timing"]["talk_value_rules"] == timing["talk_value_rules"]
    assert store.bot_config.with_name("adapter_policy.toml").read_bytes() == access
    assert store.bot_config.with_name("config.toml.reply-backup").stat().st_mode & 0o777 == 0o600
    store.save("40002", ReplySettings(conversation_minutes=2))
    assert store.read().for_group("40001") == settings


def test_legacy_shared_values_seed_each_group_without_reactivating_disabled_rules(store):
    legacy = ReplySettings(conversation_frequency=0.5, conversation_minutes=5)
    store.path.write_text(legacy.model_dump_json())
    text = store.bot_config.read_text().replace(
        "enable_talk_value_rules=true", "enable_talk_value_rules=false"
    )
    store.bot_config.write_text(text)
    assert store.read().for_group("40001") == legacy
    assert store.read().for_group("40002") == legacy
    changed = ReplySettings(at_reply=False, conversation_frequency=0.6, conversation_minutes=2)
    store.save("40001", changed)
    assert store.read().for_group("40002") == legacy
    assert store.read().defaults == legacy
    rules = tomllib.loads(store.bot_config.read_text())["chat"]["reply_timing"]["talk_value_rules"]
    assert {rule["item_id"] for rule in rules} == {"40001", "40002"}


@pytest.mark.parametrize(
    "values",
    [
        {"normal_frequency": 0},
        {"normal_frequency": float("nan")},
        {"normal_frequency": 1.1},
        {"conversation_minutes": -1},
        {"conversation_minutes": 61},
        {"normal_frequency": 0.9, "conversation_frequency": 0.8},
        {"at_reply": "true"},
        {"config_path": "/another/file"},
    ],
)
def test_rejects_invalid_or_unknown_reply_controls(values):
    with pytest.raises(ValidationError):
        ReplySettings(**values)


@pytest.mark.parametrize("group_id", ["", "../other", "0", "4000", 40001])
def test_group_update_requires_complete_group_number(group_id):
    with pytest.raises(ValidationError):
        GroupReplyUpdate(group_id=group_id, settings=ReplySettings())


def test_multi_file_save_rolls_back_when_overlay_write_fails(store, monkeypatch):
    from doraymon import reply_policy

    originals = {p: p.read_bytes() for p in (store.bot_config, store.overlay)}
    write = reply_policy.write_text_atomic

    def fail_overlay(path, text):
        if path == store.overlay:
            raise OSError("synthetic write failure")
        write(path, text)

    monkeypatch.setattr(reply_policy, "write_text_atomic", fail_overlay)
    with pytest.raises(OSError):
        store.save("40001", ReplySettings())
    assert {p: p.read_bytes() for p in originals} == originals
    assert not store.path.exists()


class Frequency:
    def __init__(self):
        self.adjustments = {"group-a": 1.0, "group-b": 0.5, "silent": 0.0}
        self.bases = {"group-a": 0.02, "group-b": 0.1, "silent": 0.02}

    async def get_adjust(self, session_id):
        return self.adjustments[session_id]

    async def get_current_talk_value(self, session_id):
        return self.bases[session_id] * self.adjustments[session_id]

    async def set_adjust(self, session_id, value):
        self.adjustments[session_id] = value
        return True


@pytest.mark.asyncio
async def test_groups_use_their_own_conversation_frequency_and_window(store):
    waits = []

    async def sleep(seconds):
        event = asyncio.Event()
        waits.append((seconds, event))
        await event.wait()

    store.save("40001", ReplySettings())
    store.save(
        "40002",
        ReplySettings(normal_frequency=0.1, conversation_frequency=0.6, conversation_minutes=2),
    )
    frequency = Frequency()
    policy = ReplyPolicy(store, None, frequency, sleep=sleep)
    try:
        await policy.activate("group-a", "40001")
        await asyncio.sleep(0)
        old_timer = policy.active["group-a"].timer
        await policy.activate("group-b", "40002")
        await asyncio.sleep(0)
        await policy.activate("group-a", "40001")
        await asyncio.sleep(0)
        assert old_timer.cancelled()
        assert [seconds for seconds, _ in waits] == [300, 120, 300]
        assert frequency.adjustments["group-a"] == 40
        assert frequency.adjustments["group-b"] == pytest.approx(6)
        waits[1][1].set()
        await policy.active["group-b"].timer
        assert frequency.adjustments["group-b"] == 0.5
        assert frequency.adjustments["group-a"] == 40
        waits[2][1].set()
        await policy.active["group-a"].timer
        assert frequency.adjustments["group-a"] == 1
        assert not policy.active
    finally:
        await policy.close()


@pytest.mark.asyncio
async def test_one_group_can_disable_conversation_while_another_remains_active(store):
    store.save("40001", ReplySettings(conversation_minutes=0))
    store.save("40002", ReplySettings(normal_frequency=0.1, conversation_frequency=0.6))
    frequency = Frequency()
    policy = ReplyPolicy(store, None, frequency)
    await policy.activate("silent", "40002")
    await policy.activate("group-a", "40001")
    assert not policy.active
    await policy.activate("group-b", "40002")
    assert frequency.adjustments["group-b"] == pytest.approx(6)
    await policy.close()
    assert frequency.adjustments["group-b"] == 0.5


@pytest.mark.asyncio
@pytest.mark.allow_hosts(["127.0.0.1"])
async def test_reply_api_auth_group_validation_persistence_and_application_status(store):
    from aiohttp.test_utils import TestClient, TestServer

    store.save("40001", ReplySettings())
    runtime = tomllib.loads(store.bot_config.read_text())["chat"]["reply_timing"]

    async def config_get(key):
        return runtime[key.rsplit(".", 1)[1]]

    policy = ReplyPolicy(store, SimpleNamespace(get=config_get), Frequency())
    service = AdminService("30001", None)
    token = "synthetic-reply-settings-access-token"
    app = create_admin_app(service, token, lambda: {}, reply_policy=policy)
    headers = {"Authorization": "Bearer " + token}
    changed = ReplySettings(at_reply=False, conversation_minutes=3)
    payload = {"group_id": "40001", "settings": changed.model_dump()}
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/reply-settings")).status == 401
        assert (
            await client.post(
                "/reply-settings",
                headers={**headers, "Origin": "https://example.com"},
                json=payload,
            )
        ).status == 403
        before = store.path.read_bytes()
        for invalid in (
            {"normal_frequency": 0},
            {"settings": changed.model_dump()},
            {**payload, "group_id": "other"},
        ):
            assert (
                await client.post("/reply-settings", headers=headers, json=invalid)
            ).status == 400
        assert store.path.read_bytes() == before
        response = await client.get("/reply-settings", headers=headers)
        assert all(group["applied"] for group in (await response.json())["groups"])
        response = await client.post("/reply-settings", headers=headers, json=payload)
        assert (await response.json()) == {"ok": True, "applied": False}
        assert store.read().for_group("40001") == changed
        groups = {row["group_id"]: row for row in (await policy.read())["groups"]}
        assert not groups["40001"]["applied"]
        assert groups["40002"]["applied"]
    restarted = ReplyPolicy(store, policy.config, policy.frequency)
    assert all(row["applied"] for row in (await restarted.read())["groups"])


def test_native_group_rules_resolve_separately_in_locked_host(store, tmp_path, monkeypatch):
    monkeypatch.setenv("MAIBOT_INSTANCE_DIR", str(tmp_path / "host"))
    from src.common.utils import utils_config
    from src.config.official_configs import ChatReplyTimingConfig

    store.save("40001", ReplySettings(normal_frequency=0.1))
    store.save("40002", ReplySettings(normal_frequency=0.3))
    timing = tomllib.loads(store.bot_config.read_text())["chat"]["reply_timing"]
    timing = ChatReplyTimingConfig.model_validate(timing)
    monkeypatch.setattr(
        utils_config, "global_config", SimpleNamespace(chat=SimpleNamespace(reply_timing=timing))
    )
    monkeypatch.setattr(
        utils_config.ChatConfigUtils,
        "_get_chat_stream",
        lambda session: SimpleNamespace(
            platform="qq", group_id={"a": "40001", "b": "40002", "c": "40003"}[session]
        ),
    )
    assert utils_config.ChatConfigUtils.get_talk_value("a", is_group_chat=True) == 0.1
    assert utils_config.ChatConfigUtils.get_talk_value("b", is_group_chat=True) == 0.3
    assert utils_config.ChatConfigUtils.get_talk_value("c", is_group_chat=True) == 1.0
