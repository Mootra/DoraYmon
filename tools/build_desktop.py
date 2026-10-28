"""Build the native Windows desktop from the authoritative checkout."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("Use the Windows build environment.")
    root = Path(__file__).resolve().parents[1]
    cache = args.output / "build-cache"
    cache.mkdir(parents=True, exist_ok=True)
    from PIL import Image, ImageDraw

    icon = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(icon)
    draw.rounded_rectangle((10, 10, 246, 246), 60, fill="#3b5445")
    draw.line((87, 67, 87, 191), fill="#f4f2e7", width=23)
    draw.arc((63, 66, 196, 192), -90, 90, fill="#f4f2e7", width=23)
    icon_path = cache / "DoraYmon.ico"
    icon.save(icon_path, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onedir",
            "--noconsole",
            "--name",
            "DoraYmon",
            "--icon",
            str(icon_path),
            "--collect-all",
            "webview",
            "--collect-all",
            "clr_loader",
            "--collect-all",
            "pythonnet",
            "--paths",
            str(root / "src"),
            "--add-data",
            str(root / "src/doraymon/desktop/assets") + os.pathsep + "assets",
            "--distpath",
            str(args.output),
            "--workpath",
            str(cache / "work"),
            "--specpath",
            str(cache),
            str(root / "desktop/launcher.py"),
        ],
        check=True,
        cwd=root,
    )
    dependencies = subprocess.check_output([sys.executable, "-m", "pip", "freeze"], text=True)
    (args.output / "build-requirements.txt").write_text(dependencies, encoding="utf-8")
    executable = args.output / "DoraYmon/DoraYmon.exe"
    print(json.dumps({"executable": str(executable), "bytes": executable.stat().st_size}))


if __name__ == "__main__":
    main()
