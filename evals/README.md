# RAG 离线评测

`rag_cases.jsonl` 是固定检索评测集，`fixtures/knowledge/` 是与正式知识库隔离的评测资料。运行评测时会使用临时 SQLite 数据库，不修改 `data/knowledge.db`。

运行默认基线：

```powershell
.\.venv\Scripts\python.exe scripts\eval_rag.py
```

生成可保存的 JSON 报告：

```powershell
.\.venv\Scripts\python.exe scripts\eval_rag.py --json-output evals/results/baseline.json
```

指标含义：

- `Recall@K`：可回答问题的预期来源是否出现在前 K 条。
- `MRR`：预期来源首次出现的位置是否足够靠前。
- `无答案空召回准确率`：知识库没有答案时是否没有返回弱相关资料。
- `权限泄漏`：结果中是否出现当前群或用户无权读取的资料。

先保存基线，再根据失败样例调整查询、分块和 Top-K。不要先调参数再修改评测答案。

## 当前默认基线

2026-08-20 使用 `trigram`、`top_k=3`、`chunk=800`、`overlap=100`：

- Recall@3：`0.850`
- MRR：`0.850`
- 无答案空召回准确率：`1.000`
- 权限泄漏：`0`

当前仍未命中的 `context-not-memory`、`semantic-follow-up`、`semantic-local-run` 是语义改写压力样例。不要为它们添加逐句硬编码；后续用真实样例判断是否值得引入 Embedding 或更系统的查询改写。

需要把当前基线作为回归门槛时运行：

```powershell
.\.venv\Scripts\python.exe scripts\eval_rag.py --min-recall 0.85 --min-mrr 0.85 --min-no-answer-accuracy 1 --require-zero-scope-violations
```

## 功能与口吻的小批次迭代

`feature_iteration_cases.jsonl` 有 20 组、40 轮合成消息：前 10 组为食物/路由，后 10 组为聊天口吻、约束、能力说明和上下文。
每组包含 `review` 人工评阅目标；不要把 fake 回复、HTTP 成功或单独的格式检查当作质量评分。

先跑一批（食物回复为真实本地插件输出，涉及模型的部分为 fake）：

```bash
.venv/bin/python scripts/eval_dialogue.py --fake --cases evals/feature_iteration_cases.jsonl --max-turns 20 --workers 1 --label food-before --output-dir evals/results/food-before
```

读每个 `dialogue-*.json` 的输入、回复、实际请求和检查结果；记录失败例及要修改的假设。
修改后将 `--label` 与 `--output-dir` 改为 `food-after`，保持用例和其余参数不变，再运行一次。
下一批加 `--offset 10`，将标签及目录换为新名称。不会自动跨批继续调用模型，便于在两批之间改配置或代码。
`--max-turns` 不拆散同一组对话，因此某批可能少于上限；以 `run.json` 中的 `next_offset` 续跑。
若首组就超过轮数上限，工具报错，不静默截掉追问。`--limit` 仍按组计数。

取得真实调用授权后，聊天口吻的 20 轮可这样运行：

```bash
.venv/bin/python scripts/eval_dialogue.py --real --model-env .env --cases evals/feature_iteration_cases.jsonl --offset 10 --max-turns 20 --workers 1 --label chat-before --output-dir evals/results/chat-before
```

真实模式仅读取指定文件中的模型配置，使用合成消息和独立临时库，不连接 QQ。
对照时使用修改前代码及提示词跑 `before`、修改后版本跑 `after`；两次用同一批问题、模型和温度。
如果新版本已改过提示词，应从修改前提交的隔离检出采集基线，不能把新版本第一次运行叫作旧版基线。
每批约 20 轮后人工查看：是否完成用户要求、是否反复追问或给多余选项、是否遵守字数和事实约束、是否谎称执行能力。
发现问题只改对应插件规则或提示词，再回归同一批；新增失败例进入下一批，保留旧样本防止退步。

`run.json` 记录所选用例 ID、轮数、模式、模型、温度、用例哈希及公开代码/提示词/食物清单的哈希。
逐轮请求也保存到结果中，不记录鉴权头或配置密钥。非空输出目录会被拒绝，以免覆盖基线。
模型错误或执行异常让进程返回非零；风格检查只作评阅线索。结果目录被 Git 忽略，代码提交不包含原文报告。
