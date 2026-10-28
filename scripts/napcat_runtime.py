"""Operate an installed NapCat/WSL account runtime without printing credentials."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.request


def post(config: dict, path: str, payload: dict, credential: str = "") -> dict:
    headers = {"Content-Type": "application/json"}
    if credential:
        headers["Authorization"] = "Bearer " + credential
    request = urllib.request.Request(
        config["webui"] + "/api" + path,
        json.dumps(payload).encode("utf-8"), headers=headers, method="POST",
    )
    # Management traffic must stay local even when the shell has a proxy set.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5) as response:
        result = json.load(response)
    if result.get("code") != 0:
        raise RuntimeError("NapCat API: " + str(result.get("message", result.get("code"))))
    return result["data"]


def status(config: dict) -> dict:
    login = post(config, "/auth/login", {
        "hash": hashlib.sha256((config["webuiToken"] + ".napcat").encode()).hexdigest(),
    })
    current = post(config, "/QQLogin/CheckLoginStatus", {}, login["Credential"])
    return {key: current.get(key) for key in (
        "isLogin", "isOffline", "loginPhase", "coreReady", "loginError",
    )}


async def probe(config: dict) -> dict:
    import aiohttp

    async def receive_reply(connection, action):
        while True:
            response = await connection.receive_json()
            if response.get("echo") == action:
                return response

    replies = {}
    async with aiohttp.ClientSession(
        trust_env=False, timeout=aiohttp.ClientTimeout(total=15, sock_connect=5),
    ) as session:
        async with session.ws_connect(
            config["wsUrl"], headers={"Authorization": "Bearer " + config["wsToken"]},
            timeout=aiohttp.ClientWSTimeout(ws_receive=5),
        ) as connection:
            for action in ("get_login_info", "get_status", "get_version_info"):
                await connection.send_json({"action": action, "params": {}, "echo": action})
                response = await asyncio.wait_for(receive_reply(connection, action), timeout=8)
                if response.get("status") != "ok" or response.get("retcode") != 0:
                    raise RuntimeError(action + " failed")
                replies[action] = response.get("data", {})
    if str(replies["get_login_info"].get("user_id", "")) != str(config["account"]):
        raise RuntimeError("Logged-in account does not match the configured account")
    return {
        "authenticated": True, "selfIdMatches": True,
        "online": replies["get_status"].get("online") is True,
        "appVersion": replies["get_version_info"].get("app_version"),
        "qqMessagesSent": 0,
    }


def runtime_environment(config: dict, profile: dict) -> dict[str, str]:
    mode = profile["accessMode"]
    if mode not in {"allowlist", "chat"}:
        raise ValueError("accessMode must be allowlist or chat")
    friends = [str(value) for value in profile["friends"]]
    groups = [str(value) for value in profile["groups"]]
    if any(not value.isdigit() for value in friends + groups):
        raise ValueError("QQ identifiers must contain digits only")
    if mode == "allowlist" and not friends and not groups:
        raise ValueError("An allowlist runtime requires a friend or group")
    if not str(config["account"]).isdigit() or not config["wsToken"]:
        raise ValueError("A QQ account and OneBot token are required")
    return {
        "BOT_QQ_ACCOUNT_ENABLED": "true",
        "BOT_QQ_ACCOUNT_SELF_ID": str(config["account"]),
        "BOT_QQ_ACCOUNT_WS_URL": config["wsUrl"],
        "BOT_QQ_ACCOUNT_TOKEN": config["wsToken"],
        "BOT_QQ_ACCOUNT_ACCESS_MODE": mode,
        "BOT_QQ_ACCOUNT_FRIENDS": ",".join(friends),
        "BOT_QQ_ACCOUNT_GROUPS": ",".join(groups),
        "BOT_PC_BRIDGE_LEASE_PATH": profile["leasePath"],
        "DATA_DIR": profile["dataDir"],
        "LOG_DIR": profile["logDir"],
    }


def run(config: dict, profile: dict) -> int:
    from dotenv import load_dotenv

    environment = runtime_environment(config, profile)
    project = Path(profile["project"])
    if not project.is_dir():
        raise RuntimeError("Run with the configured WSL project virtualenv")
    load_dotenv(profile["environmentFile"], override=False)
    os.environ.update(environment)
    os.chdir(project)
    sys.path.insert(0, str(project))
    from scripts.run_account import main

    return main()


def redact(value: object, config: dict) -> str:
    message = str(value)
    for key in ("webuiToken", "wsToken"):
        if config.get(key):
            message = message.replace(config[key], "<redacted>")
    return message


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "probe", "run"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    config = {}
    try:
        config = json.loads((args.root / "integration.json").read_text("utf-8-sig"))
        if args.command == "run":
            profile = json.loads((args.root / "runtime-profile.json").read_text("utf-8-sig"))
            return run(config, profile)
        result = status(config) if args.command == "status" else asyncio.run(probe(config))
        print(redact(json.dumps(result, ensure_ascii=False), config))
        return 1 if args.command == "probe" and not result["online"] else 0
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        print(type(error).__name__ + ": " + redact(error, config), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
