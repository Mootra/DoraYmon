"""Official QQ Bot credential and readiness preflight.

Run this before the official entry, and before spending time on a live trial. It
answers the questions that otherwise cost an hour of guessing: are the credentials
well formed, does the platform accept them, and is this deployment even allowed to
connect.

The most important detail is a trap: the token endpoint answers **HTTP 200 even
when it fails**, putting the real result in the response body's ``code``. Anything
that checks only the HTTP status will report a broken credential as working. This
module therefore reads the body first and treats the HTTP status as secondary.

Verified error codes:

    0       ok
    100001  too many requests
    100007  appid invalid, or the bot is disabled/deleted
    100016  invalid appid or secret
    10004   the AppID has no bot behind it

Verified operational constraint: newly created bots have an IP allowlist enabled by
default and it cannot be turned off, only the allowlist populated. The allowlist
applies to the **production** environment only; the sandbox is unaffected. So a
bot that works in the sandbox can still fail in production purely because the
server's IP is missing from the allowlist.

``--check`` performs a live token request. Nothing else here touches the network,
and the token itself is never printed.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import requests

from doraymon.config import load_settings

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
OFFICIAL_API_HOST = "https://api.sgroup.qq.com"
SANDBOX_API_HOST = "https://sandbox.api.sgroup.qq.com"
REQUEST_TIMEOUT_SECONDS = 10.0

OK_CODE = 0


@dataclass(frozen=True)
class TokenOutcome:
    """What the token endpoint actually said."""

    ok: bool
    code: int | None = None
    message: str = ""
    expires_in: int | None = None
    http_status: int | None = None
    # Kept out of repr so a token can never be printed by accident.
    token: str = field(default="", repr=False)

    def describe(self) -> str:
        if self.ok:
            return f"凭证有效（有效期约 {self.expires_in or '未知'} 秒）"
        return f"凭证被拒绝：code={self.code} {self.message}".strip()


# Codes whose meaning is specific enough to act on. Kept as data so the message
# and the code stay together.
_ERROR_HINTS = {
    100001: "请求过于频繁，稍后重试即可；频繁触发说明有别的地方在反复获取凭证。",
    100007: "AppID 无效，或机器人已被封禁/删除。请核对 AppID 与机器人状态。",
    100016: "AppID 或 AppSecret 不正确。注意 AppSecret 只在生成时完整显示一次。",
    10004: "该 AppID 下没有机器人，请确认 AppID 填对了。",
}


def interpret_token_response(
    payload: Any, *, http_status: int | None = None
) -> TokenOutcome:
    """Read the body, not the HTTP status.

    The endpoint returns 200 with an error payload, so a status check alone would
    report a failure as success.
    """
    if not isinstance(payload, dict):
        return TokenOutcome(
            ok=False, message="响应不是对象，无法判断结果", http_status=http_status
        )
    raw_code = payload.get("code")
    if raw_code is None:
        # A success response carries no code at all, only the token.
        token = str(payload.get("access_token", "") or "").strip()
        if not token:
            return TokenOutcome(
                ok=False,
                message="响应里既没有 code 也没有 access_token，无法判断结果",
                http_status=http_status,
            )
        return TokenOutcome(
            ok=True,
            code=OK_CODE,
            expires_in=_as_int(payload.get("expires_in")),
            http_status=http_status,
            token=token,
        )
    code = _as_int(raw_code)
    if code == OK_CODE:
        return TokenOutcome(
            ok=True,
            code=OK_CODE,
            expires_in=_as_int(payload.get("expires_in")),
            http_status=http_status,
            token=str(payload.get("access_token", "") or "").strip(),
        )
    return TokenOutcome(
        ok=False,
        code=code,
        message=str(payload.get("message", "") or "").strip(),
        http_status=http_status,
    )


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def hint_for(outcome: TokenOutcome) -> str:
    if outcome.ok:
        return ""
    if outcome.code is None:
        return "没有拿到错误码，先确认网络能到达 bots.qq.com。"
    if outcome.code in _ERROR_HINTS:
        return _ERROR_HINTS[outcome.code]
    if outcome.code == 100001:
        return _ERROR_HINTS[100001]
    return "未收录的错误码，请到开放平台文档核对 code 与 message。"


def fetch_token(
    appid: str,
    secret: str,
    *,
    post: Callable[..., Any] | None = None,
    url: str = TOKEN_URL,
) -> TokenOutcome:
    """Ask the platform for a token. Never raises for a rejected credential."""
    requester = post or requests.post
    try:
        response = requester(
            url,
            json={"appId": appid, "clientSecret": secret},
            headers={"Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except Exception as error:  # network, DNS, TLS: all the same to the operator
        return TokenOutcome(ok=False, message=f"请求失败：{type(error).__name__}")

    status = getattr(response, "status_code", None)
    try:
        payload = response.json()
    except ValueError:
        return TokenOutcome(
            ok=False, message="响应不是 JSON", http_status=status
        )
    return interpret_token_response(payload, http_status=status)


def local_problems(settings: Any) -> list[str]:
    """Configuration problems that are worth fixing before any request."""
    problems: list[str] = []
    # The raw value is needed for the whitespace checks: stripping first and then
    # comparing against .strip() makes those checks dead, which is exactly the
    # copy-paste mistake they exist to catch.
    raw_appid = str(getattr(settings, "qqbot_appid", "") or "")
    raw_secret = str(getattr(settings, "qqbot_secret", "") or "")
    appid = raw_appid.strip()
    secret = raw_secret.strip()

    if not appid:
        problems.append("缺少 QQBOT_APPID。")
    elif not appid.isdigit():
        problems.append(f"QQBOT_APPID 通常是纯数字，当前是“{appid}”。")
    if not secret:
        problems.append("缺少 QQBOT_SECRET。")
    else:
        if raw_secret != secret:
            problems.append("QQBOT_SECRET 首尾有空白字符，复制时很容易带上。")
        if any(character.isspace() for character in secret):
            problems.append("QQBOT_SECRET 中间含有空白字符。")
    if not str(getattr(settings, "deepseek_api_key", "") or "").strip():
        problems.append("缺少 DEEPSEEK_API_KEY：机器人能连上，但只会回“未配置”。")
    if not list(getattr(settings, "admin_openids", []) or []):
        problems.append("BOT_ADMIN_OPENIDS 为空：静默开关将没有人能操作。")
    return problems


def environment_notes(settings: Any) -> list[str]:
    """Facts an operator must know before a production attempt."""
    notes: list[str] = []
    if bool(getattr(settings, "qqbot_sandbox", True)):
        notes.append(
            "当前是沙箱模式：只能收到沙箱配置里的群/单聊事件。"
            "沙箱不受 IP 白名单限制，所以沙箱能通**不代表**正式环境能通。"
        )
    notes.append(
        "正式环境需要服务器公网 IP 在开放平台的白名单里；"
        "新建机器人默认开启白名单且不能关闭，"
        "白名单只作用于正式环境、不影响沙箱。"
    )
    notes.append(
        f"正式环境接口地址 {OFFICIAL_API_HOST}，沙箱 {SANDBOX_API_HOST}；"
        f"获取凭证都用 {TOKEN_URL}，不区分环境。"
    )
    groups = list(getattr(settings, "qq_account_groups", []) or [])
    if groups:
        notes.append(f"账号入口允许的群：{len(groups)} 个（与官方入口互不影响）。")
    return notes


def main() -> int:
    parser = argparse.ArgumentParser(
        description="官方 QQ Bot 凭证与就绪自检（默认只做本地检查）"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="联网向开放平台换取 access_token，验证凭证是否有效",
    )
    args = parser.parse_args()

    settings = load_settings()

    print("== 本地配置检查 ==")
    problems = local_problems(settings)
    if problems:
        for problem in problems:
            print(f"  - {problem}")
    else:
        print("  未发现明显问题。")

    print()
    print("== 环境提醒 ==")
    for note in environment_notes(settings):
        print(f"  - {note}")

    if not args.check:
        print()
        print("未联网。加上 --check 会真的向开放平台换取凭证来验证。")
        return 1 if problems else 0

    appid = str(getattr(settings, "qqbot_appid", "") or "").strip()
    secret = str(getattr(settings, "qqbot_secret", "") or "").strip()
    if not appid or not secret:
        print()
        print("缺少 AppID 或 Secret，无法联网验证。")
        return 1

    print()
    print("== 凭证检查 ==")
    outcome = fetch_token(appid, secret)
    print(f"  {outcome.describe()}")
    hint = hint_for(outcome)
    if hint:
        print(f"  建议：{hint}")
    if outcome.ok:
        print("  凭证有效只说明能换到 token，不代表机器人已审核上线或被允许连接正式环境。")
    # The token value is deliberately never printed.
    return 0 if outcome.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
