# 真实验收指南

本指南提供离线验证和真实 QQ 往返的操作步骤。文档分工见 [导航](README.md)，飞书接入状态见 [部署说明](deployment.md#飞书入口)。

QQ 账号入口已有有限私聊与真实 POI 查询的历史记录；这些记录不能证明当前版本、群聊或持续运行。下面按实际入口重新验收并记录日期、版本和范围；飞书 SDK 事件源仍待接入，尚无真实飞书往返。

---

## 第一步：跑一次离线门槛（1 分钟，不需要任何凭证）

```bash
cd ~/projects/DoraYmon
.venv/bin/python -m scripts.preflight_offline
```

它会依次运行三套门槛，并**逐条说明每套门槛证明什么、不证明什么**：

| 门槛 | 证明 | 不证明 |
|---|---|---|
| 离线单元测试 | 代码在离线条件下的行为，含边界与失败恢复 | 真实 QQ 收发、真实模型质量、真实地图数据 |
| 回放场景 | 路由、身份隔离与会话保证在固定场景下成立 | 真实回答质量（用的是假模型） |
| RAG 召回门槛 | Recall@3/MRR 门槛、无答案空召回、权限不泄漏 | 真实语料之外的泛化 |

预期输出结尾是"全部离线门槛通过"，并且**一定会**打印这句话：

> 在真实往返发生之前，不能声称机器人已在真实 QQ 环境可用。

已选门槛失败、超时、无法执行或返回“跳过”时，脚本退出码为 1，并说明原因。加 `--quick` 只运行快的门槛，报告会列出主动排除的项目；全部已选项目通过才返回 0，该结果不表示完整验收通过。

[CI 工作流](../.github/workflows/offline.yml) 在 Python 3.10/3.14 使用 `python -m scripts.preflight_offline`，子进程沿用当前解释器。无需 QQ、模型或地点服务凭证；新增工作流在推送并执行前不构成远端 CI 通过证据。

---

## 第二步：选一条路线做真实往返

### 路线 A：QQ 账号（OneBot / NapCat）——更快

需要：一个**建议用小号**的 QQ 号、NapCat 已登录，以及一个用于测试的好友 QQ 号或群号。

在 NapCat 中启用 **WebSocket 服务端（正向 WebSocket）**，设置监听地址、端口和访问令牌。
`scripts.run_account` 会主动连接这个服务端；把下面的地址与令牌填入机器人配置。

```bash
# 在 .env 里配置：
#      BOT_QQ_ACCOUNT_ENABLED=true
#      BOT_QQ_ACCOUNT_SELF_ID=<小号QQ号>
#      BOT_QQ_ACCOUNT_WS_URL=ws://<NapCat 可达地址>:3001
#      BOT_QQ_ACCOUNT_TOKEN=<NapCat WebSocket 访问令牌>
#      BOT_QQ_ACCOUNT_FRIENDS=<测试好友QQ号，私聊验收时填写>
#      BOT_QQ_ACCOUNT_GROUPS=<测试群号，群聊验收时填写>
#      BOT_ADMIN_OPENIDS=<你的 openid/QQ，用于静默开关>
# 启动：
.venv/bin/python -m scripts.run_account
```

先通过 OneBot 的 `get_login_info`、`get_status` 确认登录账号和在线状态，再由白名单中的好友或群发起测试消息。
Windows NapCat 与 WSL 机器人之间的地址选择见 [部署说明](deployment.md#qq-账号接入)。

### 路线 B：官方开放平台——需要审核与白名单

需要：AppID 与 AppSecret（`AppSecret` 只在生成时完整显示一次）、服务器公网 IP、机器人审核状态。

```bash
# 本地检查 + 联网验证凭证（凭证值不会被打印）
.venv/bin/python -m scripts.preflight_official_bot --check
# 通过后：
.venv/bin/python main.py
```

**两个必须知道的坑**（自检会提醒）：

1. 换取凭证的接口**失败时 HTTP 仍是 200**，真正结果在响应体的 `code` 里。已核对：`100016` AppID/Secret 不正确、`100007` AppID 无效或机器人被封禁、`10004` 该 AppID 下没有机器人、`100001` 请求过于频繁。
2. **新建机器人默认强制开启 IP 白名单且不能关闭**，白名单**只作用于正式环境、不影响沙箱**。所以**沙箱能通不代表正式环境能通**。

---

## 第三步：真实往返时看什么

| 现象 | 说明 |
|---|---|
| 私聊能收到回复 | 通道 + 权限 + 回复路径都通了 |
| 群里 @ 机器人有回复 | 群路径通了 |
| 群里不 @ 时**不**回复 | 发言判断正确（这是期望行为，不是故障） |
| 同一条消息重放**不会**回复两次 | 去重生效 |
| 连发多条，之后被节流 | 账号侧自我保护生效 |
| 发 `/静默 on` 后不再回复，`/静默 off` 恢复 | 管理开关可用 |
| `/status` 显示发送成功/失败段数与当日 token 用量 | 可观测性可用 |

**出问题时**：如果机器人在乱回，先发 `/静默 on`（需要你在 `BOT_ADMIN_OPENIDS` 里）。静默只挡普通回复，不挡 `/静默` 本身。

---

## 离线已覆盖的范围（可对照代码与测试核对）

| 能力 | 落点 | 验证方式 |
|---|---|---|
| 统一消息/身份/发送结果契约 | `services/message_contract.py`、`send_result.py` | 单元测试 + 回放 |
| 平台与账号命名空间隔离 | `doraymon/identity.py` | 回放检查 `no_cross_identity_leak` |
| 清空与在途回复竞争 | `services/session_revisions.py` | 回放检查 `stale_reply_not_written_back` |
| 账号入口（OneBot/NapCat） | `services/onebot_transport.py`、`account_session.py` | **真实 socket** 集成测试 |
| 访问策略默认拒绝 | `services/account_policy.py` | **真实 socket** 集成测试 |
| 账号侧自节流 | `services/account_throttle.py` | **真实 socket** 集成测试 |
| 入口进程本身 | `scripts/run_account.py` | **真实子进程** 测试 |
| 官方通道平台限制 | `services/reply_budget.py` | 按官方文档核对 + 单元测试 |
| 官方入口可启动性 | `main.py` | 实测到达开放平台 |
| 目录导入（含预览/撤回） | `services/meal_catalog_import.py`、`catalog_table.py` | 命令行端到端 + 单元测试 |
| 附近推荐与追问 | `services/meal_session.py`、`nearby_meal_service.py` | 单元测试 + 插件路径实测 |
| 高德 POI 服务 | `services/amap_provider.py` | 按官方文档核对 + 模拟响应 |
| 资料保鲜期 | `services/freshness.py` | 单元测试 + 插件路径实测 |
| 用餐反馈（吃过/拒绝） | `storage/meal_feedback_store.py` | 单元测试 + 插件路径实测 |
| 静默开关 | `services/silence.py` | 单元测试 + 真实账号会话 |
| 每日模型额度 | `services/model_usage.py` | 单元测试（含服务层不发请求） |

---

## 仍未完成的事（如实列出）

1. **真实 QQ 往返**——第一步与第二步都需要你的账号或凭证。这是唯一挡住"目标完成"的事。
2. **真实凭证的联网验证**——`preflight_official_bot --check` 尚未用有效凭证跑过。
3. **真实地图数据**——高德客户端已按文档实现并有模拟测试，但未用真实 key 打过实网。
4. **菜单图片/PDF 识别**——需要 OCR 依赖，尚未实现。
5. **真实目录数据**——需要你给一个试点区域与菜单来源（G0）。

前四项中，第 1 项只有你能做；第 2、3 项需要凭证；第 4、5 项我可以继续做，但价值低于一次真实往返。
