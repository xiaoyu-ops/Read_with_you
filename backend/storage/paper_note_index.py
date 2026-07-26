"""可重建的论文笔记 FTS5 索引。

``paper_note.md`` 与 ``annotations.json`` 始终是权威数据；SQLite 仅保存
可丢弃的派生索引，供 Pet/Agent 做有界的本地检索。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

import aiosqlite

from . import db as db_module
from . import files
from .files import now_iso


logger = logging.getLogger(__name__)

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{2,}")
_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff]{3,}")


def _annotations_revision(annotations: list[dict]) -> str:
    payload = json.dumps(
        annotations,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def split_markdown_sections(markdown: str) -> list[dict[str, Any]]:
    """按 Markdown 标题切分，保留标题层级和稳定的文档顺序。"""
    text = str(markdown or "")
    matches = list(_HEADING_RE.finditer(text))
    sections: list[dict[str, Any]] = []
    first_start = matches[0].start() if matches else len(text)
    intro = text[:first_start].strip()
    if intro:
        sections.append({"heading": "全文笔记", "level": 0, "content": intro, "order": 0})
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end():end].strip()
        sections.append(
            {
                "heading": match.group(2).strip(),
                "level": len(match.group(1)),
                "content": body,
                "order": len(sections),
            }
        )
    if not matches and not sections and text.strip():
        sections.append({"heading": "全文笔记", "level": 0, "content": text.strip(), "order": 0})
    return sections


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS paper_note_index_state (
            arxiv_id TEXT PRIMARY KEY,
            paper_note_revision TEXT NOT NULL,
            annotations_revision TEXT NOT NULL,
            indexed_at TEXT
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS paper_note_fts USING fts5(
            arxiv_id UNINDEXED,
            source_type UNINDEXED,
            annotation_id UNINDEXED,
            kind UNINDEXED,
            page UNINDEXED,
            block_index UNINDEXED,
            region_id UNINDEXED,
            heading,
            content,
            updated_at UNINDEXED,
            source_order UNINDEXED,
            tokenize='trigram'
        );
        """
    )


async def sync_paper_note_index(arxiv_id: str, *, force: bool = False) -> bool:
    """把一篇论文的当前笔记同步到派生索引；返回是否发生重建。"""
    paper_note = files.load_paper_note(arxiv_id)
    annotations = files.load_annotations(arxiv_id)
    paper_revision = str(paper_note["revision"])
    annotations_revision = _annotations_revision(annotations)

    async with aiosqlite.connect(db_module.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_schema(db)
        cur = await db.execute(
            """SELECT paper_note_revision, annotations_revision
               FROM paper_note_index_state WHERE arxiv_id=?""",
            (arxiv_id,),
        )
        previous = await cur.fetchone()
        if (
            not force
            and previous is not None
            and str(previous["paper_note_revision"]) == paper_revision
            and str(previous["annotations_revision"]) == annotations_revision
        ):
            return False

        await db.execute("DELETE FROM paper_note_fts WHERE arxiv_id=?", (arxiv_id,))
        for section in split_markdown_sections(str(paper_note["markdown"])):
            searchable = "\n".join(
                part for part in (section["heading"], section["content"]) if str(part).strip()
            )
            await db.execute(
                """INSERT INTO paper_note_fts
                   (arxiv_id, source_type, annotation_id, kind, page, block_index,
                    region_id, heading, content, updated_at, source_order)
                   VALUES (?, 'paper_note', '', '', '', '', '', ?, ?, ?, ?)""",
                (
                    arxiv_id,
                    section["heading"],
                    searchable,
                    paper_note.get("updated_at") or "",
                    int(section["order"]),
                ),
            )

        for source_order, item in enumerate(annotations):
            note = str(item.get("note") or "").strip()
            quote = str(item.get("text") or "").strip()
            if not note and not quote:
                continue
            selector = item.get("selector") if isinstance(item.get("selector"), dict) else {}
            kind = str(item.get("kind") or "highlight")
            heading = f"选区笔记 · {kind}"
            content = "\n".join(
                part
                for part in (
                    f"你的笔记：{note}" if note else "",
                    f"对应原文：{quote}" if quote else "",
                )
                if part
            )
            await db.execute(
                """INSERT INTO paper_note_fts
                   (arxiv_id, source_type, annotation_id, kind, page, block_index,
                    region_id, heading, content, updated_at, source_order)
                   VALUES (?, 'annotation', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    arxiv_id,
                    str(item.get("id") or ""),
                    kind,
                    selector.get("page") if type(selector.get("page")) is int else "",
                    item.get("block_index") if type(item.get("block_index")) is int else "",
                    str(selector.get("region_id") or ""),
                    heading,
                    content,
                    str(item.get("updated_at") or item.get("created_at") or ""),
                    source_order,
                ),
            )

        await db.execute(
            """INSERT INTO paper_note_index_state
               (arxiv_id, paper_note_revision, annotations_revision, indexed_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(arxiv_id) DO UPDATE SET
                 paper_note_revision=excluded.paper_note_revision,
                 annotations_revision=excluded.annotations_revision,
                 indexed_at=excluded.indexed_at""",
            (arxiv_id, paper_revision, annotations_revision, now_iso()),
        )
        await db.commit()
        return True


async def safe_sync_paper_note_index(arxiv_id: str) -> bool:
    """同步派生索引，但绝不让索引故障回滚已保存的用户笔记。"""
    if db_module.DB_PATH.parent.resolve() != files.PAPERS_DIR.parent.resolve():
        # Isolated storage/API tests may replace only PAPERS_DIR. Never leak
        # their fixtures into the developer's real derived index.
        return False
    try:
        return await sync_paper_note_index(arxiv_id)
    except Exception:
        logger.exception("failed to sync paper note index for %s", arxiv_id)
        return False


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fts_phrase(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _fts_terms(query: str) -> list[str]:
    terms: list[str] = []
    for token in _LATIN_TOKEN_RE.findall(query):
        terms.append(token)
    for run in _CJK_RUN_RE.findall(query):
        terms.extend(run[index:index + 3] for index in range(len(run) - 2))
    deduplicated: list[str] = []
    for term in terms:
        if term not in deduplicated:
            deduplicated.append(term)
    return deduplicated[:16]


def _row_to_result(row: aiosqlite.Row) -> dict[str, Any]:
    def optional_int(value: object) -> int | None:
        if type(value) is int:
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return None

    return {
        "arxiv_id": str(row["arxiv_id"]),
        "source_type": str(row["source_type"]),
        "annotation_id": str(row["annotation_id"] or "") or None,
        "kind": str(row["kind"] or "") or None,
        "page": optional_int(row["page"]),
        "block_index": optional_int(row["block_index"]),
        "region_id": str(row["region_id"] or "") or None,
        "heading": str(row["heading"] or ""),
        "snippet": str(row["snippet"] or "").strip(),
        "updated_at": str(row["updated_at"] or "") or None,
    }


async def search_paper_notes(arxiv_id: str, query: str, limit: int = 3) -> list[dict[str, Any]]:
    """搜索当前论文笔记；无查询时按最近修改返回，适合通用笔记整理。"""
    clean_query = " ".join(str(query or "").split())
    safe_limit = min(20, max(1, int(limit)))
    await sync_paper_note_index(arxiv_id)
    async with aiosqlite.connect(db_module.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if not clean_query:
            cur = await db.execute(
                """SELECT arxiv_id, source_type, annotation_id, kind, page, block_index,
                          region_id, heading, substr(content, 1, 700) AS snippet, updated_at
                   FROM paper_note_fts
                   WHERE arxiv_id=?
                   ORDER BY datetime(updated_at) DESC, CAST(source_order AS INTEGER) DESC
                   LIMIT ?""",
                (arxiv_id, safe_limit),
            )
        elif len(clean_query) < 3:
            pattern = f"%{_escape_like(clean_query)}%"
            cur = await db.execute(
                """SELECT arxiv_id, source_type, annotation_id, kind, page, block_index,
                          region_id, heading, substr(content, 1, 700) AS snippet, updated_at
                   FROM paper_note_fts
                   WHERE arxiv_id=?
                     AND (heading LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\')
                   ORDER BY datetime(updated_at) DESC, CAST(source_order AS INTEGER) DESC
                   LIMIT ?""",
                (arxiv_id, pattern, pattern, safe_limit),
            )
        else:
            terms = _fts_terms(clean_query)
            match_query = " OR ".join(_fts_phrase(term) for term in terms) or _fts_phrase(clean_query)
            cur = await db.execute(
                """SELECT arxiv_id, source_type, annotation_id, kind, page, block_index,
                          region_id, heading,
                          snippet(paper_note_fts, 8, '', '', '…', 42) AS snippet,
                          updated_at
                   FROM paper_note_fts
                   WHERE paper_note_fts MATCH ? AND arxiv_id=?
                   ORDER BY bm25(paper_note_fts), datetime(updated_at) DESC
                   LIMIT ?""",
                (match_query, arxiv_id, safe_limit),
            )
        return [_row_to_result(row) for row in await cur.fetchall()]


async def search_collection_notes(
    collection_id: int,
    query: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """在一个专题的论文笔记中检索，并保留每条原始论文锚点。"""
    collection = await db_module.get_collection(collection_id)
    if collection is None:
        return []
    safe_limit = min(20, max(1, int(limit)))
    ranked: list[tuple[int, dict[str, Any]]] = []
    for paper in collection.get("papers", []):
        arxiv_id = str(paper.get("arxiv_id") or "")
        if not arxiv_id:
            continue
        results = await search_paper_notes(arxiv_id, query, limit=safe_limit)
        for rank, item in enumerate(results):
            ranked.append(
                (
                    rank,
                    {
                        **item,
                        "paper_title": str(paper.get("title") or arxiv_id),
                        "collection_id": collection_id,
                    },
                )
            )
    ranked.sort(key=lambda entry: entry[0])
    return [item for _, item in ranked[:safe_limit]]


def view_paper_note(
    arxiv_id: str,
    *,
    annotation_id: str | None = None,
    heading: str | None = None,
) -> dict[str, Any] | None:
    """从权威文件读取一条选区笔记或主笔记章节。"""
    if annotation_id:
        item = next(
            (entry for entry in files.load_annotations(arxiv_id) if entry.get("id") == annotation_id),
            None,
        )
        if item is None:
            return None
        selector = item.get("selector") if isinstance(item.get("selector"), dict) else {}
        return {
            "arxiv_id": arxiv_id,
            "source_type": "annotation",
            "annotation_id": annotation_id,
            "kind": item.get("kind") or "highlight",
            "page": selector.get("page"),
            "block_index": item.get("block_index"),
            "region_id": selector.get("region_id"),
            "heading": "选区笔记",
            "markdown": str(item.get("note") or ""),
            "quote": str(item.get("text") or ""),
        }

    sections = split_markdown_sections(str(files.load_paper_note(arxiv_id)["markdown"]))
    target = str(heading or "").strip()
    section = next((item for item in sections if item["heading"] == target), None)
    if section is None:
        return None
    return {
        "arxiv_id": arxiv_id,
        "source_type": "paper_note",
        "annotation_id": None,
        "kind": None,
        "page": None,
        "block_index": None,
        "region_id": None,
        "heading": section["heading"],
        "markdown": section["content"],
        "quote": "",
    }


async def build_notes_context(
    arxiv_id: str,
    user_message: str,
    reader_context: dict | None,
    *,
    snippet_budget: int = 2_000,
) -> dict[str, Any]:
    """组装给每轮 Agent 的有界笔记上下文。"""
    summary = files.build_paper_note_summary(arxiv_id)
    annotations = files.load_annotations(arxiv_id)
    reader = reader_context if isinstance(reader_context, dict) else {}
    selected = reader.get("selected_text") if isinstance(reader.get("selected_text"), dict) else {}
    block_index = selected.get("block_index")
    region_id = reader.get("region_id")
    current = None
    for item in reversed(annotations):
        selector = item.get("selector") if isinstance(item.get("selector"), dict) else {}
        region_matches = bool(region_id and selector.get("region_id") == region_id)
        block_matches = type(block_index) is int and item.get("block_index") == block_index
        if region_matches or block_matches:
            current = view_paper_note(arxiv_id, annotation_id=str(item.get("id") or ""))
            break

    relevant = await search_paper_notes(arxiv_id, user_message, limit=6)
    if current and current.get("annotation_id"):
        relevant = [
            item for item in relevant if item.get("annotation_id") != current["annotation_id"]
        ]

    remaining = max(0, int(snippet_budget))
    bounded_current = None
    if current:
        current_text = str(current.get("markdown") or current.get("quote") or "").strip()
        clipped = current_text[:remaining]
        remaining -= len(clipped)
        bounded_current = {**current, "markdown": clipped, "quote": ""}

    bounded_relevant: list[dict[str, Any]] = []
    for item in relevant[:3]:
        if remaining <= 0:
            break
        snippet = str(item.get("snippet") or "").strip()[:remaining]
        if not snippet:
            continue
        bounded_relevant.append({**item, "snippet": snippet})
        remaining -= len(snippet)

    return {
        "has_paper_note": summary["has_paper_note"],
        "selection_note_count": summary["selection_note_count"],
        "kind_counts": summary["kind_counts"],
        "updated_at": summary["updated_at"],
        "current_note": bounded_current,
        "relevant": bounded_relevant,
        "snippet_char_count": int(snippet_budget) - remaining,
    }
