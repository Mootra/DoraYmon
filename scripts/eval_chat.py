"""Repeatable synthetic chat scenarios; real quality needs human review."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.chat_cli import local_session, send_message
from services.deepseek_service import is_model_error

MARKDOWN = re.compile(r"(?m)^#{1,6}\s|\*\*|__|\x60{3}|\[[^\]]+\]\(https?://")
BANNED_PHRASES = ("作为一个人工智能", "作为一个AI", "希望这能帮到你")


def check_reply(reply, case):
    return {
        "nonempty": bool(reply) == case.get("expect_reply", True),
        "no_model_error": not is_model_error(reply),
        "no_markdown": not bool(MARKDOWN.search(reply)),
        "length": case.get("min_chars", 1) <= len(reply) <= case.get("max_chars", 1800),
        "no_canned_phrase": not any(word in reply for word in BANNED_PHRASES),
    }


def evaluate(cases, *, real=False):
    results = []
    with tempfile.TemporaryDirectory(prefix="doraymon-chat-eval-") as temp:
        for case in cases:
            if case.get("requires") == "long_term_memory":
                results.append({"id": case["id"], "status": "skipped", "reason": "D2 未定，长期记忆未实现"})
                continue
            with local_session(fake=not real, data_dir=Path(temp) / case["id"],
                               rag=case.get("rag", False)) as (settings, model):
                async def run():
                    replies = []
                    for message in case["messages"]:
                        replies.append(await send_message(
                            settings, message, user=case.get("user", "cli-user"),
                            group=case.get("group", ""), at=case.get("at", True),
                        ))
                    return replies
                replies = asyncio.run(run())
                checks = check_reply(replies[-1], case)
                results.append({
                    "id": case["id"], "status": "completed",
                    "replies": replies, "checks": checks,
                    "model_calls": len(model.requests) if model else None,
                })
    return {
        "mode": "real" if real else "fake",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "quality_evidence": "需人工评阅真实回复" if real else "仅验证流程，不证明真实回答质量",
        "results": results,
    }


def main():
    parser = argparse.ArgumentParser(description="合成场景回答评测，默认假模型")
    parser.add_argument("--real", action="store_true", help="调用真实模型，产生费用")
    parser.add_argument("--cases", type=Path, default=PROJECT_ROOT / "evals/chat_cases.jsonl")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--strict", action="store_true", help="风格检查失败时返回非零；基线默认只记录")
    args = parser.parse_args()
    cases = [json.loads(line) for line in args.cases.read_text(encoding="utf-8").splitlines() if line.strip()]
    report = evaluate(cases, real=args.real)
    output = args.output or PROJECT_ROOT / "evals/results" / (
        datetime.now().strftime("chat-%Y%m%d-%H%M%S") + ("-real.json" if args.real else "-fake.json"))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    completed = [row for row in report["results"] if row["status"] == "completed"]
    failed = [row["id"] for row in completed if not all(row["checks"].values())]
    print(f'{report["mode"]}: {len(completed)} 完成，{len(cases)-len(completed)} 跳过；检查未全通过：{failed}')
    print(report["quality_evidence"])
    print(f"报告：{output}")
    return int(args.strict and bool(failed))


if __name__ == "__main__":
    raise SystemExit(main())
