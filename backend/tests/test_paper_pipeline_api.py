from __future__ import annotations

import asyncio
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.agent.schemas import AnalysisResult, Evidence, ReproducibilityReport
from backend.api import routes_analyze, routes_papers, routes_translate
from backend.extraction.blocks import Block, PaperDocument
from backend.extraction.local_pdf import LocalPdfExtractionError
from backend.extraction.mineru import (
    MINERU_LAYOUT_ADAPTER,
    MINERU_LAYOUT_ADAPTER_VERSION,
    MinerUError,
    MinerUStructuredResult,
)
from backend.extraction.pdf_layout import (
    POPPLER_LAYOUT_ADAPTER,
    POPPLER_LAYOUT_ADAPTER_VERSION,
)
from backend.extraction.pdf_mapping import PDF_MAPPING_VERSION
from backend.extraction.translation_layout import (
    HYBRID_LAYOUT_ADAPTER,
    HYBRID_LAYOUT_ADAPTER_VERSION,
    MINERU_TRANSLATION_LAYOUT_ADAPTER_VERSION,
    NormalizedBox,
    TranslationLayout,
    TranslationLayoutPage,
    TranslationLayoutQuality,
    TranslationLayoutRegion,
    TranslationLayoutSource,
    bind_mineru_layout_source,
    block_source_sha256,
    source_pdf_sha256,
    translation_layout_cache_key,
)
from backend.storage import files as storage_files
from backend.storage.files import load_document, save_document


class PaperPipelineApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        app = FastAPI()
        app.include_router(routes_papers.router)
        app.include_router(routes_translate.router)
        app.include_router(routes_analyze.router)
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()

    def test_create_paper_persists_document_and_extraction_quality(self) -> None:
        blocks = self._blocks()
        meta = {
            "arxiv_id": "9999.00001",
            "title": "Regression Paper",
            "authors": ["Ada"],
            "source": "ar5iv",
            "status": "extracted",
            "created_at": "2026-07-16T00:00:00Z",
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(storage_files, "PAPERS_DIR", Path(tmp) / "papers"),
            patch.object(routes_papers, "extract_paper", new=AsyncMock(return_value=(blocks, "ar5iv"))),
            patch.object(routes_papers, "get_paper", new=AsyncMock(side_effect=[None, meta])),
            patch.object(routes_papers, "insert_paper", new=AsyncMock()),
            patch.object(routes_papers, "ensure_pdf", new=AsyncMock()) as ensure_pdf,
            patch.object(
                routes_papers,
                "_warm_translation_layout",
                new=AsyncMock(),
            ) as warm_layout,
        ):
            pdf_path = Path(tmp) / "papers" / "9999.00001" / "original.pdf"
            pdf_path.parent.mkdir(parents=True)
            pdf_path.write_bytes(b"%PDF-test")
            ensure_pdf.return_value = pdf_path
            response = self.client.post(
                "/papers",
                json={"arxiv_id": "9999.00001", "title": "Regression Paper", "authors": ["Ada"]},
            )
            paper_dir = Path(tmp) / "papers" / "9999.00001"

            self.assertEqual(response.status_code, 200)
            self.assertTrue((paper_dir / "translation.json").is_file())
            self.assertTrue((paper_dir / "extraction_quality.json").is_file())
            self.assertTrue(storage_files.load_extraction_quality("9999.00001")["acceptable"])
            ensure_pdf.assert_awaited_once_with("9999.00001")
            warm_layout.assert_awaited_once()

    def test_pdf_map_generates_caches_and_returns_deterministic_error(self) -> None:
        mapping = {
            "mapping_version": PDF_MAPPING_VERSION,
            "pdf_url": "/assets/9999.00002/original.pdf",
            "page_count": 2,
            "mappable_count": 2,
            "mapping_count": 2,
            "unmapped_count": 0,
            "mapped_ratio": 1.0,
            "average_confidence": 0.95,
            "low_confidence_count": 0,
            "mappings": [],
        }
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            storage_files, "PAPERS_DIR", Path(tmp) / "papers"
        ):
            save_document(self._document("9999.00002"))
            fake_pdf = Path(tmp) / "original.pdf"
            fake_pdf.write_bytes(b"%PDF-test")
            with (
                patch.object(
                    routes_papers,
                    "_pdf_path_for_document",
                    new=AsyncMock(return_value=fake_pdf),
                ),
                patch.object(routes_papers, "build_block_pdf_map", return_value=mapping) as build,
            ):
                first = self.client.get("/papers/9999.00002/pdf-map")
                second = self.client.get("/papers/9999.00002/pdf-map")

            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 200)
            build.assert_called_once()
            self.assertTrue((Path(tmp) / "papers" / "9999.00002" / "block_to_pdf_map.json").is_file())

            (Path(tmp) / "papers" / "9999.00002" / "block_to_pdf_map.json").unlink()
            with (
                patch.object(
                    routes_papers,
                    "_pdf_path_for_document",
                    new=AsyncMock(return_value=fake_pdf),
                ),
                patch.object(routes_papers, "build_block_pdf_map", side_effect=RuntimeError("map boom")),
            ):
                failed = self.client.get("/papers/9999.00002/pdf-map")

            self.assertEqual(failed.status_code, 500)
            self.assertEqual(failed.json()["detail"], "PDF 映射生成失败: map boom")

    def test_translation_layout_accepts_poppler_thresholds_caches_and_ignores_translation_updates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            storage_files, "PAPERS_DIR", Path(tmp) / "papers"
        ):
            document = self._document("9999.00005")
            save_document(document)
            fake_pdf = Path(tmp) / "original.pdf"
            fake_pdf.write_bytes(b"%PDF-layout-api")
            layout = self._translation_layout(
                document,
                fake_pdf,
                mapped_ratio=0.90,
                average_confidence=0.90,
            )
            with (
                patch.object(
                    routes_papers,
                    "_pdf_path_for_document",
                    new=AsyncMock(return_value=fake_pdf),
                ),
                patch.object(routes_papers, "extract_pdf_layout", return_value=object()) as extract,
                patch.object(
                    routes_papers,
                    "translation_layout_from_pdf_layout",
                    return_value=layout,
                ) as build,
                patch.object(
                    routes_papers,
                    "_parse_pdf_with_standard_mineru",
                    new=AsyncMock(side_effect=AssertionError("MinerU must not run")),
                ) as mineru,
            ):
                first = self.client.get("/papers/9999.00005/translation-layout")
                second = self.client.get("/papers/9999.00005/translation-layout")

                document.blocks[0].translation = "第一段。"
                document.blocks[0].status = "done"
                save_document(document)
                after_translation = self.client.get("/papers/9999.00005/translation-layout")

            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 200)
            self.assertEqual(after_translation.status_code, 200)
            self.assertEqual(first.json()["cache_key"], second.json()["cache_key"])
            self.assertEqual(first.json()["cache_key"], after_translation.json()["cache_key"])
            self.assertEqual(first.json()["quality"]["mapped_ratio"], 0.90)
            self.assertEqual(first.json()["quality"]["average_confidence"], 0.90)
            self.assertEqual(extract.call_count, 1)
            self.assertEqual(build.call_count, 1)
            mineru.assert_not_awaited()
            self.assertTrue(
                (Path(tmp) / "papers" / "9999.00005" / "translation_layout.json").is_file()
            )

    def test_translation_layout_read_only_requires_current_precise_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            storage_files, "PAPERS_DIR", Path(tmp) / "papers"
        ):
            document = self._document("9999.00055")
            save_document(document)
            paper_path = storage_files.ensure_paper_dir(document.paper_id)
            pdf_path = paper_path / "original.pdf"
            pdf_path.write_bytes(b"%PDF-layout-read-only")
            layout = self._translation_layout(document, pdf_path)
            storage_files.save_translation_layout(
                document.paper_id,
                layout.model_dump(mode="json"),
            )
            cache_path = paper_path / "translation_layout.json"
            before = cache_path.read_bytes()

            with (
                patch.object(
                    routes_papers,
                    "extract_pdf_layout",
                    side_effect=AssertionError("read-only must not parse"),
                ),
                patch.object(
                    routes_papers,
                    "_parse_pdf_with_standard_mineru",
                    new=AsyncMock(side_effect=AssertionError("read-only must not call MinerU")),
                ),
            ):
                cached = self.client.get(
                    "/papers/9999.00055/translation-layout?build=false"
                )
                after_read = cache_path.read_bytes()
                cache_path.unlink()
                missing = self.client.get(
                    "/papers/9999.00055/translation-layout?build=false"
                )

            self.assertEqual(cached.status_code, 200)
            self.assertEqual(cached.json()["cache_key"], layout.cache_key)
            self.assertEqual(
                cached.headers["X-Pet-Layout-Source-Class"],
                "arxiv_digital",
            )
            self.assertEqual(after_read, before)
            self.assertEqual(missing.status_code, 409)
            self.assertEqual(
                missing.json()["detail"]["code"],
                "translation_layout_cache_missing",
            )

    def test_translation_layout_rejects_v2_read_only_and_rebuilds_v3(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            storage_files, "PAPERS_DIR", Path(tmp) / "papers"
        ):
            document = self._document("9999.00056")
            save_document(document)
            paper_path = storage_files.ensure_paper_dir(document.paper_id)
            pdf_path = paper_path / "original.pdf"
            pdf_path.write_bytes(b"%PDF-layout-v2-cache")
            stale_layout = self._translation_layout(
                document,
                pdf_path,
                adapter_version="2",
            )
            current_layout = self._translation_layout(document, pdf_path)
            storage_files.save_translation_layout(
                document.paper_id,
                stale_layout.model_dump(mode="json"),
            )
            cache_path = paper_path / "translation_layout.json"
            before = cache_path.read_bytes()

            with (
                patch.object(
                    routes_papers,
                    "_pdf_path_for_document",
                    new=AsyncMock(return_value=pdf_path),
                ),
                patch.object(
                    routes_papers,
                    "extract_pdf_layout",
                    return_value=object(),
                ) as extract,
                patch.object(
                    routes_papers,
                    "translation_layout_from_pdf_layout",
                    return_value=current_layout,
                ),
                patch.object(
                    routes_papers,
                    "_parse_pdf_with_standard_mineru",
                    new=AsyncMock(side_effect=AssertionError("MinerU must not run")),
                ) as mineru,
            ):
                read_only = self.client.get(
                    "/papers/9999.00056/translation-layout?build=false"
                )
                after_read = cache_path.read_bytes()
                rebuilt = self.client.get("/papers/9999.00056/translation-layout")

            self.assertEqual(read_only.status_code, 409)
            self.assertEqual(
                read_only.json()["detail"]["code"],
                "translation_layout_cache_missing",
            )
            self.assertEqual(after_read, before)
            self.assertEqual(rebuilt.status_code, 200)
            self.assertEqual(
                rebuilt.json()["adapter_version"],
                POPPLER_LAYOUT_ADAPTER_VERSION,
            )
            persisted = storage_files.load_translation_layout(document.paper_id)
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(
                persisted["adapter_version"],
                POPPLER_LAYOUT_ADAPTER_VERSION,
            )
            extract.assert_called_once()
            mineru.assert_not_awaited()

    def test_translation_layout_read_only_reports_trusted_scan_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            storage_files, "PAPERS_DIR", Path(tmp) / "papers"
        ):
            document = self._document("local-scan-provenance")
            document.source = "mineru"
            save_document(document)
            paper_path = storage_files.ensure_paper_dir(document.paper_id)
            pdf_path = paper_path / "original.pdf"
            pdf_path.write_bytes(b"%PDF-layout-scan-provenance")
            layout = self._translation_layout(
                document,
                pdf_path,
                adapter=MINERU_LAYOUT_ADAPTER,
                adapter_version=MINERU_TRANSLATION_LAYOUT_ADAPTER_VERSION,
            )
            generation = routes_papers._save_mineru_result(
                document.paper_id,
                self._mineru_result(document.blocks),
                pdf_path,
                is_ocr=True,
            )
            layout = bind_mineru_layout_source(
                layout,
                generation=generation,
                is_ocr=True,
            )
            storage_files.save_translation_layout(
                document.paper_id,
                layout.model_dump(mode="json"),
            )

            response = self.client.get(
                "/papers/local-scan-provenance/translation-layout?build=false"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["X-Pet-Layout-Source-Class"],
            "scan_ocr",
        )

    def test_v1_mineru_layout_reuses_and_republishes_legacy_raw_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            storage_files, "PAPERS_DIR", Path(tmp) / "papers"
        ):
            document = self._document("local-mineru-v1-cache")
            document.source = "mineru"
            save_document(document)
            paper_path = storage_files.ensure_paper_dir(document.paper_id)
            pdf_path = paper_path / "original.pdf"
            pdf_path.write_bytes(b"%PDF-mineru-v1-cache")
            mineru_result = self._mineru_result(document.blocks)
            storage_files._write_json(
                paper_path / "mineru_middle.json",
                mineru_result.layout,
            )
            storage_files._write_json(
                paper_path / "mineru_content_list.json",
                mineru_result.content_list,
            )
            storage_files._write_json(
                paper_path / "mineru_layout_meta.json",
                {
                    "adapter": MINERU_LAYOUT_ADAPTER,
                    "adapter_version": MINERU_LAYOUT_ADAPTER_VERSION,
                    "source_pdf_sha256": source_pdf_sha256(pdf_path),
                    "is_ocr": False,
                },
            )
            stale_layout = self._translation_layout(
                document,
                pdf_path,
                adapter=MINERU_LAYOUT_ADAPTER,
                adapter_version="1",
            )
            storage_files.save_translation_layout(
                document.paper_id,
                stale_layout.model_dump(mode="json"),
            )
            low_poppler = self._translation_layout(
                document,
                pdf_path,
                mapped_ratio=0.5,
                average_confidence=0.95,
            )
            low_poppler.quality.replaceable_count = 0
            current_mineru = self._translation_layout(
                document,
                pdf_path,
                adapter=MINERU_LAYOUT_ADAPTER,
                adapter_version=MINERU_TRANSLATION_LAYOUT_ADAPTER_VERSION,
            )

            with (
                patch.object(
                    routes_papers,
                    "_pdf_path_for_document",
                    new=AsyncMock(return_value=pdf_path),
                ),
                patch.object(routes_papers, "extract_pdf_layout", return_value=object()),
                patch.object(
                    routes_papers,
                    "translation_layout_from_pdf_layout",
                    return_value=low_poppler,
                ),
                patch.object(
                    routes_papers,
                    "translation_layout_from_mineru",
                    return_value=current_mineru,
                ) as convert,
                patch.object(
                    routes_papers,
                    "_parse_pdf_with_standard_mineru",
                    new=AsyncMock(
                        side_effect=AssertionError("stored raw generation must be reused")
                    ),
                ) as parse,
            ):
                response = self.client.get(
                    "/papers/local-mineru-v1-cache/translation-layout"
                )

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["adapter"], MINERU_LAYOUT_ADAPTER)
            self.assertEqual(
                payload["adapter_version"],
                MINERU_TRANSLATION_LAYOUT_ADAPTER_VERSION,
            )
            generation = payload["sources"][0]["generation"]
            self.assertRegex(generation, r"^[0-9a-f]{32}$")
            self.assertFalse(payload["sources"][0]["is_ocr"])
            provenance = storage_files.load_mineru_layout_provenance(
                document.paper_id,
                expected_source_pdf_sha256=source_pdf_sha256(pdf_path),
            )
            self.assertIsNotNone(provenance)
            assert provenance is not None
            self.assertEqual(provenance["generation"], generation)
            parse.assert_not_awaited()
            convert.assert_called_once()
            reused = convert.call_args.args[2]
            self.assertEqual(reused.layout, mineru_result.layout)
            self.assertEqual(reused.content_list, mineru_result.content_list)

    def test_read_only_hybrid_rejects_a_stale_mineru_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            storage_files, "PAPERS_DIR", Path(tmp) / "papers"
        ):
            document = self._document("local-hybrid-generation-mismatch")
            document.source = "mineru"
            save_document(document)
            paper_path = storage_files.ensure_paper_dir(document.paper_id)
            pdf_path = paper_path / "original.pdf"
            pdf_path.write_bytes(b"%PDF-hybrid-generation-mismatch")
            actual_generation = routes_papers._save_mineru_result(
                document.paper_id,
                self._mineru_result(document.blocks),
                pdf_path,
                is_ocr=False,
            )
            self.assertIsNotNone(actual_generation)
            mismatch = "0" * 32 if actual_generation != "0" * 32 else "1" * 32
            sources = [
                TranslationLayoutSource(
                    adapter=POPPLER_LAYOUT_ADAPTER,
                    adapter_version=POPPLER_LAYOUT_ADAPTER_VERSION,
                ),
                TranslationLayoutSource(
                    adapter=MINERU_LAYOUT_ADAPTER,
                    adapter_version=MINERU_TRANSLATION_LAYOUT_ADAPTER_VERSION,
                    generation=mismatch,
                    is_ocr=False,
                ),
            ]
            layout = self._translation_layout(
                document,
                pdf_path,
                adapter=HYBRID_LAYOUT_ADAPTER,
                adapter_version=HYBRID_LAYOUT_ADAPTER_VERSION,
                sources=sources,
            )
            storage_files.save_translation_layout(
                document.paper_id,
                layout.model_dump(mode="json"),
            )
            cache_path = paper_path / "translation_layout.json"
            before = cache_path.read_bytes()

            response = self.client.get(
                "/papers/local-hybrid-generation-mismatch/translation-layout?build=false"
            )

            self.assertEqual(response.status_code, 409)
            self.assertEqual(
                response.json()["detail"]["code"],
                "translation_layout_cache_missing",
            )
            self.assertIn("generation", response.json()["detail"]["message"])
            self.assertEqual(cache_path.read_bytes(), before)

    def test_translation_layout_rebuild_requires_admin_and_forces_precise_layout(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(storage_files, "PAPERS_DIR", Path(tmp) / "papers"),
            patch.dict("os.environ", {"PEINIDU_ADMIN_TOKEN": "layout-admin"}),
        ):
            document = self._document("9999.00006")
            save_document(document)
            fake_pdf = Path(tmp) / "original.pdf"
            fake_pdf.write_bytes(b"%PDF-layout-rebuild")
            layout = self._translation_layout(document, fake_pdf)
            with (
                patch.object(
                    routes_papers,
                    "_pdf_path_for_document",
                    new=AsyncMock(return_value=fake_pdf),
                ),
                patch.object(routes_papers, "extract_pdf_layout", return_value=object()) as extract,
                patch.object(
                    routes_papers,
                    "translation_layout_from_pdf_layout",
                    return_value=layout,
                ),
                patch.object(
                    routes_papers,
                    "_parse_pdf_with_standard_mineru",
                    new=AsyncMock(side_effect=AssertionError("MinerU must not run")),
                ) as mineru,
            ):
                denied = self.client.post("/papers/9999.00006/translation-layout/rebuild")
                allowed = self.client.post(
                    "/papers/9999.00006/translation-layout/rebuild",
                    headers={"X-Peinidu-Admin-Token": "layout-admin"},
                )
                repeated = self.client.post(
                    "/papers/9999.00006/translation-layout/rebuild",
                    headers={"X-Peinidu-Admin-Token": "layout-admin"},
                )

            self.assertEqual(denied.status_code, 401)
            self.assertEqual(allowed.status_code, 200)
            self.assertEqual(repeated.status_code, 200)
            self.assertEqual(extract.call_count, 2)
            mineru.assert_not_awaited()

    def test_translation_layout_build_survives_request_cancellation_as_single_flight(
        self,
    ) -> None:
        document = self._document("9999.00061")
        started = threading.Event()
        release = threading.Event()
        calls = 0

        with tempfile.TemporaryDirectory() as tmp:
            fake_pdf = Path(tmp) / "original.pdf"
            fake_pdf.write_bytes(b"%PDF-layout-cancellation")
            layout = self._translation_layout(document, fake_pdf)

            def blocking_extract(_path: Path) -> object:
                nonlocal calls
                calls += 1
                started.set()
                if not release.wait(timeout=2):
                    raise RuntimeError("test release timed out")
                return object()

            async def exercise() -> TranslationLayout:
                first = asyncio.create_task(
                    routes_papers._build_or_load_translation_layout(
                        document.paper_id,
                        document,
                    )
                )
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first

                second = asyncio.create_task(
                    routes_papers._build_or_load_translation_layout(
                        document.paper_id,
                        document,
                    )
                )
                await asyncio.sleep(0.02)
                self.assertEqual(calls, 1)
                release.set()
                return await second

            try:
                with (
                    patch.object(
                        routes_papers,
                        "_pdf_path_for_document",
                        new=AsyncMock(return_value=fake_pdf),
                    ),
                    patch.object(
                        routes_papers,
                        "load_translation_layout",
                        return_value=None,
                    ),
                    patch.object(
                        routes_papers,
                        "extract_pdf_layout",
                        side_effect=blocking_extract,
                    ),
                    patch.object(
                        routes_papers,
                        "translation_layout_from_pdf_layout",
                        return_value=layout,
                    ),
                    patch.object(routes_papers, "save_translation_layout"),
                    patch.object(
                        routes_papers,
                        "_parse_pdf_with_standard_mineru",
                        new=AsyncMock(side_effect=AssertionError("MinerU must not run")),
                    ),
                ):
                    result = asyncio.run(exercise())
            finally:
                release.set()

        self.assertEqual(result.cache_key, layout.cache_key)
        self.assertEqual(calls, 1)

    def test_translation_layout_marks_partial_source_document(self) -> None:
        document = self._document("9999.00062")
        document.source_page_range = "2-4"
        with tempfile.TemporaryDirectory() as tmp:
            fake_pdf = Path(tmp) / "original.pdf"
            fake_pdf.write_bytes(b"%PDF-partial-source")
            layout = self._translation_layout(document, fake_pdf)
            with (
                patch.object(
                    routes_papers,
                    "_build_or_load_translation_layout_unlocked",
                    new=AsyncMock(return_value=layout),
                ),
                patch.object(routes_papers, "save_translation_layout") as save_layout,
            ):
                result = asyncio.run(
                    routes_papers._run_translation_layout_build(
                        document.paper_id,
                        document,
                    )
                )

        self.assertIn("partial_source_document", result.warnings)
        self.assertEqual(
            save_layout.call_args.args[1]["warnings"],
            ["partial_source_document"],
        )

    def test_invalid_new_mineru_result_is_not_published_before_layout_validation(
        self,
    ) -> None:
        document = self._document("9999.00063")
        with tempfile.TemporaryDirectory() as tmp:
            fake_pdf = Path(tmp) / "original.pdf"
            fake_pdf.write_bytes(b"%PDF-invalid-mineru")
            low_layout = self._translation_layout(
                document,
                fake_pdf,
                mapped_ratio=0,
                average_confidence=0,
            )
            result = self._mineru_result(document.blocks)
            with (
                patch.object(
                    routes_papers,
                    "_pdf_path_for_document",
                    new=AsyncMock(return_value=fake_pdf),
                ),
                patch.object(routes_papers, "load_translation_layout", return_value=None),
                patch.object(routes_papers, "extract_pdf_layout", return_value=object()),
                patch.object(
                    routes_papers,
                    "translation_layout_from_pdf_layout",
                    return_value=low_layout,
                ),
                patch.object(
                    routes_papers,
                    "_stored_mineru_result",
                    return_value=(None, None),
                ),
                patch.object(
                    routes_papers,
                    "_parse_pdf_with_standard_mineru",
                    new=AsyncMock(return_value=result),
                ),
                patch.object(
                    routes_papers,
                    "translation_layout_from_mineru",
                    side_effect=ValueError("mineru_page_count_mismatch"),
                ),
                patch.object(routes_papers, "_save_mineru_result") as save_result,
            ):
                with self.assertRaises(HTTPException):
                    asyncio.run(
                        routes_papers._build_or_load_translation_layout(
                            document.paper_id,
                            document,
                        )
                    )

        save_result.assert_not_called()

    def test_translation_layout_upgrades_a_valid_legacy_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            storage_files, "PAPERS_DIR", Path(tmp) / "papers"
        ):
            document = self._document("9999.00007")
            save_document(document)
            fake_pdf = Path(tmp) / "original.pdf"
            fake_pdf.write_bytes(b"%PDF-layout-upgrade")
            legacy_layout = self._translation_layout(
                document,
                fake_pdf,
                adapter="legacy_pdf_map",
                adapter_version=str(PDF_MAPPING_VERSION),
            )
            precise_layout = self._translation_layout(document, fake_pdf)
            storage_files.save_translation_layout(
                document.paper_id,
                legacy_layout.model_dump(mode="json"),
            )
            with (
                patch.object(
                    routes_papers,
                    "_pdf_path_for_document",
                    new=AsyncMock(return_value=fake_pdf),
                ),
                patch.object(routes_papers, "extract_pdf_layout", return_value=object()) as extract,
                patch.object(
                    routes_papers,
                    "translation_layout_from_pdf_layout",
                    return_value=precise_layout,
                ),
                patch.object(
                    routes_papers,
                    "_parse_pdf_with_standard_mineru",
                    new=AsyncMock(side_effect=AssertionError("MinerU must not run")),
                ) as mineru,
            ):
                response = self.client.get(
                    "/papers/9999.00007/translation-layout"
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["adapter"], POPPLER_LAYOUT_ADAPTER)
            extract.assert_called_once()
            mineru.assert_not_awaited()

    def test_translation_layout_single_flight_avoids_duplicate_adapter_work(self) -> None:
        async def run(document: PaperDocument, fake_pdf: Path) -> list[TranslationLayout]:
            return await asyncio.gather(
                routes_papers._build_or_load_translation_layout(document.paper_id, document),
                routes_papers._build_or_load_translation_layout(document.paper_id, document),
            )

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            storage_files, "PAPERS_DIR", Path(tmp) / "papers"
        ):
            document = self._document("9999.00009")
            save_document(document)
            fake_pdf = Path(tmp) / "original.pdf"
            fake_pdf.write_bytes(b"%PDF-layout-single-flight")
            precise_layout = self._translation_layout(document, fake_pdf)
            with (
                patch.object(
                    routes_papers,
                    "_pdf_path_for_document",
                    new=AsyncMock(return_value=fake_pdf),
                ),
                patch.object(routes_papers, "extract_pdf_layout", return_value=object()) as extract,
                patch.object(
                    routes_papers,
                    "translation_layout_from_pdf_layout",
                    return_value=precise_layout,
                ),
                patch.object(
                    routes_papers,
                    "_parse_pdf_with_standard_mineru",
                    new=AsyncMock(side_effect=AssertionError("MinerU must not run")),
                ),
            ):
                results = asyncio.run(run(document, fake_pdf))

        self.assertEqual([item.cache_key for item in results], [
            precise_layout.cache_key,
            precise_layout.cache_key,
        ])
        extract.assert_called_once()

    def test_translation_layout_single_flight_reuses_concurrent_failure(self) -> None:
        async def mineru_offline(*_args, **_kwargs):
            await asyncio.sleep(0.02)
            raise MinerUError("provider offline")

        async def run(document: PaperDocument) -> list[object]:
            return await asyncio.gather(
                routes_papers._build_or_load_translation_layout(document.paper_id, document),
                routes_papers._build_or_load_translation_layout(document.paper_id, document),
                return_exceptions=True,
            )

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            storage_files, "PAPERS_DIR", Path(tmp) / "papers"
        ):
            document = self._document("9999.00010")
            save_document(document)
            fake_pdf = Path(tmp) / "original.pdf"
            fake_pdf.write_bytes(b"%PDF-layout-single-flight-failure")
            low_layout = self._translation_layout(
                document,
                fake_pdf,
                mapped_ratio=0.89,
                average_confidence=0.95,
            )
            with (
                patch.object(
                    routes_papers,
                    "_pdf_path_for_document",
                    new=AsyncMock(return_value=fake_pdf),
                ),
                patch.object(routes_papers, "extract_pdf_layout", return_value=object()),
                patch.object(
                    routes_papers,
                    "translation_layout_from_pdf_layout",
                    return_value=low_layout,
                ),
                patch.object(
                    routes_papers,
                    "_parse_pdf_with_standard_mineru",
                    new=AsyncMock(side_effect=mineru_offline),
                ) as mineru,
            ):
                results = asyncio.run(run(document))

        self.assertEqual(len(results), 2)
        self.assertTrue(all(isinstance(item, HTTPException) for item in results))
        self.assertTrue(
            all(getattr(item, "detail", {}).get("code") == "layout_unavailable" for item in results)
        )
        mineru.assert_awaited_once()

    def test_translation_layout_falls_back_when_either_poppler_threshold_is_low(self) -> None:
        for suffix, mapped_ratio, average_confidence in (
            ("ratio", 0.899, 0.95),
            ("confidence", 0.95, 0.899),
        ):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as tmp, patch.object(
                storage_files, "PAPERS_DIR", Path(tmp) / "papers"
            ):
                paper_id = f"9999.1000{1 if suffix == 'ratio' else 2}"
                document = self._document(paper_id)
                save_document(document)
                fake_pdf = Path(tmp) / "original.pdf"
                fake_pdf.write_bytes(f"%PDF-layout-low-{suffix}".encode())
                poppler_layout = self._translation_layout(
                    document,
                    fake_pdf,
                    mapped_ratio=mapped_ratio,
                    average_confidence=average_confidence,
                )
                mineru_layout = self._translation_layout(
                    document,
                    fake_pdf,
                    adapter=MINERU_LAYOUT_ADAPTER,
                    adapter_version=MINERU_TRANSLATION_LAYOUT_ADAPTER_VERSION,
                )
                mineru_result = self._mineru_result(document.blocks)
                with (
                    patch.object(
                        routes_papers,
                        "_pdf_path_for_document",
                        new=AsyncMock(return_value=fake_pdf),
                    ),
                    patch.object(routes_papers, "extract_pdf_layout", return_value=object()),
                    patch.object(
                        routes_papers,
                        "translation_layout_from_pdf_layout",
                        return_value=poppler_layout,
                    ),
                    patch.object(
                        routes_papers,
                        "_parse_pdf_with_standard_mineru",
                        new=AsyncMock(return_value=mineru_result),
                    ) as mineru,
                    patch.object(
                        routes_papers,
                        "translation_layout_from_mineru",
                        return_value=mineru_layout,
                    ),
                ):
                    response = self.client.get(f"/papers/{paper_id}/translation-layout")

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["adapter"], HYBRID_LAYOUT_ADAPTER)
                mineru.assert_awaited_once()
                self.assertFalse(mineru.await_args.kwargs["is_ocr"])

    def test_translation_layout_safe_coverage_failure_without_mineru_is_unavailable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            storage_files, "PAPERS_DIR", Path(tmp) / "papers"
        ):
            document = self._document("2512.24958")
            save_document(document)
            fake_pdf = Path(tmp) / "original.pdf"
            fake_pdf.write_bytes(b"%PDF-layout-safe-coverage-low")
            poppler_layout = self._translation_layout(
                document,
                fake_pdf,
                mapped_ratio=0.95,
                average_confidence=0.97,
                safe_replace_indexes={document.blocks[0].index},
            )
            with (
                patch.object(
                    routes_papers,
                    "_pdf_path_for_document",
                    new=AsyncMock(return_value=fake_pdf),
                ),
                patch.object(routes_papers, "extract_pdf_layout", return_value=object()),
                patch.object(
                    routes_papers,
                    "translation_layout_from_pdf_layout",
                    return_value=poppler_layout,
                ),
                patch.object(
                    routes_papers,
                    "_parse_pdf_with_standard_mineru",
                    new=AsyncMock(side_effect=MinerUError("provider offline")),
                ) as mineru,
            ):
                response = self.client.get(
                    f"/papers/{document.paper_id}/translation-layout"
                )

        self.assertEqual(response.status_code, 409)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "layout_unavailable")
        self.assertEqual(detail["poppler"]["safe_coverage"], 0.5)
        self.assertEqual(detail["poppler"]["reason"], "quality_below_threshold")
        mineru.assert_awaited_once()

    def test_translation_layout_safe_coverage_failure_uses_stored_mineru_hybrid(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            storage_files, "PAPERS_DIR", Path(tmp) / "papers"
        ):
            document = self._document("2512.24957")
            save_document(document)
            paper_path = storage_files.ensure_paper_dir(document.paper_id)
            pdf_path = paper_path / "original.pdf"
            shutil.copyfile(
                Path(__file__).parent
                / "fixtures"
                / "translation_layout"
                / "digital_two_column.pdf",
                pdf_path,
            )
            poppler_layout = self._translation_layout(
                document,
                pdf_path,
                mapped_ratio=0.95,
                average_confidence=0.97,
                safe_replace_indexes={document.blocks[0].index},
                page_count=2,
            )
            mineru_result = self._mineru_result_with_geometry(document.blocks)
            generation = storage_files.save_mineru_layout_artifacts(
                document.paper_id,
                mineru_result.layout,
                mineru_result.content_list,
                source_pdf_sha256=source_pdf_sha256(pdf_path),
                is_ocr=False,
            )
            with (
                patch.object(routes_papers, "extract_pdf_layout", return_value=object()),
                patch.object(
                    routes_papers,
                    "translation_layout_from_pdf_layout",
                    return_value=poppler_layout,
                ),
                patch.object(
                    routes_papers,
                    "_parse_pdf_with_standard_mineru",
                    new=AsyncMock(
                        side_effect=AssertionError("stored MinerU must be reused")
                    ),
                ) as parse,
            ):
                response = self.client.get(
                    f"/papers/{document.paper_id}/translation-layout"
                )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["adapter"], HYBRID_LAYOUT_ADAPTER)
        self.assertEqual(payload["sources"][1]["generation"], generation)
        self.assertFalse(payload["sources"][1]["is_ocr"])
        parse.assert_not_awaited()

    def test_translation_layout_reports_unavailable_when_both_adapters_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            storage_files, "PAPERS_DIR", Path(tmp) / "papers"
        ):
            document = self._document("9999.00008")
            save_document(document)
            fake_pdf = Path(tmp) / "original.pdf"
            fake_pdf.write_bytes(b"%PDF-layout-unavailable")
            low_layout = self._translation_layout(
                document,
                fake_pdf,
                mapped_ratio=0.89,
                average_confidence=0.95,
            )
            with (
                patch.object(
                    routes_papers,
                    "_pdf_path_for_document",
                    new=AsyncMock(return_value=fake_pdf),
                ),
                patch.object(routes_papers, "extract_pdf_layout", return_value=object()),
                patch.object(
                    routes_papers,
                    "translation_layout_from_pdf_layout",
                    return_value=low_layout,
                ),
                patch.object(
                    routes_papers,
                    "_parse_pdf_with_standard_mineru",
                    new=AsyncMock(side_effect=MinerUError("provider offline")),
                ) as mineru,
            ):
                response = self.client.get("/papers/9999.00008/translation-layout")

            self.assertEqual(response.status_code, 409)
            detail = response.json()["detail"]
            self.assertEqual(detail["code"], "layout_unavailable")
            self.assertEqual(detail["poppler"]["reason"], "quality_below_threshold")
            self.assertIn("provider offline", detail["mineru"])
            mineru.assert_awaited_once()

    def test_scanned_local_pdf_requests_mineru_with_ocr_enabled(self) -> None:
        blocks = self._blocks()[:2]
        mineru_result = self._mineru_result(blocks)
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(storage_files, "PAPERS_DIR", Path(tmp) / "papers"),
            patch.object(routes_papers, "DATA_DIR", Path(tmp) / "data"),
            patch.object(
                routes_papers,
                "extract_blocks_from_local_pdf",
                side_effect=LocalPdfExtractionError("no text layer"),
            ),
            patch.object(
                routes_papers,
                "_parse_pdf_with_standard_mineru",
                new=AsyncMock(return_value=mineru_result),
            ) as mineru,
            patch.object(
                routes_papers,
                "translation_layout_from_mineru",
                return_value=SimpleNamespace(
                    pdf_url="",
                    model_dump=lambda **_: {},
                ),
            ),
            patch.object(routes_papers, "save_translation_layout"),
            patch.object(routes_papers, "_warm_translation_layout", new=AsyncMock()) as warm_layout,
            patch.object(routes_papers, "insert_paper", new=AsyncMock()),
            patch.object(
                routes_papers,
                "get_paper",
                new=AsyncMock(
                    side_effect=lambda paper_id: {
                        "arxiv_id": paper_id,
                        "title": "Scanned Paper",
                        "authors": [],
                        "source": "mineru",
                        "status": "extracted",
                        "created_at": "2026-07-21T00:00:00Z",
                    }
                ),
            ),
        ):
            response = self.client.post(
                "/papers/local-file",
                files={"file": ("scan.pdf", b"%PDF-1.4\nscanned", "application/pdf")},
                data={"title": "Scanned Paper"},
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["source"], "mineru")
            mineru.assert_awaited_once()
            self.assertTrue(mineru.await_args.kwargs["is_ocr"])
            warm_layout.assert_awaited_once()
            stored = load_document(response.json()["arxiv_id"])
            self.assertIsNotNone(stored)
            assert stored is not None
            self.assertEqual(stored.source, "mineru")

    def test_translation_layout_rebuild_preserves_scanned_ocr_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            storage_files, "PAPERS_DIR", Path(tmp) / "papers"
        ):
            document = self._document("local-scanned-rebuild")
            save_document(document)
            pdf_path = storage_files.ensure_paper_dir(document.paper_id) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-scanned-rebuild")
            mineru_result = self._mineru_result(document.blocks)
            routes_papers._save_mineru_result(
                document.paper_id,
                mineru_result,
                pdf_path,
                is_ocr=True,
            )
            low_layout = self._translation_layout(
                document,
                pdf_path,
                mapped_ratio=0,
                average_confidence=0,
            )
            precise_layout = self._translation_layout(
                document,
                pdf_path,
                adapter=MINERU_LAYOUT_ADAPTER,
                adapter_version=MINERU_TRANSLATION_LAYOUT_ADAPTER_VERSION,
            )
            with (
                patch.object(
                    routes_papers,
                    "_pdf_path_for_document",
                    new=AsyncMock(return_value=pdf_path),
                ),
                patch.object(routes_papers, "extract_pdf_layout", return_value=object()),
                patch.object(
                    routes_papers,
                    "translation_layout_from_pdf_layout",
                    return_value=low_layout,
                ),
                patch.object(
                    routes_papers,
                    "_parse_pdf_with_standard_mineru",
                    new=AsyncMock(return_value=mineru_result),
                ) as parse,
                patch.object(
                    routes_papers,
                    "translation_layout_from_mineru",
                    return_value=precise_layout,
                ),
            ):
                asyncio.run(
                    routes_papers._build_or_load_translation_layout(
                        document.paper_id,
                        document,
                        force=True,
                    )
                )

                paper_path = storage_files.paper_dir(document.paper_id)
                (paper_path / "mineru_layout_meta.json").unlink()
                (paper_path / "translation_layout.json").unlink()
                asyncio.run(
                    routes_papers._build_or_load_translation_layout(
                        document.paper_id,
                        document,
                    )
                )

            self.assertEqual(parse.await_count, 2)
            self.assertTrue(all(call.kwargs["is_ocr"] for call in parse.await_args_list))

    def test_invalid_cached_mineru_generation_is_reparsed_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            storage_files, "PAPERS_DIR", Path(tmp) / "papers"
        ):
            document = self._document("local-mineru-retry")
            save_document(document)
            pdf_path = storage_files.ensure_paper_dir(document.paper_id) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-mineru-retry")
            mineru_result = self._mineru_result(document.blocks)
            routes_papers._save_mineru_result(
                document.paper_id,
                mineru_result,
                pdf_path,
                is_ocr=False,
            )
            low_layout = self._translation_layout(
                document,
                pdf_path,
                mapped_ratio=0.5,
                average_confidence=0.95,
            )
            precise_layout = self._translation_layout(
                document,
                pdf_path,
                adapter=MINERU_LAYOUT_ADAPTER,
                adapter_version=MINERU_TRANSLATION_LAYOUT_ADAPTER_VERSION,
            )
            with (
                patch.object(
                    routes_papers,
                    "_pdf_path_for_document",
                    new=AsyncMock(return_value=pdf_path),
                ),
                patch.object(routes_papers, "extract_pdf_layout", return_value=object()),
                patch.object(
                    routes_papers,
                    "translation_layout_from_pdf_layout",
                    return_value=low_layout,
                ),
                patch.object(
                    routes_papers,
                    "_parse_pdf_with_standard_mineru",
                    new=AsyncMock(return_value=mineru_result),
                ) as parse,
                patch.object(
                    routes_papers,
                    "translation_layout_from_mineru",
                    side_effect=[KeyError("stale generation"), precise_layout],
                ) as convert,
            ):
                result = asyncio.run(
                    routes_papers._build_or_load_translation_layout(
                        document.paper_id,
                        document,
                    )
                )

            self.assertEqual(result.adapter, HYBRID_LAYOUT_ADAPTER)
            parse.assert_awaited_once()
            self.assertFalse(parse.await_args.kwargs["is_ocr"])
            self.assertEqual(convert.call_count, 2)

    def test_translation_layout_reports_missing_non_arxiv_source_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            storage_files, "PAPERS_DIR", Path(tmp) / "papers"
        ):
            save_document(
                PaperDocument(
                    paper_id="mineru-layout-missing",
                    title="Legacy MinerU",
                    source="mineru",
                    extracted_at="2026-07-21T00:00:00Z",
                    blocks=self._blocks()[:2],
                )
            )
            with patch.object(routes_papers, "ensure_pdf", new=AsyncMock()) as ensure:
                response = self.client.get(
                    "/papers/mineru-layout-missing/translation-layout"
                )

            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["detail"]["code"], "source_pdf_missing")
            ensure.assert_not_awaited()

    def test_translation_route_streams_success_error_and_terminal_state(self) -> None:
        async def fake_translate(_doc: PaperDocument, block_index: int):
            if block_index == 0:
                return block_index, "第一段。", "done"
            return block_index, None, "error"

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(storage_files, "PAPERS_DIR", Path(tmp) / "papers"),
            patch.object(
                routes_translate,
                "try_update_status",
                new=AsyncMock(return_value=True),
            ),
            patch.object(routes_translate, "update_status", new=AsyncMock()) as update_status,
            patch(
                "backend.translation.translate.get_config",
                return_value=SimpleNamespace(translation_concurrency=1),
            ),
            patch("backend.translation.translate.translate_single_block", fake_translate),
        ):
            save_document(self._document("9999.00003"))
            response = self.client.post("/translate/9999.00003")
            reloaded = load_document("9999.00003")

            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
            self.assertEqual(response.headers["x-accel-buffering"], "no")
            self.assertIn("event: block_done", response.text)
            self.assertIn("event: block_error", response.text)
            self.assertIn("event: complete", response.text)
            self.assertEqual([block.status for block in reloaded.blocks], ["done", "error"])
            update_status.assert_awaited_once_with("9999.00003", "translation_error")

    def test_analysis_route_returns_and_persists_structured_four_agent_result(self) -> None:
        report = ReproducibilityReport(
            verdict="partially_reproducible",
            confidence="medium",
            evidence=[
                Evidence(aspect=aspect, status="found", detail=f"{aspect} evidence", citation="Section 1")
                for aspect in ("数据集", "代码", "超参数", "硬件环境")
            ],
            summary="Some details remain missing.",
        )
        result = AnalysisResult(
            summary="Structured summary.",
            reproducibility=report,
            improvements=["Add ablations."],
            highlights=["Clear method."],
        )
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(storage_files, "PAPERS_DIR", Path(tmp) / "papers"),
            patch.object(routes_analyze, "analyze_paper", new=AsyncMock(return_value=result)),
            patch.object(
                routes_analyze,
                "try_create_agent_task",
                new=AsyncMock(return_value=(7, True)),
            ),
            patch.object(routes_analyze, "update_status", new=AsyncMock()),
            patch.object(routes_analyze, "update_agent_task", new=AsyncMock()),
        ):
            save_document(self._document("9999.00004"))
            response = self.client.post("/analyze/9999.00004?force=true")
            cached = storage_files.load_analysis("9999.00004")

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["summary"], "Structured summary.")
            self.assertEqual(payload["reproducibility"]["verdict"], "partially_reproducible")
            self.assertEqual(len(payload["reproducibility"]["evidence"]), 4)
            self.assertEqual(payload["improvements"], ["Add ablations."])
            self.assertEqual(payload["highlights"], ["Clear method."])
            self.assertEqual(cached["reproducibility"]["confidence"], "medium")

    @staticmethod
    def _blocks() -> list[Block]:
        return [
            Block(index=0, type="paragraph", original="First paragraph.", status="pending"),
            Block(index=1, type="paragraph", original="Second paragraph.", status="pending"),
            Block(index=2, type="formula", original="x=y", status="skip"),
        ]

    @classmethod
    def _document(cls, paper_id: str) -> PaperDocument:
        return PaperDocument(
            paper_id=paper_id,
            title="Regression Paper",
            source="ar5iv",
            extracted_at="2026-07-16T00:00:00Z",
            blocks=cls._blocks()[:2],
        )

    @staticmethod
    def _translation_layout(
        document: PaperDocument,
        pdf_path: Path,
        *,
        mapped_ratio: float = 1.0,
        average_confidence: float = 0.97,
        adapter: str = POPPLER_LAYOUT_ADAPTER,
        adapter_version: str = POPPLER_LAYOUT_ADAPTER_VERSION,
        sources: list[TranslationLayoutSource] | None = None,
        safe_replace_indexes: set[int] | None = None,
        page_count: int = 1,
    ) -> TranslationLayout:
        source_hash = source_pdf_sha256(pdf_path)
        block_hash = block_source_sha256(document.blocks)
        safe_indexes = (
            {block.index for block in document.blocks}
            if safe_replace_indexes is None
            else safe_replace_indexes
        )
        regions = []
        for position, block in enumerate(document.blocks):
            y0 = 0.10 + position * 0.20
            bbox = NormalizedBox(x0=0.10, y0=y0, x1=0.90, y1=y0 + 0.05)
            safe = block.index in safe_indexes
            regions.append(
                TranslationLayoutRegion(
                    region_id=f"fixture-b{block.index}",
                    block_index=block.index,
                    page=1,
                    flow_order=0,
                    kind=block.type,
                    bbox=bbox,
                    line_boxes=[bbox.model_copy(deep=True)],
                    word_boxes=(
                        [bbox.model_copy(deep=True)]
                        if adapter == POPPLER_LAYOUT_ADAPTER
                        else []
                    ),
                    source_block_order=position,
                    source_line_orders=[position],
                    source_word_orders=(
                        [position] if adapter == POPPLER_LAYOUT_ADAPTER else []
                    ),
                    confidence=average_confidence,
                    render_policy="replace" if safe else "panel_only",
                    failure_reason=None if safe else "hybrid_geometry_unverified",
                    geometry_source=adapter,
                )
            )
        return TranslationLayout(
            cache_key=translation_layout_cache_key(
                source_hash,
                block_hash,
                adapter=adapter,
                adapter_version=adapter_version,
                sources=sources,
            ),
            source_pdf_sha256=source_hash,
            block_source_sha256=block_hash,
            adapter=adapter,
            adapter_version=adapter_version,
            pdf_url=f"/assets/{document.paper_id}/original.pdf",
            page_count=page_count,
            pages=[
                TranslationLayoutPage(
                    page=page,
                    width=612,
                    height=792,
                    rotation=0,
                )
                for page in range(1, page_count + 1)
            ],
            regions=regions,
            quality=TranslationLayoutQuality(
                mappable_count=len(document.blocks),
                mapped_count=len(document.blocks),
                replaceable_count=len(document.blocks),
                panel_only_count=0,
                unmapped_count=0,
                mapped_ratio=mapped_ratio,
                average_confidence=average_confidence,
                protected_overlap_count=0,
                protected_count=0,
                unmapped_block_indexes=[],
                failure_counts={},
            ),
            warnings=[],
            sources=sources or [],
        )

    @staticmethod
    def _mineru_result(blocks: list[Block]) -> MinerUStructuredResult:
        return MinerUStructuredResult(
            markdown="",
            blocks=blocks,
            layout={
                "pdf_info": [
                    {
                        "page_idx": 0,
                        "page_size": [612, 792],
                        "para_blocks": [],
                    }
                ]
            },
            content_list=[],
            layout_member="middle.json",
            content_list_member="content_list.json",
        )

    @staticmethod
    def _mineru_result_with_geometry(
        blocks: list[Block],
    ) -> MinerUStructuredResult:
        para_blocks = []
        content_list = []
        for position, block in enumerate(blocks):
            y0 = 80 + position * 160
            bbox = [72, y0, 540, y0 + 40]
            para_blocks.append(
                {
                    "type": "text",
                    "bbox": bbox,
                    "angle": 0,
                    "lines": [
                        {
                            "bbox": bbox,
                            "spans": [
                                {
                                    "type": "text",
                                    "bbox": bbox,
                                    "content": block.original,
                                }
                            ],
                        }
                    ],
                }
            )
            content_list.append(
                {
                    "type": "text",
                    "text": block.original,
                    "page_idx": 0,
                    "bbox": [
                        bbox[0] / 612 * 1000,
                        bbox[1] / 792 * 1000,
                        bbox[2] / 612 * 1000,
                        bbox[3] / 792 * 1000,
                    ],
                }
            )
        return MinerUStructuredResult(
            markdown="",
            blocks=blocks,
            layout={
                "pdf_info": [
                    {
                        "page_idx": 0,
                        "page_size": [612, 792],
                        "para_blocks": para_blocks,
                    },
                    {
                        "page_idx": 1,
                        "page_size": [612, 792],
                        "para_blocks": [],
                    },
                ]
            },
            content_list=content_list,
            layout_member="middle.json",
            content_list_member="content_list.json",
        )


if __name__ == "__main__":
    unittest.main()
