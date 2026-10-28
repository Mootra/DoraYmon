"""Provision the native panel and its SDK plugin, preserving unrelated runtime settings."""

import argparse
import hashlib
import json
import secrets
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import tomlkit

from doraymon.ops.instance import sync_plugin

REPO = Path(__file__).resolve().parents[1]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", type=Path, required=True)
    parser.add_argument("--catalog-folder", type=Path, required=True)
    parser.add_argument("--windows-python", type=Path, required=True)
    parser.add_argument("--release", required=True, help="Windows release directory")
    parser.add_argument(
        "--runtime-root", required=True, help="Existing Windows supervisor directory"
    )
    parser.add_argument("--owner-qq", required=True)
    parser.add_argument("--local", type=Path, default=Path.home() / ".config/doraymon")
    args = parser.parse_args()
    import re

    if not re.fullmatch(r"[1-9][0-9]{4,11}", args.owner_qq):
        parser.error("An exact administrator QQ number is required")
    meal_path = args.instance / "plugins/doraymon_meal/config.toml"
    if not meal_path.is_file() or not (args.catalog_folder / "city-candidates.sqlite3").is_file():
        raise SystemExit("Existing meal plugin and verified city catalog are required")
    retained = [
        args.instance / "config" / name
        for name in ("bot_config.toml", "model_config.toml", "adapter_policy.toml")
    ]
    before = {str(path): sha(path) for path in retained}
    stamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
    backup = Path.home() / ".local/state/doraymon" / ("desktop-install-" + stamp)
    backup.mkdir(parents=True, mode=0o700)
    shutil.copy2(meal_path, backup / "meal-config.toml")
    (backup / "meal-config.toml").chmod(0o600)
    admin_path = args.instance / "plugins/doraymon_admin/config.toml"
    admin = tomlkit.parse(admin_path.read_text()) if admin_path.exists() else tomlkit.document()
    if admin_path.exists():
        shutil.copy2(admin_path, backup / "admin-config.toml")
        (backup / "admin-config.toml").chmod(0o600)
    settings = admin.setdefault("plugin", tomlkit.table())
    token = str(settings.get("desktop_token") or "")
    if len(token) < 32:
        token = secrets.token_urlsafe(48)
    settings.update(
        config_version="1.0.0",
        enabled=True,
        owner_qq=args.owner_qq,
        desktop_token=token,
        desktop_port=8836,
        catalog_json_path=str(args.catalog_folder / "catalog.json"),
        collection_folder=str(args.catalog_folder),
        journal_path=str(args.instance / "data/doraymon-admin.sqlite3"),
        reply_config_path=str(args.instance / "config/bot_config.toml"),
        reply_overlay_path=str(args.local / "bot_config.toml"),
    )
    provision = REPO / "tools/provision_desktop.py"
    unc_script = "//wsl.localhost/Ubuntu" + str(provision)
    request = {
        "token": token,
        "release": args.release,
        "runtime_root": args.runtime_root,
        "catalog": json.loads((args.catalog_folder / "catalog.json").read_text()),
        "catalog_source": "//wsl.localhost/Ubuntu" + str(args.catalog_folder / "catalog.json"),
        "collection_folder": str(args.catalog_folder),
        "collection_python": str(REPO / ".venv/bin/python"),
    }
    result = subprocess.run(
        [str(args.windows_python), unc_script],
        input=json.dumps(request, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=True,
        timeout=120,
    )
    installed = json.loads(result.stdout)
    admin_path.parent.mkdir(parents=True, exist_ok=True)
    admin_path.write_text(tomlkit.dumps(admin), encoding="utf-8")
    admin_path.chmod(0o600)
    meal = tomlkit.parse(meal_path.read_text())
    meal["plugin"]["candidate_database_path"] = str(args.catalog_folder / "city-candidates.sqlite3")
    meal_path.write_text(tomlkit.dumps(meal), encoding="utf-8")
    meal_path.chmod(0o600)
    for plugin in ("doraymon_meal", "doraymon_admin"):
        sync_plugin(REPO / "plugins" / plugin, args.instance / "plugins")
    assert before == {str(path): sha(path) for path in retained}
    (backup / "preserved-hashes.json").write_text(json.dumps(before, indent=2))
    print(
        json.dumps(
            {
                "desktop": installed,
                "plugin": "installed",
                "restart_required": True,
                "backup": str(backup),
            }
        )
    )


if __name__ == "__main__":
    main()
