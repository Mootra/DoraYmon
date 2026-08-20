from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import Mock

from doraymon.config import Settings
from services.conversation_service import (
    ConversationService,
    build_context_window,
    build_retrieval_query,
    is_explicit_topic_switch,
    select_complete_history,
    summarize_history,
)
from services.deepseek_service import DEFAULT_SYSTEM_PROMPT
from storage.chat_history_store import ChatMessage
from storage.knowledge_store import KnowledgeSearchResult


class ConversationServiceTest(unittest.TestCase):
    def _message(self, message_id: int, role: str, content: str) -> ChatMessage:
        return ChatMessage(
            id=message_id,
            scope_type="private",
            scope_openid="user-a",
            user_openid="user-a",
            role=role,
            content=content,
            created_at="2026-08-20 00:00:00",
        )

    def _result(self) -> KnowledgeSearchResult:
        return KnowledgeSearchResult(
            chunk_id=1,
            document_id=1,
            source_path="project.md",
            title="项目说明",
            heading="启动",
            content="Windows 下运行 scripts/run_local.ps1。",
            scope_type="public",
            scope_openid="",
            score=-1.0,
        )

    def test_select_complete_history_drops_incomplete_edges(self) -> None:
        history = [
            self._message(1, "assistant", "缺少问题"),
            self._message(2, "user", "完整问题"),
            self._message(3, "assistant", "完整回答"),
            self._message(4, "user", "缺少回答"),
        ]

        selected = select_complete_history(history, max_chars=100)

        self.assertEqual(
            [(message.role, message.content) for message in selected],
            [("user", "完整问题"), ("assistant", "完整回答")],
        )

    def test_select_complete_history_keeps_newest_turns_within_budget(self) -> None:
        history = [
            self._message(1, "user", "旧问题123"),
            self._message(2, "assistant", "旧回答123"),
            self._message(3, "user", "新问题"),
            self._message(4, "assistant", "新回答"),
        ]

        selected = select_complete_history(history, max_chars=6)

        self.assertEqual([message.content for message in selected], ["新问题", "新回答"])

    def test_short_follow_up_expands_retrieval_query_with_previous_question(self) -> None:
        history = [
            self._message(1, "user", "这个项目在 Linux 上怎么启动？"),
            self._message(2, "assistant", "可以运行启动脚本。"),
        ]

        query = build_retrieval_query("Windows 呢？", history)

        self.assertEqual(query, "这个项目在 Linux 上怎么启动？\nWindows 呢？")

    def test_short_greeting_does_not_reuse_previous_topic_for_retrieval(self) -> None:
        history = [
            self._message(1, "user", "这个项目怎么启动？"),
            self._message(2, "assistant", "运行启动脚本。"),
        ]

        query = build_retrieval_query("你好", history)

        self.assertEqual(query, "你好")

    def test_context_window_compresses_older_turns_and_keeps_recent_turn(self) -> None:
        history = [
            self._message(1, "user", "旧问题" * 20),
            self._message(2, "assistant", "旧回答" * 20),
            self._message(3, "user", "新问题" * 15),
            self._message(4, "assistant", "新回答" * 15),
        ]

        window = build_context_window(history, max_chars=200, summary_max_chars=60)

        self.assertEqual(
            [message.content for message in window.recent_history],
            ["新问题" * 15, "新回答" * 15],
        )
        self.assertIn("较早对话摘要", window.older_summary)
        self.assertIn("旧问题", window.older_summary)
        self.assertLessEqual(len(window.older_summary), 60)

    def test_summary_uses_only_complete_turns(self) -> None:
        history = [
            self._message(1, "user", "完整问题"),
            self._message(2, "assistant", "完整回答"),
            self._message(3, "user", "不完整问题"),
        ]

        summary = summarize_history(history)

        self.assertIn("完整问题", summary)
        self.assertNotIn("不完整问题", summary)

    def test_explicit_topic_switch_requires_a_strong_prefix(self) -> None:
        self.assertTrue(is_explicit_topic_switch("换个话题，聊聊 Python"))
        self.assertTrue(is_explicit_topic_switch("忽略之前的对话，重新回答"))
        self.assertFalse(is_explicit_topic_switch("另外这个问题怎么解决？"))

    def test_answer_combines_complete_history_and_optional_knowledge(self) -> None:
        model = Mock()
        model.chat_messages.return_value = "运行启动脚本即可。[1]"
        rag = Mock()
        rag.retrieve.return_value = [self._result()]
        service = ConversationService(
            Settings(rag_enabled=True, chat_context_max_chars=1000),
            model_service=model,
            rag_service=rag,
        )
        history = [
            self._message(1, "user", "这个项目支持 Windows 吗？"),
            self._message(2, "assistant", "支持。"),
        ]

        response = service.answer(
            "那怎么启动？",
            history,
            group_openid="group-a",
            user_openid="user-a",
        )

        messages = model.chat_messages.call_args.args[0]
        self.assertEqual(messages[0], {"role": "system", "content": DEFAULT_SYSTEM_PROMPT})
        self.assertEqual(messages[1]["content"], "这个项目支持 Windows 吗？")
        self.assertIn("那怎么启动？", messages[-1]["content"])
        self.assertIn("可选知识资料", messages[-1]["content"])
        self.assertIn("scripts/run_local.ps1", messages[-1]["content"])
        self.assertIn("参考资料：", response.answer)
        self.assertIn("project.md", response.answer)
        rag.retrieve.assert_called_once_with(
            "这个项目支持 Windows 吗？\n那怎么启动？",
            group_openid="group-a",
            user_openid="user-a",
        )

    def test_answer_injects_local_summary_when_history_exceeds_budget(self) -> None:
        model = Mock()
        model.chat_messages.return_value = "继续回答"
        service = ConversationService(
            Settings(
                chat_context_max_chars=200,
                chat_context_summary_max_chars=60,
            ),
            model_service=model,
        )
        history = [
            self._message(1, "user", "旧问题" * 20),
            self._message(2, "assistant", "旧回答" * 20),
            self._message(3, "user", "新问题" * 15),
            self._message(4, "assistant", "新回答" * 15),
        ]

        service.answer("继续", history)

        messages = model.chat_messages.call_args.args[0]
        self.assertIn("较早对话的本地压缩摘要", messages[0]["content"])
        self.assertIn("旧问题", messages[0]["content"])
        self.assertEqual(messages[1]["content"], "新问题" * 15)

    def test_unused_knowledge_does_not_add_source_footer(self) -> None:
        model = Mock()
        model.chat_messages.return_value = "我需要先确认你说的是哪个项目。"
        rag = Mock()
        rag.retrieve.return_value = [self._result()]
        service = ConversationService(
            Settings(rag_enabled=True),
            model_service=model,
            rag_service=rag,
        )

        response = service.answer("它怎么启动？")

        self.assertNotIn("参考资料：", response.answer)

    def test_knowledge_failure_degrades_to_normal_chat(self) -> None:
        model = Mock()
        model.chat_messages.return_value = "普通聊天回复"
        rag = Mock()
        rag.retrieve.side_effect = sqlite3.OperationalError("fts5 unavailable")
        service = ConversationService(
            Settings(rag_enabled=True),
            model_service=model,
            rag_service=rag,
        )

        response = service.answer("你好")

        messages = model.chat_messages.call_args.args[0]
        self.assertEqual(messages[-1], {"role": "user", "content": "你好"})
        self.assertEqual(response.answer, "普通聊天回复")
        self.assertEqual(response.knowledge_results, [])


if __name__ == "__main__":
    unittest.main()
