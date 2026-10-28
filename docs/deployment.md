# 部署说明

## 本地运行

使用 Python 3.10 或更高版本。直接依赖统一在 `requirements.txt` 声明，包括 OneBot 传输直接使用的 `aiohttp`。CI 配置覆盖 Python 3.10 和 3.14；本机当前验证环境为 3.14，其他版本以实际 CI 结果为准。

```bash
cd DoraYmon
python -m venv .venv
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
notepad .env
python main.py
```

Linux/macOS：

```bash
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
python main.py
```

## 宝塔部署

上传项目到：

```bash
/www/wwwroot/DoraYmon
```

创建虚拟环境：

```bash
cd /www/wwwroot/DoraYmon
python3 -m venv venv
source venv/bin/activate
```

安装依赖：

```bash
pip install -r requirements.txt
```

创建配置：

```bash
cp .env.example .env
nano .env
```

至少填写：

```bash
QQBOT_APPID=
QQBOT_SECRET=
DEEPSEEK_API_KEY=
```

短期上下文默认开启。如需关闭，可在 `.env` 中设置：

```bash
BOT_ENABLE_CHAT_HISTORY=true
BOT_CHAT_HISTORY_LIMIT=40
BOT_CHAT_HISTORY_MAX_CONTENT_LENGTH=4000
BOT_CHAT_CONTEXT_MAX_CHARS=6000
BOT_CHAT_CONTEXT_SUMMARY_MAX_CHARS=1200
BOT_CHAT_CONTEXT_TTL_MINUTES=720
BOT_CHAT_SUMMARY_RETENTION_DAYS=7
BOT_REPLY_MAX_SEGMENTS=3
BOT_REPLY_MAX_TOTAL_CHARS=4000
```

开启后，运行数据写入本地 SQLite；闲置会话默认在最后活跃 720 分钟后整体过期，设置 `BOT_CHAT_CONTEXT_TTL_MINUTES=0` 可关闭自动过期。部署和备份时不要提交或公开真实数据库及聊天记录。

本地知识库默认关闭。将 UTF-8 Markdown/TXT 放入 `resources/knowledge/` 后建立索引：

```bash
python scripts/index_knowledge.py
```

确认索引成功后，在 `.env` 中设置：

```bash
BOT_ENABLE_RAG=true
BOT_KNOWLEDGE_DIR=resources/knowledge
BOT_RAG_TOP_K=3
BOT_RAG_TOKENIZER=trigram
```

知识索引保存在运行数据目录的 `knowledge.db`，不要提交 Git。`/重建知识库` 只允许 `BOT_ADMIN_OPENIDS` 中的管理员使用。

测试运行：

```bash
python main.py
```

宝塔进程守护管理器配置：

```text
名称：DoraYmon
运行目录：/www/wwwroot/DoraYmon
启动命令：/www/wwwroot/DoraYmon/venv/bin/python /www/wwwroot/DoraYmon/main.py
```

当前使用 `botpy` 长连接，不需要 Nginx 反向代理和域名。改成 Webhook 模式时，再配置域名和 HTTPS。

## 附近店铺查询（可选）

默认完全离线，只用人工整理的目录回答。要查询真实附近店铺，可配置高德：

```bash
BOT_NEARBY_PROVIDER=amap
BOT_NEARBY_PROVIDER_KEY=<你的高德 Web 服务 Key>
BOT_NEARBY_PROVIDER_CITY=武汉          # 可选，提高地理编码准确度
BOT_NEARBY_PROVIDER_RADIUS_METERS=1500
```

Key 只写在本地 `.env`（已被 Git 忽略），不要写进 `config.example.yaml` 或提交。

边界：POI 只提供“这家店存在、离你多远、人均多少”，**不能**说明这家店卖什么、某道菜多少钱，因此 POI 结果不会被当成餐点推荐；人均消费也不会被当成菜单价、不能用来判断预算够不够。菜单与餐点仍来自人工整理的目录。

## 官方 Bot 凭证自检（接正式环境之前）

正式环境之前先跑这个。它检查本地配置，加 `--check` 会真的向开放平台换一次凭证：

```bash
.venv/bin/python -m scripts.preflight_official_bot          # 只做本地检查
.venv/bin/python -m scripts.preflight_official_bot --check  # 联网验证凭证
```

一个必须知道的坑：**换取凭证的接口失败时 HTTP 仍然是 200**，真正的结果在响应体的 `code` 里。只判断 HTTP 状态会把「凭证错误」当成「成功」。已核对错误码：`0` 成功、`100016` AppID 或 AppSecret 不正确、`100007` AppID 无效或机器人被封禁/删除、`10004` 该 AppID 下没有机器人、`100001` 请求过于频繁。

另一条运维约束：**新建机器人默认开启 IP 白名单且不能关闭**，只能填白名单内容；白名单**只作用于正式环境、不影响沙箱**。所以沙箱能通**不代表**正式环境能通——正式环境要求服务器公网 IP 在白名单里，且机器人已审核上线。

凭证值不会被打印出来。

## QQ 账号接入

`scripts.run_account` 使用正向 WebSocket：NapCat 开启 **WebSocket 服务端**，
机器人通过 `BOT_QQ_ACCOUNT_WS_URL` 主动连接，`BOT_QQ_ACCOUNT_TOKEN` 与服务端令牌保持一致。
接收与发送共用这条连接。默认 `BOT_QQ_ACCOUNT_ACCESS_MODE=chat` 接收普通私聊，群成员
不受好友列表过滤；`BOT_QQ_ACCOUNT_GROUPS` 非空时限定群范围，空时处理账号所在群。
封闭验收可设 `BOT_QQ_ACCOUNT_ACCESS_MODE=allowlist`，通过 `BOT_QQ_ACCOUNT_GROUPS`、
`BOT_QQ_ACCOUNT_FRIENDS` 限制群和用户；两份名单都空时拒绝启动。

普通群消息默认按内容选择性接话；可设 `BOT_QQ_ACCOUNT_GROUP_PARTICIPATION_ENABLED=false`
关闭普通参与，保留 @、引用、叫名字和连续追问。普通参与默认每群冷却 30 秒、每分钟 2 轮；
直接请求默认无冷却，每分钟最多 8 段。已有环境若配置了
`BOT_QQ_ACCOUNT_GROUP_COOLDOWN_SECONDS=3`，需要显式改为 `0` 才移除直接请求的旧冷却。
详细配置与合成回放见 [QQ 群聊交互](qq_group_interaction.md)。

同一操作系统内可使用 `ws://127.0.0.1:3001`。NapCat 在 Windows、机器人在 WSL2 NAT
环境时，NapCat 可绑定 Windows 的 WSL 虚拟网卡地址，机器人连接该地址，例如
`ws://172.23.176.1:3001`。虚拟网卡地址应以当前系统为准，重建 WSL 网络后重新检查。
Windows 到 WSL 的 localhost 转发测试不能证明 WSL 到 Windows 的这条连接也可用。

登录后先调用 `get_login_info` 与 `get_status` 核对目标账号和在线状态，再从已确认的验收对象
发起一次真实消息往返。管理页可访问只证明客户端服务已启动；真实收发结果需要单独记录。

`scripts.verify_account_channel` 依赖 `tests.onebot_server` 和合成消息，用于开发诊断。
当前正式入口未实现反向 WebSocket 服务端，不能把 NapCat 反连这个工具的结果当成正式入口验收。

## 入站回路自检（不碰 QQ）

真实账号的入站验证需要有人真的发一条消息，因此常常被跳过。这条命令把「入站 socket → 适配 → 访问策略 → 路由 → 出站 action」整段跑一遍：

```bash
.venv/bin/python -m scripts.verify_inbound_loop
```

它启动**真实的** `scripts.run_account` 子进程，指向本脚本自己开的 OneBot 服务端，然后推一条与 NapCat 同形状的事件，并报告机器人实际发出的 action。事件来源是合成的；机器人、传输层、适配器、策略、路由与回复路径都是真的。默认用 `/ping`，因为它不需要模型 key，这样观察到的就只有管线本身。

**它证明什么**：机器人收到一条入站事件会正确地产生出站回复。
**它不证明什么**：NapCat 会把你发的消息推给机器人——那需要真实账号的一次真实消息。

## 静默开关

出问题时需要**一个立刻生效的动作**让机器人闭嘴，而不是改配置再重启。

- 运行中：管理员发 `/静默 on` 停止回复，`/静默 off` 恢复，`/静默 status` 查看状态。只有 `BOT_ADMIN_OPENIDS` 里的人能用。
- 启动即静默：设 `BOT_SILENCE=true`，机器人只响应 `/静默` 命令，其余一律不回。适合刚上线、先观察流量再放开的阶段。

两点必须说清楚：

1. 静默只挡普通回复，**不挡 `/静默` 本身**——否则只能靠重启才能解封，那这个开关就没有意义。
2. 运行中的静默状态**存在内存里，重启即恢复配置默认值**。静默后重新部署，机器人会重新开口，这是有意为之，不是遗漏。

## 每日模型额度

公开试用前需要一道费用上限。额度**按 token 计**：token 由接口回报、能直接测；金额需要价目表，而价目表会变，报一个错的成本比不报更糟。

```bash
BOT_DAILY_TOKEN_CAP=2000000            # 0 表示不限制
BOT_MODEL_PRICE_PER_MILLION_CNY=        # 可选，仅用于显示「估算」金额
```

用量按**本地日历日**结转，达到阈值后拒绝新请求并说明额度已用完。`/status` 显示当前进程记录的请求次数与 token 用量。

当前账本保存在进程内，重启会丢失统计；并发请求的检查与结算分离，也可能超过配置阈值。公开试用前需补持久化和预算预占，不能将当前配置作为跨重启的每日硬预算，详见 [项目审查](project_review_20261023.md)。

没有配置单价时不显示金额：`没有单价` 和 `花费为零` 是两句不同的话。

## 用餐反馈（明确说法才算数）

推荐过、不想去、吃过了，是三件不同的事，机器人分开记：

| 用户说 | 记为什么 | 影响 |
|---|---|---|
| 「这家不想去」「换掉这家」 | 一次拒绝 | 本次推荐里不再出现这家 |
| 「我吃了这家」「吃过了」 | 确实吃过 | 只做记录 |
| 只是收到推荐、没说话 | **什么都不记** | 无 |

两条边界：

1. **推荐 ≠ 吃过**。没回应的推荐不留任何痕迹——没发生过的事没有可记的。
2. **拒绝 ≠ 不喜欢**。拒绝只记这一次的选择，回复里也会明说「不代表你不喜欢这家店」。把一次拒绝变成永久口味偏好，等于替用户编造事实。

## 资料保鲜期（过期检查）

菜单价会过时，所以每条资料都带取得时间，超过保鲜期时**照常回答但标明年龄**：

```text
菜单价：¥18（menu），来源 menu（2025-09-14 的资料，已 400 天，可能已变）
以下资料是 2025-09-14 取得的（400 天前），超过 90 天的保鲜期，价格和菜单可能已经变了。
```

两条规则：**过期资料不删除**（当时的记录当时是真的，删掉就把“可能有用”变成“没有答案”）；**过期资料绝不当作当前资料呈现**（裸写一个 ¥18，和今早刚查到的 ¥18 读起来一模一样）。

各类型保鲜期不同，因为过时速度不同：菜单/网页/点评 90 天、上传资料与商家信息 180 天、地点 365 天。

## 门店/餐点目录导入

目录放在 `data/meal_catalog/meal_catalog.json`。缺少来源的店铺或餐点会被拒绝，不会以占位来源进入目录。

支持两种输入：JSON 目录文件，或店铺/餐点表格（`.csv`/`.tsv`/`.xlsx`）。
表格按表头自动判断是店铺表还是餐点表：店铺表需要 `shop_id`、`name`，餐点表需要
`item_id`、`shop_id`、`name`。多值单元格用 `;` 分隔。

表格列（多余的列会被忽略）：

```text
店铺表：shop_id, name, address, distance_meters, map_url, open_now
餐点表：item_id, shop_id, name, composition, portion,
        flavours, flavour_detail, price_cny, price_min_cny, price_max_cny,
        price_basis, suitable_meals
两者共用：source_kind, source_reference, source_fetched_at
```

表格若没有来源列，用 `--source 类型:标识` 给一个默认来源，例如
`--source upload:menu-2026-10.csv`。**没有来源的记录会被拒绝**，这是有意为之：
没有出处的餐点宁可不要，也不能看起来有依据。

店铺与餐点通常分两个文件，餐点表引用的是目录里已有的店铺，所以先导入店铺表。
`.xlsx` 需要额外安装 `openpyxl`（`pip install openpyxl`），没装时会明确提示；
也可以先把表格另存为 CSV。

先预览再写入：

```bash
.venv/bin/python -m scripts.import_catalog --from incoming.json          # 只预览
.venv/bin/python -m scripts.import_catalog --from incoming.json --apply  # 确认写入
```

写入前会自动把现有目录备份成 `meal_catalog.backup-<时间>-<批次>.json`，撤回就是把该文件复制回 `meal_catalog.json`。备份文件不会被目录读取，也不会进入 Git。

## NapCat 常驻

QQ 账号在已有 Windows + WSL 环境中的自动启动、状态检查和故障恢复，见
[NapCat 常驻运行](../deploy/napcat/README.md)。

## 飞书入口

启动脚本为 `.venv/bin/python -m scripts.run_feishu`，默认 `BOT_FEISHU_ENABLED=false`。飞书使用独立运行数据目录；契约、适配、发送 API、队列传输接缝、会话复用和入口配置已有离线测试。

`services/feishu_sdk.py` 的真实事件源尚未实现，当前环境也未安装 `lark-oapi`。启用开关和填写凭证仍不能使通道接通；SDK 导入兼容性、事件回调、连接就绪/重连与真实往返需要在接入批次分别验证。

接入时需准备用户维护的 `BOT_FEISHU_APP_ID`、`BOT_FEISHU_APP_SECRET`、`BOT_FEISHU_BOT_OPEN_ID`，以及 `BOT_FEISHU_USERS` / `BOT_FEISHU_CHATS` 允许列表。应用类型、权限、订阅和发布步骤按实施时的官方文档核对，凭证值不写进文档或聊天。当前准备事项由本机 STATUS 管理。
