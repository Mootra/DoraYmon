import importlib.util
import re
from importlib.metadata import version
from types import SimpleNamespace

import pytest
from helpers import REPO_ROOT

PLUGIN_DIR = REPO_ROOT / "plugins/doraymon_admin"


def create_plugin():
    spec = importlib.util.spec_from_file_location("test_doraymon_admin", PLUGIN_DIR / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.create_plugin()


def test_locked_host_loads_admin_plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("MAIBOT_INSTANCE_DIR", str(tmp_path / "instance"))
    from src.plugin_runtime.runner.manifest_validator import PluginManifest
    from src.plugin_runtime.runner.plugin_loader import PluginLoader

    manifest = PluginManifest.model_validate_json(
        (PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8")
    )
    loader = PluginLoader(host_version=version("maibot"))
    meta = loader.load_candidate(manifest.id, (PLUGIN_DIR, manifest, PLUGIN_DIR / "plugin.py"))
    assert meta is not None and meta.instance is not None
    assert meta.plugin_id == "mootra.doraymon-admin"
    assert not meta.instance.get_default_config()["plugin"]["enabled"]


@pytest.mark.asyncio
async def test_reply_hooks_track_groups_and_only_renew_successful_group_replies():
    plugin = create_plugin()
    activated = []

    async def activate(session_id, group_id):
        activated.append((session_id, group_id))

    from doraymon.reply_policy import ReplyConfiguration

    plugin._reply_policy = SimpleNamespace(activate=activate, settings=ReplyConfiguration())
    group = {
        "platform": "qq",
        "session_id": "synthetic-group",
        "message_info": {"group_info": {"group_id": "40001"}},
    }
    private = {"platform": "qq", "session_id": "synthetic-private", "message_info": {}}
    await plugin.prepare_reply(group)
    await plugin.prepare_reply(private)
    await plugin.begin_reply("synthetic-private")
    await plugin.begin_reply("synthetic-group")
    await plugin.after_reply(group, sent=False)
    await plugin.after_reply(private, sent=True)
    await plugin.after_reply(group, sent=True)
    assert activated == [("synthetic-group", "40001"), ("synthetic-group", "40001")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "group_id,target,boost",
    [
        ("40001", "30000", 1.0),
        ("40002", "30000", True),
        ("40001", "30002", None),
        (None, "30000", 1.0),
    ],
)
async def test_group_at_priority_reaches_locked_host_without_changing_other_mentions(
    group_id, target, boost, tmp_path, monkeypatch
):
    monkeypatch.setenv("MAIBOT_INSTANCE_DIR", str(tmp_path / "host"))
    from src.chat.utils import utils
    from src.plugin_runtime.hook_payloads import deserialize_session_message

    from doraymon.reply_policy import ReplyConfiguration, ReplySettings

    plugin = create_plugin()
    plugin._bot_qq = "30000"
    plugin._reply_policy = SimpleNamespace(
        settings=ReplyConfiguration(groups={"40002": ReplySettings(at_reply=False)})
    )
    message = {
        "message_id": "synthetic-at",
        "timestamp": "1000",
        "platform": "qq",
        "session_id": "synthetic-session",
        "message_info": {
            "user_info": {"user_id": "30001", "user_nickname": "合成用户"},
            "additional_config": {"kept": "synthetic"},
        },
        "raw_message": [
            {"type": "at", "data": {"target_user_id": target}},
            {"type": "text", "data": "合成消息"},
        ],
    }
    if group_id:
        message["message_info"]["group_info"] = {"group_id": group_id, "group_name": "合成群"}
    result = await plugin.prepare_reply(message)
    additional = message["message_info"]["additional_config"]
    assert additional.get("is_mentioned") == boost
    assert type(additional.get("is_mentioned")) is type(boost)
    assert additional["kept"] == "synthetic"
    if boost is not None:
        assert result["kwargs"]["message"] is message
    monkeypatch.setattr(
        utils,
        "global_config",
        SimpleNamespace(
            bot=SimpleNamespace(nickname="合成机器人", alias_names=[]),
            chat=SimpleNamespace(
                reply_timing=SimpleNamespace(inevitable_at_reply=False, mentioned_bot_reply=False)
            ),
        ),
    )
    monkeypatch.setattr(utils, "get_bot_accounts", lambda platform: {"30000"})
    _, is_at, probability = utils.is_mentioned_bot_in_message(deserialize_session_message(message))
    assert is_at == (target == "30000")
    assert probability == (1.0 if boost is not None and boost is not True else 0.0)


def test_reply_hook_names_exist_in_locked_host(tmp_path, monkeypatch):
    monkeypatch.setenv("MAIBOT_INSTANCE_DIR", str(tmp_path / "instance"))
    from src.chat.message_receive.bot import register_chat_hook_specs
    from src.maisaka.chat_loop_service import register_maisaka_hook_specs
    from src.plugin_runtime.host.hook_spec_registry import HookSpecRegistry
    from src.services.send_service import register_send_service_hook_specs

    registry = HookSpecRegistry()
    specs = (
        register_chat_hook_specs(registry)
        + register_maisaka_hook_specs(registry)
        + register_send_service_hook_specs(registry)
    )
    names = {spec.name for spec in specs}
    assert {
        "chat.receive.after_process",
        "maisaka.replyer.before_request",
        "send_service.after_send",
    } <= names


@pytest.mark.parametrize("text", ["#sl", " #SL ", "/机器人状态", "/机器人帮助", "/好友列表"])
def test_status_and_admin_commands_share_protected_registration(text):
    commands = [c for c in create_plugin().get_components() if c["name"] == "qq_admin"]
    assert len(commands) == 1
    command = commands[0]
    assert command["metadata"]["permission"] == "operator"
    assert re.search(command["metadata"]["command_pattern"], text)


@pytest.mark.asyncio
@pytest.mark.parametrize("sender", ["30001", "30002"])
async def test_admin_command_uses_original_sender_and_adapter_response(tmp_path, sender):
    plugin = create_plugin()
    calls, texts = [], []

    async def call(api, **kwargs):
        calls.append((api, kwargs))
        return {"status": "ok", "retcode": 0, "data": {"message_id": "synthetic-delivered"}}

    async def send(text, stream):
        texts.append((text, stream))
        return True

    plugin._set_context(
        SimpleNamespace(api=SimpleNamespace(call=call), send=SimpleNamespace(text=send))
    )
    config = plugin.get_default_config()
    config["plugin"].update(
        enabled=True, owner_qq="30001", journal_path=str(tmp_path / "admin.sqlite3")
    )
    plugin.set_plugin_config(config)
    await plugin.on_load()
    try:
        await plugin.command(
            text="/私聊 30003 合成消息",
            stream_id="synthetic-stream",
            owner_qq="30001",
            message={"platform": "qq", "message_info": {"user_info": {"user_id": sender}}},
        )
        if sender == "30001":
            assert len(calls) == 1
            assert calls[0][0] == "maibot-team.snowluma-adapter.adapter.napcat.action.call"
            assert calls[0][1]["action_name"] == "send_msg"
            assert calls[0][1]["params"]["user_id"] == 30003
            assert texts == [("消息已发送。", "synthetic-stream")]
        else:
            assert not calls
            assert texts == []
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message,allowed",
    [
        ({"platform": "qq", "message_info": {"user_info": {"user_id": "30001"}}}, True),
        (
            {
                "platform": "qq",
                "message_info": {
                    "user_info": {"user_id": "30001"},
                    "group_info": {"group_id": "40001"},
                },
            },
            True,
        ),
        ({"platform": "qq", "message_info": {"user_info": {"user_id": "30002"}}}, False),
        ({"platform": "other", "message_info": {"user_info": {"user_id": "30001"}}}, False),
        (None, False),
    ],
)
async def test_status_checks_original_identity_before_querying_adapter(message, allowed):
    plugin = create_plugin()
    calls, texts = [], []

    async def call(api, **kwargs):
        calls.append((api, kwargs))
        data = (
            {"app_name": "SyntheticAdapter", "app_version": "1.2.3"}
            if kwargs["action_name"] == "get_version_info"
            else {"online": True, "good": True, "time": 3661}
        )
        return {"status": "ok", "retcode": 0, "data": data}

    async def send(text, stream):
        texts.append((text, stream))

    plugin._set_context(
        SimpleNamespace(api=SimpleNamespace(call=call), send=SimpleNamespace(text=send))
    )
    config = plugin.get_default_config()
    config["plugin"].update(enabled=True, owner_qq="30001")
    plugin.set_plugin_config(config)
    await plugin.on_load()
    try:
        result = await plugin.command(
            text=" #SL ",
            stream_id="synthetic-stream",
            owner_qq="30001",
            message=message,
        )
        assert result[0] is True and result[2] is True
        if allowed:
            assert {c[1]["action_name"] for c in calls} == {"get_version_info", "get_status"}
            assert all(c[1]["params"] == {} for c in calls)
            assert "SyntheticAdapter 状态\n版本：1.2.3" in texts[0][0]
            assert "运行时长：1 小时 1 分 1 秒" in texts[0][0]
        else:
            assert calls == []
            assert texts == []
            assert result[1] == ""
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "retcode", "missing_data"])
async def test_status_reports_unavailable_on_adapter_failure(failure):
    plugin = create_plugin()

    async def call(*args, **kwargs):
        if failure == "timeout":
            raise TimeoutError
        if failure == "retcode":
            return {"status": "failed", "retcode": 1, "data": {}}
        return {"status": "ok", "retcode": 0, "data": {}}

    plugin._set_context(SimpleNamespace(api=SimpleNamespace(call=call)))
    assert await plugin._status() == "QQ 状态暂时无法读取。"
