from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from services.rag_evaluation import evaluate_retrieval, load_eval_cases


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class RagEvaluationTest(unittest.TestCase):
    def test_repository_eval_set_is_valid_and_has_required_coverage(self) -> None:
        cases = load_eval_cases(PROJECT_ROOT / "evals" / "rag_cases.jsonl")

        self.assertGreaterEqual(len(cases), 20)
        self.assertTrue(any(case.group_openid for case in cases))
        self.assertTrue(any(case.user_openid for case in cases))
        self.assertTrue(any(not case.answerable for case in cases))

    def test_loader_rejects_inconsistent_answerable_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "cases.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "id": "broken",
                        "query": "问题",
                        "expected_sources": [],
                        "answerable": True,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "不一致"):
                load_eval_cases(path)

    def test_evaluation_reports_hits_no_answer_and_scope_safety(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            knowledge_dir = root / "knowledge"
            (knowledge_dir / "groups" / "group-a").mkdir(parents=True)
            (knowledge_dir / "public.md").write_text(
                "# 部署说明\n\n部署机器人前先安装 Python 依赖。",
                encoding="utf-8",
            )
            (knowledge_dir / "groups" / "group-a" / "secret.md").write_text(
                "# 群暗号\n\n群暗号是彩虹桥梁。",
                encoding="utf-8",
            )
            cases_path = root / "cases.jsonl"
            cases_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "id": "hit",
                                "query": "如何安装 Python 依赖",
                                "expected_sources": ["public.md"],
                                "answerable": True,
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "id": "denied",
                                "query": "群暗号是什么",
                                "expected_sources": [],
                                "answerable": False,
                                "group_openid": "group-b",
                            },
                            ensure_ascii=False,
                        ),
                    ]
                ),
                encoding="utf-8",
            )

            report = evaluate_retrieval(
                load_eval_cases(cases_path),
                knowledge_dir,
                top_k=3,
            )

        self.assertEqual(report.metrics.case_count, 2)
        self.assertEqual(report.metrics.recall_at_k, 1.0)
        self.assertEqual(report.metrics.mrr, 1.0)
        self.assertEqual(report.metrics.no_answer_accuracy, 1.0)
        self.assertEqual(report.metrics.scope_violation_count, 0)


if __name__ == "__main__":
    unittest.main()
