from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from storage.db import get_connection


SUPPORTED_SUFFIXES = {".md", ".txt"}
SUPPORTED_TOKENIZERS = {"trigram", "unicode61"}
DEFAULT_TOKENIZER = "trigram"
HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
SEARCH_SEGMENT_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u9fff]+")
CJK_PATTERN = re.compile(r"^[\u3400-\u9fff]+$")


class KnowledgeIndexUnavailable(RuntimeError):
    """Raised when the local SQLite build cannot create or query FTS5."""


@dataclass(frozen=True)
class KnowledgeDocument:
    source_path: str
    title: str
    content: str
    updated_at: str
    scope_type: str = "public"
    scope_openid: str = ""


@dataclass(frozen=True)
class KnowledgeChunk:
    heading: str
    content: str


@dataclass(frozen=True)
class KnowledgeSearchResult:
    chunk_id: int
    document_id: int
    source_path: str
    title: str
    heading: str
    content: str
    scope_type: str
    scope_openid: str
    score: float


@dataclass(frozen=True)
class KnowledgeIndexStatus:
    document_count: int
    chunk_count: int
    last_indexed_at: str
    tokenizer: str


def discover_documents(knowledge_dir: Path) -> list[KnowledgeDocument]:
    root = Path(knowledge_dir)
    if not root.exists():
        return []
    if not root.is_dir():
        raise ValueError("knowledge_dir must be a directory")

    documents: list[KnowledgeDocument] = []
    for path in sorted(root.rglob("*")):
        if (
            not path.is_file()
            or path.is_symlink()
            or path.suffix.lower() not in SUPPORTED_SUFFIXES
        ):
            continue

        relative_path = path.relative_to(root)
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            continue

        scope_type, scope_openid = _scope_from_relative_path(relative_path)
        documents.append(
            KnowledgeDocument(
                source_path=relative_path.as_posix(),
                title=_document_title(content, path.stem),
                content=content,
                updated_at=datetime.fromtimestamp(
                    path.stat().st_mtime,
                    tz=timezone.utc,
                ).isoformat(timespec="seconds"),
                scope_type=scope_type,
                scope_openid=scope_openid,
            )
        )
    return documents


def split_document(
    content: str,
    max_chars: int = 800,
    overlap_chars: int = 100,
) -> list[KnowledgeChunk]:
    if max_chars < 100:
        raise ValueError("max_chars must be at least 100")
    if overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("overlap_chars must be between 0 and max_chars")

    sections: list[tuple[str, str]] = []
    current_heading = ""
    body_lines: list[str] = []

    def flush_section() -> None:
        body = "\n".join(body_lines).strip()
        if body:
            sections.append((current_heading, body))

    for line in str(content or "").splitlines():
        heading_match = HEADING_PATTERN.match(line.strip())
        if heading_match:
            flush_section()
            current_heading = heading_match.group(2).strip()
            body_lines = []
            continue
        body_lines.append(line.rstrip())
    flush_section()

    chunks: list[KnowledgeChunk] = []
    for heading, body in sections:
        normalized_body = re.sub(r"\n{3,}", "\n\n", body).strip()
        start = 0
        while start < len(normalized_body):
            end = min(len(normalized_body), start + max_chars)
            if end < len(normalized_body):
                boundary = normalized_body.rfind("\n", start, end)
                if boundary > start + max_chars // 2:
                    end = boundary

            chunk_text = normalized_body[start:end].strip()
            if chunk_text:
                chunks.append(KnowledgeChunk(heading=heading, content=chunk_text))
            if end >= len(normalized_body):
                break
            start = max(start + 1, end - overlap_chars)

    return chunks


def rebuild_knowledge_index(
    knowledge_dir: Path,
    tokenizer: str = DEFAULT_TOKENIZER,
    chunk_max_chars: int = 800,
    chunk_overlap_chars: int = 100,
) -> KnowledgeIndexStatus:
    normalized_tokenizer = _normalize_tokenizer(tokenizer)
    documents = discover_documents(Path(knowledge_dir))
    indexed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with closing(get_connection("knowledge")) as connection:
        try:
            connection.execute("BEGIN")
            _drop_schema(connection)
            _create_schema(connection, normalized_tokenizer)

            chunk_count = 0
            for document in documents:
                cursor = connection.execute(
                    """
                    INSERT INTO knowledge_documents (
                        source_path,
                        title,
                        content_hash,
                        updated_at,
                        indexed_at,
                        scope_type,
                        scope_openid
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document.source_path,
                        document.title,
                        hashlib.sha256(document.content.encode("utf-8")).hexdigest(),
                        document.updated_at,
                        indexed_at,
                        document.scope_type,
                        document.scope_openid,
                    ),
                )
                document_id = int(cursor.lastrowid)
                chunks = split_document(
                    document.content,
                    max_chars=chunk_max_chars,
                    overlap_chars=chunk_overlap_chars,
                )
                for chunk_index, chunk in enumerate(chunks):
                    chunk_cursor = connection.execute(
                        """
                        INSERT INTO knowledge_chunks (
                            document_id,
                            chunk_index,
                            title,
                            heading,
                            content
                        )
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            document_id,
                            chunk_index,
                            document.title,
                            chunk.heading,
                            chunk.content,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO knowledge_chunks_fts(rowid, title, heading, content)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            int(chunk_cursor.lastrowid),
                            document.title,
                            chunk.heading,
                            chunk.content,
                        ),
                    )
                    chunk_count += 1

            connection.execute(
                "INSERT INTO knowledge_index_meta(key, value) VALUES ('tokenizer', ?)",
                (normalized_tokenizer,),
            )
            connection.execute(
                "INSERT INTO knowledge_index_meta(key, value) VALUES ('last_indexed_at', ?)",
                (indexed_at,),
            )
            connection.commit()
        except sqlite3.OperationalError as exc:
            connection.rollback()
            if "fts5" in str(exc).lower() or "tokenizer" in str(exc).lower():
                raise KnowledgeIndexUnavailable(
                    "当前 SQLite 未提供所需的 FTS5/tokenizer 支持"
                ) from exc
            raise

    return KnowledgeIndexStatus(
        document_count=len(documents),
        chunk_count=chunk_count,
        last_indexed_at=indexed_at,
        tokenizer=normalized_tokenizer,
    )


def get_knowledge_status() -> KnowledgeIndexStatus:
    with closing(get_connection("knowledge")) as connection:
        if not _table_exists(connection, "knowledge_documents"):
            return KnowledgeIndexStatus(0, 0, "未建立", "未建立")

        document_count = int(
            connection.execute("SELECT COUNT(*) FROM knowledge_documents").fetchone()[0]
        )
        chunk_count = int(
            connection.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0]
        )
        meta = {
            str(row["key"]): str(row["value"])
            for row in connection.execute(
                "SELECT key, value FROM knowledge_index_meta"
            ).fetchall()
        }
    return KnowledgeIndexStatus(
        document_count=document_count,
        chunk_count=chunk_count,
        last_indexed_at=meta.get("last_indexed_at", "未知"),
        tokenizer=meta.get("tokenizer", "未知"),
    )


def search_knowledge(
    query: str,
    limit: int = 3,
    group_openid: str = "",
    user_openid: str = "",
) -> list[KnowledgeSearchResult]:
    normalized_query = str(query or "").strip()
    if not normalized_query or limit <= 0:
        return []

    with closing(get_connection("knowledge")) as connection:
        if not _table_exists(connection, "knowledge_chunks_fts"):
            return []

        meta_row = connection.execute(
            "SELECT value FROM knowledge_index_meta WHERE key = 'tokenizer'"
        ).fetchone()
        tokenizer = str(meta_row["value"] if meta_row else DEFAULT_TOKENIZER)
        fts_query = _build_fts_query(normalized_query, tokenizer=tokenizer)
        if fts_query:
            try:
                rows = connection.execute(
                    """
                    SELECT
                        c.id AS chunk_id,
                        d.id AS document_id,
                        d.source_path,
                        d.title,
                        c.heading,
                        c.content,
                        d.scope_type,
                        d.scope_openid,
                        knowledge_chunks_fts.rank AS score
                    FROM knowledge_chunks_fts
                    JOIN knowledge_chunks AS c
                      ON c.id = knowledge_chunks_fts.rowid
                    JOIN knowledge_documents AS d
                      ON d.id = c.document_id
                    WHERE knowledge_chunks_fts MATCH ?
                      AND (
                        d.scope_type = 'public'
                        OR (d.scope_type = 'group' AND d.scope_openid = ?)
                        OR (d.scope_type = 'private' AND d.scope_openid = ?)
                      )
                    ORDER BY knowledge_chunks_fts.rank
                    LIMIT ?
                    """,
                    (
                        fts_query,
                        str(group_openid or ""),
                        str(user_openid or ""),
                        int(limit),
                    ),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                if "fts5" in str(exc).lower():
                    raise KnowledgeIndexUnavailable(
                        "当前 SQLite 无法查询 FTS5 索引"
                    ) from exc
                raise
        else:
            like_query = f"%{_escape_like(_fallback_like_term(normalized_query))}%"
            rows = connection.execute(
                """
                SELECT
                    c.id AS chunk_id,
                    d.id AS document_id,
                    d.source_path,
                    d.title,
                    c.heading,
                    c.content,
                    d.scope_type,
                    d.scope_openid,
                    0.0 AS score
                FROM knowledge_chunks AS c
                JOIN knowledge_documents AS d
                  ON d.id = c.document_id
                WHERE (
                    c.title LIKE ? ESCAPE '\\'
                    OR c.heading LIKE ? ESCAPE '\\'
                    OR c.content LIKE ? ESCAPE '\\'
                )
                  AND (
                    d.scope_type = 'public'
                    OR (d.scope_type = 'group' AND d.scope_openid = ?)
                    OR (d.scope_type = 'private' AND d.scope_openid = ?)
                  )
                ORDER BY d.id, c.chunk_index
                LIMIT ?
                """,
                (
                    like_query,
                    like_query,
                    like_query,
                    str(group_openid or ""),
                    str(user_openid or ""),
                    int(limit),
                ),
            ).fetchall()

    return [
        KnowledgeSearchResult(
            chunk_id=int(row["chunk_id"]),
            document_id=int(row["document_id"]),
            source_path=str(row["source_path"]),
            title=str(row["title"]),
            heading=str(row["heading"] or ""),
            content=str(row["content"]),
            scope_type=str(row["scope_type"]),
            scope_openid=str(row["scope_openid"] or ""),
            score=float(row["score"]),
        )
        for row in rows
    ]


def _create_schema(connection: sqlite3.Connection, tokenizer: str) -> None:
    connection.execute(
        """
        CREATE TABLE knowledge_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            indexed_at TEXT NOT NULL,
            scope_type TEXT NOT NULL CHECK(scope_type IN ('public', 'group', 'private')),
            scope_openid TEXT NOT NULL DEFAULT ''
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE knowledge_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL,
            title TEXT NOT NULL,
            heading TEXT NOT NULL,
            content TEXT NOT NULL,
            UNIQUE(document_id, chunk_index)
        )
        """
    )
    connection.execute(
        f"""
        CREATE VIRTUAL TABLE knowledge_chunks_fts USING fts5(
            title,
            heading,
            content,
            tokenize = {tokenizer}
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE knowledge_index_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )


def _drop_schema(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE IF EXISTS knowledge_chunks_fts")
    connection.execute("DROP TABLE IF EXISTS knowledge_chunks")
    connection.execute("DROP TABLE IF EXISTS knowledge_documents")
    connection.execute("DROP TABLE IF EXISTS knowledge_index_meta")


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def _normalize_tokenizer(tokenizer: str) -> str:
    normalized = str(tokenizer or "").strip().lower()
    if normalized not in SUPPORTED_TOKENIZERS:
        raise ValueError(
            f"unsupported tokenizer: {normalized or '<empty>'}; "
            f"expected one of {sorted(SUPPORTED_TOKENIZERS)}"
        )
    return normalized


def _document_title(content: str, fallback: str) -> str:
    for line in content.splitlines():
        match = HEADING_PATTERN.match(line.strip())
        if match and len(match.group(1)) == 1:
            return match.group(2).strip()
    return str(fallback or "未命名文档").strip()


def _scope_from_relative_path(relative_path: Path) -> tuple[str, str]:
    parts = relative_path.parts
    if len(parts) >= 3 and parts[0].lower() == "groups":
        return "group", parts[1]
    if len(parts) >= 3 and parts[0].lower() in {"users", "private"}:
        return "private", parts[1]
    return "public", ""


def _build_fts_query(
    query: str,
    tokenizer: str = DEFAULT_TOKENIZER,
    max_terms: int = 16,
) -> str:
    terms: list[str] = []
    for segment in SEARCH_SEGMENT_PATTERN.findall(query.lower()):
        if tokenizer == "trigram" and CJK_PATTERN.fullmatch(segment):
            if len(segment) < 3:
                continue
            segment_terms = [segment[index : index + 3] for index in range(len(segment) - 2)]
        elif tokenizer == "trigram" and len(segment) >= 3:
            segment_terms = [segment]
        elif tokenizer == "unicode61":
            segment_terms = [segment]
        else:
            continue

        for term in segment_terms:
            if term not in terms:
                terms.append(term)
            if len(terms) >= max_terms:
                break
        if len(terms) >= max_terms:
            break

    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fallback_like_term(query: str) -> str:
    segments = SEARCH_SEGMENT_PATTERN.findall(query)
    if not segments:
        return query
    return max(segments, key=len)
