"""doraymon: prepare, check and launch the MaiBot instance that runs DoraYmon."""

import argparse
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

from tomlkit.exceptions import ParseError

from .instance import (
    Instance,
    apply_configs,
    configure_adapter,
    initialize,
    install_adapter,
    sync_plugin,
)
from .napcat import NapcatConfigError, OneBotEndpoint, parse_endpoint, read_integration
from .overlay import Change, OverlayError, load_overlay, merge_overlays

REPO_ROOT = Path(__file__).resolve().parents[3]
OWN_PLUGINS = ("doraymon_meal", "doraymon_admin")
OVERLAY_FILES = ("bot_config", "model_config", "adapter_policy", "adapter")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="doraymon", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("check", "只比较，不写入；有差异时退出码为 1"),
        ("prepare", "初始化实例并写入配置覆盖项和插件"),
        ("run", "同步插件和 NapCat 地址后以 maibot run 替换当前进程（供 systemd 使用）"),
        ("install-adapter", "克隆锁定版本的统一 QQ 连接器"),
    ):
        command = commands.add_parser(name, help=text)
        command.add_argument(
            "--instance",
            type=Path,
            default=Path("~/.local/share/doraymon/maibot").expanduser(),
            help="MaiBot 实例目录（配置、数据、日志都在这里）",
        )
        command.add_argument(
            "--local",
            type=Path,
            default=Path("~/.config/doraymon").expanduser(),
            help="本机覆盖目录：群号、QQ 号等不进仓库的值，文件名与 deploy/maibot 相同",
        )
        command.add_argument("--deploy", type=Path, default=REPO_ROOT / "deploy" / "maibot")
        endpoint = command.add_mutually_exclusive_group()
        endpoint.add_argument("--napcat", type=Path, help="NapCat 运行目录里的 integration.json")
        endpoint.add_argument(
            "--onebot", help="直接指定 OneBot 地址，例如回放台 ws://127.0.0.1:3901"
        )
        command.add_argument("--onebot-token", default="", help="配合 --onebot 使用的本地 token")
    return parser


def load_overlays(deploy: Path, local: Path) -> dict[str, dict[str, Any]]:
    if not deploy.is_dir():
        raise OverlayError(f"找不到覆盖目录 {deploy}；需要在仓库检出中以可编辑方式安装后运行")
    overlays = {}
    for name in OVERLAY_FILES:
        layers = [
            path for path in (deploy / f"{name}.toml", local / f"{name}.toml") if path.is_file()
        ]
        overlays[name] = merge_overlays(*(load_overlay(path) for path in layers))
    return overlays


def resolve_endpoint(args: argparse.Namespace) -> OneBotEndpoint | None:
    if args.napcat is not None:
        return read_integration(args.napcat)
    if args.onebot:
        return parse_endpoint(args.onebot, args.onebot_token)
    return None


def prepare(
    args: argparse.Namespace, *, dry_run: bool, configs: bool = True
) -> tuple[list[Change], list[str]]:
    """``configs=False`` is the start-up path: it refreshes only what follows the environment
    (NapCat address, plugin code) so settings edited in the WebUI survive a restart."""
    instance = Instance(args.instance.expanduser().resolve())
    if not instance.initialized() and (dry_run or not configs):
        raise OverlayError(f"实例尚未初始化：{instance.root}；先运行 doraymon prepare")
    if not dry_run and initialize(instance, [str(maibot_executable())]):
        print(f"已初始化实例 {instance.root}")
    overlays = load_overlays(args.deploy, args.local)
    changes = []
    if configs:
        changes = apply_configs(
            instance,
            bot=overlays["bot_config"],
            model=overlays["model_config"],
            policy=overlays["adapter_policy"],
            dry_run=dry_run,
        )
    actions = []
    for name in OWN_PLUGINS:
        actions += sync_plugin(REPO_ROOT / "plugins" / name, instance.plugins, dry_run=dry_run)
    if (endpoint := resolve_endpoint(args)) is not None:
        settings = overlays["adapter"] if configs or not instance.adapter_config.exists() else {}
        changes += configure_adapter(instance, endpoint, settings, dry_run=dry_run)
    return changes, actions


def maibot_executable() -> Path:
    # 与当前解释器同一虚拟环境里的 maibot，保证版本就是 uv.lock 锁定的那个。
    return Path(sys.executable).with_name("maibot")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "install-adapter":
            instance = Instance(args.instance.expanduser().resolve())
            print(
                "已安装统一 QQ 连接器"
                if install_adapter(instance)
                else "统一 QQ 连接器已是锁定版本"
            )
            return 0
        changes, actions = prepare(
            args, dry_run=args.command == "check", configs=args.command != "run"
        )
    except (
        OverlayError,
        NapcatConfigError,
        subprocess.CalledProcessError,
        OSError,
        tomllib.TOMLDecodeError,
        ParseError,
    ) as error:
        print(f"doraymon {args.command} 失败：{error}", file=sys.stderr)
        return 2
    for line in [change.render() for change in changes] + actions:
        print(line)
    if args.command == "check":
        return 1 if changes or actions else 0
    if args.command == "run":
        executable = str(maibot_executable())
        os.execv(executable, [executable, "run", "--directory", str(args.instance.expanduser())])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
