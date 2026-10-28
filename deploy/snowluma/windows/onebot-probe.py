"""Check OneBot identity and connectivity without sending QQ messages."""

import argparse
import asyncio
import json
from pathlib import Path

import aiohttp


async def probe(config: dict) -> dict:
    replies = {}
    async with asyncio.timeout(15):
        async with aiohttp.ClientSession(trust_env=False) as session:
            async with session.ws_connect(
                config["wsUrl"],
                headers={"Authorization": "Bearer " + config["wsToken"]},
                timeout=aiohttp.ClientWSTimeout(ws_receive=5),
            ) as connection:
                for action in ("get_login_info", "get_status", "get_version_info"):
                    await connection.send_json({"action": action, "params": {}, "echo": action})
                    while True:
                        reply = await connection.receive_json()
                        if reply.get("echo") == action:
                            break
                    if reply.get("status") != "ok" or reply.get("retcode") != 0:
                        raise RuntimeError(action + " failed")
                    replies[action] = reply.get("data", {})
    if str(replies["get_login_info"].get("user_id", "")) != str(config["account"]):
        raise RuntimeError("The connected QQ account does not match the bot account")
    return {
        "authenticated": True,
        "selfIdMatches": True,
        "online": replies["get_status"].get("online") is True,
        "appName": replies["get_version_info"].get("app_name"),
        "appVersion": replies["get_version_info"].get("app_version"),
        "qqMessagesSent": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads((args.root / "integration.json").read_text("utf-8-sig"))
    try:
        result = asyncio.run(probe(config))
    except Exception as error:
        print(json.dumps({"error": type(error).__name__}))
        return 1
    print(json.dumps(result))
    return 0 if result["online"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
