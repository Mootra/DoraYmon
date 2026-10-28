"""One command that runs every offline gate and reports what it proves.

A reviewer should not have to read a checklist and run six commands in the right
order. This runs them, reports each result plainly, and states what each gate does
and does not prove. A gate that cannot run is reported as skipped, never as passed.

Usage:
    .venv/bin/python -m scripts.preflight_offline
    .venv/bin/python -m scripts.preflight_offline --quick   # skip the slow gates
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable

PASS = "通过"
FAIL = "未通过"
SKIP = "跳过"


@dataclass
class Gate:
    """One verification step and what it does or does not establish."""

    name: str
    args: tuple[str, ...]
    proves: str
    does_not_prove: str = ""
    slow: bool = False
    timeout: int = 900


GATES: tuple[Gate, ...] = (
    Gate(
        name="离线单元测试",
        args=("-m", "unittest", "discover", "-s", "tests", "-t", "."),
        proves="代码在离线条件下的行为，包括边界与失败恢复",
        does_not_prove="真实 QQ 收发、真实模型质量、真实地图数据",
    ),
    Gate(
        name="回放场景",
        args=("-m", "scripts.replay_scenarios"),
        proves="路由、身份隔离与会话保证在固定场景下成立",
        does_not_prove="真实回答质量（用的是假模型）",
        slow=True,
    ),
    Gate(
        name="RAG 召回门槛",
        args=("-m", "scripts.eval_rag"),
        proves="Recall@3/MRR 门槛、无答案空召回与权限不泄漏",
        does_not_prove="真实语料外的泛化",
        slow=True,
    ),
)


def _run(gate: Gate) -> tuple[str, str, float]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            [PYTHON, *gate.args],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=gate.timeout,
        )
    except subprocess.TimeoutExpired:
        return FAIL, f"超过 {gate.timeout} 秒未完成", time.monotonic() - started
    except OSError as error:
        return FAIL, f"无法执行：{type(error).__name__}: {error}", time.monotonic() - started
    elapsed = time.monotonic() - started
    output = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode != 0:
        return FAIL, _tail(output), elapsed
    return PASS, _summary(gate.name, output), elapsed


def _tail(text: str, lines: int = 12) -> str:
    kept = [line for line in text.splitlines() if line.strip()][-lines:]
    return "\n".join(kept)


def _summary(name: str, output: str) -> str:
    """One useful line from a gate's output, rather than all of it."""
    if "单元测试" in name:
        for line in output.splitlines():
            if line.startswith("Ran "):
                match = re.match(r"Ran (\d+) tests", line)
                if match:
                    return f"{match.group(1)} 项测试通过"
        return "通过"
    if "回放" in name:
        match = re.search(r"回放场景：(\d+)，未通过：(\S+)", output)
        if match:
            return f"{match.group(1)} 个场景，未通过：{match.group(2)}"
        return "通过"
    if "RAG" in name:
        lines = []
        for key in ("Recall@3", "MRR", "无答案空召回准确率", "权限泄漏"):
            match = re.search(rf"{re.escape(key)}：(\S+)", output)
            if match:
                lines.append(f"{key} {match.group(1)}")
        return "，".join(lines) or "通过"
    return "通过"


def select_gates(*, quick: bool, gates: tuple[Gate, ...] = GATES) -> list[Gate]:
    return [gate for gate in gates if not (quick and gate.slow)]


def report(
    *,
    quick: bool = False,
    gates: tuple[Gate, ...] = GATES,
    runner=_run,
) -> int:
    """Run the gates and report them. ``runner`` is injectable so this is testable.

    Only selected, completed PASS results qualify for exit code 0. Quick mode
    explicitly excludes gates; it never excuses an incomplete selected gate.
    """
    selected = select_gates(quick=quick, gates=gates)
    results: list[tuple[Gate, str, str, float]] = []

    print("离线验收门槛")
    print("=" * 60)
    excluded = [gate for gate in gates if gate not in selected]
    for gate in excluded:
        print(f"跳过：{gate.name}（--quick 未选择）")
    for gate in selected:
        print(f"运行：{gate.name} …", flush=True)
        status, detail, elapsed = runner(gate)
        results.append((gate, status, detail, elapsed))
        print(f"  {status}（{elapsed:.1f} 秒）：{detail}")

    print()
    print("这份结果说明了什么")
    print("=" * 60)
    for gate, status, detail, _ in results:
        print(f"  {gate.name}：{status}")
        print(f"    证明：{gate.proves}")
        if gate.does_not_prove:
            print(f"    不证明：{gate.does_not_prove}")

    incomplete = [gate for gate, status, _, _ in results if status != PASS]
    print()
    if not selected:
        print("未选择任何验收门槛，验收未完成。")
    elif incomplete:
        print(f"有 {len(incomplete)} 项必需门槛未通过或未完成：" + "、".join(gate.name for gate in incomplete))
    elif excluded:
        print(f"已选离线门槛通过；--quick 排除 {len(excluded)} 项。")
    else:
        print("全部离线门槛通过。")

    print()
    print("这些门槛都不涉及真实 QQ。真实收发需要下面两条路线之一：")
    print("  官方：.venv/bin/python -m scripts.preflight_official_bot --check")
    print("  账号：.venv/bin/python -m scripts.verify_account_channel --port 3001")
    print("在真实往返发生之前，不能声称机器人已在真实 QQ 环境可用。")
    return 1 if incomplete or not selected else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="运行全部离线验收门槛")
    parser.add_argument("--quick", action="store_true", help="只跑快的门槛")
    args = parser.parse_args()
    return report(quick=args.quick)


if __name__ == "__main__":
    raise SystemExit(main())
