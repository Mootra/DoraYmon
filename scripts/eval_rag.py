from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.rag_evaluation import (
    evaluate_retrieval,
    format_report,
    load_eval_cases,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="离线评测 DoraYmon RAG 检索质量")
    parser.add_argument(
        "--cases",
        default="evals/rag_cases.jsonl",
        help="JSONL 评测集路径",
    )
    parser.add_argument(
        "--knowledge-dir",
        default="evals/fixtures/knowledge",
        help="评测知识目录",
    )
    parser.add_argument("--tokenizer", choices=("trigram", "unicode61"), default="trigram")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--chunk-max-chars", type=int, default=800)
    parser.add_argument("--chunk-overlap-chars", type=int, default=100)
    parser.add_argument("--json-output", help="可选的 JSON 报告输出路径")
    parser.add_argument("--min-recall", type=float)
    parser.add_argument("--min-mrr", type=float)
    parser.add_argument("--min-no-answer-accuracy", type=float)
    parser.add_argument("--require-zero-scope-violations", action="store_true")
    args = parser.parse_args()

    cases_path = _resolve_project_path(args.cases)
    knowledge_dir = _resolve_project_path(args.knowledge_dir)
    report = evaluate_retrieval(
        load_eval_cases(cases_path),
        knowledge_dir,
        tokenizer=args.tokenizer,
        top_k=args.top_k,
        chunk_max_chars=args.chunk_max_chars,
        chunk_overlap_chars=args.chunk_overlap_chars,
    )
    print(format_report(report))

    if args.json_output:
        output_path = _resolve_project_path(args.json_output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"JSON 报告：{output_path}")

    failures: list[str] = []
    if args.min_recall is not None and report.metrics.recall_at_k < args.min_recall:
        failures.append("Recall 未达到阈值")
    if args.min_mrr is not None and report.metrics.mrr < args.min_mrr:
        failures.append("MRR 未达到阈值")
    if (
        args.min_no_answer_accuracy is not None
        and report.metrics.no_answer_accuracy is not None
        and report.metrics.no_answer_accuracy < args.min_no_answer_accuracy
    ):
        failures.append("无答案准确率未达到阈值")
    if args.require_zero_scope_violations and report.metrics.scope_violation_count:
        failures.append("存在权限泄漏")

    if failures:
        print("评测未通过：" + "；".join(failures), file=sys.stderr)
        return 1
    return 0


def _resolve_project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
