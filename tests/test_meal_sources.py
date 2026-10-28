import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
from helpers import make_place

from doraymon.meal.sources import McpTools, ResearchSources, SourceError


class SourceDouble:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        return self.handler(name, arguments)


@pytest.mark.parametrize(
    "tool_result,expected,error",
    [
        ({"content": [{"type": "text", "text": '{"feeds": []}'}]}, {"feeds": []}, None),
        ({"content": [], "structuredContent": {"feeds": []}}, {"feeds": []}, None),
        (
            {"content": [{"type": "text", "text": "private-error"}], "isError": True},
            None,
            "tool_failed",
        ),
        ({"content": [{"type": "text", "text": "invalid JSON"}]}, None, "invalid_response"),
    ],
)
async def test_actual_mcp_sdk_initializes_and_parses_tool_responses_without_network(
    tool_result, expected, error
):
    methods = []

    def server(request):
        if request.method != "POST":
            return httpx.Response(405)
        body = json.loads(request.content)
        method = body["method"]
        methods.append(method)
        if "id" not in body:
            return httpx.Response(202)
        if method == "initialize":
            result = {
                "protocolVersion": body["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "synthetic-source", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": [{"name": "search_feeds", "inputSchema": {"type": "object"}}]}
        else:
            assert method == "tools/call"
            assert body["params"] == {"name": "search_feeds", "arguments": {"keyword": "合成餐馆"}}
            result = tool_result
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    async with asyncio.timeout(3):
        async with McpTools(
            "https://mcp.test/mcp", transport=httpx.MockTransport(server)
        ) as client:
            if error:
                with pytest.raises(SourceError, match=error):
                    await client.call("search_feeds", {"keyword": "合成餐馆"})
            else:
                assert await client.call("search_feeds", {"keyword": "合成餐馆"}) == expected
            with pytest.raises(ValueError):
                await client.call("publish_content", {})
    assert methods[0:2] == ["initialize", "notifications/initialized"]
    assert methods.count("tools/call") == 1


@pytest.mark.parametrize("timestamp", [1750000000000, 32503680000000])
async def test_xhs_reads_detail_and_keeps_attributed_experience_without_access_token(timestamp):
    place = make_place(1).model_copy(update={"city": "合成市"})

    def handle(name, args):
        if name == "search_feeds":
            assert "少辣" in args["keyword"]
            return {
                "feeds": [
                    {
                        "id": "synthetic-note1",
                        "xsecToken": "private-token",
                        "noteCard": {"displayTitle": place.name},
                    }
                ]
            }
        assert args == {
            "feed_id": "synthetic-note1",
            "xsec_token": "private-token",
            "load_all_comments": False,
        }
        return {
            "data": {
                "note": {
                    "title": "一次用餐记录",
                    "desc": f"{place.name}口味偏辣。",
                    "time": timestamp,
                }
            }
        }

    client = SourceDouble(handle)
    sources = ResearchSources(
        xiaohongshu_url="https://xhs.test/mcp", client_factory=lambda *a, **kw: client
    )
    report = await sources.enrich((place,), "少辣", lambda _: None)
    [evidence] = report.candidates[0].evidence
    assert evidence.kind == "experience" and not evidence.branch_confirmed
    expected = datetime.fromtimestamp(1750000000, UTC) if timestamp == 1750000000000 else None
    assert evidence.published_at == expected
    assert "private-token" not in report.model_dump_json()
    assert evidence.url.endswith("/synthetic-note1")
    assert client.closed


@pytest.mark.parametrize("address,confirmed", [("合成路一百号二楼", True), ("别的路九号", False)])
async def test_dianping_uses_actual_items_contract_and_verifies_branch(address, confirmed):
    place = make_place(1).model_copy(update={"city": "合成市", "address": "合成路一百号二楼"})

    def handle(name, args):
        if name == "dianping_search":
            assert args == {"keyword": place.name, "city": "合成市", "limit": 3}
            return {"items": [{"id": "shop-1", "name": place.name}]}
        assert args == {"shop_id": "shop-1"}
        return {"id": "shop-1", "name": place.name, "address": address, "rating": "4.2"}

    client = SourceDouble(handle)
    report = await ResearchSources(
        dianping_url="https://dp.test/mcp", client_factory=lambda *a, **kw: client
    ).enrich((place,), "", lambda _: None)
    [evidence] = report.candidates[0].evidence
    assert evidence.branch_confirmed is confirmed
    assert evidence.text == "平台评分：4.2"
    assert evidence.kind == "listing"
    assert "菜单" not in evidence.text


async def test_source_failure_preserves_earlier_candidates_without_leaking_error():
    def handle(name, args):
        if name == "dianping_search":
            if args["keyword"].endswith("2"):
                raise SourceError("tool_failed")
            return {"items": [{"id": "shop-1", "name": "合成餐馆1"}]}
        return {"name": "合成餐馆1", "address": "合成路一百号二楼", "rating": "4"}

    places = tuple(make_place(n).model_copy(update={"city": "合成市"}) for n in (1, 2))
    report = await ResearchSources(
        dianping_url="https://dp.test/mcp", client_factory=lambda *a, **kw: SourceDouble(handle)
    ).enrich(places, "", lambda _: None)
    assert len(report.candidates[0].evidence) == 1
    assert not report.candidates[1].evidence
    assert report.coverage[2].status == "partial"

    def failing(*args):
        raise RuntimeError("https://private.test/?token=secret")

    report = await ResearchSources(
        dianping_url="https://dp.test/mcp", client_factory=lambda *a, **kw: SourceDouble(failing)
    ).enrich(places, "", lambda _: None)
    assert report.coverage[2].status == "unavailable"
    assert "secret" not in report.model_dump_json()


async def test_configured_source_timeout_and_cancellation_close_the_client():
    entered = asyncio.Event()

    class WaitingSource(SourceDouble):
        async def call(self, *args):
            entered.set()
            await asyncio.Event().wait()

    client = WaitingSource(None)
    sources = ResearchSources(
        xiaohongshu_url="https://xhs.test/mcp", timeout=0.02, client_factory=lambda *a, **kw: client
    )
    report = await sources.enrich((make_place(1),), "", lambda _: None)
    assert report.coverage[1].status == "unavailable" and client.closed

    entered.clear()
    client.closed = False
    sources.timeout = 5
    job = asyncio.create_task(sources.enrich((make_place(1),), "", lambda _: None))
    async with asyncio.timeout(1):
        await entered.wait()
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert client.closed


async def test_catalog_matches_amap_id_and_requires_usable_observation_time(tmp_path):
    path = tmp_path / "menu.json"
    entry = {
        "amap_id": "p1",
        "title": "合成商家菜单",
        "text": "清汤面 18 元，可选少盐。",
        "source_url": "https://menu.test/menu?token=private-token&item=1",
        "observed_at": "2025-01-01T10:00:00+08:00",
    }
    path.write_text(json.dumps({"entries": [entry]}), encoding="utf-8")
    sources = ResearchSources(catalog_path=path)
    report = await sources.enrich((make_place(1), make_place(2)), "", lambda _: None)
    [evidence] = report.candidates[0].evidence
    assert evidence.branch_confirmed and evidence.kind == "menu"
    assert "private-token" not in report.model_dump_json()
    assert not report.candidates[1].evidence
    for stamp in ("2025-01-01T10:00:00", "2999-01-01T10:00:00Z"):
        entry["observed_at"] = stamp
        path.write_text(json.dumps({"entries": [entry]}), encoding="utf-8")
        report = await sources.enrich((make_place(1),), "", lambda _: None)
        assert report.coverage[0].status == "unavailable"
        assert not report.candidates[0].evidence


async def test_unconfigured_sources_do_not_emit_fictional_research_stages():
    stages = []
    report = await ResearchSources().enrich((make_place(1),), "", stages.append)
    assert stages == []
    assert all(c.status == "not_configured" for c in report.coverage)


async def test_actual_mcp_transport_closes_inflight_requests_on_timeout_and_cancel():
    entered = asyncio.Event()
    cleaned = []

    async def server(request):
        if request.method != "POST":
            return httpx.Response(405)
        body = json.loads(request.content)
        method = body["method"]
        if "id" not in body:
            return httpx.Response(202)
        if method == "initialize":
            result = {
                "protocolVersion": body["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "synthetic-source", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": []}
        else:
            assert method == "tools/call"
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.append(True)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    def factory(url, **kwargs):
        return McpTools(url, transport=httpx.MockTransport(server), **kwargs)

    sources = ResearchSources(
        xiaohongshu_url="https://mcp.test/mcp", timeout=0.1, client_factory=factory
    )
    async with asyncio.timeout(3):
        report = await sources.enrich((make_place(1),), "", lambda _: None)
        assert entered.is_set() and cleaned == [True]
        assert report.coverage[1].status == "unavailable"
        sources.timeout = 5
        entered.clear()
        job = asyncio.create_task(sources.enrich((make_place(1),), "", lambda _: None))
        await entered.wait()
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert cleaned == [True, True]
