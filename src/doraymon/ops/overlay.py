"""Apply repository-owned TOML overlays to MaiBot instance files, keeping their comments.

An overlay may only touch keys that already exist in the target. A missing key means MaiBot
renamed or dropped a setting, so the run stops instead of silently writing a dead key.
"""

import os
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.container import Container
from tomlkit.items import AoT, InlineTable, Item, Table

SECRET_KEYS = frozenset({"token", "api_key", "amap_key", "access_token", "password", "ws_token"})


class OverlayError(Exception):
    pass


@dataclass(frozen=True)
class Change:
    path: str
    old: Any
    new: Any

    def render(self) -> str:
        if self.path.rsplit(".", 1)[-1] in SECRET_KEYS:
            return f"{self.path}: *** -> ***"
        return f"{self.path}: {_mask_secrets(self.old)!r} -> {_mask_secrets(self.new)!r}"


def _mask_secrets(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: "***" if key in SECRET_KEYS else _mask_secrets(item) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_mask_secrets(item) for item in value]
    return value


def load_overlay(path: Path) -> dict[str, Any]:
    with path.open("rb") as file:
        return tomllib.load(file)


def merge_overlays(*overlays: Mapping[str, Any]) -> dict[str, Any]:
    """Later overlays win; tables merge key by key, other values (including lists) replace."""
    merged: dict[str, Any] = {}
    for overlay in overlays:
        _merge_into(merged, overlay)
    return merged


def _merge_into(target: dict[str, Any], overlay: Mapping[str, Any]) -> None:
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            _merge_into(target[key], value)
        elif isinstance(value, Mapping):
            target[key] = {}
            _merge_into(target[key], value)
        else:
            target[key] = value


def apply_overlay(
    target: Path,
    overlay: Mapping[str, Any],
    *,
    create_missing: bool = False,
    dry_run: bool = False,
) -> list[Change]:
    if target.exists():
        document = tomlkit.parse(target.read_text(encoding="utf-8"))
    elif create_missing:
        document = tomlkit.document()
    else:
        raise OverlayError(f"目标配置不存在：{target}")
    changes: list[Change] = []
    _apply(document, overlay, "", create_missing, changes)
    if changes and not dry_run:
        write_text_atomic(target, tomlkit.dumps(document))
    return changes


def _apply(
    container: Container | Table,
    overlay: Mapping[str, Any],
    prefix: str,
    create_missing: bool,
    changes: list[Change],
) -> None:
    items = container if isinstance(container, Container) else container.value
    for key, value in overlay.items():
        path = f"{prefix}{key}"
        current = items.item(key) if key in items else None
        if current is None and not create_missing:
            raise OverlayError(f"MaiBot 配置中没有 {path}；先核对新版本的配置项再改覆盖文件")
        if isinstance(value, Mapping) and not isinstance(current, InlineTable):
            if current is None:
                current = tomlkit.table(is_super_table=True)
                container[key] = current
            elif not isinstance(current, Table):
                raise OverlayError(f"{path} 在 MaiBot 配置中不是表")
            _apply(current, value, f"{path}.", create_missing, changes)
            continue
        old = None if current is None else current.unwrap()
        if current is not None and not _same_kind(old, value):
            raise OverlayError(f"{path} 类型不符：MaiBot 为 {type(old).__name__}")
        new = float(value) if isinstance(old, float) and not isinstance(value, bool) else value
        if current is not None and old == new:
            continue
        item = tomlkit.item(new) if isinstance(current, AoT) else _to_item(new)
        if current is not None:
            item.trivia.comment = current.trivia.comment
            item.trivia.comment_ws = current.trivia.comment_ws
        container[key] = item
        changes.append(Change(path, old, new))


def _same_kind(old: Any, new: Any) -> bool:
    if isinstance(old, bool) or isinstance(new, bool):
        return isinstance(old, bool) and isinstance(new, bool)
    if isinstance(old, int | float):
        return isinstance(new, int | float)
    if isinstance(old, Mapping):
        return isinstance(new, Mapping)
    return type(old) is type(new)


def _to_item(value: Any) -> Item:
    # Lists of tables stay inline, matching how MaiBot writes learning_list and similar keys.
    if isinstance(value, list):
        array = tomlkit.array()
        for element in value:
            array.append(_to_item(element))
        return array
    if isinstance(value, Mapping):
        table = tomlkit.inline_table()
        for key, element in value.items():
            table[key] = _to_item(element)
        return table
    return tomlkit.item(value)


def write_text_atomic(path: Path, text: str) -> None:
    """Replace a file in one rename on the same filesystem, keeping its permission bits."""
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as file:
            file.write(text if text.endswith("\n") else text + "\n")
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
