"""Provision the owner's native app; credentials arrive on stdin and only DPAPI leaves it."""

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from doraymon.desktop.bridge import protect_token  # noqa: E402


def main():
    if os.name != "nt":
        raise SystemExit("Use the Windows build environment")
    request = json.load(sys.stdin)
    release = Path(request["release"])
    app_root = Path(os.environ["LOCALAPPDATA"]) / "DoraYmon"
    app_root.mkdir(parents=True, exist_ok=True)
    app = app_root / "app" / release.name
    if app.exists():
        raise SystemExit(
            "That release is already installed; preserve it and use a new release name"
        )
    shutil.copytree(release / "DoraYmon", app)
    backup = app_root / "backups" / release.name
    backup.mkdir(parents=True, exist_ok=True)
    for name in ("desktop.json", "desktop-token.dpapi"):
        path = app_root / name
        if path.exists():
            shutil.copy2(path, backup / name)
    token_path = app_root / "desktop-token.dpapi"
    token_path.write_bytes(base64.b64encode(protect_token(request["token"].encode())))
    settings = {
        "runtime_root": request["runtime_root"],
        "api_port": 8836,
        "token_file": str(token_path),
        "catalog_snapshot": str(app_root / "catalog.json"),
        "catalog_source": request["catalog_source"],
        "collection_folder": request["collection_folder"],
        "collection_python": request["collection_python"],
    }
    (app_root / "desktop.json").write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if request.get("catalog"):
        (app_root / "catalog.json").write_text(
            json.dumps(request["catalog"], ensure_ascii=False), encoding="utf-8"
        )
    shortcut_script = app_root / "install-shortcuts.ps1"
    shortcut_script.write_text(
        """param([string]$Executable)
$shell = New-Object -ComObject WScript.Shell
$targets = @((Join-Path ($shell.SpecialFolders('Desktop')) 'DoraYmon.lnk'),
             (Join-Path ($shell.SpecialFolders('Programs')) 'DoraYmon.lnk'))
foreach ($target in $targets) {
  $shortcut = $shell.CreateShortcut($target)
  $shortcut.TargetPath = $Executable
  $shortcut.WorkingDirectory = Split-Path $Executable
  $shortcut.IconLocation = $Executable + ',0'
  $shortcut.Description = 'DoraYmon'
  $shortcut.Save()
}
""",
        encoding="utf-8-sig",
    )
    executable = app / "DoraYmon.exe"
    subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(shortcut_script),
            "-Executable",
            str(executable),
        ],
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    shortcut_script.unlink()
    print(
        json.dumps(
            {
                "installed": str(executable),
                "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                "credential": "Windows DPAPI",
            }
        )
    )


if __name__ == "__main__":
    main()
