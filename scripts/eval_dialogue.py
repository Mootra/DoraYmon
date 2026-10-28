"""Record bounded dialogue batches for test/review/change/retest cycles."""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import ContextVar
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import requests
from dotenv import dotenv_values
from doraymon.config import Settings
from scripts.chat_cli import FakeModel, send_message
from scripts.eval_chat import check_reply
from services.deepseek_service import DeepSeekService
from storage.db import init_all_tables, use_data_directory
from storage.knowledge_store import rebuild_knowledge_index

TRACES = ContextVar("dialogue_traces", default=None)


def select_cases(cases, *, offset=0, limit=None, max_turns=None):
    """Keep conversations whole, so a batch never loses follow-up context."""
    if offset < 0 or (limit is not None and limit < 1) or (max_turns is not None and max_turns < 1):
        raise ValueError("offset 必须非负，limit 和 max-turns 必须为正数")
    selected = []
    turns = 0
    for case in cases[offset:]:
        count = len(case["messages"])
        if not count:
            raise ValueError("用例不能没有消息")
        if max_turns is not None and turns + count > max_turns:
            if not selected:
                raise ValueError("首组对话超过 max-turns；请增加上限，不能截断多轮对话")
            break
        selected.append(case)
        turns += count
        if limit is not None and len(selected) >= limit:
            break
    if not selected:
        raise ValueError("没有可运行的用例")
    return selected


def fake_chat(messages, **options):
    reply = FakeModel().chat_messages(messages, **options)
    traces = TRACES.get()
    if traces is not None:
        traces.append({"fake": True, "request": {"messages": messages, **options},
                       "reply": reply})
    return reply


def source_fingerprints():
    """Public source hashes identify edits even before a local commit."""
    paths = []
    for directory in ("services", "plugins", "doraymon", "storage", "scripts"):
        paths.extend((PROJECT_ROOT / directory).glob("*.py"))
    paths.extend((PROJECT_ROOT / "resources/persona").glob("*.md"))
    paths.append(PROJECT_ROOT / "data/food_catalog.example.json")
    return {str(path.relative_to(PROJECT_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths)}


def write_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    tmp.replace(path)


def instrument_post(original):
    def post(url, **kwargs):
        payload = kwargs.get("json", {})
        trace = {"request": {k: payload[k] for k in ("model", "messages", "temperature", "max_tokens") if k in payload}}
        start = time.perf_counter()
        try:
            response = original(url, **kwargs)
            trace["http_status"] = response.status_code
            if response.ok:
                data = response.json()
                trace["model"] = data.get("model")
                trace["usage"] = data.get("usage")
                trace["choices"] = data.get("choices")
            return response
        except Exception as exc:
            trace["exception_type"] = type(exc).__name__
            raise
        finally:
            trace["seconds"] = round(time.perf_counter() - start, 3)
            traces = TRACES.get()
            if traces is not None:
                traces.append(trace)
    return post


def run_case(case, settings, output):
    dest = output / (case["id"] + ".json")
    result = {"id": case["id"], "category": case["category"], "status": "running", "turns": []}
    with tempfile.TemporaryDirectory(prefix="doraymon-dialogue-") as temp:
        selected = replace(
            settings, data_dir=temp, chat_history_enabled=True,
            rag_enabled=case.get("rag", False),
            knowledge_dir=str(PROJECT_ROOT / "evals/fixtures/knowledge"),
            admin_openids=[],
        )
        with use_data_directory(Path(temp)):
            init_all_tables()
            if selected.rag_enabled:
                rebuild_knowledge_index(Path(selected.knowledge_dir))
            async def run():
                for number, entry in enumerate(case["messages"], 1):
                    step = {"text": entry} if isinstance(entry, str) else entry
                    trace = []
                    token = TRACES.set(trace)
                    started = time.perf_counter()
                    try:
                        reply = await send_message(
                            selected, step["text"], user=step.get("user", "audit-user"),
                            group=step.get("group", ""), at=step.get("at", True),
                        )
                        turn = {
                            "number": number, "input": step,
                            "reply": reply, "seconds": round(time.perf_counter()-started, 3),
                            "chars": len(reply), "checks": check_reply(reply, {**case, **step}),
                            "model_calls": trace,
                        }
                    except Exception as exc:
                        turn = {"number": number, "input": step, "exception_type": type(exc).__name__,
                                "model_calls": trace, "seconds": round(time.perf_counter()-started, 3)}
                    finally:
                        TRACES.reset(token)
                    result["turns"].append(turn)
                    write_json(dest, result)
            asyncio.run(run())
    result["status"] = "completed" if all(
        "exception_type" not in turn and turn["checks"]["no_model_error"]
        for turn in result["turns"]
    ) else "failed"
    write_json(dest, result)
    return result


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--real", action="store_true")
    mode.add_argument("--fake", action="store_true", help="只验证流程，不读取模型配置或联网")
    parser.add_argument("--model-env", type=Path)
    parser.add_argument("--cases", type=Path, default=PROJECT_ROOT / "evals/dialogue_100_cases.jsonl")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int, default=0, help="跳过前 N 组，下一批从此处开始")
    parser.add_argument("--max-turns", type=int, help="本批轮数上限，不拆分一组对话")
    parser.add_argument("--label", default="", help="本次修改假设/版本，写入报告")
    args = parser.parse_args()
    if args.real and args.model_env is None:
        parser.error("--real 需要 --model-env")
    if args.fake and args.model_env is not None:
        parser.error("--fake 不接受 --model-env")
    all_cases = [json.loads(line) for line in args.cases.read_text(encoding="utf8").splitlines() if line.strip()]
    try:
        cases = select_cases(all_cases, offset=args.offset, limit=args.limit, max_turns=args.max_turns)
    except ValueError as exc:
        parser.error(str(exc))
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)) or any(not item.startswith("dialogue-") or not item[9:].isdigit() for item in ids):
        raise SystemExit("评测 ID 必须唯一且采用 dialogue-数字 格式")
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        raise SystemExit("输出目录非空，请选用新目录，以免覆盖")
    # Only model credentials enter Settings; never serialize this object or env.
    values = dotenv_values(args.model_env) if args.real else {}
    settings = Settings(
        deepseek_api_key=values.get("DEEPSEEK_API_KEY") or "",
        deepseek_base_url=(values.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/"),
        deepseek_model=values.get("DEEPSEEK_MODEL") or "deepseek-v4-flash",
        deepseek_temperature=float(values.get("DEEPSEEK_TEMPERATURE") or "0.7"),
    )
    del values
    if args.real and not settings.deepseek_api_key:
        raise SystemExit("未配置模型密钥，未发起请求。")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_json(args.output_dir / "run.json", {
        "started_at": datetime.now(timezone.utc).isoformat(), "model": settings.deepseek_model,
        "temperature": settings.deepseek_temperature, "groups": len(cases),
        "turns_planned": sum(len(c["messages"]) for c in cases),
        "history_enabled": True, "history_limit": settings.chat_history_limit,
        "history_max_content_length": settings.chat_history_max_content_length,
        "context_max_chars": settings.chat_context_max_chars,
        "cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        "workers": max(1, min(args.workers, 4)), "real": args.real, "qq_connected": False,
        "label": args.label, "offset": args.offset, "next_offset": args.offset + len(cases),
        "case_ids": ids, "max_turns": args.max_turns,
        "source_sha256": source_fingerprints(),
        "quality_evidence": "需人工评阅真实回复" if args.real else "仅验证流程，不证明真实回答质量",
    })
    logging.disable(logging.CRITICAL)
    start = time.perf_counter()
    results = []
    # This wraps the real transport only to record metadata; it does not fake responses.
    transport_patch = (patch.object(requests, "post", instrument_post(requests.post)) if args.real
                       else patch.object(DeepSeekService, "chat_messages", side_effect=fake_chat))
    with transport_patch:
        with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 4))) as pool:
            futures = [pool.submit(run_case, case, settings, args.output_dir) for case in cases]
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                errors = sum(any(not t.get("fake") and t.get("http_status", 0) != 200 for t in turn["model_calls"]) for turn in result["turns"])
                print(f'{len(results)}/{len(cases)} {result["id"]} turns={len(result["turns"])} transport_errors={errors}', flush=True)
    write_json(args.output_dir / "results.json", {
        "elapsed_seconds": round(time.perf_counter()-start, 3),
        "results": sorted(results, key=lambda item: item["id"]),
    })
    print(f"记录完成：{args.output_dir}", flush=True)
    return int(any(result["status"] == "failed" for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
