"""P0: /status reports delivery outcomes without exposing message text."""

from __future__ import annotations

import unittest
from datetime import datetime

from doraymon.config import Settings
from doraymon.context import BotContext
from plugins import status
from services.send_result import ERROR_HTTP_FORMAT, SEND_LOG, SendResult


class StatusDeliveryReportTest(unittest.TestCase):
    def setUp(self) -> None:
        SEND_LOG.clear()

    def tearDown(self) -> None:
        SEND_LOG.clear()

    def _context(self) -> BotContext:
        return BotContext(
            settings=Settings(chat_history_enabled=True),
            started_at=datetime.now(),
            user_openid="user-a",
            platform="qqbot",
            bot_account="bot-1",
        )

    def _session(self) -> tuple[str, str, str]:
        return ("private", "qqbot:bot-1:user-a", "qqbot:bot-1:user-a")

    def test_status_reports_no_records_before_any_send(self) -> None:
        reply = status.handle(self._context())

        self.assertIn("DoraYmon 运行中", reply)
        self.assertIn("还没有发送记录", reply)

    def test_status_counts_delivered_and_failed_segments(self) -> None:
        SEND_LOG.record(
            SendResult(
                scope_type="private",
                session_key=self._session(),
                segment_index=1,
                success=True,
                recorded_at=datetime.now(),
            )
        )
        SEND_LOG.record(
            SendResult(
                scope_type="private",
                session_key=self._session(),
                segment_index=2,
                success=False,
                recorded_at=datetime.now(),
                error_label=ERROR_HTTP_FORMAT.format(status=500),
            )
        )

        reply = status.handle(self._context())

        self.assertIn("成功 1 段，失败 1 段", reply)
        self.assertIn("最近一次发送失败：接口返回 500", reply)

    def test_status_never_echoes_message_content(self) -> None:
        SEND_LOG.record(
            SendResult(
                scope_type="private",
                session_key=self._session(),
                segment_index=1,
                success=False,
                recorded_at=datetime.now(),
                error_label="接口返回 500",
            )
        )

        reply = status.handle(self._context())

        self.assertNotIn("secret", reply)
        self.assertNotIn("打开", reply)

    def test_status_without_identity_reports_that_clearly(self) -> None:
        context = self._context()
        context.user_openid = ""

        reply = status.handle(context)

        self.assertIn("无法识别当前会话", reply)


if __name__ == "__main__":
    unittest.main()
