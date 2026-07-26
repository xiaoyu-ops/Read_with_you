"""Monolingual Chinese PDF export Run API."""

from __future__ import annotations

import asyncio
import io
import os
import zipfile
from pathlib import Path
from typing import Any, Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from ..llm.config import get_config
from ..pdf_export.errors import PdfExportError, RETRYABLE_ERROR_CODES
from ..pdf_export.service import (
    cancel_pdf_export_run,
    create_pdf_export_run,
    pdf_export_download_url_allowed,
    probe_pdf_export_capability,
    _source_pdf_path,
    validated_pdf_export_download_path,
)
from ..storage.db import (
    get_pdf_export_run,
    list_active_pdf_export_runs,
    list_pdf_export_runs,
)
from .routes_config import _require_admin

router = APIRouter(tags=["pdf-exports"])
_THIRD_PARTY_NOTICE = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "third-party"
    / "pdf-export-sidecar.md"
)
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_BAKED_WRAPPER_SOURCE_ROOT = Path("/app/pdf_export_wrapper_source")
_WRAPPER_ARCHIVE_PREFIX = "sidecar/pdf_export/"
_WRAPPER_SOURCE_FILES = (
    "sidecar/pdf_export/app.py",
    "sidecar/pdf_export/Dockerfile",
    "sidecar/pdf_export/entrypoint.sh",
    "sidecar/pdf_export/healthcheck.py",
    "sidecar/pdf_export/runtime_probe.py",
    "sidecar/pdf_export/README.md",
    "sidecar/pdf_export/THIRD_PARTY.md",
    "sidecar/pdf_export/tests/__init__.py",
    "sidecar/pdf_export/tests/test_app.py",
    "backend/Dockerfile",
    "deploy/nginx.conf",
    "docker-compose.yml",
    "scripts/verify_pdf_export_sidecar.py",
    "docs/third-party/pdf-export-sidecar.md",
)
_WRAPPER_SOURCE_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_WRAPPER_EXECUTABLES = {
    "sidecar/pdf_export/entrypoint.sh",
    "scripts/verify_pdf_export_sidecar.py",
}
_PDF_EXPORT_CREATION_LOCK = asyncio.Lock()
_PDF_EXPORT_MAX_ACTIVE_DEFAULT = 2


class PdfExportRunItem(BaseModel):
    id: str
    arxiv_id: str
    status: Literal["queued", "running", "done", "error", "cancelled"]
    target_language: Literal["zh-CN"] = "zh-CN"
    source_sha256: str
    output_sha256: str | None = None
    source_bytes: int
    output_bytes: int | None = None
    source_pages: int
    output_pages: int | None = None
    page_count: int
    pages_done: int | None = None
    progress: float | None = None
    stage: str = ""
    provenance: dict[str, Any] | None = None
    retryable: bool = False
    error_code: str = ""
    error_message: str = ""
    created_at: str
    updated_at: str
    completed_at: str | None = None
    timestamps: dict[str, str | None]
    original_download_url: str
    download_url: str | None = None


class PdfExportCapability(BaseModel):
    enabled: bool
    error_code: str = ""
    reason: str = ""
    target_language: Literal["zh-CN"]
    output_mode: Literal["monolingual"]
    wrapper_version: str
    sidecar: dict[str, Any]
    limits: dict[str, int | float]
    version: str
    digest: str
    source_url: str
    modified_source_url: str
    license: str
    notice_url: str


def _run_item(row: dict[str, Any]) -> PdfExportRunItem:
    run_id = str(row["id"])
    arxiv_id = str(row["arxiv_id"])
    run_status = str(row["status"])
    done = run_status == "done"
    download_allowed = done and pdf_export_download_url_allowed(row)
    source_pages = int(row["source_pages"])
    output_pages = row.get("output_pages")
    progress = row.get("progress")
    pages_done = row.get("pages_done")
    if done and progress is None:
        progress = 1.0
    if done and pages_done is None:
        pages_done = int(output_pages or source_pages)
    created_at = str(row["created_at"])
    updated_at = str(row["updated_at"])
    completed_at = row.get("completed_at")
    payload = dict(row)
    payload.update(
        page_count=source_pages,
        pages_done=pages_done,
        progress=progress,
        stage=str(row.get("stage") or ""),
        retryable=str(row.get("error_code") or "") in RETRYABLE_ERROR_CODES,
        timestamps={
            "created_at": created_at,
            "updated_at": updated_at,
            "completed_at": str(completed_at) if completed_at else None,
        },
        original_download_url=f"/papers/{arxiv_id}/original-pdf/download",
        download_url=(
            f"/papers/{arxiv_id}/pdf-exports/{run_id}/download"
            if download_allowed
            else None
        ),
    )
    return PdfExportRunItem(**payload)


def _raise_export_error(exc: PdfExportError) -> NoReturn:
    status_code = {
        "export_disabled": status.HTTP_503_SERVICE_UNAVAILABLE,
        "source_pdf_missing": status.HTTP_404_NOT_FOUND,
        "source_pdf_too_large": status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        "page_limit_exceeded": status.HTTP_422_UNPROCESSABLE_ENTITY,
    }.get(exc.code, status.HTTP_400_BAD_REQUEST)
    raise HTTPException(
        status_code=status_code,
        detail={"code": exc.code, "message": exc.message, "retryable": exc.retryable},
    ) from exc


def _max_active_pdf_export_runs() -> int:
    try:
        return max(
            1,
            int(
                os.environ.get(
                    "PEINIDU_PDF_EXPORT_MAX_ACTIVE_RUNS",
                    str(_PDF_EXPORT_MAX_ACTIVE_DEFAULT),
                )
            ),
        )
    except ValueError:
        return _PDF_EXPORT_MAX_ACTIVE_DEFAULT


@router.get("/pdf-exports/capability", response_model=PdfExportCapability)
async def pdf_export_capability() -> PdfExportCapability:
    return PdfExportCapability(**(await probe_pdf_export_capability()))


@router.get("/pdf-exports/third-party-notice", response_class=Response)
async def pdf_export_third_party_notice() -> Response:
    try:
        content = _THIRD_PARTY_NOTICE.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=404, detail="第三方声明不存在") from exc
    return Response(content=content, media_type="text/markdown; charset=utf-8")


def _build_wrapper_source_archive(root: Path = _REPOSITORY_ROOT) -> bytes:
    """Build a deterministic archive from an exact, non-recursive allowlist."""
    if root.is_symlink():
        raise OSError("wrapper repository root contains a symbolic link")
    resolved_root = root.resolve(strict=True)
    output = io.BytesIO()
    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_STORED,
    ) as archive:
        for archive_path in _WRAPPER_SOURCE_FILES:
            if (
                root == _REPOSITORY_ROOT
                and archive_path.startswith(_WRAPPER_ARCHIVE_PREFIX)
                and _BAKED_WRAPPER_SOURCE_ROOT.is_dir()
            ):
                source_root = _BAKED_WRAPPER_SOURCE_ROOT
                relative_source = Path(
                    archive_path.removeprefix(_WRAPPER_ARCHIVE_PREFIX)
                )
                resolved_source_root = source_root.resolve(strict=True)
            else:
                source_root = root
                relative_source = Path(archive_path)
                resolved_source_root = resolved_root
            if source_root.is_symlink():
                raise OSError("wrapper source root contains a symbolic link")
            source = source_root / relative_source
            current = source_root
            if any(
                (current := current / part).is_symlink()
                for part in relative_source.parts
            ):
                raise OSError("wrapper source contains a symbolic link")
            resolved_source = source.resolve(strict=True)
            if (
                not resolved_source.is_relative_to(resolved_source_root)
                or not resolved_source.is_file()
            ):
                raise OSError("wrapper source file is unavailable")
            info = zipfile.ZipInfo(archive_path, date_time=_WRAPPER_SOURCE_TIMESTAMP)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            mode = 0o100755 if archive_path in _WRAPPER_EXECUTABLES else 0o100644
            info.external_attr = mode << 16
            archive.writestr(info, resolved_source.read_bytes())
    return output.getvalue()


@router.get("/pdf-exports/wrapper-source", response_class=Response)
async def pdf_export_wrapper_source() -> Response:
    try:
        content = _build_wrapper_source_archive()
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PDF 导出 wrapper 源码当前不可用",
        ) from exc
    version = get_config().pdf_export.wrapper_version
    return Response(
        content=content,
        media_type="application/zip",
        headers={
            "Cache-Control": "public, max-age=300",
            "Content-Disposition": (
                f'attachment; filename="peinidu-pdf-export-wrapper-{version}.zip"'
            ),
        },
    )


@router.post(
    "/papers/{arxiv_id}/pdf-exports",
    response_model=PdfExportRunItem,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(_require_admin)],
)
async def create_pdf_export(arxiv_id: str) -> PdfExportRunItem:
    # Compose runs one backend worker. Serialize the active-count check with
    # creation so concurrent public requests cannot grow an unbounded queue.
    async with _PDF_EXPORT_CREATION_LOCK:
        active_runs = await list_active_pdf_export_runs()
        for active in active_runs:
            if active.get("arxiv_id") == arxiv_id:
                return _run_item(active)
        if len(active_runs) >= _max_active_pdf_export_runs():
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "export_capacity_reached",
                    "message": "PDF 导出队列已满，请稍后再试。",
                    "retryable": True,
                },
                headers={"Retry-After": "60"},
            )
        try:
            run, _created = await create_pdf_export_run(arxiv_id)
        except PdfExportError as exc:
            _raise_export_error(exc)
    return _run_item(run)


@router.get(
    "/papers/{arxiv_id}/pdf-exports",
    response_model=list[PdfExportRunItem],
)
async def get_pdf_exports(
    arxiv_id: str,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[PdfExportRunItem]:
    return [_run_item(row) for row in await list_pdf_export_runs(arxiv_id, limit)]


@router.get(
    "/papers/{arxiv_id}/pdf-exports/{run_id}",
    response_model=PdfExportRunItem,
)
async def get_pdf_export(arxiv_id: str, run_id: str) -> PdfExportRunItem:
    run = await get_pdf_export_run(run_id)
    if run is None or run["arxiv_id"] != arxiv_id:
        raise HTTPException(status_code=404, detail="PDF 导出任务不存在")
    return _run_item(run)


@router.post(
    "/papers/{arxiv_id}/pdf-exports/{run_id}/cancel",
    response_model=PdfExportRunItem,
    dependencies=[Depends(_require_admin)],
)
async def cancel_pdf_export(arxiv_id: str, run_id: str) -> PdfExportRunItem:
    run = await cancel_pdf_export_run(arxiv_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="PDF 导出任务不存在")
    return _run_item(run)


@router.get("/papers/{arxiv_id}/original-pdf/download")
async def download_original_pdf(arxiv_id: str) -> FileResponse:
    try:
        path = _source_pdf_path(arxiv_id)
    except PdfExportError as exc:
        _raise_export_error(exc)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="论文原始 PDF 不存在")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"{arxiv_id}.original.pdf",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/papers/{arxiv_id}/pdf-exports/{run_id}/download")
async def download_exported_pdf(arxiv_id: str, run_id: str) -> FileResponse:
    run = await get_pdf_export_run(run_id)
    if run is None or run["arxiv_id"] != arxiv_id:
        raise HTTPException(status_code=404, detail="PDF 导出任务不存在")
    if run["status"] != "done":
        raise HTTPException(status_code=409, detail="PDF 导出尚未完成")
    try:
        path = await validated_pdf_export_download_path(run)
    except PdfExportError as exc:
        if exc.code in {
            "export_not_ready",
            "legacy_output_quarantined",
            "output_validation_failed",
        }:
            raise HTTPException(status_code=409, detail=exc.message) from exc
        raise HTTPException(status_code=404, detail="导出 PDF 不存在") from exc
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"{arxiv_id}.zh-CN.pdf",
        headers={"Cache-Control": "private, no-store"},
    )
