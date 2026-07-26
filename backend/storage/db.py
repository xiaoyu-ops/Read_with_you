"""SQLite 元数据存储（D11 混合方案）。

papers 表：id / arxiv_id / title / authors / source / status / file_path / created_at
查询靠 SQLite（秒搜标题/作者），大文本靠文件系统（files.py）。
升级云端时 SQLite → PostgreSQL（架构已预留）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import aiosqlite

from .files import DATA_DIR

DB_PATH = DATA_DIR / "papers.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    arxiv_id TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    authors TEXT,          -- JSON 数组
    source TEXT,           -- ar5iv | latex | mineru | local_pdf | failed
    status TEXT DEFAULT 'extracted',  -- extracted | translating | translated | translation_error | analyzed
    file_path TEXT,        -- data/papers/{arxiv_id}
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_papers_title ON papers(title);
CREATE INDEX IF NOT EXISTS idx_papers_arxiv_id ON papers(arxiv_id);

CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS collection_papers (
    collection_id INTEGER NOT NULL,
    arxiv_id TEXT NOT NULL,
    added_at TEXT,
    PRIMARY KEY (collection_id, arxiv_id),
    FOREIGN KEY (collection_id) REFERENCES collections(id) ON DELETE CASCADE,
    FOREIGN KEY (arxiv_id) REFERENCES papers(arxiv_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_collection_papers_arxiv_id ON collection_papers(arxiv_id);

CREATE TABLE IF NOT EXISTS agent_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    arxiv_id TEXT NOT NULL,
    collection_id INTEGER,
    task_type TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT DEFAULT '',
    error TEXT DEFAULT '',
    created_at TEXT,
    updated_at TEXT,
    completed_at TEXT,
    FOREIGN KEY (arxiv_id) REFERENCES papers(arxiv_id) ON DELETE CASCADE,
    FOREIGN KEY (collection_id) REFERENCES collections(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_created_at ON agent_tasks(created_at);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_arxiv_id ON agent_tasks(arxiv_id);

CREATE TABLE IF NOT EXISTS agent_session_index_state (
    arxiv_id TEXT PRIMARY KEY,
    updated_at TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,
    paper_title TEXT DEFAULT '',
    indexed_at TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS agent_session_fts USING fts5(
    arxiv_id UNINDEXED,
    message_id UNINDEXED,
    role UNINDEXED,
    paper_title,
    content,
    created_at UNINDEXED,
    message_index UNINDEXED,
    tokenize='trigram'
);

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

CREATE TABLE IF NOT EXISTS pdf_export_runs (
    run_id TEXT PRIMARY KEY,
    arxiv_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'done', 'error', 'cancelled')),
    target_language TEXT NOT NULL DEFAULT 'zh-CN',
    sidecar_job_id TEXT,
    cleanup_pending INTEGER NOT NULL DEFAULT 0,
    cleanup_attempts INTEGER NOT NULL DEFAULT 0,
    source_sha256 TEXT NOT NULL,
    output_sha256 TEXT,
    source_bytes INTEGER NOT NULL,
    output_bytes INTEGER,
    source_pages INTEGER NOT NULL,
    output_pages INTEGER,
    progress REAL,
    stage TEXT NOT NULL DEFAULT '',
    pages_done INTEGER,
    provenance TEXT,
    output_path TEXT,
    error_code TEXT DEFAULT '',
    error_message TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pdf_export_runs_paper_created
    ON pdf_export_runs(arxiv_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pdf_export_runs_one_active
    ON pdf_export_runs(arxiv_id)
    WHERE status IN ('queued', 'running');
"""


async def init_db() -> None:
    """初始化数据库（建表）。在 FastAPI lifespan 里调用。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_SCHEMA)
        await _ensure_agent_task_columns(db)
        await _ensure_pdf_export_run_columns(db)
        await db.commit()


async def _ensure_agent_task_columns(db: aiosqlite.Connection) -> None:
    """兼容已有本地 DB：补充新版本 agent_tasks 列。"""
    cur = await db.execute("PRAGMA table_info(agent_tasks)")
    rows = await cur.fetchall()
    columns = {row[1] for row in rows}
    if "collection_id" not in columns:
        await db.execute("ALTER TABLE agent_tasks ADD COLUMN collection_id INTEGER")


async def _ensure_pdf_export_run_columns(db: aiosqlite.Connection) -> None:
    """兼容旧数据库中的 PDF 导出 Run。"""
    cur = await db.execute("PRAGMA table_info(pdf_export_runs)")
    columns = {row[1] for row in await cur.fetchall()}
    cleanup_state_is_new = "cleanup_pending" not in columns
    additions = {
        "progress": "REAL",
        "stage": "TEXT NOT NULL DEFAULT ''",
        "pages_done": "INTEGER",
        "provenance": "TEXT",
        "cleanup_pending": "INTEGER NOT NULL DEFAULT 0",
        "cleanup_attempts": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, definition in additions.items():
        if name not in columns:
            await db.execute(
                f"ALTER TABLE pdf_export_runs ADD COLUMN {name} {definition}"
            )
    if cleanup_state_is_new:
        await db.execute(
            """UPDATE pdf_export_runs
               SET cleanup_pending=1
               WHERE sidecar_job_id IS NOT NULL AND sidecar_job_id != ''"""
        )
    else:
        await db.execute(
            """UPDATE pdf_export_runs
               SET cleanup_pending=1
               WHERE status IN ('queued', 'running')
                 AND sidecar_job_id IS NOT NULL
                 AND sidecar_job_id != ''"""
        )


def _row_to_dict(row: sqlite3.Row | tuple) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        d = dict(row)
    else:
        # tuple 形式（aiosqlite 默认）
        keys = ["id", "arxiv_id", "title", "authors", "source", "status", "file_path", "created_at", "updated_at"]
        d = dict(zip(keys, row))
    if d.get("authors"):
        try:
            d["authors"] = json.loads(d["authors"])
        except (json.JSONDecodeError, TypeError):
            pass
    return d


async def insert_paper(
    arxiv_id: str,
    title: str,
    authors: list[str],
    source: str,
    file_path: str,
) -> int:
    """插入一条论文元数据，已存在则更新。返回 id。"""
    from .files import now_iso
    ts = now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO papers (arxiv_id, title, authors, source, file_path, created_at, updated_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'extracted')
               ON CONFLICT(arxiv_id) DO UPDATE SET
                 title=excluded.title, authors=excluded.authors,
                 source=excluded.source, file_path=excluded.file_path,
                 updated_at=excluded.updated_at
               RETURNING id""",
            (arxiv_id, title, json.dumps(authors, ensure_ascii=False), source, file_path, ts, ts),
        )
        row = await cur.fetchone()
        await db.commit()
        return row[0] if row else 0


async def update_status(arxiv_id: str, status: str) -> None:
    from .files import now_iso
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE papers SET status=?, updated_at=? WHERE arxiv_id=?",
            (status, now_iso(), arxiv_id),
        )
        await db.commit()


async def try_update_status(
    arxiv_id: str,
    status: str,
    blocked_status: str,
) -> bool:
    """原子更新论文状态；若当前状态为 blocked_status 则不更新。"""
    from .files import now_iso

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute("SELECT status FROM papers WHERE arxiv_id=?", (arxiv_id,))
        row = await cur.fetchone()
        if row is None:
            await db.commit()
            return False
        if row[0] == blocked_status:
            await db.commit()
            return False
        await db.execute(
            "UPDATE papers SET status=?, updated_at=? WHERE arxiv_id=?",
            (status, now_iso(), arxiv_id),
        )
        await db.commit()
        return True


async def get_paper(arxiv_id: str) -> dict[str, Any] | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM papers WHERE arxiv_id=?", (arxiv_id,))
        row = await cur.fetchone()
        return _row_to_dict(row) if row else None


async def list_papers(limit: int = 50) -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM papers ORDER BY datetime(created_at) DESC LIMIT ?", (limit,)
        )
        rows = await cur.fetchall()
        return [_row_to_dict(r) for r in rows]


async def create_collection(name: str) -> dict[str, Any]:
    """创建专题；同名专题直接返回已有记录。"""
    from .files import now_iso

    clean_name = name.strip()
    ts = now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """INSERT INTO collections (name, created_at, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET updated_at=updated_at
               RETURNING *""",
            (clean_name, ts, ts),
        )
        row = await cur.fetchone()
        await db.commit()
        return dict(row)


async def list_collections(arxiv_id: str | None = None) -> list[dict[str, Any]]:
    """列出专题。传 arxiv_id 时附带该论文是否已在专题中。"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if arxiv_id:
            cur = await db.execute(
                """SELECT
                     c.*,
                     COUNT(cp.arxiv_id) AS paper_count,
                     EXISTS(
                       SELECT 1 FROM collection_papers selected
                       WHERE selected.collection_id = c.id AND selected.arxiv_id = ?
                     ) AS contains_paper
                   FROM collections c
                   LEFT JOIN collection_papers cp ON cp.collection_id = c.id
                   GROUP BY c.id
                   ORDER BY datetime(c.updated_at) DESC, c.id DESC""",
                (arxiv_id,),
            )
        else:
            cur = await db.execute(
                """SELECT c.*, COUNT(cp.arxiv_id) AS paper_count, 0 AS contains_paper
                   FROM collections c
                   LEFT JOIN collection_papers cp ON cp.collection_id = c.id
                   GROUP BY c.id
                   ORDER BY datetime(c.updated_at) DESC, c.id DESC"""
            )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_collection(collection_id: int) -> dict[str, Any] | None:
    """读取专题详情和其中论文。"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM collections WHERE id=?", (collection_id,))
        collection = await cur.fetchone()
        if collection is None:
            return None
        paper_cur = await db.execute(
            """SELECT p.*, cp.added_at
               FROM collection_papers cp
               JOIN papers p ON p.arxiv_id = cp.arxiv_id
               WHERE cp.collection_id=?
               ORDER BY datetime(cp.added_at) DESC""",
            (collection_id,),
        )
        papers = await paper_cur.fetchall()
        data = dict(collection)
        data["papers"] = [_row_to_dict(p) for p in papers]
        return data


async def add_paper_to_collection(collection_id: int, arxiv_id: str) -> None:
    """把论文加入专题。调用方负责确认专题和论文存在。"""
    from .files import now_iso

    ts = now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR IGNORE INTO collection_papers (collection_id, arxiv_id, added_at)
               VALUES (?, ?, ?)""",
            (collection_id, arxiv_id, ts),
        )
        await db.execute(
            "UPDATE collections SET updated_at=? WHERE id=?",
            (ts, collection_id),
        )
        await db.commit()


async def remove_paper_from_collection(collection_id: int, arxiv_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM collection_papers WHERE collection_id=? AND arxiv_id=?",
            (collection_id, arxiv_id),
        )
        await db.commit()


async def create_agent_task(
    arxiv_id: str,
    task_type: str,
    summary: str = "",
    collection_id: int | None = None,
) -> int:
    """创建 Agent 任务记录，返回 task id。"""
    from .files import now_iso

    ts = now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """INSERT INTO agent_tasks
               (arxiv_id, collection_id, task_type, status, summary, created_at, updated_at)
               VALUES (?, ?, ?, 'running', ?, ?, ?)""",
            (arxiv_id, collection_id, task_type, summary, ts, ts),
        )
        await db.commit()
        return int(cur.lastrowid)


async def try_create_agent_task(
    arxiv_id: str,
    task_type: str,
    summary: str = "",
    collection_id: int | None = None,
) -> tuple[int, bool]:
    """创建 Agent 任务；若同类任务正在运行，返回已有 task id。

    使用 SQLite `BEGIN IMMEDIATE` 获得写锁，避免多 worker 同时插入同一
    arxiv_id + task_type 的 running 任务。
    """
    from .files import now_iso

    ts = now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            """SELECT id FROM agent_tasks
               WHERE arxiv_id=? AND task_type=? AND status='running'
               ORDER BY datetime(created_at) DESC, id DESC
               LIMIT 1""",
            (arxiv_id, task_type),
        )
        row = await cur.fetchone()
        if row:
            await db.commit()
            return int(row[0]), False

        cur = await db.execute(
            """INSERT INTO agent_tasks
               (arxiv_id, collection_id, task_type, status, summary, created_at, updated_at)
               VALUES (?, ?, ?, 'running', ?, ?, ?)""",
            (arxiv_id, collection_id, task_type, summary, ts, ts),
        )
        await db.commit()
        return int(cur.lastrowid), True


async def update_agent_task(
    task_id: int,
    status: str,
    summary: str = "",
    error: str = "",
) -> None:
    """更新 Agent 任务状态。终态写 completed_at。"""
    from .files import now_iso

    ts = now_iso()
    completed_at = ts if status in ("done", "error", "cancelled") else None
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE agent_tasks
               SET status=?, summary=?, error=?, updated_at=?, completed_at=COALESCE(?, completed_at)
               WHERE id=?""",
            (status, summary, error, ts, completed_at, task_id),
        )
        await db.commit()


async def sweep_stale_agent_tasks(error_message: str = "后端重启，任务中断。请重新发起。") -> int:
    """启动清扫：把上一进程遗留的 running 任务标记为 error（后台执行体不跨进程存活）。"""
    from .files import now_iso

    ts = now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """UPDATE agent_tasks
               SET status='error', error=?, updated_at=?, completed_at=?
               WHERE status='running'""",
            (error_message, ts, ts),
        )
        await db.commit()
        return cur.rowcount or 0


async def list_agent_tasks(limit: int = 50) -> list[dict[str, Any]]:
    """列出 Agent 任务历史，附带论文标题。"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT
                 t.*,
                 p.title AS paper_title,
                 c.name AS collection_name
               FROM agent_tasks t
               LEFT JOIN papers p ON p.arxiv_id = t.arxiv_id
               LEFT JOIN collections c ON c.id = t.collection_id
               ORDER BY datetime(t.created_at) DESC, t.id DESC
               LIMIT ?""",
            (limit,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


def _pdf_export_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    data["id"] = data.pop("run_id")
    raw_provenance = data.get("provenance")
    if isinstance(raw_provenance, str) and raw_provenance:
        try:
            parsed_provenance = json.loads(raw_provenance)
            data["provenance"] = (
                parsed_provenance if isinstance(parsed_provenance, dict) else None
            )
        except (json.JSONDecodeError, TypeError):
            data["provenance"] = None
    elif not isinstance(raw_provenance, dict):
        data["provenance"] = None
    data.setdefault("progress", None)
    data.setdefault("stage", "")
    data.setdefault("pages_done", None)
    raw_cleanup_pending = data.get("cleanup_pending", False)
    if isinstance(raw_cleanup_pending, str):
        data["cleanup_pending"] = raw_cleanup_pending.strip().lower() in {
            "1",
            "true",
        }
    else:
        data["cleanup_pending"] = bool(raw_cleanup_pending)
    try:
        data["cleanup_attempts"] = max(0, int(data.get("cleanup_attempts") or 0))
    except (TypeError, ValueError):
        data["cleanup_attempts"] = 0
    return data


async def try_create_pdf_export_run(
    *,
    run_id: str,
    arxiv_id: str,
    source_sha256: str,
    source_bytes: int,
    source_pages: int,
    target_language: str = "zh-CN",
    reuse_completed: bool = True,
) -> tuple[dict[str, Any], bool]:
    """原子创建导出 Run；优先复用 active 或同源已完成 Run。"""
    from .files import now_iso

    ts = now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            """SELECT * FROM pdf_export_runs
               WHERE arxiv_id=? AND status IN ('queued', 'running')
               ORDER BY datetime(created_at) DESC LIMIT 1""",
            (arxiv_id,),
        )
        existing = await cur.fetchone()
        if existing is not None:
            await db.commit()
            return _pdf_export_row(existing) or {}, False
        if reuse_completed:
            cur = await db.execute(
                """SELECT * FROM pdf_export_runs
                   WHERE arxiv_id=? AND source_sha256=? AND status='done'
                   ORDER BY datetime(completed_at) DESC, rowid DESC LIMIT 1""",
                (arxiv_id, source_sha256),
            )
            completed = await cur.fetchone()
            if completed is not None:
                await db.commit()
                return _pdf_export_row(completed) or {}, False
        await db.execute(
            """INSERT INTO pdf_export_runs
               (run_id, arxiv_id, status, target_language, source_sha256,
                source_bytes, source_pages, created_at, updated_at)
               VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                arxiv_id,
                target_language,
                source_sha256,
                source_bytes,
                source_pages,
                ts,
                ts,
            ),
        )
        cur = await db.execute("SELECT * FROM pdf_export_runs WHERE run_id=?", (run_id,))
        row = await cur.fetchone()
        await db.commit()
        return _pdf_export_row(row) or {}, True


async def get_pdf_export_run(run_id: str) -> dict[str, Any] | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM pdf_export_runs WHERE run_id=?", (run_id,))
        return _pdf_export_row(await cur.fetchone())


async def list_pdf_export_runs(arxiv_id: str, limit: int = 50) -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM pdf_export_runs WHERE arxiv_id=?
               ORDER BY datetime(created_at) DESC, rowid DESC LIMIT ?""",
            (arxiv_id, limit),
        )
        rows = await cur.fetchall()
        return [_pdf_export_row(row) or {} for row in rows]


async def list_active_pdf_export_runs() -> list[dict[str, Any]]:
    """读取启动清扫前仍可能对应远端 sidecar job 的 active Runs。"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM pdf_export_runs
               WHERE status IN ('queued', 'running')
               ORDER BY datetime(created_at), rowid"""
        )
        rows = await cur.fetchall()
        return [_pdf_export_row(row) or {} for row in rows]


async def list_pdf_export_cleanup_pending_runs(
    limit: int = 16,
    *,
    include_active: bool = False,
) -> list[dict[str, Any]]:
    """Return a bounded oldest-first batch of remote jobs needing deletion.

    The active-row compatibility branch preserves cleanup for databases created
    before ``cleanup_pending`` existed.
    """
    safe_limit = max(1, min(100, int(limit)))
    if include_active:
        pending_clause = "(cleanup_pending=1 OR status IN ('queued', 'running'))"
    else:
        pending_clause = (
            "cleanup_pending=1 AND status NOT IN ('queued', 'running')"
        )
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"""SELECT * FROM pdf_export_runs
               WHERE sidecar_job_id IS NOT NULL
                 AND sidecar_job_id != ''
                 AND ({pending_clause})
               ORDER BY datetime(updated_at), rowid
               LIMIT ?""",
            (safe_limit,),
        )
        rows = await cur.fetchall()
        return [_pdf_export_row(row) or {} for row in rows]


async def list_completed_pdf_export_runs(
    arxiv_id: str, source_sha256: str
) -> list[dict[str, Any]]:
    """按新到旧列出同一源文件的完成 Runs，供磁盘安全复用检查。"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM pdf_export_runs
               WHERE arxiv_id=? AND source_sha256=? AND status='done'
               ORDER BY datetime(completed_at) DESC, rowid DESC""",
            (arxiv_id, source_sha256),
        )
        rows = await cur.fetchall()
        return [_pdf_export_row(row) or {} for row in rows]


async def transition_pdf_export_run(
    run_id: str,
    *,
    from_statuses: tuple[str, ...],
    status: str,
    sidecar_job_id: str | None = None,
    output_sha256: str | None = None,
    output_bytes: int | None = None,
    output_pages: int | None = None,
    progress: float | None = None,
    stage: str | None = None,
    pages_done: int | None = None,
    provenance: dict[str, Any] | None = None,
    output_path: str | None = None,
    error_code: str = "",
    error_message: str = "",
) -> bool:
    """条件式状态迁移；终态不会被迟到的 sidecar 结果覆盖。"""
    from .files import now_iso

    if not from_statuses:
        return False
    ts = now_iso()
    completed_at = ts if status in {"done", "error", "cancelled"} else None
    placeholders = ",".join("?" for _ in from_statuses)
    values: list[Any] = [
        status,
        sidecar_job_id,
        output_sha256,
        output_bytes,
        output_pages,
        progress,
        stage,
        pages_done,
        (
            json.dumps(provenance, ensure_ascii=False, sort_keys=True)
            if provenance is not None
            else None
        ),
        output_path,
        error_code,
        error_message,
        ts,
        completed_at,
        run_id,
        *from_statuses,
    ]
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            f"""UPDATE pdf_export_runs SET
                    status=?,
                    sidecar_job_id=COALESCE(?, sidecar_job_id),
                    output_sha256=COALESCE(?, output_sha256),
                    output_bytes=COALESCE(?, output_bytes),
                    output_pages=COALESCE(?, output_pages),
                    progress=COALESCE(?, progress),
                    stage=COALESCE(?, stage),
                    pages_done=COALESCE(?, pages_done),
                    provenance=COALESCE(?, provenance),
                    output_path=COALESCE(?, output_path),
                    error_code=?, error_message=?, updated_at=?,
                    completed_at=COALESCE(?, completed_at)
                WHERE run_id=? AND status IN ({placeholders})""",
            values,
        )
        await db.commit()
        return (cur.rowcount or 0) == 1


async def update_pdf_export_progress(
    run_id: str,
    *,
    progress: float | None,
    stage: str,
    pages_done: int | None,
) -> bool:
    """保存 sidecar 的最新进度；未知值保持 NULL。"""
    from .files import now_iso

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """UPDATE pdf_export_runs
               SET progress=?, stage=?, pages_done=?, updated_at=?
               WHERE run_id=? AND status='running'""",
            (progress, stage, pages_done, now_iso(), run_id),
        )
        await db.commit()
        return (cur.rowcount or 0) == 1


async def set_pdf_export_sidecar_job(run_id: str, sidecar_job_id: str) -> bool:
    """保存远端 job id，但只允许 active Run 写入。"""
    from .files import now_iso

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """UPDATE pdf_export_runs
               SET sidecar_job_id=?, cleanup_pending=1, cleanup_attempts=0,
                   updated_at=?
               WHERE run_id=? AND status IN ('queued', 'running')""",
            (sidecar_job_id, now_iso(), run_id),
        )
        await db.commit()
        return (cur.rowcount or 0) == 1


async def record_pdf_export_cleanup_result(run_id: str, *, deleted: bool) -> bool:
    """Persist one remote-delete outcome without losing a future retry."""
    from .files import now_iso

    async with aiosqlite.connect(DB_PATH) as db:
        if deleted:
            cur = await db.execute(
                """UPDATE pdf_export_runs
                   SET cleanup_pending=0, updated_at=?
                   WHERE run_id=? AND cleanup_pending=1""",
                (now_iso(), run_id),
            )
        else:
            cur = await db.execute(
                """UPDATE pdf_export_runs
                   SET cleanup_pending=1,
                       cleanup_attempts=cleanup_attempts + 1,
                       updated_at=?
                   WHERE run_id=? AND sidecar_job_id IS NOT NULL""",
                (now_iso(), run_id),
            )
        await db.commit()
        return (cur.rowcount or 0) == 1


async def mark_pdf_export_cleanup_pending(run_id: str) -> bool:
    """Ensure a known remote job survives interruption before a delete attempt."""
    from .files import now_iso

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """UPDATE pdf_export_runs
               SET cleanup_pending=1, updated_at=?
               WHERE run_id=? AND sidecar_job_id IS NOT NULL""",
            (now_iso(), run_id),
        )
        await db.commit()
        return (cur.rowcount or 0) == 1


async def sweep_stale_pdf_export_runs(
    error_message: str = "后端重启，PDF 导出任务中断。请重新发起。",
) -> int:
    """启动清扫：进程重启后 queued/running 的执行体已经不存在。"""
    from .files import now_iso

    ts = now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """UPDATE pdf_export_runs
               SET status='error', error_code='backend_restarted', error_message=?,
                   updated_at=?, completed_at=?
               WHERE status IN ('queued', 'running')""",
            (error_message, ts, ts),
        )
        await db.commit()
        return cur.rowcount or 0
