"""Verify the account channel against a real OneBot client (NapCat).

This is the pre-flight check for a live QQ account. It listens as an OneBot v11
WebSocket server, you point NapCat's reverse-WS at it, and it reports exactly what
happens: whether the client connected, what it sent, and what this bot would reply
if it were the one running.

It is deliberately NOT the bot. Running the bot against a real account is the
operator's decision; this tool only proves the channel, the identity rules, the
access policy and the reply path behave against a genuine OneBot peer. Nothing is
sent to QQ that the bot would not send, and no reply leaves until you send a test
event yourself.

Usage:
    .venv/bin/python -m scripts.verify_account_channel --port 3001
    # then point NapCat reverse-WS at ws://<host>:3001
    # press Enter to send a test private message, or type 'q' to quit
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Default to listening beyond loopback: NapCat frequently runs on the Windows
# side or in another WSL layer, where 127.0.0.1 means the wrong machine.
DEFAULT_HOST = "0.0.0.0"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from doraymon.config import load_settings
from doraymon.account_entry import build_policy
from services.account_session import AccountSession
from services.account_throttle import ConversationThrottle
from services.onebot_transport import OneBotWebSocketTransport
from services.reply_budget import REPLY_BUDGET
from services.session_revisions import SESSION_REVISIONS
from tests.onebot_server import (
    OneBotTestServer,
    group_message_event,
    private_message_event,
)


def _build_events(session: AccountSession) -> list[tuple[str, dict]]:
    settings = session.settings
    self_id = int(str(getattr(settings, "qq_account_self_id", "0") or "0") or 0)
    group_text = str(getattr(settings, "qq_account_groups", "") or "").strip()
    friend_text = str(getattr(settings, "qq_account_friends", "") or "").strip()
    group_id = int(group_text.split(",")[0]) if group_text else 30003
    user_id = int(friend_text.split(",")[0]) if friend_text else 10001
    return [
        ("私聊消息", private_message_event(user_id=user_id, self_id=self_id, message="通道自检")),
        ("群里 @ 机器人", group_message_event(group_id=group_id, self_id=self_id, message="通道自检")),
    ]


async def run(port: int, token: str, host: str = DEFAULT_HOST) -> int:
    # Listen first, then report configuration problems. An operator pointing
    # NapCat at this port needs to know the port is live before anything else;
    # refusing to listen because .env is incomplete hides that.
    server = OneBotTestServer(host=host, port=port, token=token)
    await server.start()
    print(f"正在监听 {server.url}")
    if host not in ("127.0.0.1", "localhost"):
        print(
            f"（绑定了 {host}，同网络的其他机器也能连进来。"
            "自检只在需要时开，跑完就关；其他机器可达的端口不要久留。）"
        )
    # The address depends on where NapCat runs, and guessing wrong costs the
    # operator an hour. Both candidates are printed, with which one applies.
    print(f"  NapCat 在 Windows（本机 WSL）：ws://127.0.0.1:{port}")
    print("    —— WSL2 的 localhost 转发让 Windows 能直接连到这个地址，实测可用。")
    for address in _reachable_addresses(port):
        print(f"  NapCat 在别的机器 / 容器：{address}")
    print("把 NapCat 的反向 WebSocket 指向对应那一个，然后回到这里按回车。")
    if token:
        print("（已要求 Authorization: Bearer <token>）")

    if not await server.wait_for_connection(timeout=120.0):
        print("两分钟内没有任何 OneBot 客户端接入。")
        print("检查：NapCat 是否在运行、反向 WS 地址是否正确、端口是否被占用。")
        await server.stop()
        return 1

    print(f"客户端已接入（第 {server.connections} 次连接）。")

    settings = load_settings()
    if not str(getattr(settings, "qq_account_self_id", "") or "").strip():
        print("通道通了，但 .env 里没有 BOT_QQ_ACCOUNT_SELF_ID（机器人自己的 QQ 号），")
        print("无法判断哪些消息是机器人自己发的，自检到此为止。")
        await server.stop()
        return 1
    REPLY_BUDGET.clear()
    throttle = ConversationThrottle()
    SESSION_REVISIONS.clear()

    session = AccountSession(
        settings=settings,
        transport=None,
        bot_account=str(settings.qq_account_self_id),
        group_reply_mode=str(getattr(settings, "qq_account_group_reply_mode", "reply")),
        throttle=ConversationThrottle(),
    )
    transport = OneBotWebSocketTransport(
        ws_url=server.url,
        token=token,
        on_event=session.handle_payload,
        on_connected=session.on_connected,
        on_disconnected=session.on_disconnected,
    )
    # The session sends through whatever object it holds, so hand it the real
    # transport; there is no second connection.
    session.transport = transport

    policy = build_policy(settings)
    print(
        f"访问策略：群 {sorted(policy.allowed_groups) or '（未配置，全部拒绝）'}"
        f"；好友 {sorted(policy.allowed_users) or '（未配置，全部拒绝）'}"
    )
    loop = asyncio.get_running_loop()
    for label, payload in _build_events(session):
        answer = await loop.run_in_executor(
            None, input, f"按回车向「{label}」发一条测试事件（q 退出）："
        )
        if str(answer).strip().lower() == "q":
            break
        server.clear()
        await server.push_event(payload)
        action = await server.wait_for_action(timeout=30.0)
        if action is None:
            print("  机器人没有回复。可能原因：未通过访问策略、被频控挡住、或路由为空。")
        else:
            print(f"  机器人发出 {action.action}：{action.params}")
        print(f"  跳过统计：{session.stats.skipped or '（无）'}")

    print()
    print("自检结束。这只证明通道与策略按预期工作，不代表已经在真实群里稳定运行。")
    await server.stop()
    REPLY_BUDGET.clear()
    throttle.clear()
    SESSION_REVISIONS.clear()
    return 0


def _reachable_addresses(port: int) -> list[str]:
    """Addresses another machine could use to reach this process."""
    addresses: list[str] = []
    try:
        import socket

        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            address = info[4][0]
            if address and not address.startswith("127.") and address not in addresses:
                addresses.append(address)
    except OSError:
        pass
    # WSL's own IP is the one a Windows-side client needs.
    for candidate in _wsl_addresses():
        if candidate not in addresses:
            addresses.append(candidate)
    return [f"ws://{address}:{port}" for address in addresses]


def _wsl_addresses() -> list[str]:
    """This machine's non-loopback IPv4 addresses, without shelling out."""
    import socket

    found: list[str] = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No packet is sent; this only asks the routing table which source it
        # would use, which is exactly the address another host can reach.
        probe.connect(("8.8.8.8", 80))
        found.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    return found


def _ids(text: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in str(text or "").split(",") if part.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description="QQ 账号通道自检（不代替机器人本体）")
    parser.add_argument("--port", type=int, default=3001, help="监听端口，默认 3001")
    parser.add_argument("--token", default="", help="可选的 Bearer token")
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=(
            "监听地址。默认 0.0.0.0，让 NapCat 在别的机器或别的 WSL 层也能连上；"
            "只在本机测试时可以改成 127.0.0.1"
        ),
    )
    args = parser.parse_args()
    try:
        return asyncio.run(run(args.port, args.token, args.host))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
