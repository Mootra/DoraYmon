# NapCat 常驻运行

本目录用于已有的 Windows NapCat + WSL DoraYmon 环境。Windows 登录后由计划任务
`DoraYmon-NapCat` 每分钟检查一次，WSL 中的 `doraymon-qq.service` 托管 QQ 后端。
电脑关机、休眠或退出 Windows 用户会话时，不保证在线。电脑关机后仍需在线，应部署到常开主机。

## 日常操作

在 Windows PowerShell 中进入本机运行目录：

```powershell
Set-Location "$env:USERPROFILE\DoraYmon-NapCat"
.\runtime.ps1 -Action Status
.\runtime.ps1 -Action Stop
.\runtime.ps1 -Action Start
```

`Stop` 会停用自动任务和后端自启，并停止 NapCat、后端与 WSL 保活进程；`Start` 恢复它们。
需要重新登录时运行 `open-client.cmd`，在本机浏览器扫码；不要转发带 token 的页面地址或登录二维码。

`runtime-health.json` 保存最近检查时间、QQ 登录/OneBot/后端状态、失败次数及恢复时间，不含聊天正文或凭据。
登录失效或检查失败时会尝试显示 Windows 通知；通知不可用时，以该文件为准。

## 安装文件

安装时保留原 NapCat 目录和 QQ 数据，仅复制以下运行脚本到本机运行目录：

| 开发源 | 安装文件 |
|---|---|
| `scripts/napcat_runtime.py` | `account-client.py` |
| `deploy/napcat/windows/start-client.ps1` | `start-client.ps1` |
| `deploy/napcat/windows/runtime.ps1` | `runtime.ps1` |

`integration.json` 使用已有的 `account`、`webui`、`webuiToken`、`wsUrl`、`wsToken`。
本目录的 `runtime-profile.example.json` 说明私有 `runtime-profile.json` 的字段。
安装前填写实际路径和已授权的访问范围；`allowlist` 模式的名单不能为空。数据目录需沿用原实例，
`dataDir` 指最终 `qq_account` 子目录的父目录。真实 `.env` 由用户维护，脚本只读取，不修改。

`doraymon-qq.service.in` 替换 `@PROJECT@` 和 `@CLIENT@` 后放入 WSL 的
`~/.config/systemd/user/doraymon-qq.service`。`@CLIENT@` 是已安装 `account-client.py` 的 WSL 路径。
启用该用户的 lingering，再执行：

```bash
systemctl --user daemon-reload
systemctl --user enable --now doraymon-qq.service
```

Windows 任务使用当前用户的交互登录身份、普通权限，以隐藏窗口运行：

```text
powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "<运行目录>\runtime.ps1" -Action Check
```

触发器为该用户登录，以及每分钟重复；允许电池运行、禁止任务重叠，单次执行上限 2 分钟。
安装前备份原启动脚本和配置到本机 `runtime-backups/`，不要将运行目录、凭据和登录数据加入 Git。

## 恢复机制

- 后端进程退出由 systemd 在 10 秒后重启；连续启动失败受频率限制。
- NapCat 进程退出后，下一轮检查使用已有 QQ 数据尝试快速登录。
- WSL 网络地址变化时更新 OneBot 监听与接入地址，并重载两端。
- WebUI、OneBot 或离线状态连续失败 3 轮才重启客户端和后端；两次此类恢复至少相隔 10 分钟。
- 等待扫码不会触发自动重启。会话失效或账号验证仍需用户操作。
- 单独保留一个隐藏的 WSL 客户端进程，避免仅有 systemd 服务时实例因空闲而退出。
  相关行为见 [Microsoft WSL 文档](https://learn.microsoft.com/en-us/windows/wsl/systemd)。

`status` 读取 NapCat 管理接口，`probe` 仅调用 `get_login_info`、`get_status` 和 `get_version_info`，
不会发送 QQ 消息或请求模型。端口、进程、在线探针、真实聊天往返和多日运行是不同的验证层级。

NapCat 后端自带的离线恢复与版本相关，升级时应备份配置并验证当前版本；安装说明见
[NapCat 文档](https://napneko.github.io/guide/boot/Shell)。OneBot 保持 token 鉴权，管理面板仅监听本机。
