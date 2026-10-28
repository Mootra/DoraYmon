from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from doraymon.client import MAX_REPLY_LENGTH, MyClient, split_reply
from doraymon.config import Settings


class ClientReplyTest(unittest.TestCase):
    def _client(self, *, max_segments: int = 3, max_total_chars: int = 4000):
        client = object.__new__(MyClient)
        client.settings = Settings(
            reply_max_segments=max_segments,
            reply_max_total_chars=max_total_chars,
        )
        client.started_at = datetime.now()
        client.api = SimpleNamespace(
            post_c2c_message=AsyncMock(),
            post_group_message=AsyncMock(),
        )
        return client

    def test_short_reply_uses_one_message_with_sequence_one(self):
        chunks = split_reply("短回复")
        self.assertEqual(chunks, ["短回复"])

    def test_boundary_punctuation_never_exceeds_one_segment_limit(self):
        chunks = split_reply("a" * (MAX_REPLY_LENGTH - 1) + "。b")
        self.assertEqual([len(chunk) for chunk in chunks], [MAX_REPLY_LENGTH, 1])

    def test_split_preserves_code_indentation_and_all_internal_whitespace(self):
        code = "def example():\n" + "    value = 123\n" * 220 + "    return value"
        chunks = split_reply(code)
        self.assertEqual("".join(chunks), code)
        self.assertLessEqual(len(chunks), 3)
        self.assertTrue(all(len(chunk) <= MAX_REPLY_LENGTH for chunk in chunks))

    def test_paragraph_boundaries_do_not_discard_tail_within_capacity(self):
        text = ("a" * 949 + "\n") * 4 + "tail"
        chunks = split_reply(text)
        self.assertEqual("".join(chunks), text)
        self.assertLessEqual(len(chunks), 3)

    def test_prepared_reply_is_idempotent_and_matches_delivered_text(self):
        from services.reply_delivery import prepare_reply
        for segments, total in ((3, 4000), (1, 4000), (2, 2500)):
            settings = Settings(reply_max_segments=segments, reply_max_total_chars=total)
            text = prepare_reply("x" * 6000, settings)
            self.assertTrue(text.endswith("[回复过长，已截断]"))
            self.assertLessEqual(len(text), min(segments * 1800, total))
            self.assertEqual(prepare_reply(text, settings), text)
            self.assertEqual("".join(split_reply(text, max_segments=segments, max_total_chars=total)), text)

    def test_long_reply_splits_and_increments_c2c_sequence(self):
        client = self._client()
        message = SimpleNamespace(
            id="message-1",
            content="你好",
            author=SimpleNamespace(user_openid="user-a"),
        )
        reply = "第一段。" + ("内容" * 1100) + "\n\n第二段。" + ("更多" * 1000)

        async def run():
            with patch("doraymon.client.route_incoming_message", new=AsyncMock(return_value=reply)):
                await client._handle_message(message, is_at_message=False)

        asyncio.run(run())
        calls = client.api.post_c2c_message.await_args_list
        self.assertEqual([call.kwargs["msg_seq"] for call in calls], [1, 2, 3])
        self.assertTrue(all(len(call.kwargs["content"]) <= MAX_REPLY_LENGTH for call in calls))
        self.assertTrue(calls[-1].kwargs["content"].endswith("[回复过长，已截断]"))

    def test_long_reply_uses_incrementing_group_sequence(self):
        client = self._client(max_segments=2, max_total_chars=3500)
        message = SimpleNamespace(
            id="message-2",
            content="@bot 你好",
            group_openid="group-a",
            author=SimpleNamespace(member_openid="user-a"),
        )
        reply = "段落。" + ("内容" * 1600)

        async def run():
            with patch("doraymon.client.route_incoming_message", new=AsyncMock(return_value=reply)):
                await client._handle_message(message, is_at_message=True)

        asyncio.run(run())
        calls = client.api.post_group_message.await_args_list
        self.assertEqual([call.kwargs["msg_seq"] for call in calls], [1, 2])
        self.assertTrue(all(len(call.kwargs["content"]) <= MAX_REPLY_LENGTH for call in calls))


class SegmentFailureTest(unittest.TestCase):
    """A failure mid-reply must not leave a non-contiguous answer.

    Every test clears the process-wide singletons first: the reply budget is
    shared with the rest of the suite, and a leftover count from another test
    would silently turn these assertions into no-ops.
    """

    def setUp(self) -> None:
        from services.reply_budget import REPLY_BUDGET
        from services.session_revisions import SESSION_REVISIONS

        REPLY_BUDGET.clear()
        SESSION_REVISIONS.clear()
        self.addCleanup(REPLY_BUDGET.clear)
        self.addCleanup(SESSION_REVISIONS.clear)

    def _client(self, *, fail_on: int = 0):
        client = object.__new__(MyClient)
        client.settings = Settings(reply_max_segments=3, reply_max_total_chars=4000)
        client.started_at = datetime.now()
        attempts: list[int] = []

        async def post_c2c_message(**kwargs):
            attempts.append(int(kwargs.get("msg_seq") or 0))
            if fail_on and int(kwargs.get("msg_seq") or 0) == fail_on:
                raise RuntimeError("接口返回 400")
            return SimpleNamespace(id="sent")

        client.api = SimpleNamespace(
            post_c2c_message=post_c2c_message,
            post_group_message=AsyncMock(),
        )
        # A plain async function is used rather than an AsyncMock side_effect:
        # the latter returns a coroutine that is never awaited, which would make
        # a deliberately failing send look like a successful one.
        client.attempts = attempts
        return client

    def _run(self, client) -> None:
        reply = "第一段。" + ("内容" * 1100) + chr(10) * 2 + "第二段。" + ("更多" * 1000)
        message = SimpleNamespace(
            id="message-1",
            content="你好",
            author=SimpleNamespace(user_openid="user-a"),
        )

        async def run():
            with patch(
                "doraymon.client.route_incoming_message",
                new=AsyncMock(return_value=reply),
            ):
                await client._handle_message(message, is_at_message=False)

        asyncio.run(run())

    def test_a_long_reply_needs_three_segments(self) -> None:
        """Guards the fixture: if the reply fits in one segment these prove nothing."""
        client = self._client()

        self.assertEqual(len(client._split_reply(
            "第一段。" + ("内容" * 1100) + chr(10) * 2 + "第二段。" + ("更多" * 1000)
        )), 3)

    def test_all_segments_are_attempted_when_nothing_fails(self) -> None:
        client = self._client()

        self._run(client)

        self.assertEqual(client.attempts, [1, 2, 3])

    def test_failure_in_the_first_segment_stops_the_rest(self) -> None:
        client = self._client(fail_on=1)

        self._run(client)

        self.assertEqual(client.attempts, [1])

    def test_a_later_segment_is_not_sent_after_a_failure(self) -> None:
        """Skipping the failed segment would leave a gap in contiguous prose."""
        client = self._client(fail_on=2)

        self._run(client)

        self.assertEqual(client.attempts, [1, 2])
