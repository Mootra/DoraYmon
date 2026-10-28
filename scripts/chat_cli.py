"""Local routed chat; fake mode always uses a fresh synthetic-data session."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from doraymon.config import Settings, load_settings
from doraymon.context import BotContext
from doraymon.router import route_incoming_message
from services.deepseek_service import DeepSeekService
from services.pending_reply import commit_delivered_reply
from storage.db import init_all_tables, use_data_directory
from storage.knowledge_store import rebuild_knowledge_index


class FakeModel:
    """Deterministic wiring probe, not an estimate of model quality."""
    def __init__(self):
        self.requests: list[list[dict[str, str]]] = []

    def chat_messages(self, messages, **options):
        self.requests.append([dict(message) for message in messages])
        prompt = messages[-1]["content"].split("\n\n")[0]
        turns = sum(message["role"] == "assistant" for message in messages)
        if "只能依据用户消息" in messages[0]["content"]:
            return "假模型：已收到检索资料。[1]"
        return f"假模型（历史 {turns} 轮）：{prompt}"


@contextmanager
def local_session(*, fake: bool, data_dir: Path, rag: bool = False):
    # Fake sessions never inspect an existing database or configuration.
    with ExitStack() as stack:
        data_dir = data_dir.resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        session_dir = Path(stack.enter_context(tempfile.TemporaryDirectory(
            prefix="fake-", dir=data_dir,
        ))) if fake else data_dir
        settings = Settings() if fake else load_settings()
        settings = replace(
            settings, data_dir=str(session_dir), chat_history_enabled=True,
            rag_enabled=rag, knowledge_dir=str(PROJECT_ROOT / "evals/fixtures/knowledge"),
            admin_openids=["cli-user"],
        )
        stack.enter_context(use_data_directory(session_dir))
        model = FakeModel() if fake else None
        if model is not None:
            stack.enter_context(patch.object(
                DeepSeekService, "chat_messages", side_effect=model.chat_messages,
            ))
        init_all_tables()
        if rag:
            rebuild_knowledge_index(
                Path(settings.knowledge_dir), tokenizer=settings.rag_tokenizer,
                chunk_max_chars=settings.rag_chunk_max_chars,
                chunk_overlap_chars=settings.rag_chunk_overlap_chars,
            )
        yield settings, model


async def send_message(settings, text, *, user="cli-user", group="", at=True):
    context = BotContext(
        settings=settings, started_at=datetime.now(), user_openid=user,
        group_openid=group, raw_content=text, is_at_message=bool(group and at),
    )
    reply = await route_incoming_message(
        context, fallback_command="" if group else "chat",
    )
    # This local API presents the returned text directly, without a platform
    # send. Treat that presentation as the CLI's delivery acknowledgement.
    commit_delivered_reply(reply, reply)
    return reply


def main():
    parser = argparse.ArgumentParser(description="不连接 QQ 的本地对话工具")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fake", action="store_true", help="隔离临时数据，不读取 .env，不调用 API")
    mode.add_argument("--real", action="store_true", help="加载模型配置并调用真实 DeepSeek（有费用）")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data/cli")
    parser.add_argument("--user", default="cli-user")
    parser.add_argument("--group", default="", help="群 ID；默认模拟群聊 @")
    parser.add_argument("--no-at", action="store_true")
    parser.add_argument("--rag", action="store_true", help="只索引公开的合成评测资料")
    parser.add_argument("--message", action="append", help="按顺序执行消息，可重复指定")
    args = parser.parse_args()

    async def run(settings):
        if args.message:
            for message in args.message:
                print(await send_message(settings, message, user=args.user,
                                         group=args.group, at=not args.no_at))
        else:
            print("输入 /exit 退出；支持 /help 和上下文管理命令。")
            while True:
                try:
                    message = input("> ")
                except (EOFError, KeyboardInterrupt):
                    break
                if message.strip() == "/exit":
                    break
                print(await send_message(settings, message, user=args.user,
                                         group=args.group, at=not args.no_at))

    with local_session(fake=args.fake, data_dir=args.data_dir, rag=args.rag) as (settings, _):
        asyncio.run(run(settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
