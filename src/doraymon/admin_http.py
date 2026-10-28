"""Loopback desktop gateway; bearer possession identifies the configured local owner."""

import secrets
from collections.abc import Callable
from typing import Any

from aiohttp import web
from pydantic import ValidationError

from .admin import AdminRequest, AdminService
from .catalog_control import CollectionControl
from .reply_policy import ReplyPolicy


def create_admin_app(
    service: AdminService,
    token: str,
    catalog: Callable[[], dict[str, Any]],
    collection: CollectionControl | None = None,
    reply_policy: ReplyPolicy | None = None,
) -> web.Application:
    if len(token) < 32:
        raise ValueError("桌面连接凭据尚未配置。")

    @web.middleware
    async def authenticate(request, handler):
        supplied = request.headers.get("Authorization", "")
        if not secrets.compare_digest(supplied, "Bearer " + token):
            return web.json_response({"error": "桌面连接未认证。"}, status=401)
        if request.headers.get("Origin"):
            return web.json_response({"error": "请通过桌面程序操作。"}, status=403)
        try:
            return await handler(request)
        except PermissionError:
            return web.json_response({"error": "管理员身份未确认。"}, status=403)
        except (ValueError, ValidationError):
            return web.json_response({"error": "操作参数不合法，请检查目标和内容。"}, status=400)

    app = web.Application(middlewares=[authenticate], client_max_size=32768)

    async def health(request):
        del request
        return web.json_response(
            {
                "connected": True,
                "owner": service.owner,
                "capabilities": service.capabilities,
                "collection_control": collection is not None,
                "reply_settings": reply_policy is not None,
            }
        )

    async def execute(request):
        if request.content_type != "application/json":
            return web.json_response({"error": "需要 JSON 操作参数。"}, status=415)
        operation = AdminRequest.model_validate(await request.json())
        result = await service.execute(service.owner, operation)
        return web.json_response(result)

    async def history(request):
        del request
        return web.json_response({"actions": service.history()})

    async def read_catalog(request):
        del request
        return web.json_response(catalog())

    async def control_collection(request):
        if not collection:
            return web.json_response({"error": "采集控制尚未配置。"}, status=503)
        data = await request.json()
        if not isinstance(data, dict) or set(data) != {"action"}:
            raise ValueError("Unknown collection arguments")
        return web.json_response(await collection.execute(data["action"]))

    async def read_reply_settings(request):
        del request
        if reply_policy is None:
            return web.json_response({"error": "回复设置尚未配置。"}, status=503)
        return web.json_response(await reply_policy.read())

    async def save_reply_settings(request):
        if reply_policy is None:
            return web.json_response({"error": "回复设置尚未配置。"}, status=503)
        if request.content_type != "application/json":
            return web.json_response({"error": "需要 JSON 设置参数。"}, status=415)
        return web.json_response(await reply_policy.save(await request.json()))

    app.add_routes(
        [
            web.get("/health", health),
            web.post("/actions", execute),
            web.get("/history", history),
            web.get("/catalog", read_catalog),
            web.post("/collection", control_collection),
            web.get("/reply-settings", read_reply_settings),
            web.post("/reply-settings", save_reply_settings),
        ]
    )
    return app
