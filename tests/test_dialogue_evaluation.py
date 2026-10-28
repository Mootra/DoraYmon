from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from doraymon.config import Settings
from scripts.eval_dialogue import instrument_post, run_case, select_cases, main


class DialogueEvaluationTest(unittest.TestCase):
    def test_batch_keeps_conversations_whole_and_has_exact_resume_point(self):
        cases = [{"messages": ["a"] * count} for count in (2, 3, 2)]
        self.assertEqual(select_cases(cases, max_turns=4), cases[:1])
        self.assertEqual(select_cases(cases, offset=1, max_turns=5), cases[1:])
        self.assertEqual(select_cases(cases, limit=1), cases[:1])
        for kwargs in ({"max_turns": 1}, {"offset": 3}, {"offset": -1}, {"limit": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                select_cases(cases, **kwargs)

    def test_fake_cli_never_reads_config_or_calls_network(self):
        with tempfile.TemporaryDirectory() as temp:
            cases = Path(temp) / "cases.jsonl"
            cases.write_text(json.dumps({"id": "dialogue-001", "category": "test", "messages": ["你好"]}))
            output = Path(temp) / "result"
            args = ["eval_dialogue", "--fake", "--cases", str(cases), "--output-dir", str(output), "--max-turns", "20"]
            with patch("sys.argv", args), patch("scripts.eval_dialogue.dotenv_values", side_effect=AssertionError("env")), patch("storage.db.load_settings", side_effect=AssertionError("config")), patch("requests.post", side_effect=AssertionError("network")), patch("scripts.eval_dialogue.logging.disable"), patch("builtins.print"):
                self.assertEqual(main(), 0)
            manifest = json.loads((output / "run.json").read_text())
            result = json.loads((output / "dialogue-001.json").read_text())
            self.assertFalse(manifest["real"])
            self.assertEqual(manifest["next_offset"], 1)
            self.assertEqual(manifest["turns_planned"], 1)
            self.assertTrue(manifest["source_sha256"])
            self.assertTrue(result["turns"][0]["model_calls"][0]["fake"])

    def test_records_real_transport_shape_without_credentials(self):
        response = Mock(status_code=200, ok=True)
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "model": "test-model", "usage": {"total_tokens": 12},
            "choices": [{"message": {"content": "离线回复"}, "finish_reason": "stop"}],
        }
        transport = Mock(return_value=response)
        with tempfile.TemporaryDirectory() as temp, patch(
            "requests.post", side_effect=instrument_post(transport)
        ), patch("storage.db.load_settings", side_effect=AssertionError("real config")):
            result = run_case(
                {"id": "dialogue-001", "category": "test",
                 "messages": ["你好", {"text": "继续", "user": "another-user"}]},
                Settings(deepseek_api_key="synthetic-secret", deepseek_base_url="https://example.test"),
                Path(temp),
            )
            saved = json.loads((Path(temp) / "dialogue-001.json").read_text())
        self.assertEqual(result, saved)
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(len(saved["turns"]), 2)
        call = saved["turns"][0]["model_calls"][0]
        self.assertEqual(call["http_status"], 200)
        self.assertEqual(call["usage"]["total_tokens"], 12)
        self.assertNotIn("synthetic-secret", json.dumps(saved))
        self.assertEqual(len(saved["turns"][1]["model_calls"][0]["request"]["messages"]), 2)

    def test_next_turn_history_matches_bounded_reply_and_mock_qq_segments(self):
        from doraymon.client import split_reply
        response = Mock(status_code=200, ok=True)
        response.json.return_value = {"choices": [{"message": {"content": "代码行\n    缩进行\n" * 600}, "finish_reason": "stop"}]}
        with tempfile.TemporaryDirectory() as temp, patch("requests.post", side_effect=instrument_post(Mock(return_value=response))):
            result = run_case(
                {"id": "dialogue-001", "category": "length", "messages": ["给个示例", "最后一句？"]},
                Settings(deepseek_api_key="synthetic-secret"), Path(temp),
            )
        first = result["turns"][0]["reply"]
        self.assertEqual(len(first), 4000)
        self.assertTrue(first.endswith("[回复过长，已截断]"))
        self.assertEqual("".join(split_reply(first)), first)
        second_messages = result["turns"][1]["model_calls"][0]["request"]["messages"]
        self.assertEqual(next(m["content"] for m in second_messages if m["role"] == "assistant"), first)

    def test_records_http_failure_as_failure_not_quality_success(self):
        import requests
        response = Mock(status_code=429, ok=False)
        response.raise_for_status.side_effect = requests.HTTPError("synthetic rate limit")
        with tempfile.TemporaryDirectory() as temp, patch(
            "requests.post", side_effect=instrument_post(Mock(return_value=response))
        ):
            result = run_case(
                {"id": "dialogue-001", "category": "test", "messages": ["你好"]},
                Settings(deepseek_api_key="synthetic-secret"), Path(temp),
            )
        turn = result["turns"][0]
        self.assertEqual(result["status"], "failed")
        self.assertFalse(turn["checks"]["no_model_error"])
        self.assertEqual(turn["model_calls"][0]["http_status"], 429)
