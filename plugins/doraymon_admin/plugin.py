"""MaiBot SDK command binding and lifecycle for the local desktop gateway."""

import asyncio
import json
from pathlib import Path
from typing import Any

from aiohttp import web
from cachetools import TTLCache
from maibot_sdk import (
    CONFIG_RELOAD_SCOPE_SELF,
    Command,
    Field,
    HookHandler,
    MaiBotPlugin,
    PluginConfigBase,
)

from doraymon.admin import HELP, AdminService, parse_command, sender_from_message
from doraymon.admin_http import create_admin_app
from doraymon.catalog_control import CollectionControl
from doraymon.reply_policy import ReplyPolicy, ReplySettingsStore

ADAPTER_API = "maibot-team.snowluma-adapter.adapter.napcat.action.call"


class AdminSettings(PluginConfigBase):
    config_version: str = Field(default="1.0.0", description="配置版本")
    enabled: bool = Field(default=False, description="启用管理员命令")
    owner_qq: str = Field(default="", description="唯一管理员的完整 QQ 号")
    desktop_token: str = Field(default="", description="本机桌面连接凭据")
    desktop_port: int = Field(default=8836, ge=1024, le=65535, description="本机桌面端口")
    catalog_json_path: str = Field(default="", description="共享门店资料 JSON 路径")
    journal_path: str = Field(default="", description="管理员操作记录路径")
    collection_folder: str = Field(default="", description="独立采集任务目录")
    reply_config_path: str = Field(default="", description="MaiBot 聊天配置路径")
    reply_overlay_path: str = Field(default="", description="本机聊天配置覆盖路径")


class AdminConfig(PluginConfigBase):
    plugin: AdminSettings = Field(default_factory=AdminSettings)


class DoraymonAdminPlugin(MaiBotPlugin):
    config_model = AdminConfig

    def __init__(self) -> None:
        super().__init__()
        self._service: AdminService | None = None
        self._runner: web.AppRunner | None = None
        self._reply_policy: ReplyPolicy | None = None
        self._bot_qq = ""
        self._group_sessions = TTLCache(maxsize=512, ttl=3600)

    async def on_load(self) -> None:
        await self._rebuild()

    async def on_unload(self) -> None:
        if self._runner:
            await self._runner.cleanup()
        self._runner = None
        self._service = None
        if self._reply_policy:
            await self._reply_policy.close()
        self._reply_policy = None
        self._group_sessions.clear()

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        del config_data, version
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            await self._rebuild()

    async def _rebuild(self) -> None:
        await self.on_unload()
        settings = self.config.plugin
        if not settings.enabled or not settings.owner_qq.strip():
            return
        if settings.reply_config_path and settings.reply_overlay_path:
            store = ReplySettingsStore(
                self.ctx.paths.data_dir / "reply-settings.json",
                Path(settings.reply_config_path),
                Path(settings.reply_overlay_path),
            )
            self._reply_policy = ReplyPolicy(store, self.ctx.config, self.ctx.frequency)
            self._bot_qq = str(await self.ctx.config.get("bot.qq_account") or "")
        self._service = AdminService(
            settings.owner_qq.strip(),
            self._call_action,
            journal_path=Path(settings.journal_path) if settings.journal_path else None,
        )
        if len(settings.desktop_token) >= 32:
            collection = (
                CollectionControl(Path(settings.collection_folder))
                if (settings.collection_folder)
                else None
            )
            app = create_admin_app(
                self._service, settings.desktop_token, self._catalog, collection, self._reply_policy
            )
            self._runner = web.AppRunner(app, access_log=None)
            await self._runner.setup()
            try:
                await web.TCPSite(self._runner, "127.0.0.1", settings.desktop_port).start()
            except BaseException:
                await self._runner.cleanup()
                self._runner = None
                raise

    @HookHandler("chat.receive.after_process", name="reply_group_settings")
    async def prepare_reply(self, message: dict, **kwargs):
        del kwargs
        if message.get("platform") != "qq":
            return {"action": "continue"}
        info = message.get("message_info") or {}
        group = info.get("group_info") or {}
        group_id = str(group.get("group_id") or "")
        if group_id:
            if session_id := message.get("session_id"):
                self._group_sessions[session_id] = group_id
        if self._reply_policy:
            additional = info.get("additional_config") or {}
            account = str(message.get("account_id") or self._bot_qq)
            at_bot = (
                message.get("is_at")
                or additional.get("at_bot")
                or any(
                    part.get("type") == "at"
                    and str(part.get("data", {}).get("target_user_id") or "") == account
                    and account
                    for part in message.get("raw_message", [])
                )
            )
            if at_bot:
                settings = self._reply_policy.settings.for_group(group_id)
                additional["is_mentioned"] = 1.0 if settings.at_reply else True
                info["additional_config"] = additional
                message["is_at"] = True
                return {"action": "continue", "kwargs": {"message": message}}
        return {"action": "continue"}

    @HookHandler("maisaka.replyer.before_request", name="reply_conversation_start")
    async def begin_reply(self, session_id: str, **kwargs):
        del kwargs
        if self._reply_policy and session_id in self._group_sessions:
            await self._reply_policy.activate(session_id, self._group_sessions[session_id])
        return {"action": "continue"}

    @HookHandler("send_service.after_send", name="reply_conversation_renew")
    async def after_reply(self, message: dict, sent: bool, **kwargs):
        del kwargs
        info = message.get("message_info") or {}
        if (
            self._reply_policy
            and sent
            and message.get("platform") == "qq"
            and info.get("group_info")
            and (session_id := message.get("session_id"))
        ):
            await self._reply_policy.activate(session_id, str(info["group_info"]["group_id"]))
        return {"action": "continue"}

    async def _call_action(self, action: str, params: dict) -> dict:
        async with asyncio.timeout(20):
            result = await self.ctx.api.call(
                ADAPTER_API, version="1", action_name=action, params=params
            )
        if not isinstance(result, dict):
            raise RuntimeError("QQ 接入返回了未知结果。")
        return result

    def _catalog(self) -> dict:
        location = self.config.plugin.catalog_json_path
        if not location:
            return {"shops": [], "coverage": {}, "tasks": []}
        try:
            path = Path(location)
            if path.stat().st_size > 16 * 1024 * 1024:
                raise ValueError("资料文件过大。")
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or not isinstance(value.get("shops"), list):
                raise ValueError("资料格式不合法。")
            return value
        except (OSError, ValueError):
            return {"shops": [], "coverage": {}, "tasks": [], "error": "门店资料暂时读不到。"}

    @Command(
        "qq_admin",
        description="管理员私聊、群内 @ 与好友申请处理",
        pattern=(
            r"^\s*(?:(?i:#sl)\s*$|/"
            r"(?:私聊|群消息|加好友|好友申请|好友列表|群列表|群成员|机器人状态|机器人帮助)(?:\s|$))"
        ),
        permission="operator",
    )
    async def command(self, **kwargs: Any):
        service = self._service
        sender = sender_from_message(kwargs.get("message"))
        if not service or not service.authorized(sender):
            return True, "", True
        elif kwargs.get("text", "").strip().lower() == "#sl":
            text = await self._status()
        elif kwargs.get("text", "").strip() == "/机器人状态":
            text = "管理员命令已连接。" + (
                " 桌面入口已启用。" if self._runner else " 桌面入口尚未配置。"
            )
        elif kwargs.get("text", "").strip() == "/机器人帮助":
            text = HELP
        else:
            try:
                request = parse_command(kwargs.get("text", ""))
                if request is None:
                    raise ValueError("请核对命令格式。\n" + HELP)
                result = await service.execute(sender, request)
                if "data" in result:
                    rows = result["data"]
                    text = (
                        "\n".join(
                            f"{row['id']}  {row['remark'] or row['name']}" for row in rows[:50]
                        )
                        or "没有返回列表记录。"
                    )
                    if len(rows) > 50:
                        text += f"\n共 {len(rows)} 条，完整列表可在桌面查看。"
                else:
                    text = result.get("message", "操作已记录。")
            except ValueError as error:
                text = str(error)
        stream_id = kwargs.get("stream_id")
        if stream_id:
            await self.ctx.send.text(text, stream_id)
        return True, text, True

    async def _status(self) -> str:
        try:
            version, status = await asyncio.gather(
                self._call_action("get_version_info", {}),
                self._call_action("get_status", {}),
            )
            if any(r.get("status") != "ok" or r.get("retcode") != 0 for r in (version, status)):
                return "QQ 状态暂时无法读取。"
            version_data, status_data = version["data"], status["data"]
            hours, remaining = divmod(int(status_data["time"]), 3600)
            minutes, seconds = divmod(remaining, 60)
            online = "在线" if status_data["online"] else "离线"
            health = "正常" if status_data["good"] else "异常"
            return (
                f"{version_data['app_name']} 状态\n版本：{version_data['app_version']}\n"
                f"账号：{online}\n收发链路：{health}\n"
                f"运行时长：{hours} 小时 {minutes} 分 {seconds} 秒"
            )
        except Exception:
            return "QQ 状态暂时无法读取。"


def create_plugin() -> DoraymonAdminPlugin:
    return DoraymonAdminPlugin()
