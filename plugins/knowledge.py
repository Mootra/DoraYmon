from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from doraymon.config import PROJECT_ROOT
from doraymon.context import BotContext
from services.rag_service import RagService, format_sources
from storage.knowledge_store import (
    KnowledgeIndexUnavailable,
    get_knowledge_status,
    rebuild_knowledge_index,
)


async def handle_ask(context: BotContext) -> str:
    question = context.args.strip()
    if not question:
        return "请在 /知识问 后输入问题，例如：/知识问 如何在本地启动项目"
    if not context.settings.rag_enabled:
        return "本地知识库问答当前未开启，请设置 BOT_ENABLE_RAG=true。"

    service = RagService(context.settings)
    try:
        response = await asyncio.to_thread(
            service.answer,
            question,
            context.group_openid,
            context.user_openid,
        )
    except (KnowledgeIndexUnavailable, sqlite3.Error):
        return "当前 SQLite 不支持知识库全文检索，请检查 FTS5 配置。"
    return response.answer


def handle_status(context: BotContext) -> str:
    try:
        status = get_knowledge_status()
    except (KnowledgeIndexUnavailable, sqlite3.Error):
        return "当前 SQLite 不支持知识库全文检索，请检查 FTS5 配置。"

    enabled_text = "开启" if context.settings.rag_enabled else "关闭"
    return "\n".join(
        [
            "📚 DoraYmon 知识库状态",
            f"问答开关：{enabled_text}",
            f"文档数：{status.document_count}",
            f"分块数：{status.chunk_count}",
            f"更新时间：{status.last_indexed_at}",
            (
                f"索引类型：SQLite FTS5 / {status.tokenizer}"
                if status.tokenizer != "未建立"
                else "索引类型：未建立"
            ),
        ]
    )


def handle_sources(context: BotContext) -> str:
    question = context.args.strip()
    if not question:
        return "请在 /知识来源 后输入要检索的问题。"

    service = RagService(context.settings)
    try:
        results = service.retrieve(
            question,
            group_openid=context.group_openid,
            user_openid=context.user_openid,
        )
    except (KnowledgeIndexUnavailable, sqlite3.Error):
        return "当前 SQLite 不支持知识库全文检索，请检查 FTS5 配置。"
    return format_sources(results)


async def handle_rebuild(context: BotContext) -> str:
    if not context.user_openid or context.user_openid not in context.settings.admin_openids:
        return "你没有权限重建知识库。"

    knowledge_dir = Path(context.settings.knowledge_dir)
    if not knowledge_dir.is_absolute():
        knowledge_dir = PROJECT_ROOT / knowledge_dir

    try:
        status = await asyncio.to_thread(
            rebuild_knowledge_index,
            knowledge_dir,
            context.settings.rag_tokenizer,
            context.settings.rag_chunk_max_chars,
            context.settings.rag_chunk_overlap_chars,
        )
    except KnowledgeIndexUnavailable:
        return "当前 SQLite 不支持知识库全文检索，请检查 FTS5 配置。"
    except (OSError, ValueError, sqlite3.Error):
        return "知识库重建失败，请检查知识目录和分块配置。"

    return "\n".join(
        [
            "知识库重建完成。",
            f"文档数：{status.document_count}",
            f"分块数：{status.chunk_count}",
            f"索引类型：SQLite FTS5 / {status.tokenizer}",
        ]
    )
