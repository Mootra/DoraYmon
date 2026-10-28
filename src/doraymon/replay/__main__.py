"""python -m doraymon.replay: validate scenarios or replay them against a running MaiBot.

The MaiBot instance under test must be separate from production (its own instance directory,
WebUI port and database) and point its QQ adapter at this bench, e.g.
``doraymon prepare --instance <replay instance> --onebot ws://127.0.0.1:3901``.
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from .runner import play, summarize
from .scenario import load_scenarios
from .server import FakeNapCat

REPO_ROOT = Path(__file__).resolve().parents[3]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m doraymon.replay", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "run"):
        command = commands.add_parser(name)
        command.add_argument("--scenarios", type=Path, default=REPO_ROOT / "evals" / "scenarios")
        command.add_argument("--split", choices=["development", "holdout"])
    run = commands.choices["run"]
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=3901)
    run.add_argument("--token", default="", help="与回放实例里连接器配置的 token 一致")
    run.add_argument("--ready-timeout", type=float, default=300, help="等待 MaiBot 连接的秒数")
    run.add_argument("--speed", type=float, default=1.0, help="大于 1 时压缩时间轴，仅用于冒烟")
    run.add_argument("--out", type=Path, default=REPO_ROOT / "evals" / "runs")
    return parser


async def replay(args: argparse.Namespace) -> int:
    scenarios = load_scenarios(args.scenarios, args.split)
    bench = FakeNapCat(token=args.token)
    port = await bench.start(args.host, args.port)
    print(f"回放台监听 ws://{args.host}:{port}，等待 MaiBot 连接……")
    try:
        await bench.wait_ready(args.ready_timeout)
        runs = []
        for scenario in scenarios:
            print(f"播放：{scenario.name}")
            runs.append(await play(bench, scenario, speed=args.speed))
    finally:
        await bench.stop()
    summary = summarize(runs)
    report = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "split": args.split or "all",
        "summary": summary,
        "unsupported_actions": sorted(bench.unsupported),
        "runs": runs,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"replay-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"结果：{path}")
    failed = summary["missed_replies"] or summary["unwanted_replies"] or bench.unsupported
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate":
            scenarios = load_scenarios(args.scenarios, args.split)
            print(f"{len(scenarios)} 个场景格式正确")
            return 0
        return asyncio.run(replay(args))
    except TimeoutError:
        print("等待 MaiBot 连接超时：确认回放实例已启动且连接器指向本回放台", file=sys.stderr)
        return 2
    except (OSError, RuntimeError, ValueError) as error:
        print(f"回放失败：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
