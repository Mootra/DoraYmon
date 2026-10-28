import asyncio
from unittest.mock import AsyncMock

import pytest

from doraymon.flow import BusyError, Conversations


async def test_burst_merges_text_and_preserves_order(event):
    process = AsyncMock(return_value=True)
    flow = Conversations(process, gap=0.01, window=0.05)
    send = AsyncMock()
    try:
        assert all(
            await asyncio.gather(
                flow.submit(event("第一条"), send),
                flow.submit(event("补充条件", message_id="2"), send),
            )
        )
        process.assert_awaited_once()
        assert process.call_args.args[0].text == "第一条\n补充条件"
        assert process.call_args.args[0].message_id == "2"
        assert [(p.message_id, p.text) for p in process.call_args.args[0].parts] == [
            ("m1", "第一条"),
            ("2", "补充条件"),
        ]
    finally:
        await flow.close()


@pytest.mark.parametrize(
    "change", [{"quote_id": "other"}, {"mentions": {"someone"}}, {"text": "/ping"}]
)
async def test_merge_keeps_recipient_quote_and_command_boundaries(event, change):
    process = AsyncMock(return_value=True)
    flow = Conversations(process, gap=0.01, window=0.05)
    try:
        await asyncio.gather(
            flow.submit(event(), AsyncMock()),
            flow.submit(event(message_id="2", **change), AsyncMock()),
        )
        assert process.await_count == 2
    finally:
        await flow.close()


async def test_backpressure_and_shutdown_resolve_waiters(event):
    entered, hold = asyncio.Event(), asyncio.Event()

    async def process(message, send):
        entered.set()
        await hold.wait()
        return True

    flow = Conversations(process, queue_size=1, max_sessions=1, gap=0)
    pending = asyncio.create_task(flow.submit(event(), AsyncMock()))
    await entered.wait()
    with pytest.raises(BusyError, match="session_full"):
        await flow.submit(event(message_id="2"), AsyncMock())
    with pytest.raises(BusyError, match="sessions_full"):
        await flow.submit(event(user_id="bob"), AsyncMock())
    await flow.close()
    assert not await pending
    assert not flow.lanes
    with pytest.raises(BusyError, match="closed"):
        await flow.submit(event(), AsyncMock())


async def test_new_input_queues_after_interrupt_action(event):
    clear_entered, clear_release = asyncio.Event(), asyncio.Event()
    seen = []

    async def clear(message, send):
        clear_entered.set()
        await clear_release.wait()
        seen.append("clear")
        return True

    async def process(message, send):
        seen.append(message.text)
        return True

    flow = Conversations(process, gap=0)
    control = asyncio.create_task(flow.submit(event("/清空上下文"), AsyncMock(), control=clear))
    await clear_entered.wait()
    next_input = asyncio.create_task(flow.submit(event("next", message_id="2"), AsyncMock()))
    clear_release.set()
    assert all(await asyncio.gather(control, next_input))
    assert seen == ["clear", "next"]
    await flow.close()
