import asyncio
import json

import aiohttp
import pytest
from helpers import REPO_ROOT
from pydantic import ValidationError

from doraymon.replay import __main__ as cli
from doraymon.replay.runner import evaluate, percentile, play, summarize
from doraymon.replay.scenario import Scenario, load_scenarios
from doraymon.replay.server import FakeNapCat

pytestmark = pytest.mark.allow_hosts(["127.0.0.1"])


def scenario(*steps, **extra):
    return Scenario.model_validate(
        {
            "name": "合成回放",
            "split": "development",
            "chat": {"type": "group", "group_id": "20001"},
            "members": {"a": "小林", "b": "阿杰"},
            "steps": list(steps) or [{"at": 0, "user": "a", "text": "你好", "expect": "reply"}],
            "settle": 0.03,
            **extra,
        }
    )


@pytest.fixture
async def bench():
    server = FakeNapCat(token="synthetic-token", heartbeat_seconds=0.02)
    port = await server.start(port=0)
    async with aiohttp.ClientSession() as http:
        yield server, http, f"http://127.0.0.1:{port}"
    await server.stop()


async def action(socket, name, params=None):
    await socket.send_json({"action": name, "params": params or {}, "echo": name})
    async with asyncio.timeout(1):
        while True:
            message = await socket.receive_json()
            if message.get("echo") == name:
                return message


async def connect(http, url):
    return await http.ws_connect(url, headers={"Authorization": "Bearer synthetic-token"})


async def test_websocket_authentication_profile_and_readiness(bench):
    server, http, url = bench
    with pytest.raises(aiohttp.WSServerHandshakeError) as caught:
        await http.ws_connect(url)
    assert caught.value.status == 401
    async with await connect(http, url) as socket:
        lifecycle = await socket.receive_json()
        assert lifecycle["meta_event_type"] == "lifecycle"
        profile = await action(socket, "get_version_info")
        assert profile["data"]["app_name"] == "NapCat.Onebot"
        login = await action(socket, "get_login_info")
        assert login["data"]["user_id"] == 10001
        await server.wait_ready(0.1)
        heartbeat = await socket.receive_json()
        assert heartbeat["status"] == {"online": True, "good": True}
    with pytest.raises(TimeoutError):
        await server.wait_ready(0.01)


async def test_message_lookup_quotes_and_recorded_delivery(bench):
    server, http, url = bench
    server.add_member("30001", "小林")
    server.add_group("20001", "测试群")
    async with await connect(http, url) as socket:
        await action(socket, "get_login_info")
        inbound_id = await server.push_message(
            ("group", "20001"), "30001", "吃什么", mention_bot=True, mention_user="30002"
        )
        event = await socket.receive_json()
        assert event["user_id"] == 30001
        assert [s["data"]["qq"] for s in event["message"] if s["type"] == "at"] == [
            "10001",
            "30002",
        ]
        sent = await action(
            socket,
            "send_group_msg",
            {
                "group_id": 20001,
                "message": [
                    {"type": "reply", "data": {"id": str(inbound_id)}},
                    {"type": "text", "data": {"text": "合成回复"}},
                ],
            },
        )
        bot_id = sent["data"]["message_id"]
        lookup = await action(socket, "get_msg", {"message_id": bot_id})
        assert lookup["data"]["sender"]["user_id"] == 10001
        assert server.sent[0].text == "合成回复"
        assert server.sent[0].quote == str(inbound_id)
        assert server.last_bot_message(("group", "20001")) == bot_id
        missing = await action(socket, "unsupported_action")
        assert missing["status"] == "failed"
        assert server.unsupported == {"unsupported_action"}


async def test_disconnected_bench_refuses_to_drop_messages_silently(bench):
    server, _, _ = bench
    with pytest.raises(RuntimeError, match="连接已断开"):
        await server.push_message(("private", "30001"), "30001", "你好")
    assert not server.messages


async def test_timeline_replies_and_bot_quote_use_real_websocket_events(bench):
    server, http, url = bench
    case = scenario(
        {"at": 0, "user": "a", "text": "你好", "expect": "reply"},
        {"at": 0.03, "user": "a", "text": "接着说", "quote": "bot", "expect": "reply"},
    )
    async with await connect(http, url) as socket:
        await action(socket, "get_login_info")

        async def respond():
            async for frame in socket:
                event = json.loads(frame.data)
                if event.get("post_type") == "message":
                    await socket.send_json(
                        {
                            "action": "send_group_msg",
                            "params": {"group_id": 20001, "message": "合成回复"},
                        }
                    )

        responding = asyncio.create_task(respond())
        try:
            report = await play(server, case)
        finally:
            await socket.close()
            await responding
    assert [check["passed"] for check in report["checks"]] == [True, True]
    assert len(report["replies"]) == 2
    assert list(server.messages.values())[-2]["message"][0]["type"] == "reply"
    assert summarize([report])["missed_replies"] == 0


async def test_bot_quote_fails_when_no_previous_reply_was_received(bench):
    server, http, url = bench
    case = scenario({"at": 0, "user": "a", "text": "继续", "quote": "bot"})
    async with await connect(http, url) as socket:
        await action(socket, "get_login_info")
        with pytest.raises(ValueError, match="此前没有收到回复"):
            await play(server, case)


def test_scoring_windows_include_the_start_and_exclude_the_next_step():
    case = scenario(
        {"at": 0, "user": "a", "text": "答", "expect": "reply"},
        {"at": 1, "user": "a", "text": "停", "expect": "silent"},
        {"at": 2, "user": "b", "text": "随意", "expect": "any"},
    )
    checks = evaluate(case, [0, 1, 2], [0, 1, 2.1], 3)
    assert [(c["replies"], c["passed"]) for c in checks] == [(1, True), (1, False)]
    summary = summarize([{"checks": checks, "replies": [{"text": "合成回复"}]}])
    assert summary["unwanted_replies"] == 1
    assert summary["latency_p50"] == 0
    assert summary["length_median"] == 4
    assert percentile(list(range(1, 11)), 0.9) == 9
    assert summarize([])["latency_p50"] is None


@pytest.mark.parametrize("speed", [0, -1, float("inf"), float("nan")])
async def test_invalid_playback_speed_is_rejected(bench, speed):
    with pytest.raises(ValueError, match="speed"):
        await play(bench[0], scenario(), speed=speed)


@pytest.mark.parametrize(
    "step",
    [
        {"at": -1, "user": "a", "text": "负时间"},
        {"at": float("inf"), "user": "a", "text": "无穷"},
        {"at": 0, "user": "a", "text": "不存在的引用", "quote": 0},
        {"at": 0, "user": "unknown", "text": "未知成员"},
        {"at": 0, "user": "a", "text": "未知接收人", "mention_user": "unknown"},
    ],
)
def test_scenario_rejects_unplayable_steps(step):
    with pytest.raises(ValidationError):
        scenario(step)


def test_scenarios_are_nonempty_and_unique(tmp_path):
    with pytest.raises(ValueError, match="没有可用"):
        load_scenarios(tmp_path)
    content = scenario().model_dump_json()
    (tmp_path / "a.json").write_text(content, encoding="utf-8")
    (tmp_path / "b.json").write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="name 不能重复"):
        load_scenarios(tmp_path)


def test_validate_uses_repository_scenarios_and_reports_bad_paths(tmp_path):
    assert cli.main(["validate", "--scenarios", str(REPO_ROOT / "evals" / "scenarios")]) == 0
    assert cli.main(["validate", "--scenarios", str(tmp_path)]) == 2


@pytest.mark.parametrize("unsupported,passed", [(False, False), (True, True), (False, True)])
def test_replay_saves_report_and_exits_nonzero_on_failed_checks(
    tmp_path, monkeypatch, unsupported, passed
):
    class ReadyBench:
        def __init__(self, **kwargs):
            self.unsupported = {"unknown"} if unsupported else set()

        async def start(self, *args):
            return 3901

        async def wait_ready(self, seconds):
            pass

        async def stop(self):
            pass

    async def played(*args, **kwargs):
        return {
            "checks": [
                {
                    "expect": "reply",
                    "passed": passed,
                    "first_latency": 0 if passed else None,
                }
            ],
            "replies": [],
        }

    monkeypatch.setattr(cli, "FakeNapCat", ReadyBench)
    monkeypatch.setattr(cli, "play", played)
    args = cli.build_parser().parse_args(["run", "--out", str(tmp_path)])
    assert asyncio.run(cli.replay(args)) == (1 if unsupported or not passed else 0)
    [report_file] = list(tmp_path.glob("*.json"))
    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert bool(report["unsupported_actions"]) == unsupported
    assert bool(report["summary"]["missed_replies"]) != passed
