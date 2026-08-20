from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from doraymon.config import PROJECT_ROOT, Settings
from doraymon.context import BotContext
from doraymon.router import COMMANDS, route_incoming_message
from plugins import chat
from services.deepseek_service import DeepSeekService


class AIProjectContextTest(unittest.TestCase):
    def test_ai_project_context_document_covers_core_boundaries(self) -> None:
        context_path = PROJECT_ROOT / "docs" / "ai_project_context.md"

        self.assertTrue(context_path.exists())
        content = context_path.read_text(encoding="utf-8")

        required_terms = (
            "main.py",
            "doraymon/client.py",
            "doraymon/router.py",
            "插件",
            "services",
            "storage",
            "测试",
            ".env",
            "QQ Bot",
            "DeepSeek",
        )
        for term in required_terms:
            with self.subTest(term=term):
                self.assertIn(term, content)

    @patch("services.deepseek_service.requests.post")
    def test_deepseek_service_without_api_key_never_posts(self, post_mock) -> None:
        service = DeepSeekService(Settings(deepseek_api_key=""))

        reply = service.chat("hello")

        self.assertIn("API Key 未配置", reply)
        post_mock.assert_not_called()

    @patch("services.deepseek_service.requests.post")
    def test_deepseek_service_error_reply_does_not_leak_fake_secret(self, post_mock) -> None:
        fake_secret = "sk-test-secret-never-print"
        post_mock.side_effect = requests.RequestException("network unavailable")
        service = DeepSeekService(Settings(deepseek_api_key=fake_secret))

        reply = service.chat("hello")

        self.assertEqual(reply, "DeepSeek 请求失败，请稍后再试。")
        self.assertNotIn(fake_secret, reply)

    @patch("plugins.chat.DeepSeekService")
    def test_chat_plugin_uses_fake_service_offline(self, service_cls_mock) -> None:
        service_mock = service_cls_mock.return_value
        service_mock.chat.return_value = "fake model reply"
        context = BotContext(
            settings=Settings(deepseek_api_key=""),
            started_at=datetime.now(),
            user_openid="user-a",
            raw_content="/chat 你好",
            command="chat",
            args="你好",
        )

        reply = asyncio.run(chat.handle(context))

        self.assertEqual(reply, "fake model reply")
        service_cls_mock.assert_called_once_with(context.settings)
        service_mock.chat.assert_called_once_with("你好")

    @patch("doraymon.router.food.handle", return_value="自然语言食物推荐")
    def test_food_natural_language_keeps_priority_over_chat_fallback(self, food_mock) -> None:
        chat_handler = Mock(return_value="不应回退到聊天")
        context = BotContext(
            settings=Settings(food_natural_trigger_enabled=True),
            started_at=datetime.now(),
            user_openid="user-a",
            raw_content="今晚外卖点什么",
        )

        with patch.dict(COMMANDS, {"chat": chat_handler}, clear=False):
            reply = asyncio.run(route_incoming_message(context, fallback_command="chat"))

        self.assertEqual(reply, "自然语言食物推荐")
        self.assertEqual(context.command, "吃什么")
        self.assertEqual(context.args, "今晚外卖点什么")
        food_mock.assert_called_once_with(context)
        chat_handler.assert_not_called()

    def test_short_term_context_is_wired_only_to_chat_plugin(self) -> None:
        content = (PROJECT_ROOT / "docs" / "ai_project_context.md").read_text(
            encoding="utf-8"
        )
        chat_history_store = PROJECT_ROOT / "storage" / "chat_history_store.py"
        chat_plugin = (PROJECT_ROOT / "plugins" / "chat.py").read_text(encoding="utf-8")
        router = (PROJECT_ROOT / "doraymon" / "router.py").read_text(encoding="utf-8")
        client = (PROJECT_ROOT / "doraymon" / "client.py").read_text(encoding="utf-8")

        self.assertTrue(chat_history_store.exists())
        self.assertIn("chat_history_store", chat_plugin)
        self.assertIn("chat_history_enabled", chat_plugin)
        self.assertNotIn("chat_history_store", client)
        self.assertNotIn("chat_history_store", router)
        self.assertIn("可控短期上下文", content)
        self.assertIn("SQLite FTS5/BM25", content)
        self.assertIn("Embedding、向量检索、长期个人记忆和 Agent 尚未实现", content)
        self.assertIn("不实现无限聊天记录", content)

    def test_chat_history_commands_and_config_are_documented(self) -> None:
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        commands = (PROJECT_ROOT / "docs" / "commands.md").read_text(encoding="utf-8")
        env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        yaml_example = (PROJECT_ROOT / "config.example.yaml").read_text(encoding="utf-8")
        help_source = (PROJECT_ROOT / "plugins" / "help.py").read_text(encoding="utf-8")

        for command in ("清空上下文", "上下文状态"):
            with self.subTest(command=command):
                self.assertIn(command, COMMANDS)
                self.assertIn(f"/{command}", readme)
                self.assertIn(f"/{command}", commands)
                self.assertIn(command, help_source)

        for env_name in (
            "BOT_ENABLE_CHAT_HISTORY",
            "BOT_CHAT_HISTORY_LIMIT",
            "BOT_CHAT_HISTORY_MAX_CONTENT_LENGTH",
            "BOT_CHAT_CONTEXT_MAX_CHARS",
        ):
            with self.subTest(env_name=env_name):
                self.assertIn(env_name, readme)
                self.assertIn(env_name, env_example)

        for yaml_key in (
            "history_enabled:",
            "history_limit:",
            "history_max_content_length:",
            "context_max_chars:",
        ):
            with self.subTest(yaml_key=yaml_key):
                self.assertIn(yaml_key, yaml_example)

    def test_rag_commands_and_config_are_documented(self) -> None:
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        commands = (PROJECT_ROOT / "docs" / "commands.md").read_text(encoding="utf-8")
        env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        help_source = (PROJECT_ROOT / "plugins" / "help.py").read_text(encoding="utf-8")

        for command in ("知识问", "知识来源", "知识库状态", "重建知识库"):
            with self.subTest(command=command):
                self.assertIn(command, COMMANDS)
                self.assertIn(f"/{command}", readme)
                self.assertIn(f"/{command}", commands)
                self.assertIn(command, help_source)

        for env_name in (
            "BOT_ENABLE_RAG",
            "BOT_KNOWLEDGE_DIR",
            "BOT_RAG_TOP_K",
            "BOT_RAG_TOKENIZER",
        ):
            with self.subTest(env_name=env_name):
                self.assertIn(env_name, readme)
                self.assertIn(env_name, env_example)

    def test_test_file_does_not_read_real_secret_files(self) -> None:
        source = Path(__file__).read_text(encoding="utf-8")

        forbidden_patterns = (
            "load_" + "settings()",
            'open("' + '.env"',
            "QQBOT_" + "SECRET=",
            "DEEPSEEK_" + "API_KEY=",
        )
        for pattern in forbidden_patterns:
            with self.subTest(pattern=pattern):
                self.assertNotIn(pattern, source)


if __name__ == "__main__":
    unittest.main()
