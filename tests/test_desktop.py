import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from doraymon.desktop.bridge import DesktopBridge


def test_expired_health_does_not_claim_online(tmp_path):
    (tmp_path / "runtime-health.json").write_text(
        json.dumps(
            {
                "checkedAt": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
                "online": True,
                "oneBotAuthenticated": True,
                "backendActive": True,
            }
        )
    )
    bridge = DesktopBridge({"runtime_root": str(tmp_path)}, token_loader=lambda: "")
    bridge._request = lambda route: {"connected": True}
    result = bridge.get_status()
    assert result["qq"] == result["backend"] == "unknown"
    assert result["phase"] == "stale"


def test_runtime_only_accepts_known_operations_and_uses_existing_entry(tmp_path):
    (tmp_path / "runtime.ps1").write_text("synthetic runtime")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    bridge = DesktopBridge({"runtime_root": str(tmp_path)}, runner=run)
    assert not bridge.runtime_action("arbitrary command")["ok"]
    assert not calls
    assert bridge.runtime_action("start")["ok"]
    assert calls[0][0][-2:] == ["-Action", "Start"]
    assert "capture_output" in calls[0][1]


def test_saving_reply_settings_restarts_backend_and_confirms_application():
    from doraymon.reply_policy import ReplySettings

    settings = {"group_id": "40001", "settings": ReplySettings().model_dump()}
    calls = []

    def request(route, payload=None):
        calls.append((route, payload))
        return {"ok": True} if payload is not None else {"groups": [{**settings, "applied": True}]}

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0)

    bridge = DesktopBridge({}, runner=run)
    bridge._request = request
    assert bridge.save_reply_settings(settings)["applied"]
    assert calls[0] == ("/reply-settings", settings)
    assert calls[1][-4:] == ["systemctl", "--user", "restart", "doraymon-maibot-test.service"]
    assert calls[2] == ("/reply-settings", None)


def test_reply_save_distinguishes_rejected_settings_from_unconfirmed_restart():
    import urllib.error

    from doraymon.reply_policy import ReplySettings

    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1)

    bridge = DesktopBridge({}, runner=run)

    def reject(*args):
        raise urllib.error.HTTPError("http://127.0.0.1", 400, "invalid", {}, None)

    bridge._request = reject
    assert not bridge.save_reply_settings(ReplySettings().model_dump())["ok"]
    assert not calls
    bridge._request = lambda *args: {"ok": True}
    result = bridge.save_reply_settings(ReplySettings().model_dump())
    assert result["saved"] and not result["ok"]


def test_catalog_fallback_is_marked_snapshot_and_contains_no_credentials(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"shops": [{"id": "synthetic", "display_name": "测试门店"}]}))
    bridge = DesktopBridge({"catalog_snapshot": str(path)})
    bridge._request = lambda route: (_ for _ in ()).throw(OSError())
    result = bridge.get_catalog()
    assert result["mode"] == "snapshot"
    assert result["shops"][0]["id"] == "synthetic"
    assert "token" not in result


def test_catalog_reads_live_source_while_qq_is_offline(tmp_path):
    source = tmp_path / "live.json"
    snapshot = tmp_path / "snapshot.json"
    source.write_text(json.dumps({"shops": [{"id": "latest"}]}))
    snapshot.write_text(json.dumps({"shops": [{"id": "old"}]}))
    bridge = DesktopBridge({"catalog_source": str(source), "catalog_snapshot": str(snapshot)})
    bridge._request = lambda route: (_ for _ in ()).throw(OSError())
    assert bridge.get_catalog() == {"shops": [{"id": "latest"}], "mode": "local"}
    source.write_text('{"shops": "incomplete export"}')
    assert bridge.get_catalog()["mode"] == "snapshot"
    assert bridge.get_catalog()["shops"] == [{"id": "old"}]


def test_collection_control_runs_without_qq_and_rejects_unknown_actions():
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"ok": true, "message": "paused"}')

    bridge = DesktopBridge(
        {
            "collection_python": "/synthetic/.venv/bin/python",
            "collection_folder": "/synthetic/data",
        },
        runner=run,
    )
    bridge._request = lambda *args: (_ for _ in ()).throw(OSError())
    assert not bridge.collection_action("arbitrary command")["ok"]
    assert not calls
    assert bridge.collection_action("pause")["ok"]
    assert calls[0][0] == [
        "wsl.exe",
        "--distribution",
        "Ubuntu",
        "--exec",
        "/synthetic/.venv/bin/python",
        "-m",
        "doraymon.catalog_control",
        "--folder",
        "/synthetic/data",
        "--action",
        "pause",
    ]
    assert calls[0][1]["encoding"] == "utf-8"
