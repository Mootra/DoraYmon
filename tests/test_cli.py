import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from helpers import REPO_ROOT

from doraymon.ops import cli
from doraymon.ops.instance import ADAPTER_DIR, ADAPTER_VERSION

LOCAL = REPO_ROOT / "deploy" / "local.example" / "replay"
MAIBOT = Path(sys.executable).with_name("maibot")


@pytest.fixture(scope="module")
def fresh_instance(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("cli") / "instance"
    subprocess.run([str(MAIBOT), "init", "--directory", str(root)], check=True, capture_output=True)
    return root


@pytest.fixture
def instance(fresh_instance, tmp_path) -> Path:
    root = tmp_path / "instance"
    shutil.copytree(fresh_instance, root)
    return root


def run(*args: str) -> int:
    return cli.main(list(args))


def test_check_needs_an_initialized_instance(tmp_path, capsys):
    assert run("check", "--instance", str(tmp_path / "none"), "--local", str(LOCAL)) == 2
    assert "尚未初始化" in capsys.readouterr().err


def test_prepare_then_check_reports_no_drift(instance, capsys):
    assert run("check", "--instance", str(instance), "--local", str(LOCAL)) == 1
    assert run("prepare", "--instance", str(instance), "--local", str(LOCAL)) == 0
    assert "telemetry.enable: True -> False" in capsys.readouterr().out
    assert run("check", "--instance", str(instance), "--local", str(LOCAL)) == 0


def test_check_notices_settings_changed_elsewhere(instance):
    assert run("prepare", "--instance", str(instance), "--local", str(LOCAL)) == 0
    config = instance / "config" / "bot_config.toml"
    edited = config.read_text(encoding="utf-8").replace(
        'nickname = "DoraYmon"', 'nickname = "改名"'
    )
    config.write_text(edited, encoding="utf-8")
    assert run("check", "--instance", str(instance), "--local", str(LOCAL)) == 1


def test_run_keeps_webui_edits_and_execs_maibot(instance, monkeypatch):
    assert run("prepare", "--instance", str(instance), "--local", str(LOCAL)) == 0
    config = instance / "config" / "bot_config.toml"
    edited = config.read_text(encoding="utf-8").replace(
        'nickname = "DoraYmon"', 'nickname = "改名"'
    )
    config.write_text(edited, encoding="utf-8")
    (instance / "plugins" / "doraymon_meal" / "plugin.py").unlink()
    calls = []
    monkeypatch.setattr(cli.os, "execv", lambda path, argv: calls.append(argv))
    assert run("run", "--instance", str(instance), "--local", str(LOCAL)) == 0
    assert 'nickname = "改名"' in config.read_text(encoding="utf-8")
    assert (instance / "plugins" / "doraymon_meal" / "plugin.py").is_file()
    [argv] = calls
    assert argv[1:] == ["run", "--directory", str(instance)]


def test_run_refuses_an_uninitialized_instance(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.os, "execv", lambda *a: pytest.fail("must not start MaiBot"))
    assert run("run", "--instance", str(tmp_path / "none"), "--local", str(LOCAL)) == 2


def test_endpoint_requires_the_adapter(instance, capsys):
    code = run(
        "prepare",
        "--instance",
        str(instance),
        "--local",
        str(LOCAL),
        "--onebot",
        "ws://127.0.0.1:3901",
    )
    assert code == 2
    assert "install-adapter" in capsys.readouterr().err


def test_missing_deploy_directory_is_reported(instance, tmp_path):
    assert run("check", "--instance", str(instance), "--deploy", str(tmp_path / "nothing")) == 2


def test_invalid_local_toml_is_reported_without_a_traceback(instance, tmp_path, capsys):
    (tmp_path / "bot_config.toml").write_text("broken = [", encoding="utf-8")
    assert run("check", "--instance", str(instance), "--local", str(tmp_path)) == 2
    assert "doraymon check 失败" in capsys.readouterr().err


def test_run_refreshes_endpoint_without_resetting_adapter_webui_edits(instance, monkeypatch):
    target = instance / "plugins" / ADAPTER_DIR
    target.mkdir(parents=True)
    (target / "_manifest.json").write_text(
        '{"version": "' + ADAPTER_VERSION + '"}', encoding="utf-8"
    )
    args = ["--instance", str(instance), "--local", str(LOCAL), "--onebot", "ws://127.0.0.1:3901"]
    assert run("prepare", *args) == 0
    config = target / "config.toml"
    edited = config.read_text(encoding="utf-8").replace(
        "enable_poke_tool = false", "enable_poke_tool = true"
    )
    config.write_text(edited, encoding="utf-8")
    monkeypatch.setattr(cli.os, "execv", lambda *args: None)
    args[-1] = "ws://127.0.0.1:3902"
    assert run("run", *args) == 0
    final = config.read_text(encoding="utf-8")
    assert "port = 3902" in final
    assert "enable_poke_tool = true" in final
