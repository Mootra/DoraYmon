from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.chat_cli import local_session, send_message
from scripts.eval_chat import check_reply, evaluate
from storage.db import get_db_path


class ChatToolsTest(unittest.TestCase):
    def test_fake_session_isolated_and_routes_history_commands(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            sentinel = path / "doraymon.db"
            sentinel.write_text("must not be opened")
            with patch("scripts.chat_cli.load_settings", side_effect=AssertionError("real config")),                  patch("storage.db.load_settings", side_effect=AssertionError("real config")),                  patch("services.deepseek_service.requests.post", side_effect=AssertionError("network")):
                with local_session(fake=True, data_dir=path, rag=True) as (settings, model):
                    async def run():
                        self.assertIn("历史 0 轮", await send_message(settings, "你好"))
                        self.assertIn("历史 1 轮", await send_message(settings, "继续"))
                        self.assertIn("历史 0 轮", await send_message(settings, "你好", user="other"))
                        self.assertIn("历史 0 轮", await send_message(settings, "你好", group="g"))
                        self.assertEqual(await send_message(settings, "你好", group="g", at=False), "")
                        self.assertIn("已清空", await send_message(settings, "/清空上下文"))
                        self.assertIn("历史 0 轮", await send_message(settings, "继续"))
                        self.assertTrue(await send_message(settings, "/help"))
                        self.assertTrue(await send_message(settings, "/知识问 如何启动项目？"))
                        self.assertNotEqual(get_db_path("doraymon"), sentinel)
                    asyncio.run(run())
                    self.assertGreater(len(model.requests), 0)
            self.assertEqual(sentinel.read_text(), "must not be opened")
            self.assertEqual(list(path.iterdir()), [sentinel])

    def test_evaluation_skips_unimplemented_memory_and_checks_style(self):
        report = evaluate([
            {"id": "hello", "messages": ["你好"]},
            {"id": "memory", "requires": "long_term_memory", "messages": ["/记住 小明"]},
        ])
        self.assertEqual(report["mode"], "fake")
        self.assertTrue(all(report["results"][0]["checks"].values()))
        self.assertEqual(report["results"][1]["status"], "skipped")
        checks = check_reply("**作为一个人工智能**", {"max_chars": 3})
        self.assertFalse(checks["no_markdown"])
        self.assertFalse(checks["no_canned_phrase"])
        self.assertFalse(checks["length"])
