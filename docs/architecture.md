# 架构

```text
NapCat → NoneBot2 / 官方 OneBot v11 适配器 → onebot.py
  → Policy（访问、群参与、电脑桥接租约）
  → Conversations（有界 FIFO、连续输入合并、控制中断）
  → 本地命令 / 用餐查询 / 模型与可选知识检索
  → 官方 API 返回发送消息 ID → Store 保存完整已投递轮次
```

## 维护边界

| 代码 | 唯一职责 |
|---|---|
| `config.py`、`models.py` | Pydantic 校验外部配置与业务数据 |
| `onebot.py`、`__main__.py` | 官方事件接入、生命周期、运行和离线索引入口 |
| `policy.py` | 允许范围、群参与、短期公共窗口、桥接让行 |
| `flow.py` | 同会话 FIFO、短输入合并、有界排队、取消和关闭 |
| `app.py` | 处理一个轮次并在投递确认后提交状态 |
| `commands.py` | 无模型的本地命令 |
| `clients.py` | 复用异步 HTTP 会话，核验模型/高德响应 |
| `meals.py` | 地点、预算、半径、排除候选和地图结果呈现 |
| `store.py`、`prompt.py` | SQLite 事务、按权限检索、有限上下文装配 |

## 依赖选择

`uv.lock` 锁定完整依赖树。当前直接运行依赖为 8 个，数量不作为质量目标。

| 依赖 | 当前锁定版本 | 承担的职责 |
|---|---|---|
| NoneBot2、官方 OneBot 适配器 | 2.5.0、2.4.6 | 协议、事件、API 应答、连接和重连 |
| Pydantic、pydantic-settings | 2.14.1、2.16.1 | 校验、序列化、dotenv 与环境配置 |
| aiohttp | 3.14.5 | 异步连接池；模型与地图共用业务 HTTP 会话 |
| aiosqlite | 0.22.1 | 在线程队列中执行 SQLite 操作 |
| cachetools | 7.2.4 | 有容量和有效期的去重、地图及临时会话缓存 |
| aiolimiter | 1.3.0 | 群内直接请求与普通参与各自的速率限制 |

选择 NoneBot 是为了复用协议实现，同时保留小型业务包。AstrBot 提供更多集成功能，但本项目当前不需要同时维护其控制台、Agent 和额外知识服务。新增需求先检查现有框架能力，再决定是否添加依赖。

## 聊天设计对照

| 项目 | 在本项目中的落点 | 验证入口 |
|---|---|---|
| [MaiBot](https://github.com/Mai-with-u/MaiBot) | 分开发言时机、回复对象与表达；点名优先，收尾后结束自动追问 | `tests/test_policy.py` |
| [AstrBot Chat Plus](https://github.com/Him666233/astrbot_plugin_group_chat_plus) | 接收首轮回复前同成员的补充，有限等待，保留消息边界和引用目标 | `tests/test_policy.py`、`tests/test_flow.py` |
| [AstrBot](https://github.com/AstrBotDevs/AstrBot) | 平台适配和业务分开，模型、地图与资料通过既有服务边界接入 | `tests/test_onebot.py`、`tests/test_app.py` |

这些项目用于行为和结构对照。本地合成回归验证本项目的规则；真实表达质量须另用固定模型与场景评阅。

## 必须保持的行为

- 访问检查在缓存群消息及调用外部服务之前；空封闭名单拒绝所有用户。`chat` 模式须显式设置，不能由空名单推导。
- 会话键包含平台、机器人、群和成员；群公开窗口只保存已获准的本群消息。群场景读取公共及本群资料，私聊读取公共及本人资料。
- 每会话至多 8 个未完成请求，全局至多 128 个活动会话；模型/地图并发默认 4。队列满明确返回忙碌，关闭会完成或取消所有等待者。
- 短输入按消息边界合并，默认安静间隔 0.35 秒、总等待 1.5 秒；命令、引用目标、接收对象和附件变化会结束合并。等待补充和等待速率额度不占模型并发槽。
- 群里已接收请求的同一成员可在首轮回复前继续补充；发给其他成员的消息仍让开。点名机器人可同时提及其他成员。未点名的“谢谢”“先这样”“晚安”等收尾释放自动追问，取消仍在生成的本成员分支，不追加确认消息；再次明确求助可重新参与。
- 清空、停止和静默通过控制任务取消当前分支；后续输入排在控制动作之后。已经发出的网络消息无法撤回，结果不明时不自动重发。
- 只有每段发送均返回有效消息 ID 后才保存完整轮次并激活群追问；失败、未知或部分投递均不写成已成功历史。进程在发送与提交之间退出仍存在结果不明窗口，不承诺恰好一次投递。
- 店铺/用餐选择在发送后提交；地点 provider 缓存只按地点和半径缓存查询结果，不保存个人条件。POI 不自动推导菜单价或营业状态。
- 历史与资料共用字符预算，保留最近的完整轮次，当前请求放最后；资料与群原话作为不可信数据传入。模型 API 和发送请求不因超时自动重试。

## 研究依据及其限度

| 研究 | 本项目采用的做法 | 不能据此声称 |
|---|---|---|
| [Lost in the Middle](https://arxiv.org/html/2307.03172v3)，TACL 2024 | 控制上下文长度、只保留近期完整轮次、按需注入少量资料；由 `test_prompt.py` 验证装配约束 | 论文中的模型与当前配置不同；尚无当前模型效果提升数据 |
| [LongMemEval](https://arxiv.org/html/2410.10813v2)，ICLR 2025 | 借用知识更新、时效和拒答这三类失败场景，测试方向更正、过期历史和无资料回答 | 这些是本地合成回归，不是跑过 LongMemEval，也不是长期记忆实现 |

FTS5/BM25 是当前原生检索基线：[SQLite 文档](https://www.sqlite.org/fts5.html)。权限在排序和 Top-K 之前过滤。引入向量、重排或记忆框架前，需用固定语料比较召回、拒答、权限泄漏、时延和新增运维成本；当前未运行模型质量评测。

接口依据：[OneBot 连接配置](https://onebot.adapters.nonebot.dev/docs/guide/setup/)、[NoneBot 驱动](https://nonebot.dev/docs/advanced/driver)、[Pydantic Settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)、[aiohttp 会话](https://docs.aiohttp.org/en/stable/client_quickstart.html)、[aiosqlite](https://aiosqlite.omnilib.dev/en/stable/)、[aiolimiter](https://aiolimiter.readthedocs.io/en/stable/)。已结合安装版本及真实框架事件测试核对，不依靠示例片段推断兼容。

## 能力迁移边界

| 能力 | 当前状态 |
|---|---|
| 普通聊天、清空、短期历史、群选择性参与 | 已实现，离线验证；真实平台待验 |
| 连续输入、方向更正、电脑桥接让行 | 已实现，合成回归 |
| 地图附近店铺及换店/预算/距离/地点/地图追问 | 已实现，provider 替身验证；菜单事实仍不足 |
| 按作用域的本地知识检索、严格知识问答入口 | 已实现，新固定样例验证；不沿用旧版 Recall/MRR 数字 |
| 口味和签到本地命令 | 已实现并通过离线验收；重复/上限区分、稳定排序、并发、北京时间日期和存储故障均有回归 |
| 菜单目录导入、回滚、合成目录、后台模型摘要、进程用量报表 | 未纳入当前运行链路；有明确需求和验收后再选取迁移 |
| 官方 Bot Agent 控制 | 由 Windows `computer/qq-agent-bridge` 独立接入 DSH / Codex，不进入 QQ 账号的聊天业务包 |
| 飞书和未实现的占位插件 | 当前 QQ 助手产品范围之外；历史实现由原分支和外部快照保留 |
| NapCat 与 Windows 电脑桥接常驻程序 | 原运行系统继续使用；不属于本业务包的部署变更 |

## 文档管理

README 维护使用入口，本文维护稳定设计，runbook 维护运维，STATUS 维护唯一当前任务。AGENTS 维护协作规则，CLAUDE 仅指向它。完成报告在 STATUS 原位更新；细碎日志不进入仓库。重要历史和回退依据保留在 Git 或工作树外快照，不复制到活跃文档目录。
