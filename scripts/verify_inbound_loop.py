"""Prove the inbound path end to end, without touching QQ.

Starts the real account entry as a subprocess, points it at a local OneBot server
this script runs, pushes one event shaped exactly like NapCat's, and reports the
outbound action the bot produces. The event source is synthetic; the bot, its
transport, the adapter, the access policy, the router and the reply path are all the
real ones.

This exists because the live account still depends on a person sending a message,
and a channel that can only be checked by hand is a channel that gets checked
rarely. ``/ping`` is used because it needs no model key, so what is observed is the
pipeline and nothing else.

Usage:
    .venv/bin/python -m scripts.verify_inbound_loop
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from aiohttp import web

DEFAULT_TOKEN = "inbound-loop-token"
DEFAULT_SELF_ID = "3963059558"
DEFAULT_USER_ID = "100000001"


async def run(port: int, token: str, self_id: str, user_id: str, command: str) -> int:
    received: list[dict] = []
    pushed = False

    async def handler(request: web.Request) -> web.WebSocketResponse:
        nonlocal pushed
        if token and request.query.get("access_token") != token:
            return web.Response(status=401)
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        print("机器人已连接到自检服务端", flush=True)

        async def push() -> None:
            nonlocal pushed
            await asyncio.sleep(1.5)
            event = {
                "post_type": "message",
                "message_type": "private",
                "sub_type": "friend",
                "message_id": 910001,
                "user_id": int(user_id),
                "self_id": int(self_id),
                "time": int(time.time()),
                "raw_message": command,
                "message": [{"type": "text", "data": {"text": command}}],
                "sender": {"user_id": int(user_id), "nickname": "自检"},
            }
            await socket.send_str(json.dumps(event, ensure_ascii=False))
            pushed = True
            print(f"已推送入站事件：{command!r} 来自 {user_id}", flush=True)

        asyncio.ensure_future(push())
        async for message in socket:
            if message.type is not web.WSMsgType.TEXT:
                continue
            payload = json.loads(message.data)
            received.append(payload)
            print(
                "机器人发出 "
                f"action={payload.get('action')} "
                f"params={json.dumps(payload.get('params', {}), ensure_ascii=False)[:200]}",
                flush=True,
            )
            await socket.send_str(
                json.dumps(
                    {"status": "ok", "retcode": 0, "data": {"message_id": 1},
                     "echo": payload.get("echo")}
                )
            )
        return socket

    app = web.Application()
    app.router.add_get("/", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    print(f"自检服务端监听 127.0.0.1:{port}", flush=True)

    environment = dict(os.environ)
    environment.update(
        {
            "BOT_QQ_ACCOUNT_ENABLED": "true",
            "BOT_QQ_ACCOUNT_SELF_ID": self_id,
            "BOT_QQ_ACCOUNT_WS_URL": f"ws://127.0.0.1:{port}",
            "BOT_QQ_ACCOUNT_TOKEN": token,
            "BOT_QQ_ACCOUNT_FRIENDS": user_id,
            "BOT_QQ_ACCOUNT_GROUPS": "",
            "DATA_DIR": "data/inbound_loop_run",
            "LOG_DIR": "logs/inbound_loop_run",
        }
    )
    process = await asyncio.create_subprocess_exec(
        str(PROJECT_ROOT / ".venv" / "bin" / "python"),
        "-u",
        "-m",
        "scripts.run_account",
        cwd=str(PROJECT_ROOT),
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    async def drain() -> None:
        assert process.stdout is not None
        while True:
            line = await process.stdout.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()
            if any(token_ in text for token_ in ("连接", "入口启动", "跳过", "失败", "ERROR")):
                print(f"[bot] {text}", flush=True)

    asyncio.ensure_future(drain())

    deadline = time.time() + 25
    while time.time() < deadline and not any(item.get("action") for item in received):
        await asyncio.sleep(0.5)

    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=8)
    except asyncio.TimeoutError:
        process.kill()
    await runner.cleanup()

    actions = [item for item in received if item.get("action")]
    print()
    print("=" * 56)
    if actions:
        print(f"结果：入站事件触发了 {len(actions)} 个出站 action")
        for action in actions:
            print(
                f"  {action['action']} "
                f"{json.dumps(action.get('params', {}), ensure_ascii=False)}"
            )
    else:
        print("结果：入站事件没有触发任何 action")
    print(f"事件已推送：{pushed}")
    print()
    print("这证明的是：入站 socket → 适配 → 访问策略 → 路由 → 出站 action 这一段闭环。")
    print("它不证明 NapCat 会把你发的消息推给机器人，那需要真实账号的一次真实消息。")
    return 0 if actions else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="验证入站到出站的完整回路（不碰 QQ）")
    parser.add_argument("--port", type=int, default=3055)
    parser.add_argument("--token", default=DEFAULT_TOKEN)
    parser.add_argument("--self-id", default=DEFAULT_SELF_ID)
    parser.add_argument("--user-id", default=DEFAULT_USER_ID)
    parser.add_argument("--command", default="/ping", help="推送的消息内容，默认 /ping")
    args = parser.parse_args()
    return asyncio.run(
        run(args.port, args.token, args.self_id, args.user_id, args.command)
    )


if __name__ == "__main__":
    raise SystemExit(main())
