"""Persistent PDF export Run orchestration.

The source PDF is read-only. Sidecar output is downloaded to a temporary file,
validated, then atomically published outside the paper source directory.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..llm.config import get_config
from ..llm.models import PdfExportConfig
from ..storage import files as storage_files
from ..storage.db import (
    get_pdf_export_run,
    list_completed_pdf_export_runs,
    list_pdf_export_cleanup_pending_runs,
    mark_pdf_export_cleanup_pending,
    record_pdf_export_cleanup_result,
    set_pdf_export_sidecar_job,
    sweep_stale_pdf_export_runs as _sweep_stale_pdf_export_runs,
    transition_pdf_export_run,
    try_create_pdf_export_run,
    update_pdf_export_progress,
)
from .errors import PdfExportError
from .sidecar import PdfExportSidecarClient, sidecar_environment

_SAFE_PAPER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}$")
_RUN_TASKS: dict[str, asyncio.Task[None]] = {}
_RUN_CLIENTS: dict[str, PdfExportSidecarClient] = {}
_RUN_SEMAPHORE: asyncio.Semaphore | None = None
_RUN_SEMAPHORE_LIMIT = 0
_WRAPPER_SOURCE_URL = "/pdf-exports/wrapper-source"
_BAKED_WRAPPER_SOURCE_ROOT = Path("/app/pdf_export_wrapper_source")
_DEVELOPMENT_WRAPPER_SOURCE_ROOT = (
    Path(__file__).resolve().parents[2] / "sidecar" / "pdf_export"
)
_WRAPPER_SOURCE_ROOT = (
    _BAKED_WRAPPER_SOURCE_ROOT
    if Path(__file__).resolve().parents[2] == Path("/app")
    else _DEVELOPMENT_WRAPPER_SOURCE_ROOT
)
_WRAPPER_RUNTIME_SOURCE_FILES = (
    "app.py",
    "Dockerfile",
    "entrypoint.sh",
    "healthcheck.py",
    "runtime_probe.py",
    "README.md",
    "THIRD_PARTY.md",
    "tests/__init__.py",
    "tests/test_app.py",
)
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_CLEANUP_BATCH_LIMIT = 16


def _config(value: PdfExportConfig | None = None) -> PdfExportConfig:
    return value or get_config().pdf_export


def trusted_wrapper_source_sha256(root: Path | None = None) -> str:
    """Hash the backend-owned runtime wrapper with the sidecar's framed format."""
    source_root = root or _WRAPPER_SOURCE_ROOT
    if source_root.is_symlink():
        raise OSError("wrapper source root is a symbolic link")
    resolved_root = source_root.resolve(strict=True)
    if not resolved_root.is_dir():
        raise OSError("wrapper source root is unavailable")

    digest = hashlib.sha256()
    for relative_name in _WRAPPER_RUNTIME_SOURCE_FILES:
        relative_path = Path(relative_name)
        current = source_root
        for part in relative_path.parts:
            current = current / part
            if current.is_symlink():
                raise OSError("wrapper source contains a symbolic link")
        source = source_root / relative_path
        resolved_source = source.resolve(strict=True)
        if (
            not resolved_source.is_relative_to(resolved_root)
            or not resolved_source.is_file()
        ):
            raise OSError("wrapper source file is unavailable")
        path_bytes = relative_name.encode("utf-8")
        file_bytes = resolved_source.read_bytes()
        digest.update(struct.pack(">Q", len(path_bytes)))
        digest.update(path_bytes)
        digest.update(struct.pack(">Q", len(file_bytes)))
        digest.update(file_bytes)
    return digest.hexdigest()


def get_pdf_export_capability(
    config: PdfExportConfig | None = None,
) -> dict[str, Any]:
    cfg = _config(config)
    modified_source_url = cfg.modified_source_url or _WRAPPER_SOURCE_URL
    sidecar_url, token = sidecar_environment()
    enabled = bool(
        cfg.enabled
        and cfg.license_disclosure_complete
        and sidecar_url
        and token
    )
    if not cfg.enabled:
        reason = "feature_disabled"
    elif not cfg.license_disclosure_complete:
        reason = "license_disclosure_incomplete"
    elif not sidecar_url or not token:
        reason = "sidecar_not_configured"
    else:
        reason = ""
    return {
        "enabled": enabled,
        "error_code": "" if enabled else "export_disabled",
        "reason": reason,
        "target_language": cfg.target_language,
        "output_mode": "monolingual",
        "sidecar": {
            "wrapper_version": cfg.wrapper_version,
            "name": cfg.sidecar_name,
            "version": cfg.sidecar_version,
            "commit": cfg.sidecar_commit,
            "image_digest": cfg.sidecar_image_digest,
            "source_code_url": cfg.source_code_url,
            "modified_source_url": modified_source_url,
            "license": cfg.license_name,
            "license_disclosure_complete": cfg.license_disclosure_complete,
            "configured": bool(sidecar_url and token),
            "healthy": None,
        },
        # Flat aliases keep older clients able to render the mandatory license
        # disclosure while the canonical contract lives under `sidecar`.
        "wrapper_version": cfg.wrapper_version,
        "version": cfg.sidecar_version,
        "digest": cfg.sidecar_image_digest,
        "source_url": cfg.source_code_url,
        "modified_source_url": modified_source_url,
        "license": cfg.license_name,
        "notice_url": "/pdf-exports/third-party-notice",
        "limits": {
            "max_source_bytes": cfg.max_source_bytes,
            "max_pages": cfg.max_pages,
            "max_output_bytes": cfg.max_output_bytes,
            "max_concurrent_runs": cfg.max_concurrent_runs,
            "timeout_seconds": cfg.timeout_seconds,
        },
    }


async def probe_pdf_export_capability(
    config: PdfExportConfig | None = None,
    *,
    client: PdfExportSidecarClient | None = None,
) -> dict[str, Any]:
    """Apply a short live sidecar check after the static license/config gate."""
    cfg = _config(config)
    capability = get_pdf_export_capability(cfg)
    if not capability["enabled"]:
        return capability
    probe_client = client or PdfExportSidecarClient.from_environment()
    if client is None:
        probe_client.request_timeout = 2.0
    try:
        await _live_sidecar_attestation(cfg, probe_client)
    except PdfExportError:
        capability["enabled"] = False
        capability["error_code"] = "sidecar_unavailable"
        capability["reason"] = "sidecar_unavailable"
        capability["sidecar"]["healthy"] = False
        return capability
    capability["sidecar"]["healthy"] = True
    return capability


async def _live_sidecar_attestation(
    config: PdfExportConfig,
    client: PdfExportSidecarClient,
) -> dict[str, Any]:
    """Probe and verify the exact sidecar disclosed by the capability API."""
    try:
        async with asyncio.timeout(2.0):
            trusted_source_sha256 = await asyncio.to_thread(
                trusted_wrapper_source_sha256
            )
            await client.health()
            info = await client.info()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise PdfExportError(
            "sidecar_unavailable",
            "PDF 导出服务暂时不可用。",
            retryable=True,
        ) from exc

    actual_image = str(info.get("image") or "")
    actual_wrapper_source_sha256 = str(
        info.get("wrapper_source_sha256") or ""
    )
    metadata_matches = all(
        (
            str(info.get("wrapper_version") or "") == config.wrapper_version,
            str(info.get("name") or "") == config.sidecar_name,
            str(info.get("version") or "").removeprefix("v")
            == config.sidecar_version.removeprefix("v"),
            str(info.get("revision") or "") == config.sidecar_commit,
            actual_image.endswith(config.sidecar_image_digest),
            str(info.get("source") or "") == config.source_code_url,
            str(info.get("license") or "") == config.license_name,
            str(info.get("output") or "")
            == "monolingual-watermarked-zh-CN",
            bool(_SHA256_HEX.fullmatch(actual_wrapper_source_sha256)),
            hmac.compare_digest(
                actual_wrapper_source_sha256, trusted_source_sha256
            ),
        )
    )
    if not metadata_matches:
        raise PdfExportError(
            "sidecar_unavailable",
            "PDF 导出服务版本与已披露版本不一致。",
            retryable=True,
        )
    return info


def _build_export_provenance(
    config: PdfExportConfig,
    info: dict[str, Any],
) -> dict[str, Any]:
    actual_image = str(info.get("image") or "")
    image_digest = actual_image.rsplit("@", 1)[-1]
    critical_config = {
        "model_alias": "pdf-translation",
        "output_mode": str(info["output"]),
        "source_language": "en",
        "target_language": config.target_language,
        "wrapper_version": str(info["wrapper_version"]),
        "wrapper_source_sha256": str(info["wrapper_source_sha256"]),
        "upstream_name": str(info["name"]),
        "upstream_version": str(info["version"]),
        "upstream_revision": str(info["revision"]),
        "upstream_image_digest": image_digest,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            critical_config,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": 2,
        "wrapper_version": str(info["wrapper_version"]),
        "wrapper_source_sha256": str(info["wrapper_source_sha256"]),
        "upstream": {
            "name": str(info["name"]),
            "version": str(info["version"]),
            "revision": str(info["revision"]),
            "image_digest": image_digest,
            "source_url": str(info["source"]),
            "license": str(info["license"]),
        },
        "output_mode": str(info["output"]),
        "language": {"source": "en", "target": config.target_language},
        "config_fingerprint": fingerprint,
    }


async def create_pdf_export_run(
    arxiv_id: str,
    *,
    config: PdfExportConfig | None = None,
    client: PdfExportSidecarClient | None = None,
) -> tuple[dict[str, Any], bool]:
    cfg = _config(config)
    capability = get_pdf_export_capability(cfg)
    if not capability["enabled"]:
        raise PdfExportError("export_disabled", "中文 PDF 导出功能当前未启用。")
    run_client = client or PdfExportSidecarClient.from_environment()
    attested_info = await _live_sidecar_attestation(cfg, run_client)
    provenance = _build_export_provenance(cfg, attested_info)
    await retry_pending_pdf_export_cleanups(
        client=run_client,
        limit=4,
        cleanup_timeout_seconds=0.25,
    )
    source_path = _source_pdf_path(arxiv_id)
    source_bytes, source_pages, source_hash = await _preflight_source(source_path, cfg)
    run, created = await try_create_pdf_export_run(
        run_id=uuid4().hex,
        arxiv_id=arxiv_id,
        source_sha256=source_hash,
        source_bytes=source_bytes,
        source_pages=source_pages,
        target_language=cfg.target_language,
    )
    if not created and run.get("status") == "done":
        if await asyncio.to_thread(
            _completed_run_is_reusable, run, cfg, provenance
        ):
            return run, False
        completed_runs = await list_completed_pdf_export_runs(arxiv_id, source_hash)
        for candidate in completed_runs:
            if candidate.get("id") == run.get("id"):
                continue
            if await asyncio.to_thread(
                _completed_run_is_reusable, candidate, cfg, provenance
            ):
                return candidate, False
        run, created = await try_create_pdf_export_run(
            run_id=uuid4().hex,
            arxiv_id=arxiv_id,
            source_sha256=source_hash,
            source_bytes=source_bytes,
            source_pages=source_pages,
            target_language=cfg.target_language,
            reuse_completed=False,
        )
    if created:
        _schedule_run(
            run,
            source_path=source_path,
            config=cfg,
            client=run_client,
            provenance=provenance,
        )
    return run, created


async def cancel_pdf_export_run(
    arxiv_id: str,
    run_id: str,
) -> dict[str, Any] | None:
    run = await get_pdf_export_run(run_id)
    if run is None or run["arxiv_id"] != arxiv_id:
        return None
    if run["status"] in {"done", "error", "cancelled"}:
        return run
    changed = await transition_pdf_export_run(
        run_id,
        from_statuses=("queued", "running"),
        status="cancelled",
        error_code="",
        error_message="",
    )
    if not changed:
        return await get_pdf_export_run(run_id)
    current = await get_pdf_export_run(run_id)
    client = _RUN_CLIENTS.get(run_id)
    job_id = (current or {}).get("sidecar_job_id")
    if client is not None and job_id:
        try:
            await client.cancel_job(job_id)
        except Exception:
            pass
    task = _RUN_TASKS.get(run_id)
    if task is not None and not task.done():
        task.cancel()
    return await get_pdf_export_run(run_id)


async def sweep_stale_pdf_export_runs(
    *,
    client: PdfExportSidecarClient | None = None,
    cleanup_timeout_seconds: float = 3.0,
) -> int:
    """Retry persisted cleanup, then mark process-orphaned Runs as error."""
    await retry_pending_pdf_export_cleanups(
        client=client,
        limit=_CLEANUP_BATCH_LIMIT,
        cleanup_timeout_seconds=cleanup_timeout_seconds,
        include_active=True,
    )
    return await _sweep_stale_pdf_export_runs()


async def retry_pending_pdf_export_cleanups(
    *,
    client: PdfExportSidecarClient | None = None,
    limit: int = _CLEANUP_BATCH_LIMIT,
    cleanup_timeout_seconds: float = 3.0,
    include_active: bool = False,
) -> int:
    """Retry a bounded batch of persisted remote deletions.

    Normal request safe points only touch terminal Runs. Startup passes
    ``include_active=True`` because no pre-restart execution task survives.
    """
    pending = await list_pdf_export_cleanup_pending_runs(
        limit=limit,
        include_active=include_active,
    )
    if not pending:
        return 0

    cleanup_client = client
    if cleanup_client is None:
        sidecar_url, token = sidecar_environment()
        if not sidecar_url or not token:
            return 0
        cleanup_client = PdfExportSidecarClient(
            sidecar_url,
            token,
            request_timeout=min(
                1.5, max(0.25, float(cleanup_timeout_seconds))
            ),
        )

    deleted = 0
    timeout_seconds = min(5.0, max(0.05, float(cleanup_timeout_seconds)))

    async def cleanup_batch() -> None:
        nonlocal deleted
        per_job_timeout = timeout_seconds / max(1, len(pending))
        for run in pending:
            run_id = str(run["id"])
            job_id = str(run.get("sidecar_job_id") or "")
            if not job_id:
                continue
            try:
                async with asyncio.timeout(per_job_timeout):
                    await mark_pdf_export_cleanup_pending(run_id)
                    await _best_effort_cancel(cleanup_client, job_id)
                    if await _delete_remote_job(
                        run_id, cleanup_client, job_id
                    ):
                        deleted += 1
            except TimeoutError:
                continue

    try:
        async with asyncio.timeout(timeout_seconds):
            await cleanup_batch()
    except TimeoutError:
        pass
    return deleted


def _schedule_run(
    run: dict[str, Any],
    *,
    source_path: Path,
    config: PdfExportConfig,
    client: PdfExportSidecarClient,
    provenance: dict[str, Any],
) -> None:
    run_id = str(run["id"])
    task = asyncio.create_task(
        _execute_run(
            run,
            source_path=source_path,
            config=config,
            client=client,
            provenance=provenance,
        ),
        name=f"pdf-export-{run_id}",
    )
    _RUN_TASKS[run_id] = task
    _RUN_CLIENTS[run_id] = client

    def cleanup(done: asyncio.Task[None]) -> None:
        if _RUN_TASKS.get(run_id) is done:
            _RUN_TASKS.pop(run_id, None)
            _RUN_CLIENTS.pop(run_id, None)

    task.add_done_callback(cleanup)


async def _execute_run(
    run: dict[str, Any],
    *,
    source_path: Path,
    config: PdfExportConfig,
    client: PdfExportSidecarClient,
    provenance: dict[str, Any],
) -> None:
    run_id = str(run["id"])
    try:
        await asyncio.wait_for(
            _perform_run(
                run,
                source_path=source_path,
                config=config,
                client=client,
                provenance=provenance,
            ),
            timeout=config.timeout_seconds,
        )
    except asyncio.TimeoutError:
        await _cancel_remote_if_known(run_id, client)
        await transition_pdf_export_run(
            run_id,
            from_statuses=("queued", "running"),
            status="error",
            error_code="export_timeout",
            error_message="中文 PDF 导出超时，请稍后重试。",
        )
    except PdfExportError as exc:
        await transition_pdf_export_run(
            run_id,
            from_statuses=("queued", "running"),
            status="error",
            error_code=exc.code,
            error_message=exc.message,
        )
    except asyncio.CancelledError:
        current = await get_pdf_export_run(run_id)
        if current and current["status"] in {"queued", "running"}:
            await _cancel_remote_if_known(run_id, client)
            await transition_pdf_export_run(
                run_id,
                from_statuses=("queued", "running"),
                status="error",
                error_code="backend_restarted",
                error_message="后端停止，PDF 导出任务已中断。",
            )
        raise
    except Exception:
        await transition_pdf_export_run(
            run_id,
            from_statuses=("queued", "running"),
            status="error",
            error_code="sidecar_crashed",
            error_message="PDF 导出服务意外退出，请稍后重试。",
        )
    finally:
        await _cancel_remote_if_known(run_id, client)
        await _delete_remote_if_known(run_id, client)


async def _perform_run(
    run: dict[str, Any],
    *,
    source_path: Path,
    config: PdfExportConfig,
    client: PdfExportSidecarClient,
    provenance: dict[str, Any],
) -> None:
    run_id = str(run["id"])
    async with _run_semaphore(config.max_concurrent_runs):
        started = await transition_pdf_export_run(
            run_id, from_statuses=("queued",), status="running"
        )
        if not started:
            return
        if not await set_pdf_export_sidecar_job(run_id, run_id):
            return
        job_id = await client.create_job(source_path, run_id)
        if job_id != run_id:
            await _best_effort_cancel(client, job_id)
            await _best_effort_delete(client, job_id)
            raise PdfExportError(
                "sidecar_unavailable", "PDF 导出服务返回了错误的任务标识。", retryable=True
            )
        while True:
            current = await get_pdf_export_run(run_id)
            if current is None or current["status"] != "running":
                await _best_effort_cancel(client, job_id)
                return
            state = await client.get_job(job_id)
            pages_done = state.pages_done
            if pages_done is not None:
                pages_done = min(int(run["source_pages"]), max(0, pages_done))
            if state.status == "done":
                pages_done = int(run["source_pages"])
            await update_pdf_export_progress(
                run_id,
                progress=1.0 if state.status == "done" else state.progress,
                stage=state.stage,
                pages_done=pages_done,
            )
            if state.status == "done":
                await _download_validate_publish(
                    current,
                    source_path=source_path,
                    job_id=job_id,
                    config=config,
                    client=client,
                    provenance=provenance,
                )
                return
            if state.status == "cancelled":
                raise PdfExportError(
                    "sidecar_crashed", "PDF 导出服务提前取消了任务。", retryable=True
                )
            await asyncio.sleep(config.poll_interval_seconds)


async def _download_validate_publish(
    run: dict[str, Any],
    *,
    source_path: Path,
    job_id: str,
    config: PdfExportConfig,
    client: PdfExportSidecarClient,
    provenance: dict[str, Any],
) -> None:
    run_id = str(run["id"])
    output_dir = _output_dir(str(run["arxiv_id"]), run_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=str(output_dir), prefix=".translated.", suffix=".pdf.part"
    )
    os.close(fd)
    temp_path = Path(temp_name)
    final_path = output_dir / "translated.zh-CN.pdf"
    try:
        await client.download_output(job_id, temp_path, max_bytes=config.max_output_bytes)
        validation = await asyncio.to_thread(
            _validate_output,
            temp_path,
            source_path,
            expected_source_sha256=str(run["source_sha256"]),
            expected_pages=int(run["source_pages"]),
            max_bytes=config.max_output_bytes,
        )
        os.replace(temp_path, final_path)
        completed = await transition_pdf_export_run(
            run_id,
            from_statuses=("running",),
            status="done",
            output_sha256=validation["sha256"],
            output_bytes=validation["bytes"],
            output_pages=validation["pages"],
            progress=1.0,
            stage="done",
            pages_done=validation["pages"],
            provenance=provenance,
            output_path=str(final_path),
        )
        if not completed:
            final_path.unlink(missing_ok=True)
    finally:
        temp_path.unlink(missing_ok=True)


def _run_semaphore(limit: int) -> asyncio.Semaphore:
    global _RUN_SEMAPHORE, _RUN_SEMAPHORE_LIMIT
    safe_limit = max(1, int(limit))
    if _RUN_SEMAPHORE is None or _RUN_SEMAPHORE_LIMIT != safe_limit:
        _RUN_SEMAPHORE = asyncio.Semaphore(safe_limit)
        _RUN_SEMAPHORE_LIMIT = safe_limit
    return _RUN_SEMAPHORE


async def _cancel_remote_if_known(
    run_id: str, client: PdfExportSidecarClient
) -> None:
    run = await get_pdf_export_run(run_id)
    job_id = (run or {}).get("sidecar_job_id")
    if job_id:
        await _best_effort_cancel(client, str(job_id))


async def _best_effort_cancel(client: PdfExportSidecarClient, job_id: str) -> None:
    try:
        await client.cancel_job(job_id)
    except Exception:
        pass


async def _delete_remote_if_known(
    run_id: str, client: PdfExportSidecarClient
) -> None:
    run = await get_pdf_export_run(run_id)
    job_id = (run or {}).get("sidecar_job_id")
    if job_id and bool((run or {}).get("cleanup_pending")):
        await _delete_remote_job(run_id, client, str(job_id))


async def _delete_remote_job(
    run_id: str,
    client: PdfExportSidecarClient,
    job_id: str,
) -> bool:
    """Delete one known remote job and durably record the outcome."""
    await mark_pdf_export_cleanup_pending(run_id)
    try:
        await client.delete_job(job_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        await record_pdf_export_cleanup_result(run_id, deleted=False)
        return False
    await record_pdf_export_cleanup_result(run_id, deleted=True)
    return True


async def _best_effort_delete(client: PdfExportSidecarClient, job_id: str) -> None:
    try:
        await client.delete_job(job_id)
    except Exception:
        pass


def _source_pdf_path(arxiv_id: str) -> Path:
    if not _SAFE_PAPER_ID.fullmatch(arxiv_id) or arxiv_id in {".", ".."}:
        raise PdfExportError("source_pdf_missing", "论文原始 PDF 不存在。")
    return storage_files.PAPERS_DIR / arxiv_id / "original.pdf"


def _output_dir(arxiv_id: str, run_id: str) -> Path:
    return storage_files.DATA_DIR / "pdf_exports" / arxiv_id / run_id


def _has_current_wrapper_provenance(run: dict[str, Any]) -> bool:
    provenance = run.get("provenance")
    if not isinstance(provenance, dict):
        return False
    try:
        schema_version = int(provenance.get("schema_version") or 0)
    except (TypeError, ValueError):
        return False
    wrapper_source_sha256 = str(
        provenance.get("wrapper_source_sha256") or ""
    )
    config_fingerprint = str(provenance.get("config_fingerprint") or "")
    return (
        schema_version == 2
        and bool(str(provenance.get("wrapper_version") or ""))
        and bool(_SHA256_HEX.fullmatch(wrapper_source_sha256))
        and bool(_SHA256_HEX.fullmatch(config_fingerprint))
    )


def pdf_export_download_url_allowed(run: dict[str, Any]) -> bool:
    """Hide download URLs for legacy or incomplete completed records."""
    return (
        run.get("status") == "done"
        and _has_current_wrapper_provenance(run)
        and bool(_SHA256_HEX.fullmatch(str(run.get("output_sha256") or "")))
    )


async def validated_pdf_export_download_path(
    run: dict[str, Any],
    config: PdfExportConfig | None = None,
) -> Path:
    """Return an attested output path after re-hashing it for this download."""
    if run.get("status") != "done":
        raise PdfExportError("export_not_ready", "PDF 导出尚未完成。")
    if not _has_current_wrapper_provenance(run):
        raise PdfExportError(
            "legacy_output_quarantined",
            "旧版 PDF 导出缺少完整来源证明，已隔离且不可下载。",
        )
    return await asyncio.to_thread(
        _validated_pdf_export_download_path_sync,
        run,
        _config(config),
    )


def _validated_pdf_export_download_path_sync(
    run: dict[str, Any],
    config: PdfExportConfig,
) -> Path:
    try:
        arxiv_id = str(run["arxiv_id"])
        run_id = str(run["id"])
        candidate = Path(str(run.get("output_path") or ""))
        expected = _output_dir(arxiv_id, run_id) / "translated.zh-CN.pdf"
        export_root_path = storage_files.DATA_DIR / "pdf_exports"
        paper_output_dir = export_root_path / arxiv_id
        run_output_dir = paper_output_dir / run_id
        if (
            not candidate.is_absolute()
            or candidate != expected
            or any(
                path.is_symlink()
                for path in (
                    export_root_path,
                    paper_output_dir,
                    run_output_dir,
                    candidate,
                )
            )
        ):
            raise PdfExportError("export_output_missing", "导出 PDF 不存在。")
        resolved = candidate.resolve(strict=True)
        export_root = export_root_path.resolve(strict=True)
        if not resolved.is_relative_to(export_root) or not resolved.is_file():
            raise PdfExportError("export_output_missing", "导出 PDF 不存在。")
        size = resolved.stat().st_size
    except PdfExportError:
        raise
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise PdfExportError("export_output_missing", "导出 PDF 不存在。") from exc

    try:
        expected_size = int(run["output_bytes"])
        expected_pages = int(run["output_pages"])
        source_pages = int(run["source_pages"])
        expected_hash = str(run.get("output_sha256") or "")
    except (KeyError, TypeError, ValueError) as exc:
        raise PdfExportError(
            "output_validation_failed",
            "导出 PDF 完整性校验失败，请重新生成。",
        ) from exc
    if (
        size <= 0
        or size > config.max_output_bytes
        or size != expected_size
        or not _has_pdf_header(resolved)
        or expected_pages <= 0
        or expected_pages != source_pages
        or not _SHA256_HEX.fullmatch(expected_hash)
    ):
        raise PdfExportError(
            "output_validation_failed",
            "导出 PDF 完整性校验失败，请重新生成。",
        )
    try:
        actual_hash = _sha256_file(resolved)
    except OSError as exc:
        raise PdfExportError(
            "output_validation_failed",
            "导出 PDF 完整性校验失败，请重新生成。",
        ) from exc
    if not hmac.compare_digest(actual_hash, expected_hash):
        raise PdfExportError(
            "output_validation_failed",
            "导出 PDF 完整性校验失败，请重新生成。",
        )
    return resolved


def _completed_run_is_reusable(
    run: dict[str, Any],
    config: PdfExportConfig,
    provenance: dict[str, Any],
) -> bool:
    """Fail closed when a persisted completed output was moved or tampered with."""
    try:
        if run.get("provenance") != provenance:
            return False
        arxiv_id = str(run["arxiv_id"])
        run_id = str(run["id"])
        candidate = Path(str(run.get("output_path") or ""))
        expected = _output_dir(arxiv_id, run_id) / "translated.zh-CN.pdf"
        if not candidate.is_absolute() or candidate != expected or candidate.is_symlink():
            return False
        export_root_path = storage_files.DATA_DIR / "pdf_exports"
        paper_output_dir = export_root_path / arxiv_id
        run_output_dir = paper_output_dir / run_id
        if any(
            path.is_symlink()
            for path in (export_root_path, paper_output_dir, run_output_dir)
        ):
            return False
        resolved = candidate.resolve(strict=True)
        export_root = export_root_path.resolve(strict=True)
        if not resolved.is_relative_to(export_root) or not resolved.is_file():
            return False
        size = resolved.stat().st_size
        if size <= 0 or size > config.max_output_bytes:
            return False
        if run.get("output_bytes") is not None and size != int(run["output_bytes"]):
            return False
        if not _has_pdf_header(resolved):
            return False
        if int(run.get("output_pages") or 0) != int(run.get("source_pages") or 0):
            return False
        expected_hash = str(run.get("output_sha256") or "")
        return bool(expected_hash) and _sha256_file(resolved) == expected_hash
    except (KeyError, OSError, TypeError, ValueError):
        return False


async def _preflight_source(
    source_path: Path, config: PdfExportConfig
) -> tuple[int, int, str]:
    try:
        size = source_path.stat().st_size
    except OSError as exc:
        raise PdfExportError("source_pdf_missing", "论文原始 PDF 不存在。") from exc
    if size <= 0:
        raise PdfExportError("source_pdf_missing", "论文原始 PDF 不存在。")
    if size > config.max_source_bytes:
        raise PdfExportError("source_pdf_too_large", "原始 PDF 超过导出大小限制。")
    if not await asyncio.to_thread(_has_pdf_header, source_path):
        raise PdfExportError("source_pdf_missing", "论文原始 PDF 无效或不存在。")
    pages = await asyncio.to_thread(_pdf_page_count, source_path)
    if pages > config.max_pages:
        raise PdfExportError("page_limit_exceeded", "原始 PDF 页数超过导出限制。")
    source_hash = await asyncio.to_thread(_sha256_file, source_path)
    return size, pages, source_hash


def _validate_output(
    output_path: Path,
    source_path: Path,
    *,
    expected_source_sha256: str,
    expected_pages: int,
    max_bytes: int,
) -> dict[str, Any]:
    try:
        size = output_path.stat().st_size
    except OSError as exc:
        raise PdfExportError("output_validation_failed", "导出 PDF 不存在。") from exc
    if size <= 0 or size > max_bytes or not _has_pdf_header(output_path):
        raise PdfExportError("output_validation_failed", "导出 PDF 文件无效。")
    if _sha256_file(source_path) != expected_source_sha256:
        raise PdfExportError(
            "output_validation_failed", "导出期间原始 PDF 已发生变化。"
        )
    pages = _pdf_page_count(output_path)
    if pages != expected_pages:
        raise PdfExportError(
            "output_validation_failed", "导出 PDF 页数与原文件不一致。"
        )
    return {"bytes": size, "pages": pages, "sha256": _sha256_file(output_path)}


def _has_pdf_header(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return b"%PDF-" in handle.read(1024)
    except OSError:
        return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pdf_page_count(path: Path) -> int:
    if shutil.which("pdfinfo") is None:
        raise PdfExportError(
            "output_validation_failed", "服务器缺少 pdfinfo，无法校验 PDF 页数。"
        )
    try:
        result = subprocess.run(
            ["pdfinfo", str(path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise PdfExportError(
            "output_validation_failed", "无法读取 PDF 页数。"
        ) from exc
    match = re.search(r"^Pages:\s+(\d+)\s*$", result.stdout, flags=re.MULTILINE)
    if match is None or int(match.group(1)) <= 0:
        raise PdfExportError("output_validation_failed", "无法读取 PDF 页数。")
    return int(match.group(1))


def reset_pdf_export_runtime_for_tests() -> None:
    """Clear process-local registries after isolated tests."""
    global _RUN_SEMAPHORE, _RUN_SEMAPHORE_LIMIT
    for task in tuple(_RUN_TASKS.values()):
        if not task.done():
            task.cancel()
    _RUN_TASKS.clear()
    _RUN_CLIENTS.clear()
    _RUN_SEMAPHORE = None
    _RUN_SEMAPHORE_LIMIT = 0
