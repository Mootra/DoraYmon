import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from helpers import REPO_ROOT

from doraymon.ops.instance import (
    ADAPTER_DIR,
    ADAPTER_VERSION,
    Instance,
    apply_configs,
    configure_adapter,
    install_adapter,
    sync_plugin,
)
from doraymon.ops.napcat import OneBotEndpoint
from doraymon.ops.overlay import OverlayError, load_overlay, merge_overlays

DEPLOY = REPO_ROOT / "deploy" / "maibot"
MAIBOT = Path(sys.executable).with_name("maibot")


@pytest.fixture(scope="module")
def fresh_instance(tmp_path_factory) -> Path:
    """Tests copy a real ``maibot init`` result, using the locked MaiBot's schema."""
    root = tmp_path_factory.mktemp("maibot") / "instance"
    subprocess.run([str(MAIBOT), "init", "--directory", str(root)], check=True, capture_output=True)
    return root


@pytest.fixture
def instance(fresh_instance, tmp_path) -> Instance:
    root = tmp_path / "instance"
    shutil.copytree(fresh_instance, root)
    return Instance(root)


def repo_overlays(*local: Path) -> dict:
    return {
        name: merge_overlays(
            *(
                load_overlay(p)
                for p in (DEPLOY / f"{name}.toml", *(d / f"{name}.toml" for d in local))
                if p.is_file()
            )
        )
        for name in ("bot_config", "model_config", "adapter_policy", "adapter")
    }


def test_repository_overlays_fit_the_locked_maibot_config(instance):
    overlays = repo_overlays(REPO_ROOT / "deploy" / "local.example")
    changes = apply_configs(
        instance,
        bot=overlays["bot_config"],
        model=overlays["model_config"],
        policy=overlays["adapter_policy"],
    )
    paths = {change.path for change in changes}
    assert {"bot.nickname", "telemetry.enable", "expression.learning_list"} <= paths
    assert (
        apply_configs(
            instance,
            bot=overlays["bot_config"],
            model=overlays["model_config"],
            policy=overlays["adapter_policy"],
            dry_run=True,
        )
        == []
    )
    policy = instance.adapter_policy.read_text(encoding="utf-8")
    assert 'default_action = "block"' in policy
    bot = instance.bot_config.read_text(encoding="utf-8")
    assert "# 是否发送匿名运行统计" in bot  # MaiBot's own comments are kept
    model = tomllib.loads(instance.model_config.read_text(encoding="utf-8"))
    assert [item["name"] for item in model["models"]] == ["deepseek-flash"]
    assert model["models"][0]["model_identifier"] == "deepseek-flash"
    tasks = model["model_task_config"]
    assert all(
        tasks[name]["model_list"] == ["deepseek-flash"] for name in ("planner", "replyer", "utils")
    )


def test_model_catalog_and_provider_key_survive_init_and_host_config_upgrade(instance, monkeypatch):
    import tomlkit

    document = tomlkit.parse(instance.model_config.read_text(encoding="utf-8"))
    document["api_providers"][0]["api_key"] = "synthetic-provider-key"
    instance.model_config.write_text(tomlkit.dumps(document), encoding="utf-8")
    overlays = repo_overlays()
    apply_configs(instance, bot={}, model=overlays["model_config"], policy={})
    subprocess.run(
        [str(MAIBOT), "init", "--directory", str(instance.root)], check=True, capture_output=True
    )
    document = tomlkit.parse(instance.model_config.read_text(encoding="utf-8"))
    assert [item["name"] for item in document["models"]] == ["deepseek-flash"]
    document["inner"]["version"] = "0.0.0"
    instance.model_config.write_text(tomlkit.dumps(document), encoding="utf-8")
    monkeypatch.setenv("MAIBOT_INSTANCE_DIR", str(instance.root))
    monkeypatch.setenv("MAIBOT_INITIALIZE_ONLY", "1")
    from src.config.config import MODEL_CONFIG_VERSION, ModelConfig, load_config_from_file

    loaded, upgraded = load_config_from_file(
        ModelConfig, instance.model_config, MODEL_CONFIG_VERSION, True
    )
    assert upgraded
    assert [model.name for model in loaded.models] == ["deepseek-flash"]
    assert loaded.api_providers[0].api_key == "synthetic-provider-key"
    assert loaded.model_task_config.planner.model_list == ["deepseek-flash"]


def test_sync_plugin_mirrors_code_but_keeps_instance_config(tmp_path):
    source = tmp_path / "repo" / "demo"
    (source / "pkg").mkdir(parents=True)
    (source / "plugin.py").write_text("v2", encoding="utf-8")
    (source / "pkg" / "util.py").write_text("u", encoding="utf-8")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "plugin.cpython-313.pyc").write_bytes(b"x")
    plugins = tmp_path / "plugins"
    (plugins / "demo").mkdir(parents=True)
    (plugins / "demo" / "plugin.py").write_text("v1", encoding="utf-8")
    (plugins / "demo" / "config.toml").write_text("amap_key = 'k'", encoding="utf-8")
    (plugins / "demo" / "old.py").write_text("stale", encoding="utf-8")

    assert sync_plugin(source, plugins, dry_run=True)
    assert (plugins / "demo" / "plugin.py").read_text(encoding="utf-8") == "v1"
    actions = sync_plugin(source, plugins)
    assert actions == ["删除 demo/old.py", "写入 demo/pkg/util.py", "写入 demo/plugin.py"]
    assert (plugins / "demo" / "config.toml").read_text(encoding="utf-8") == "amap_key = 'k'"
    assert not (plugins / "demo" / "__pycache__").exists()
    assert sync_plugin(source, plugins) == []


def test_closed_access_policy_is_enforced_by_the_locked_host(instance, monkeypatch):
    monkeypatch.setenv("MAIBOT_INSTANCE_DIR", str(instance.root))
    from src.platform_io.adapter_policy import AdapterIdentity, AdapterPolicyManager

    overlays = repo_overlays()
    apply_configs(
        instance,
        bot=overlays["bot_config"],
        model=overlays["model_config"],
        policy=overlays["adapter_policy"],
    )
    identity = AdapterIdentity(platform="qq", account_id="10001")
    manager = AdapterPolicyManager(instance.adapter_policy)
    for kind, target in (("group", "20001"), ("private", "30001")):
        assert not manager.evaluate(identity, chat_type=kind, target_id=target).allowed
    apply_configs(
        instance,
        bot={},
        model={},
        policy={
            "defaults": {
                "group": {"allow_ids": ["20001"]},
                "private": {"allow_ids": ["30001"]},
            }
        },
    )
    for kind, target in (("group", "20001"), ("private", "30001")):
        assert manager.evaluate(identity, chat_type=kind, target_id=target).allowed
    assert not manager.evaluate(identity, chat_type="group", target_id="20002").allowed
    assert not manager.evaluate(identity, chat_type="private", target_id="30002").allowed


def test_own_plugin_syncs_into_an_instance(instance):
    sync_plugin(REPO_ROOT / "plugins" / "doraymon_meal", instance.plugins)
    assert (instance.plugins / "doraymon_meal" / "_manifest.json").is_file()
    assert not (instance.plugins / "doraymon_meal" / "config.toml").exists()


def test_sync_refuses_a_missing_source_without_removing_installed_files(tmp_path):
    target = tmp_path / "plugins" / "demo"
    target.mkdir(parents=True)
    (target / "plugin.py").write_text("v1", encoding="utf-8")
    with pytest.raises(OverlayError, match="源码目录不存在"):
        sync_plugin(tmp_path / "missing" / "demo", target.parent)
    assert (target / "plugin.py").read_text(encoding="utf-8") == "v1"


def test_sync_does_not_follow_an_existing_plugin_symlink(tmp_path):
    source = tmp_path / "repo" / "demo"
    source.mkdir(parents=True)
    (source / "plugin.py").write_text("v1", encoding="utf-8")
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "demo").symlink_to(source, target_is_directory=True)
    with pytest.raises(OverlayError, match="符号链接"):
        sync_plugin(source, plugins)
    assert (source / "plugin.py").read_text(encoding="utf-8") == "v1"


def install_fake_adapter(instance: Instance, version: str = ADAPTER_VERSION) -> None:
    target = instance.plugins / ADAPTER_DIR
    target.mkdir(parents=True)
    (target / "_manifest.json").write_text(json.dumps({"version": version}), encoding="utf-8")


def test_adapter_endpoint_is_written_before_first_load(instance):
    endpoint = OneBotEndpoint("172.29.16.1", 3001, "tok")
    with pytest.raises(OverlayError):
        configure_adapter(instance, endpoint, {})
    install_fake_adapter(instance)
    settings = load_overlay(DEPLOY / "adapter.toml")
    changes = configure_adapter(instance, endpoint, settings)
    text = instance.adapter_config.read_text(encoding="utf-8")
    assert 'server = "172.29.16.1"' in text
    assert "port = 3001" in text
    assert "enable_mute_tool = false" in text
    assert 'config_version = "2.7.1"' in text
    assert "client.token: *** -> ***" in [change.render() for change in changes]
    assert configure_adapter(instance, endpoint, settings) == []


def test_adapter_config_is_strict_once_it_exists(instance):
    install_fake_adapter(instance)
    instance.adapter_config.write_text(
        '[client]\nserver = "a"\nport = 1\ntoken = ""\n', encoding="utf-8"
    )
    with pytest.raises(OverlayError):
        configure_adapter(instance, OneBotEndpoint("b", 2), {"client": {"unknown": 1}})


def test_install_adapter_clones_the_pinned_release(instance, tmp_path):
    log = tmp_path / "git-args.json"
    fake_git = tmp_path / "git"
    fake_git.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(log)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        "target = pathlib.Path(sys.argv[-1]); target.mkdir(parents=True)\n"
        f"(target / '_manifest.json').write_text(json.dumps({{'version': {ADAPTER_VERSION!r}}}))\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    assert install_adapter(instance, git=str(fake_git)) is True
    args = json.loads(log.read_text(encoding="utf-8"))
    assert args[:5] == ["clone", "--depth", "1", "--branch", f"v{ADAPTER_VERSION}"]
    assert install_adapter(instance, git=str(fake_git)) is False


def test_install_adapter_refuses_an_unexpected_version(instance):
    install_fake_adapter(instance, "0.9.0")
    with pytest.raises(OverlayError, match="0.9.0"):
        install_adapter(instance)
    with pytest.raises(OverlayError, match="0.9.0"):
        configure_adapter(instance, OneBotEndpoint("127.0.0.1", 3901), {})
