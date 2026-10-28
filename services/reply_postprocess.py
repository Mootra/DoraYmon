"""Format model replies and their referenced sources."""
from __future__ import annotations

import re
from collections.abc import Iterable

from doraymon.config import Settings
from services.conversation_types import ConversationContext, PromptBundle
from services.deepseek_service import is_model_error
from services.reply_delivery import prepare_reply
from storage.knowledge_store import KnowledgeSearchResult

SOURCE_REFERENCE_PATTERN = re.compile(r"\[(?:来源\s*)?(\d+)\]")


def source_offset_after(contents: Iterable[str]) -> int:
    """Allocate fresh source numbers without reusing retained history citations."""
    return max((int(match.group(1)) for content in contents
                for match in SOURCE_REFERENCE_PATTERN.finditer(content)
                if len(match.group(1)) <= 6), default=0)


def finalize_reply(
    raw: str, ctx: ConversationContext, bundle: PromptBundle, settings: Settings,
) -> str:
    return prepare_reply(
        _append_referenced_sources(str(raw or "").strip(), list(bundle.included_knowledge),
                                   source_offset=bundle.source_offset), settings,
    )


def _append_referenced_sources(
    answer: str,
    results: list[KnowledgeSearchResult],
    *, source_offset: int = 0,
) -> str:
    if not answer or not results or is_model_error(answer):
        return answer

    answer = SOURCE_REFERENCE_PATTERN.sub(lambda match: f"[{match.group(1)}]", answer)

    referenced_numbers = {
        int(match.group(1))
        for match in SOURCE_REFERENCE_PATTERN.finditer(answer)
        if len(match.group(1)) <= 6 and source_offset < int(match.group(1)) <= source_offset + len(results)
    }
    if not referenced_numbers:
        return answer

    lines = ["参考资料："]
    for source_number in sorted(referenced_numbers):
        result = results[source_number - source_offset - 1]
        heading = f" · {result.heading}" if result.heading else ""
        lines.append(f"[{source_number}] {result.title}{heading} ({result.source_path})")
    return f"{answer}\n\n" + "\n".join(lines)
