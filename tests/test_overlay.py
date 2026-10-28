import stat

import pytest

from doraymon.ops.overlay import (
    Change,
    OverlayError,
    apply_overlay,
    merge_overlays,
    write_text_atomic,
)

BASE = """[inner]
version = "8.15.47"

[bot]
nickname = "麦麦" # 显示和自称的名字。
alias_names = [] # 别人可能用来称呼的名字。

[chat]
max_context_size = 30 # 群聊参考的消息数量。

[chat.reply_timing] # 什么时候回复。
talk_value = 1 # 群聊主动说话的频率。

[expression]
# 学习范围。
learning_list = [{platform = "", item_id = "", type = "group", use = true, learn = true}]

[telemetry]
enable = true # 是否发送匿名运行统计。
"""


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "bot_config.toml"
    path.write_text(BASE, encoding="utf-8")
    return path


def test_values_change_and_comments_survive(config):
    changes = apply_overlay(
        config,
        {
            "bot": {"nickname": "DoraYmon", "alias_names": ["哆啦"]},
            "chat": {"reply_timing": {"talk_value": 0.5}},
            "telemetry": {"enable": False},
        },
    )
    text = config.read_text(encoding="utf-8")
    assert 'nickname = "DoraYmon" # 显示和自称的名字。' in text
    assert 'alias_names = ["哆啦"] # 别人可能用来称呼的名字。' in text
    assert "talk_value = 0.5 # 群聊主动说话的频率。" in text
    assert "enable = false # 是否发送匿名运行统计。" in text
    assert [c.path for c in changes] == [
        "bot.nickname",
        "bot.alias_names",
        "chat.reply_timing.talk_value",
        "telemetry.enable",
    ]


def test_lists_of_tables_stay_inline(config):
    rules = [
        {"platform": "", "item_id": "", "type": "group", "use": False, "learn": False},
        {"platform": "qq", "item_id": "20001", "type": "group", "use": True, "learn": True},
    ]
    apply_overlay(config, {"expression": {"learning_list": rules}})
    text = config.read_text(encoding="utf-8")
    assert 'learning_list = [{platform = "", item_id = "", type = "group", use = false' in text
    assert "[[expression.learning_list]]" not in text


def test_model_tables_keep_readable_toml_sections(tmp_path):
    target = tmp_path / "model_config.toml"
    target.write_text('[[models]]\nname = "example"\n', encoding="utf-8")
    apply_overlay(target, {"models": [{"name": "deepseek-flash", "api_provider": "DeepSeek"}]})
    text = target.read_text(encoding="utf-8")
    assert '[[models]]\nname = "deepseek-flash"' in text
    assert 'api_provider = "DeepSeek"' in text


def test_matching_values_are_not_rewritten(config):
    before = config.stat().st_mtime_ns
    assert apply_overlay(config, {"chat": {"max_context_size": 30}}) == []
    assert config.stat().st_mtime_ns == before


def test_unknown_key_stops_the_run(config):
    with pytest.raises(OverlayError, match="chat.reply_timing.talk_values"):
        apply_overlay(config, {"chat": {"reply_timing": {"talk_values": 1}}})
    assert config.read_text(encoding="utf-8") == BASE


def test_type_mismatch_stops_the_run(config):
    with pytest.raises(OverlayError, match="telemetry.enable"):
        apply_overlay(config, {"telemetry": {"enable": "no"}})
    with pytest.raises(OverlayError, match="bot.nickname"):
        apply_overlay(config, {"bot": {"nickname": {"text": "x"}}})


def test_dry_run_reports_without_writing(config):
    changes = apply_overlay(config, {"telemetry": {"enable": False}}, dry_run=True)
    assert changes == [Change("telemetry.enable", True, False)]
    assert config.read_text(encoding="utf-8") == BASE


def test_missing_file_needs_permission_to_create(tmp_path):
    target = tmp_path / "config" / "adapter_policy.toml"
    with pytest.raises(OverlayError):
        apply_overlay(target, {"defaults": {"group": {"default_action": "block"}}})
    apply_overlay(
        target,
        {"defaults": {"group": {"default_action": "block", "allow_ids": ["20001"]}}},
        create_missing=True,
    )
    text = target.read_text(encoding="utf-8")
    assert "[defaults.group]" in text
    assert 'allow_ids = ["20001"]' in text


def test_secret_values_are_masked_in_reports():
    assert Change("client.token", "old", "new").render() == "client.token: *** -> ***"
    assert Change("plugin.amap_key", "", "k").render() == "plugin.amap_key: *** -> ***"
    assert "False" in Change("telemetry.enable", True, False).render()


def test_later_overlays_win_and_tables_merge():
    merged = merge_overlays(
        {"bot": {"nickname": "DoraYmon", "alias_names": ["哆啦"]}, "telemetry": {"enable": False}},
        {"bot": {"alias_names": ["小哆"], "qq_account": "10001"}},
    )
    assert merged == {
        "bot": {"nickname": "DoraYmon", "alias_names": ["小哆"], "qq_account": "10001"},
        "telemetry": {"enable": False},
    }


def test_provider_list_diffs_mask_nested_keys():
    line = Change(
        "api_providers",
        [{"name": "provider", "api_key": "old-secret"}],
        [{"name": "provider", "api_key": "new-secret"}],
    ).render()
    assert "provider" in line
    assert "old-secret" not in line
    assert "new-secret" not in line
    assert "***" in line


def test_atomic_write_keeps_permission_bits(tmp_path):
    path = tmp_path / "model_config.toml"
    path.write_text("a = 1\n", encoding="utf-8")
    path.chmod(0o600)
    write_text_atomic(path, "a = 2")
    assert path.read_text(encoding="utf-8") == "a = 2\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["model_config.toml"]
