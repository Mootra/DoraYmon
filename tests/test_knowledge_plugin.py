from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from unittest.mock import patch

from doraymon.config import Settings
from doraymon.context import BotContext
from doraymon.router import COMMANDS
from plugins import knowledge
from storage.knowledge_store import KnowledgeIndexStatus


class KnowledgePluginTest(unittest.TestCase):
    def _context(
        self,
        args: str = "如何部署",
        rag_enabled: bool = True,
        user_openid: str = "user-a",
        admin_openids: list[str] | None = None,
    ) -> BotContext:
        return BotContext(
            settings=Settings(
                rag_enabled=rag_enabled,
                admin_openids=admin_openids or [],
            ),
            started_at=datetime.now(),
            user_openid=user_openid,
            raw_content=f"/知识问 {args}",
            args=args,
        )

    def test_knowledge_commands_are_registered(self) -> None:
        for command in ("知识问", "知识库状态", "知识来源", "重建知识库"):
            with self.subTest(command=command):
                self.assertIn(command, COMMANDS)

    def test_ask_requires_feature_flag(self) -> None:
        reply = asyncio.run(knowledge.handle_ask(self._context(rag_enabled=False)))

        self.assertIn("BOT_ENABLE_RAG=true", reply)

    def test_rebuild_requires_admin(self) -> None:
        reply = asyncio.run(knowledge.handle_rebuild(self._context()))

        self.assertIn("没有权限", reply)

    @patch("plugins.knowledge.rebuild_knowledge_index")
    def test_admin_can_rebuild_and_receives_counts(self, rebuild_mock) -> None:
        rebuild_mock.return_value = KnowledgeIndexStatus(2, 5, "now", "trigram")
        context = self._context(user_openid="admin-a", admin_openids=["admin-a"])

        reply = asyncio.run(knowledge.handle_rebuild(context))

        self.assertIn("文档数：2", reply)
        self.assertIn("分块数：5", reply)
        rebuild_mock.assert_called_once()

    @patch("plugins.knowledge.get_knowledge_status")
    def test_status_does_not_expose_document_content(self, status_mock) -> None:
        status_mock.return_value = KnowledgeIndexStatus(2, 5, "now", "trigram")

        reply = knowledge.handle_status(self._context())

        self.assertIn("文档数：2", reply)
        self.assertIn("SQLite FTS5 / trigram", reply)
        self.assertNotIn("完整私人问题", reply)


if __name__ == "__main__":
    unittest.main()
