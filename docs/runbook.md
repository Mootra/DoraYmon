# 配置与运维

## 本地运行

在仓库根目录运行 `uv sync --locked`，由用户将 `.env.example` 复制为 `.env` 并填写配置。运行命令为 `uv run python -m doraymon`。`ONEBOT_WS_URLS` 使用标准正向 WebSocket 地址；token 使用独立的 `ONEBOT_ACCESS_TOKEN`，不要附在 URL。

先配置原有封闭范围。允许用户、允许群和管理员为 JSON 字符串数组，例如 `["123456"]`。`DORAYMON_ACCESS_MODE=chat` 会接收普通私聊，群列表为空时覆盖账号所在群；切换之前应明确确认范围。

模型与地图使用不同密钥。仅配置 OneBot 时 `/ping` 等本地命令可用；模型或地图未配置/请求失败时明确返回状态，不产生真实结果的替代内容。默认模型请求名沿用 `deepseek-v4-flash`，须按实际服务提供方支持的名称配置。

全部业务设置见 `src/doraymon/config.py`。常用项：

| 环境变量 | 默认值 |
|---|---|
| `DORAYMON_DATABASE` | `data/runtime.sqlite3` |
| `DORAYMON_HISTORY_TURNS`、`DORAYMON_HISTORY_TTL_SECONDS` | 12、43200 |
| `DORAYMON_CONTEXT_CHARS`、`DORAYMON_REPLY_CHARS` | 6000、4000 |
| `DORAYMON_MODEL_CONCURRENCY`、`DORAYMON_SESSION_QUEUE` | 4、8 |
| `DORAYMON_MERGE_GAP`、`DORAYMON_MERGE_WINDOW` | 0.35、1.5 秒 |
| `DORAYMON_BRIDGE_LEASE` | 空；使用电脑桥接时填双方可读的有效租约路径 |

## 知识资料

每次只导入一种作用域的独立目录。命令校验全部文件后，在单一事务内替换该作用域索引。文件须为 UTF-8 Markdown/TXT、单文件不超过 200KB，拒绝越界符号链接。

```bash
uv run python -m doraymon index knowledge/public --scope public
uv run python -m doraymon index knowledge/alice --scope user:机器人号:用户号
uv run python -m doraymon index knowledge/team --scope group:机器人号:群号
```

由操作者指定 `scope`；不要将包含多个用户/群的总目录整体标成 `public`。设置 `DORAYMON_KNOWLEDGE_ENABLED=true` 后使用 `/知识来源` 检查可见范围，再测试 `/知识问`。群回复不会读取用户私人资料。

## 从运行系统切换

目前工作树是待平台验收的候选版本，原服务继续运行。切换前：

1. 保存原代码版本、启动配置与 SQLite 一致性备份。在线数据库使用 SQLite backup API；不要只复制正在使用的主库文件。
2. 在候选工作树新建配置。逐项迁移 OneBot 地址/token、原允许名单、模型配置、地图 key、桥接租约；不要整份复制旧配置后假设兼容。
3. 保持新默认数据库路径；不自动读取、修改或迁移旧历史命名空间。迁移真实历史需要另行确定来源、目标键和回退策略。
4. 停止原业务后端，启动一个候选实例，避免两个实例重复回复。NapCat 与电脑桥接继续使用原配置。
5. 在原封闭私聊范围验收 `/ping`、连续问答、清空/停止、桥接让行和用餐追问。真实模型/地图调用按具体授权执行。群参与和持续运行单独验收。
6. 任一关键行为失败，停止候选，恢复原启动命令和原配置/数据库。候选数据与原数据相互独立。

原常驻脚本调用的 `scripts.run_account` 与候选入口不同；未经入口、动态 WSL 地址、环境和桥接联动核对，不让监督脚本指向候选工作树。本轮没有修改监督脚本或重载运行服务。

## 排查

- 无任何回复：核对 OneBot 连接和允许范围，再检查本地命令。空名单默认为拒绝。
- 模型/地图失败：检查本机密钥、URL/模型名及服务方状态；不将原始异常 URL 或鉴权头复制到公开日志。业务日志仅记录异常类型。
- 群内静默：检查全局静默、当前成员停止状态、@/引用对象和普通参与额度。普通参与每 30 秒最多 1 次，直接请求每分钟容量 8；按群分别计算。
- 发送结果不明：不自动重发；先从平台核对是否已收到。部分成功不记作完整助手历史。
- `no such module: fts5`：运行 Python 的 SQLite 未启用 FTS5；准备带该扩展的 Python/SQLite 后再初始化，不降级成跨权限的全文扫描。
