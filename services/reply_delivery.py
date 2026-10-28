"""Prepare one bounded reply shared by history, CLI and QQ delivery."""
from __future__ import annotations

from doraymon.config import Settings

MAX_REPLY_LENGTH = 1800
DEFAULT_MAX_REPLY_SEGMENTS = 3
DEFAULT_MAX_REPLY_TOTAL_CHARS = 4000
TRUNCATION_SUFFIX = "\n\n[回复过长，已截断]"


def prepare_reply(content: str, settings: Settings) -> str:
    return "".join(split_reply(
        content, max_segments=settings.reply_max_segments,
        max_total_chars=settings.reply_max_total_chars,
    ))


def split_reply(
    content: str, *, max_length: int = MAX_REPLY_LENGTH,
    max_segments: int = DEFAULT_MAX_REPLY_SEGMENTS,
    max_total_chars: int = DEFAULT_MAX_REPLY_TOTAL_CHARS,
) -> list[str]:
    text = str(content or "").strip()
    if not text:
        return []
    segment_limit = max(1, int(max_length))
    segment_count = max(1, int(max_segments))
    total_limit = min(max(1, int(max_total_chars)), segment_limit * segment_count)
    if len(text) > total_limit:
        suffix = TRUNCATION_SUFFIX[:total_limit]
        text = text[:total_limit - len(suffix)] + suffix
    chunks = []
    while len(text) > segment_limit:
        # Leave enough capacity for the remaining text without dropping a tail.
        minimum = max(segment_limit // 2, len(text) - segment_limit * (segment_count - len(chunks) - 1))
        candidates = [text.rfind(marker, minimum - 1, segment_limit) + 1
                      for marker in ("\n", "。", "！", "？", "!", "?", "；", ";", "，", ",", " ")]
        cut = max((pos for pos in candidates if pos >= max(1, minimum)), default=segment_limit)
        chunks.append(text[:cut])
        text = text[cut:]
    if text:
        chunks.append(text)
    return chunks
