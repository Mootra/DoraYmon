from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from doraymon.config import load_settings
from storage.knowledge_store import rebuild_knowledge_index


def main() -> int:
    parser = argparse.ArgumentParser(description="离线重建 DoraYmon 本地知识库索引")
    parser.add_argument("--knowledge-dir", help="Markdown/TXT 知识目录")
    parser.add_argument("--tokenizer", choices=("trigram", "unicode61"))
    args = parser.parse_args()

    settings = load_settings()
    knowledge_dir = Path(args.knowledge_dir or settings.knowledge_dir)
    if not knowledge_dir.is_absolute():
        knowledge_dir = PROJECT_ROOT / knowledge_dir

    status = rebuild_knowledge_index(
        knowledge_dir,
        tokenizer=args.tokenizer or settings.rag_tokenizer,
        chunk_max_chars=settings.rag_chunk_max_chars,
        chunk_overlap_chars=settings.rag_chunk_overlap_chars,
    )
    print(
        "知识库索引完成："
        f"文档 {status.document_count}，"
        f"分块 {status.chunk_count}，"
        f"tokenizer={status.tokenizer}，"
        f"时间 {status.last_indexed_at}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
