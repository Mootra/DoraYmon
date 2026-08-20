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
