"""Read the OneBot endpoint that the existing Windows NapCat runtime publishes.

``start-client.ps1`` rewrites ``integration.json`` whenever the WSL address changes, so the
endpoint is read at every start instead of being copied into the repository.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


class NapcatConfigError(Exception):
    pass


@dataclass(frozen=True)
class OneBotEndpoint:
    host: str
    port: int
    token: str = field(default="", repr=False)


def parse_endpoint(url: str, token: str = "") -> OneBotEndpoint:
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as error:
        raise NapcatConfigError("OneBot 地址的端口不合法") from error
    if parts.scheme != "ws" or not parts.hostname or port is None or port == 0:
        raise NapcatConfigError("OneBot 地址必须是带主机和端口的 ws:// 地址")
    if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
        raise NapcatConfigError("连接器地址只支持主机和端口；认证请通过单独的 token 配置")
    return OneBotEndpoint(host=parts.hostname, port=port, token=token)


def read_integration(path: Path) -> OneBotEndpoint:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise NapcatConfigError(f"无法读取 NapCat 接入文件：{path}") from error
    if not isinstance(data, dict) or not isinstance(data.get("wsUrl"), str):
        raise NapcatConfigError("NapCat 接入文件缺少 wsUrl")
    token = data.get("wsToken") or ""
    if not isinstance(token, str):
        raise NapcatConfigError("wsToken 必须是字符串")
    return parse_endpoint(data["wsUrl"], token)
