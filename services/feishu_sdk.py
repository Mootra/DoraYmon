"""The Feishu long-connection SDK seam: the one piece that is not wired yet.

Everything else in this channel is verified offline — the contract, the adapter,
the sending half, the transport, the entry. Opening the long connection is the
single part that needs the official SDK, and that SDK is deliberately not
installed yet: adding a runtime dependency to a project that also deploys to a
Linux server is a decision, not a detail.

So this module refuses to pretend. It raises an actionable error instead of
returning a source that would connect nowhere, because a bot that starts, logs
"started" and then silently never answers is worse than one that does not start.

What still has to be written here, once ``lark-oapi`` is installed and its real
API has been read rather than assumed:

1. build the WebSocket client from the app credentials;
2. register a handler for ``im.message.receive_v1`` that decodes the event into a
   plain ``dict`` and calls ``on_payload`` **without awaiting anything** — that
   call is the three-second acknowledgement;
3. call ``on_ready`` when the SDK reports the connection is actually up. The
   session uses it to tell a first connect from a reconnect, so it must not be
   guessed at, and the SDK's readiness hook has to be confirmed;
4. block until the connection closes, then return.
"""

from __future__ import annotations

from typing import Any


class FeishuSdkUnavailable(Exception):
    """The long connection cannot be opened yet."""


def build_default_event_source(settings: Any) -> Any:
    """Return the SDK-backed event source, or explain why it cannot exist yet."""
    app_id = str(getattr(settings, "feishu_app_id", "") or "").strip()
    raise FeishuSdkUnavailable(
        f"飞书长连接的 SDK 装配尚未完成（应用 {app_id or '(未配置 App ID)'}）："
        "请先安装 lark-oapi，并核对事件回调与就绪通知的真实 API——"
        "步骤见 services/feishu_sdk.py 顶部说明。在此之前请保持 "
        "BOT_FEISHU_ENABLED=false；收发的其余部分已可用并在离线测试覆盖内。"
    )
