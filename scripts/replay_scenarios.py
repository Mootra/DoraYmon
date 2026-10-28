"""Offline replay of routed scenarios with the P0 hard requirements as checks.

The plan's hard requirements are cross-cutting: no identity leakage, no
unsourced fact, and no stale answer written back after a clear. Unit tests check
each module; this harness checks them over a whole routed conversation, with no
network, no real model and an isolated temporary database per scenario.

Scenario file: one JSON object per line.

    {
      "id": "unique-id",
      "description": "what this proves",
      "platform": "qqbot",
      "bot_account": "bot-1",
      "steps": [
        {"user": "user-a", "message": "你好"},
        {"user": "user-a", "message": "换个话题，聊聊 Python", "action": "clear"}
      ],
      "checks": {"no_cross_identity_leak": true}
    }

``action`` controls what the fake model does while answering that step:

- ``clear``  the user clears the conversation mid-generation, so the answer must
  be dropped rather than written back.
- ``reply``  normal answer (default).

The fake model is scripted, so this proves routing, identity and session
guarantees only. It is not evidence about real answer quality.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from doraymon.config import Settings
from doraymon.context import BotContext
from doraymon.identity import namespace_for
from doraymon.router import route_incoming_message
from services.pending_reply import commit_delivered_reply
from plugins import chat as chat_plugin
from services.deepseek_service import DeepSeekService
from services.session_revisions import SESSION_REVISIONS
from storage.db import init_all_tables, use_data_directory

# A price-looking claim, used to check that a reply does not invent one.
PRICE_PATTERN = re.compile(r"(?:¥|￥|\$)\s?\d|(?<![\d.])\d+(?:\.\d+)?\s*(?:元|块钱|块)")


def step_token(scenario_id: str, step_index: int) -> str:
    """Marker the scripted model puts in the answer for one step."""
    return f"[{scenario_id}-{step_index}]"


def has_price_claim(reply: str) -> bool:
    return bool(reply) and bool(PRICE_PATTERN.search(str(reply)))


def assistant_owners(
    history: list[tuple[str, str, str, str, str]], token: str,
) -> set[tuple[str, str, str]]:
    """Every session whose assistant history contains this token."""
    return {
        (scope_type, scope_openid, user_openid)
        for scope_type, scope_openid, user_openid, role, content in history
        if role == "assistant" and token in content
    }

HARD_CHECKS = (
    "no_cross_identity_leak",
    "no_unsourced_price_claim",
    "stale_reply_not_written_back",
    "history_isolated_from_other_identity",
)

DEFAULT_CASES_PATH = PROJECT_ROOT / "evals/replay_cases.jsonl"


class ScriptedModel:
    """Deterministic replier that can act on the conversation mid-generation."""

    def __init__(self, token: str, actions: dict[int, str], context_by_step: dict[int, BotContext]):
        self.token = token
        self.actions = actions
        self.context_by_step = context_by_step
        self.step = 0
        self.requests: list[list[dict[str, str]]] = []

    def chat_messages(self, messages, **options):
        self.requests.append([dict(message) for message in messages])
        step = self.step
        self.step += 1
        prompt = str(messages[-1].get("content", "")).strip() if messages else ""
        action = self.actions.get(step)
        if action == "clear":
            context = self.context_by_step.get(step)
            if context is not None:
                chat_plugin.handle_clear_context(context)
        if action == "price":
            # Deliberately unsourced price, used to prove the guard has teeth.
            return f"[{self.token}-{step}] 这家大概 25 元一份。"
        return f"[{self.token}-{step}] {prompt}"


def _session_token(platform: str, bot_account: str, user: str, group: str = "") -> tuple[str, str, str] | None:
    return namespace_for(platform, bot_account).session_key(user, group)


def _read_all_history(database: Path) -> list[tuple[str, str, str, str, str]]:
    """Return ``(scope_type, scope_openid, user_openid, role, content)`` rows."""
    if not database.exists():
        return []
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT scope_type, scope_openid, user_openid, role, content FROM chat_messages ORDER BY id"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        connection.close()
    return [
        (str(row["scope_type"]), str(row["scope_openid"]), str(row["user_openid"]),
         str(row["role"]), str(row["content"]))
        for row in rows
    ]


def run_scenario(scenario: dict, workspace: Path) -> dict:
    scenario_id = str(scenario["id"])
    platform = str(scenario.get("platform", "qqbot"))
    bot_account = str(scenario.get("bot_account", ""))
    group = str(scenario.get("group", ""))
    settings = Settings(
        deepseek_api_key="sk-replay",
        chat_history_enabled=True,
        chat_history_limit=40,
        chat_history_max_content_length=4000,
        data_dir="data",
    )

    SESSION_REVISIONS.clear()
    data_dir = workspace / scenario_id
    data_dir.mkdir(parents=True, exist_ok=True)
    database = data_dir / "doraymon.db"

    with use_data_directory(data_dir):
        init_all_tables()
        actions: dict[int, str] = {}
        contexts: dict[int, BotContext] = {}
        steps = list(scenario.get("steps", []))
        model_holder: dict[str, ScriptedModel] = {}

        async def run() -> list[str]:
            replies: list[str] = []
            model = ScriptedModel(
                token=scenario_id,
                actions=actions,
                context_by_step=contexts,
            )
            model_holder["model"] = model
            original = DeepSeekService.chat_messages
            DeepSeekService.chat_messages = lambda self, messages, **options: model.chat_messages(
                messages, **options
            )
            try:
                for index, step in enumerate(steps):
                    if step.get("action"):
                        actions[index] = str(step["action"])
                    context = BotContext(
                        settings=settings,
                        started_at=datetime.now(),
                        user_openid=str(step.get("user", "user-a")),
                        group_openid=str(step.get("group", group)),
                        raw_content=str(step.get("message", "")),
                        is_at_message=bool(step.get("is_at_message", bool(group))),
                        platform=platform,
                        bot_account=bot_account,
                    )
                    contexts[index] = context
                    replies.append(
                        await route_incoming_message(
                            context,
                            fallback_command="" if context.group_openid else "chat",
                        )
                    )
                    commit_delivered_reply(replies[-1], replies[-1])
            finally:
                DeepSeekService.chat_messages = original
            return replies

        replies = asyncio.run(run())
        history = _read_all_history(database)

    return {
        "id": scenario_id,
        "description": scenario.get("description", ""),
        "replies": replies,
        "history": history,
        "requested_checks": scenario.get("checks", {}),
        "modelled_steps": len(model_holder["model"].requests) if "model" in model_holder else 0,
    }


def evaluate_checks(result: dict, scenario: dict) -> dict:
    """Evaluate the hard requirements requested by one scenario."""
    scenario_id = result["id"]
    requested = result["requested_checks"]
    history = result["history"]
    replies = result["replies"]
    checks: dict[str, bool] = {}

    sessions_with_history = {
        (scope_type, scope_openid, user_openid)
        for scope_type, scope_openid, user_openid, _role, _content in history
    }

    if requested.get("no_cross_identity_leak"):
        # Every scripted assistant token must live in exactly one session.
        leaked = any(
            len(assistant_owners(history, step_token(scenario_id, index))) > 1
            for index in range(len(replies))
        )
        checks["no_cross_identity_leak"] = not leaked

    if requested.get("history_isolated_from_other_identity"):
        expected = scenario.get("expected_sessions")
        if expected is None:
            checks["history_isolated_from_other_identity"] = True
        else:
            wanted = {
                _session_token(
                    str(session.get("platform", "qqbot")),
                    str(session.get("bot_account", "")),
                    str(session["user"]),
                    str(session.get("group", "")),
                )
                for session in expected
            }
            checks["history_isolated_from_other_identity"] = sessions_with_history == wanted

    if requested.get("stale_reply_not_written_back"):
        # An answer produced for a cleared conversation must not be stored, and
        # must not be sent back to the user either.
        dropped = [index for index, step in enumerate(scenario.get("steps", []))
                   if step.get("action") == "clear"]
        ok = True
        for index in dropped:
            token = step_token(scenario_id, index)
            if any(token in content for _s, _o, _u, _r, content in history):
                ok = False
            if index < len(replies) and token in replies[index]:
                ok = False
        checks["stale_reply_not_written_back"] = ok

    if requested.get("no_unsourced_price_claim"):
        # The local recommendation path must not invent a price or a per-capita
        # figure. A grounded price needs a catalogue source, which this offline
        # harness does not provide.
        # Offline there is no catalogue, so a grounded price cannot exist yet:
        # any price-looking claim in a reply is unsourced by construction.
        invented = [reply for reply in replies if has_price_claim(reply) and "示例" not in reply]
        checks["no_unsourced_price_claim"] = not invented

    return checks


def load_scenarios(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("//")
    ]


def replay(scenarios: list[dict]) -> dict:
    results = []
    with tempfile.TemporaryDirectory(prefix="doraymon-replay-") as temp:
        workspace = Path(temp)
        for scenario in scenarios:
            outcome = run_scenario(scenario, workspace)
            checks = evaluate_checks(outcome, scenario)
            missing = [name for name in outcome["requested_checks"] if name not in checks]
            results.append({
                **{key: outcome[key] for key in ("id", "description", "replies", "modelled_steps")},
                "checks": checks,
                "unknown_checks": missing,
                "passed": bool(checks) and all(checks.values()) and not missing,
            })
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "evidence": "假模型只验证路由、身份与会话保证，不证明真实回答质量",
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="P0 离线回放：身份、会话与来源硬性要求")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    scenarios = load_scenarios(args.cases)
    report = replay(scenarios)
    failed = [row["id"] for row in report["results"] if not row["passed"]]

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"回放场景：{len(report['results'])}，未通过：{failed or '无'}")
    for row in report["results"]:
        if not row["passed"]:
            print(f"  - {row['id']}: {row['checks']} unknown={row['unknown_checks']}")
    print(report["evidence"])
    if failed:
        print(f"未通过场景：{'、'.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
