"""Prepare a MaiBot instance directory from the repository: config overlays, plugins, adapter."""

import json
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .napcat import OneBotEndpoint
from .overlay import Change, OverlayError, apply_overlay

ADAPTER_DIR = "MaiBot-SnowLuma-Adapter"
ADAPTER_REPOSITORY = "https://github.com/Mai-with-u/MaiBot-SnowLuma-Adapter.git"
# MaiBot 1.3.15 候选版本使用的官方连接器；升级流程见 docs/runbook.md。
ADAPTER_VERSION = "1.1.8"
SKIPPED_PLUGIN_FILES = frozenset({"config.toml", "__pycache__"})


@dataclass(frozen=True)
class Instance:
    root: Path

    @property
    def bot_config(self) -> Path:
        return self.root / "config" / "bot_config.toml"

    @property
    def model_config(self) -> Path:
        return self.root / "config" / "model_config.toml"

    @property
    def adapter_policy(self) -> Path:
        return self.root / "config" / "adapter_policy.toml"

    @property
    def plugins(self) -> Path:
        return self.root / "plugins"

    @property
    def adapter_config(self) -> Path:
        return self.plugins / ADAPTER_DIR / "config.toml"

    def initialized(self) -> bool:
        return self.bot_config.is_file() and self.model_config.is_file()


def initialize(instance: Instance, maibot: Sequence[str]) -> bool:
    """Run ``maibot init`` once; it only creates missing files and never resets existing ones."""
    if instance.initialized():
        return False
    subprocess.run([*maibot, "init", "--directory", str(instance.root)], check=True)
    if not instance.initialized():
        raise OverlayError(f"maibot init 没有生成配置文件：{instance.root}")
    return True


def apply_configs(
    instance: Instance,
    *,
    bot: Mapping[str, Any],
    model: Mapping[str, Any],
    policy: Mapping[str, Any],
    dry_run: bool = False,
) -> list[Change]:
    changes = apply_overlay(instance.bot_config, bot, dry_run=dry_run)
    changes += apply_overlay(instance.model_config, model, dry_run=dry_run)
    # MaiBot 不生成 adapter_policy.toml，文件缺失等于全部放行，所以这里允许创建。
    changes += apply_overlay(instance.adapter_policy, policy, create_missing=True, dry_run=dry_run)
    return changes


def sync_plugin(source: Path, plugins_dir: Path, *, dry_run: bool = False) -> list[str]:
    """Mirror one plugin directory, keeping the instance's own ``config.toml`` (it holds keys).

    Copying instead of linking matters: MaiBot resolves plugin paths, so a symlink would make the
    Runner write the generated config, secrets included, into the repository checkout.
    """
    target = plugins_dir / source.name
    if not source.is_dir():
        raise OverlayError(f"插件源码目录不存在：{source}")
    if target.is_symlink():
        raise OverlayError(f"插件目录仍是符号链接：{target}；请先迁出实例配置并移除链接")
    wanted = {
        path.relative_to(source)
        for path in source.rglob("*")
        if path.is_file() and not SKIPPED_PLUGIN_FILES.intersection(path.relative_to(source).parts)
    }
    actions: list[str] = []
    existing = set()
    if target.exists():
        existing = {
            path.relative_to(target)
            for path in target.rglob("*")
            if path.is_file()
            and not SKIPPED_PLUGIN_FILES.intersection(path.relative_to(target).parts)
        }
    for relative in sorted(existing - wanted):
        actions.append(f"删除 {source.name}/{relative}")
        if not dry_run:
            (target / relative).unlink()
    for relative in sorted(wanted):
        origin, destination = source / relative, target / relative
        if destination.is_file() and destination.read_bytes() == origin.read_bytes():
            continue
        actions.append(f"写入 {source.name}/{relative}")
        if not dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, destination)
    return actions


def adapter_version(instance: Instance) -> str | None:
    manifest = instance.plugins / ADAPTER_DIR / "_manifest.json"
    if not manifest.is_file():
        return None
    version = json.loads(manifest.read_text(encoding="utf-8")).get("version")
    return version if isinstance(version, str) else None


def install_adapter(instance: Instance, *, git: str = "git") -> bool:
    """Clone the pinned adapter release; an existing copy must already be that version."""
    installed = adapter_version(instance)
    if installed == ADAPTER_VERSION:
        return False
    if installed is not None:
        raise OverlayError(
            f"已安装的统一 QQ 连接器是 {installed}，期望 {ADAPTER_VERSION}；"
            "按 runbook 的升级步骤处理"
        )
    target = instance.plugins / ADAPTER_DIR
    if target.exists():
        raise OverlayError(f"{target} 已存在但没有 _manifest.json，请先人工确认其内容")
    instance.plugins.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            git,
            "clone",
            "--depth",
            "1",
            "--branch",
            f"v{ADAPTER_VERSION}",
            ADAPTER_REPOSITORY,
            str(target),
        ],
        check=True,
    )
    return True


def configure_adapter(
    instance: Instance,
    endpoint: OneBotEndpoint,
    settings: Mapping[str, Any],
    *,
    dry_run: bool = False,
) -> list[Change]:
    """Point the adapter at NapCat. Before its first load the adapter has no config file yet;
    the Runner fills in every other field from the plugin's own defaults. Once the file exists,
    unknown keys are rejected like any other overlay."""
    installed = adapter_version(instance)
    if installed is None:
        raise OverlayError("统一 QQ 连接器尚未安装；先运行 doraymon install-adapter")
    if installed != ADAPTER_VERSION:
        raise OverlayError(f"统一 QQ 连接器版本为 {installed}，需要 {ADAPTER_VERSION}")
    overlay = {
        **settings,
        "client": {
            **settings.get("client", {}),
            "server": endpoint.host,
            "port": endpoint.port,
            "token": endpoint.token,
        },
    }
    return apply_overlay(
        instance.adapter_config,
        overlay,
        create_missing=not instance.adapter_config.exists(),
        dry_run=dry_run,
    )
