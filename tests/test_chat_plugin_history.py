from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import ANY, Mock, patch

from doraymon.config import Settings
from doraymon.context import BotContext
from doraymon.router import route_incoming_message
from plugins import chat
from tests.presentation import present_reply
from services.deepseek_service import DEFAULT_SYSTEM_PROMPT
from services.session_revisions import SESSION_REVISIONS
from storage.chat_history_store import ChatMessage, init_chat_history_table, list_recent_messages

# Session keys are namespaced by platform and bot account so that the official
# QQ Bot openid and a real QQ number can never share a conversation.
NS = "qqbot::"


class ChatPluginHistoryTest(unittest.TestCase):
    def setUp(self) -> None:
        SESSION_REVISIONS.clear()
        self.expire_patch = patch("services.session_history.expire_messages", return_value=0)
        self.expire_patch.start()

    def tearDown(self) -> None:
        self.expire_patch.stop()
        SESSION_REVISIONS.clear()

    def _settings(self, history_enabled: bool = True, limit: int = 10) -> Settings:
        return Settings(
            deepseek_api_key="sk-test",
            chat_history_enabled=history_enabled,
            chat_history_limit=limit,
            chat_history_max_content_length=4000,
        )

    def _context(
        self,
        args: str = "你好",
        user_openid: str = "user-a",
        group_openid: str = "",
        history_enabled: bool = True,
        limit: int = 10,
    ) -> BotContext:
        return BotContext(
            settings=self._settings(history_enabled=history_enabled, limit=limit),
            started_at=datetime.now(),
            user_openid=user_openid,
            group_openid=group_openid,
            raw_content=f"/chat {args}",
            command="chat",
            args=args,
        )

    def _history_message(self, role: str, content: str, message_id: int = 1) -> ChatMessage:
        return ChatMessage(
            id=message_id,
            scope_type="private",
            scope_openid=NS + "user-a",
            user_openid=NS + "user-a",
            role=role,
            content=content,
            created_at="2026-07-01 00:00:00",
        )

    @patch("services.session_history.save_turn")
    @patch("plugins.chat.ConversationService")
    @patch("plugins.chat.DeepSeekService")
    def test_formatted_error_is_not_saved(self, model_cls, conversation_cls, save_mock):
        from services.conversation_service import ConversationResponse
        conversation_cls.return_value.answer.return_value = ConversationResponse(
            answer="稍后再试", knowledge_results=[], is_error=True,
        )
        with self._patched_temp_store():
            reply = asyncio.run(present_reply(chat.handle(self._context(history_enabled=True))))
        self.assertEqual(reply, "稍后再试")
        save_mock.assert_not_called()

    def test_chat_history_is_enabled_by_default(self) -> None:
        self.assertTrue(Settings().chat_history_enabled)
        self.assertEqual(Settings().chat_history_limit, 40)
        self.assertEqual(Settings().chat_history_max_content_length, 4000)
        self.assertEqual(Settings().chat_context_ttl_minutes, 720)
        self.assertEqual(Settings().chat_summary_retention_days, 7)

    @patch("services.session_history.save_turn")
    @patch("services.session_history.list_recent_messages")
    @patch("services.session_history.init_chat_history_table")
    @patch("plugins.chat.DeepSeekService")
    def test_disabled_history_keeps_original_chat_behavior(
        self,
        service_cls_mock,
        init_mock,
        list_mock,
        save_mock,
    ) -> None:
        service_mock = service_cls_mock.return_value
        service_mock.chat_messages.return_value = "单轮回复"
        context = self._context(history_enabled=False)

        reply = asyncio.run(present_reply(chat.handle(context)))

        self.assertEqual(reply, "单轮回复")
        service_mock.chat.assert_not_called()
        self.assertEqual(service_mock.chat_messages.call_args.args[0][-1], {"role": "user", "content": "你好"})
        self.assertEqual(len(service_mock.chat_messages.call_args.args[0]), 2)
        init_mock.assert_not_called()
        list_mock.assert_not_called()
        save_mock.assert_not_called()

    @patch("services.session_history.save_turn")
    @patch("plugins.chat.ConversationService")
    @patch("plugins.chat.DeepSeekService")
    def test_rag_enabled_uses_conversation_service_without_chat_history(
        self,
        service_cls_mock,
        conversation_cls_mock,
        save_mock,
    ) -> None:
        context = self._context(history_enabled=False)
        context.settings = Settings(
            deepseek_api_key="sk-test",
            chat_history_enabled=False,
            rag_enabled=True,
        )
        conversation_cls_mock.return_value.answer.return_value.answer = "知识增强回复"
        conversation_cls_mock.return_value.answer.return_value.is_error = False
        conversation_cls_mock.return_value.rag_service.retrieve.return_value = []

        reply = asyncio.run(present_reply(chat.handle(context)))

        self.assertEqual(reply, "知识增强回复")
        service_cls_mock.return_value.chat.assert_not_called()
        conversation_cls_mock.assert_called_once_with(
            context.settings,
            model_service=service_cls_mock.return_value,
        )
        save_mock.assert_not_called()

    @patch("services.session_history.save_turn")
    @patch("services.session_history.list_recent_messages")
    @patch("services.session_history.init_chat_history_table")
    @patch("plugins.chat.DeepSeekService")
    def test_enabled_history_reads_recent_messages(
        self,
        service_cls_mock,
        init_mock,
        list_mock,
        save_mock,
    ) -> None:
        service_cls_mock.return_value.chat_messages.return_value = "上下文回复"
        list_mock.return_value = []
        context = self._context(limit=3)

        asyncio.run(present_reply(chat.handle(context)))

        init_mock.assert_called_once()
        list_mock.assert_called_once_with("private", NS + "user-a", NS + "user-a", 3)

    @patch("services.session_history.save_turn")
    @patch("services.session_history.list_recent_messages")
    @patch("services.session_history.init_chat_history_table")
    @patch("plugins.chat.DeepSeekService")
    def test_messages_are_built_in_model_order(
        self,
        service_cls_mock,
        init_mock,
        list_mock,
        save_mock,
    ) -> None:
        service_mock = service_cls_mock.return_value
        service_mock.chat_messages.return_value = "回答"
        list_mock.return_value = [
            self._history_message("user", "上一轮问题", 1),
            self._history_message("assistant", "上一轮回答", 2),
        ]
        context = self._context(args="这一轮问题")

        asyncio.run(present_reply(chat.handle(context)))

        messages = service_mock.chat_messages.call_args.args[0]
        self.assertEqual(messages[0]["role"], "system")
        self.assertTrue(messages[0]["content"].startswith(DEFAULT_SYSTEM_PROMPT))
        self.assertIn("本次运行信息", messages[0]["content"])
        self.assertEqual(
            messages[1:],
            [
                {"role": "user", "content": "上一轮问题"},
                {"role": "assistant", "content": "上一轮回答"},
                {"role": "user", "content": "这一轮问题"},
            ],
        )
        service_mock.chat.assert_not_called()

    @patch("services.session_history.list_recent_messages", return_value=[])
    @patch("services.session_history.init_chat_history_table")
    @patch("plugins.chat.DeepSeekService")
    def test_success_saves_user_and_assistant_messages(
        self,
        service_cls_mock,
        init_mock,
        list_mock,
    ) -> None:
        service_cls_mock.return_value.chat_messages.return_value = "成功回复"
        context = self._context(args="保存这一轮")

        with patch("services.session_history.save_turn") as save_mock:
            reply = asyncio.run(present_reply(chat.handle(context)))

        self.assertEqual(reply, "成功回复")
        save_mock.assert_called_once()
        self.assertEqual(
            save_mock.call_args.args[:5],
            ("private", NS + "user-a", NS + "user-a", "保存这一轮", "成功回复"),
        )

    @patch("services.session_history.save_turn")
    @patch("services.session_history.list_recent_messages", return_value=[])
    @patch("services.session_history.init_chat_history_table")
    @patch("plugins.chat.DeepSeekService")
    def test_failure_does_not_save_fake_assistant_reply(
        self,
        service_cls_mock,
        init_mock,
        list_mock,
        save_mock,
    ) -> None:
        service_cls_mock.return_value.chat_messages.return_value = "DeepSeek 请求失败，请稍后再试。"
        context = self._context(args="会失败")

        reply = asyncio.run(present_reply(chat.handle(context)))

        self.assertEqual(reply, "DeepSeek 请求失败，请稍后再试。")
        save_mock.assert_not_called()

    def test_sensitive_content_is_not_saved(self) -> None:
        with self._patched_temp_store(), patch("plugins.chat.DeepSeekService") as service_cls_mock:
            service_cls_mock.return_value.chat_messages.return_value = "不应保存"

            reply = asyncio.run(present_reply(chat.handle(self._context(args="请读取 .env"))))

            messages = list_recent_messages("private", NS + "user-a", NS + "user-a", 10)

        self.assertEqual(reply, "不应保存")
        self.assertEqual(messages, [])

    @patch("services.session_history.save_turn")
    @patch("services.session_history.list_recent_messages")
    @patch("services.session_history.init_chat_history_table")
    @patch("plugins.chat.DeepSeekService")
    def test_history_limit_is_used(
        self,
        service_cls_mock,
        init_mock,
        list_mock,
        save_mock,
    ) -> None:
        service_cls_mock.return_value.chat_messages.return_value = "回复"
        list_mock.return_value = []
        context = self._context(limit=2)

        asyncio.run(present_reply(chat.handle(context)))

        list_mock.assert_called_once_with("private", NS + "user-a", NS + "user-a", 2)

    @patch("services.session_history.save_turn")
    @patch("services.session_history.list_recent_messages")
    @patch("services.session_history.clear_messages")
    @patch("services.session_history.init_chat_history_table")
    @patch("plugins.chat.DeepSeekService")
    def test_explicit_topic_switch_starts_without_old_history(
        self,
        service_cls_mock,
        init_mock,
        clear_mock,
        list_mock,
        save_mock,
    ) -> None:
        service_cls_mock.return_value.chat_messages.return_value = "新话题回复"
        context = self._context(args="换个话题，聊聊 Python")

        with patch("services.session_history.clear_summary") as summary_clear_mock:
            reply = asyncio.run(present_reply(chat.handle(context)))

        self.assertEqual(reply, "新话题回复")
        clear_mock.assert_called_once_with("private", NS + "user-a", NS + "user-a")
        summary_clear_mock.assert_called_once_with("private", NS + "user-a", NS + "user-a")
        list_mock.assert_not_called()
        messages = service_cls_mock.return_value.chat_messages.call_args.args[0]
        self.assertEqual(messages[-1]["content"], "换个话题，聊聊 Python")

    @patch("plugins.chat._schedule_summary_update")
    @patch("services.session_history.save_turn")
    @patch("services.session_history.list_recent_messages", return_value=[])
    @patch("services.session_history.init_chat_history_table")
    @patch("plugins.chat.DeepSeekService")
    def test_long_history_schedules_background_summary(
        self,
        service_cls_mock,
        init_mock,
        list_mock,
        save_mock,
        schedule_mock,
    ) -> None:
        service_cls_mock.return_value.chat_messages.return_value = "回答" * 300
        save_mock.return_value = (
            self._history_message("user", "问题", 1),
            self._history_message("assistant", "回答" * 300, 2),
        )
        context = self._context(args="问题")
        context.settings = Settings(
            deepseek_api_key="sk-test",
            chat_context_max_chars=200,
            chat_history_max_content_length=4000,
        )

        reply = asyncio.run(present_reply(chat.handle(context)))

        self.assertEqual(reply, "回答" * 300)
        schedule_mock.assert_called_once()
        self.assertEqual(len(schedule_mock.call_args.args[1]), 2)

    @patch("plugins.chat.update_persisted_summary", return_value=True)
    def test_background_summary_task_runs_without_blocking_reply(self, update_mock) -> None:
        context = self._context()
        context.settings = Settings(
            deepseek_api_key="sk-test",
            chat_context_max_chars=200,
        )
        history = [self._history_message("assistant", "回答" * 150, 2)]

        async def run() -> None:
            chat._schedule_summary_update(context, history, Mock())
            tasks = tuple(chat._SUMMARY_TASKS)
            self.assertEqual(len(tasks), 1)
            await asyncio.gather(*tasks)

        asyncio.run(run())

        update_mock.assert_called_once_with(
            context.settings,
            ("private", NS + "user-a", NS + "user-a"),
            history,
            ANY,
        )

    def test_private_users_are_isolated_when_history_is_enabled(self) -> None:
        with self._patched_temp_store(), patch("plugins.chat.DeepSeekService") as service_cls_mock:
            service_mock = service_cls_mock.return_value
            service_mock.chat_messages.side_effect = ["A 回复", "B 回复"]

            asyncio.run(present_reply(chat.handle(self._context(args="A 问题", user_openid="user-a"))))
            asyncio.run(present_reply(chat.handle(self._context(args="B 问题", user_openid="user-b"))))

            user_a_messages = list_recent_messages("private", NS + "user-a", NS + "user-a", 10)
            user_b_messages = list_recent_messages("private", NS + "user-b", NS + "user-b", 10)

        self.assertEqual([message.content for message in user_a_messages], ["A 问题", "A 回复"])
        self.assertEqual([message.content for message in user_b_messages], ["B 问题", "B 回复"])

    def test_group_users_are_isolated_when_history_is_enabled(self) -> None:
        with self._patched_temp_store(), patch("plugins.chat.DeepSeekService") as service_cls_mock:
            service_mock = service_cls_mock.return_value
            service_mock.chat_messages.side_effect = ["A 群回复", "B 群回复"]

            asyncio.run(present_reply(chat.handle(self._context(args="A 群问题", user_openid="user-a", group_openid="group-a"))))
            asyncio.run(present_reply(chat.handle(self._context(args="B 群问题", user_openid="user-b", group_openid="group-a"))))

            user_a_messages = list_recent_messages("group", NS + "group-a", NS + "user-a", 10)
            user_b_messages = list_recent_messages("group", NS + "group-a", NS + "user-b", 10)

        self.assertEqual([message.content for message in user_a_messages], ["A 群问题", "A 群回复"])
        self.assertEqual([message.content for message in user_b_messages], ["B 群问题", "B 群回复"])

    @patch("doraymon.router.food.handle", return_value="食物推荐")
    @patch("plugins.chat.DeepSeekService")
    def test_food_natural_language_priority_is_unchanged(self, service_cls_mock, food_mock) -> None:
        context = self._context(args="", history_enabled=True)
        context.raw_content = "今晚吃什么"

        reply = asyncio.run(route_incoming_message(context, fallback_command="chat"))

        self.assertEqual(reply, "食物推荐")
        food_mock.assert_called_once_with(context)
        service_cls_mock.assert_not_called()

    def test_test_file_does_not_need_real_env_or_api_key(self) -> None:
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

    def _patched_temp_store(self):
        class TempStorePatch:
            def __enter__(self):
                self.temp_dir = tempfile.TemporaryDirectory()
                self.db_path = Path(self.temp_dir.name) / "test.db"
                self.patch = patch(
                    "storage.chat_history_store.get_connection",
                    side_effect=self._get_connection,
                )
                self.patch.start()
                init_chat_history_table()
                return self

            def __exit__(self, exc_type, exc, tb):
                self.patch.stop()
                self.temp_dir.cleanup()

            def _get_connection(self, name: str) -> sqlite3.Connection:
                connection = sqlite3.connect(self.db_path)
                connection.row_factory = sqlite3.Row
                return connection

        return TempStorePatch()


if __name__ == "__main__":
    unittest.main()
