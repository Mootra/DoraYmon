from __future__ import annotations

from pathlib import Path
import tempfile
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import Mock, patch

from doraymon.config import Settings
from services.context_builder import build_context
from services.summary_service import generate_summary, update_persisted_summary
from storage.chat_history_store import clear_messages, init_chat_history_table, save_turn
from storage.chat_summary_store import (
    clear_summary,
    get_summary,
    init_chat_summary_table,
    save_summary_if_current,
)
from storage.db import get_connection, use_data_directory


class ChatSummaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_context = use_data_directory(Path(self.temp_dir.name))
        self.data_context.__enter__()
        init_chat_history_table()
        init_chat_summary_table()

    def tearDown(self) -> None:
        self.data_context.__exit__(None, None, None)
        self.temp_dir.cleanup()

    def test_summary_is_saved_and_retrieved_for_one_session(self):
        _, assistant = save_turn("private", "user-a", "user-a", "旧问题", "旧回答")

        self.assertTrue(
            save_summary_if_current(
                "private", "user-a", "user-a", assistant.id, "用户正在讨论旧问题。"
            )
        )
        summary = get_summary("private", "user-a", "user-a")
        self.assertIsNotNone(summary)
        self.assertEqual(summary.summary, "用户正在讨论旧问题。")
        self.assertEqual(summary.covered_through_id, assistant.id)

    def test_stale_summary_cannot_overwrite_newer_messages_or_survive_clear(self):
        _, assistant = save_turn("private", "user-a", "user-a", "旧问题", "旧回答")
        self.assertTrue(
            save_summary_if_current(
                "private", "user-a", "user-a", assistant.id, "旧摘要"
            )
        )
        save_turn("private", "user-a", "user-a", "新问题", "新回答")

        self.assertFalse(
            save_summary_if_current(
                "private", "user-a", "user-a", assistant.id, "过时摘要"
            )
        )
        self.assertEqual(get_summary("private", "user-a", "user-a").summary, "旧摘要")
        self.assertEqual(clear_summary("private", "user-a", "user-a"), 1)
        self.assertIsNone(get_summary("private", "user-a", "user-a"))

    def test_expired_summary_is_removed_and_not_returned(self):
        _, assistant = save_turn("private", "user-a", "user-a", "问题", "回答")
        save_summary_if_current("private", "user-a", "user-a", assistant.id, "旧摘要")
        with closing(get_connection("doraymon")) as connection:
            connection.execute(
                "UPDATE chat_summaries SET expires_at = datetime('now', '-1 day')"
            )
            connection.commit()

        self.assertIsNone(get_summary("private", "user-a", "user-a"))

    def test_empty_or_cleared_session_rejects_summary(self):
        session = ("private", "user-a", "user-a")
        self.assertFalse(save_summary_if_current(*session, 0, "无历史摘要"))
        _, assistant = save_turn(*session, "问题", "回答")
        clear_messages(*session)
        clear_summary(*session)
        self.assertFalse(save_summary_if_current(*session, assistant.id, "迟到摘要"))
        self.assertIsNone(get_summary(*session))

    def test_expiry_cleanup_preserves_a_concurrent_refresh(self):
        session = ("private", "user-a", "user-a")
        _, assistant = save_turn(*session, "问题", "回答")
        save_summary_if_current(*session, assistant.id, "旧摘要")
        with closing(get_connection("doraymon")) as connection:
            connection.execute("UPDATE chat_summaries SET expires_at = datetime('now', '-1 day')")
            connection.commit()

        class RefreshingConnection:
            def __init__(self):
                self.connection = get_connection("doraymon")

            def execute(inner, sql, parameters=()):
                cursor = inner.connection.execute(sql, parameters)
                if "SELECT scope_type" in sql:
                    row = cursor.fetchone()
                    with closing(get_connection("doraymon")) as writer:
                        writer.execute(
                            "UPDATE chat_summaries SET summary = ?, expires_at = datetime('now', '+7 days')",
                            ("新摘要",),
                        )
                        writer.commit()
                    return Mock(fetchone=Mock(return_value=row))
                return cursor

            def commit(inner):
                inner.connection.commit()

            def close(inner):
                inner.connection.close()

        with patch("storage.chat_history_store.get_connection", side_effect=lambda _: RefreshingConnection()):
            self.assertIsNone(get_summary(*session))
        self.assertEqual(get_summary(*session).summary, "新摘要")

    def test_clear_cannot_interleave_summary_validation_and_write(self):
        session = ("private", "user-a", "user-a")
        _, assistant = save_turn(*session, "问题", "回答")
        attempts = []
        original_connect = get_connection

        class CheckedConnection:
            def __init__(self):
                self.connection = original_connect("doraymon")

            def execute(inner, sql, parameters=()):
                cursor = inner.connection.execute(sql, parameters)
                if "SELECT COALESCE(MAX(id)" in sql:
                    row = cursor.fetchone()
                    # A second real connection attempts a clear at the exact
                    # boundary between checking the history and writing.
                    with closing(original_connect("doraymon")) as competing:
                        competing.execute("PRAGMA busy_timeout = 0")
                        try:
                            competing.execute("DELETE FROM chat_messages")
                            competing.commit()
                        except sqlite3.OperationalError as error:
                            if "locked" not in str(error):
                                raise
                            attempts.append("locked")
                        else:
                            attempts.append("cleared")
                    return Mock(fetchone=Mock(return_value=row))
                return cursor

            def commit(inner):
                inner.connection.commit()

            def close(inner):
                inner.connection.close()

        with patch("storage.chat_history_store.get_connection", side_effect=lambda _: CheckedConnection()):
            self.assertTrue(save_summary_if_current(*session, assistant.id, "摘要"))
        self.assertEqual(attempts, ["locked"])
        clear_messages(*session)
        clear_summary(*session)
        self.assertIsNone(get_summary(*session))

    def test_model_summary_uses_low_temperature_and_falls_back_on_error(self):
        _, assistant = save_turn("private", "user-a", "user-a", "问题", "回答")
        history = [
            type(assistant)(assistant.id - 1, "private", "user-a", "user-a", "user", "问题", assistant.created_at),
            assistant,
        ]
        model = Mock()
        model.chat_messages.return_value = "模型摘要"

        self.assertEqual(generate_summary(history, model, 100), "模型摘要")
        self.assertEqual(model.chat_messages.call_args.kwargs["temperature"], 0.2)

        model.chat_messages.return_value = "DeepSeek 请求失败，请稍后再试。"
        fallback = generate_summary(history, model, 100)
        self.assertIn("较早对话摘要", fallback)

    def test_background_update_rejects_a_summary_when_the_session_changed(self):
        _, assistant = save_turn("private", "user-a", "user-a", "问题", "回答")
        history = [
            type(assistant)(assistant.id - 1, "private", "user-a", "user-a", "user", "问题", assistant.created_at),
            assistant,
        ]
        save_turn("private", "user-a", "user-a", "新问题", "新回答")
        model = Mock()
        model.chat_messages.return_value = "不应回写"

        self.assertFalse(
            update_persisted_summary(
                Settings(chat_summary_retention_days=7),
                ("private", "user-a", "user-a"),
                history,
                model,
            )
        )
        self.assertIsNone(get_summary("private", "user-a", "user-a"))

    def test_persisted_summary_is_included_in_conversation_context(self):
        context = build_context(
            Settings(),
            "继续",
            scene="private",
            user_openid="user-a",
            summary="用户已经决定使用 SQLite 保存短期摘要。",
        )

        self.assertIn("用户已经决定使用 SQLite 保存短期摘要。", context.summary)


if __name__ == "__main__":
    unittest.main()
