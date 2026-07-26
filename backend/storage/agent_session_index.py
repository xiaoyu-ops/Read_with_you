"""可重建的 Agent Session 搜索索引。

论文级 chat JSON 始终是权威数据；SQLite FTS5 只保存可丢弃、可重建的
派生索引，供 Agent 页面和 Pet 本地工具搜索历史讨论。
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from . import db as db_module
from .agent_workspace import list_chat_states
from .files import now_iso


EXCLUDED_ASSISTANT_KINDS = {
    "welcome",
    "permission_request",
    "agent_run_result",
    "mcp_config_written",
}


def _is_searchable_message(message: dict) -> bool:
    role = str(message.get("role") or "")
    content = str(message.get("content") or "").strip()
    if role not in ("user", "assistant") or not content:
        return False
    if role == "user":
        return True

    meta = message.get("meta") if isinstance(message.get("meta"), dict) else {}
    if str(meta.get("kind") or "") in EXCLUDED_ASSISTANT_KINDS:
        return False
    if meta.get("permission_request") or meta.get("mcp_config_draft"):
        return False
    if str(meta.get("agent_loop_status") or "") in ("error", "timeout"):
        return False
    return True


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fts_phrase(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


async def sync_agent_session_index() -> int:
    """按 chat JSON 的更新时间和消息数增量同步派生索引。"""
    chat_states = list_chat_states()
    async with aiosqlite.connect(db_module.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        paper_cur = await db.execute("SELECT arxiv_id, title FROM papers")
        paper_titles = {str(row[0]): str(row[1] or row[0]) for row in await paper_cur.fetchall()}
        state_cur = await db.execute(
            "SELECT arxiv_id, updated_at, message_count, paper_title FROM agent_session_index_state"
        )
        indexed_states = {str(row["arxiv_id"]): dict(row) for row in await state_cur.fetchall()}

        current_ids: set[str] = set()
        changed = 0
        for chat in chat_states:
            arxiv_id = str(chat.get("arxiv_id") or "").strip()
            if not arxiv_id:
                continue
            current_ids.add(arxiv_id)
            messages = chat.get("messages") if isinstance(chat.get("messages"), list) else []
            updated_at = str(chat.get("updated_at") or "")
            paper_title = paper_titles.get(arxiv_id) or str(
                indexed_states.get(arxiv_id, {}).get("paper_title") or arxiv_id
            )
            previous = indexed_states.get(arxiv_id)
            if (
                previous is not None
                and str(previous.get("updated_at") or "") == updated_at
                and int(previous.get("message_count") or 0) == len(messages)
                and str(previous.get("paper_title") or "") == paper_title
            ):
                continue

            await db.execute("DELETE FROM agent_session_fts WHERE arxiv_id=?", (arxiv_id,))
            for message_index, message in enumerate(messages):
                if not isinstance(message, dict) or not _is_searchable_message(message):
                    continue
                await db.execute(
                    """INSERT INTO agent_session_fts
                       (arxiv_id, message_id, role, paper_title, content, created_at, message_index)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        arxiv_id,
                        str(message.get("id") or f"{arxiv_id}-{message_index}"),
                        str(message.get("role") or ""),
                        paper_title,
                        str(message.get("content") or "").strip(),
                        str(message.get("created_at") or updated_at),
                        message_index,
                    ),
                )
            await db.execute(
                """INSERT INTO agent_session_index_state
                   (arxiv_id, updated_at, message_count, paper_title, indexed_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(arxiv_id) DO UPDATE SET
                     updated_at=excluded.updated_at,
                     message_count=excluded.message_count,
                     paper_title=excluded.paper_title,
                     indexed_at=excluded.indexed_at""",
                (arxiv_id, updated_at, len(messages), paper_title, now_iso()),
            )
            changed += 1

        missing_ids = set(indexed_states) - current_ids
        for arxiv_id in missing_ids:
            await db.execute("DELETE FROM agent_session_fts WHERE arxiv_id=?", (arxiv_id,))
            await db.execute("DELETE FROM agent_session_index_state WHERE arxiv_id=?", (arxiv_id,))
            changed += 1

        await db.commit()
        return changed


async def search_agent_sessions(
    query: str,
    limit: int = 20,
    *,
    exclude_message_id: str | None = None,
) -> list[dict[str, Any]]:
    """搜索历史消息；短中文走 LIKE，其余使用 trigram FTS5。"""
    clean_query = " ".join(str(query or "").split())
    if not clean_query:
        return []
    safe_limit = min(50, max(1, int(limit)))
    await sync_agent_session_index()

    async with aiosqlite.connect(db_module.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if len(clean_query) < 3:
            pattern = f"%{_escape_like(clean_query)}%"
            cur = await db.execute(
                """SELECT
                     f.arxiv_id, f.message_id, f.role,
                     COALESCE(p.title, f.paper_title, f.arxiv_id) AS paper_title,
                     substr(f.content, 1, 260) AS snippet,
                     f.created_at,
                     CASE WHEN p.arxiv_id IS NULL THEN 0 ELSE 1 END AS paper_exists
                   FROM agent_session_fts AS f
                   LEFT JOIN papers AS p ON p.arxiv_id = f.arxiv_id
                   WHERE (f.content LIKE ? ESCAPE '\\'
                      OR f.paper_title LIKE ? ESCAPE '\\')
                     AND (? IS NULL OR f.message_id != ?)
                   ORDER BY datetime(f.created_at) DESC, CAST(f.message_index AS INTEGER) DESC
                   LIMIT ?""",
                (pattern, pattern, exclude_message_id, exclude_message_id, safe_limit),
            )
        else:
            cur = await db.execute(
                """SELECT
                     f.arxiv_id, f.message_id, f.role,
                     COALESCE(p.title, f.paper_title, f.arxiv_id) AS paper_title,
                     snippet(agent_session_fts, 4, '', '', '…', 28) AS snippet,
                     f.created_at,
                     CASE WHEN p.arxiv_id IS NULL THEN 0 ELSE 1 END AS paper_exists
                   FROM agent_session_fts AS f
                   LEFT JOIN papers AS p ON p.arxiv_id = f.arxiv_id
                   WHERE agent_session_fts MATCH ?
                     AND (? IS NULL OR f.message_id != ?)
                   ORDER BY bm25(agent_session_fts), datetime(f.created_at) DESC
                   LIMIT ?""",
                (_fts_phrase(clean_query), exclude_message_id, exclude_message_id, safe_limit),
            )
        return [
            {
                **dict(row),
                "paper_exists": bool(row["paper_exists"]),
            }
            for row in await cur.fetchall()
        ]
