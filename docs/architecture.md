# 架构说明

DoraYmon 是一个独立、插件化的 Python QQ AI 助手。项目不放在 `botpy/examples/` 下，方便单独部署、维护和扩展功能。

## 分层

- `main.py` 只负责启动。
- `doraymon/client.py` 继承 `botpy.Client`，负责接收消息、调用路由、发送回复。
- `doraymon/router.py` 负责命令分发。
- `plugins/` 放具体命令逻辑。插件返回文本，不直接调用 QQ 发送 API。
- `services/` 放外部服务调用，例如 DeepSeek、天气和图片服务。
- `storage/` 放 SQLite 连接和数据读写，不使用 ORM。
- `data/` 放运行数据和示例数据。真实数据库不提交。
- `skills/` 放提示词、人格和技能文档。私有内容不提交。
- `logs/` 放运行日志。日志文件不提交。

## 数据流

```text
QQ 消息
  ↓
doraymon/client.py
  ↓
doraymon/router.py
  ↓
显式命令或 services/intent_service.py
  ↓
plugins/
  ↓
services/ 或 storage/
  ↓
返回文本给 QQ
```

私聊普通消息会先经过规则意图识别。明确的用餐决策表达会进入吃什么插件，其他内容回退到 `/chat`。群聊只有显式命令或 @ 机器人时才进入这条流程，@ 后未命中意图的普通文本也会回退到 `/chat`，未 @ 的普通群消息不会自动触发插件。

短期上下文只在 `plugins/chat.py` 中按配置接入。`services/conversation_service.py` 负责选择完整问答轮次、执行字符预算、扩展短追问检索查询，并在 RAG 开启时注入可选知识资料。`storage/chat_history_store.py` 按私聊用户或“群 + 用户”隔离有限消息，整轮原子保存并清理旧消息；client 和 router 不直接读写聊天历史。

## RAG 数据流

当前已实现以下 SQLite FTS5/BM25 基线：

```text
resources/knowledge/ Markdown/TXT
  ↓ 离线分块和建索引
storage/knowledge_store.py + SQLite FTS5/BM25
  ↓ Top-K 知识块和元数据
services/rag_service.py
  ↓ 带来源编号和拒答规则的 Prompt
DeepSeek 生成层
  ↓
答案 + 参考来源
```

`storage/knowledge_store.py` 从目录结构读取公共、群和私人作用域，只向当前群或用户返回有权访问的知识块。`services/rag_service.py` 把检索内容标为不可信资料，限制上下文长度，并要求资料不足时拒答。普通聊天通过 `services/conversation_service.py` 可选复用同一检索能力，但资料不足或索引故障时继续普通聊天。Embedding、余弦相似度和 RRF 混合检索只在固定评测集证明有效后加入。

## 扩展原则

新增功能时一次只处理一个插件。先确定命令入口，再拆出存储层或服务层，最后接入路由并补充验证步骤。
