# AI 项目上下文

## 定位与阅读入口

DoraYmon 是插件化 Python QQ AI 助手，支持官方 QQ Bot（botpy）、QQ 账号（OneBot）和正在接入的飞书通道；业务共用路由、DeepSeek、可控短期上下文与 SQLite FTS5/BM25 本地知识库 RAG。意图识别为规则匹配；Embedding、向量检索、长期个人记忆和 Agent 尚未实现。

协作规则统一读取根目录 `AGENTS.md`；文档分工见 [导航](README.md)。本机存在 `docs/private/STATUS.md` 时，以它和实际代码核对当前任务；历史完成记录不重复写入多个 agent 的入口。架构细节见 [架构说明](architecture.md)，本次缺口见 [2026-10-23 审查](project_review_20261023.md)。

## 三个启动入口

| 入口 | 装配与发送 | 状态边界 |
|---|---|---|
| `main.py` | `doraymon/client.py`（botpy） | 官方 QQ 凭证、回复窗口和平台验收独立 |
| `scripts/run_account.py` | `doraymon/account_entry.py`、OneBotAdapter、OneBotWebSocketTransport、AccountSession | 入口默认关闭；启用后默认普通聊天，可配置群范围/封闭名单；运行目录独立 |
| `scripts/run_feishu.py` | `doraymon/feishu_entry.py`、FeishuAdapter、FeishuLongConnectionTransport、AccountSession | 默认关闭；`services/feishu_sdk.py` 的真实事件源仍未装配，尚无真实飞书往返 |

`doraymon/config.py` 读取 `.env`、环境变量与 `config.yaml`；进程环境优先于 YAML。`storage/db.py` 管理 SQLite 连接，离线工具用 `use_data_directory()` 指定临时库，避免读取真实配置。

## 消息处理链路

```text
平台入口 → 契约/适配与访问策略 → 客户端或 AccountSession
        → doraymon/router.py → plugins/ → services / storage
        → 文本与 PendingReply → 入口分段发送 → SendResult → 确认送达后提交历史
```

- `MessageEvent`、身份命名空间和 `BotContext` 是共用边界。平台动作、API 错误形状和引用作者解析留在适配器/传输；业务插件不直接调用平台发送 API。
- 路由优先处理显式命令，再处理食物意图和有效用餐追问，私聊普通文本回退到 chat；群聊需通过寻址规则。静默命令和管理员权限保留各自规则。
- QQ 账号/飞书按 `SessionKey` 顺序调度并限制并发与积压，清空优先取消旧任务。地点/模型/飞书同步网络调用在有容量上限的执行器运行；查询每轮状态独立。平台确认送达后才提交可见历史，失败与未知结果不盲重试。合成故障回归见 `tests/test_message_reliability.py`，真实平台验收仍须单独进行。

## 各层职责

| 模块 | 负责内容 |
|---|---|
| `ConversationService` | 所有普通聊天的上下文→提示词→模型→文本编排，兼容旧入口，返回 `is_error` |
| `context_builder` / `prompt_builder` / `reply_postprocess` | 完整轮次、字符预算、短追问检索、场景/北京时间、来源引用及纯文本处理 |
| `session_history` / `summary_service` | chat、food、knowledge 共用历史；过期、清空、摘要和生成版本校验 |
| `session_scheduler` / `pending_reply` | 会话顺序、背压、取消，以及投递后的历史提交 |
| `group_dialogue` | QQ 群有界公共窗口、当前回复对象、选择性参与、连续追问与局部结束；不改飞书默认策略 |
| `intent_service` / `meal_session` | 餐饮规则、追问条件与用餐状态 |
| `nearby_lookup` / `amap_*` | 地点解析、候选查询、来源、预算及缓存 |
| `food_recommend_service` / `nearby_meal_service` | 样例食物建议与有来源的店铺/餐点选择；两者的事实能力分别说明 |
| `rag_service` / `knowledge_store` | FTS5/BM25 分块检索、作用域过滤、知识预算、来源与严格拒答 |
| `storage` | SQLite 完整轮次、摘要、签到、明确口味和用餐反馈；不向消息出口暴露数据库或凭证 |
| `reply_delivery` / `send_result` | 分段、长度上限与实际投递结果；未知结果不能盲重试 |

公开默认人格为 `resources/persona/default.md`，导入时读取，修改后需重启。普通聊天知识注入与严格 `/知识问` 共用检索能力、保持不同拒答策略。知识来源编号避开历史中的已有引用；PromptBundle 的来源偏移由渲染和来源追加共同遵守。

## 已实现能力与限制

- 命令和自然聊天、有限短期上下文、过期/换话题/清空、后台摘要、来源追问。
- QQ 普通私聊不局限指定号码，群成员不受好友名单过滤；群普通求助按内容选择性接话，直接请求与普通参与分开限频。多人公共资料保留发言人和引用，个人问答与用餐条件继续隔离；24 组合成场景见 [交互说明](qq_group_interaction.md)，自然度和平台效果待真实评阅。
- `/吃什么` 样例建议、明确口味保存/查看/删除；配置地点服务后可查真实 POI，保留一个首推、按需地图及预算条件。
- 有来源的目录导入、餐点筛选、资料年龄提示和明确用餐反馈。POI 与地图人均不能证明菜单价、份量、口味或实时营业；完整餐点闭环仍待真实来源验收。
- `/知识问`、`/知识来源`、`/知识库状态`、管理员 `/重建知识库`，带公共/群/私人权限过滤。
- 签到、帮助、状态、管理员静默、发送记录与进程内模型用量统计。
- 天气、待办、钓鱼、宠物等为轻量功能或占位，按各插件实际实现说明。
- 飞书契约、适配、发送、传输接缝与入口有离线测试；事件源接通、平台真实收发和稳定运行分别待验收。

不实现无限聊天记录，不自动把聊天转成长记忆或知识库，不让群成员共享私人上下文。长期记忆的写入、可见性和撤回规则仍待 D2 确定。外部电脑控制桥接属于独立项目；本仓库只有可选入口分流，不能据此宣称已实现通用 Agent。

## 修改与测试

1. 先核对开发检出、Git 与未提交改动，再读当前任务相关模块；开发源是 WSL，Windows 为独立 QQ 沙箱。
2. 新插件接收 `BotContext` 并返回文本；模型、HTTP 和领域规则放 services，持久化放 storage，注册到路由并同步必要文档。
3. 离线测试使用 mock/fake 平台和模型、临时 SQLite 与合成数据；不读取 `.env`、`config.yaml`、真实日志、业务数据库、聊天内容或私有 skill。
4. 标准入口为 `.venv/bin/python -m scripts.preflight_offline`；聚焦测试按变更选择。真实模型评阅和真实平台验收需单独记录。
5. 错误文案保持简短，日志和回复不得暴露密钥、请求头、原始配置或堆栈。文档只描述已实现及实际完成的验证。
