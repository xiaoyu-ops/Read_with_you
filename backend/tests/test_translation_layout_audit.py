from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from backend.extraction.blocks import Block, PaperDocument
from backend.extraction.mineru import (
    MINERU_LAYOUT_ADAPTER,
    MINERU_LAYOUT_ADAPTER_VERSION,
    MinerUStructuredResult,
)
from backend.extraction.pdf_layout import (
    POPPLER_LAYOUT_ADAPTER,
    POPPLER_LAYOUT_ADAPTER_VERSION,
    extract_pdf_layout,
)
from backend.extraction.translation_layout import (
    HYBRID_LAYOUT_ADAPTER,
    HYBRID_LAYOUT_ADAPTER_VERSION,
    MINERU_TRANSLATION_LAYOUT_ADAPTER_VERSION,
    NormalizedBox,
    bind_mineru_layout_source,
    source_pdf_sha256,
    translation_layout_from_hybrid,
    translation_layout_from_mineru,
    translation_layout_from_pdf_map,
    translation_layout_from_pdf_layout,
)
from backend.storage import files as storage_files
from scripts import audit_translation_layout


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "translation_layout"
REAL_1706_DIR = Path(__file__).parents[2] / "data" / "papers" / "1706.03762"
REAL_2512_DIR = Path(__file__).parents[2] / "data" / "papers" / "2512.24957"
REAL_2303_DIR = Path(__file__).parents[2] / "data" / "papers" / "2303.09540"


class TranslationLayoutAuditTest(unittest.TestCase):
    @unittest.skipUnless(
        shutil.which("pdftotext")
        and shutil.which("pdfinfo")
        and (REAL_2303_DIR / "original.pdf").is_file()
        and (REAL_2303_DIR / "translation.json").is_file(),
        "The local 2303.09540 paper and Poppler are required",
    )
    def test_real_2303_hybrid_recovers_ordered_poppler_geometry(self) -> None:
        document = PaperDocument.from_dict(
            json.loads(
                (REAL_2303_DIR / "translation.json").read_text(encoding="utf-8")
            )
        )
        pdf_path = REAL_2303_DIR / "original.pdf"
        poppler = translation_layout_from_pdf_layout(
            document.blocks,
            pdf_path,
            extract_pdf_layout(pdf_path),
        )
        bundle = storage_files.load_mineru_layout_artifact_bundle_from_dir(
            REAL_2303_DIR,
            expected_source_pdf_sha256=source_pdf_sha256(pdf_path),
        )
        self.assertIsNotNone(bundle)
        assert bundle is not None
        middle, content_list, provenance = bundle
        mineru = translation_layout_from_mineru(
            document.blocks,
            pdf_path,
            MinerUStructuredResult(
                markdown="",
                blocks=[],
                layout=middle,
                content_list=content_list,
            ),
        )
        hybrid = translation_layout_from_hybrid(
            document.blocks,
            pdf_path,
            poppler,
            mineru,
            mineru_generation=provenance["generation"],
            mineru_is_ocr=provenance["is_ocr"],
        )

        metrics = audit_translation_layout._safe_layout_metrics(document, hybrid)

        self.assertEqual(metrics["eligible_count"], 106)
        self.assertEqual(metrics["safe_replace_count"], 97)
        self.assertEqual(metrics["safe_coverage"], 0.915094)
        self.assertFalse(
            any(region.kind == "figure" for region in poppler.regions)
        )

        block_78 = [region for region in hybrid.regions if region.block_index == 78]
        block_114 = [region for region in hybrid.regions if region.block_index == 114]
        self.assertEqual({region.page for region in block_78}, {9})
        self.assertEqual({region.page for region in block_114}, {17})
        self.assertEqual(
            {region.geometry_source for region in (*block_78, *block_114)},
            {POPPLER_LAYOUT_ADAPTER},
        )

        expected_geometry = {
            103: (6, 73),
            106: (5, 75),
            107: (2, 29),
            109: (6, 71),
            111: (3, 34),
        }
        for block_index, (line_count, word_count) in expected_geometry.items():
            regions = [
                region
                for region in hybrid.regions
                if region.block_index == block_index
            ]
            self.assertEqual(len(regions), 1)
            self.assertEqual(regions[0].page, 13)
            self.assertEqual(regions[0].geometry_source, POPPLER_LAYOUT_ADAPTER)
            self.assertEqual(len(regions[0].line_boxes), line_count)
            self.assertEqual(len(regions[0].word_boxes), word_count)

    @unittest.skipUnless(
        shutil.which("pdftotext")
        and shutil.which("pdfinfo")
        and (REAL_2512_DIR / "original.pdf").is_file()
        and (REAL_2512_DIR / "translation.json").is_file(),
        "The local 2512.24957 paper and Poppler are required",
    )
    def test_real_2512_partial_poppler_groups_fail_closed(self) -> None:
        document = PaperDocument.from_dict(
            json.loads(
                (REAL_2512_DIR / "translation.json").read_text(encoding="utf-8")
            )
        )
        layout = translation_layout_from_pdf_layout(
            document.blocks,
            REAL_2512_DIR / "original.pdf",
            extract_pdf_layout(REAL_2512_DIR / "original.pdf"),
        )

        metrics = audit_translation_layout._safe_layout_metrics(document, layout)

        self.assertEqual(metrics["eligible_count"], 144)
        self.assertEqual(metrics["safe_replace_count"], 109)
        self.assertEqual(metrics["safe_coverage"], 0.756944)
        self.assertLess(
            metrics["safe_coverage"],
            audit_translation_layout.SOURCE_CLASS_THRESHOLDS["arxiv_digital"],
        )
        unsafe_eligible = {9, 106}
        self.assertTrue(
            unsafe_eligible.isdisjoint(
                region.block_index
                for region in layout.regions
                if region.render_policy == "replace"
            )
        )
        recovered_complete_groups = {48, 142, 144}
        self.assertTrue(
            recovered_complete_groups.issubset(
                region.block_index
                for region in layout.regions
                if region.render_policy == "replace"
            )
        )

        bundle = storage_files.load_mineru_layout_artifact_bundle_from_dir(
            REAL_2512_DIR,
            expected_source_pdf_sha256=source_pdf_sha256(REAL_2512_DIR / "original.pdf"),
        )
        self.assertIsNotNone(bundle)
        assert bundle is not None
        middle, content_list, provenance = bundle
        mineru = translation_layout_from_mineru(
            document.blocks,
            REAL_2512_DIR / "original.pdf",
            MinerUStructuredResult(
                markdown="",
                blocks=[],
                layout=middle,
                content_list=content_list,
            ),
        )
        hybrid = translation_layout_from_hybrid(
            document.blocks,
            REAL_2512_DIR / "original.pdf",
            layout,
            mineru,
            mineru_generation=provenance["generation"],
            mineru_is_ocr=provenance["is_ocr"],
        )
        hybrid_metrics = audit_translation_layout._safe_layout_metrics(
            document,
            hybrid,
        )
        self.assertEqual(hybrid_metrics["safe_replace_count"], 121)
        self.assertEqual(hybrid_metrics["safe_coverage"], 0.840278)

    @unittest.skipUnless(
        shutil.which("pdftotext")
        and shutil.which("pdfinfo")
        and (REAL_1706_DIR / "original.pdf").is_file()
        and (REAL_1706_DIR / "translation.json").is_file(),
        "The local 1706.03762 paper and Poppler are required",
    )
    def test_real_1706_poppler_citation_geometry_meets_safe_coverage(self) -> None:
        document = PaperDocument.from_dict(
            json.loads(
                (REAL_1706_DIR / "translation.json").read_text(encoding="utf-8")
            )
        )
        layout = translation_layout_from_pdf_layout(
            document.blocks,
            REAL_1706_DIR / "original.pdf",
            extract_pdf_layout(REAL_1706_DIR / "original.pdf"),
        )

        metrics = audit_translation_layout._safe_layout_metrics(document, layout)

        self.assertEqual(metrics["eligible_count"], 75)
        self.assertEqual(metrics["safe_replace_count"], 70)
        self.assertEqual(metrics["safe_coverage"], 0.933333)
        self.assertGreaterEqual(
            metrics["safe_coverage"],
            audit_translation_layout.SOURCE_CLASS_THRESHOLDS["arxiv_digital"],
        )

        bundle = storage_files.load_mineru_layout_artifact_bundle_from_dir(
            REAL_1706_DIR,
            expected_source_pdf_sha256=source_pdf_sha256(REAL_1706_DIR / "original.pdf"),
        )
        self.assertIsNotNone(bundle)
        assert bundle is not None
        middle, content_list, provenance = bundle
        mineru = translation_layout_from_mineru(
            document.blocks,
            REAL_1706_DIR / "original.pdf",
            MinerUStructuredResult(
                markdown="",
                blocks=[],
                layout=middle,
                content_list=content_list,
            ),
        )
        hybrid = translation_layout_from_hybrid(
            document.blocks,
            REAL_1706_DIR / "original.pdf",
            layout,
            mineru,
            mineru_generation=provenance["generation"],
            mineru_is_ocr=provenance["is_ocr"],
        )
        hybrid_metrics = audit_translation_layout._safe_layout_metrics(
            document,
            hybrid,
        )
        self.assertEqual(hybrid_metrics["safe_replace_count"], 72)
        self.assertEqual(hybrid_metrics["safe_coverage"], 0.96)

    def test_legacy_latex_debris_fails_even_when_safe_coverage_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            papers_dir = Path(tmp)
            paper_dir = self._write_sample(papers_dir, "2512.24957")
            self._write_precise_layout(
                paper_dir,
                source="latex",
                adapter=POPPLER_LAYOUT_ADAPTER,
                eligible_count=2,
                safe_count=2,
                confidence=0.93,
                originals=[
                    "Clean extracted prose remains safely replaceable.",
                    r"\appendix",
                ],
            )

            result = audit_translation_layout.audit_paper(
                papers_dir,
                "2512.24957",
            )

        self.assertEqual(result["source_class"], "arxiv_digital")
        self.assertEqual(result["safe_coverage"], 1.0)
        self.assertEqual(result["legacy_latex_debris_count"], 1)
        self.assertEqual(result["status"], "fail")
        self.assertIn("legacy_latex_extraction_debris", result["reasons"])

    def test_legacy_preview_is_read_only_but_fails_the_precise_layout_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            papers_dir = Path(tmp)
            paper_dir = self._write_sample(papers_dir)

            result = audit_translation_layout.audit_paper(papers_dir, "sample")

            self.assertEqual(result["status"], "fail")
            self.assertEqual(result["layout_source"], "legacy_preview")
            self.assertEqual(result["page_count"], 2)
            self.assertEqual(result["region_count"], 2)
            self.assertIn("precise_layout_missing", result["reasons"])
            self.assertFalse((paper_dir / "translation_layout.json").exists())

    def test_missing_source_pdf_returns_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            papers_dir = Path(tmp)
            paper_dir = self._write_sample(papers_dir)
            (paper_dir / "original.pdf").unlink()

            with redirect_stdout(io.StringIO()):
                exit_code = audit_translation_layout.main(
                    ["--papers-dir", str(papers_dir), "--paper", "sample"]
                )
            result = audit_translation_layout.audit_paper(papers_dir, "sample")

        self.assertEqual(exit_code, 1)
        self.assertEqual(result["status"], "fail")
        self.assertIn("source_pdf_missing", result["reasons"])

    def test_poppler_probe_is_read_only_and_reports_a_precise_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            papers_dir = Path(tmp)
            paper_dir = self._write_sample(papers_dir)
            layout = self._write_precise_layout(
                paper_dir,
                source="local_pdf",
                adapter=POPPLER_LAYOUT_ADAPTER,
                eligible_count=1,
                safe_count=1,
                confidence=0.93,
            )
            (paper_dir / "translation_layout.json").unlink()
            with (
                patch.object(audit_translation_layout, "extract_pdf_layout", return_value=object()),
                patch.object(
                    audit_translation_layout,
                    "translation_layout_from_pdf_layout",
                    return_value=layout,
                ),
            ):
                result = audit_translation_layout.audit_paper(
                    papers_dir,
                    "sample",
                    probe_poppler=True,
                )
            cache_exists = (paper_dir / "translation_layout.json").exists()

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["layout_source"], "poppler_probe")
        self.assertEqual(result["adapter"], audit_translation_layout.POPPLER_LAYOUT_ADAPTER)
        self.assertEqual(result["source_text_page_evidence"], "unavailable")
        self.assertIn("source_text_page_evidence_unavailable", result["warnings"])
        self.assertFalse(cache_exists)

    def test_source_text_page_missing_even_when_page_has_protected_geometry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            papers_dir = Path(tmp)
            paper_dir = self._write_sample(papers_dir, "mineru-missing-page")
            layout = self._write_precise_layout(
                paper_dir,
                source="mineru",
                adapter=MINERU_LAYOUT_ADAPTER,
                eligible_count=1,
                safe_count=1,
                confidence=0.93,
                is_ocr=False,
            )
            region = layout.regions[0]
            region.render_policy = "preserve"
            region.failure_reason = "protected_content"
            layout.pages[0].protected_boxes = [region.bbox.model_copy(deep=True)]
            self._save_layout(paper_dir, layout)

            result = audit_translation_layout.audit_paper(
                papers_dir,
                "mineru-missing-page",
            )

        self.assertEqual(result["source_text_page_count"], 1)
        self.assertEqual(result["accessible_text_page_count"], 0)
        self.assertEqual(result["missing_text_pages"], [1])
        self.assertIn("source_text_page_without_region", result["reasons"])

    def test_reference_and_protected_only_pages_are_exempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            papers_dir = Path(tmp)
            paper_dir = self._write_sample(papers_dir, "mineru-exempt-pages")
            layout = self._write_precise_layout(
                paper_dir,
                source="mineru",
                adapter=MINERU_LAYOUT_ADAPTER,
                eligible_count=1,
                safe_count=1,
                confidence=0.93,
                is_ocr=False,
            )
            first_page = layout.pages[0]
            layout.page_count = 3
            layout.pages = [
                first_page,
                first_page.model_copy(update={"page": 2, "protected_boxes": []}),
                first_page.model_copy(update={"page": 3, "protected_boxes": []}),
            ]
            layout.regions[0].page = 3
            self._save_layout(paper_dir, layout)
            self._write_mineru_artifact_generation(
                paper_dir,
                layout,
                [
                    {"type": "ref_text", "text": "Reference entry", "page_idx": 0},
                    {"type": "image", "img_path": "figure.png", "page_idx": 1},
                    {"type": "text", "text": "Reachable prose", "page_idx": 2},
                ],
                generation="a" * 32,
                is_ocr=False,
            )

            result = audit_translation_layout.audit_paper(
                papers_dir,
                "mineru-exempt-pages",
            )

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["source_text_page_count"], 1)
        self.assertEqual(result["accessible_text_page_count"], 1)
        self.assertEqual(result["missing_text_pages"], [])
        self.assertEqual(result["reference_only_pages"], [1])
        self.assertEqual(result["protected_only_pages"], [2])

    def test_panel_only_is_reachable_but_preserve_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            papers_dir = Path(tmp)
            paper_dir = self._write_sample(papers_dir, "mineru-panel-preserve")
            layout = self._write_precise_layout(
                paper_dir,
                source="mineru",
                adapter=MINERU_LAYOUT_ADAPTER,
                eligible_count=2,
                safe_count=1,
                confidence=0.93,
                is_ocr=False,
            )
            first_page = layout.pages[0]
            layout.page_count = 2
            layout.pages = [
                first_page,
                first_page.model_copy(update={"page": 2, "protected_boxes": []}),
            ]
            layout.regions[0].render_policy = "panel_only"
            layout.regions[0].failure_reason = "low_confidence"
            layout.regions[1].page = 2
            layout.regions[1].render_policy = "preserve"
            layout.regions[1].failure_reason = "protected_content"
            self._save_layout(paper_dir, layout)
            self._write_mineru_artifact_generation(
                paper_dir,
                layout,
                [
                    {"type": "text", "text": "Panel text", "page_idx": 0},
                    {"type": "text", "text": "Preserved text", "page_idx": 1},
                ],
                generation="a" * 32,
                is_ocr=False,
            )

            result = audit_translation_layout.audit_paper(
                papers_dir,
                "mineru-panel-preserve",
            )

        self.assertEqual(result["source_text_page_count"], 2)
        self.assertEqual(result["accessible_text_page_count"], 1)
        self.assertEqual(result["missing_text_pages"], [2])
        self.assertIn("source_text_page_without_region", result["reasons"])

    def test_safe_replace_average_confidence_boundary(self) -> None:
        for confidence, expected_status in ((0.919, "fail"), (0.920, "pass")):
            with self.subTest(confidence=confidence), tempfile.TemporaryDirectory() as tmp:
                papers_dir = Path(tmp)
                paper_dir = self._write_sample(papers_dir)
                self._write_precise_layout(
                    paper_dir,
                    source="local_pdf",
                    adapter=POPPLER_LAYOUT_ADAPTER,
                    eligible_count=1,
                    safe_count=1,
                    confidence=confidence,
                )

                result = audit_translation_layout.audit_paper(papers_dir, "sample")

            self.assertEqual(result["status"], expected_status)
            self.assertEqual(result["replace_average_confidence"], confidence)
            if expected_status == "fail":
                self.assertIn(
                    "replace_average_confidence_below_threshold",
                    result["reasons"],
                )
            else:
                self.assertNotIn(
                    "replace_average_confidence_below_threshold",
                    result["reasons"],
                )

    def test_source_classes_enforce_their_coverage_thresholds(self) -> None:
        cases = (
            ("1706.03762", "ar5iv", POPPLER_LAYOUT_ADAPTER, None, "arxiv_digital", 10, 9, 8, 0.90),
            ("local-sample", "local_pdf", POPPLER_LAYOUT_ADAPTER, None, "local_digital", 20, 17, 16, 0.85),
            ("mineru-sample", "mineru", MINERU_LAYOUT_ADAPTER, False, "mineru_complex", 5, 4, 3, 0.80),
            ("scan-sample", "ocr", MINERU_LAYOUT_ADAPTER, True, "scan_ocr", 10, 7, 6, 0.70),
        )
        for paper_id, source, adapter, is_ocr, source_class, total, at_gate, below, threshold in cases:
            for safe_count, expected_status in ((at_gate, "pass"), (below, "fail")):
                with (
                    self.subTest(source_class=source_class, safe_count=safe_count),
                    tempfile.TemporaryDirectory() as tmp,
                ):
                    papers_dir = Path(tmp)
                    paper_dir = self._write_sample(papers_dir, paper_id)
                    self._write_precise_layout(
                        paper_dir,
                        source=source,
                        adapter=adapter,
                        eligible_count=total,
                        safe_count=safe_count,
                        confidence=0.93,
                        is_ocr=is_ocr,
                    )

                    result = audit_translation_layout.audit_paper(
                        papers_dir,
                        paper_id,
                    )

                self.assertEqual(result["status"], expected_status)
                self.assertEqual(result["source_class"], source_class)
                self.assertEqual(result["threshold"], threshold)
                self.assertEqual(result["eligible_count"], total)
                self.assertEqual(result["safe_replace_count"], safe_count)

    def test_mixed_policy_regions_make_the_whole_block_unsafe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            papers_dir = Path(tmp)
            paper_dir = self._write_sample(papers_dir)
            layout = self._write_precise_layout(
                paper_dir,
                source="local_pdf",
                adapter=POPPLER_LAYOUT_ADAPTER,
                eligible_count=1,
                safe_count=1,
                confidence=0.93,
            )
            second = layout.regions[0].model_copy(deep=True)
            second.region_id = f"{second.region_id}-second"
            second.flow_order = 1
            second.render_policy = "panel_only"
            second.failure_reason = "low_confidence"
            layout.regions.append(second)
            self._save_layout(paper_dir, layout)

            result = audit_translation_layout.audit_paper(papers_dir, "sample")

        self.assertEqual(result["safe_replace_count"], 0)
        self.assertEqual(result["safe_coverage"], 0.0)
        self.assertIn("safe_coverage_below_threshold", result["reasons"])

    def test_tampered_stored_quality_cannot_create_a_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            papers_dir = Path(tmp)
            paper_dir = self._write_sample(papers_dir)
            layout = self._write_precise_layout(
                paper_dir,
                source="local_pdf",
                adapter=POPPLER_LAYOUT_ADAPTER,
                eligible_count=1,
                safe_count=1,
                confidence=0.919,
            )
            layout.quality.replaceable_count = 999
            layout.quality.average_confidence = 1.0
            self._save_layout(paper_dir, layout)

            result = audit_translation_layout.audit_paper(papers_dir, "sample")

        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["safe_replace_count"], 1)
        self.assertEqual(result["replace_average_confidence"], 0.919)
        self.assertIn(
            "replace_average_confidence_below_threshold",
            result["reasons"],
        )

    def test_mineru_without_ocr_provenance_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            papers_dir = Path(tmp)
            paper_dir = self._write_sample(papers_dir)
            self._write_precise_layout(
                paper_dir,
                source="mineru",
                adapter=MINERU_LAYOUT_ADAPTER,
                eligible_count=1,
                safe_count=1,
                confidence=0.93,
            )

            result = audit_translation_layout.audit_paper(papers_dir, "sample")

        self.assertEqual(result["status"], "fail")
        self.assertIsNone(result["source_class"])
        self.assertIn("source_class_unknown", result["reasons"])

    def test_mineru_provenance_must_match_current_pdf_and_schema(self) -> None:
        for mutation in ("bare", "stale_hash", "conflicting_generation"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                papers_dir = Path(tmp)
                paper_dir = self._write_sample(papers_dir, "mineru-sample")
                self._write_precise_layout(
                    paper_dir,
                    source="mineru",
                    adapter=MINERU_LAYOUT_ADAPTER,
                    eligible_count=1,
                    safe_count=1,
                    confidence=0.93,
                    is_ocr=False,
                )
                source_meta_path = paper_dir / "mineru_source_meta.json"
                source_meta = json.loads(source_meta_path.read_text(encoding="utf-8"))
                if mutation == "bare":
                    source_meta_path.write_text(json.dumps({"is_ocr": True}), encoding="utf-8")
                    (paper_dir / "mineru_layout_meta.json").unlink()
                elif mutation == "stale_hash":
                    source_meta["source_pdf_sha256"] = "f" * 64
                    source_meta_path.write_text(json.dumps(source_meta), encoding="utf-8")
                else:
                    source_meta["generation"] = "b" * 32
                    source_meta_path.write_text(json.dumps(source_meta), encoding="utf-8")

                result = audit_translation_layout.audit_paper(
                    papers_dir,
                    "mineru-sample",
                )

            self.assertEqual(result["status"], "fail")
            self.assertIsNone(result["source_class"])
            self.assertIn("source_class_unknown", result["reasons"])

    def test_poppler_source_must_agree_with_identifier_class(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            papers_dir = Path(tmp)
            paper_dir = self._write_sample(papers_dir, "1706.03762")
            self._write_precise_layout(
                paper_dir,
                source="local_pdf",
                adapter=POPPLER_LAYOUT_ADAPTER,
                eligible_count=1,
                safe_count=1,
                confidence=0.93,
            )

            result = audit_translation_layout.audit_paper(papers_dir, "1706.03762")

        self.assertEqual(result["status"], "fail")
        self.assertIsNone(result["source_class"])
        self.assertIn("source_class_unknown", result["reasons"])

    def test_safe_region_requires_text_kind_and_contained_line_geometry(self) -> None:
        for mutation in ("non_text", "empty_lines", "line_outside"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                papers_dir = Path(tmp)
                paper_dir = self._write_sample(papers_dir)
                layout = self._write_precise_layout(
                    paper_dir,
                    source="local_pdf",
                    adapter=POPPLER_LAYOUT_ADAPTER,
                    eligible_count=1,
                    safe_count=1,
                    confidence=0.93,
                )
                region = layout.regions[0]
                if mutation == "non_text":
                    region.kind = "image"
                elif mutation == "empty_lines":
                    region.line_boxes = []
                    region.source_line_orders = []
                else:
                    region.line_boxes = [
                        NormalizedBox(
                            x0=region.bbox.x0,
                            y0=max(0.0, region.bbox.y0 - 0.005),
                            x1=region.bbox.x1,
                            y1=region.bbox.y1,
                        )
                    ]
                    region.source_line_orders = [0]
                self._save_layout(paper_dir, layout)

                result = audit_translation_layout.audit_paper(papers_dir, "sample")

            self.assertEqual(result["safe_replace_count"], 0)
            self.assertIn("safe_coverage_below_threshold", result["reasons"])

    def test_positive_protected_overlap_is_not_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            papers_dir = Path(tmp)
            paper_dir = self._write_sample(papers_dir)
            layout = self._write_precise_layout(
                paper_dir,
                source="local_pdf",
                adapter=POPPLER_LAYOUT_ADAPTER,
                eligible_count=1,
                safe_count=1,
                confidence=0.93,
            )
            layout.regions[0].protected_boxes = [
                NormalizedBox(x0=0.1, y0=0.01, x1=0.2, y1=0.03)
            ]
            self._save_layout(paper_dir, layout)

            result = audit_translation_layout.audit_paper(papers_dir, "sample")

        self.assertEqual(result["safe_replace_count"], 0)
        self.assertIn("safe_coverage_below_threshold", result["reasons"])

    def test_math_blocks_are_excluded_from_coverage_denominator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            papers_dir = Path(tmp)
            paper_dir = self._write_sample(papers_dir)
            self._write_precise_layout(
                paper_dir,
                source="local_pdf",
                adapter=POPPLER_LAYOUT_ADAPTER,
                eligible_count=2,
                safe_count=1,
                confidence=0.93,
                originals=[
                    "Ordinary prose remains eligible.",
                    r"The protected equation is \(E = mc^2\).",
                ],
            )

            result = audit_translation_layout.audit_paper(papers_dir, "sample")

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["protected_excluded_count"], 1)
        self.assertEqual(result["eligible_count"], 1)
        self.assertEqual(result["safe_replace_count"], 1)
        self.assertEqual(result["safe_coverage"], 1.0)

    def test_citation_fragments_do_not_receive_math_exclusion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            papers_dir = Path(tmp)
            paper_dir = self._write_sample(papers_dir)
            self._write_precise_layout(
                paper_dir,
                source="local_pdf",
                adapter=POPPLER_LAYOUT_ADAPTER,
                eligible_count=2,
                safe_count=1,
                confidence=0.93,
                originals=[
                    "Ordinary prose remains eligible.",
                    r"Prior work \cite{author2026} remains eligible too.",
                ],
            )

            result = audit_translation_layout.audit_paper(papers_dir, "sample")

        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["protected_excluded_count"], 0)
        self.assertEqual(result["eligible_count"], 2)
        self.assertEqual(result["safe_replace_count"], 1)
        self.assertEqual(result["safe_coverage"], 0.5)
        self.assertIn("safe_coverage_below_threshold", result["reasons"])

    def test_hybrid_audit_uses_clean_denominator_and_page_protection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            papers_dir = Path(tmp)
            paper_id = "mineru-hybrid-audit"
            paper_dir = papers_dir / paper_id
            paper_dir.mkdir()
            table = json.dumps(
                {
                    "kind": "table",
                    "rows": [
                        [{"text": "Data / Model", "header": True}],
                        [{"text": "Dedup20", "header": False}],
                    ],
                }
            )
            blocks = [
                Block(0, "paragraph", r"0.1pt \contournumber 10"),
                Block(1, "table", table),
                Block(2, "paragraph", "Data / Model"),
                Block(3, "paragraph", "Dedup20"),
                Block(4, "paragraph", "This block is safe to replace."),
                Block(5, "paragraph", "This block overlaps protected page geometry."),
                Block(6, "paragraph", r"Equation \(E=mc^2\) stays protected."),
            ]
            document = PaperDocument(
                paper_id=paper_id,
                title="Hybrid audit fixture",
                source="mineru",
                extracted_at="2026-07-21T00:00:00Z",
                blocks=blocks,
            )
            (paper_dir / "translation.json").write_text(
                json.dumps(document.to_dict()),
                encoding="utf-8",
            )
            pdf_path = paper_dir / "original.pdf"
            pdf_path.write_bytes(b"%PDF-hybrid-audit")
            mapping = {
                "pdf_url": f"/assets/{paper_id}/original.pdf",
                "page_count": 1,
                "mappings": [
                    {
                        "block_index": block_index,
                        "confidence": 0.93,
                        "boxes": [
                            {
                                "page": 1,
                                "x0": 60,
                                "y0": y0,
                                "x1": 540,
                                "y1": y0 + 40,
                                "page_width": 600,
                                "page_height": 1000,
                            }
                        ],
                    }
                    for block_index, y0 in ((4, 100), (5, 300), (6, 500))
                ],
            }
            poppler = translation_layout_from_pdf_map(
                blocks,
                pdf_path,
                mapping,
                adapter=POPPLER_LAYOUT_ADAPTER,
                adapter_version=POPPLER_LAYOUT_ADAPTER_VERSION,
            )
            for region in poppler.regions:
                region.confidence = 0.93
                region.render_policy = "replace"
                region.failure_reason = None
            mineru = poppler.model_copy(deep=True)
            mineru.adapter = MINERU_LAYOUT_ADAPTER
            mineru.adapter_version = MINERU_TRANSLATION_LAYOUT_ADAPTER_VERSION
            mineru.regions = []
            protected = next(
                region.bbox.model_copy(deep=True)
                for region in poppler.regions
                if region.block_index == 5
            )
            mineru.pages[0].protected_boxes = [protected]
            generation = "c" * 32
            layout = translation_layout_from_hybrid(
                blocks,
                pdf_path,
                poppler,
                mineru,
                mineru_generation=generation,
                mineru_is_ocr=False,
            )
            self._save_layout(paper_dir, layout)
            self._write_mineru_artifact_generation(
                paper_dir,
                layout,
                [
                    {
                        "type": "text",
                        "text": block.original,
                        "page_idx": 0,
                        "bbox": [60, 100 + block.index * 20, 540, 115 + block.index * 20],
                    }
                    for block in blocks
                    if block.index in {4, 5, 6}
                ],
                generation=generation,
                is_ocr=False,
            )

            result = audit_translation_layout.audit_paper(papers_dir, paper_id)

        self.assertEqual(result["adapter"], HYBRID_LAYOUT_ADAPTER)
        self.assertEqual(layout.adapter_version, HYBRID_LAYOUT_ADAPTER_VERSION)
        self.assertEqual(result["source_class"], "mineru_complex")
        self.assertEqual(result["eligible_count"], 2)
        self.assertEqual(result["protected_excluded_count"], 1)
        self.assertEqual(result["safe_replace_count"], 1)
        self.assertEqual(result["safe_coverage"], 0.5)
        self.assertNotIn("precise_adapter_required", result["reasons"])
        self.assertIn("safe_coverage_below_threshold", result["reasons"])

    @unittest.skipUnless(
        shutil.which("pdftotext") and shutil.which("pdfinfo"),
        "Poppler is required for the checked-in PDF quality fixtures",
    )
    def test_checked_in_digital_and_scanned_pdfs_pass_source_quality_gates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            papers_dir = Path(tmp)

            digital_id = "local-digital-fixture"
            digital_dir = papers_dir / digital_id
            digital_dir.mkdir()
            digital_pdf = digital_dir / "original.pdf"
            shutil.copyfile(FIXTURE_DIR / "digital_two_column.pdf", digital_pdf)
            digital_blocks = [
                Block(0, "heading", "Deterministic Two Column Layout", level=1),
                Block(1, "heading", "Abstract", level=2),
                Block(2, "heading", "Method", level=2),
                Block(3, "paragraph", "The left column preserves reading order"),
                Block(4, "paragraph", "The right column starts after the left flow."),
                Block(5, "heading", "Results", level=2),
                Block(
                    6,
                    "paragraph",
                    "A paragraph on the second page verifies page continuity.",
                ),
            ]
            digital_document = PaperDocument(
                paper_id=digital_id,
                title="Deterministic Two Column Layout",
                source="local_pdf",
                extracted_at="2026-07-21T00:00:00Z",
                blocks=digital_blocks,
            )
            (digital_dir / "translation.json").write_text(
                json.dumps(digital_document.to_dict()),
                encoding="utf-8",
            )
            digital_layout = translation_layout_from_pdf_layout(
                digital_blocks,
                digital_pdf,
                extract_pdf_layout(digital_pdf),
            )
            self._save_layout(digital_dir, digital_layout)

            scan_id = "local-scan-fixture"
            scan_dir = papers_dir / scan_id
            scan_dir.mkdir()
            scan_pdf = scan_dir / "original.pdf"
            shutil.copyfile(FIXTURE_DIR / "scanned_two_page.pdf", scan_pdf)
            scan_middle = json.loads(
                (FIXTURE_DIR / "scanned_middle.json").read_text(encoding="utf-8")
            )
            scan_content = json.loads(
                (FIXTURE_DIR / "scanned_content_list.json").read_text(
                    encoding="utf-8"
                )
            )
            scan_blocks = [
                Block(0, "paragraph", "OCR text from scanned page one."),
                Block(1, "paragraph", "OCR text from scanned page two."),
            ]
            scan_document = PaperDocument(
                paper_id=scan_id,
                title="Two Page Scan",
                source="mineru",
                extracted_at="2026-07-21T00:00:00Z",
                blocks=scan_blocks,
            )
            (scan_dir / "translation.json").write_text(
                json.dumps(scan_document.to_dict()),
                encoding="utf-8",
            )
            scan_result = MinerUStructuredResult(
                markdown="fixture",
                blocks=scan_blocks,
                layout=scan_middle,
                content_list=scan_content,
            )
            scan_layout = translation_layout_from_mineru(
                scan_blocks,
                scan_pdf,
                scan_result,
            )
            generation = "a" * 32
            scan_layout = bind_mineru_layout_source(
                scan_layout,
                generation=generation,
                is_ocr=True,
            )
            self._save_layout(scan_dir, scan_layout)
            self._write_mineru_artifact_generation(
                scan_dir,
                scan_layout,
                scan_content,
                generation=generation,
                is_ocr=True,
            )

            digital_audit = audit_translation_layout.audit_paper(
                papers_dir,
                digital_id,
            )
            scan_audit = audit_translation_layout.audit_paper(
                papers_dir,
                scan_id,
            )

        self.assertEqual(digital_audit["status"], "pass")
        self.assertEqual(digital_audit["source_class"], "local_digital")
        self.assertEqual(digital_audit["safe_coverage"], 1.0)
        self.assertEqual(scan_audit["status"], "pass")
        self.assertEqual(scan_audit["source_class"], "scan_ocr")
        self.assertEqual(scan_audit["safe_coverage"], 1.0)

    @staticmethod
    def _write_sample(papers_dir: Path, paper_id: str = "sample") -> Path:
        paper_dir = papers_dir / paper_id
        paper_dir.mkdir(parents=True)
        document = {
            "paper_id": paper_id,
            "title": "Layout Sample",
            "source": "local_pdf",
            "extracted_at": "2026-07-21T00:00:00Z",
            "blocks": [
                {
                    "index": 0,
                    "type": "paragraph",
                    "original": "A reliable inline translation fixture.",
                    "status": "pending",
                },
                {
                    "index": 1,
                    "type": "paragraph",
                    "original": "This region must stay visible as original text.",
                    "status": "pending",
                },
                {
                    "index": 2,
                    "type": "paragraph",
                    "original": "This block intentionally has no PDF mapping.",
                    "status": "pending",
                },
            ],
        }
        mapping = json.loads(
            (FIXTURE_DIR / "legacy_pdf_map.json").read_text(encoding="utf-8")
        )
        (paper_dir / "translation.json").write_text(
            json.dumps(document), encoding="utf-8"
        )
        (paper_dir / "block_to_pdf_map.json").write_text(
            json.dumps(mapping), encoding="utf-8"
        )
        (paper_dir / "original.pdf").write_bytes(b"%PDF-audit-layout")
        return paper_dir

    @staticmethod
    def _save_layout(paper_dir: Path, layout) -> None:
        (paper_dir / "translation_layout.json").write_text(
            json.dumps(layout.model_dump(mode="json")),
            encoding="utf-8",
        )

    @classmethod
    def _write_precise_layout(
        cls,
        paper_dir: Path,
        *,
        source: str,
        adapter: str,
        eligible_count: int,
        safe_count: int,
        confidence: float,
        is_ocr: bool | None = None,
        originals: list[str] | None = None,
    ):
        if originals is not None and len(originals) != eligible_count:
            raise ValueError("originals must match eligible_count")
        document_data = {
            "paper_id": paper_dir.name,
            "title": "Layout Sample",
            "source": source,
            "extracted_at": "2026-07-21T00:00:00Z",
            "blocks": [
                {
                    "index": index,
                    "type": "paragraph",
                    "original": (
                        originals[index]
                        if originals is not None
                        else f"Eligible text block {index}."
                    ),
                    "status": "done",
                    "translation": f"合格译文 {index}。",
                }
                for index in range(eligible_count)
            ],
        }
        (paper_dir / "translation.json").write_text(
            json.dumps(document_data),
            encoding="utf-8",
        )
        mapping = {
            "pdf_url": "/assets/sample/original.pdf",
            "page_count": 1,
            "mappings": [
                {
                    "block_index": index,
                    "confidence": confidence,
                    "boxes": [
                        {
                            "page": 1,
                            "x0": 50,
                            "y0": 10 + index * 20,
                            "x1": 550,
                            "y1": 25 + index * 20,
                            "page_width": 600,
                            "page_height": 1000,
                        }
                    ],
                }
                for index in range(eligible_count)
            ],
        }
        document = PaperDocument.from_dict(document_data)
        adapter_version = (
            POPPLER_LAYOUT_ADAPTER_VERSION
            if adapter == POPPLER_LAYOUT_ADAPTER
            else MINERU_TRANSLATION_LAYOUT_ADAPTER_VERSION
        )
        layout = translation_layout_from_pdf_map(
            document.blocks,
            paper_dir / "original.pdf",
            mapping,
            adapter=adapter,
            adapter_version=adapter_version,
        )
        for index, region in enumerate(layout.regions):
            region.confidence = confidence
            if not region.line_boxes:
                region.line_boxes = [region.bbox.model_copy(deep=True)]
                region.source_line_orders = [0]
            if index < safe_count:
                region.render_policy = "replace"
                region.failure_reason = None
            else:
                region.render_policy = "panel_only"
                region.failure_reason = "low_confidence"
        layout.quality.mappable_count = eligible_count
        layout.quality.mapped_count = eligible_count
        layout.quality.replaceable_count = safe_count
        layout.quality.panel_only_count = eligible_count - safe_count
        layout.quality.unmapped_count = 0
        layout.quality.mapped_ratio = 1.0
        layout.quality.average_confidence = confidence
        layout.quality.unmapped_block_indexes = []
        layout.quality.failure_counts = (
            {"low_confidence": eligible_count - safe_count}
            if safe_count < eligible_count
            else {}
        )
        if is_ocr is not None:
            generation = "a" * 32
            layout = bind_mineru_layout_source(
                layout,
                generation=generation,
                is_ocr=is_ocr,
            )
            cls._write_mineru_artifact_generation(
                paper_dir,
                layout,
                [
                    {
                        "type": "text",
                        "text": block.original,
                        "page_idx": 0,
                        "bbox": [50, 10 + block.index * 20, 550, 25 + block.index * 20],
                    }
                    for block in document.blocks
                ],
                generation=generation,
                is_ocr=is_ocr,
            )
        cls._save_layout(paper_dir, layout)
        return layout

    @staticmethod
    def _write_mineru_artifact_generation(
        paper_dir: Path,
        layout,
        content_list: list[dict],
        *,
        generation: str,
        is_ocr: bool,
    ) -> None:
        metadata = {
            "adapter": MINERU_LAYOUT_ADAPTER,
            "adapter_version": MINERU_LAYOUT_ADAPTER_VERSION,
            "source_pdf_sha256": layout.source_pdf_sha256,
            "is_ocr": is_ocr,
            "generation": generation,
        }
        generation_dir = paper_dir / "mineru_layout_generations" / generation
        generation_dir.mkdir(parents=True, exist_ok=True)
        (generation_dir / "mineru_middle.json").write_text(
            json.dumps({"pdf_info": []}),
            encoding="utf-8",
        )
        (generation_dir / "mineru_content_list.json").write_text(
            json.dumps(content_list),
            encoding="utf-8",
        )
        (generation_dir / "meta.json").write_text(
            json.dumps({"schema_version": 2, **metadata}),
            encoding="utf-8",
        )
        (paper_dir / "mineru_layout_meta.json").write_text(
            json.dumps({"schema_version": 2, **metadata}),
            encoding="utf-8",
        )
        (paper_dir / "mineru_source_meta.json").write_text(
            json.dumps({"schema_version": 1, **metadata}),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
