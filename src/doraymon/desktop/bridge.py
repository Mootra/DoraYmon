"""Small native bridge; no credentials or shell execution are exposed to page JavaScript."""

import base64
import ctypes
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def protect_token(value: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("桌面凭据仅可由所属 Windows 用户读取。")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    result = Blob()
    method = (
        ctypes.windll.crypt32.CryptUnprotectData
        if decrypt
        else ctypes.windll.crypt32.CryptProtectData
    )
    if not method(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise RuntimeError("桌面凭据无法由当前 Windows 用户解密。")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.windll.kernel32.LocalFree(result.data)


class DesktopBridge:
    def __init__(self, settings: dict[str, Any], *, token_loader=None, runner=None):
        self._settings = settings
        self._token_loader = token_loader or self._load_token
        self._runner = runner or subprocess.run
        self._operation_lock = threading.Lock()
        self._window = None

    def _load_token(self) -> str:
        value = Path(self._settings["token_file"]).read_bytes()
        return protect_token(base64.b64decode(value), decrypt=True).decode("utf-8")

    def _request(self, route: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        url = f"http://127.0.0.1:{int(self._settings.get('api_port', 8836))}{route}"
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": "Bearer " + self._token_loader(),
                "Content-Type": "application/json",
            },
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=25) as response:
            return json.loads(response.read(16 * 1024 * 1024))

    def get_status(self) -> dict:
        result: dict[str, Any] = {"qq": "unknown", "connection": "unknown", "backend": "unknown"}
        result["collection_control"] = bool(
            self._settings.get("collection_python") and self._settings.get("collection_folder")
        )
        try:
            location = Path(self._settings["runtime_root"]) / "runtime-health.json"
            health = json.loads(location.read_text(encoding="utf-8-sig"))
            stamp = datetime.fromisoformat(str(health["checkedAt"]).replace("Z", "+00:00"))
            age = (datetime.now(UTC) - stamp).total_seconds()
            if 0 <= age <= 180:
                result.update(
                    qq="online"
                    if health.get("online") and health.get("selfIdMatches")
                    else "offline",
                    connection="online" if health.get("oneBotAuthenticated") else "offline",
                    backend="online" if health.get("backendActive") else "offline",
                    phase=health.get("phase", "unknown"),
                    checked_at=health["checkedAt"],
                )
            else:
                result["phase"] = "stale"
        except (OSError, ValueError, KeyError, TypeError):
            result["phase"] = "unavailable"
        try:
            result["admin"] = self._request("/health")
        except (OSError, ValueError, KeyError, RuntimeError):
            result["admin"] = {"connected": False}
        return result

    def get_catalog(self) -> dict:
        try:
            value = self._request("/catalog")
            if self._settings.get("catalog_snapshot") and isinstance(value.get("shops"), list):
                try:
                    path = Path(self._settings["catalog_snapshot"])
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = path.with_suffix(".tmp")
                    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
                    temporary.replace(path)
                except OSError:
                    pass
            return {**value, "mode": "connected"}
        except (OSError, ValueError, KeyError, RuntimeError):
            for setting, mode in (("catalog_source", "local"), ("catalog_snapshot", "snapshot")):
                if not self._settings.get(setting):
                    continue
                try:
                    path = Path(self._settings[setting])
                    if path.stat().st_size > 16 * 1024 * 1024:
                        continue
                    value = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(value, dict) and isinstance(value.get("shops"), list):
                        return {**value, "mode": mode}
                except (OSError, ValueError):
                    continue
            return {"shops": [], "tasks": [], "coverage": {}, "mode": "unavailable"}

    def collection_action(self, action: str) -> dict:
        if action not in {"pause", "resume"}:
            return {"ok": False, "message": "未知采集操作。"}
        try:
            return self._request("/collection", {"action": action})
        except (OSError, ValueError, KeyError, RuntimeError):
            if not self._settings.get("collection_python") or not self._settings.get(
                "collection_folder"
            ):
                return {"ok": False, "message": "采集控制尚未配置，请检查桌面安装。"}
        try:
            result = self._runner(
                [
                    "wsl.exe",
                    "--distribution",
                    "Ubuntu",
                    "--exec",
                    self._settings["collection_python"],
                    "-m",
                    "doraymon.catalog_control",
                    "--folder",
                    self._settings["collection_folder"],
                    "--action",
                    action,
                ],
                capture_output=True,
                encoding="utf-8",
                timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode == 0:
                return json.loads(result.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        return {"ok": False, "message": "采集操作尚未确认，请刷新任务状态后重试。"}

    def get_history(self) -> dict:
        try:
            return self._request("/history")
        except (OSError, ValueError, KeyError, RuntimeError):
            return {"actions": [], "error": "管理连接不可用，检查机器人状态后重试。"}

    def get_reply_settings(self) -> dict:
        try:
            return self._request("/reply-settings")
        except (OSError, ValueError, KeyError, RuntimeError):
            return {"error": "回复设置暂时无法读取，请启动机器人后重试。"}

    def save_reply_settings(self, settings: dict) -> dict:
        if not isinstance(settings, dict):
            return {"ok": False, "message": "回复设置参数不合法。"}
        if not self._operation_lock.acquire(blocking=False):
            return {"ok": False, "message": "已有启动或恢复操作正在进行。"}
        saved = False
        try:
            result = self._request("/reply-settings", settings)
            if not result.get("ok"):
                return {"ok": False, "message": "回复设置未保存，请重试。"}
            saved = True
            result = self._runner(
                [
                    "wsl.exe",
                    "--distribution",
                    "Ubuntu",
                    "--exec",
                    "systemctl",
                    "--user",
                    "restart",
                    "doraymon-maibot-test.service",
                ],
                capture_output=True,
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode == 0:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    current = self.get_reply_settings()
                    group = next(
                        (
                            row
                            for row in current.get("groups", [])
                            if row.get("group_id") == settings.get("group_id")
                        ),
                        {},
                    )
                    if group.get("applied") and group.get("settings") == settings.get("settings"):
                        return {"ok": True, "applied": True, "message": "回复设置已生效。"}
                    time.sleep(0.5)
        except urllib.error.HTTPError as error:
            if error.code == 400:
                return {
                    "ok": False,
                    "message": "请检查参数范围，连续对话活跃度不能低于平时活跃度。",
                }
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            pass
        finally:
            self._operation_lock.release()
        return {
            "ok": False,
            "saved": saved,
            "message": "设置已保存，重新连接机器人后检查生效状态。"
            if saved
            else "设置未确认保存，请刷新后重试。",
        }

    def execute(self, operation: dict) -> dict:
        if not isinstance(operation, dict):
            return {"state": "failed", "message": "操作参数不合法。"}
        try:
            return self._request("/actions", operation)
        except urllib.error.HTTPError as error:
            if error.code in (400, 401, 403, 415):
                return {"state": "failed", "message": "目标、参数或桌面连接凭据未通过检查。"}
            return {"state": "uncertain", "message": "操作响应未确认，请先在 QQ 核对。"}
        except (OSError, ValueError, KeyError, RuntimeError):
            return {"state": "uncertain", "message": "操作响应未确认，请先在 QQ 核对。"}

    def runtime_action(self, operation: str) -> dict:
        actions = {"start": "Start", "reconnect": "Start", "stop": "Stop"}
        if operation not in actions:
            return {"ok": False, "message": "未知的机器人操作。"}
        if not self._operation_lock.acquire(blocking=False):
            return {"ok": False, "message": "已有启动或恢复操作正在进行。"}
        try:
            script = Path(self._settings["runtime_root"]) / "runtime.ps1"
            if not script.is_file():
                return {"ok": False, "message": "找不到机器人启动入口，请检查安装位置。"}
            result = self._runner(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script),
                    "-Action",
                    actions[operation],
                ],
                capture_output=True,
                timeout=75,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return {
                "ok": result.returncode == 0,
                "message": "操作已完成。"
                if result.returncode == 0
                else "操作未完成，请检查机器人 QQ 登录后重试。",
            }
        except (OSError, subprocess.TimeoutExpired):
            return {"ok": False, "message": "启动或恢复尚未确认，请刷新状态后检查 QQ 登录。"}
        finally:
            self._operation_lock.release()

    def open_qq(self) -> dict:
        paths = [
            Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Tencent/QQNT/QQ.exe",
            Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Tencent/QQ/QQ.exe",
        ]
        for path in paths:
            if path.is_file() and os.name == "nt":
                os.startfile(path)
                return {"ok": True}
        return {"ok": False, "message": "请从 Windows 开始菜单打开 QQ，登录机器人账号。"}

    def open_external(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(str(url))
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
        ):
            return False
        return bool(webbrowser.open(url))

    def open_settings(self, target: str) -> bool:
        urls = {"chat": "http://127.0.0.1:8003", "connection": "http://127.0.0.1:5099"}
        return self.open_external(urls[target]) if target in urls else False

    def export_catalog(self) -> dict:
        if self._window is None:
            return {"ok": False}
        import webview

        paths = self._window.create_file_dialog(webview.SAVE_DIALOG, save_filename="南昌门店.json")
        if not paths:
            return {"ok": False}
        value = self.get_catalog()
        value.pop("mode", None)
        Path(paths[0]).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "message": "门店资料已导出。"}
