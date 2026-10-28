import asyncio

from doraymon.meal.progress import SearchProgress


async def test_fast_result_does_not_send_progress():
    sent = []

    async def send(text):
        sent.append(text)
        return True

    async with SearchProgress(send, delay=1) as progress:
        progress.stage("正在查附近餐厅。")
    assert sent == []


async def test_progress_coalesces_pending_stages_and_stops_before_final_result():
    sent = []
    delivered = asyncio.Event()

    async def send(text):
        sent.append(text)
        delivered.set()
        return True

    async with SearchProgress(send, delay=0, interval=0, limit=2) as progress:
        progress.stage("查门店")
        progress.stage("补资料")
        async with asyncio.timeout(1):
            await delivered.wait()
        assert sent == ["补资料"]
        delivered.clear()
        progress.stage("核对分店")
        async with asyncio.timeout(1):
            await delivered.wait()
        progress.stage("后续不再播报")
    sent.append("最终结果")
    progress.stage("结束后也不再播报")
    await asyncio.sleep(0)
    assert sent == ["补资料", "核对分店", "最终结果"]


async def test_stopping_progress_cancels_inflight_send_and_waits_for_cleanup():
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def send(text):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    async with SearchProgress(send, delay=0) as progress:
        progress.stage("查门店")
        async with asyncio.timeout(1):
            await entered.wait()
    assert cleaned.is_set()


async def test_unconfirmed_delivery_is_attempted_once():
    sent = []
    attempted = asyncio.Event()

    async def send(text):
        sent.append(text)
        attempted.set()
        return False

    async with SearchProgress(send, delay=0, interval=0) as progress:
        progress.stage("查门店")
        async with asyncio.timeout(1):
            await attempted.wait()
        progress.stage("补资料")
        await asyncio.sleep(0)
    assert sent == ["查门店"]
