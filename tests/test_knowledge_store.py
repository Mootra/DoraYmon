from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from storage.knowledge_store import (
    discover_documents,
    get_knowledge_status,
    rebuild_knowledge_index,
    search_knowledge,
    split_document,
)


class KnowledgeStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.knowledge_dir = self.root / "knowledge"
        self.knowledge_dir.mkdir()
        self.db_path = self.root / "knowledge.db"
        self.connection_patch = patch(
            "storage.knowledge_store.get_connection",
            side_effect=self._get_connection,
        )
        self.connection_patch.start()

    def tearDown(self) -> None:
        self.connection_patch.stop()
        self.temp_dir.cleanup()

    def _get_connection(self, name: str) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _write(self, relative_path: str, content: str) -> None:
        path = self.knowledge_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_discovers_markdown_and_text_with_scope_metadata(self) -> None:
        self._write("public.md", "# 公共说明\n\n公共内容")
        self._write("groups/group-a/rules.txt", "群规则")
        self._write("users/user-a/private.md", "# 私人说明\n\n私人内容")
        self._write("ignored.json", "{}")

        documents = discover_documents(self.knowledge_dir)

        self.assertEqual(len(documents), 3)
        by_path = {document.source_path: document for document in documents}
        self.assertEqual(by_path["public.md"].scope_type, "public")
        self.assertEqual(by_path["groups/group-a/rules.txt"].scope_type, "group")
        self.assertEqual(by_path["groups/group-a/rules.txt"].scope_openid, "group-a")
        self.assertEqual(by_path["users/user-a/private.md"].scope_type, "private")
        self.assertEqual(by_path["users/user-a/private.md"].scope_openid, "user-a")

    def test_split_document_preserves_heading_and_overlap(self) -> None:
        content = "# 标题\n\n" + ("第一段内容" * 30) + "\n\n## 第二节\n\n第二节内容"

        chunks = split_document(content, max_chars=120, overlap_chars=20)

        self.assertGreaterEqual(len(chunks), 3)
        self.assertEqual(chunks[0].heading, "标题")
        self.assertEqual(chunks[-1].heading, "第二节")
        self.assertIn(chunks[0].content[-20:], chunks[1].content)

    def test_rebuild_and_search_chinese_with_trigram_bm25(self) -> None:
        self._write(
            "deploy.md",
            "# 项目部署说明\n\n知识库部署需要先运行离线索引脚本。",
        )
        self._write("plugin.md", "# 插件开发规范\n\n插件需要注册到路由。")

        status = rebuild_knowledge_index(self.knowledge_dir, tokenizer="trigram")
        results = search_knowledge("知识库部署怎么做", limit=3)

        self.assertEqual(status.document_count, 2)
        self.assertEqual(status.chunk_count, 2)
        self.assertTrue(results)
        self.assertEqual(results[0].title, "项目部署说明")
        self.assertLessEqual(results[0].score, 0)

    def test_two_character_query_uses_safe_like_fallback(self) -> None:
        self._write("deploy.md", "# 项目部署说明\n\n部署前先安装依赖。")
        rebuild_knowledge_index(self.knowledge_dir)

        results = search_knowledge("部署？")

        self.assertEqual([result.title for result in results], ["项目部署说明"])

    def test_long_query_falls_back_to_multiple_two_character_matches(self) -> None:
        self._write(
            "context.md",
            "# 短期上下文\n\n清空上下文只清除当前会话，不影响其他聊天。",
        )
        self._write("unrelated.md", "# 其他说明\n\n当前版本支持普通聊天。")
        rebuild_knowledge_index(self.knowledge_dir)

        results = search_knowledge("怎样只清空当前聊天会话", limit=3)

        self.assertEqual([result.title for result in results], ["短期上下文"])
        self.assertLessEqual(results[0].score, -3)

    def test_long_like_fallback_rejects_a_single_generic_match(self) -> None:
        self._write("status.md", "# 运行状态\n\n当前机器人运行正常。")
        rebuild_knowledge_index(self.knowledge_dir)

        results = search_knowledge("我的宠物现在是多少级", limit=3)

        self.assertEqual(results, [])

    def test_long_like_fallback_accepts_two_distinct_key_matches(self) -> None:
        self._write(
            "context.md",
            "# 会话设置\n\n聊天上下文支持闲置失效。",
        )
        rebuild_knowledge_index(self.knowledge_dir)

        results = search_knowledge("聊天记录闲置多久会自动过期", limit=3)

        self.assertEqual([result.title for result in results], ["会话设置"])
        self.assertLessEqual(results[0].score, -2)

    def test_long_like_fallback_keeps_scope_isolation(self) -> None:
        self._write(
            "groups/group-a/context.md",
            "# 群上下文\n\n清空上下文只清除当前群聊会话。",
        )
        rebuild_knowledge_index(self.knowledge_dir)

        allowed = search_knowledge(
            "怎样只清空当前聊天会话",
            group_openid="group-a",
        )
        denied = search_knowledge(
            "怎样只清空当前聊天会话",
            group_openid="group-b",
        )

        self.assertEqual([result.title for result in allowed], ["群上下文"])
        self.assertEqual(denied, [])

    def test_query_syntax_characters_do_not_escape_generated_fts_query(self) -> None:
        self._write("deploy.md", "# 项目部署说明\n\n知识库部署流程。")
        rebuild_knowledge_index(self.knowledge_dir)

        results = search_knowledge('知识库部署" OR NOT (secret*)')

        self.assertTrue(results)

    def test_public_group_and_private_scopes_are_isolated(self) -> None:
        self._write("public.md", "# 公共知识\n\n公共知识内容")
        self._write("groups/group-a/group.md", "# 群知识\n\n群组专属暗号 alpha")
        self._write("users/user-a/private.md", "# 私人知识\n\n私人专属暗号 bravo")
        rebuild_knowledge_index(self.knowledge_dir)

        group_a = search_knowledge("群组专属暗号", group_openid="group-a", user_openid="user-b")
        group_b = search_knowledge("群组专属暗号", group_openid="group-b", user_openid="user-b")
        user_a = search_knowledge("私人专属暗号", user_openid="user-a")
        user_b = search_knowledge("私人专属暗号", user_openid="user-b")

        self.assertEqual([result.title for result in group_a], ["群知识"])
        self.assertEqual(group_b, [])
        self.assertEqual([result.title for result in user_a], ["私人知识"])
        self.assertEqual(user_b, [])

    def test_status_reports_document_chunk_time_and_tokenizer(self) -> None:
        self._write("one.md", "# 文档一\n\n第一段")

        rebuild_knowledge_index(self.knowledge_dir, tokenizer="trigram")
        status = get_knowledge_status()

        self.assertEqual(status.document_count, 1)
        self.assertEqual(status.chunk_count, 1)
        self.assertNotEqual(status.last_indexed_at, "未建立")
        self.assertEqual(status.tokenizer, "trigram")


if __name__ == "__main__":
    unittest.main()
