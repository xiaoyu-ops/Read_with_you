from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from sidecar.pdf_export import app as sidecar


TOKEN_ENV = {"PEINIDU_PDF_EXPORT_INTERNAL_TOKEN": "sidecar-test-token"}
AUTH = {"Authorization": "Bearer sidecar-test-token"}


class SidecarAPITests(unittest.TestCase):
    def setUp(self) -> None:
        sidecar._jobs.clear()
        self.client = TestClient(sidecar.app)

    def tearDown(self) -> None:
        self.client.close()
        sidecar._jobs.clear()

    def test_bearer_is_required(self) -> None:
        with patch.dict(os.environ, TOKEN_ENV, clear=True):
            response = self.client.get("/info")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], "Bearer")

    def test_info_discloses_pinned_source_license_and_output(self) -> None:
        with patch.dict(os.environ, TOKEN_ENV, clear=True):
            response = self.client.get("/info", headers=AUTH)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["version"], "2.9.0")
        self.assertEqual(body["revision"], sidecar.UPSTREAM_REVISION)
        self.assertEqual(body["license"], "AGPL-3.0")
        self.assertEqual(
            body["source"],
            "https://github.com/PDFMathTranslate-next/PDFMathTranslate-next/tree/v2.9.0",
        )
        self.assertIn("@sha256:", body["image"])
        self.assertEqual(body["wrapper_version"], "1.0.1")
        self.assertEqual(
            body["wrapper_source_sha256"],
            sidecar._wrapper_source_sha256(),
        )
        self.assertEqual(len(body["wrapper_source_sha256"]), 64)
        self.assertEqual(body["output"], "monolingual-watermarked-zh-CN")

    def test_wrapper_source_digest_is_deterministic_and_content_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for relative_name in sidecar.WRAPPER_SOURCE_FILES:
                source = root / relative_name
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(f"fixture:{relative_name}\n".encode())

            first = sidecar._wrapper_source_sha256(root)
            second = sidecar._wrapper_source_sha256(root)
            (root / "app.py").write_bytes(b"changed app source\n")
            changed = sidecar._wrapper_source_sha256(root)

        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertNotEqual(first, changed)

    def test_wrapper_source_digest_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for relative_name in sidecar.WRAPPER_SOURCE_FILES:
                source = root / relative_name
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(f"fixture:{relative_name}\n".encode())
            app_source = root / "app.py"
            real_source = root / "real-app.py"
            app_source.replace(real_source)
            app_source.symlink_to(real_source)

            with self.assertRaises(OSError):
                sidecar._wrapper_source_sha256(root)

    def test_info_fails_closed_when_wrapper_source_is_unavailable(self) -> None:
        with (
            patch.dict(os.environ, TOKEN_ENV, clear=True),
            patch.object(
                sidecar,
                "_wrapper_source_sha256",
                side_effect=OSError("missing source"),
            ),
        ):
            response = self.client.get("/info", headers=AUTH)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("missing source", response.text)

    def test_post_contract_requires_caller_job_id_and_file_field(self) -> None:
        async def finish_immediately(job: sidecar.ExportJob) -> None:
            job.status = "error"
            job.error_code = "translation_failed"
            job.error_message = "mocked"

        with (
            tempfile.TemporaryDirectory() as temp,
            patch.dict(os.environ, TOKEN_ENV, clear=True),
            patch.object(sidecar, "WORK_ROOT", Path(temp)),
            patch.object(sidecar, "_validate_pdf", return_value=1),
            patch.object(sidecar, "_translate_job", new=finish_immediately),
        ):
            wrong_field = self.client.post(
                "/jobs",
                headers=AUTH,
                data={"job_id": "run-1"},
                files={"input": ("paper.pdf", b"%PDF-1.4\n", "application/pdf")},
            )
            missing_id = self.client.post(
                "/jobs",
                headers=AUTH,
                files={"file": ("paper.pdf", b"%PDF-1.4\n", "application/pdf")},
            )
            accepted = self.client.post(
                "/jobs",
                headers=AUTH,
                data={"job_id": "run-1"},
                files={"file": ("paper.pdf", b"%PDF-1.4\n", "application/pdf")},
            )
            traversal = self.client.post(
                "/jobs",
                headers=AUTH,
                data={"job_id": "../escape"},
                files={"file": ("paper.pdf", b"%PDF-1.4\n", "application/pdf")},
            )
        self.assertEqual(wrong_field.status_code, 422)
        self.assertEqual(missing_id.status_code, 422)
        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(accepted.json()["job_id"], "run-1")
        self.assertEqual(traversal.status_code, 422)

    def test_status_api_exposes_only_stable_error_codes(self) -> None:
        codes = (
            "provider_authentication_failed",
            "provider_rate_limited",
            "provider_timeout",
            "translation_failed",
            "output_validation_failed",
        )
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, TOKEN_ENV, clear=True):
            for index, code in enumerate(codes):
                directory = Path(temp) / f"job-{index}"
                job = sidecar.ExportJob(
                    job_id=f"job-{index}",
                    directory=directory,
                    input_path=directory / "input.pdf",
                    output_path=directory / "output" / "translated.zh-CN.pdf",
                    page_count=1,
                    status="error",
                    stage="error",
                    error_code=code,
                    error_message="Safe public message.",
                )
                sidecar._jobs[job.job_id] = job
                response = self.client.get(f"/jobs/{job.job_id}", headers=AUTH)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["error"]["code"], code)
                self.assertNotIn("traceback", response.text.lower())

    def test_upload_type_and_streaming_size_limits_are_enforced(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.dict(os.environ, TOKEN_ENV, clear=True),
            patch.object(sidecar, "WORK_ROOT", Path(temp)),
            patch.object(sidecar, "MAX_FILE_BYTES", 9),
        ):
            wrong_type = self.client.post(
                "/jobs",
                headers=AUTH,
                data={"job_id": "wrong-type"},
                files={"file": ("paper.txt", b"%PDF-1.4\n", "text/plain")},
            )
            too_large = self.client.post(
                "/jobs",
                headers=AUTH,
                data={"job_id": "too-large"},
                files={"file": ("paper.pdf", b"%PDF-1.4\nextra", "application/pdf")},
            )
        self.assertEqual(wrong_type.status_code, 415)
        self.assertEqual(too_large.status_code, 413)


class SidecarTranslationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        directory = Path(self.temp.name) / "job"
        directory.mkdir()
        input_path = directory / "input.pdf"
        input_path.write_bytes(b"%PDF-1.4\n")
        self.job = sidecar.ExportJob(
            job_id="job-1",
            directory=directory,
            input_path=input_path,
            output_path=directory / "output" / "translated.zh-CN.pdf",
            page_count=1,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def test_finish_retains_only_mono_result(self) -> None:
        output = self.job.directory / "output"
        output.mkdir()
        mono = output / "paper-mono.pdf"
        dual = output / "paper-dual.pdf"
        mono.write_bytes(b"%PDF-mono")
        dual.write_bytes(b"%PDF-dual")

        async def events(_settings, _path):
            yield {"type": "progress", "progress": 50, "stage": "translating"}
            yield {"type": "finish", "translate_result": SimpleNamespace(mono_pdf_path=mono)}

        with (
            patch.dict(os.environ, TOKEN_ENV, clear=True),
            patch.object(sidecar, "_make_settings", return_value=object()),
            patch.object(sidecar, "do_translate_async_stream", new=events),
            patch.object(sidecar, "_restore_safe_interactivity"),
            patch.object(sidecar, "_validate_pdf", return_value=1),
        ):
            await sidecar._translate_job(self.job)
        self.assertEqual(self.job.status, "done")
        self.assertEqual(self.job.progress, 1.0)
        self.assertEqual(self.job.output_path.read_bytes(), b"%PDF-mono")
        self.assertEqual([path.name for path in output.iterdir()], ["translated.zh-CN.pdf"])

    async def test_initialization_failure_cannot_leave_job_running(self) -> None:
        with patch.object(
            sidecar,
            "_make_settings",
            side_effect=RuntimeError("unexpected initialization failure"),
        ):
            await sidecar._translate_job(self.job)
        self.assertEqual(self.job.status, "error")
        self.assertEqual(self.job.error_code, "translation_failed")

    async def test_error_events_map_to_stable_public_codes(self) -> None:
        cases = {
            "Authentication failed with status 401": "provider_authentication_failed",
            "Too many requests, status 429": "provider_rate_limited",
            "Provider timed out": "provider_timeout",
            "Unexpected subprocess exit": "translation_failed",
        }
        for index, (message, expected) in enumerate(cases.items()):
            directory = Path(self.temp.name) / f"case-{index}"
            directory.mkdir()
            input_path = directory / "input.pdf"
            input_path.write_bytes(b"%PDF-1.4\n")
            job = sidecar.ExportJob(
                job_id=f"case-{index}",
                directory=directory,
                input_path=input_path,
                output_path=directory / "output" / "translated.zh-CN.pdf",
                page_count=1,
            )

            async def events(_settings, _path, error=message):
                yield {"type": "error", "error": error, "details": "must not leak"}

            with (
                patch.dict(os.environ, TOKEN_ENV, clear=True),
                patch.object(sidecar, "_make_settings", return_value=object()),
                patch.object(sidecar, "do_translate_async_stream", new=events),
            ):
                await sidecar._translate_job(job)
            self.assertEqual(job.status, "error")
            self.assertEqual(job.error_code, expected)
            self.assertNotIn("must not leak", job.error_message or "")

    async def test_output_validation_errors_map_to_stable_public_code(self) -> None:
        output = self.job.directory / "output"
        output.mkdir()
        mono = output / "paper-mono.pdf"
        mono.write_bytes(b"%PDF-mono")

        async def events(_settings, _path):
            yield {"type": "finish", "translate_result": SimpleNamespace(mono_pdf_path=mono)}

        with (
            patch.dict(os.environ, TOKEN_ENV, clear=True),
            patch.object(sidecar, "_make_settings", return_value=object()),
            patch.object(sidecar, "do_translate_async_stream", new=events),
            patch.object(
                sidecar,
                "_restore_safe_interactivity",
                side_effect=sidecar.OutputValidationError("unsafe action"),
            ),
        ):
            await sidecar._translate_job(self.job)
        self.assertEqual(self.job.status, "error")
        self.assertEqual(self.job.error_code, "output_validation_failed")
        self.assertEqual(
            self.job.error_message,
            "The translated PDF did not pass safety validation.",
        )
        self.assertFalse(self.job.output_path.exists())

    async def test_cancel_waits_for_translation_generator_cleanup(self) -> None:
        started = asyncio.Event()
        finalized = asyncio.Event()

        async def events(_settings, _path):
            started.set()
            try:
                while True:
                    await asyncio.sleep(10)
                    yield {"type": "progress", "progress": 0.1}
            finally:
                finalized.set()

        with (
            patch.dict(os.environ, TOKEN_ENV, clear=True),
            patch.object(sidecar, "_make_settings", return_value=object()),
            patch.object(sidecar, "do_translate_async_stream", new=events),
        ):
            task = asyncio.create_task(sidecar._translate_job(self.job))
            self.job.task = task
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.assertTrue(finalized.is_set())
        self.assertEqual(self.job.status, "cancelled")
        self.assertFalse(self.job.directory.exists())


@unittest.skipIf(sidecar.fitz is None, "PyMuPDF is only present in the pinned sidecar image")
class SidecarInteractivityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_plain_pdf(self, path: Path, pages: int = 2) -> None:
        document = sidecar.fitz.open()
        for index in range(pages):
            page = document.new_page(width=300, height=400)
            page.insert_text((30, 70), f"Page {index + 1}")
        document.save(path)
        document.close()

    def test_named_destination_is_serialized_as_internal_goto(self) -> None:
        inserted: list[dict] = []
        page = SimpleNamespace(insert_link=inserted.append)

        sidecar._insert_safe_link(
            page,
            {
                "kind": sidecar.fitz.LINK_NAMED,
                "from": (10, 20, 30, 40),
                "nameddest": "section.2",
            },
        )

        self.assertEqual(inserted[0]["kind"], sidecar.fitz.LINK_GOTO)
        self.assertEqual(inserted[0]["page"], -1)
        self.assertEqual(inserted[0]["to"], "section.2")

    def _write_interactive_pdf(self, path: Path) -> None:
        fitz = sidecar.fitz
        document = fitz.open()
        document.new_page(width=300, height=400)
        document.new_page(width=300, height=400)
        first = document[0]
        second = document[1]
        first.insert_text((30, 70), "Source page one")
        second.insert_text((30, 70), "Source page two")
        first.insert_link(
            {
                "kind": fitz.LINK_URI,
                "from": fitz.Rect(20, 20, 110, 45),
                "uri": "https://example.com/paper",
            }
        )
        first.insert_link(
            {
                "kind": fitz.LINK_GOTO,
                "from": fitz.Rect(20, 90, 110, 115),
                "page": 1,
                "to": fitz.Point(0, 60),
            }
        )
        second.insert_link(
            {
                "kind": fitz.LINK_GOTO,
                "from": fitz.Rect(20, 20, 110, 45),
                "page": 1,
                "to": fitz.Point(0, 120),
            }
        )
        note = first.add_text_annot((130, 40), "Review this claim", icon="Comment")
        note.set_info(title="Reviewer", subject="Evidence")
        note.update()
        highlight = second.add_highlight_annot(fitz.Rect(25, 55, 150, 80))
        highlight.set_info(content="Important evidence")
        highlight.update()
        free_text = second.add_freetext_annot(
            fitz.Rect(30, 130, 180, 170),
            "Replication note",
            text_color=(0, 0, 0),
            fill_color=(1, 1, 0),
        )
        free_text.set_info(title="Researcher")
        free_text.update()
        document.set_toc([[1, "Introduction", 1], [2, "Evidence", 2]])
        document.save(path)
        document.close()

    def _write_global_danger_pdf(self, path: Path, case: str) -> None:
        fitz = sidecar.fitz
        document = fitz.open()
        page = document.new_page(width=300, height=400)
        page.insert_text((30, 70), "Danger fixture")
        catalog_xref = document.pdf_catalog()
        page_xref = page.xref
        javascript_action = "<< /S /JavaScript /JS (alert) >>"

        if case == "catalog_open_action":
            document.xref_set_key(catalog_xref, "OpenAction", javascript_action)
        elif case == "catalog_additional_actions":
            document.xref_set_key(
                catalog_xref,
                "AA",
                f"<< /WC {javascript_action} >>",
            )
        elif case == "page_open_action":
            document.xref_set_key(page_xref, "OpenAction", javascript_action)
        elif case == "page_additional_actions":
            document.xref_set_key(
                page_xref,
                "AA",
                f"<< /O {javascript_action} >>",
            )
        elif case == "javascript_name_tree":
            names_xref = document.get_new_xref()
            document.update_object(
                names_xref,
                (
                    "<< /JavaScript << /Names "
                    f"[(run) {javascript_action}] >> >>"
                ),
            )
            document.xref_set_key(catalog_xref, "Names", f"{names_xref} 0 R")
        elif case == "embedded_files_name_tree":
            document.embfile_add("payload.bin", b"payload")
        elif case in {"catalog_associated_files", "page_associated_files"}:
            filespec_xref = document.get_new_xref()
            document.update_object(
                filespec_xref,
                "<< /Type /Filespec /F (payload.bin) >>",
            )
            target_xref = (
                catalog_xref if case == "catalog_associated_files" else page_xref
            )
            document.xref_set_key(target_xref, "AF", f"[{filespec_xref} 0 R]")
        elif case == "acroform":
            document.xref_set_key(catalog_xref, "AcroForm", "<< /Fields [] >>")
        elif case == "collection":
            document.xref_set_key(catalog_xref, "Collection", "<< /Type /Collection >>")
        elif case == "renditions_name_tree":
            document.xref_set_key(
                catalog_xref,
                "Names",
                "<< /Renditions << /Names [] >> >>",
            )
        elif case == "alternate_presentations_name_tree":
            document.xref_set_key(
                catalog_xref,
                "Names",
                "<< /AlternatePresentations << /Names [] >> >>",
            )
        elif case == "page_presentation_steps":
            document.xref_set_key(page_xref, "PresSteps", "<< /Type /NavNode >>")
        else:
            self.fail(f"unknown danger fixture: {case}")

        document.save(path)
        document.close()

    def test_safe_links_and_common_annotations_are_restored(self) -> None:
        source = self.root / "source.pdf"
        output = self.root / "output.pdf"
        self._write_interactive_pdf(source)
        self._write_plain_pdf(output)

        sidecar._restore_safe_interactivity(source, output)

        with sidecar.fitz.open(output) as document:
            self.assertEqual(document.page_count, 2)
            first_links = document[0].get_links()
            second_links = document[1].get_links()
            self.assertEqual(len(first_links), 2)
            self.assertEqual(len(second_links), 1)
            internal_targets = [
                link["page"]
                for link in first_links + second_links
                if link["kind"] == sidecar.fitz.LINK_GOTO
            ]
            self.assertEqual(internal_targets, [1, 1])
            self.assertEqual(
                [link["uri"] for link in first_links if link["kind"] == sidecar.fitz.LINK_URI],
                ["https://example.com/paper"],
            )
            annotations = [
                annotation
                for page_index in range(document.page_count)
                for annotation in document[page_index].annots()
            ]
            self.assertEqual(len(annotations), 3)
            self.assertEqual(
                {annotation.info.get("content") for annotation in annotations},
                {"Review this claim", "Important evidence", "Replication note"},
            )
            self.assertEqual(
                document.get_toc(),
                [[1, "Introduction", 1], [2, "Evidence", 2]],
            )
        self.assertEqual(sidecar._validate_pdf(output), 2)

    def test_plain_pdf_passes_global_active_content_scan(self) -> None:
        source = self.root / "plain-source.pdf"
        output = self.root / "plain-output.pdf"
        self._write_plain_pdf(source, pages=1)
        self._write_plain_pdf(output, pages=1)

        sidecar._restore_safe_interactivity(source, output)

        self.assertEqual(sidecar._validate_pdf(output), 1)

    def test_job_upload_rejects_global_active_source_before_translation(self) -> None:
        source = self.root / "active-upload.pdf"
        work_root = self.root / "jobs"
        self._write_global_danger_pdf(source, "catalog_open_action")

        with (
            TestClient(sidecar.app) as client,
            patch.dict(os.environ, TOKEN_ENV, clear=True),
            patch.object(sidecar, "WORK_ROOT", work_root),
        ):
            response = client.post(
                "/jobs",
                headers=AUTH,
                data={"job_id": "active-upload"},
                files={"file": ("paper.pdf", source.read_bytes(), "application/pdf")},
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"],
            "Uploaded PDF contains unsupported active content",
        )
        self.assertFalse((work_root / "active-upload").exists())
        self.assertNotIn("active-upload", sidecar._jobs)

    def test_source_and_output_global_active_content_fail_closed(self) -> None:
        cases = (
            "catalog_open_action",
            "catalog_additional_actions",
            "page_open_action",
            "page_additional_actions",
            "javascript_name_tree",
            "embedded_files_name_tree",
            "catalog_associated_files",
            "page_associated_files",
            "acroform",
            "collection",
            "renditions_name_tree",
            "alternate_presentations_name_tree",
            "page_presentation_steps",
        )
        for case in cases:
            for dangerous_side in ("source", "output"):
                with self.subTest(case=case, dangerous_side=dangerous_side):
                    source = self.root / f"{case}-{dangerous_side}-source.pdf"
                    output = self.root / f"{case}-{dangerous_side}-output.pdf"
                    self._write_plain_pdf(source, pages=1)
                    self._write_plain_pdf(output, pages=1)
                    dangerous_path = source if dangerous_side == "source" else output
                    dangerous_path.unlink()
                    self._write_global_danger_pdf(dangerous_path, case)

                    with self.assertRaises(sidecar.OutputValidationError):
                        sidecar._restore_safe_interactivity(source, output)

    def test_global_object_parse_failure_fails_closed(self) -> None:
        broken_document = SimpleNamespace(
            pdf_catalog=lambda: 1,
            xref_get_keys=lambda _xref: (_ for _ in ()).throw(
                RuntimeError("broken xref object")
            ),
        )

        with self.assertRaises(sidecar.OutputValidationError) as raised:
            sidecar._reject_unsafe_global_entries(broken_document)

        self.assertIsInstance(raised.exception.__cause__, RuntimeError)

    def test_unsafe_actions_and_unsupported_annotations_fail_closed(self) -> None:
        fitz = sidecar.fitz
        cases = (
            "javascript_uri",
            "launch",
            "file_attachment",
            "stamp",
            "widget",
            "external_toc",
        )
        for case in cases:
            with self.subTest(case=case):
                source = self.root / f"{case}-source.pdf"
                output = self.root / f"{case}-output.pdf"
                document = fitz.open()
                page = document.new_page(width=300, height=400)
                if case == "javascript_uri":
                    page.insert_link(
                        {
                            "kind": fitz.LINK_URI,
                            "from": fitz.Rect(20, 20, 100, 40),
                            "uri": "javascript:alert(1)",
                        }
                    )
                elif case == "launch":
                    page.insert_link(
                        {
                            "kind": fitz.LINK_LAUNCH,
                            "from": fitz.Rect(20, 20, 100, 40),
                            "file": "/tmp/unsafe",
                        }
                    )
                elif case == "file_attachment":
                    page.add_file_annot((40, 40), b"payload", "unsafe.bin")
                elif case == "external_toc":
                    document.set_toc(
                        [
                            [
                                1,
                                "External",
                                -1,
                                {"kind": fitz.LINK_URI, "uri": "https://example.com"},
                            ]
                        ]
                    )
                elif case == "widget":
                    widget = fitz.Widget()
                    widget.field_name = "unsafe-form"
                    widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
                    widget.rect = fitz.Rect(20, 20, 150, 45)
                    page.add_widget(widget)
                else:
                    page.add_stamp_annot(fitz.Rect(20, 20, 100, 60))
                document.save(source)
                document.close()
                self._write_plain_pdf(output, pages=1)

                with self.assertRaises(sidecar.OutputValidationError):
                    sidecar._restore_safe_interactivity(source, output)

    def test_normalization_preserves_source_image_digest(self) -> None:
        fitz = sidecar.fitz
        source = self.root / "image-source.pdf"
        output = self.root / "image-output.pdf"
        pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8), False)
        pixmap.clear_with(0x336699)
        image_bytes = pixmap.tobytes("png")
        for path in (source, output):
            document = fitz.open()
            page = document.new_page(width=300, height=400)
            page.insert_image(fitz.Rect(40, 50, 140, 150), stream=image_bytes)
            document.save(path)
            document.close()

        sidecar._restore_safe_interactivity(source, output)

        with fitz.open(source) as source_document, fitz.open(output) as output_document:
            source_images = sidecar._image_signatures(source_document)
            output_images = sidecar._image_signatures(output_document)
            self.assertEqual(source_images, output_images)

    def test_normalization_repaints_source_figure_covered_by_output(self) -> None:
        fitz = sidecar.fitz
        source = self.root / "covered-image-source.pdf"
        output = self.root / "covered-image-output.pdf"
        image_rect = fitz.Rect(40, 50, 140, 150)
        pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8), False)
        pixmap.clear_with(0x336699)
        image_bytes = pixmap.tobytes("png")

        document = fitz.open()
        page = document.new_page(width=300, height=400)
        page.insert_image(image_rect, stream=image_bytes)
        document.save(source)
        document.close()

        document = fitz.open()
        page = document.new_page(width=300, height=400)
        page.insert_image(image_rect, stream=image_bytes)
        page.draw_rect(image_rect, color=(1, 1, 1), fill=(1, 1, 1), overlay=True)
        document.save(output)
        document.close()

        with fitz.open(source) as source_document, fitz.open(output) as output_document:
            self.assertNotEqual(
                sidecar._rendered_clip_digest(source_document[0], tuple(image_rect)),
                sidecar._rendered_clip_digest(output_document[0], tuple(image_rect)),
            )

        sidecar._restore_safe_interactivity(source, output)

        with fitz.open(source) as source_document, fitz.open(output) as output_document:
            self.assertEqual(
                sidecar._rendered_clip_digest(source_document[0], tuple(image_rect)),
                sidecar._rendered_clip_digest(output_document[0], tuple(image_rect)),
            )

    def test_clean_save_failure_never_publishes_output(self) -> None:
        source = self.root / "job" / "input.pdf"
        mono = self.root / "job" / "output" / "upstream-mono.pdf"
        source.parent.mkdir()
        mono.parent.mkdir()
        self._write_plain_pdf(source, pages=1)
        self._write_plain_pdf(mono, pages=1)
        job = sidecar.ExportJob(
            job_id="clean-save-failure",
            directory=source.parent,
            input_path=source,
            output_path=mono.parent / "translated.zh-CN.pdf",
            page_count=1,
        )

        with patch.object(sidecar.fitz.Document, "save", side_effect=RuntimeError("save failed")):
            with self.assertRaises(sidecar.OutputValidationError):
                sidecar._retain_mono_pdf(job, mono)

        self.assertFalse(job.output_path.exists())
        self.assertFalse(job.output_path.with_name(f".{job.output_path.name}.pending").exists())


if __name__ == "__main__":
    unittest.main()
