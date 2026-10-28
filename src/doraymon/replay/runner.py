"""Play scenarios against a running MaiBot through FakeNapCat and score the replies.

A reply counts for the step whose window it falls in: from that step until the next step, or
until the settle time after the last one. Scenarios therefore leave enough silence after any
step that expects a reply, so a slow answer is not credited to the following step.
"""

import asyncio
import math
import statistics
from typing import Any

from .scenario import Scenario
from .server import Chat, FakeNapCat

FIRST_MEMBER_ID = 30001


def member_ids(scenario: Scenario) -> dict[str, str]:
    return {alias: str(FIRST_MEMBER_ID + n) for n, alias in enumerate(scenario.members)}


async def play(
    bench: FakeNapCat, scenario: Scenario, *, speed: float = 1.0, settle: float | None = None
) -> dict[str, Any]:
    if not math.isfinite(speed) or speed <= 0:
        raise ValueError("speed 必须是大于 0 的有限数")
    if settle is not None and (not math.isfinite(settle) or settle < 0):
        raise ValueError("settle 必须是非负有限数")
    ids = member_ids(scenario)
    for alias, user_id in ids.items():
        bench.add_member(user_id, scenario.members[alias])
    if scenario.chat.type == "group":
        bench.add_group(scenario.chat.group_id, scenario.chat.group_name)
        chat: Chat = ("group", scenario.chat.group_id)
    else:
        chat = ("private", next(iter(ids.values())))
    loop = asyncio.get_running_loop()
    start = loop.time()
    sent_before = len(bench.sent)
    message_ids: list[int] = []
    step_times: list[float] = []
    for step in scenario.steps:
        delay = start + step.at / speed - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        quote = message_ids[step.quote] if isinstance(step.quote, int) else None
        if step.quote == "bot":
            quote = bench.last_bot_message(chat)
            if quote is None:
                raise ValueError(f"{scenario.name} 需要引用机器人消息，但此前没有收到回复")
        step_times.append(loop.time() - start)
        message_id = await bench.push_message(
            chat,
            ids[step.user],
            step.text,
            mention_bot=step.mention_bot,
            mention_user=ids[step.mention_user] if step.mention_user else None,
            quote=quote,
        )
        message_ids.append(message_id)
    await asyncio.sleep((scenario.settle if settle is None else settle) / speed)
    end = loop.time() - start
    messages = [m for m in bench.sent[sent_before:] if m.chat == chat]
    replies = [{"at": round(m.at - start, 2), "text": m.text, "quote": m.quote} for m in messages]
    return {
        "scenario": scenario.name,
        "split": scenario.split,
        "steps": [
            {
                "index": index,
                "at": round(step_times[index], 2),
                "user": scenario.members[step.user],
                "text": step.text,
                "message_id": message_ids[index],
                "expect": step.expect,
            }
            for index, step in enumerate(scenario.steps)
        ],
        "replies": replies,
        "checks": evaluate(scenario, step_times, [m.at - start for m in messages], end),
    }


def evaluate(
    scenario: Scenario, step_times: list[float], reply_times: list[float], end: float
) -> list[dict[str, Any]]:
    checks = []
    for index, step in enumerate(scenario.steps):
        if step.expect == "any":
            continue
        until = step_times[index + 1] if index + 1 < len(step_times) else end
        inside = [t for t in reply_times if step_times[index] <= t < until]
        checks.append(
            {
                "step": index,
                "expect": step.expect,
                "replies": len(inside),
                "first_latency": round(inside[0] - step_times[index], 2) if inside else None,
                "passed": bool(inside) if step.expect == "reply" else not inside,
            }
        )
    return checks


def summarize(runs: list[dict[str, Any]]) -> dict[str, Any]:
    checks = [check for run in runs for check in run["checks"]]
    lengths = sorted(len(reply["text"]) for run in runs for reply in run["replies"])
    latencies = sorted(
        c["first_latency"]
        for c in checks
        if c["expect"] == "reply" and c["first_latency"] is not None
    )
    return {
        "scenarios": len(runs),
        "reply_checks": sum(c["expect"] == "reply" for c in checks),
        "missed_replies": sum(c["expect"] == "reply" and not c["passed"] for c in checks),
        "silent_checks": sum(c["expect"] == "silent" for c in checks),
        "unwanted_replies": sum(c["expect"] == "silent" and not c["passed"] for c in checks),
        "bot_messages": len(lengths),
        "length_median": statistics.median(lengths) if lengths else None,
        "length_p90": percentile(lengths, 0.9),
        "latency_p50": percentile(latencies, 0.5),
        "latency_p95": percentile(latencies, 0.95),
    }


def percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile of an already sorted list."""
    if not values:
        return None
    rank = max(1, math.ceil(len(values) * fraction))
    return values[rank - 1]
