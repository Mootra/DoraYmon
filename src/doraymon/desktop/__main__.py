"""Launch the Windows desktop window and its tray icon."""

import argparse
import json
import os
import sys
from pathlib import Path

from .bridge import DesktopBridge


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    parser.add_argument("--debug-port", type=int, default=0)
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--background", action="store_true")
    args = parser.parse_args()
    config = args.config or Path(os.environ.get("LOCALAPPDATA", ".")) / "DoraYmon/desktop.json"
    try:
        settings = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        if args.health:
            print(json.dumps({"error": "桌面配置尚未安装。"}, ensure_ascii=False))
        elif os.name == "nt":
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None, "桌面配置尚未安装，请重新安装 DoraYmon。", "DoraYmon", 0x10
            )
        return 1
    bridge = DesktopBridge(settings)
    if args.health:
        print(json.dumps(bridge.get_status(), ensure_ascii=False))
        return 0
    if args.debug_port:
        if not 1024 <= args.debug_port <= 65535:
            return 1
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
            f"--remote-debugging-port={args.debug_port}"
        )
    import pystray
    import webview
    from PIL import Image, ImageDraw

    from .windows import DesktopInstance

    instance = DesktopInstance(str(config.resolve()))
    if instance.existing:
        instance.show_existing()
        instance.close()
        return 0

    root = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    page = root / "assets/index.html"
    window = webview.create_window(
        "DoraYmon",
        str(page),
        js_api=bridge,
        width=1320,
        height=860,
        min_size=(960, 640),
        background_color="#f1eee9",
        text_select=True,
        hidden=args.background,
    )
    bridge._window = window
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((3, 3, 61, 61), 16, fill="#3b5445")
    draw.line((21, 17, 21, 47), fill="#f4efe7", width=6)
    draw.arc((15, 16, 48, 48), -90, 90, fill="#f4efe7", width=6)
    closing = False

    def show(icon=None, item=None):
        del icon, item
        window.show()
        window.restore()

    def quit_panel(icon, item):
        nonlocal closing
        del item
        closing = True
        icon.stop()
        window.destroy()

    def on_close():
        if not closing:
            window.hide()
            return False
        return True

    tray = pystray.Icon(
        "DoraYmon",
        image,
        "DoraYmon",
        menu=pystray.Menu(
            pystray.MenuItem("显示面板", show, default=True),
            pystray.MenuItem("启动机器人", lambda icon, item: bridge.runtime_action("start")),
            pystray.MenuItem("退出面板", quit_panel),
        ),
    )
    window.events.closing += on_close
    tray.run_detached()
    try:
        webview.start(gui="edgechromium")
    finally:
        tray.stop()
        instance.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
