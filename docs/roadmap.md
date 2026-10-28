# DoraYmon 路线与阶段计划

更新：2026-10-24（Asia/Shanghai）。状态：**草案，D1～D4 待用户确认**。当前进行中的任务只记录在 `STATUS.md`；本文件维护路线、阶段验收和决策。

## 1. 结论

- **聊天主体改用 MaiBot 正式版**，作为 DoraYmon 仓库的锁定依赖（和 slim 依赖 NoneBot2 的方式一样），不再自研对话引擎。DoraYmon 自有代码收缩为三块：用餐等专用能力插件、部署配置模板、评测与运维脚本。
- **QQ 接入不变**：继续用现有 NapCat 常驻、健康检查和自动恢复脚本；MaiBot 官方的统一 QQ 连接器支持 NapCat。
- **AstrBot 辅助与 Neo 启动器不进首期**：AstrBot 保留为 P5 按需扩展，并写明进入条件；管理界面用 MaiBot WebUI。
- **健壮性靠工程纪律，不靠更多自研框架代码**：分层、版本锁定、单实例、影子试运行、备份与一键回退、固定评测基线。
- 官方 QQ Bot 的电脑控制服务（Windows `computer/qq-agent-bridge`）保持独立，不在本计划范围内。

## 2. 现状核对

| 项目 | 事实（2026-10-24 核对） |
|---|---|
| 在线服务 | 旧版 DoraYmon（`refactor/foundation`，含未提交改动）由 `doraymon-qq.service` 托管；Windows NapCat 由计划任务每分钟检查 |
| slim 候选 | NoneBot2/OneBot 单入口，`src/doraymon` 13 个文件共 1381 行；今天复跑 101 项离线测试通过；DS-01、CHAT-01 未提交；从未接过真实 QQ |
| 聊天实现 | 系统提示是一段规则，没有人设和示例；回复上限 4000 字，超过 1500 字才分条；群里靠正则（“怎么、如何、为什么、吃什么”等）决定插话；提示里的群友是 QQ 号；没有跨天记忆、表情包和看图 |
| 参考体验 | 你指定的参考群里，两个机器人的文字回复中位长度为 12～13 字（旧仓库 `docs/private/chat_quality_review.md` 的统计） |
| 上次 Codex 方案 | MaiBot 主体，AstrBot 通过 HTTP API 辅助，借鉴 Neo 启动器；只查了接口，未部署 |

推进不顺的三个原因：

1. 精力主要投在可靠性工程（有序队列、取消、投递确认后入库）上，而“自然聊天”本身——何时开口、说多短、学群友说法、记得人——几乎从零开始。这部分恰好是成熟项目积累最多的地方。
2. 路线多次切换（官方 Bot → 账号 OneBot → 飞书 → slim → 成品），每次都重建同样的基础设施。
3. 文档以流程说明和限定语为主，缺少“阶段—验收—回退”的主干，每次换会话都要重读大量文本。

## 3. 路线比较

| | A 继续自研 slim | **B MaiBot 主体 + 自有插件（推荐）** | C AstrBot 主体 + Chat Plus | D MaiBot + AstrBot 双主体（上次方案） |
|---|---|---|---|---|
| 聊天自然度 | 开口判断、防抖、分条、表达学习、人物记忆都要自己实现 | 核心设计目标：Planner 决定何时说话，自带防抖、分条、表达与黑话学习、人物记忆、表情包 | 依赖单人维护的第三方插件：须关闭官方主动回复，私聊尚未支持，与分段回复类插件有兼容注意事项 | 同 B |
| 健壮性与维护 | 全部代码自己维护 | 主程序由上游维护，插件在独立子进程运行；风险是迭代很快，需要锁版本 | 生态最大，但未关闭 issue 约 690 个（MaiBot 为 54 个） | 两套主程序、两份配置、两套模型调用，故障面翻倍 |
| 你要写的代码 | 最多 | 少：插件、配置、评测 | 少到中 | 中：再加桥接插件 |
| 能练到的 | 底层实现 | 插件开发、行为调参、评测设计、运维 | 插件开发 | 系统集成 |
| 结论 | 不作为主线 | 推荐 | 备选：更看重知识库和插件数量时 | 首期不采用，拆成 B + P5 |

核对依据（2026-10-24）：MaiBot 约 6500 Star、GPL-3.0，10 月 19 日至 23 日连发 1.3.12～1.3.15，插件 SDK 10 天内从 2.12 升到 2.16；官方文档写明一次典型回复会触发 3～6 次模型调用。AstrBot 约 4.3 万 Star、AGPL-3.0，最新 v4.29.1。Chat Plus 约 160 Star。Neo-MoFox 约 120 Star，最新版本仍是 1.2.0-rc.2 预发布；其启动器专为 Neo-MoFox 设计，2026 年 6 月后没有更新。

## 4. 目标架构

```text
Windows：QQ ── NapCat（已有：常驻、健康检查、地址同步）
                 │ OneBot v11 正向 WebSocket + token
WSL：            ▼
  MaiBot 正式版（DoraYmon 的锁定依赖，systemd 托管，WebUI 仅监听 127.0.0.1）
    ├─ 统一 QQ 连接器（官方插件）
    ├─ 开口判断、回复、防抖、分条、学习、记忆（官方能力，只改配置）
    └─ 插件 Runner 子进程
         └─ doraymon_meal 插件（薄适配层）
              └─ doraymon_core（自有领域包：地点、高德、用餐状态；纯 Python + 离线测试）
外部服务：DeepSeek（对话）、嵌入模型（记忆与表达检索）、高德（附近店铺）
```

| 层 | 归属 | 规则 |
|---|---|---|
| QQ 接入 | NapCat + 现有运行脚本 | 不在项目里重写协议；升级 NapCat 前备份配置 |
| 对话引擎 | MaiBot 上游 | 只改配置和提示词，不改 MaiBot 源码；确需改动时先提 issue 或用插件实现 |
| 专用能力 | `doraymon_meal` 插件 | 只通过 SDK 的 `self.ctx` 访问宿主；工具返回结构化事实，措辞交给 MaiBot |
| 领域逻辑 | `doraymon_core` | 不依赖 MaiBot 或 NoneBot，离线测试覆盖；将来换宿主时整体搬走 |
| 部署与评测 | 本仓库 `deploy/`、`evals/` | 配置模板进 Git，密钥不进 Git；改配置和改代码一样留历史 |

仓库与运行数据分开放：

```text
DoraYmon/                        仓库，就是 Dora
├─ pyproject.toml、uv.lock       依赖 maibot==1.3.15，确切版本由锁文件记录
├─ deploy/                       配置模板（人设、聊天节奏、名单，不含密钥）、启动/备份/升级脚本、systemd 单元
├─ plugins/doraymon_meal/        Dora 自己的插件
├─ core/                         doraymon_core 与离线测试
└─ evals/                        场景与评测
~/.local/share/doraymon/maibot/  MaiBot 实例目录：config（含密钥）、data（记忆、聊天库）、logs、plugins（链接到仓库里的插件）
```

MaiBot 官方支持代码与实例目录分离（`maibot init --directory`、`maibot run --directory`），PyPI 上的 `maibot` 包由官方仓库的发布流程上传。记忆和聊天库放在仓库外，重建或清理仓库都不会误删。不复制 MaiBot 源码进仓库：复制后拿不到官方更新、分不清哪些是自己改的；而且本仓库是公开的，拷入 GPL-3.0 代码后整个仓库要按 GPL 发布。

健壮性约定：

1. **单实例**：同一 QQ 号只允许一个后端在线；启动脚本先确认旧后端已停止。
2. **版本锁定**：MaiBot 版本写在 `pyproject.toml` 并由 `uv.lock` 锁定，每次升级都留在 Git 历史里；按 P4 的流程升级，随时能回到上一版本。
3. **自动恢复**：进程由 systemd 拉起；NapCat 掉线由现有计划任务处理；连接器自带心跳与重连。
4. **最小权限**：连接器访问策略默认不阅读，只放行名单内的群和人；禁言、撤回、主动私聊等工具保持关闭；WebUI 只监听本机。
5. **数据**：实例目录放在仓库外，每日备份并定期演练恢复；真实聊天和密钥不进 Git。
6. **可观察**：用 WebUI 的推理回放和用量统计看每次回复；健康状态继续写入 `runtime-health.json`。
7. **自有代码要薄**：插件只做参数转换和结果整理，业务规则全部放在 `doraymon_core`。

## 5. 阶段计划

完成等级统一写为：**离线**（单元测试）→ **回放**（真实模型 + 假 QQ）→ **QQ 往返**（测试群或私聊真实收发）→ **持续运行**（多日无人值守）。汇报进度时写明到达哪一级。

P0、P1 依次执行；P2 与 P3 可以并行；P2、P3 都通过后进入 P4。

### P0 冻结与准备（约半天）

- **目标**：保住现有成果，建好新路线的工作区，不影响在线服务。
- **需要你**：确认 D1～D4。
- **任务**：
  1. 旧版继续在线作为回退，只修影响使用的故障，不再加功能。
  2. slim 的 DS-01、CHAT-01 提交到 `refactor/slim-onebot` 留档（D11），随后冻结；其中的用餐、高德代码和测试作为 P3 的移植来源。
  3. 在本仓库开新分支和工作树（建议 `route/maibot`），按第 4 节的布局建立骨架；把本文件、STATUS、AGENTS、architecture、runbook 迁入并改写入口。
  4. 记录基线：旧服务提交号、NapCat 版本、名单与配置字段名（只记字段名，不记值）。
- **验收**：旧服务健康状态正常；新工作树干净；STATUS 的当前任务指向 P1。
- **回退**：没有运行变更，删除新分支即可。

### P1 MaiBot 影子试运行（1～2 天）

- **目标**：MaiBot 正式版小范围接通 QQ，先用默认能力验证收发和稳定性。
- **需要你**：按 D2 准备试运行账号；在 MaiBot 本地配置中填写 DeepSeek key；需要时扫码登录。
- **任务**：
  1. 安装：在新工作树执行 `uv add "maibot==1.3.15"`（写入 `pyproject.toml` 并更新 `uv.lock`），之后用 `uv sync --locked` 复现安装；Python 用官方安装示例的 3.13，至少 2GB 可用内存。用 `uv run maibot init --directory ~/.local/share/doraymon/maibot` 创建实例目录，之后用 `maibot run --directory` 启动。
  2. 基础配置：`[bot]` 填 QQ 号，`nickname = "DoraYmon"`，`alias_names` 沿用“哆啦A梦、哆啦、小哆”；`[telemetry] enable = false`（D8）；`replyer`、`planner`、`utils` 三个必配任务先都用 `deepseek-flash`（DeepSeek 已把旧名 `deepseek-v4-flash` 指向新版 Flash，旧名仍可用）。
  3. 访问范围：`config/adapter_policy.toml` 打开“默认不阅读”，只放行你的私聊和一个测试群；连接器 `[chat_abilities]` 全部保持关闭。
  4. 连接：统一 QQ 连接器连到试运行账号所在 NapCat 的正向 WebSocket，地址取法与现有 `start-client.ps1` 一致（Windows 侧 WSL 网卡的 IPv4）。若 D2 选主号，本阶段同时完成 P4 第 1 项的脚本改造。
  5. 配置模板进库：`deploy/maibot/` 保存去掉密钥的 `bot_config`、`model_config`、`adapter_policy` 模板，再写一个“本地配置与模板差异”检查脚本。
- **验收（QQ 往返）**：测试群里 @ 必回；私聊多轮能衔接；连续几条补充合并成一次回复；“没问你”之后不再插话；重启服务或断网恢复后自动重连；24 小时无崩溃；WebUI 能看到每次回复的调用次数和 token。
- **回退**：停止 MaiBot；旧服务一直未受影响（选主号时恢复旧服务和旧脚本即可），两边数据互不影响。

### P2 自然度调优与评测（3～7 天，可与 P3 并行）

- **目标**：在测试群里像个群友；每次调整都有可重复的对比证据。
- **需要你**：开启记忆和向量表达前提供嵌入模型的 key（DeepSeek 不提供嵌入模型）；确认 D3；每轮调整后盲评约 20 条回复。
- **任务**：
  1. 人设移植：把旧仓库 `resources/persona/default.md` 的核心（热心靠谱的伙伴，有哆啦A梦式的亲切感，但不冒充哆啦A梦本人）写进 `[personality]` 的 `personality`、`behavior_style`、`reply_style`。旧提示里的能力声明交给 MaiBot 的工具机制处理，不整段搬运。
  2. 开口节奏：调整 `[chat.reply_timing]` 的 `message_debounce_mode`（先用 `natural`）、`talk_value`、`reply_trigger_mode`、`inevitable_at_reply`；不同群用 `talk_value_rules` 分别设置。
  3. 回复形态：调整 `[response_splitter]`、`typing_speed`、`[emoji]` 和引用频率；`[chinese_typo]`（故意打错字）是否保留由盲评决定。
  4. 学习与记忆：按 D3 设置 `[expression]`、`[jargon]` 的 `learning_list`；配好嵌入模型后开启 `[a_memorix]`；每周在 WebUI 的记忆管理页看一次学到了什么。
  5. 模型分配：`planner`、`utils` 用 flash；`replyer` 对比 flash 与 pro。一次只改一个变量，记录每轮调用次数和费用。
  6. 评测：
     - 场景：旧仓库 `tests/fixtures/group_dialogues.json` 的 16 组开发场景和 8 组保留场景（全部为合成内容），补上发言人和时间轴；
     - 回放台（推荐）：一个最小的假 NapCat，作为 OneBot v11 正向 WebSocket 服务端按时间推送场景消息，记录 MaiBot 的回复和耗时；不碰真实 QQ，可以反复运行，P4 的升级检查也用它；
     - 指标：明确该答却漏答的次数、不该答却插话的次数、回复长度的中位数和 P90、气泡数、首条回复耗时、每轮调用次数与 token；
     - 盲评：以“MaiBot 默认配置 + DoraYmon 人设”的首轮输出为基线，之后每次调整都抽样，由你判断像群友还是像助手、回复对象对不对、长短合不合适。
- **验收（回放 + QQ 往返）**：保留组漏答 0 次，“没问你”之后再插话 0 次，事实类回答没有编造；盲评中“像群友”的比例明显高于基线；费用在 D4 预算内。参考群的长度分布只作方向参考，不作硬指标。
- **回退**：配置模板有 Git 历史，可以恢复到任一版本。

### P3 用餐能力插件化（3～5 天，可与 P2 并行）

- **目标**：“吃什么、换一个、15 元以内、再近点、换到某地附近、地图”在 MaiBot 里自然可用；店名、地址、距离只来自高德结果。
- **需要你**：把已有的高德 key 填进插件本地配置。
- **任务**：
  1. 从 slim 提取 `doraymon_core`：`meals.py` 的地点、预算、半径、排除与结果整理，`clients.py` 的高德查询，`models.py` 的数据模型，连同对应测试；配置改为显式参数，不依赖 NoneBot 或 MaiBot。
  2. 编写 `doraymon_meal` 插件：提供一个 `@Tool("nearby_food")`，参数为地点、预算、半径和是否换一家；返回店名、地址、直线距离、地图链接和缺项说明；用餐状态按聊天流和成员保存，30 分钟过期。
  3. 口味、签到命令和知识检索暂不迁移：先试用 MaiBot 的人物记忆和知识功能，结合 P2 的盲评判断是否有缺口，确有需要再用 `@Command` 或 Tool 补上。
- **验收（离线 + QQ 往返）**：`doraymon_core` 离线测试全部通过；测试群完成上述多轮对话；回复里的店名和距离逐条对得上工具返回；高德失败或地点有歧义时明确说明。
- **回退**：在 WebUI 单独禁用插件，聊天不受影响。

### P4 切换与运维加固（1 天 + 7 天观察）

- **目标**：MaiBot 成为主号唯一后端，具备自动恢复、备份、升级和回退流程。
- **需要你**：确认切换时间；是否扩大名单由你决定。
- **任务**：
  1. 接入现有运行脚本：写一个 MaiBot 启动包装，读取 `integration.json` 的 `wsUrl` 和 token，写入连接器配置后再启动；`start-client.ps1` 的重载目标从 `doraymon-qq.service` 改为 `maibot.service`；健康检查加入 MaiBot 状态。
  2. 切换：停止旧后端并取消自启，MaiBot 连到主号 NapCat；名单先沿用旧版的允许范围。
  3. 备份：每天低峰时停服数秒，打包整个实例目录，保留 7 份日备份和 4 份周备份；升级前强制备份；每月演练一次恢复。
  4. 升级流程：读 changelog → 备份实例目录 → 修改 `pyproject.toml` 中的版本并执行 `uv lock` → 回放台跑保留组 → 测试群观察 → 主号生效并提交。回退时恢复上一版锁文件，同时恢复升级前的实例备份（新版可能已迁移数据库，官方不保证旧版能读）。不追每日新版（D12）。
  5. runbook 写清日常操作、排障、回退三部分。
- **验收（持续运行）**：连续 7 天除 QQ 重新登录外无需人工处理；完成一次演练，10 分钟内回到上一版本；费用在预算内。
- **回退**：旧版保留两周，随时可以启用，之后归档。

### P5 按需扩展（每项单独立项）

| 扩展 | 进入条件 | 做法 |
|---|---|---|
| AstrBot 辅助 | 出现 MaiBot 插件和 MCP 都满足不了、而 AstrBot 已有成熟实现的具体需求 | MaiBot 的 Tool 插件调用 AstrBot 的 `POST /api/v1/chat`（v4.18.0 起提供），设置超时、按用户隔离、失败时退回；AstrBot 不接 QQ |
| 真实菜单与价格 | 学校或商圈有可用的菜单来源 | 参考旧版菜单目录导入，标注来源和更新时间 |
| 联网、看图、语音 | 有实际使用需求 | 配置 MaiBot 自带的 `web_search`、`vlm`、`voice` 模型任务 |
| 主动消息 | 需要提醒或问候 | 插件调用 `maisaka.proactive.trigger`，措辞由 MaiBot 决定 |
| 统一管理页 | 需要一个面板看全部服务 | 写 MaiBot WebUI 首页卡片插件，不改造 Neo 启动器 |
| 公开试用 | P4 通过，限流、费用上限和管理员关闭都可用 | 开放范围由你决定 |

## 6. 决策登记

需要你拍板：

| 编号 | 决策 | 选项 | 推荐 | 状态 |
|---|---|---|---|---|
| D1 | 路线 | A 自研 / B MaiBot 主体 / C AstrBot 主体 / D 双主体 | B | 待确认 |
| D2 | 试运行账号 | 独立测试号 + 第二个 NapCat / 主号 + 严格名单，试运行期间旧服务停用 | 测试号：旧服务全程不受影响，P4 才切主号。没有测试号时用主号 | 待确认 |
| D3 | 记忆与学习范围 | 只在测试群和你的私聊开启 / 全部开启 / 全部关闭 | 先只开测试范围，P2 结束时再决定是否扩大；全部关闭会明显削弱自然度 | 待确认（取代旧 D2） |
| D4 | 模型月度预算 | 由你给一个上限 | 超出时先降低 `talk_value`，再换模型 | 待确认 |

按推荐默认执行，随时可改：

| 编号 | 决策 | 默认 | 备选 |
|---|---|---|---|
| D5 | AstrBot 何时接入 | P5，满足进入条件后 | 首期接入 |
| D6 | 启动与管理 | MaiBot WebUI + 现有运行脚本 | 改造 Neo 启动器；Windows 一键包 |
| D7 | 部署位置 | WSL：复用 systemd、地址同步和开发工具 | Windows 一键包：与 NapCat 同机、有图形界面，但要放弃现有常驻方案 |
| D8 | 遥测 | 关闭（不影响功能） | 保持默认开启 |
| D9 | 参考群聊记录 | 只用统计作评测参考 | 导入表达学习（会把真实群友的说法写进数据库，需要你明确同意） |
| D10 | 电脑桥接让行 | QQ 账号不再承担，电脑控制只走官方 Bot | 写一个读取租约的 MaiBot 拦截插件 |
| D11 | slim 未提交改动 | 提交到 `refactor/slim-onebot` 留档（执行前再确认） | 只保留工作树外的快照 |
| D12 | 升级节奏 | 锁定版本，每月评估一次或遇到需要的修复时升级 | 跟随最新版 |

改变路线或新增限制前，先在本表登记来源和理由，再改其他文档。

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| MaiBot 迭代快，插件 SDK 经常变化 | 锁文件固定版本；插件层保持很薄；升级前读 changelog 并跑回放台 |
| 模型费用：一次回复 3～6 次调用 | `planner`、`utils` 用 flash；用 `talk_value` 控制频率；每周看 WebUI 用量统计 |
| 隐私：群聊会存入本地库并用于学习和记忆 | D3 限定范围；关闭遥测；数据目录不进 Git；备份留在本机 |
| 同一 QQ 号两个后端重复回复 | 切换脚本先停旧服务；启动包装检查旧服务状态 |
| 内存：Windows 主机曾因内存不足严重换页 | MaiBot 跑在 WSL（当前上限约 7.6GB）；避免和 Android 构建同时运行；关注 MaiBot 的内存诊断日志 |
| 许可证：MaiBot 为 GPL-3.0，插件 SDK 为 LGPL-3.0 | 自用没有影响；将来公开分发修改版时遵守 GPL |
| 路线和文档再次漂移 | 规划入口只有本文件和 STATUS；改路线先改决策登记 |

## 8. 协作方式

- 入口顺序：`AGENTS.md` → `STATUS.md`（当前阶段与任务）→ 本文件（阶段定义与决策）。
- 每个任务在 STATUS 写清目标、文件范围、验收和不做的事；执行者完成后填写实际命令和结果，并标明到达的完成等级。
- 分工：规划和验收由 Codex 或 Claude 负责；边界清楚的小任务可以交给 DeepSeek，按 STATUS 中 PLANNED 的任务执行。
- 文档只保留五份：README（使用）、STATUS（状态）、本文件（路线与决策）、architecture（结构）、runbook（运维）。不新增报告类文件，历史交给 Git。
- 每个阶段结束时更新本文件的阶段状态；P0 完成后，本文件随项目迁入新工作树。

## 9. 参考（核对日期 2026-10-24）

- MaiBot：<https://github.com/Mai-with-u/MaiBot>；文档：<https://docs.mai-mai.org>（配置见 `manual/configuration/bot-config`、`model-config`，安装版与实例目录见 `manual/deployment/uv-tool`，QQ 连接器见 `manual/adapters/qq-local-client`，备份见 `faq/backup-and-migration`）
- MaiBot 插件 SDK：<https://github.com/Mai-with-u/maibot-plugin-sdk/blob/main/docs/guide.md>
- AstrBot HTTP API：<https://docs.astrbot.app/dev/openapi.html>
- Chat Plus：<https://github.com/Him666233/astrbot_plugin_group_chat_plus>
- Neo-MoFox 启动器：<https://github.com/MoFox-Studio/Neo-MoFox-Launcher>
- NapCat：<https://github.com/NapNeko/NapCatQQ>
