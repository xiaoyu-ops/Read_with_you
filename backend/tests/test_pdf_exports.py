from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx
import aiosqlite
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.api.routes_pdf_exports import router
from backend.api import routes_pdf_exports
from backend.llm.models import PdfExportConfig
from backend.pdf_export import service
from backend.pdf_export.errors import PdfExportError
from backend.pdf_export.sidecar import PdfExportSidecarClient, SidecarJob
from backend.storage import db as db_module
from backend.storage import files as storage_files


PDF_BYTES = b"%PDF-1.4\n% PeiNiDu export test\n%%EOF\n"


class MockSidecar:
    def __init__(
        self,
        *,
        output: bytes = PDF_BYTES,
        info_overrides: dict | None = None,
    ) -> None:
        self.output = output
        self.info_overrides = info_overrides or {}
        self.create_calls = 0
        self.health_calls = 0
        self.info_calls = 0
        self.cancel_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.events: list[tuple[str, str]] = []
        self.job_id = "sidecar-job-1"
        self.statuses: list[SidecarJob | Exception] = [SidecarJob("done")]
        self.status_gate: asyncio.Event | None = None
        self.health_error: Exception | None = None

    async def health(self) -> None:
        self.health_calls += 1
        if self.health_error is not None:
            raise self.health_error

    async def info(self) -> dict:
        self.info_calls += 1
        info = {
            "wrapper_version": "1.0.1",
            "name": "PDFMathTranslate-next",
            "version": "2.9.0",
            "revision": "f8dffcf4c3a33b254391d43514439b975ce8d966",
            "image": (
                "awwaawwa/pdfmathtranslate-next@"
                "sha256:c737d5342c9220a56026733f3a42182581bb4d8e5052b133e3326babffea109a"
            ),
            "source": (
                "https://github.com/PDFMathTranslate-next/"
                "PDFMathTranslate-next/tree/v2.9.0"
            ),
            "license": "AGPL-3.0",
            "output": "monolingual-watermarked-zh-CN",
            "wrapper_source_sha256": service.trusted_wrapper_source_sha256(),
        }
        info.update(self.info_overrides)
        return info

    async def create_job(self, source_path: Path, job_id: str) -> str:
        self.create_calls += 1
        self.job_id = job_id
        return job_id

    async def get_job(self, job_id: str) -> SidecarJob:
        if self.status_gate is not None:
            await self.status_gate.wait()
        value = self.statuses.pop(0) if self.statuses else SidecarJob("done")
        if isinstance(value, Exception):
            raise value
        return value

    async def cancel_job(self, job_id: str) -> None:
        self.cancel_calls.append(job_id)
        self.events.append(("cancel", job_id))

    async def delete_job(self, job_id: str) -> None:
        self.delete_calls.append(job_id)
        self.events.append(("delete", job_id))

    async def download_output(
        self, job_id: str, target: Path, *, max_bytes: int
    ) -> Path:
        target.write_bytes(self.output)
        return target


class BlockingCreateSidecar(MockSidecar):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.active_creates = 0
        self.max_active_creates = 0

    async def create_job(self, source_path: Path, job_id: str) -> str:
        self.create_calls += 1
        self.active_creates += 1
        self.max_active_creates = max(self.max_active_creates, self.active_creates)
        self.entered.set()
        await self.release.wait()
        self.active_creates -= 1
        return job_id


class LostCreateResponseSidecar(MockSidecar):
    async def create_job(self, source_path: Path, job_id: str) -> str:
        self.create_calls += 1
        raise PdfExportError(
            "sidecar_unavailable", "response was lost", retryable=True
        )


class FlakyDeleteSidecar(MockSidecar):
    def __init__(self, failures: int = 1) -> None:
        super().__init__()
        self.delete_failures = failures

    async def delete_job(self, job_id: str) -> None:
        self.delete_calls.append(job_id)
        self.events.append(("delete", job_id))
        if self.delete_failures > 0:
            self.delete_failures -= 1
            raise PdfExportError(
                "sidecar_unavailable", "transient delete failure", retryable=True
            )


class ProgressSidecar(MockSidecar):
    def __init__(self, first_state: SidecarJob) -> None:
        super().__init__()
        self.first_state = first_state
        self.first_seen = asyncio.Event()
        self.release_done = asyncio.Event()
        self.polls = 0

    async def get_job(self, job_id: str) -> SidecarJob:
        self.polls += 1
        if self.polls == 1:
            self.first_seen.set()
            return self.first_state
        await self.release_done.wait()
        return SidecarJob("done", progress=1.0, stage="complete", pages_done=2)


class PdfExportTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.data_dir = root / "data"
        self.papers_dir = self.data_dir / "papers"
        self.db_patch = patch.object(db_module, "DB_PATH", self.data_dir / "papers.db")
        self.db_data_patch = patch.object(db_module, "DATA_DIR", self.data_dir)
        self.data_patch = patch.object(storage_files, "DATA_DIR", self.data_dir)
        self.papers_patch = patch.object(storage_files, "PAPERS_DIR", self.papers_dir)
        self.page_patch = patch.object(service, "_pdf_page_count", return_value=2)
        self.env_patch = patch.dict(
            "os.environ",
            {
                "PEINIDU_PDF_EXPORT_SIDECAR_URL": "http://sidecar.test:8090",
                "PEINIDU_PDF_EXPORT_SIDECAR_TOKEN": "test-sidecar-token",
            },
            clear=False,
        )
        for patcher in (
            self.db_patch,
            self.db_data_patch,
            self.data_patch,
            self.papers_patch,
            self.page_patch,
            self.env_patch,
        ):
            patcher.start()
        await db_module.init_db()
        self.config = PdfExportConfig(
            enabled=True,
            license_disclosure_complete=True,
            max_source_bytes=1024,
            max_pages=10,
            max_output_bytes=1024,
            max_concurrent_runs=1,
            timeout_seconds=0.5,
            poll_interval_seconds=0.001,
        )

    async def asyncTearDown(self) -> None:
        tasks = list(service._RUN_TASKS.values())
        service.reset_pdf_export_runtime_for_tests()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for patcher in (
            self.env_patch,
            self.page_patch,
            self.papers_patch,
            self.data_patch,
            self.db_patch,
            self.db_data_patch,
        ):
            patcher.stop()
        self.temp.cleanup()

    def _write_source(self, arxiv_id: str = "1706.03762") -> Path:
        path = self.papers_dir / arxiv_id / "original.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PDF_BYTES)
        return path

    async def _wait_terminal(self, run_id: str, timeout: float = 1.0) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            run = await db_module.get_pdf_export_run(run_id)
            if run and run["status"] in {"done", "error", "cancelled"}:
                task = service._RUN_TASKS.get(run_id)
                if task is not None and not task.done():
                    await asyncio.gather(task, return_exceptions=True)
                return await db_module.get_pdf_export_run(run_id) or run
            await asyncio.sleep(0.005)
        self.fail(f"run {run_id} did not reach a terminal state")

    async def _wait_job_id(self, run_id: str) -> dict:
        deadline = asyncio.get_running_loop().time() + 1.0
        while asyncio.get_running_loop().time() < deadline:
            run = await db_module.get_pdf_export_run(run_id)
            if run and run.get("sidecar_job_id"):
                return run
            await asyncio.sleep(0.005)
        self.fail("sidecar job id was not persisted")

    async def _wait_deleted(self, client: MockSidecar, run_id: str) -> None:
        deadline = asyncio.get_running_loop().time() + 1.0
        while asyncio.get_running_loop().time() < deadline:
            if run_id in client.delete_calls:
                return
            await asyncio.sleep(0.005)
        self.fail("sidecar job was not deleted")

    async def _wait_cleanup_pending(
        self,
        run_id: str,
        expected: bool,
        timeout: float = 1.0,
        min_attempts: int = 0,
    ) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            run = await db_module.get_pdf_export_run(run_id)
            if (
                run
                and run["cleanup_pending"] is expected
                and int(run["cleanup_attempts"]) >= min_attempts
            ):
                return run
            await asyncio.sleep(0.005)
        self.fail(f"run {run_id} cleanup_pending did not become {expected}")

    async def _cancel_and_wait(self, arxiv_id: str, run_id: str) -> dict | None:
        task = service._RUN_TASKS.get(run_id)
        result = await service.cancel_pdf_export_run(arxiv_id, run_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        return result

    async def test_capability_requires_feature_license_and_token(self) -> None:
        disabled = service.get_pdf_export_capability(PdfExportConfig())
        self.assertFalse(disabled["enabled"])
        self.assertEqual(disabled["error_code"], "export_disabled")

        no_license = service.get_pdf_export_capability(
            PdfExportConfig(enabled=True, license_disclosure_complete=False)
        )
        self.assertFalse(no_license["enabled"])
        self.assertEqual(no_license["reason"], "license_disclosure_incomplete")

        with patch.dict(
            "os.environ",
            {
                "PEINIDU_PDF_EXPORT_SIDECAR_URL": "",
                "PEINIDU_PDF_EXPORT_SIDECAR_TOKEN": "",
                "PEINIDU_PDF_EXPORT_INTERNAL_TOKEN": "",
            },
        ):
            missing_token = service.get_pdf_export_capability(self.config)
        self.assertFalse(missing_token["enabled"])
        self.assertEqual(missing_token["reason"], "sidecar_not_configured")

        healthy = await service.probe_pdf_export_capability(
            self.config, client=MockSidecar()
        )
        self.assertTrue(healthy["enabled"])
        self.assertTrue(healthy["sidecar"]["healthy"])
        self.assertEqual(healthy["wrapper_version"], "1.0.1")
        self.assertEqual(healthy["sidecar"]["wrapper_version"], "1.0.1")
        self.assertEqual(
            healthy["modified_source_url"], "/pdf-exports/wrapper-source"
        )
        self.assertEqual(
            healthy["sidecar"]["modified_source_url"],
            "/pdf-exports/wrapper-source",
        )
        legacy_capability = service.get_pdf_export_capability(
            self.config.model_copy(update={"modified_source_url": ""})
        )
        self.assertEqual(
            legacy_capability["modified_source_url"],
            "/pdf-exports/wrapper-source",
        )
        self.assertEqual(
            healthy["notice_url"], "/pdf-exports/third-party-notice"
        )

        unavailable_client = MockSidecar()
        unavailable_client.health_error = PdfExportError(
            "sidecar_unavailable", "down", retryable=True
        )
        unavailable = await service.probe_pdf_export_capability(
            self.config, client=unavailable_client
        )
        self.assertFalse(unavailable["enabled"])
        self.assertEqual(unavailable["error_code"], "sidecar_unavailable")
        self.assertFalse(unavailable["sidecar"]["healthy"])

        source_hash_mismatch = await service.probe_pdf_export_capability(
            self.config,
            client=MockSidecar(
                info_overrides={"wrapper_source_sha256": "0" * 64}
            ),
        )
        self.assertFalse(source_hash_mismatch["enabled"])
        self.assertEqual(
            source_hash_mismatch["error_code"], "sidecar_unavailable"
        )
        self.assertFalse(source_hash_mismatch["sidecar"]["healthy"])

        async def network_down(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        real_unreachable = await service.probe_pdf_export_capability(
            self.config,
            client=PdfExportSidecarClient(
                "http://sidecar.test:8090",
                "test-token",
                transport=httpx.MockTransport(network_down),
            ),
        )
        self.assertFalse(real_unreachable["enabled"])
        self.assertEqual(real_unreachable["reason"], "sidecar_unavailable")

    async def test_disabled_and_missing_source_fail_before_run_creation(self) -> None:
        with self.assertRaises(PdfExportError) as disabled:
            await service.create_pdf_export_run(
                "1706.03762", config=PdfExportConfig(), client=MockSidecar()
            )
        self.assertEqual(disabled.exception.code, "export_disabled")

        with self.assertRaises(PdfExportError) as missing:
            await service.create_pdf_export_run(
                "1706.03762", config=self.config, client=MockSidecar()
            )
        self.assertEqual(missing.exception.code, "source_pdf_missing")
        self.assertEqual(await db_module.list_pdf_export_runs("1706.03762"), [])

    async def test_create_requires_live_attestation_before_persisting_run(self) -> None:
        self._write_source()
        unavailable = MockSidecar()
        unavailable.health_error = PdfExportError(
            "sidecar_unavailable", "down", retryable=True
        )
        with self.assertRaises(PdfExportError) as failed_probe:
            await service.create_pdf_export_run(
                "1706.03762", config=self.config, client=unavailable
            )
        self.assertEqual(failed_probe.exception.code, "sidecar_unavailable")
        self.assertEqual(unavailable.create_calls, 0)
        self.assertEqual(await db_module.list_pdf_export_runs("1706.03762"), [])

        mismatch = MockSidecar(info_overrides={"revision": "unexpected"})
        with self.assertRaises(PdfExportError) as failed_attestation:
            await service.create_pdf_export_run(
                "1706.03762", config=self.config, client=mismatch
            )
        self.assertEqual(failed_attestation.exception.code, "sidecar_unavailable")
        self.assertEqual(mismatch.health_calls, 1)
        self.assertEqual(mismatch.info_calls, 1)
        self.assertEqual(mismatch.create_calls, 0)
        self.assertEqual(await db_module.list_pdf_export_runs("1706.03762"), [])

        wrapper_mismatch = MockSidecar(
            info_overrides={"wrapper_version": "0.9.0"}
        )
        with self.assertRaises(PdfExportError) as failed_wrapper:
            await service.create_pdf_export_run(
                "1706.03762", config=self.config, client=wrapper_mismatch
            )
        self.assertEqual(failed_wrapper.exception.code, "sidecar_unavailable")
        self.assertEqual(wrapper_mismatch.create_calls, 0)
        self.assertEqual(await db_module.list_pdf_export_runs("1706.03762"), [])

        source_hash_mismatch = MockSidecar(
            info_overrides={"wrapper_source_sha256": "0" * 64}
        )
        with self.assertRaises(PdfExportError) as failed_source_hash:
            await service.create_pdf_export_run(
                "1706.03762", config=self.config, client=source_hash_mismatch
            )
        self.assertEqual(failed_source_hash.exception.code, "sidecar_unavailable")
        self.assertEqual(source_hash_mismatch.create_calls, 0)
        self.assertEqual(await db_module.list_pdf_export_runs("1706.03762"), [])

    async def test_public_third_party_notice_is_fixed_markdown(self) -> None:
        app = FastAPI()
        app.include_router(router)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            response = await http.get("/pdf-exports/third-party-notice")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/markdown", response.headers["content-type"])
        self.assertIn("charset=utf-8", response.headers["content-type"])
        self.assertIn("PDFMathTranslate-next", response.text)
        self.assertIn("BabelDOC", response.text)
        self.assertIn("sidecar/pdf_export", response.text)

        with patch.object(
            routes_pdf_exports,
            "_THIRD_PARTY_NOTICE",
            self.data_dir / "missing-notice.md",
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as http:
                missing = await http.get("/pdf-exports/third-party-notice")
        self.assertEqual(missing.status_code, 404)

    async def test_wrapper_source_archive_is_deterministic_and_allowlisted(self) -> None:
        expected_files = [
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
        ]
        app = FastAPI()
        app.include_router(router)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            first = await http.get("/pdf-exports/wrapper-source")
            second = await http.get("/pdf-exports/wrapper-source")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.headers["content-type"], "application/zip")
        self.assertEqual(first.headers["cache-control"], "public, max-age=300")
        self.assertIn(
            "peinidu-pdf-export-wrapper-1.0.1.zip",
            first.headers["content-disposition"],
        )
        self.assertEqual(first.content, second.content)

        with zipfile.ZipFile(io.BytesIO(first.content)) as archive:
            infos = archive.infolist()
            self.assertEqual([info.filename for info in infos], expected_files)
            for info in infos:
                self.assertEqual(
                    info.date_time,
                    routes_pdf_exports._WRAPPER_SOURCE_TIMESTAMP,
                )
                source = routes_pdf_exports._REPOSITORY_ROOT / info.filename
                self.assertEqual(archive.read(info.filename), source.read_bytes())

        forbidden_parts = {".env", "config", "data", "cache", "__pycache__"}
        for archive_path in expected_files:
            self.assertTrue(forbidden_parts.isdisjoint(Path(archive_path).parts))

    async def test_wrapper_source_archive_rejects_allowlisted_symlink(self) -> None:
        root = Path(self.temp.name) / "wrapper-source"
        for archive_path in routes_pdf_exports._WRAPPER_SOURCE_FILES:
            source = root / archive_path
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(archive_path, encoding="utf-8")
        target = root / "secret.env"
        target.write_text("SECRET=value", encoding="utf-8")
        linked = root / routes_pdf_exports._WRAPPER_SOURCE_FILES[0]
        linked.unlink()
        linked.symlink_to(target)
        with self.assertRaises(OSError):
            routes_pdf_exports._build_wrapper_source_archive(root)

    async def test_source_size_and_page_limits(self) -> None:
        source = self._write_source()
        too_small = self.config.model_copy(update={"max_source_bytes": 8})
        with self.assertRaises(PdfExportError) as large:
            await service.create_pdf_export_run(
                "1706.03762", config=too_small, client=MockSidecar()
            )
        self.assertEqual(large.exception.code, "source_pdf_too_large")

        with patch.object(service, "_pdf_page_count", return_value=11):
            with self.assertRaises(PdfExportError) as pages:
                await service.create_pdf_export_run(
                    "1706.03762", config=self.config, client=MockSidecar()
                )
        self.assertEqual(pages.exception.code, "page_limit_exceeded")
        self.assertEqual(source.read_bytes(), PDF_BYTES)

    async def test_same_paper_reuses_one_active_run(self) -> None:
        self._write_source()
        client = MockSidecar()
        client.status_gate = asyncio.Event()
        first, first_created = await service.create_pdf_export_run(
            "1706.03762", config=self.config, client=client
        )
        second, second_created = await service.create_pdf_export_run(
            "1706.03762", config=self.config, client=client
        )
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first["id"], second["id"])
        await self._wait_job_id(first["id"])
        self.assertEqual(client.create_calls, 1)
        self.assertEqual(client.delete_calls, [])
        await self._cancel_and_wait("1706.03762", first["id"])

    async def test_global_semaphore_limits_different_papers(self) -> None:
        self._write_source("paper-one")
        self._write_source("paper-two")
        client = BlockingCreateSidecar()
        first, _ = await service.create_pdf_export_run(
            "paper-one", config=self.config, client=client
        )
        second, _ = await service.create_pdf_export_run(
            "paper-two", config=self.config, client=client
        )
        await asyncio.wait_for(client.entered.wait(), timeout=1.0)
        await asyncio.sleep(0.02)
        self.assertEqual(client.create_calls, 1)
        self.assertEqual(client.max_active_creates, 1)
        client.release.set()
        first_done = await self._wait_terminal(first["id"])
        second_done = await self._wait_terminal(second["id"])
        self.assertEqual(first_done["status"], "done")
        self.assertEqual(second_done["status"], "done")
        self.assertEqual(client.max_active_creates, 1)

    async def test_job_id_is_persisted_before_create_and_lost_response_is_cleaned(self) -> None:
        self._write_source()
        client = LostCreateResponseSidecar()
        run, _ = await service.create_pdf_export_run(
            "1706.03762", config=self.config, client=client
        )
        terminal = await self._wait_terminal(run["id"])
        self.assertEqual(terminal["status"], "error")
        self.assertEqual(terminal["sidecar_job_id"], run["id"])
        await self._wait_deleted(client, run["id"])
        self.assertEqual(
            client.events[-2:],
            [("cancel", run["id"]), ("delete", run["id"])],
        )

    async def test_cancel_calls_sidecar_and_late_done_cannot_win(self) -> None:
        self._write_source()
        client = MockSidecar()
        client.status_gate = asyncio.Event()
        run, _ = await service.create_pdf_export_run(
            "1706.03762", config=self.config, client=client
        )
        await self._wait_job_id(run["id"])
        cancelled = await self._cancel_and_wait("1706.03762", run["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertGreaterEqual(client.cancel_calls.count(client.job_id), 1)

        late = await db_module.transition_pdf_export_run(
            run["id"],
            from_statuses=("running",),
            status="done",
            output_sha256="late",
            output_bytes=10,
            output_pages=2,
            output_path="/tmp/late.pdf",
        )
        self.assertFalse(late)
        persisted = await db_module.get_pdf_export_run(run["id"])
        self.assertEqual(persisted["status"], "cancelled")

    async def test_timeout_cancels_remote_and_records_stable_code(self) -> None:
        self._write_source()
        client = MockSidecar()
        client.status_gate = asyncio.Event()
        config = self.config.model_copy(update={"timeout_seconds": 0.02})
        run, _ = await service.create_pdf_export_run(
            "1706.03762", config=config, client=client
        )
        terminal = await self._wait_terminal(run["id"])
        self.assertEqual(terminal["status"], "error")
        self.assertEqual(terminal["error_code"], "export_timeout")
        self.assertGreaterEqual(client.cancel_calls.count(client.job_id), 1)

    async def test_sidecar_failure_codes_are_persisted(self) -> None:
        for index, code in enumerate(
            ("sidecar_rate_limited", "sidecar_auth_failed", "sidecar_crashed")
        ):
            arxiv_id = f"paper-{index}"
            self._write_source(arxiv_id)
            client = MockSidecar()
            client.statuses = [
                PdfExportError(code, f"failure-{index}", retryable=code != "sidecar_auth_failed")
            ]
            run, _ = await service.create_pdf_export_run(
                arxiv_id, config=self.config, client=client
            )
            terminal = await self._wait_terminal(run["id"])
            self.assertEqual(terminal["status"], "error")
            self.assertEqual(terminal["error_code"], code)

    async def test_sidecar_http_status_mapping(self) -> None:
        for status_code, code in (
            (401, "sidecar_auth_failed"),
            (403, "sidecar_auth_failed"),
            (429, "sidecar_rate_limited"),
            (413, "source_pdf_too_large"),
            (503, "sidecar_unavailable"),
        ):
            with self.subTest(status_code=status_code):
                response = httpx.Response(status_code)
                with self.assertRaises(PdfExportError) as raised:
                    PdfExportSidecarClient._raise_for_status(response)
                self.assertEqual(raised.exception.code, code)

    async def test_sidecar_multipart_and_structured_error_contract(self) -> None:
        source = self._write_source()
        seen_body = b""

        async def create_handler(request: httpx.Request) -> httpx.Response:
            nonlocal seen_body
            seen_body = await request.aread()
            self.assertEqual(request.url.path, "/jobs")
            self.assertEqual(request.headers["authorization"], "Bearer test-token")
            self.assertNotIn("x-peinidu-internal-token", request.headers)
            return httpx.Response(200, json={"job_id": "backend-run-id"})

        client = PdfExportSidecarClient(
            "http://sidecar.test:8090",
            "test-token",
            transport=httpx.MockTransport(create_handler),
        )
        returned = await client.create_job(source, "backend-run-id")
        self.assertEqual(returned, "backend-run-id")
        self.assertIn(b'name="job_id"', seen_body)
        self.assertIn(b"backend-run-id", seen_body)
        self.assertIn(b'name="file"', seen_body)
        self.assertNotIn(b'name="input"', seen_body)
        self.assertNotIn(b"target_language", seen_body)
        self.assertNotIn(b"output_mode", seen_body)

        for provider_code, expected in (
            ("provider_authentication_failed", "sidecar_auth_failed"),
            ("provider_rate_limited", "sidecar_rate_limited"),
            ("provider_timeout", "export_timeout"),
            ("worker_crashed", "sidecar_crashed"),
        ):
            async def error_handler(
                request: httpx.Request, code: str = provider_code
            ) -> httpx.Response:
                return httpx.Response(
                    200,
                    json={
                        "status": "error",
                        "error": {"code": code, "message": f"mapped-{code}"},
                    },
                )

            error_client = PdfExportSidecarClient(
                "http://sidecar.test:8090",
                "test-token",
                transport=httpx.MockTransport(error_handler),
            )
            with self.subTest(provider_code=provider_code):
                with self.assertRaises(PdfExportError) as raised:
                    await error_client.get_job("backend-run-id")
                self.assertEqual(raised.exception.code, expected)
                self.assertEqual(raised.exception.message, f"mapped-{provider_code}")

        async def progress_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "status": "running",
                    "progress": 0.4,
                    "stage": "translating",
                    "pages_done": 3,
                },
            )

        progress_client = PdfExportSidecarClient(
            "http://sidecar.test:8090",
            "test-token",
            transport=httpx.MockTransport(progress_handler),
        )
        progress_state = await progress_client.get_job("backend-run-id")
        self.assertEqual(progress_state.progress, 0.4)
        self.assertEqual(progress_state.stage, "translating")
        self.assertEqual(progress_state.pages_done, 3)

    async def test_invalid_oversized_and_page_mismatched_outputs_fail_closed(self) -> None:
        scenarios = (
            ("invalid", b"not-a-pdf", self.config, 2),
            (
                "oversized",
                PDF_BYTES + b"x" * 128,
                self.config.model_copy(update={"max_output_bytes": 64}),
                2,
            ),
            ("page-mismatch", PDF_BYTES, self.config, 1),
        )
        for name, output, config, output_pages in scenarios:
            arxiv_id = f"paper-{name}"
            self._write_source(arxiv_id)

            def page_count(path: Path, expected=output_pages) -> int:
                return 2 if path.name == "original.pdf" else expected

            with patch.object(service, "_pdf_page_count", side_effect=page_count):
                run, _ = await service.create_pdf_export_run(
                    arxiv_id, config=config, client=MockSidecar(output=output)
                )
                terminal = await self._wait_terminal(run["id"])
            self.assertEqual(terminal["status"], "error", name)
            self.assertEqual(terminal["error_code"], "output_validation_failed", name)
            output_path = (
                self.data_dir
                / "pdf_exports"
                / arxiv_id
                / run["id"]
                / "translated.zh-CN.pdf"
            )
            self.assertFalse(output_path.exists(), name)

    async def test_success_publishes_atomically_outside_paper_directory(self) -> None:
        source = self._write_source()
        client = MockSidecar()
        run, _ = await service.create_pdf_export_run(
            "1706.03762", config=self.config, client=client
        )
        terminal = await self._wait_terminal(run["id"])
        self.assertEqual(terminal["status"], "done")
        output = Path(terminal["output_path"])
        self.assertTrue(output.is_file())
        self.assertTrue(output.is_relative_to(self.data_dir / "pdf_exports"))
        self.assertFalse(output.is_relative_to(self.papers_dir))
        self.assertEqual(source.read_bytes(), PDF_BYTES)
        self.assertEqual(terminal["source_pages"], terminal["output_pages"])
        self.assertTrue(terminal["source_sha256"])
        self.assertTrue(terminal["output_sha256"])
        self.assertEqual(terminal["progress"], 1.0)
        self.assertEqual(terminal["stage"], "done")
        self.assertEqual(terminal["pages_done"], 2)
        self.assertEqual(terminal["provenance"]["schema_version"], 2)
        self.assertEqual(terminal["provenance"]["wrapper_version"], "1.0.1")
        self.assertEqual(
            terminal["provenance"]["wrapper_source_sha256"],
            service.trusted_wrapper_source_sha256(),
        )
        self.assertEqual(
            terminal["provenance"]["upstream"]["revision"],
            self.config.sidecar_commit,
        )
        self.assertEqual(
            terminal["provenance"]["output_mode"],
            "monolingual-watermarked-zh-CN",
        )
        self.assertEqual(
            terminal["provenance"]["language"],
            {"source": "en", "target": "zh-CN"},
        )
        self.assertEqual(len(terminal["provenance"]["config_fingerprint"]), 64)
        await self._wait_deleted(client, run["id"])
        self.assertEqual(
            client.events[-2:],
            [("cancel", run["id"]), ("delete", run["id"])],
        )

    async def test_completed_same_source_is_reused_concurrently_without_new_files(self) -> None:
        self._write_source()
        first_client = MockSidecar()
        first, _ = await service.create_pdf_export_run(
            "1706.03762", config=self.config, client=first_client
        )
        completed = await self._wait_terminal(first["id"])
        await self._wait_deleted(first_client, first["id"])
        output = Path(completed["output_path"])
        before_mtime = output.stat().st_mtime_ns
        before_files = sorted(
            path.relative_to(self.data_dir)
            for path in (self.data_dir / "pdf_exports").rglob("*")
            if path.is_file()
        )

        unused_client = MockSidecar()
        results = await asyncio.gather(
            service.create_pdf_export_run(
                "1706.03762", config=self.config, client=unused_client
            ),
            service.create_pdf_export_run(
                "1706.03762", config=self.config, client=unused_client
            ),
        )
        self.assertEqual(
            [result[0]["id"] for result in results],
            [first["id"], first["id"]],
        )
        self.assertEqual([result[1] for result in results], [False, False])
        self.assertEqual(unused_client.create_calls, 0)
        self.assertEqual(output.stat().st_mtime_ns, before_mtime)
        after_files = sorted(
            path.relative_to(self.data_dir)
            for path in (self.data_dir / "pdf_exports").rglob("*")
            if path.is_file()
        )
        self.assertEqual(after_files, before_files)

    async def test_completed_run_requires_matching_provenance_for_reuse(self) -> None:
        self._write_source()
        first, _ = await service.create_pdf_export_run(
            "1706.03762", config=self.config, client=MockSidecar()
        )
        first_done = await self._wait_terminal(first["id"])
        self.assertIsNotNone(first_done["provenance"])

        new_revision = "1" * 40
        changed_config = self.config.model_copy(
            update={"sidecar_commit": new_revision}
        )
        changed_client = MockSidecar(info_overrides={"revision": new_revision})
        second, second_created = await service.create_pdf_export_run(
            "1706.03762", config=changed_config, client=changed_client
        )
        self.assertTrue(second_created)
        self.assertNotEqual(second["id"], first["id"])
        second_done = await self._wait_terminal(second["id"])
        self.assertEqual(
            second_done["provenance"]["upstream"]["revision"], new_revision
        )

    async def test_wrapper_version_change_does_not_reuse_completed_output(self) -> None:
        self._write_source()
        first, _ = await service.create_pdf_export_run(
            "1706.03762", config=self.config, client=MockSidecar()
        )
        await self._wait_terminal(first["id"])

        changed_config = self.config.model_copy(update={"wrapper_version": "1.0.2"})
        changed_client = MockSidecar(
            info_overrides={"wrapper_version": "1.0.2"}
        )
        second, second_created = await service.create_pdf_export_run(
            "1706.03762", config=changed_config, client=changed_client
        )
        self.assertTrue(second_created)
        self.assertNotEqual(second["id"], first["id"])
        second_done = await self._wait_terminal(second["id"])
        self.assertEqual(second_done["provenance"]["wrapper_version"], "1.0.2")
        self.assertNotEqual(
            second_done["provenance"]["config_fingerprint"],
            (await db_module.get_pdf_export_run(first["id"]))["provenance"][
                "config_fingerprint"
            ],
        )

    async def test_wrapper_source_change_does_not_reuse_completed_output(self) -> None:
        self._write_source()
        first, _ = await service.create_pdf_export_run(
            "1706.03762", config=self.config, client=MockSidecar()
        )
        first_done = await self._wait_terminal(first["id"])

        changed_hash = "1" * 64
        changed_client = MockSidecar(
            info_overrides={"wrapper_source_sha256": changed_hash}
        )
        with patch.object(
            service,
            "trusted_wrapper_source_sha256",
            return_value=changed_hash,
        ):
            second, second_created = await service.create_pdf_export_run(
                "1706.03762", config=self.config, client=changed_client
            )
            second_done = await self._wait_terminal(second["id"])

        self.assertTrue(second_created)
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(
            second_done["provenance"]["wrapper_source_sha256"], changed_hash
        )
        self.assertNotEqual(
            second_done["provenance"]["config_fingerprint"],
            first_done["provenance"]["config_fingerprint"],
        )

    async def test_legacy_completed_run_without_provenance_is_read_but_not_reused(self) -> None:
        self._write_source()
        first, _ = await service.create_pdf_export_run(
            "1706.03762", config=self.config, client=MockSidecar()
        )
        first_done = await self._wait_terminal(first["id"])
        async with aiosqlite.connect(db_module.DB_PATH) as db:
            await db.execute(
                "UPDATE pdf_export_runs SET provenance=NULL WHERE run_id=?",
                (first["id"],),
            )
            await db.commit()
        legacy = await db_module.get_pdf_export_run(first["id"])
        self.assertIsNone(legacy["provenance"])
        self.assertEqual(legacy["status"], "done")
        self.assertTrue(Path(first_done["output_path"]).is_file())
        app = FastAPI()
        app.include_router(router)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            download = await http.get(
                f"/papers/1706.03762/pdf-exports/{first['id']}/download"
            )
            listed = await http.get("/papers/1706.03762/pdf-exports")
        self.assertEqual(download.status_code, 409)
        self.assertIsNone(listed.json()[0]["download_url"])

        second, second_created = await service.create_pdf_export_run(
            "1706.03762", config=self.config, client=MockSidecar()
        )
        self.assertTrue(second_created)
        self.assertNotEqual(second["id"], first["id"])
        await self._wait_terminal(second["id"])

    async def test_completed_run_missing_wrapper_hash_is_quarantined(self) -> None:
        self._write_source()
        run, _ = await service.create_pdf_export_run(
            "1706.03762", config=self.config, client=MockSidecar()
        )
        completed = await self._wait_terminal(run["id"])
        provenance = dict(completed["provenance"])
        provenance.pop("wrapper_source_sha256")
        async with aiosqlite.connect(db_module.DB_PATH) as db:
            await db.execute(
                "UPDATE pdf_export_runs SET provenance=? WHERE run_id=?",
                (json.dumps(provenance, sort_keys=True), run["id"]),
            )
            await db.commit()
        quarantined = await db_module.get_pdf_export_run(run["id"])
        self.assertFalse(service.pdf_export_download_url_allowed(quarantined))
        with self.assertRaises(PdfExportError) as raised:
            await service.validated_pdf_export_download_path(
                quarantined, self.config
            )
        self.assertEqual(raised.exception.code, "legacy_output_quarantined")

    async def test_existing_database_adds_progress_and_provenance_columns(self) -> None:
        legacy_db = self.data_dir / "legacy.db"
        async with aiosqlite.connect(legacy_db) as db:
            await db.execute(
                """CREATE TABLE pdf_export_runs (
                    run_id TEXT PRIMARY KEY,
                    arxiv_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    target_language TEXT NOT NULL DEFAULT 'zh-CN',
                    sidecar_job_id TEXT,
                    source_sha256 TEXT NOT NULL,
                    output_sha256 TEXT,
                    source_bytes INTEGER NOT NULL,
                    output_bytes INTEGER,
                    source_pages INTEGER NOT NULL,
                    output_pages INTEGER,
                    output_path TEXT,
                    error_code TEXT DEFAULT '',
                    error_message TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                )"""
            )
            await db.execute(
                """INSERT INTO pdf_export_runs
                   (run_id, arxiv_id, status, source_sha256, source_bytes,
                    source_pages, created_at, updated_at)
                   VALUES ('legacy-run', 'legacy-paper', 'queued', 'hash', 10,
                           2, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"""
            )
            await db.execute(
                """INSERT INTO pdf_export_runs
                   (run_id, arxiv_id, status, sidecar_job_id, source_sha256,
                    source_bytes, source_pages, created_at, updated_at)
                   VALUES ('legacy-remote', 'legacy-remote-paper', 'done',
                           'legacy-remote', 'hash', 10, 2,
                           '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"""
            )
            await db.commit()

        with patch.object(db_module, "DB_PATH", legacy_db):
            await db_module.init_db()
            legacy = await db_module.get_pdf_export_run("legacy-run")
            legacy_remote = await db_module.get_pdf_export_run("legacy-remote")
            async with aiosqlite.connect(legacy_db) as db:
                columns = {
                    row[1]
                    for row in await (await db.execute(
                        "PRAGMA table_info(pdf_export_runs)"
                    )).fetchall()
                }
            await db_module.record_pdf_export_cleanup_result(
                "legacy-remote", deleted=True
            )
            await db_module.init_db()
            cleaned_legacy_remote = await db_module.get_pdf_export_run(
                "legacy-remote"
            )
        self.assertTrue(
            {
                "progress",
                "stage",
                "pages_done",
                "provenance",
                "cleanup_pending",
                "cleanup_attempts",
            }.issubset(columns)
        )
        self.assertIsNone(legacy["progress"])
        self.assertEqual(legacy["stage"], "")
        self.assertIsNone(legacy["pages_done"])
        self.assertIsNone(legacy["provenance"])
        self.assertFalse(legacy["cleanup_pending"])
        self.assertEqual(legacy["cleanup_attempts"], 0)
        self.assertTrue(legacy_remote["cleanup_pending"])
        self.assertEqual(legacy_remote["cleanup_attempts"], 0)
        self.assertFalse(cleaned_legacy_remote["cleanup_pending"])

    async def test_sidecar_progress_is_persisted_and_unknown_stays_null(self) -> None:
        self._write_source()
        client = ProgressSidecar(
            SidecarJob(
                "running", progress=0.4, stage="translating", pages_done=1
            )
        )
        run, _ = await service.create_pdf_export_run(
            "1706.03762", config=self.config, client=client
        )
        await asyncio.wait_for(client.first_seen.wait(), timeout=1.0)
        deadline = asyncio.get_running_loop().time() + 1.0
        persisted = None
        while asyncio.get_running_loop().time() < deadline:
            persisted = await db_module.get_pdf_export_run(run["id"])
            if persisted and persisted["stage"] == "translating":
                break
            await asyncio.sleep(0.005)
        self.assertEqual(persisted["progress"], 0.4)
        self.assertEqual(persisted["pages_done"], 1)

        client.release_done.set()
        completed = await self._wait_terminal(run["id"])
        self.assertEqual(completed["progress"], 1.0)
        self.assertEqual(completed["pages_done"], 2)

        unknown, _ = await db_module.try_create_pdf_export_run(
            run_id="unknown-progress",
            arxiv_id="unknown-paper",
            source_sha256="hash",
            source_bytes=10,
            source_pages=2,
        )
        item = routes_pdf_exports._run_item(unknown)
        self.assertIsNone(item.progress)
        self.assertIsNone(item.pages_done)

    async def test_missing_or_unsafe_completed_output_creates_new_run(self) -> None:
        source = self._write_source()
        first_client = MockSidecar()
        first, _ = await service.create_pdf_export_run(
            "1706.03762", config=self.config, client=first_client
        )
        first_done = await self._wait_terminal(first["id"])
        await self._wait_deleted(first_client, first["id"])
        Path(first_done["output_path"]).unlink()

        second_client = MockSidecar()
        second, second_created = await service.create_pdf_export_run(
            "1706.03762", config=self.config, client=second_client
        )
        self.assertTrue(second_created)
        self.assertNotEqual(second["id"], first["id"])
        await self._wait_terminal(second["id"])
        await self._wait_deleted(second_client, second["id"])

        async with aiosqlite.connect(db_module.DB_PATH) as db:
            await db.execute(
                "UPDATE pdf_export_runs SET output_path=? WHERE run_id=?",
                (str(source), second["id"]),
            )
            await db.commit()
        app = FastAPI()
        app.include_router(router)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            unsafe_download = await http.get(
                f"/papers/1706.03762/pdf-exports/{second['id']}/download"
            )
        self.assertEqual(unsafe_download.status_code, 404)
        third_client = MockSidecar()
        third, third_created = await service.create_pdf_export_run(
            "1706.03762", config=self.config, client=third_client
        )
        self.assertTrue(third_created)
        self.assertNotEqual(third["id"], second["id"])
        third_done = await self._wait_terminal(third["id"])
        await self._wait_deleted(third_client, third["id"])
        self.assertEqual(third_done["status"], "done")
        self.assertEqual(source.read_bytes(), PDF_BYTES)

    async def test_failed_job_is_deleted_after_terminal_state(self) -> None:
        self._write_source()
        client = MockSidecar()
        client.statuses = [
            PdfExportError("sidecar_crashed", "worker crashed", retryable=True)
        ]
        run, _ = await service.create_pdf_export_run(
            "1706.03762", config=self.config, client=client
        )
        terminal = await self._wait_terminal(run["id"])
        self.assertEqual(terminal["error_code"], "sidecar_crashed")
        await self._wait_deleted(client, run["id"])

    async def test_failed_remote_delete_is_retried_after_restart(self) -> None:
        self._write_source()
        client = FlakyDeleteSidecar(failures=1)
        run, _ = await service.create_pdf_export_run(
            "1706.03762", config=self.config, client=client
        )
        terminal = await self._wait_terminal(run["id"])
        self.assertEqual(terminal["status"], "done")
        pending = await self._wait_cleanup_pending(
            run["id"], True, min_attempts=1
        )
        self.assertEqual(pending["cleanup_attempts"], 1)
        self.assertEqual(client.delete_calls, [run["id"]])

        service.reset_pdf_export_runtime_for_tests()
        swept = await service.sweep_stale_pdf_export_runs(client=client)
        self.assertEqual(swept, 0)
        cleaned = await self._wait_cleanup_pending(run["id"], False)
        self.assertFalse(cleaned["cleanup_pending"])
        self.assertEqual(cleaned["cleanup_attempts"], 1)
        self.assertEqual(client.delete_calls, [run["id"], run["id"]])

    async def test_later_create_retries_terminal_remote_cleanup(self) -> None:
        self._write_source("first-paper")
        self._write_source("second-paper")
        client = FlakyDeleteSidecar(failures=1)
        first, _ = await service.create_pdf_export_run(
            "first-paper", config=self.config, client=client
        )
        await self._wait_terminal(first["id"])
        await self._wait_cleanup_pending(first["id"], True, min_attempts=1)

        second, _ = await service.create_pdf_export_run(
            "second-paper", config=self.config, client=client
        )
        cleaned = await self._wait_cleanup_pending(first["id"], False)
        self.assertFalse(cleaned["cleanup_pending"])
        self.assertEqual(client.delete_calls[:2], [first["id"], first["id"]])
        second_done = await self._wait_terminal(second["id"])
        self.assertEqual(second_done["status"], "done")

    async def test_downloads_allow_original_and_only_done_output(self) -> None:
        self._write_source()
        client = MockSidecar()
        client.status_gate = asyncio.Event()
        queued, _ = await service.create_pdf_export_run(
            "1706.03762", config=self.config, client=client
        )
        app = FastAPI()
        app.include_router(router)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            original = await http.get("/papers/1706.03762/original-pdf/download")
            pending = await http.get(
                f"/papers/1706.03762/pdf-exports/{queued['id']}/download"
            )
        self.assertEqual(original.status_code, 200)
        self.assertEqual(original.content, PDF_BYTES)
        self.assertEqual(original.headers["cache-control"], "private, no-store")
        self.assertEqual(pending.status_code, 409)
        await self._cancel_and_wait("1706.03762", queued["id"])

        done_client = MockSidecar()
        done_run, _ = await service.create_pdf_export_run(
            "1706.03762", config=self.config, client=done_client
        )
        terminal = await self._wait_terminal(done_run["id"])
        self.assertEqual(terminal["status"], "done")
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            exported = await http.get(
                f"/papers/1706.03762/pdf-exports/{done_run['id']}/download"
            )
            listed = await http.get("/papers/1706.03762/pdf-exports")
        self.assertEqual(exported.status_code, 200)
        self.assertEqual(exported.content, PDF_BYTES)
        self.assertEqual(exported.headers["cache-control"], "private, no-store")
        self.assertEqual(listed.status_code, 200)
        item = listed.json()[0]
        self.assertEqual(item["page_count"], 2)
        self.assertEqual(item["pages_done"], 2)
        self.assertEqual(item["progress"], 1.0)
        self.assertEqual(item["stage"], "done")
        self.assertIsNotNone(item["provenance"])

    async def test_download_rejects_output_changed_after_publication(self) -> None:
        self._write_source()
        run, _ = await service.create_pdf_export_run(
            "1706.03762", config=self.config, client=MockSidecar()
        )
        completed = await self._wait_terminal(run["id"])
        output = Path(completed["output_path"])
        tampered = PDF_BYTES.replace(b"test", b"evil")
        self.assertEqual(len(tampered), len(PDF_BYTES))
        output.write_bytes(tampered)

        app = FastAPI()
        app.include_router(router)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            download = await http.get(
                f"/papers/1706.03762/pdf-exports/{run['id']}/download"
            )
        self.assertEqual(download.status_code, 409)

    async def test_startup_sweep_marks_active_runs_backend_restarted(self) -> None:
        self._write_source()
        client = MockSidecar()
        client.status_gate = asyncio.Event()
        run, _ = await service.create_pdf_export_run(
            "1706.03762", config=self.config, client=client
        )
        await self._wait_job_id(run["id"])
        swept = await service.sweep_stale_pdf_export_runs(client=client)
        self.assertEqual(swept, 1)
        persisted = await db_module.get_pdf_export_run(run["id"])
        self.assertEqual(persisted["status"], "error")
        self.assertEqual(persisted["error_code"], "backend_restarted")
        self.assertFalse(persisted["cleanup_pending"])
        self.assertEqual(persisted["cleanup_attempts"], 0)
        self.assertEqual(
            client.events[:2],
            [("cancel", run["id"]), ("delete", run["id"])],
        )
        task = service._RUN_TASKS.get(run["id"])
        client.status_gate.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def test_startup_sweep_still_marks_error_when_cleanup_is_unavailable(self) -> None:
        run, _ = await db_module.try_create_pdf_export_run(
            run_id="orphan-run",
            arxiv_id="1706.03762",
            source_sha256="source-hash",
            source_bytes=10,
            source_pages=2,
        )
        await db_module.transition_pdf_export_run(
            run["id"], from_statuses=("queued",), status="running"
        )
        await db_module.set_pdf_export_sidecar_job(run["id"], run["id"])

        class UnavailableCleanup(MockSidecar):
            async def cancel_job(self, job_id: str) -> None:
                raise PdfExportError("sidecar_unavailable", "down", retryable=True)

            async def delete_job(self, job_id: str) -> None:
                raise PdfExportError("sidecar_unavailable", "down", retryable=True)

        started = asyncio.get_running_loop().time()
        swept = await service.sweep_stale_pdf_export_runs(client=UnavailableCleanup())
        elapsed = asyncio.get_running_loop().time() - started
        self.assertEqual(swept, 1)
        self.assertLess(elapsed, 0.5)
        persisted = await db_module.get_pdf_export_run(run["id"])
        self.assertEqual(persisted["status"], "error")
        self.assertEqual(persisted["error_code"], "backend_restarted")
        self.assertTrue(persisted["cleanup_pending"])
        self.assertEqual(persisted["cleanup_attempts"], 1)

    async def test_startup_sweep_has_bounded_cleanup_budget(self) -> None:
        run, _ = await db_module.try_create_pdf_export_run(
            run_id="hanging-orphan",
            arxiv_id="1706.03762",
            source_sha256="source-hash",
            source_bytes=10,
            source_pages=2,
        )
        await db_module.transition_pdf_export_run(
            run["id"], from_statuses=("queued",), status="running"
        )
        await db_module.set_pdf_export_sidecar_job(run["id"], run["id"])

        class HangingCleanup(MockSidecar):
            async def cancel_job(self, job_id: str) -> None:
                await asyncio.Event().wait()

        started = asyncio.get_running_loop().time()
        swept = await service.sweep_stale_pdf_export_runs(
            client=HangingCleanup(), cleanup_timeout_seconds=2.0
        )
        elapsed = asyncio.get_running_loop().time() - started
        self.assertEqual(swept, 1)
        self.assertGreaterEqual(elapsed, 1.8)
        self.assertLess(elapsed, 2.6)
        persisted = await db_module.get_pdf_export_run(run["id"])
        self.assertEqual(persisted["error_code"], "backend_restarted")
        self.assertTrue(persisted["cleanup_pending"])
        self.assertEqual(persisted["cleanup_attempts"], 0)


if __name__ == "__main__":
    unittest.main()
