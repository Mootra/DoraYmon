"""Real routing/session/storage with synthetic models, providers and send results."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from doraymon.client import MyClient
from doraymon.config import Settings
from doraymon.context import BotContext
from plugins import chat, knowledge
from services.account_session import AccountSession, SKIP_QUEUE_FULL
from services.account_throttle import ConversationThrottle, ThrottlePolicy
from services.blocking_io import BlockingIOBusy, BoundedExecutor
from services.feishu_adapter import FeishuAdapter
from services.meal_session import MEAL_STATES
from services.message_contract import ConversationKind, MessageEvent
from services.nearby_lookup import NearbyLookup, ProviderPlace
from services.pending_reply import PendingReply, commit_delivered_reply
from services.send_result import REJECTED, UNKNOWN, SEND_LOG
from services.session_revisions import SESSION_REVISIONS
from services.session_scheduler import SessionScheduler
from storage.chat_history_store import init_chat_history_table, list_recent_messages
from storage.db import init_all_tables, use_data_directory


def event(text="问题", *, user="alice", message_id="first", platform="qqaccount"):
    return MessageEvent(
        platform=platform, bot_account="bot", kind=ConversationKind.PRIVATE,
        message_id=message_id, raw_user_id=user, text=text,
    )


class Transport:
    def __init__(self, *outcomes):
        self.actions = []
        self.outcomes = iter(outcomes)

    async def send_action(self, action):
        self.actions.append(action)
        result = next(self.outcomes, {"retcode": 0, "code": 0})
        if isinstance(result, BaseException):
            raise result
        return result


class MessageReliabilityTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(use_data_directory(Path(directory)))
        self.stack.enter_context(patch(
            "storage.db.load_settings", side_effect=AssertionError("real settings forbidden"),
        ))
        self.stack.enter_context(patch(
            "requests.sessions.Session.request", side_effect=AssertionError("network forbidden"),
        ))
        init_all_tables()
        init_chat_history_table()
        SESSION_REVISIONS.clear()
        MEAL_STATES.clear()
        SEND_LOG.clear()
        self.settings = Settings(
            food_natural_trigger_enabled=False, chat_history_enabled=True,
            chat_history_limit=20, chat_context_max_chars=10000,
            chat_history_max_content_length=5000,
        )
        self.sessions = []

    async def asyncTearDown(self):
        for session in self.sessions:
            await session.close()
        SESSION_REVISIONS.clear()
        MEAL_STATES.clear()
        SEND_LOG.clear()

    def session(self, transport=None, *, settings=None, **options):
        session = AccountSession(
            settings=settings or self.settings, transport=transport or Transport(),
            bot_account="bot", **options,
            throttle=ConversationThrottle(),
        )
        self.sessions.append(session)
        return session

    def rows(self, incoming=None):
        return list_recent_messages(*(incoming or event()).session_key, 20)

    def model(self, answer="可见回复"):
        service = Mock()
        if callable(answer):
            service.chat_messages.side_effect = answer
        else:
            service.chat_messages.return_value = answer
        self.stack.enter_context(patch("plugins.chat.DeepSeekService", return_value=service))
        return service

    async def test_generation_is_uncommitted_until_delivery_and_commit_is_once(self):
        self.model()
        context = event().to_bot_context(self.settings, datetime.now())
        context.args = "问题"
        reply = await chat.handle(context)
        self.assertIsInstance(reply, PendingReply)
        self.assertEqual(self.rows(), [])
        commit_delivered_reply(reply, "可见回复")
        commit_delivered_reply(reply, "可见回复")
        self.assertEqual([row.content for row in self.rows()], ["问题", "可见回复"])

    async def test_refused_delivery_is_absent_from_the_next_model_context(self):
        model = self.model()
        transport = Transport({"retcode": 1400}, {"retcode": 0})
        session = self.session(transport)
        await session.handle_event(event())
        self.assertEqual(self.rows(), [])
        self.assertEqual(SEND_LOG.results_for(event().session_key)[0].delivery_status, REJECTED)
        await session.handle_event(event("再问一次", message_id="second"))
        messages = model.chat_messages.call_args.args[0]
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        self.assertEqual([row.content for row in self.rows()], ["再问一次", "可见回复"])

    async def test_timeout_stays_unknown_without_history_or_reconnect_resend(self):
        self.model()
        transport = Transport(TimeoutError("synthetic timeout"))
        session = self.session(transport)
        await session.handle_event(event())
        session.on_disconnected()
        session.on_reconnected()
        self.assertEqual(len(transport.actions), 1)
        self.assertEqual(self.rows(), [])
        result = SEND_LOG.results_for(event().session_key)[0]
        self.assertEqual(result.delivery_status, UNKNOWN)
        self.assertEqual(result.event_id, "first")

    async def test_partial_delivery_saves_only_the_acknowledged_prefix(self):
        self.model("甲" * 3600)
        transport = Transport({"retcode": 0}, {"retcode": 1400})
        session = self.session(transport)
        await session.handle_event(event())
        self.assertEqual(len(transport.actions), 2)
        self.assertEqual([row.content for row in self.rows()], ["问题", "甲" * 1800])
        self.assertEqual(session.stats.segments_sent, 1)
        self.assertEqual(session.stats.segments_failed, 1)

    async def test_throttled_turn_does_not_generate_or_commit(self):
        model = self.model()
        session = self.session(private_throttle=ThrottlePolicy(cooldown_seconds=60))
        await session.handle_event(event())
        await session.handle_event(event("被限流的问题", message_id="second"))
        self.assertEqual(model.chat_messages.call_count, 1)
        self.assertEqual([row.content for row in self.rows()], ["问题", "可见回复"])

    async def test_clear_is_available_during_a_reply_cooldown(self):
        self.model()
        session = self.session(private_throttle=ThrottlePolicy(cooldown_seconds=60))
        await session.handle_event(event())
        await session.handle_event(event("/清空上下文", message_id="clear"))
        self.assertEqual(self.rows(), [])
        self.assertIn("已清空", session.transport.actions[-1].params["message"])

    async def test_same_session_is_fifo_and_another_session_can_ping(self):
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()
        requests = []

        def answer(messages, **options):
            requests.append(messages)
            if messages[-1]["content"].startswith("先问甲"):
                loop.call_soon_threadsafe(started.set)
                if not release.wait(5):
                    raise TimeoutError("barrier")
                return "甲的答复"
            return "乙的答复"

        self.model(answer)
        transport = Transport()
        session = self.session(transport)
        first = asyncio.create_task(session.handle_event(event("先问甲")))
        try:
            await asyncio.wait_for(started.wait(), 2)
            second = asyncio.create_task(session.handle_event(event("再问乙", message_id="second")))
            await session.handle_event(event("/ping", user="bob", message_id="ping"))
            self.assertEqual(transport.actions[-1].params["message"], "pong")
            self.assertEqual(len(requests), 1)
        finally:
            release.set()
        await asyncio.gather(first, second)
        self.assertEqual([a.params["message"] for a in transport.actions], ["pong", "甲的答复", "乙的答复"])
        self.assertEqual([m["content"] for m in requests[1][1:]], ["先问甲", "甲的答复", "再问乙"])

    async def test_clear_preempts_generation_and_queued_turns_but_preserves_newer_input(self):
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()

        def answer(messages, **options):
            if messages[-1]["content"].startswith("旧问题"):
                loop.call_soon_threadsafe(started.set)
                release.wait(5)
                return "旧回复"
            self.assertEqual([m["role"] for m in messages], ["system", "user"])
            return "新回复"

        model = self.model(answer)
        transport = Transport()
        session = self.session(transport)
        first = asyncio.create_task(session.handle_event(event("旧问题")))
        try:
            await asyncio.wait_for(started.wait(), 2)
            queued = asyncio.create_task(session.handle_event(event("旧的排队输入", message_id="queued")))
            await asyncio.sleep(0)
            clearing = asyncio.create_task(session.handle_event(event("/清空上下文", message_id="clear")))
            await asyncio.sleep(0)
            newer = asyncio.create_task(session.handle_event(event("新问题", message_id="new")))
            await asyncio.wait_for(asyncio.gather(clearing, newer), 2)
        finally:
            release.set()
        await asyncio.gather(first, queued, return_exceptions=True)
        self.assertEqual(model.chat_messages.call_count, 2)
        self.assertEqual([row.content for row in self.rows()], ["新问题", "新回复"])
        self.assertEqual(len(transport.actions), 2)
        self.assertIn("已清空", transport.actions[0].params["message"])
        self.assertEqual(transport.actions[1].params["message"], "新回复")

    async def test_slow_lookup_runs_off_loop_and_clear_cannot_restore_meal_state(self):
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        started, finished = asyncio.Event(), asyncio.Event()
        release = threading.Event()

        def find(start, **options):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            loop.call_soon_threadsafe(started.set)
            release.wait(5)
            loop.call_soon_threadsafe(finished.set)
            return [ProviderPlace("shop", "合成餐馆", address="合成路")]

        lookup = NearbyLookup(places=SimpleNamespace(search_nearby=find))
        self.stack.enter_context(patch("plugins.food.lookup_for", return_value=lookup))
        settings = replace(
            self.settings, food_natural_trigger_enabled=True, chat_history_enabled=False,
            nearby_provider="amap", nearby_provider_key="synthetic", meal_catalog_dir="",
        )
        transport = Transport()
        session = self.session(transport, settings=settings)
        querying = asyncio.create_task(session.handle_event(event("吃什么")))
        try:
            await asyncio.wait_for(started.wait(), 2)
            await asyncio.wait_for(session.handle_event(event("/ping", user="bob", message_id="ping")), 1)
            await asyncio.wait_for(session.handle_event(event("/清空上下文", message_id="clear")), 1)
            self.assertIsNone(MEAL_STATES.get(event().session_key))
        finally:
            release.set()
        await asyncio.gather(querying, return_exceptions=True)
        await asyncio.wait_for(finished.wait(), 2)
        self.assertIsNone(MEAL_STATES.get(event().session_key))
        self.assertEqual(len(transport.actions), 2)

    async def test_queue_rejection_spends_no_model_call_and_close_cancels_work(self):
        started = asyncio.Event()
        session = self.session(scheduler=SessionScheduler(max_pending=1))

        async def blocked(incoming):
            started.set()
            await asyncio.Event().wait()

        self.stack.enter_context(patch.object(session, "_route", side_effect=blocked))
        task = asyncio.create_task(session.handle_event(event()))
        await started.wait()
        self.assertIsNone(await session.handle_event(event("extra", user="bob")))
        self.assertEqual(session.stats.skipped[SKIP_QUEUE_FULL], 1)
        await session.close()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(session.scheduler.pending, 0)
        self.assertEqual(self.rows(), [])

    async def test_close_during_send_records_unknown_without_committing_history(self):
        self.model()
        started = asyncio.Event()

        class WaitingTransport:
            async def send_action(self, action):
                started.set()
                await asyncio.Event().wait()

        session = self.session(WaitingTransport())
        task = asyncio.create_task(session.handle_event(event()))
        await started.wait()
        await session.close()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(SEND_LOG.results_for(event().session_key)[0].delivery_status, UNKNOWN)
        self.assertEqual(self.rows(), [])
        self.assertEqual(session.scheduler.pending, 0)

    async def test_official_channel_also_commits_only_visible_segments(self):
        self.model("甲" * 3600)
        client = object.__new__(MyClient)
        client.settings = replace(self.settings, qqbot_reply_window_enabled=False)
        client.started_at = datetime.now()
        client.api = SimpleNamespace(post_c2c_message=AsyncMock(side_effect=[{}, TimeoutError()]))
        message = SimpleNamespace(id="official", content="问题", author=SimpleNamespace(user_openid="alice"))
        await client._handle_message(message, is_at_message=False)
        key = ("private", "qqbot::alice", "qqbot::alice")
        self.assertEqual([r.content for r in list_recent_messages(*key, 20)], ["问题", "甲" * 1800])

    async def test_feishu_failed_delivery_has_the_same_history_policy(self):
        self.model()
        transport = Transport({"code": 230001})
        session = self.session(transport, platform="feishu", adapter=FeishuAdapter(bot_account="bot"))
        incoming = event(platform="feishu")
        await session.handle_event(incoming)
        self.assertEqual(self.rows(incoming), [])

    async def test_knowledge_clear_during_query_discards_result(self):
        context = event().to_bot_context(replace(self.settings, rag_enabled=True), datetime.now())
        context.args = "合成知识问题"

        def answer(*args, **options):
            SESSION_REVISIONS.bump(event().session_key)
            return SimpleNamespace(answer="旧知识答复", is_error=False)

        with patch("plugins.knowledge.RagService") as service:
            service.return_value.answer.side_effect = answer
            self.assertEqual(await knowledge.handle_ask(context), "")
        self.assertEqual(self.rows(), [])


class BlockingCapacityTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_awaiter_keeps_capacity_until_the_thread_finishes(self):
        executor = BoundedExecutor(max_workers=1)
        loop = asyncio.get_running_loop()
        started, finished = asyncio.Event(), asyncio.Event()
        release = threading.Event()

        def blocked():
            loop.call_soon_threadsafe(started.set)
            release.wait(5)
            loop.call_soon_threadsafe(finished.set)

        task = asyncio.create_task(executor.run(blocked))
        try:
            await started.wait()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            with self.assertRaises(BlockingIOBusy):
                await executor.run(lambda: "must not start")
        finally:
            release.set()
        await finished.wait()
        # The worker's done callback releases the capacity after returning.
        async def once_free():
            while True:
                try:
                    return await executor.run(lambda: "available")
                except BlockingIOBusy:
                    await asyncio.sleep(0)

        self.assertEqual(await asyncio.wait_for(once_free(), 1), "available")
