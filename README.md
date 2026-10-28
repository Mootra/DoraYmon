# DoraYmon

DoraYmon 是一个插件化的 Python AI 助手，通过官方 QQ Bot（`botpy`）或 QQ 账号（OneBot/NapCat）接收消息，飞书通道正在接入。业务共用 DeepSeek、默认开启且按会话隔离的可控短期上下文，以及带来源引用和权限过滤的本地知识库 RAG。

当前 RAG 是 SQLite FTS5/BM25 关键词检索基线，不包含 Embedding、向量检索、长期个人记忆或 Agent。所有检索和权限行为都可以离线测试，再根据评测决定是否增加复杂组件。

## 项目结构

```text
DoraYmon/
├── main.py
├── doraymon/
├── plugins/
├── services/
├── storage/
├── data/
├── skills/
├── resources/
├── logs/
├── scripts/
└── docs/
```

主要分层：

- `main.py` 负责启动。
- `doraymon/` 放客户端、路由、配置和日志。
- `plugins/` 放具体命令。
- `services/` 放外部服务调用。
- `storage/` 放 SQLite 连接和数据读写。
- `docs/` 放架构、部署和功能扩展说明。
- `resources/knowledge/` 放管理员整理的 UTF-8 Markdown/TXT 知识。

## 当前功能

- QQ Bot 长连接启动
- 命令路由
- 插件式命令
- DeepSeek 对话：显式 `/chat`、私聊普通文本；QQ 账号群内选择性接话、@/引用与连续追问
- 统一对话编排：完整问答轮次、上下文字符预算、旧轮次后台摘要（失败时本地节选），以及开启 RAG 后的可选知识增强
- 可选短期上下文：默认开启，支持闲置过期、明确换话题、防跨会话串话、查询摘要和按当前会话清空；后台摘要默认保留 7 天
- 规则式食物意图识别（不是 AI 分类模型）
- SQLite FTS5/BM25 本地知识问答、拒答规则和来源引用
- 公共、群、私人知识库读取权限隔离
- SQLite 签到
- 控制台日志和 `logs/doraymon.log`
- 本地运行脚本
- 宝塔部署文档
- GitHub 初始化文档

QQ 账号聊天、附近真实用餐选择及数据接入的开发批次、验收标准和登录安排见 [QQ 账号聊天与附近用餐推荐开发计划](docs/qq_account_chat_plan.md)。

## 学习和协作

协作规则见 [AGENTS.md](AGENTS.md)，文档与各 agent 共用入口见 [文档导航](docs/README.md)。本机当前任务、完成记录和归档分别维护在 `docs/private/STATUS.md`、`completed.md` 和 `archive/`；`docs/private/` 由 Git 忽略。实施任务按依赖完成连贯批次，明确学习任务才保留用户练习步骤。

当前架构、函数职责和工程缺口的检查见 [2026-10-23 项目审查](docs/project_review_20261023.md)，逐项列有位置、证据、建议形式和修复状态。

RAG 评测、Embedding 和混合检索的后续路线见 [docs/learning_outline.md](docs/learning_outline.md)。

当前仓库还提供一套与正式知识库隔离的固定离线检索评测：

```powershell
.\.venv\Scripts\python.exe scripts\eval_rag.py
```

评测会报告 Recall@K、MRR、无答案空召回准确率和权限泄漏数量，并使用临时数据库，不覆盖运行中的知识索引。样例和说明见 [evals/README.md](evals/README.md)。

## 验证与验收

想知道当前到底做到了什么，先跑一条命令：

```bash
.venv/bin/python -m scripts.preflight_offline
```

它会运行离线单元测试、回放场景和 RAG 门槛，并**逐条说明每套门槛证明什么、不证明什么**。

已选检查失败、超时、无法执行或未完成时退出码为 1。`--quick` 只主动排除回放和 RAG，不能代替完整验收。[离线 CI](.github/workflows/offline.yml) 在 Python 3.10/3.14 上执行同一入口；本机验证与远端 CI 结果分别记录。

QQ 账号入口已有有限私聊与真实 POI 查询的历史验收记录；当前版本的完整用餐多轮、群聊和持续运行仍需分别验收。飞书真实事件源尚未接通，不能作为已上线通道。平台验收步骤、现象对照与失败排查见
[`docs/acceptance_guide.md`](docs/acceptance_guide.md)。

部署相关的配置与运维说明见 [`docs/deployment.md`](docs/deployment.md)，
"真实 QQ 可用"的开发计划见 [`docs/qq_account_chat_plan.md`](docs/qq_account_chat_plan.md)。

### 三个入口

| 入口 | 启动方式 | 前提 |
|---|---|---|
| 官方机器人 | `.venv/bin/python main.py` | AppID/Secret、审核上线、正式环境需服务器 IP 在白名单 |
| QQ 账号（OneBot/NapCat） | `.venv/bin/python -m scripts.run_account` | `BOT_QQ_ACCOUNT_ENABLED=true`、账号和 OneBot；默认普通聊天，可配置群范围 |
| 飞书（接入中） | `.venv/bin/python -m scripts.run_feishu` | SDK 事件源待实现；应用配置与允许列表尚不能代替真实接通 |

接真实账号或正式环境之前，先跑对应的自检：

```bash
.venv/bin/python -m scripts.preflight_official_bot --check        # 官方凭证
.venv/bin/python -m scripts.verify_account_channel --port 3001    # 账号通道
```

## 从零搭建

### 第一步：在 QQ 开放平台创建机器人

1. 打开 [QQ 开放平台](https://q.qq.com/)，使用手机 QQ 扫码注册或登录。
2. 进入机器人管理页面，按照页面引导创建一个 QQ 机器人。
3. 在机器人的开发设置中复制 `AppID` 和 `AppSecret`。
4. 根据开放平台当前页面配置测试范围、测试成员或群聊，并确认机器人处于可测试状态。

> `AppSecret` 是敏感凭证，离开页面后通常不能再次明文查看，只能重置。不要把它发到群聊、截图、日志或提交到 GitHub。

机器人创建后可能会立即出现在 QQ 消息列表中。在 DoraYmon 服务启动前，向它发送消息出现“机器人去火星了”之类的离线提示属于正常现象。

### 第二步：准备代码和 Python

需要 Python 3.10 或更高版本。首次使用先克隆仓库并进入项目目录：

```powershell
git clone https://github.com/Mootra/DoraYmon.git
cd DoraYmon
python --version
```

如果已经下载了项目，只需进入包含 `main.py` 和 `requirements.txt` 的目录。

### 第三步：创建虚拟环境并安装依赖

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Linux/macOS：

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

### 第四步：填写 QQ Bot 配置

打开刚创建的 `.env`。Windows 可以使用：

```powershell
notepad .env
```

Linux/macOS 可以使用：

```bash
nano .env
```

先填写从 QQ 开放平台取得的两个必填项：

```bash
QQBOT_APPID=你的 AppID
QQBOT_SECRET=你的 AppSecret
QQBOT_SANDBOX=true
```

- 在开放平台沙箱或测试环境调试时使用 `QQBOT_SANDBOX=true`。
- 切换到开放平台正式环境后改为 `QQBOT_SANDBOX=false`。
- `.env` 已被 Git 忽略，不要把真实凭证写进 `.env.example` 或 `config.example.yaml`。

`DEEPSEEK_API_KEY` 不是连接 QQ 的必填项；不配置时 `/ping`、`/help`、签到等本地命令仍可使用，但 AI 对话和 RAG 生成回答不可用。

### 第五步：启动 DoraYmon

Windows：

```powershell
.\.venv\Scripts\python.exe main.py
```

也可以双击 `start.bat`，或在 PowerShell 中运行 `scripts/run_local.ps1`。启动脚本会自动创建虚拟环境、安装或更新依赖，并在缺少 `.env` 时复制示例配置。

Linux/macOS：

```bash
./.venv/bin/python main.py
```

看到类似下面的日志表示 QQ 长连接已经建立：

```text
DoraYmon 已登录：机器人名称
```

如果启动时提示 QQBot 配置缺失，请检查 `.env` 中的 `QQBOT_APPID` 和 `QQBOT_SECRET`，以及等号后是否混入空格或引号。

### 第六步：在 QQ 中验证

1. 打开 QQ，找到刚创建的机器人。
2. 私聊发送 `/ping`，预期返回 `pong`。
3. 发送 `/status` 查看运行模式，再发送 `/help` 查看命令列表。
4. 群聊测试时先把机器人加入开放平台允许的测试群，并在消息中 @ 机器人；未 @ 且不是显式命令的群消息默认不会触发回复。

如果机器人没有回复，按顺序检查：

1. 先跑 `.venv/bin/python -m scripts.preflight_official_bot --check`：它会读出开放平台返回的**真实错误码**（`100016` 凭证不对、`100007` AppID 无效或机器人被封禁、`10004` 该 AppID 下没有机器人），并提醒 IP 白名单只作用于正式环境。比翻日志快得多。
2. 本地进程是否仍在运行。
3. `QQBOT_SANDBOX` 是否与开放平台环境一致；沙箱只收到沙箱里配置的群/单聊事件。
4. 当前 QQ 用户或群是否在测试范围内。
5. `logs/doraymon.log` 中的错误。

### 第七步：开启 DeepSeek 对话和短期上下文（可选）

在 `.env` 中填写：

```bash
DEEPSEEK_API_KEY=你的 DeepSeek API Key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
```

重启后先发送 `/chat 你好`。短期上下文默认已开启；如需单轮请求，可显式关闭：

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

### 第八步：建立本地知识库（可选）

1. 把 UTF-8 Markdown/TXT 放入 `resources/knowledge/`。
2. Windows 运行 `.\.venv\Scripts\python.exe scripts\index_knowledge.py`；Linux/macOS 运行 `./.venv/bin/python scripts/index_knowledge.py`。
3. 在 `.env` 中设置 `BOT_ENABLE_RAG=true`。
4. 重启机器人，通过 `/知识库状态` 和 `/知识问 如何在本地启动项目` 验证。

## 配置参考

最小 QQ 连接配置：

```bash
QQBOT_APPID=
QQBOT_SECRET=
QQBOT_SANDBOX=true
```

完整配置项：

```bash
QQBOT_APPID=
QQBOT_SECRET=
QQBOT_SANDBOX=true
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_TEMPERATURE=0.7
BOT_COMMAND_PREFIX=/
BOT_DEFAULT_LOCATION=江西师范大学瑶湖校区
BOT_ADMIN_OPENIDS=
BOT_ENABLE_FOOD_NATURAL_TRIGGER=true
BOT_ENABLE_CHAT_HISTORY=true
BOT_CHAT_HISTORY_LIMIT=40
BOT_CHAT_HISTORY_MAX_CONTENT_LENGTH=4000
BOT_CHAT_CONTEXT_MAX_CHARS=6000
BOT_CHAT_CONTEXT_SUMMARY_MAX_CHARS=1200
BOT_CHAT_CONTEXT_TTL_MINUTES=720
BOT_CHAT_SUMMARY_RETENTION_DAYS=7
BOT_REPLY_MAX_SEGMENTS=3
BOT_REPLY_MAX_TOTAL_CHARS=4000
BOT_ENABLE_RAG=false
BOT_KNOWLEDGE_DIR=resources/knowledge
BOT_RAG_TOP_K=3
BOT_RAG_TOKENIZER=trigram
BOT_RAG_MAX_CONTEXT_CHARS=6000
BOT_RAG_CHUNK_MAX_CHARS=800
BOT_RAG_CHUNK_OVERLAP_CHARS=100
LOG_LEVEL=INFO
DATA_DIR=data
LOG_DIR=logs
```

`.env` 的优先级高于 `config.yaml`。仓库只提供 `config.example.yaml`，真实配置需要在本地创建。

涉及本地或附近的问题默认按 `BOT_DEFAULT_LOCATION` 理解；默认值为江西师范大学瑶湖校区。附近餐饮先使用用户明确说出的地点，再沿用当前用餐会话的地点，最后使用默认地点。清空上下文会清除当前用餐地点。

查询真实附近店铺需配置 `BOT_NEARBY_PROVIDER=amap` 和 `BOT_NEARBY_PROVIDER_KEY`。Key 的服务平台选择 [Web 服务](https://lbs.amap.com/api/webservice/create-project-and-key)，当前调用地理编码与周边搜索。真实 Key 只放本地 `.env`。未配置时明确说明地图未接通；地图结果提供店名、地址、直线距离和地图链接，不能代替菜单、现价或营业状态。普通“今晚吃什么”仍可使用规则库选菜。

## DeepSeek

`/chat` 命令会请求：

```text
POST https://api.deepseek.com/chat/completions
```

默认模型：

```bash
DEEPSEEK_MODEL=deepseek-v4-flash
```

需要更强模型时，可以改为：

```bash
DEEPSEEK_MODEL=deepseek-v4-pro
```

短期上下文默认开启；如果关闭，则每次对话都是单轮请求。开启后，chat 插件只读取当前私聊用户或当前“群 + 用户”会话最近的有限消息：

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

未配置 `DEEPSEEK_API_KEY` 时，进入 AI 聊天的消息会返回配置缺失提示。

开启后，历史按完整的“用户问题 + 助手回答”轮次进入模型，并受 `BOT_CHAT_CONTEXT_MAX_CHARS` 总字符预算约束。超出预算时，较早完整轮次保留在短期摘要中，后台优先请求低温度模型压缩，失败时退回本地受限节选；摘要不会阻塞当前回复，不是长期记忆，并按 `BOT_CHAT_SUMMARY_RETENTION_DAYS` 过期。普通聊天启用 RAG 时只注入相关度足够高的前 1～2 条资料；严格 `/知识问` 仍按知识库规则拒答并附来源。

`BOT_CHAT_CONTEXT_TTL_MINUTES` 按当前会话最后活跃时间控制闲置过期，设置为 `0` 表示不自动过期。以“换个话题”“忽略之前的对话”“重新开始”等明确表达开头时，只清空当前会话旧历史并从新问题开始，普通的“另外……”不会自动清空。

## 初始命令

```text
/help
/ping
/status
/chat 你好
/清空上下文
/上下文状态
/上下文摘要
/知识问 如何在本地启动项目
/知识来源 如何在本地启动项目
/知识库状态
/重建知识库
/天气 南昌
/今日运势
/签到
/我的签到
/吃什么 今天很累
/记住口味 我喜欢辣
/我的口味
/忘记口味 我喜欢辣
/todo
/admin status
```

私聊可以直接发送普通文本。明确的用餐决策表达会优先进入食物助手，其他文本回退到 AI 聊天。官方 QQ Bot 的群自然消息仍需 @。

QQ 账号入口默认使用 `BOT_QQ_ACCOUNT_ACCESS_MODE=chat`：接收普通私聊，群成员不受好友名单过滤。`BOT_QQ_ACCOUNT_GROUPS` 有值时限定群范围，留空处理账号所在群。需要封闭验收时设为 `allowlist`，恢复指定好友/群限制且名单不可同时为空。管理员和知识资料权限仍各自校验。

QQ 群普通消息按内容选择性接话：明确求助、知识提问和用餐请求可直接说；@、引用机器人、叫名字和显式命令也能触发。回复后同一人的 120 秒内追问可继续，@ 或引用其他成员时让开，“没问你”“不用回复了”只结束当前成员分支。普通参与默认每群冷却 30 秒、每分钟最多 2 轮；直接请求和连续追问独立限频，默认每分钟最多 8 段，无冷却。旧配置中的非零 `BOT_QQ_ACCOUNT_GROUP_COOLDOWN_SECONDS` 仍会限制直接请求，需改为 `0` 才使用新默认节奏。

`BOT_QQ_ACCOUNT_GROUP_PARTICIPATION_ENABLED=false` 可关闭普通群接话，保留直接请求和连续追问；称呼可用 `BOT_QQ_ACCOUNT_BOT_NAMES` 配置。群公共窗口只在内存保留至多 24 条、约 6000 字符、5 分钟，按平台/账号/群隔离；个人问答历史仍按群和成员隔离。短期上下文不会自动成为知识库或长期记忆。行为示例与离线验收见 [QQ 群聊交互](docs/qq_group_interaction.md)。

## 本地知识库 RAG

管理员把 Markdown/TXT 放入 `resources/knowledge/`，然后离线建立索引：

```powershell
.\.venv\Scripts\python.exe scripts\index_knowledge.py
```

开启问答：

```bash
BOT_ENABLE_RAG=true
```

资料作用域按目录区分：

```text
resources/knowledge/*.md                         公共知识
resources/knowledge/groups/<group_openid>/*.md  对应群知识
resources/knowledge/users/<user_openid>/*.md    对应用户私人知识
```

`/知识问` 只把当前用户有权访问的 Top-K 知识块交给 DeepSeek；无结果时直接拒答，不调用模型。`/知识来源` 可单独检查检索结果，`/知识库状态` 显示文档数、分块数、更新时间和 tokenizer。`/重建知识库` 仅管理员可用。

开启 `BOT_ENABLE_RAG=true` 后，普通 `/chat` 和自然聊天也会尝试检索有权限访问的知识。检索没有结果或索引暂时不可用时会继续普通对话；短追问会结合上一轮用户问题生成检索查询。只有回答实际引用 `[编号]` 时才附上对应资料来源。`/知识问` 仍保留为严格的“只能依据知识库回答”入口。

默认 `trigram` tokenizer 用于验证中文子串召回。两字查询以及 trigram 完全无结果的长查询会使用受控 `LIKE` 回退：长查询必须同时命中至少两个不同短词，并只保留短词命中数最高的结果。当前版本还不是向量 RAG，后续是否加入 Embedding 和混合检索由固定评测集决定。

## 宝塔部署

上传项目到：

```bash
/www/wwwroot/DoraYmon
```

执行：

```bash
cd /www/wwwroot/DoraYmon
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
python main.py
```

宝塔进程守护管理器配置：

```text
名称：DoraYmon
运行目录：/www/wwwroot/DoraYmon
启动命令：/www/wwwroot/DoraYmon/venv/bin/python /www/wwwroot/DoraYmon/main.py
```

当前使用 `botpy` 长连接，不需要 Nginx 反向代理和域名。改成 Webhook 模式时，再配置域名和 HTTPS。

## GitHub 上传

```bash
git init
git add .
git commit -m "init DoraYmon qqbot framework"
git branch -M main
git remote add origin <你的仓库地址>
git push -u origin main
```

提交前执行：

```bash
git status
```

不要提交 `.env`、`config.yaml`、数据库、日志、真实 API Key、QQBot 密钥、真实聊天记录和私有 skill。

## 新增插件

1. 在 `plugins/` 新建文件。
2. 写一个接收 `BotContext` 并返回字符串的 `handle` 函数。
3. 在 `doraymon/router.py` 的 `COMMANDS` 注册命令。
4. 数据库逻辑放 `storage/`，外部 API 放 `services/`。

## 安全注意事项

- 不实现任意 Linux 命令执行。
- 不提供读取 `.env`、`config.yaml`、密钥文件的 QQ 命令。
- 管理员命令必须校验 `BOT_ADMIN_OPENIDS`。
- 返回到 QQ 群的错误信息保持简短，不暴露堆栈和密钥。
- `.env`、`config.yaml`、数据库、日志和私有技能不上传 GitHub。

## 本地聊天与回答评测

不连接 QQ 的离线试聊：

```bash
.venv/bin/python scripts/chat_cli.py --fake --message 你好 --message 继续 --message /上下文状态
.venv/bin/python scripts/chat_cli.py --fake --group demo-group --rag
.venv/bin/python scripts/eval_chat.py
```

CLI 走完整路由，支持命令、私聊和群聊 @；群聊加 `--no-at` 可检查普通消息不回复。
默认数据目录为 `data/cli`，可用 `--data-dir` 改写。fake 模式在其下创建临时会话，退出后清理，不读取真实配置或既有数据库；本地历史在该会话内开启，`--rag` 只索引公开的合成评测资料。

CLI 必须明确选择 `--fake` 或 `--real`；后者加载本地模型配置，会调用 DeepSeek 并产生费用。
回答评测默认 fake，只有加 `--real` 才联网；每个场景使用独立临时库。
评测报告写入被 Git 忽略的 `evals/results/`，包含回复、Markdown/长度/套话检查及未实现功能的跳过原因。
基线阶段只记录风格检查；加 `--strict` 才将检查失败作为非零退出。
fake 只能验证流程，真实质量须人工评阅真实模型报告；没有变更前的真实基线时不能声称质量已改善。

按功能分批验证可使用 `scripts/eval_dialogue.py`：默认用例包含 100 组合成对话，
`evals/feature_iteration_cases.jsonl` 则提供食物/路由和聊天口吻两批，每批 20 轮。
`--max-turns 20` 设置每次上限，`--offset` 按组跳过，报告中的 `next_offset` 是下一批起点。
每批结束后评阅原文、修改一个相关问题，再用相同用例和新输出目录回归。
命令和人工评阅标准见 [evals/README.md](evals/README.md)。

普通聊天的公开默认提示词在 `resources/persona/default.md`，修改后需重新启动进程。
运行信息由 `services/prompt_builder.py` 补充，包括北京时间、私聊/群聊场景、实际历史开关和保留策略。
`/知识问` 仍使用独立的严格资料问答提示词；普通聊天默认使用 8192 的 `max_tokens` 和略高于配置值的温度，含可选资料时降至不高于 0.4 并使用 4096 的 `max_tokens`，严格知识问答也使用低温度和 4096 上限。模型名称仍沿用 `DEEPSEEK_MODEL`，不自动切换 pro。

开启短期上下文后，`/吃什么` 和 `/知识问` 的回复也会进入同一会话的有限历史，可用普通聊天继续追问。
`evals/plugin_followup_cases.jsonl` 提供20轮跨插件追问和隔离场景；运行方式同上述分批工具。
知识问答直接给答案和来源列表；无资料时简短拒答，模型接口失败时不把失败消息存成知识答案。
