"""Bounded server cache for papers safely mirrored to a user-owned folder."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import files
from .agent_workspace import load_runs
from .db import get_paper, list_pdf_export_runs
from .portable_bundle import (
    PortableBundleError,
    _agent_chat_path,
    build_portable_export,
    safe_paper_directory,
)


PORTABLE_CACHE_STATE_VERSION = 1
PORTABLE_CACHE_IDLE_SECONDS = 7 * 24 * 60 * 60
PORTABLE_CACHE_MAX_BYTES = 2 * 1024 * 1024 * 1024
PORTABLE_CACHE_LEASE_SECONDS = 5 * 60

_cleanup_lock = asyncio.Lock()


def _state_root() -> Path:
    return files.DATA_DIR / "portable_cache_state"


def _state_path(paper_id: str) -> Path:
    digest = hashlib.sha256(paper_id.encode("utf-8")).hexdigest()
    return _state_root() / f"{digest}.json"


def load_portable_cache_state(paper_id: str) -> dict[str, Any] | None:
    path = _state_path(paper_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("version") != PORTABLE_CACHE_STATE_VERSION
        or payload.get("paper_id") != paper_id
        or payload.get("storage_mode") != "local_folder"
    ):
        return None
    return payload


def _save_state(state: dict[str, Any]) -> None:
    root = _state_root()
    root.mkdir(parents=True, exist_ok=True)
    files._write_json(_state_path(str(state["paper_id"])), state)


async def acknowledge_portable_cache(
    paper_id: str,
    revision: str,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    metadata = await get_paper(paper_id)
    if metadata is None:
        raise PortableBundleError("portable_paper_missing", "论文不存在。")
    export = await asyncio.to_thread(
        build_portable_export,
        paper_id,
        metadata,
    )
    current_revision = str(export.manifest["revision"])
    if revision != current_revision:
        raise PortableBundleError(
            "portable_ack_revision_mismatch",
            "本地确认的 revision 与当前服务端论文不一致。",
        )
    timestamp = float(now if now is not None else time.time())
    state = {
        "version": PORTABLE_CACHE_STATE_VERSION,
        "paper_id": paper_id,
        "storage_mode": "local_folder",
        "synced_revision": current_revision,
        "acknowledged_at": timestamp,
        "last_accessed_at": timestamp,
        "lease_until": timestamp + PORTABLE_CACHE_LEASE_SECONDS,
        "cached": True,
        "evicted_at": None,
    }
    _save_state(state)
    return state


def touch_portable_cache(
    paper_id: str,
    *,
    lease_seconds: int = 0,
    now: float | None = None,
) -> dict[str, Any] | None:
    state = load_portable_cache_state(paper_id)
    if state is None:
        return None
    timestamp = float(now if now is not None else time.time())
    state["last_accessed_at"] = timestamp
    if lease_seconds > 0:
        state["lease_until"] = max(
            float(state.get("lease_until") or 0),
            timestamp + lease_seconds,
        )
    state["cached"] = safe_paper_directory(files.PAPERS_DIR, paper_id).is_dir()
    _save_state(state)
    return state


async def renew_portable_cache_lease(paper_id: str) -> dict[str, Any]:
    async with _cleanup_lock:
        state = await asyncio.to_thread(
            touch_portable_cache,
            paper_id,
            lease_seconds=PORTABLE_CACHE_LEASE_SECONDS,
        )
    if state is None:
        raise PortableBundleError(
            "portable_cache_not_acknowledged",
            "这篇论文尚未确认保存到本地，服务端不会清理它。",
        )
    return state


def _load_states() -> list[dict[str, Any]]:
    root = _state_root()
    if not root.is_dir():
        return []
    states: list[dict[str, Any]] = []
    for path in root.glob("*.json"):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        paper_id = str(state.get("paper_id") or "") if isinstance(state, dict) else ""
        if paper_id and load_portable_cache_state(paper_id) is not None:
            states.append(state)
    return states


def _directory_size(path: Path) -> int:
    total = 0
    if not path.is_dir():
        return total
    for item in path.rglob("*"):
        if item.is_file() and not item.is_symlink():
            total += item.stat().st_size
    return total


async def _is_protected(
    paper_id: str,
    state: dict[str, Any],
    metadata: dict[str, Any],
    *,
    now: float,
) -> tuple[bool, str | None]:
    if float(state.get("lease_until") or 0) > now:
        return True, "active_lease"
    if str(metadata.get("status") or "") == "translating":
        return True, "translation_running"
    if any(
        str(run.get("status") or "") in {"running", "waiting_permission"}
        for run in load_runs(paper_id, limit=10_000)
    ):
        return True, "agent_running"
    pdf_runs = await list_pdf_export_runs(paper_id, limit=10)
    if any(str(run.get("status") or "") in {"queued", "running"} for run in pdf_runs):
        return True, "pdf_export_running"
    return False, None


async def _eligible_state(
    state: dict[str, Any],
    *,
    now: float,
) -> tuple[bool, str | None]:
    paper_id = str(state["paper_id"])
    metadata = await get_paper(paper_id)
    if metadata is None:
        return False, "metadata_missing"
    protected, reason = await _is_protected(paper_id, state, metadata, now=now)
    if protected:
        return False, reason
    try:
        export = await asyncio.to_thread(build_portable_export, paper_id, metadata)
    except PortableBundleError as error:
        return False, error.code
    if str(export.manifest["revision"]) != str(state.get("synced_revision") or ""):
        return False, "local_revision_stale"
    return True, None


def _evict_cache_files(paper_id: str, state: dict[str, Any], *, now: float) -> int:
    target = safe_paper_directory(files.PAPERS_DIR, paper_id)
    if not target.is_dir():
        state["cached"] = False
        state["evicted_at"] = now
        _save_state(state)
        return 0
    size = _directory_size(target)
    trash = files.DATA_DIR / f".portable-cache-trash-{uuid4().hex}"
    trash.mkdir(parents=True, exist_ok=False)
    staged_paper = trash / "paper"
    chat = _agent_chat_path(files.DATA_DIR / "agent_workspace", paper_id)
    staged_chat = trash / "chat.json"
    moved_paper = False
    moved_chat = False
    try:
        os.replace(target, staged_paper)
        moved_paper = True
        if chat.is_file():
            staged_chat.parent.mkdir(parents=True, exist_ok=True)
            os.replace(chat, staged_chat)
            moved_chat = True
        state["cached"] = False
        state["evicted_at"] = now
        state["lease_until"] = 0
        _save_state(state)
    except Exception:
        if moved_chat and staged_chat.exists():
            chat.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged_chat, chat)
        if moved_paper and staged_paper.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged_paper, target)
        raise
    finally:
        shutil.rmtree(trash, ignore_errors=True)
    return size


async def enforce_portable_cache_limits(
    *,
    now: float | None = None,
    idle_seconds: int = PORTABLE_CACHE_IDLE_SECONDS,
    max_bytes: int = PORTABLE_CACHE_MAX_BYTES,
) -> dict[str, Any]:
    timestamp = float(now if now is not None else time.time())
    async with _cleanup_lock:
        states = await asyncio.to_thread(_load_states)
        items: list[dict[str, Any]] = []
        total_before = 0
        for state in states:
            paper_id = str(state["paper_id"])
            size = await asyncio.to_thread(
                _directory_size,
                safe_paper_directory(files.PAPERS_DIR, paper_id),
            )
            total_before += size
            items.append({"state": state, "size": size})
        items.sort(key=lambda item: float(item["state"].get("last_accessed_at") or 0))

        evicted: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        total = total_before
        checked: dict[str, tuple[bool, str | None]] = {}
        for item in items:
            state = item["state"]
            paper_id = str(state["paper_id"])
            if item["size"] <= 0:
                continue
            expired = timestamp - float(state.get("last_accessed_at") or 0) >= idle_seconds
            over_limit = total > max_bytes
            if not expired and not over_limit:
                continue
            eligible, reason = await _eligible_state(state, now=timestamp)
            checked[paper_id] = (eligible, reason)
            if not eligible:
                skipped.append({"paper_id": paper_id, "reason": reason})
                continue
            removed = await asyncio.to_thread(
                _evict_cache_files,
                paper_id,
                state,
                now=timestamp,
            )
            total -= removed
            evicted.append(
                {
                    "paper_id": paper_id,
                    "bytes": removed,
                    "reason": "idle_expired" if expired else "capacity_lru",
                }
            )
        return {
            "total_bytes_before": total_before,
            "total_bytes_after": max(0, total),
            "max_bytes": max_bytes,
            "idle_seconds": idle_seconds,
            "evicted": evicted,
            "skipped": skipped,
            "limit_satisfied": total <= max_bytes,
        }
