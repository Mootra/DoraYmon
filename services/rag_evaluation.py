from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from storage.knowledge_store import (
    KnowledgeIndexStatus,
    KnowledgeSearchResult,
    rebuild_knowledge_index,
    search_knowledge,
)


@dataclass(frozen=True)
class RagEvalCase:
    case_id: str
    query: str
    expected_sources: tuple[str, ...]
    answerable: bool
    group_openid: str = ""
    user_openid: str = ""
    note: str = ""


@dataclass(frozen=True)
class RagEvalCaseResult:
    case_id: str
    query: str
    expected_sources: tuple[str, ...]
    retrieved_sources: tuple[str, ...]
    answerable: bool
    hit: bool
    reciprocal_rank: float
    scope_violations: tuple[str, ...]
    note: str = ""


@dataclass(frozen=True)
class RagEvalMetrics:
    case_count: int
    answerable_count: int
    unanswerable_count: int
    recall_at_k: float
    mrr: float
    no_answer_accuracy: float | None
    scope_violation_count: int


@dataclass(frozen=True)
class RagEvalReport:
    tokenizer: str
    top_k: int
    chunk_max_chars: int
    chunk_overlap_chars: int
    index_status: KnowledgeIndexStatus
    metrics: RagEvalMetrics
    cases: tuple[RagEvalCaseResult, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_eval_cases(path: Path) -> list[RagEvalCase]:
    case_path = Path(path)
    cases: list[RagEvalCase] = []
    seen_ids: set[str] = set()

    for line_number, raw_line in enumerate(
        case_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"评测集第 {line_number} 行不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"评测集第 {line_number} 行必须是 JSON 对象")

        case_id = str(payload.get("id", "")).strip()
        query = str(payload.get("query", "")).strip()
        raw_sources = payload.get("expected_sources", [])
        if not case_id or not query:
            raise ValueError(f"评测集第 {line_number} 行缺少 id 或 query")
        if case_id in seen_ids:
            raise ValueError(f"评测集包含重复 id：{case_id}")
        if not isinstance(raw_sources, list):
            raise ValueError(f"评测样例 {case_id} 的 expected_sources 必须是数组")

        expected_sources = tuple(
            _normalize_source_path(str(source))
            for source in raw_sources
            if str(source).strip()
        )
        answerable = payload.get("answerable", bool(expected_sources))
        if not isinstance(answerable, bool):
            raise ValueError(f"评测样例 {case_id} 的 answerable 必须是布尔值")
        if answerable != bool(expected_sources):
            raise ValueError(
                f"评测样例 {case_id} 的 answerable 与 expected_sources 不一致"
            )

        seen_ids.add(case_id)
        cases.append(
            RagEvalCase(
                case_id=case_id,
                query=query,
                expected_sources=expected_sources,
                answerable=answerable,
                group_openid=str(payload.get("group_openid", "")).strip(),
                user_openid=str(payload.get("user_openid", "")).strip(),
                note=str(payload.get("note", "")).strip(),
            )
        )

    if not cases:
        raise ValueError("评测集不能为空")
    return cases


def evaluate_retrieval(
    cases: list[RagEvalCase],
    knowledge_dir: Path,
    tokenizer: str = "trigram",
    top_k: int = 3,
    chunk_max_chars: int = 800,
    chunk_overlap_chars: int = 100,
) -> RagEvalReport:
    if not cases:
        raise ValueError("cases 不能为空")
    if top_k <= 0:
        raise ValueError("top_k 必须大于 0")

    with tempfile.TemporaryDirectory(prefix="doraymon-rag-eval-") as temp_dir:
        with _temporary_data_dir(temp_dir):
            index_status = rebuild_knowledge_index(
                Path(knowledge_dir),
                tokenizer=tokenizer,
                chunk_max_chars=chunk_max_chars,
                chunk_overlap_chars=chunk_overlap_chars,
            )
            case_results = tuple(
                _evaluate_case(case, top_k=top_k) for case in cases
            )

    answerable_results = [result for result in case_results if result.answerable]
    unanswerable_results = [result for result in case_results if not result.answerable]
    hit_count = sum(result.hit for result in answerable_results)
    no_answer_count = sum(
        not result.retrieved_sources for result in unanswerable_results
    )
    scope_violation_count = sum(
        len(result.scope_violations) for result in case_results
    )

    metrics = RagEvalMetrics(
        case_count=len(case_results),
        answerable_count=len(answerable_results),
        unanswerable_count=len(unanswerable_results),
        recall_at_k=(hit_count / len(answerable_results) if answerable_results else 0.0),
        mrr=(
            sum(result.reciprocal_rank for result in answerable_results)
            / len(answerable_results)
            if answerable_results
            else 0.0
        ),
        no_answer_accuracy=(
            no_answer_count / len(unanswerable_results)
            if unanswerable_results
            else None
        ),
        scope_violation_count=scope_violation_count,
    )
    return RagEvalReport(
        tokenizer=tokenizer,
        top_k=top_k,
        chunk_max_chars=chunk_max_chars,
        chunk_overlap_chars=chunk_overlap_chars,
        index_status=index_status,
        metrics=metrics,
        cases=case_results,
    )


def format_report(report: RagEvalReport) -> str:
    metrics = report.metrics
    no_answer_text = (
        f"{metrics.no_answer_accuracy:.3f}"
        if metrics.no_answer_accuracy is not None
        else "n/a"
    )
    lines = [
        "DoraYmon RAG 离线检索评测",
        f"样例：{metrics.case_count}（可回答 {metrics.answerable_count}，无答案 {metrics.unanswerable_count}）",
        f"索引：{report.index_status.document_count} 文档 / {report.index_status.chunk_count} 分块",
        f"参数：tokenizer={report.tokenizer}, top_k={report.top_k}, chunk={report.chunk_max_chars}, overlap={report.chunk_overlap_chars}",
        f"Recall@{report.top_k}：{metrics.recall_at_k:.3f}",
        f"MRR：{metrics.mrr:.3f}",
        f"无答案空召回准确率：{no_answer_text}",
        f"权限泄漏：{metrics.scope_violation_count}",
    ]

    failed = [result for result in report.cases if result.answerable and not result.hit]
    false_positives = [
        result
        for result in report.cases
        if not result.answerable and result.retrieved_sources
    ]
    if failed:
        lines.append("未命中样例：" + ", ".join(result.case_id for result in failed))
    if false_positives:
        lines.append(
            "无答案误召回样例："
            + ", ".join(result.case_id for result in false_positives)
        )
    return "\n".join(lines)


def _evaluate_case(case: RagEvalCase, top_k: int) -> RagEvalCaseResult:
    results = search_knowledge(
        case.query,
        limit=top_k,
        group_openid=case.group_openid,
        user_openid=case.user_openid,
    )
    retrieved_sources = tuple(result.source_path for result in results)
    first_expected_rank = next(
        (
            rank
            for rank, source in enumerate(retrieved_sources, start=1)
            if source in case.expected_sources
        ),
        0,
    )
    scope_violations = tuple(
        result.source_path
        for result in results
        if not _is_accessible(result, case.group_openid, case.user_openid)
    )
    return RagEvalCaseResult(
        case_id=case.case_id,
        query=case.query,
        expected_sources=case.expected_sources,
        retrieved_sources=retrieved_sources,
        answerable=case.answerable,
        hit=first_expected_rank > 0,
        reciprocal_rank=(1.0 / first_expected_rank if first_expected_rank else 0.0),
        scope_violations=scope_violations,
        note=case.note,
    )


def _is_accessible(
    result: KnowledgeSearchResult,
    group_openid: str,
    user_openid: str,
) -> bool:
    if result.scope_type == "public":
        return True
    if result.scope_type == "group":
        return result.scope_openid == group_openid
    if result.scope_type == "private":
        return result.scope_openid == user_openid
    return False


def _normalize_source_path(value: str) -> str:
    return value.strip().replace("\\", "/")


@contextmanager
def _temporary_data_dir(path: str):
    previous_value = os.environ.get("DATA_DIR")
    os.environ["DATA_DIR"] = path
    try:
        yield
    finally:
        if previous_value is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = previous_value
