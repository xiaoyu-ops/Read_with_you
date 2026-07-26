"""Portable paper bundle endpoints for the opt-in browser local folder."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..storage import files
from ..storage.agent_session_index import sync_agent_session_index
from ..storage.db import get_paper, insert_paper, update_status
from ..storage.paper_note_index import safe_sync_paper_note_index
from ..storage.portable_cache import (
    acknowledge_portable_cache,
    renew_portable_cache_lease,
)
from ..storage.portable_bundle import (
    PortableBundleError,
    PortableExport,
    apply_staged_portable_bundle,
    build_portable_export,
    parse_portable_manifest,
    safe_paper_directory,
    stage_portable_files,
)


router = APIRouter(tags=["portable-library"])


class PortableCacheAckRequest(BaseModel):
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


def _portable_http_error(error: PortableBundleError) -> HTTPException:
    status = 413 if error.code in {
        "portable_bundle_too_large",
        "portable_file_too_large",
        "portable_manifest_too_large",
        "portable_too_many_files",
    } else 422
    return HTTPException(
        status_code=status,
        detail={"code": error.code, "message": str(error)},
    )


async def _multipart_stream(
    export: PortableExport,
    boundary: str,
) -> AsyncIterator[bytes]:
    manifest = json.dumps(
        export.manifest,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    yield (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="manifest"\r\n'
        "Content-Type: application/json; charset=utf-8\r\n\r\n"
    ).encode("ascii")
    yield manifest
    yield b"\r\n"
    for index, source in enumerate(export.sources):
        yield (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{index}.bin"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("ascii")
        with source.file_path.open("rb") as payload:
            while chunk := await asyncio.to_thread(payload.read, 1024 * 1024):
                yield chunk
        yield b"\r\n"
    yield f"--{boundary}--\r\n".encode("ascii")


@router.get("/papers/{arxiv_id:path}/portable-bundle")
async def download_portable_bundle(
    arxiv_id: str,
    base_revision: str | None = Query(default=None),
):
    paper = await get_paper(arxiv_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="论文不存在。")
    try:
        export = await asyncio.to_thread(
            build_portable_export,
            arxiv_id,
            paper,
            base_revision=base_revision,
        )
    except PortableBundleError as error:
        raise _portable_http_error(error) from error
    boundary = f"peinidu-{uuid4().hex}"
    return StreamingResponse(
        _multipart_stream(export, boundary),
        media_type=f"multipart/form-data; boundary={boundary}",
        headers={
            "Cache-Control": "no-store",
            "X-Peinidu-Portable-Revision": str(export.manifest["revision"]),
            "X-Peinidu-Portable-Type": str(export.manifest["bundle_type"]),
        },
    )


@router.post("/papers/portable-bundle")
async def restore_portable_bundle(
    manifest: UploadFile = File(...),
    file: list[UploadFile] = File(default=[]),
    conflict_policy: str = Form(default="reject"),
):
    if conflict_policy not in {"reject", "keep_local"}:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "portable_conflict_policy",
                "message": "不支持的冲突处理方式。",
            },
        )
    try:
        manifest_data = parse_portable_manifest(await manifest.read())
        paper_id = str(manifest_data["paper_id"])
        current = await get_paper(paper_id)
        current_revision: str | None = None
        if current is not None:
            try:
                current_export = await asyncio.to_thread(
                    build_portable_export,
                    paper_id,
                    current,
                )
                current_revision = str(current_export.manifest["revision"])
            except PortableBundleError as error:
                if error.code not in {"source_pdf_missing", "portable_paper_missing"}:
                    raise
        base_revision = manifest_data.get("base_revision")
        target_exists = safe_paper_directory(files.PAPERS_DIR, paper_id).exists()
        if (
            conflict_policy == "reject"
            and target_exists
            and current_revision is not None
            and base_revision != current_revision
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "portable_revision_conflict",
                    "message": "本地副本和服务端都已变化，请选择保留哪一份。",
                    "current_revision": current_revision,
                    "base_revision": base_revision,
                },
            )

        files.PAPERS_DIR.parent.mkdir(parents=True, exist_ok=True)
        stage_root = await asyncio.to_thread(
            stage_portable_files,
            manifest_data,
            [upload.file for upload in file],
            staging_parent=files.PAPERS_DIR.parent,
        )
        try:
            await asyncio.to_thread(
                apply_staged_portable_bundle,
                manifest_data,
                stage_root,
            )
        except Exception:
            await asyncio.to_thread(_remove_stage, stage_root)
            raise

        metadata = manifest_data["paper"]
        await insert_paper(
            paper_id,
            str(metadata["title"]),
            list(metadata["authors"]),
            str(metadata["source"]),
            str(files.paper_dir(paper_id)),
        )
        status = str(metadata.get("status") or "extracted")
        if status in {
            "extracted",
            "translating",
            "translated",
            "translation_error",
            "analyzed",
        }:
            await update_status(paper_id, status)
        await safe_sync_paper_note_index(paper_id)
        try:
            await sync_agent_session_index()
        except Exception:
            # The FTS index is derived and can be rebuilt on the next search.
            pass
        return {
            "arxiv_id": paper_id,
            "revision": manifest_data["revision"],
            "status": "restored",
        }
    except HTTPException:
        raise
    except PortableBundleError as error:
        raise _portable_http_error(error) from error


def _remove_stage(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


@router.post("/papers/{arxiv_id:path}/portable-bundle/ack")
async def acknowledge_local_portable_bundle(
    arxiv_id: str,
    payload: PortableCacheAckRequest,
):
    try:
        state = await acknowledge_portable_cache(arxiv_id, payload.revision)
    except PortableBundleError as error:
        status = 409 if error.code == "portable_ack_revision_mismatch" else 422
        raise HTTPException(
            status_code=status,
            detail={"code": error.code, "message": str(error)},
        ) from error
    return {
        "arxiv_id": arxiv_id,
        "revision": state["synced_revision"],
        "storage_mode": "local_folder",
        "cache_acknowledged": True,
    }


@router.post("/papers/{arxiv_id:path}/portable-bundle/lease")
async def renew_local_portable_bundle_lease(arxiv_id: str):
    try:
        state = await renew_portable_cache_lease(arxiv_id)
    except PortableBundleError as error:
        raise HTTPException(
            status_code=409,
            detail={"code": error.code, "message": str(error)},
        ) from error
    return {
        "arxiv_id": arxiv_id,
        "lease_until": state["lease_until"],
    }
