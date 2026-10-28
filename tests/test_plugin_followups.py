import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.chat_cli import local_session, send_message
from storage.chat_history_store import init_chat_history_table, list_recent_messages


class PluginFollowupTest(unittest.TestCase):
    def test_food_result_reaches_next_chat_and_stays_in_its_session(self):
        with tempfile.TemporaryDirectory() as temp, local_session(fake=True, data_dir=Path(temp)) as (settings, model):
            async def run():
                reply = await send_message(settings, "/吃什么 不辣，20元以内")
                await send_message(settings, "刚才推荐的是什么")
                messages = model.requests[-1]
                self.assertEqual(messages[1]["content"], "不辣，20元以内")
                self.assertEqual(messages[2]["content"], reply)
                await send_message(settings, "刚才推荐的是什么", user="other")
                self.assertEqual(len(model.requests[-1]), 2)
                await send_message(settings, "刚才推荐的是什么", group="g")
                self.assertEqual(len(model.requests[-1]), 2)
                await send_message(settings, "/清空上下文")
                await send_message(settings, "刚才推荐的是什么")
                self.assertEqual(len(model.requests[-1]), 2)
            asyncio.run(run())

    def test_knowledge_result_and_source_reach_followup(self):
        with tempfile.TemporaryDirectory() as temp, local_session(fake=True, data_dir=Path(temp), rag=True) as (settings, model):
            async def run():
                reply = await send_message(settings, "/知识问 本地安装与启动")
                self.assertIn("setup.md", reply)
                await send_message(settings, "这个说明的来源是什么？")
                messages = model.requests[-1]
                self.assertIn("setup.md", messages[2]["content"])
                self.assertEqual(messages[1]["content"], "本地安装与启动")
            asyncio.run(run())

    def test_model_failure_is_not_saved_as_a_knowledge_answer(self):
        with tempfile.TemporaryDirectory() as temp, local_session(fake=True, data_dir=Path(temp), rag=True) as (settings, _):
            init_chat_history_table()
            with patch("services.rag_service.DeepSeekService.chat_messages", return_value="DeepSeek 请求失败，请稍后再试。"):
                asyncio.run(send_message(settings, "/知识问 本地安装与启动"))
            self.assertEqual(list_recent_messages("private", "cli-user", "cli-user", 10), [])

    def test_no_result_reply_is_remembered_as_a_refusal(self):
        with tempfile.TemporaryDirectory() as temp, local_session(fake=True, data_dir=Path(temp), rag=True) as (settings, model):
            async def run():
                reply = await send_message(settings, "/知识问 火星酒店电话号码")
                self.assertIn("无法确定", reply)
                await send_message(settings, "刚才查到了吗？")
                self.assertIn("无法确定", model.requests[-1][2]["content"])
            asyncio.run(run())
