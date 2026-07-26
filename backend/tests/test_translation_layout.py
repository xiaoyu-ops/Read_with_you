from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError
from rapidfuzz.distance import Indel as RapidFuzzIndel

from backend.extraction import translation_layout as translation_layout_module
from backend.extraction.blocks import Block, PaperDocument
from backend.extraction.mineru import (
    MINERU_LAYOUT_ADAPTER_VERSION,
    MinerUStructuredResult,
    markdown_to_blocks,
)
from backend.extraction.pdf_layout import (
    POPPLER_LAYOUT_ADAPTER_VERSION,
    PdfLayoutBlock,
    PdfLayoutBox,
    PdfLayoutDocument,
    PdfLayoutLine,
    PdfLayoutPage,
    PdfLayoutWord,
    extract_pdf_layout,
)
from backend.extraction.translation_layout import (
    HYBRID_LAYOUT_ADAPTER,
    HYBRID_LAYOUT_ADAPTER_VERSION,
    MINERU_TRANSLATION_LAYOUT_ADAPTER_VERSION,
    NormalizedBox,
    TranslationLayout,
    TranslationLayoutPage,
    _MinerUContentEntry,
    _match_blocks_to_mineru_content,
    _mineru_composite_entries,
    block_source_sha256,
    legacy_latex_extraction_debris_indexes,
    mappable_text_block_indexes,
    source_pdf_sha256,
    translation_layout_cache_matches,
    translation_layout_from_hybrid,
    translation_layout_from_mineru,
    translation_layout_from_pdf_layout,
    translation_layout_from_pdf_map,
)
from backend.storage import files as storage_files


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "translation_layout"
REAL_MINERU_DIR = (
    Path(__file__).parents[2] / "data" / "papers" / "mineru-10809ca1792d"
)
REAL_2104_DIR = Path(__file__).parents[2] / "data" / "papers" / "2104.08691"


class TranslationLayoutTest(unittest.TestCase):
    def test_legacy_map_conversion_is_normalized_stable_and_safe(self) -> None:
        mapping = json.loads(
            (FIXTURE_DIR / "legacy_pdf_map.json").read_text(encoding="utf-8")
        )
        blocks = _blocks()
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-layout-contract")

            first = translation_layout_from_pdf_map(blocks, pdf_path, mapping)
            second = translation_layout_from_pdf_map(blocks, pdf_path, mapping)

        self.assertEqual(first.model_dump(), second.model_dump())
        self.assertEqual(first.page_count, 2)
        self.assertEqual([page.page for page in first.pages], [1, 2])
        self.assertEqual(len(first.regions), 2)
        self.assertTrue(all(region.render_policy == "panel_only" for region in first.regions))
        self.assertEqual(first.regions[0].failure_reason, "legacy_mapping_unverified")
        self.assertEqual(first.regions[1].failure_reason, "low_confidence")
        self.assertAlmostEqual(first.regions[0].line_boxes[0].x0, 72 / 612)
        self.assertAlmostEqual(first.regions[0].line_boxes[0].y0, 90 / 792)
        self.assertTrue(all(region.word_boxes == [] for region in first.regions))
        self.assertTrue(
            all(region.source_block_order is None for region in first.regions)
        )
        self.assertTrue(all(region.source_line_orders == [] for region in first.regions))
        self.assertTrue(all(region.source_word_orders == [] for region in first.regions))
        self.assertEqual(first.quality.unmapped_block_indexes, [2])
        self.assertEqual(first.quality.replaceable_count, 0)
        self.assertEqual(first.quality.panel_only_count, 2)
        self.assertEqual(
            first.quality.failure_counts,
            {"legacy_mapping_unverified": 1, "low_confidence": 1},
        )

    def test_layout_region_source_order_fields_default_for_old_payloads(self) -> None:
        mapping = json.loads(
            (FIXTURE_DIR / "legacy_pdf_map.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-layout-contract")
            payload = translation_layout_from_pdf_map(
                _blocks(), pdf_path, mapping
            ).model_dump(mode="json")

        for region in payload["regions"]:
            region.pop("word_boxes")
            region.pop("source_block_order")
            region.pop("source_line_orders")
            region.pop("source_word_orders")

        restored = TranslationLayout.model_validate(payload)

        self.assertTrue(all(region.word_boxes == [] for region in restored.regions))
        self.assertTrue(
            all(region.source_block_order is None for region in restored.regions)
        )
        self.assertTrue(all(region.source_line_orders == [] for region in restored.regions))
        self.assertTrue(all(region.source_word_orders == [] for region in restored.regions))

    def test_block_fingerprint_ignores_translation_state(self) -> None:
        blocks = _blocks()
        initial = block_source_sha256(blocks)

        blocks[0].translation = "可靠的原位译文。"
        blocks[0].status = "done"
        self.assertEqual(block_source_sha256(blocks), initial)

        blocks[0].original = "Changed source text."
        self.assertNotEqual(block_source_sha256(blocks), initial)

    def test_cache_matches_translation_updates_but_not_source_changes(self) -> None:
        mapping = json.loads(
            (FIXTURE_DIR / "legacy_pdf_map.json").read_text(encoding="utf-8")
        )
        blocks = _blocks()
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-layout-contract")
            layout = translation_layout_from_pdf_map(blocks, pdf_path, mapping)
            data = layout.model_dump(mode="json")

            self.assertTrue(translation_layout_cache_matches(data, blocks, pdf_path))
            blocks[0].translation = "译文"
            blocks[0].status = "done"
            self.assertTrue(translation_layout_cache_matches(data, blocks, pdf_path))

            blocks[0].original = "Different source"
            self.assertFalse(translation_layout_cache_matches(data, blocks, pdf_path))
            blocks[0].original = "A reliable inline translation fixture."
            pdf_path.write_bytes(b"%PDF-layout-contract-changed")
            self.assertFalse(translation_layout_cache_matches(data, blocks, pdf_path))

    def test_invalid_normalized_boxes_and_rotation_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            NormalizedBox(x0=-0.1, y0=0.1, x1=0.2, y1=0.3)
        with self.assertRaises(ValidationError):
            NormalizedBox(x0=float("nan"), y0=0.1, x1=0.2, y1=0.3)
        with self.assertRaises(ValidationError):
            NormalizedBox(x0=0.1, y0=0.1, x1=float("inf"), y1=0.3)
        with self.assertRaises(ValidationError):
            NormalizedBox(x0=0.4, y0=0.1, x1=0.4, y1=0.3)
        with self.assertRaises(ValidationError):
            TranslationLayoutPage(page=1, width=612, height=792, rotation=45)

    def test_legacy_layout_debris_and_adjacent_structured_table_cells_are_not_mappable(
        self,
    ) -> None:
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
            Block(4, "paragraph", "Dedup20"),
            Block(5, "paragraph", "Real prose remains mappable."),
            Block(6, "paragraph", r"\appendix"),
            Block(7, "paragraph", r"\input{sec2-3_promptcuration}"),
            Block(
                8,
                "paragraph",
                r"Legacy figure prose \includegraphics{figures/result.pdf}",
            ),
            Block(9, "heading", r"\subsubsection{Case Studies}", level=3),
            Block(10, "heading", "Clean extracted heading", level=2),
        ]

        self.assertEqual(
            legacy_latex_extraction_debris_indexes(blocks),
            {6, 7, 8, 9},
        )
        self.assertEqual(mappable_text_block_indexes(blocks), {4, 5, 10})

    def test_storage_round_trip_is_atomic_and_schema_preserving(self) -> None:
        mapping = json.loads(
            (FIXTURE_DIR / "legacy_pdf_map.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            storage_files, "PAPERS_DIR", Path(tmp) / "papers"
        ):
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-layout-contract")
            layout = translation_layout_from_pdf_map(_blocks(), pdf_path, mapping)
            payload = layout.model_dump(mode="json")

            storage_files.save_translation_layout("layout-paper", payload)

            self.assertEqual(storage_files.load_translation_layout("layout-paper"), payload)
            self.assertFalse(
                list((Path(tmp) / "papers" / "layout-paper").glob("*.tmp"))
            )

    def test_mineru_artifacts_are_bound_to_the_source_pdf_fingerprint(self) -> None:
        layout = json.loads(
            (FIXTURE_DIR / "mineru_middle.json").read_text(encoding="utf-8")
        )
        content = json.loads(
            (FIXTURE_DIR / "mineru_content_list.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            storage_files, "PAPERS_DIR", Path(tmp) / "papers"
        ):
            storage_files.save_mineru_layout_artifacts(
                "mineru-paper",
                layout,
                content,
                source_pdf_sha256="a" * 64,
                is_ocr=True,
            )

            self.assertEqual(
                storage_files.load_mineru_layout_artifacts(
                    "mineru-paper",
                    expected_source_pdf_sha256="a" * 64,
                ),
                (layout, content),
            )
            self.assertIsNone(
                storage_files.load_mineru_layout_artifacts(
                    "mineru-paper",
                    expected_source_pdf_sha256="b" * 64,
                )
            )
            bundle = storage_files.load_mineru_layout_artifact_bundle(
                "mineru-paper",
                expected_source_pdf_sha256="a" * 64,
            )
            self.assertIsNotNone(bundle)
            assert bundle is not None
            self.assertEqual(bundle[:2], (layout, content))
            self.assertTrue(bundle[2]["is_ocr"])
            self.assertRegex(bundle[2]["generation"], r"^[0-9a-f]{32}$")
            with patch.object(
                storage_files,
                "_load_mineru_artifact_pair",
                side_effect=AssertionError("provenance must not load payload JSON"),
            ):
                provenance = storage_files.load_mineru_layout_provenance(
                    "mineru-paper",
                    expected_source_pdf_sha256="a" * 64,
                )
            self.assertIsNotNone(provenance)
            assert provenance is not None
            self.assertTrue(provenance["is_ocr"])
            self.assertIsNone(
                storage_files.load_mineru_layout_provenance(
                    "mineru-paper",
                    expected_source_pdf_sha256="b" * 64,
                )
            )
            self.assertEqual(
                storage_files.load_mineru_source_meta(
                    "mineru-paper",
                    expected_source_pdf_sha256="a" * 64,
                )["is_ocr"],
                True,
            )

    def test_mineru_generation_switch_never_exposes_same_hash_mixed_artifacts(self) -> None:
        old_layout = {"pdf_info": [{"page_idx": 0, "generation": "old"}]}
        old_content = [{"page_idx": 0, "type": "text", "generation": "old"}]
        new_layout = {"pdf_info": [{"page_idx": 0, "generation": "new"}]}
        new_content = [{"page_idx": 0, "type": "text", "generation": "new"}]
        source_hash = "c" * 64

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            storage_files, "PAPERS_DIR", Path(tmp) / "papers"
        ):
            storage_files.save_mineru_layout_artifacts(
                "mineru-paper",
                old_layout,
                old_content,
                source_pdf_sha256=source_hash,
                is_ocr=False,
            )
            before = storage_files.load_mineru_layout_artifact_bundle(
                "mineru-paper",
                expected_source_pdf_sha256=source_hash,
            )
            self.assertIsNotNone(before)
            assert before is not None

            original_write_json = storage_files._write_json

            def interrupt_manifest(path: Path, data) -> None:
                if path.name == "mineru_layout_meta.json":
                    raise OSError("simulated manifest interruption")
                original_write_json(path, data)

            with patch.object(
                storage_files,
                "_write_json",
                side_effect=interrupt_manifest,
            ):
                with self.assertRaisesRegex(OSError, "manifest interruption"):
                    storage_files.save_mineru_layout_artifacts(
                        "mineru-paper",
                        new_layout,
                        new_content,
                        source_pdf_sha256=source_hash,
                        is_ocr=True,
                    )

            after = storage_files.load_mineru_layout_artifact_bundle(
                "mineru-paper",
                expected_source_pdf_sha256=source_hash,
            )
            self.assertIsNotNone(after)
            assert after is not None
            self.assertEqual(after[0], old_layout)
            self.assertEqual(after[1], old_content)
            self.assertEqual(after[2]["generation"], before[2]["generation"])
            source_meta = storage_files.load_mineru_source_meta(
                "mineru-paper",
                expected_source_pdf_sha256=source_hash,
            )
            self.assertIsNotNone(source_meta)
            assert source_meta is not None
            self.assertEqual(source_meta["generation"], before[2]["generation"])
            self.assertFalse(source_meta["is_ocr"])
            generation_root = (
                Path(tmp) / "papers" / "mineru-paper" / "mineru_layout_generations"
            )
            self.assertFalse(list(generation_root.glob(".*.tmp")))

            storage_files.save_mineru_layout_artifacts(
                "mineru-paper",
                new_layout,
                new_content,
                source_pdf_sha256=source_hash,
                is_ocr=True,
            )
            committed = storage_files.load_mineru_layout_artifact_bundle(
                "mineru-paper",
                expected_source_pdf_sha256=source_hash,
            )
            self.assertIsNotNone(committed)
            assert committed is not None
            self.assertEqual(committed[0], new_layout)
            self.assertEqual(committed[1], new_content)
            self.assertNotEqual(committed[2]["generation"], before[2]["generation"])

    def test_mineru_legacy_artifacts_have_unknown_provenance(self) -> None:
        layout = {"pdf_info": [{"page_idx": 0}]}
        content = [{"page_idx": 0, "type": "text"}]
        source_hash = "d" * 64

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            storage_files, "PAPERS_DIR", Path(tmp) / "papers"
        ):
            paper_path = storage_files.ensure_paper_dir("legacy-mineru")
            storage_files._write_json(paper_path / "mineru_middle.json", layout)
            storage_files._write_json(paper_path / "mineru_content_list.json", content)
            storage_files._write_json(
                paper_path / "mineru_layout_meta.json",
                {
                    "adapter": "mineru_middle",
                    "adapter_version": "1",
                    "source_pdf_sha256": source_hash,
                },
            )

            bundle = storage_files.load_mineru_layout_artifact_bundle(
                "legacy-mineru",
                expected_source_pdf_sha256=source_hash,
            )

            self.assertIsNotNone(bundle)
            assert bundle is not None
            self.assertEqual(bundle[:2], (layout, content))
            self.assertIsNone(bundle[2]["is_ocr"])
            self.assertIsNone(bundle[2]["generation"])
            self.assertEqual(
                storage_files.load_mineru_layout_artifacts(
                    "legacy-mineru",
                    expected_source_pdf_sha256=source_hash,
                ),
                (layout, content),
            )

    def test_mineru_generation_storage_is_bounded_and_keeps_current(self) -> None:
        source_hash = "f" * 64
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            storage_files,
            "PAPERS_DIR",
            Path(tmp) / "papers",
        ):
            for generation_number in range(6):
                storage_files.save_mineru_layout_artifacts(
                    "bounded-mineru",
                    {"pdf_info": [{"page_idx": 0, "value": generation_number}]},
                    [{"page_idx": 0, "type": "text", "value": generation_number}],
                    source_pdf_sha256=source_hash,
                    is_ocr=False,
                )

            bundle = storage_files.load_mineru_layout_artifact_bundle(
                "bounded-mineru",
                expected_source_pdf_sha256=source_hash,
            )
            self.assertIsNotNone(bundle)
            assert bundle is not None
            self.assertEqual(bundle[0]["pdf_info"][0]["value"], 5)
            generation_root = (
                Path(tmp)
                / "papers"
                / "bounded-mineru"
                / "mineru_layout_generations"
            )
            generations = [entry for entry in generation_root.iterdir() if entry.is_dir()]
            self.assertLessEqual(len(generations), 3)
            self.assertTrue((generation_root / bundle[2]["generation"]).is_dir())

    def test_mineru_ocr_provenance_survives_layout_artifact_invalidation(self) -> None:
        source_hash = "e" * 64
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            storage_files, "PAPERS_DIR", Path(tmp) / "papers"
        ):
            storage_files.save_mineru_layout_artifacts(
                "scanned-mineru",
                {"pdf_info": [{"page_idx": 0}]},
                [{"page_idx": 0, "type": "text"}],
                source_pdf_sha256=source_hash,
                is_ocr=True,
            )
            meta_path = (
                Path(tmp) / "papers" / "scanned-mineru" / "mineru_layout_meta.json"
            )
            meta_path.unlink()

            self.assertIsNone(
                storage_files.load_mineru_layout_artifact_bundle(
                    "scanned-mineru",
                    expected_source_pdf_sha256=source_hash,
                )
            )
            source_meta = storage_files.load_mineru_source_meta(
                "scanned-mineru",
                expected_source_pdf_sha256=source_hash,
            )
            self.assertIsNotNone(source_meta)
            assert source_meta is not None
            self.assertTrue(source_meta["is_ocr"])

    def test_future_adapter_fixtures_cover_complex_and_scanned_documents(self) -> None:
        mineru = json.loads((FIXTURE_DIR / "mineru_middle.json").read_text(encoding="utf-8"))
        content = json.loads(
            (FIXTURE_DIR / "mineru_content_list.json").read_text(encoding="utf-8")
        )
        scanned = json.loads((FIXTURE_DIR / "scanned_middle.json").read_text(encoding="utf-8"))
        scanned_content = json.loads(
            (FIXTURE_DIR / "scanned_content_list.json").read_text(encoding="utf-8")
        )

        first_page = mineru["pdf_info"][0]
        span_types = {
            span["type"]
            for block in first_page["para_blocks"]
            for line in block["lines"]
            for span in line["spans"]
        }
        self.assertEqual(first_page["page_size"], [612, 792])
        self.assertIn("inline_equation", span_types)
        self.assertTrue(first_page["tables"])
        self.assertTrue(first_page["images"])
        self.assertEqual([item["type"] for item in content[-2:]], ["table", "image"])
        self.assertEqual([page["page_idx"] for page in scanned["pdf_info"]], [0, 1])
        self.assertEqual([item["page_idx"] for item in scanned_content], [0, 1])

    def test_poppler_layout_conversion_is_stable_normalized_and_replaceable(self) -> None:
        blocks = _blocks()[:2]
        document = _poppler_document()
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-poppler-layout")

            first = translation_layout_from_pdf_layout(blocks, pdf_path, document)
            second = translation_layout_from_pdf_layout(blocks, pdf_path, document)
            stale = first.model_dump(mode="json")
            stale["adapter_version"] = "13"
            self.assertFalse(
                translation_layout_cache_matches(stale, blocks, pdf_path)
            )

        self.assertEqual(first.model_dump(), second.model_dump())
        self.assertEqual(first.adapter, "poppler_bbox_layout")
        self.assertEqual(first.quality.mapped_ratio, 1)
        self.assertEqual(first.quality.average_confidence, 1)
        self.assertEqual(first.quality.replaceable_count, 2)
        self.assertTrue(all(region.render_policy == "replace" for region in first.regions))
        self.assertTrue(
            all(
                0 <= value <= 1
                for region in first.regions
                for box in [region.bbox, *region.line_boxes, *region.word_boxes]
                for value in (box.x0, box.y0, box.x1, box.y1)
            )
        )
        self.assertEqual(
            [region.source_block_order for region in first.regions],
            [0, 1],
        )
        self.assertEqual(
            [region.source_line_orders for region in first.regions],
            [[0], [1]],
        )
        self.assertEqual(first.regions[0].source_word_orders, list(range(5)))
        self.assertEqual(first.regions[1].source_word_orders, list(range(5, 13)))
        self.assertEqual(len(first.regions[0].word_boxes), 5)
        self.assertAlmostEqual(first.regions[0].word_boxes[0].x0, 72 / 612)
        self.assertAlmostEqual(first.regions[0].word_boxes[0].y0, 80 / 792)

    def test_poppler_layout_rejects_rotated_pages(self) -> None:
        document = _poppler_document(rotation=90)
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-rotated-layout")

            with self.assertRaisesRegex(ValueError, "rotation_unsupported"):
                translation_layout_from_pdf_layout(_blocks()[:2], pdf_path, document)

    def test_poppler_layout_does_not_reuse_one_pdf_span_for_duplicate_blocks(self) -> None:
        blocks = [
            Block(0, "paragraph", "A reliable inline translation fixture."),
            Block(1, "paragraph", "A reliable inline translation fixture."),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-poppler-layout")
            converted = translation_layout_from_pdf_layout(
                blocks,
                pdf_path,
                _poppler_document(),
            )

        self.assertEqual(len(converted.regions), 1)
        self.assertEqual(converted.quality.mapped_ratio, 0.5)
        self.assertEqual(converted.quality.unmapped_block_indexes, [1])

    def test_poppler_protected_match_does_not_advance_the_prose_cursor(self) -> None:
        blocks = [
            Block(0, "paragraph", "Opening prose appears first."),
            Block(1, "table", "Protected table appears at the document end."),
            Block(2, "paragraph", "Following prose remains before the table."),
        ]
        document = _poppler_document(
            texts=[
                "Opening prose appears first.",
                "Following prose remains before the table.",
                "Protected table appears at the document end.",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-protected-cursor")
            converted = translation_layout_from_pdf_layout(
                blocks,
                pdf_path,
                document,
            )

        self.assertEqual(
            [region.block_index for region in converted.regions],
            [0, 1, 2],
        )
        self.assertEqual(
            [region.source_block_order for region in converted.regions],
            [0, 2, 1],
        )
        self.assertEqual(converted.quality.mapped_ratio, 1)
        self.assertEqual(converted.quality.unmapped_block_indexes, [])

    def test_poppler_layout_does_not_reuse_tokens_across_protected_and_prose_blocks(
        self,
    ) -> None:
        shared = "Shared source words belong to one region only."
        blocks = [
            Block(0, "table", shared),
            Block(1, "paragraph", shared),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-cross-kind-token-reuse")
            converted = translation_layout_from_pdf_layout(
                blocks,
                pdf_path,
                _poppler_document(texts=[shared]),
            )

        self.assertEqual(len(converted.regions), 1)
        self.assertEqual(converted.regions[0].block_index, 0)
        self.assertEqual(converted.quality.mapped_ratio, 0)
        self.assertEqual(converted.quality.unmapped_block_indexes, [1])

    def test_poppler_structured_figure_does_not_claim_caption_text_geometry(
        self,
    ) -> None:
        caption = (
            "Figure A12: Comparison of perplexity values for 125M OPT model after "
            "pruning via different methods at 96% pruning. Note that the reference "
            "method pruned examples, while Random and SemDeDup prune examples. Mean "
            "and standard deviation are provided across three training seeds. Note "
            "that the Baseline column does not prune data, which is why the "
            "perplexities are lower, and bolded numbers compare between methods."
        )
        blocks = [
            Block(
                0,
                "figure",
                json.dumps({"images": [], "caption": caption}),
            ),
            Block(1, "paragraph", caption),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-structured-figure-caption")
            converted = translation_layout_from_pdf_layout(
                blocks,
                pdf_path,
                _poppler_document(
                    texts=[
                        "Unrelated preface tokens before caption",
                        *[
                        " ".join(caption.split()[start : start + 10])
                        for start in range(0, len(caption.split()), 10)
                        ],
                    ]
                ),
            )

        self.assertTrue(converted.regions)
        self.assertEqual({region.block_index for region in converted.regions}, {1})
        self.assertTrue(
            all(region.render_policy == "replace" for region in converted.regions)
        )
        self.assertEqual(converted.quality.protected_count, 0)
        self.assertEqual(converted.quality.mapped_ratio, 1)

    def test_poppler_short_structured_subcaption_does_not_guess_repeated_text(
        self,
    ) -> None:
        caption = "(a) Prompt length"
        blocks = [
            Block(0, "paragraph", "Earlier prose uses a prompt length in context."),
            Block(
                1,
                "figure",
                json.dumps({"images": ["plot.png"], "caption": caption}),
            ),
            Block(2, "paragraph", caption),
        ]
        document = _poppler_document(
            texts=[
                "Earlier prose uses a prompt length in context.",
                caption,
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-short-subcaption")
            converted = translation_layout_from_pdf_layout(
                blocks,
                pdf_path,
                document,
            )

        self.assertEqual(
            {region.block_index for region in converted.regions},
            {0},
        )
        self.assertEqual(converted.quality.unmapped_block_indexes, [2])

    def test_poppler_panel_candidate_does_not_advance_the_prose_cursor(self) -> None:
        blocks = [
            Block(0, "paragraph", "Opening trusted anchor words."),
            Block(1, "paragraph", "Approximate source text for a panel."),
            Block(2, "paragraph", "Later exact prose remains mappable."),
        ]
        document = _poppler_document(
            texts=[
                "Opening trusted anchor words.",
                "Later exact prose remains mappable.",
                "Approximate candidate text for the panel.",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-panel-cursor")
            converted = translation_layout_from_pdf_layout(
                blocks,
                pdf_path,
                document,
            )

        regions = {region.block_index: region for region in converted.regions}
        self.assertEqual(regions[1].render_policy, "panel_only")
        self.assertEqual(regions[2].render_policy, "replace")
        self.assertEqual(regions[2].source_block_order, 1)
        self.assertEqual(converted.quality.mapped_ratio, 1)

    def test_poppler_unique_exact_before_cursor_recovers_without_rewinding(self) -> None:
        heading = "6.1 Number of k-means clusters for SemDeDup"
        blocks = [
            Block(0, "paragraph", "Anchor appears after the true heading."),
            Block(1, "heading", heading, level=2),
            Block(2, "paragraph", "Following prose remains at the original cursor."),
        ]
        document = _poppler_document(
            texts=[
                heading,
                "Anchor appears after the true heading.",
                "Following prose remains at the original cursor.",
                "A.1 Number of k-means clusters for SemDeDup",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-global-exact-recovery")
            converted = translation_layout_from_pdf_layout(
                blocks,
                pdf_path,
                document,
            )

        regions = {region.block_index: region for region in converted.regions}
        self.assertEqual(regions[1].source_block_order, 0)
        self.assertEqual(regions[2].source_block_order, 2)
        self.assertEqual(converted.quality.unmapped_block_indexes, [])

    def test_poppler_short_exact_before_cursor_stays_a_single_line_panel(self) -> None:
        blocks = [
            Block(0, "paragraph", "Anchor appears after the short text."),
            Block(1, "paragraph", "Brief result"),
        ]
        document = _poppler_document(
            texts=["Brief", "result", "Anchor appears after the short text."],
            x_positions=[72, 112, 72],
            y_positions=[80, 80, 120],
        )
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-short-exact-corroboration")
            converted = translation_layout_from_pdf_layout(
                blocks,
                pdf_path,
                document,
            )

        region = next(
            region for region in converted.regions if region.block_index == 1
        )
        self.assertEqual(region.render_policy, "panel_only")
        self.assertEqual(region.failure_reason, "source_order_unverified_exact")
        self.assertEqual(region.confidence, 1.0)
        self.assertEqual(len(region.line_boxes), 1)
        self.assertEqual(len(region.word_boxes), 2)
        self.assertEqual(region.source_word_orders, [0, 1])

    def test_poppler_repeated_exact_before_cursor_rejects_remote_fuzzy_match(
        self,
    ) -> None:
        heading = "6.1 Number of k-means clusters for SemDeDup"
        blocks = [
            Block(0, "paragraph", "Anchor appears after both repeated headings."),
            Block(1, "heading", heading, level=2),
            Block(2, "paragraph", "Following prose keeps the established cursor."),
        ]
        document = _poppler_document(
            texts=[
                heading,
                heading,
                "Anchor appears after both repeated headings.",
                "Following prose keeps the established cursor.",
                "A.1 Number of k-means clusters for SemDeDup",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-ambiguous-global-exact")
            converted = translation_layout_from_pdf_layout(
                blocks,
                pdf_path,
                document,
            )

        regions = {region.block_index: region for region in converted.regions}
        self.assertNotIn(1, regions)
        self.assertEqual(regions[2].source_block_order, 3)
        self.assertEqual(converted.quality.unmapped_block_indexes, [1])

    def test_poppler_panel_candidate_does_not_reserve_tokens_from_a_replace(
        self,
    ) -> None:
        pdf_text = "Approximate candidate text for the panel."
        blocks = [
            Block(0, "paragraph", "Approximate source text for a panel."),
            Block(1, "paragraph", pdf_text),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-panel-token-priority")
            converted = translation_layout_from_pdf_layout(
                blocks,
                pdf_path,
                _poppler_document(texts=[pdf_text]),
            )

        self.assertEqual(
            [region.block_index for region in converted.regions],
            [1],
        )
        self.assertEqual(converted.regions[0].render_policy, "replace")
        self.assertEqual(converted.quality.unmapped_block_indexes, [0])

    def test_poppler_layout_splits_one_source_block_across_pdf_flows(self) -> None:
        source = "Left flow text continues in the right flow region."
        document = _poppler_split_flow_document()
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-poppler-split-flow")
            converted = translation_layout_from_pdf_layout(
                [Block(0, "paragraph", source)],
                pdf_path,
                document,
            )

        self.assertEqual(len(converted.regions), 2)
        self.assertEqual([region.flow_order for region in converted.regions], [0, 1])
        self.assertEqual([region.page for region in converted.regions], [1, 1])
        self.assertEqual(
            [region.source_block_order for region in converted.regions], [0, 1]
        )
        self.assertEqual(
            [region.source_line_orders for region in converted.regions], [[0], [1]]
        )
        self.assertEqual(
            [region.source_word_orders for region in converted.regions],
            [list(range(4)), list(range(4, 9))],
        )
        self.assertLess(converted.regions[0].bbox.x1, converted.regions[1].bbox.x0)

    def test_poppler_layout_splits_one_source_block_across_pages(self) -> None:
        source = "The paragraph starts on page one and continues on page two."
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-poppler-cross-page")
            converted = translation_layout_from_pdf_layout(
                [Block(0, "paragraph", source)],
                pdf_path,
                _poppler_cross_page_document(),
            )

        self.assertEqual(len(converted.regions), 2)
        self.assertEqual([region.page for region in converted.regions], [1, 2])
        self.assertEqual([region.flow_order for region in converted.regions], [0, 1])
        self.assertEqual(converted.quality.mapped_ratio, 1)

    def test_poppler_bracket_citations_keep_geometry_and_exact_numeric_evidence(
        self,
    ) -> None:
        source = "Alpha [11,12] beta remains complete."
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-poppler-bracket-citation")
            converted = translation_layout_from_pdf_layout(
                [Block(0, "paragraph", source)],
                pdf_path,
                _poppler_document(texts=["Alpha [11, 12] beta remains complete."]),
            )

        self.assertEqual(len(converted.regions), 1)
        region = converted.regions[0]
        self.assertEqual(region.render_policy, "replace")
        self.assertEqual(region.failure_reason, None)
        self.assertEqual(len(region.word_boxes), 6)
        self.assertEqual(region.source_word_orders, list(range(6)))

    def test_poppler_bracket_citation_cannot_hide_missing_or_changed_prose(
        self,
    ) -> None:
        cases = (
            (
                "Alpha [11,12] beta remains complete and verified.",
                "Alpha [11, 12] beta remains complete.",
            ),
            ("Training takes 10 epochs.", "Training takes 11 epochs."),
            ("Alpha [11,12] beta gamma.", "Alpha [11, 12] extra beta gamma."),
            ("Alpha [11,12] beta gamma.", "Alpha [11, 13] beta gamma."),
            ("The interval [0,1] matters.", "The interval [0,2] matters."),
            ("The date [2024-01-01] matters.", "The date [2024-01-02] matters."),
        )
        for source, pdf_text in cases:
            with self.subTest(source=source, pdf_text=pdf_text):
                with tempfile.TemporaryDirectory() as tmp:
                    pdf_path = Path(tmp) / "original.pdf"
                    pdf_path.write_bytes(b"%PDF-poppler-citation-negative")
                    converted = translation_layout_from_pdf_layout(
                        [Block(0, "paragraph", source)],
                        pdf_path,
                        _poppler_document(texts=[pdf_text]),
                    )

                self.assertFalse(
                    any(
                        region.render_policy == "replace"
                        for region in converted.regions
                    )
                )

    def test_poppler_cited_duplicate_binds_only_the_exact_numeric_sentence(
        self,
    ) -> None:
        source = "Alpha [1] beta."
        for texts, expected_order in (
            ([source, "Alpha beta."], 0),
            (["Alpha beta.", source], 1),
        ):
            with self.subTest(texts=texts):
                with tempfile.TemporaryDirectory() as tmp:
                    pdf_path = Path(tmp) / "original.pdf"
                    pdf_path.write_bytes(b"%PDF-poppler-citation-duplicate")
                    converted = translation_layout_from_pdf_layout(
                        [Block(0, "paragraph", source)],
                        pdf_path,
                        _poppler_document(texts=texts),
                    )

                self.assertEqual(len(converted.regions), 1)
                region = converted.regions[0]
                self.assertEqual(region.source_block_order, expected_order)
                self.assertEqual(region.render_policy, "replace")
                self.assertEqual(len(region.word_boxes), 3)

    def test_poppler_leading_trailing_and_adjacent_citations_keep_own_geometry(
        self,
    ) -> None:
        blocks = [
            Block(0, "paragraph", "[1] Alpha beta [2]."),
            Block(1, "paragraph", "[3] Gamma delta [4]."),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-poppler-citation-boundaries")
            converted = translation_layout_from_pdf_layout(
                blocks,
                pdf_path,
                _poppler_document(texts=[block.original for block in blocks]),
            )

        self.assertEqual(len(converted.regions), 2)
        self.assertTrue(
            all(region.render_policy == "replace" for region in converted.regions)
        )
        self.assertEqual(
            [len(region.word_boxes) for region in converted.regions],
            [4, 4],
        )
        self.assertTrue(
            set(converted.regions[0].source_word_orders).isdisjoint(
                converted.regions[1].source_word_orders
            )
        )

    def test_poppler_cross_page_fuzzy_start_recovers_only_complete_source(
        self,
    ) -> None:
        source = (
            "In Fig. 7 we show the performance of SemDeDup versus random pruning "
            "on every validation set."
        )
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-poppler-cross-page-footer")
            converted = translation_layout_from_pdf_layout(
                [Block(0, "paragraph", source)],
                pdf_path,
                _poppler_cross_page_footer_document(include_leading_word=True),
            )

        self.assertEqual([region.page for region in converted.regions], [1, 2])
        self.assertEqual([region.flow_order for region in converted.regions], [0, 1])
        self.assertTrue(
            all(region.render_policy == "replace" for region in converted.regions)
        )
        self.assertNotIn(
            900,
            {
                order
                for region in converted.regions
                for order in region.source_word_orders
            },
        )

        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-poppler-cross-page-missing-prefix")
            incomplete = translation_layout_from_pdf_layout(
                [Block(0, "paragraph", source)],
                pdf_path,
                _poppler_cross_page_footer_document(include_leading_word=False),
            )

        self.assertFalse(
            any(region.render_policy == "replace" for region in incomplete.regions)
        )

    def test_poppler_cross_page_numeric_evidence_must_match_exactly(self) -> None:
        source = (
            "The interval [0,1] starts on page one and continues on page two."
        )
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-poppler-cross-page-number-mismatch")
            converted = translation_layout_from_pdf_layout(
                [Block(0, "paragraph", source)],
                pdf_path,
                _poppler_cross_page_document(
                    [
                        "The interval [0,2] starts on page one",
                        "and continues on page two.",
                    ]
                ),
            )

        self.assertFalse(
            any(region.render_policy == "replace" for region in converted.regions)
        )

    def test_poppler_layout_maps_one_word_heading_and_counts_it_in_quality(self) -> None:
        word = PdfLayoutWord(
            text="Abstract",
            bbox=PdfLayoutBox(x0=72, y0=80, x1=128, y1=94),
            reading_order=0,
        )
        line = PdfLayoutLine(bbox=word.bbox, words=(word,), reading_order=0)
        document = PdfLayoutDocument(
            pages=(
                PdfLayoutPage(
                    page=1,
                    width=612,
                    height=792,
                    rotation=0,
                    blocks=(
                        PdfLayoutBlock(
                            bbox=line.bbox,
                            lines=(line,),
                            flow_index=0,
                            reading_order=0,
                        ),
                    ),
                ),
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-short-heading")
            converted = translation_layout_from_pdf_layout(
                [Block(0, "heading", "Abstract", level=1)],
                pdf_path,
                document,
            )

        self.assertEqual(converted.quality.mappable_count, 1)
        self.assertEqual(converted.quality.mapped_count, 1)
        self.assertEqual(converted.quality.mapped_ratio, 1)
        self.assertEqual(converted.regions[0].render_policy, "replace")

    def test_poppler_chinese_text_matches_word_or_character_pdf_segmentation(self) -> None:
        source = "中文案例支持原位翻译"
        self.assertEqual(
            translation_layout_module._normalize_layout_tokens("ＡＢＣ１２３中文"),
            ["abc123", "中", "文"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-cjk-tokenization")
            for pdf_text, expected_word_count in (
                (source, 1),
                (" ".join(source), len(source)),
            ):
                with self.subTest(pdf_text=pdf_text):
                    converted = translation_layout_from_pdf_layout(
                        [Block(0, "paragraph", source)],
                        pdf_path,
                        _poppler_document(texts=[pdf_text]),
                    )

                    self.assertEqual(len(converted.regions), 1)
                    region = converted.regions[0]
                    self.assertEqual(region.render_policy, "replace")
                    self.assertEqual(region.confidence, 1.0)
                    self.assertEqual(len(region.word_boxes), expected_word_count)

    def test_poppler_text_region_uses_only_matched_words_from_a_longer_line(self) -> None:
        texts = ["Matched", "source", "trailing", "evidence"]
        words = tuple(
            PdfLayoutWord(
                text=text,
                bbox=PdfLayoutBox(
                    x0=72 + index * 70,
                    y0=80,
                    x1=122 + index * 70,
                    y1=94,
                ),
                reading_order=index,
            )
            for index, text in enumerate(texts)
        )
        full_line = PdfLayoutLine(
            bbox=PdfLayoutBox(x0=72, y0=80, x1=332, y1=94),
            words=words,
            reading_order=0,
        )
        document = PdfLayoutDocument(
            pages=(
                PdfLayoutPage(
                    page=1,
                    width=612,
                    height=792,
                    rotation=0,
                    blocks=(
                        PdfLayoutBlock(
                            bbox=full_line.bbox,
                            lines=(full_line,),
                            flow_index=0,
                            reading_order=0,
                        ),
                    ),
                ),
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-partial-line")
            converted = translation_layout_from_pdf_layout(
                [Block(0, "paragraph", "Matched source")],
                pdf_path,
                document,
            )

        region = converted.regions[0]
        self.assertEqual(len(region.word_boxes), 2)
        self.assertAlmostEqual(region.line_boxes[0].x1, words[1].bbox.x1 / 612)
        self.assertLess(region.line_boxes[0].x1, full_line.bbox.x1 / 612)

    def test_poppler_run_in_paragraph_splits_first_line_from_later_lines(self) -> None:
        heading_words = (
            PdfLayoutWord(
                text="Evaluation.",
                bbox=PdfLayoutBox(x0=72, y0=80, x1=142, y1=94),
                reading_order=0,
            ),
        )
        paragraph_words = (
            PdfLayoutWord(
                text="The",
                bbox=PdfLayoutBox(x0=150, y0=80, x1=170, y1=94),
                reading_order=1,
            ),
            PdfLayoutWord(
                text="experiment",
                bbox=PdfLayoutBox(x0=176, y0=80, x1=238, y1=94),
                reading_order=2,
            ),
            PdfLayoutWord(
                text="continues",
                bbox=PdfLayoutBox(x0=72, y0=98, x1=126, y1=112),
                reading_order=3,
            ),
            PdfLayoutWord(
                text="on",
                bbox=PdfLayoutBox(x0=132, y0=98, x1=146, y1=112),
                reading_order=4,
            ),
            PdfLayoutWord(
                text="later",
                bbox=PdfLayoutBox(x0=152, y0=98, x1=182, y1=112),
                reading_order=5,
            ),
            PdfLayoutWord(
                text="lines.",
                bbox=PdfLayoutBox(x0=188, y0=98, x1=222, y1=112),
                reading_order=6,
            ),
        )
        lines = (
            PdfLayoutLine(
                bbox=PdfLayoutBox(x0=72, y0=80, x1=238, y1=94),
                words=(*heading_words, *paragraph_words[:2]),
                reading_order=0,
            ),
            PdfLayoutLine(
                bbox=PdfLayoutBox(x0=72, y0=98, x1=222, y1=112),
                words=paragraph_words[2:],
                reading_order=1,
            ),
        )
        document = PdfLayoutDocument(
            pages=(
                PdfLayoutPage(
                    page=1,
                    width=612,
                    height=792,
                    rotation=0,
                    blocks=(
                        PdfLayoutBlock(
                            bbox=PdfLayoutBox(x0=72, y0=80, x1=238, y1=112),
                            lines=lines,
                            flow_index=0,
                            reading_order=0,
                        ),
                    ),
                ),
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-run-in-heading")
            converted = translation_layout_from_pdf_layout(
                [
                    Block(0, "heading", "Evaluation.", level=3),
                    Block(1, "paragraph", "The experiment continues on later lines."),
                ],
                pdf_path,
                document,
            )

        heading = next(region for region in converted.regions if region.block_index == 0)
        paragraph = [
            region for region in converted.regions if region.block_index == 1
        ]
        self.assertEqual([region.flow_order for region in paragraph], [0, 1])
        self.assertEqual(
            [region.source_line_orders for region in paragraph],
            [[0], [1]],
        )
        self.assertEqual(
            [region.source_word_orders for region in paragraph],
            [[1, 2], [3, 4, 5, 6]],
        )
        self.assertGreaterEqual(paragraph[0].bbox.x0, heading.bbox.x1)
        self.assertGreater(paragraph[1].bbox.y0, heading.bbox.y1)

    def test_poppler_fuzzy_match_does_not_consume_the_next_block_boundary(self) -> None:
        blocks = [
            Block(
                0,
                "paragraph",
                "Similarly self attention see Figure 2 missing.",
            ),
            Block(1, "heading", "3.3 Position", level=2),
            Block(2, "paragraph", "Following exact prose remains mapped."),
        ]
        document = _poppler_document(
            texts=[
                "Similarly self attention see Figure 2",
                "3.3 Position",
                "Following exact prose remains mapped.",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-fuzzy-boundary")
            converted = translation_layout_from_pdf_layout(
                blocks,
                pdf_path,
                document,
            )

        regions = {region.block_index: region for region in converted.regions}
        self.assertEqual(set(regions), {0, 1, 2})
        self.assertTrue(
            all(
                len(
                    [
                        region
                        for region in converted.regions
                        if region.block_index == index
                    ]
                )
                == 1
                for index in regions
            )
        )
        self.assertEqual(regions[0].source_block_order, 0)
        self.assertEqual(regions[1].source_block_order, 1)
        self.assertEqual(regions[2].source_block_order, 2)
        self.assertEqual(regions[0].render_policy, "panel_only")
        self.assertEqual(
            regions[0].failure_reason,
            "incomplete_source_evidence",
        )
        self.assertEqual(regions[1].render_policy, "replace")
        self.assertEqual(regions[2].render_policy, "replace")
        self.assertLess(regions[0].bbox.y1, regions[1].bbox.y0)
        self.assertLess(regions[1].bbox.y1, regions[2].bbox.y0)
        self.assertTrue(
            set(regions[0].source_word_orders).isdisjoint(
                regions[1].source_word_orders
            )
        )

    def test_poppler_boundary_trim_rejects_too_little_token_evidence(self) -> None:
        blocks = [
            Block(
                0,
                "paragraph",
                "supercalifragilisticexpialidocious x",
            )
        ]
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-insufficient-token-evidence")
            converted = translation_layout_from_pdf_layout(
                blocks,
                pdf_path,
                _poppler_document(
                    texts=["supercalifragilisticexpialidocious unrelated"]
                ),
            )

        self.assertEqual(converted.regions, [])
        self.assertEqual(converted.quality.unmapped_block_indexes, [0])

    def test_poppler_sparse_equal_tokens_cannot_become_replaceable(self) -> None:
        leading = "x" * 200
        trailing = "z" * 200
        target = [
            leading,
            "alpha",
            "beta",
            "gamma",
            "delta",
            "epsilon",
            "theta",
            "iota",
            trailing,
        ]
        candidate = [
            leading,
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            trailing,
        ]

        match = translation_layout_module._match_layout_tokens(
            target,
            [SimpleNamespace(norm=token) for token in candidate],
            0,
            set(),
        )

        self.assertTrue(match is None or match[2] < 0.90)

    def test_poppler_common_anchor_bounds_fuzzy_candidates(self) -> None:
        target = ["model", *(f"missing{index}" for index in range(99))]
        tokens = [SimpleNamespace(norm="model") for _ in range(5_000)]
        calls = 0

        def counted_opcodes(source: list[str], candidate: list[str]):
            nonlocal calls
            calls += 1
            return RapidFuzzIndel.opcodes(source, candidate)

        with patch.object(
            translation_layout_module,
            "Indel",
            SimpleNamespace(opcodes=counted_opcodes),
        ):
            match = translation_layout_module._match_layout_tokens(
                target,
                tokens,
                0,
                set(),
            )

        self.assertIsNone(match)
        self.assertLessEqual(calls, 256 * 4)

    def test_poppler_candidate_cap_preserves_late_exact_order_match(self) -> None:
        target = ["model", "result", "model", "result"]
        norms = [item for _ in range(300) for item in ("model", "result", "noise")]
        exact_start = len(norms)
        norms.extend(target)
        tokens = [SimpleNamespace(norm=norm) for norm in norms]

        match = translation_layout_module._match_layout_tokens(
            target,
            tokens,
            0,
            set(),
        )

        self.assertEqual(match, (exact_start, exact_start + len(target), 1.0))

    def test_poppler_anchor_uses_a_present_token_instead_of_missing_prefix(self) -> None:
        target = ["color", "amapblue", "method"]
        tokens = [SimpleNamespace(norm="noise") for _ in range(1_000)]
        tokens.extend(
            [SimpleNamespace(norm="amapblue"), SimpleNamespace(norm="method")]
        )

        match = translation_layout_module._match_layout_tokens(
            target,
            tokens,
            0,
            set(),
        )

        self.assertIsNotNone(match)
        self.assertEqual(match[:2], (1_000, 1_002))
        self.assertLess(match[2], 0.90)

    def test_mineru_entry_inline_citations_and_percentages_do_not_protect_paragraph(
        self,
    ) -> None:
        paragraph_bbox = [100, 120, 900, 180]

        def protected_boxes(content: str) -> list[NormalizedBox]:
            return translation_layout_module._mineru_protected_boxes(
                {
                    "type": "text",
                    "bbox": paragraph_bbox,
                    "lines": [
                        {
                            "bbox": paragraph_bbox,
                            "spans": [
                                {
                                    "type": "inline_equation",
                                    "bbox": paragraph_bbox,
                                    "content": content,
                                }
                            ],
                        }
                    ],
                },
                1000,
                1000,
            )

        for content in ("[13]", r"50\%", r" +61.3 \% "):
            with self.subTest(unprotected=content):
                self.assertEqual(protected_boxes(content), [])

        expected = NormalizedBox(x0=0.1, y0=0.12, x1=0.9, y1=0.18)
        for content in ("L(x)", r"\epsilon", r"6 \times 10^{-5}"):
            with self.subTest(protected=content):
                self.assertEqual(protected_boxes(content), [expected])

    def test_mineru_layout_uses_middle_geometry_and_protects_non_text(self) -> None:
        layout = json.loads(
            (FIXTURE_DIR / "mineru_middle.json").read_text(encoding="utf-8")
        )
        content = json.loads(
            (FIXTURE_DIR / "mineru_content_list.json").read_text(encoding="utf-8")
        )
        blocks = [
            Block(0, "heading", "Layout-Aware Scientific Translation", level=1),
            Block(1, "paragraph", "The loss is $L(x)$"),
            Block(2, "paragraph", "The second column preserves reading order."),
            Block(3, "table", "| Model | Score |\n| --- | --- |\n| Pet | 0.98 |"),
            Block(4, "figure", "Figure 1: Protected visual evidence."),
        ]
        result = MinerUStructuredResult(
            markdown="fixture",
            blocks=blocks,
            layout=layout,
            content_list=content,
        )
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-mineru-layout")
            with patch(
                "backend.extraction.translation_layout._source_pdf_page_count",
                return_value=1,
            ):
                converted = translation_layout_from_mineru(blocks, pdf_path, result)

        self.assertEqual(converted.adapter, "mineru_middle")
        self.assertEqual(converted.page_count, 1)
        self.assertEqual(converted.quality.mapped_ratio, 1)
        paragraph = next(region for region in converted.regions if region.block_index == 1)
        table = next(region for region in converted.regions if region.block_index == 3)
        image_regions = [region for region in converted.regions if region.block_index == 4]
        image = next(region for region in image_regions if region.kind == "image")
        image_caption = next(
            region for region in image_regions if region.kind == "image_caption"
        )
        self.assertEqual(paragraph.render_policy, "panel_only")
        self.assertEqual(paragraph.failure_reason, "protected_overlap")
        self.assertTrue(paragraph.protected_boxes)
        self.assertEqual(paragraph.word_boxes, [])
        self.assertIsNone(paragraph.source_block_order)
        self.assertEqual(paragraph.source_line_orders, [])
        self.assertEqual(paragraph.source_word_orders, [])
        self.assertEqual(table.render_policy, "preserve")
        self.assertEqual(image.render_policy, "preserve")
        self.assertEqual(image_caption.render_policy, "panel_only")
        self.assertEqual(image_caption.failure_reason, "protected_overlap")
        self.assertTrue(table.protected_boxes)
        self.assertTrue(image.protected_boxes)
        self.assertEqual(image_caption.protected_boxes, image.protected_boxes)
        self.assertGreater(image_caption.bbox.y0, image.bbox.y1)

    def test_mineru_low_confidence_short_heading_does_not_jump_to_later_appendix(
        self,
    ) -> None:
        body = (
            "For CLIP evaluation we use zero-shot evaluation on thirty datasets "
            "and report the complete benchmark results."
        )
        entries = [
            _mineru_entry(f"CLIP Evaluation {body}"),
            *[_mineru_entry(f"Unrelated filler entry {index}.") for index in range(9)],
            _mineru_entry("B CLIP Zeroshot Evaluation"),
        ]
        matches = _match_blocks_to_mineru_content(
            [
                Block(0, "heading", "CLIP Evaluation", level=2),
                Block(1, "paragraph", body),
            ],
            entries,
        )

        self.assertNotIn(0, {match[0].index for match in matches})
        body_match = next(match for match in matches if match[0].index == 1)
        self.assertIs(body_match[1], entries[0])
        self.assertEqual(body_match[4], "merged_source_entry")

    def test_mineru_near_exact_short_heading_does_not_jump_across_sections(
        self,
    ) -> None:
        body = "Alpha beta gamma delta epsilon zeta eta theta iota kappa."
        entries = [
            _mineru_entry(body),
            *[_mineru_entry(f"Unrelated filler entry {index}.") for index in range(9)],
            _mineru_entry("A.1 Number of k-means Clusters for SemDeDup"),
        ]
        matches = _match_blocks_to_mineru_content(
            [
                Block(0, "heading", "6.1 Number of k-means clusters for SemDeDup", level=3),
                Block(1, "paragraph", body),
            ],
            entries,
        )

        self.assertNotIn(0, {match[0].index for match in matches})
        body_match = next(match for match in matches if match[0].index == 1)
        self.assertIs(body_match[1], entries[0])

    def test_mineru_caption_matches_float_geometry_outside_prose_order(self) -> None:
        entries = [
            _mineru_entry(
                "Figure 4: Parameter usage of adaptation techniques.",
                kind="chart_caption",
            ),
            _mineru_entry("Prose before the floated caption."),
            _mineru_entry("Prose after the floated caption."),
        ]
        matches = _match_blocks_to_mineru_content(
            [
                Block(0, "paragraph", "Prose before the floated caption."),
                Block(1, "paragraph", "Prose after the floated caption."),
                Block(
                    2,
                    "paragraph",
                    "Figure 4: Parameter usage of adaptation techniques.",
                ),
            ],
            entries,
        )
        first_entry_by_block = {match[0].index: match[1] for match in matches}

        self.assertIs(first_entry_by_block[0], entries[1])
        self.assertIs(first_entry_by_block[1], entries[2])
        self.assertIs(first_entry_by_block[2], entries[0])

    def test_mineru_figure_does_not_advance_the_prose_cursor(self) -> None:
        entries = [
            _mineru_entry("Opening prose appears first."),
            _mineru_entry("Following prose remains before the figure."),
            _mineru_entry("Figure evidence", kind="image", protected=True),
        ]
        matches = _match_blocks_to_mineru_content(
            [
                Block(0, "paragraph", "Opening prose appears first."),
                Block(1, "figure", "Figure evidence"),
                Block(2, "paragraph", "Following prose remains before the figure."),
            ],
            entries,
        )
        first_entry_by_block = {match[0].index: match[1] for match in matches}

        self.assertIs(first_entry_by_block[0], entries[0])
        self.assertIs(first_entry_by_block[2], entries[1])
        self.assertIs(first_entry_by_block[1], entries[2])

    def test_mineru_match_trims_geometry_entries_without_text_evidence(self) -> None:
        empty = _mineru_entry("")
        heading = _mineru_entry("A.3 Datasets")

        matches = _match_blocks_to_mineru_content(
            [Block(0, "heading", "A.3 Datasets", level=2)],
            [empty, heading],
        )

        self.assertEqual(len(matches), 1)
        self.assertIs(matches[0][1], heading)
        self.assertEqual(matches[0][3], 0)

    def test_mineru_low_confidence_candidate_does_not_reserve_replace_entry(
        self,
    ) -> None:
        exact = "Approximate candidate text for the panel."
        entries = [_mineru_entry(exact)]
        matches = _match_blocks_to_mineru_content(
            [
                Block(0, "paragraph", "Approximate source text for a panel."),
                Block(1, "paragraph", exact),
            ],
            entries,
        )

        self.assertEqual({match[0].index for match in matches}, {1})

    def test_mineru_merged_source_entry_is_panel_only(self) -> None:
        body = (
            "The evaluation paragraph contains enough words for reliable matching "
            "while retaining the original scientific meaning."
        )
        layout, content = _single_page_mineru_payload([f"Evaluation {body}"])
        blocks = [Block(0, "paragraph", body)]
        result = MinerUStructuredResult(
            markdown="fixture",
            blocks=blocks,
            layout=layout,
            content_list=content,
        )
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-mineru-merged-source")
            with patch(
                "backend.extraction.translation_layout._source_pdf_page_count",
                return_value=1,
            ):
                converted = translation_layout_from_mineru(blocks, pdf_path, result)

        self.assertEqual(converted.regions[0].render_policy, "panel_only")
        self.assertEqual(converted.regions[0].failure_reason, "merged_source_entry")
        self.assertEqual(converted.quality.replaceable_count, 0)

    def test_mineru_entries_are_not_reused_across_prose_and_protected_blocks(
        self,
    ) -> None:
        shared = "Shared source words belong to one MinerU entry only."
        entries = [_mineru_entry(shared)]
        matches = _match_blocks_to_mineru_content(
            [
                Block(0, "figure", shared),
                Block(1, "paragraph", shared),
            ],
            entries,
        )

        self.assertEqual({match[0].index for match in matches}, {1})

    def test_legacy_mineru_layout_cache_is_stale_but_raw_version_remains_v1(
        self,
    ) -> None:
        layout, content = _single_page_mineru_payload(["Stable MinerU paragraph."])
        blocks = [Block(0, "paragraph", "Stable MinerU paragraph.")]
        result = MinerUStructuredResult(
            markdown="fixture",
            blocks=blocks,
            layout=layout,
            content_list=content,
        )
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-mineru-cache-version")
            with patch(
                "backend.extraction.translation_layout._source_pdf_page_count",
                return_value=1,
            ):
                converted = translation_layout_from_mineru(blocks, pdf_path, result)
            stale = converted.model_dump(mode="json")
            stale["adapter_version"] = "7"

            self.assertFalse(
                translation_layout_cache_matches(stale, blocks, pdf_path)
            )

        self.assertEqual(MINERU_LAYOUT_ADAPTER_VERSION, "1")
        self.assertEqual(
            converted.adapter_version,
            MINERU_TRANSLATION_LAYOUT_ADAPTER_VERSION,
        )

    def test_real_mineru_markdown_image_path_is_not_a_translatable_layout_block(self) -> None:
        layout = json.loads(
            (FIXTURE_DIR / "mineru_middle.json").read_text(encoding="utf-8")
        )
        content = json.loads(
            (FIXTURE_DIR / "mineru_content_list.json").read_text(encoding="utf-8")
        )
        markdown = """
# Layout-Aware Scientific Translation

The loss is $L(x)$

The second column preserves reading order.

| Model | Score |
| --- | --- |
| Pet | 0.98 |

![](images/figure-1.jpg)

Figure 1: Protected visual evidence.
""".strip()
        blocks = markdown_to_blocks(markdown)
        result = MinerUStructuredResult(
            markdown=markdown,
            blocks=blocks,
            layout=layout,
            content_list=content,
        )
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-mineru-markdown-layout")
            with patch(
                "backend.extraction.translation_layout._source_pdf_page_count",
                return_value=1,
            ):
                converted = translation_layout_from_mineru(blocks, pdf_path, result)

        image_block = next(block for block in blocks if block.type == "figure")
        caption_block = next(
            block
            for block in blocks
            if block.type == "paragraph" and block.original.startswith("Figure 1")
        )
        self.assertEqual(image_block.status, "skip")
        self.assertNotIn(
            image_block.index,
            converted.quality.unmapped_block_indexes,
        )
        self.assertEqual(converted.quality.mapped_ratio, 1)
        self.assertTrue(
            any(
                region.block_index == caption_block.index
                and region.kind == "image_caption"
                and region.render_policy == "replace"
                and region.failure_reason is None
                for region in converted.regions
            )
        )

    def test_mineru_composite_regions_inherit_middle_rotation(self) -> None:
        layout = json.loads(
            (FIXTURE_DIR / "mineru_middle.json").read_text(encoding="utf-8")
        )
        image = layout["pdf_info"][0]["images"][0]
        image["angle"] = 90
        for child in image["blocks"]:
            child["angle"] = 90
        content = json.loads(
            (FIXTURE_DIR / "mineru_content_list.json").read_text(encoding="utf-8")
        )
        blocks = [Block(0, "figure", "Figure 1: Protected visual evidence.")]
        result = MinerUStructuredResult(
            markdown="fixture",
            blocks=blocks,
            layout=layout,
            content_list=[content[-1]],
        )
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-mineru-rotated-composite")
            with patch(
                "backend.extraction.translation_layout._source_pdf_page_count",
                return_value=1,
            ):
                converted = translation_layout_from_mineru(blocks, pdf_path, result)

        self.assertEqual(
            {region.rotation for region in converted.regions},
            {90},
        )

    def test_mineru_multiple_chart_captions_keep_separate_geometry(self) -> None:
        middle = {
            "type": "chart",
            "bbox": [100, 100, 900, 800],
            "blocks": [
                {
                    "type": "chart_caption",
                    "bbox": [100, 100, 900, 160],
                    "lines": [{"bbox": [100, 100, 900, 160]}],
                },
                {
                    "type": "chart_body",
                    "bbox": [100, 200, 900, 650],
                },
                {
                    "type": "chart_caption",
                    "bbox": [100, 680, 900, 740],
                    "lines": [{"bbox": [100, 680, 900, 740]}],
                },
            ],
        }
        entries = _mineru_composite_entries(
            {
                "type": "chart",
                "content": "protected chart body",
                "chart_caption": ["Caption above chart.", "Caption below chart."],
            },
            page_index=1,
            kind="chart",
            content_bbox=NormalizedBox(x0=0.1, y0=0.1, x1=0.9, y1=0.8),
            middle=middle,
            raw_page={"page_size": [1000, 1000]},
        )

        body = next(entry for entry in entries if entry.kind == "chart")
        captions = [entry for entry in entries if entry.kind == "chart_caption"]
        self.assertEqual(len(captions), 2)
        self.assertTrue(all(entry.authoritative for entry in captions))
        self.assertNotEqual(captions[0].bbox, captions[1].bbox)
        self.assertLessEqual(captions[0].bbox.y1, body.bbox.y0)
        self.assertGreaterEqual(captions[1].bbox.y0, body.bbox.y1)

    def test_mineru_layout_rejects_source_page_count_mismatch(self) -> None:
        layout = json.loads(
            (FIXTURE_DIR / "mineru_middle.json").read_text(encoding="utf-8")
        )
        content = json.loads(
            (FIXTURE_DIR / "mineru_content_list.json").read_text(encoding="utf-8")
        )
        result = MinerUStructuredResult(
            markdown="fixture",
            blocks=_blocks(),
            layout=layout,
            content_list=content,
        )
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-mineru-layout")
            with patch(
                "backend.extraction.translation_layout._source_pdf_page_count",
                return_value=2,
            ):
                with self.assertRaisesRegex(ValueError, "mineru_page_count_mismatch"):
                    translation_layout_from_mineru(_blocks(), pdf_path, result)

    def test_mineru_content_bbox_without_middle_geometry_is_not_counted_as_mapped(self) -> None:
        layout = json.loads(
            (FIXTURE_DIR / "mineru_middle.json").read_text(encoding="utf-8")
        )
        content = [
            {
                "type": "text",
                "text": "Layout-Aware Scientific Translation",
                "text_level": 1,
                "page_idx": 0,
                "bbox": [0, 0, 40, 40],
            }
        ]
        blocks = [Block(0, "heading", "Layout-Aware Scientific Translation", level=1)]
        result = MinerUStructuredResult(
            markdown="fixture",
            blocks=blocks,
            layout=layout,
            content_list=content,
        )
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-mineru-layout")
            with patch(
                "backend.extraction.translation_layout._source_pdf_page_count",
                return_value=1,
            ):
                converted = translation_layout_from_mineru(blocks, pdf_path, result)

        self.assertEqual(converted.regions[0].failure_reason, "middle_geometry_missing")
        self.assertEqual(converted.quality.mapped_ratio, 0)
        self.assertEqual(converted.quality.unmapped_block_indexes, [0])

    def test_poppler_region_requires_local_target_token_evidence(self) -> None:
        target = translation_layout_module._normalize_layout_tokens(
            "Li and Liang propose prefix tuning with task representations."
        )
        wrong = [
            SimpleNamespace(norm=token)
            for token in ("of", "task", "quality")
        ]
        continuation = [
            SimpleNamespace(norm=token)
            for token in ("with", "task", "representations")
        ]

        self.assertFalse(
            translation_layout_module._poppler_region_has_target_evidence(
                wrong,
                target,
            )
        )
        self.assertTrue(
            translation_layout_module._poppler_region_has_target_evidence(
                continuation,
                target,
            )
        )

    def test_poppler_replace_group_requires_whole_target_evidence(self) -> None:
        target = translation_layout_module._normalize_layout_tokens(
            "We train five prompts and compare the ensemble against the best prompt."
        )
        complete = [[SimpleNamespace(norm=token) for token in target]]
        near_complete = [[SimpleNamespace(norm=token) for token in target[:-1]]]
        split_target = ["competitive", "models"]
        split_complete = [
            [SimpleNamespace(norm=token) for token in ("com", "petitive", "models")]
        ]
        trailing_fragment = [
            [
                SimpleNamespace(norm=token)
                for token in translation_layout_module._normalize_layout_tokens(
                    "against the best prompt"
                )
            ]
        ]

        self.assertTrue(
            translation_layout_module._poppler_group_covers_target(
                complete,
                target,
            )
        )
        self.assertFalse(
            translation_layout_module._poppler_group_covers_target(
                near_complete,
                target,
            )
        )
        self.assertTrue(
            translation_layout_module._poppler_group_covers_target(
                split_complete,
                split_target,
            )
        )
        self.assertFalse(
            translation_layout_module._poppler_group_covers_target(
                trailing_fragment,
                target,
            )
        )

    def test_scanned_mineru_fixture_maps_both_pages_into_the_common_layout(self) -> None:
        layout = json.loads(
            (FIXTURE_DIR / "scanned_middle.json").read_text(encoding="utf-8")
        )
        content = json.loads(
            (FIXTURE_DIR / "scanned_content_list.json").read_text(encoding="utf-8")
        )
        blocks = [
            Block(0, "paragraph", "OCR text from scanned page one."),
            Block(1, "paragraph", "OCR text from scanned page two."),
        ]
        result = MinerUStructuredResult(
            markdown="fixture",
            blocks=blocks,
            layout=layout,
            content_list=content,
        )
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-scanned-layout")
            with patch(
                "backend.extraction.translation_layout._source_pdf_page_count",
                return_value=2,
            ):
                converted = translation_layout_from_mineru(blocks, pdf_path, result)

        self.assertEqual(converted.page_count, 2)
        self.assertEqual([region.page for region in converted.regions], [1, 2])
        self.assertEqual(converted.quality.mapped_ratio, 1)
        self.assertEqual(converted.quality.replaceable_count, 2)

    def test_mineru_page_wrap_geometry_stays_on_explicit_page_and_fails_closed(
        self,
    ) -> None:
        text = "A paragraph continues from the page bottom onto the next page top."
        middle_bbox = [100, 80, 900, 900]
        middle = {
            "pdf_info": [
                {
                    "page_idx": 0,
                    "page_size": [1000, 1000],
                    "para_blocks": [
                        {
                            "type": "text",
                            "bbox": middle_bbox,
                            "angle": 0,
                            "lines": [
                                {
                                    "bbox": [100, 800, 900, 900],
                                    "spans": [
                                        {
                                            "type": "text",
                                            "bbox": [100, 800, 900, 900],
                                            "content": text,
                                        }
                                    ],
                                },
                                {
                                    "bbox": [100, 80, 900, 160],
                                    "spans": [
                                        {
                                            "type": "text",
                                            "bbox": [100, 80, 900, 160],
                                            "content": text,
                                        }
                                    ],
                                },
                            ],
                        }
                    ],
                    "tables": [],
                    "images": [],
                    "interline_equations": [],
                    "discarded_blocks": [],
                },
                {
                    "page_idx": 1,
                    "page_size": [1000, 1000],
                    "para_blocks": [],
                    "tables": [],
                    "images": [],
                    "interline_equations": [],
                    "discarded_blocks": [],
                },
            ]
        }
        result = MinerUStructuredResult(
            markdown="fixture",
            blocks=[Block(0, "paragraph", text)],
            layout=middle,
            content_list=[
                {
                    "type": "text",
                    "text": text,
                    "page_idx": 0,
                    "bbox": middle_bbox,
                }
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-mineru-page-wrap")
            with patch(
                "backend.extraction.translation_layout._source_pdf_page_count",
                return_value=2,
            ):
                converted = translation_layout_from_mineru(
                    result.blocks,
                    pdf_path,
                    result,
                )

        self.assertEqual(len(converted.regions), 1)
        region = converted.regions[0]
        self.assertEqual(region.page, 1)
        self.assertEqual(region.flow_order, 0)
        self.assertEqual(region.block_index, 0)
        self.assertEqual(region.render_policy, "panel_only")
        self.assertEqual(region.failure_reason, "cross_page_geometry_ambiguous")
        self.assertEqual(len(region.line_boxes), 2)
        self.assertAlmostEqual(region.bbox.y0, 0.08)
        self.assertAlmostEqual(region.bbox.y1, 0.9)
        self.assertEqual(converted.quality.replaceable_count, 0)
        self.assertEqual(converted.quality.panel_only_count, 1)

    def test_hybrid_selects_one_complete_source_group_and_tracks_provenance(
        self,
    ) -> None:
        blocks = [Block(0, "paragraph", "One block spans two source pages.")]
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-hybrid-whole-block")
            poppler = _precise_layout_from_region_specs(
                blocks,
                pdf_path,
                [
                    (0, 1, 0.10, 0.10, 0.90, 0.20),
                    (0, 2, 0.10, 0.10, 0.90, 0.20),
                ],
                adapter="poppler_bbox_layout",
                adapter_version=POPPLER_LAYOUT_ADAPTER_VERSION,
                page_count=2,
            )
            poppler.regions[1].render_policy = "panel_only"
            poppler.regions[1].failure_reason = "low_confidence"
            mineru = _precise_layout_from_region_specs(
                blocks,
                pdf_path,
                [
                    (0, 1, 0.15, 0.25, 0.85, 0.35),
                    (0, 2, 0.15, 0.25, 0.85, 0.35),
                ],
                adapter="mineru_middle",
                adapter_version=MINERU_TRANSLATION_LAYOUT_ADAPTER_VERSION,
                page_count=2,
            )

            converted = translation_layout_from_hybrid(
                blocks,
                pdf_path,
                poppler,
                mineru,
                mineru_generation="a" * 32,
                mineru_is_ocr=False,
            )

            self.assertTrue(
                translation_layout_cache_matches(
                    converted.model_dump(mode="json"),
                    blocks,
                    pdf_path,
                )
            )
            tampered = converted.model_dump(mode="json")
            tampered["sources"][1]["generation"] = "b" * 32
            self.assertFalse(
                translation_layout_cache_matches(tampered, blocks, pdf_path)
            )
            stale_source = converted.model_dump(mode="json")
            stale_source["sources"][0]["adapter_version"] = "13"
            self.assertFalse(
                translation_layout_cache_matches(stale_source, blocks, pdf_path)
            )
            stale_mineru_source = converted.model_dump(mode="json")
            stale_mineru_source["sources"][1]["adapter_version"] = "7"
            self.assertFalse(
                translation_layout_cache_matches(
                    stale_mineru_source,
                    blocks,
                    pdf_path,
                )
            )
            stale_hybrid = converted.model_dump(mode="json")
            stale_hybrid["adapter_version"] = "14"
            self.assertFalse(
                translation_layout_cache_matches(stale_hybrid, blocks, pdf_path)
            )

        self.assertEqual(converted.adapter, HYBRID_LAYOUT_ADAPTER)
        self.assertEqual(converted.adapter_version, HYBRID_LAYOUT_ADAPTER_VERSION)
        self.assertEqual([region.page for region in converted.regions], [1, 2])
        self.assertEqual([region.flow_order for region in converted.regions], [0, 1])
        self.assertEqual(
            {region.geometry_source for region in converted.regions},
            {"mineru_middle"},
        )
        self.assertTrue(
            all(region.render_policy == "replace" for region in converted.regions)
        )
        self.assertEqual(
            [source.adapter for source in converted.sources],
            ["poppler_bbox_layout", "mineru_middle"],
        )
        self.assertEqual(converted.sources[1].generation, "a" * 32)
        self.assertFalse(converted.sources[1].is_ocr)

    def test_hybrid_page_protection_downgrades_overlapping_text(self) -> None:
        blocks = [Block(0, "paragraph", "Protected page geometry stays visible.")]
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-hybrid-page-protection")
            poppler = _precise_layout_from_region_specs(
                blocks,
                pdf_path,
                [(0, 1, 0.10, 0.10, 0.90, 0.20)],
                adapter="poppler_bbox_layout",
                adapter_version="3",
            )
            mineru = _precise_layout_from_region_specs(
                blocks,
                pdf_path,
                [(0, 1, 0.10, 0.10, 0.90, 0.20)],
                adapter="mineru_middle",
                adapter_version=MINERU_TRANSLATION_LAYOUT_ADAPTER_VERSION,
            )
            mineru.pages[0].protected_boxes = [
                poppler.regions[0].bbox.model_copy(deep=True)
            ]

            converted = translation_layout_from_hybrid(
                blocks,
                pdf_path,
                poppler,
                mineru,
                mineru_generation="c" * 32,
                mineru_is_ocr=False,
            )

        self.assertEqual(converted.pages[0].protected_boxes, mineru.pages[0].protected_boxes)
        self.assertEqual(converted.regions[0].render_policy, "panel_only")
        self.assertEqual(converted.regions[0].failure_reason, "protected_overlap")
        self.assertEqual(converted.quality.replaceable_count, 0)
        self.assertEqual(converted.quality.protected_overlap_count, 1)

    def test_hybrid_unsafe_fallback_keeps_precise_poppler_word_geometry(self) -> None:
        blocks = [Block(0, "paragraph", "Fallback text remains inspectable.")]
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-hybrid-precise-panel")
            poppler = _precise_layout_from_region_specs(
                blocks,
                pdf_path,
                [(0, 1, 0.10, 0.10, 0.90, 0.20)],
                adapter="poppler_bbox_layout",
                adapter_version=POPPLER_LAYOUT_ADAPTER_VERSION,
            )
            poppler_region = poppler.regions[0]
            poppler_region.word_boxes = [poppler_region.bbox.model_copy()]
            poppler_region.source_word_orders = [0]
            poppler_region.render_policy = "panel_only"
            poppler_region.failure_reason = "low_confidence"
            mineru = _precise_layout_from_region_specs(
                blocks,
                pdf_path,
                [(0, 1, 0.05, 0.05, 0.95, 0.25)],
                adapter="mineru_middle",
                adapter_version=MINERU_TRANSLATION_LAYOUT_ADAPTER_VERSION,
            )
            mineru.regions[0].render_policy = "panel_only"
            mineru.regions[0].failure_reason = "merged_source_entry"

            converted = translation_layout_from_hybrid(
                blocks,
                pdf_path,
                poppler,
                mineru,
                mineru_generation="a" * 32,
                mineru_is_ocr=False,
            )
            poppler_region.failure_reason = "incomplete_source_evidence"
            incomplete_converted = translation_layout_from_hybrid(
                blocks,
                pdf_path,
                poppler,
                mineru,
                mineru_generation="a" * 32,
                mineru_is_ocr=False,
            )

        self.assertEqual(len(converted.regions), 1)
        region = converted.regions[0]
        self.assertEqual(region.geometry_source, "poppler_bbox_layout")
        self.assertEqual(region.render_policy, "panel_only")
        self.assertEqual(region.failure_reason, "low_confidence")
        self.assertEqual(len(region.word_boxes), 1)
        self.assertEqual(len(incomplete_converted.regions), 1)
        self.assertEqual(
            incomplete_converted.regions[0].geometry_source,
            "mineru_middle",
        )
        self.assertEqual(
            incomplete_converted.regions[0].failure_reason,
            "merged_source_entry",
        )

    def test_hybrid_promotes_exact_poppler_words_corroborated_by_mineru(self) -> None:
        blocks = [Block(0, "paragraph", "Brief result")]
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-hybrid-exact-corroboration")
            poppler = _precise_layout_from_region_specs(
                blocks,
                pdf_path,
                [(0, 1, 0.10, 0.10, 0.30, 0.13)],
                adapter="poppler_bbox_layout",
                adapter_version=POPPLER_LAYOUT_ADAPTER_VERSION,
            )
            poppler_region = poppler.regions[0]
            poppler_region.word_boxes = [
                NormalizedBox(x0=0.10, y0=0.10, x1=0.17, y1=0.13),
                NormalizedBox(x0=0.19, y0=0.10, x1=0.30, y1=0.13),
            ]
            poppler_region.source_block_order = 4
            poppler_region.source_line_orders = [34]
            poppler_region.source_word_orders = [222, 223]
            poppler_region.render_policy = "panel_only"
            poppler_region.failure_reason = "source_order_unverified_exact"
            mineru = _precise_layout_from_region_specs(
                blocks,
                pdf_path,
                [(0, 1, 0.098, 0.098, 0.302, 0.132)],
                adapter="mineru_middle",
                adapter_version=MINERU_TRANSLATION_LAYOUT_ADAPTER_VERSION,
            )

            converted = translation_layout_from_hybrid(
                blocks,
                pdf_path,
                poppler,
                mineru,
                mineru_generation="b" * 32,
                mineru_is_ocr=False,
            )
            far_mineru = mineru.model_copy(deep=True)
            far_box = NormalizedBox(x0=0.50, y0=0.50, x1=0.70, y1=0.53)
            far_mineru.regions[0].bbox = far_box
            far_mineru.regions[0].line_boxes = [far_box]
            uncorroborated = translation_layout_from_hybrid(
                blocks,
                pdf_path,
                poppler,
                far_mineru,
                mineru_generation="b" * 32,
                mineru_is_ocr=False,
            )

        self.assertEqual(len(converted.regions), 1)
        region = converted.regions[0]
        self.assertEqual(region.geometry_source, "poppler_bbox_layout")
        self.assertEqual(region.render_policy, "replace")
        self.assertIsNone(region.failure_reason)
        self.assertEqual(len(region.line_boxes), 1)
        self.assertEqual(len(region.word_boxes), 2)
        self.assertEqual(uncorroborated.regions[0].geometry_source, "mineru_middle")
        self.assertEqual(uncorroborated.regions[0].word_boxes, [])

    def test_hybrid_near_duplicate_cross_source_geometry_fails_closed(self) -> None:
        blocks = [
            Block(0, "paragraph", "Trusted Poppler geometry."),
            Block(1, "paragraph", "Conflicting MinerU geometry."),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-hybrid-near-duplicate")
            poppler = _precise_layout_from_region_specs(
                blocks,
                pdf_path,
                [(0, 1, 0.13, 0.90, 0.47, 0.914)],
                adapter="poppler_bbox_layout",
                adapter_version=POPPLER_LAYOUT_ADAPTER_VERSION,
            )
            poppler.regions[0].word_boxes = [
                poppler.regions[0].bbox.model_copy(deep=True)
            ]
            poppler.regions[0].source_word_orders = [100]
            mineru = _precise_layout_from_region_specs(
                blocks,
                pdf_path,
                [(1, 1, 0.134, 0.902, 0.471, 0.913)],
                adapter="mineru_middle",
                adapter_version=MINERU_TRANSLATION_LAYOUT_ADAPTER_VERSION,
            )

            converted = translation_layout_from_hybrid(
                blocks,
                pdf_path,
                poppler,
                mineru,
                mineru_generation="d" * 32,
                mineru_is_ocr=False,
            )

        regions = {region.block_index: region for region in converted.regions}
        self.assertEqual(regions[0].render_policy, "replace")
        self.assertNotIn(1, regions)
        self.assertEqual(converted.quality.replaceable_count, 1)
        self.assertEqual(converted.quality.panel_only_count, 0)
        self.assertEqual(converted.quality.unmapped_block_indexes, [1])

    def test_hybrid_conflicting_middle_region_unmaps_the_whole_block(self) -> None:
        blocks = [
            Block(0, "paragraph", "MinerU block with a bad middle region."),
            Block(1, "paragraph", "Trusted neighboring Poppler block."),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-hybrid-middle-conflict")
            poppler = _precise_layout_from_region_specs(
                blocks,
                pdf_path,
                [(1, 1, 0.51, 0.21, 0.88, 0.26)],
                adapter="poppler_bbox_layout",
                adapter_version=POPPLER_LAYOUT_ADAPTER_VERSION,
            )
            poppler.regions[0].word_boxes = [
                poppler.regions[0].bbox.model_copy(deep=True)
            ]
            poppler.regions[0].source_word_orders = [200]
            mineru = _precise_layout_from_region_specs(
                blocks,
                pdf_path,
                [
                    (0, 1, 0.11, 0.10, 0.49, 0.18),
                    (0, 1, 0.507, 0.212, 0.884, 0.257),
                    (0, 1, 0.11, 0.30, 0.49, 0.38),
                ],
                adapter="mineru_middle",
                adapter_version=MINERU_TRANSLATION_LAYOUT_ADAPTER_VERSION,
            )

            converted = translation_layout_from_hybrid(
                blocks,
                pdf_path,
                poppler,
                mineru,
                mineru_generation="e" * 32,
                mineru_is_ocr=False,
            )

        self.assertFalse(
            any(region.block_index == 0 for region in converted.regions)
        )
        trusted = next(region for region in converted.regions if region.block_index == 1)
        self.assertEqual(trusted.render_policy, "replace")
        self.assertEqual(converted.quality.unmapped_block_indexes, [0])

    def test_hybrid_conflict_unmaps_the_whole_multi_region_block(self) -> None:
        blocks = [
            Block(0, "paragraph", "Body prose continues around a figure caption."),
            Block(1, "paragraph", "Fig. 1. Exact protected caption."),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-hybrid-partial-crop")
            poppler = _precise_layout_from_region_specs(
                blocks,
                pdf_path,
                [
                    (0, 1, 0.09, 0.50, 0.48, 0.89),
                    (0, 1, 0.527, 0.373, 0.922, 0.396),
                    (0, 1, 0.527, 0.413, 0.922, 0.62),
                ],
                adapter="poppler_bbox_layout",
                adapter_version=POPPLER_LAYOUT_ADAPTER_VERSION,
            )
            for index, region in enumerate(poppler.regions):
                region.word_boxes = [region.bbox.model_copy(deep=True)]
                region.source_block_order = 15 + index
                region.source_line_orders = [80 + index]
                region.source_word_orders = [900 + index]
            mineru = _precise_layout_from_region_specs(
                blocks,
                pdf_path,
                [
                    (1, 1, 0.526, 0.124, 0.917, 0.358),
                    (1, 1, 0.520, 0.369, 0.923, 0.396),
                ],
                adapter="mineru_middle",
                adapter_version=MINERU_TRANSLATION_LAYOUT_ADAPTER_VERSION,
            )
            mineru.regions[0].render_policy = "preserve"
            mineru.regions[0].failure_reason = "protected_content"
            mineru.regions[1].render_policy = "panel_only"
            mineru.regions[1].failure_reason = "protected_overlap"

            converted = translation_layout_from_hybrid(
                blocks,
                pdf_path,
                poppler,
                mineru,
                mineru_generation="1" * 32,
                mineru_is_ocr=False,
            )

        body_regions = [
            region for region in converted.regions if region.block_index == 0
        ]
        self.assertEqual(body_regions, [])
        self.assertIn(0, converted.quality.unmapped_block_indexes)
        self.assertTrue(
            any(region.block_index == 1 for region in converted.regions)
        )

    @unittest.skipUnless(
        shutil.which("pdftotext")
        and shutil.which("pdfinfo")
        and (REAL_MINERU_DIR / "original.pdf").is_file()
        and (REAL_MINERU_DIR / "translation.json").is_file(),
        "The local MinerU paper and Poppler are required",
    )
    def test_real_mineru_block_12_drops_only_the_figure_caption_region(self) -> None:
        document = PaperDocument.from_dict(
            json.loads(
                (REAL_MINERU_DIR / "translation.json").read_text(encoding="utf-8")
            )
        )
        pdf_path = REAL_MINERU_DIR / "original.pdf"
        poppler = translation_layout_from_pdf_layout(
            document.blocks,
            pdf_path,
            extract_pdf_layout(pdf_path),
        )
        bundle = storage_files.load_mineru_layout_artifact_bundle_from_dir(
            REAL_MINERU_DIR,
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

        converted = translation_layout_from_hybrid(
            document.blocks,
            pdf_path,
            poppler,
            mineru,
            mineru_generation=provenance["generation"],
            mineru_is_ocr=provenance["is_ocr"],
        )

        body_regions = [
            region for region in converted.regions if region.block_index == 12
        ]
        self.assertEqual(len(body_regions), 2)
        self.assertEqual([region.flow_order for region in body_regions], [0, 1])
        self.assertEqual(
            [region.source_block_order for region in body_regions],
            [15, 17],
        )
        self.assertFalse(
            any(0.36 <= region.bbox.y0 <= 0.40 for region in body_regions)
        )
        self.assertNotIn(12, converted.quality.unmapped_block_indexes)
        self.assertTrue(
            any(region.block_index == 14 for region in converted.regions)
        )

    @unittest.skipUnless(
        shutil.which("pdftotext")
        and shutil.which("pdfinfo")
        and (REAL_2104_DIR / "original.pdf").is_file()
        and (REAL_2104_DIR / "translation.json").is_file(),
        "The local 2104.08691 paper and Poppler are required",
    )
    def test_real_2104_corroborates_headings_and_prunes_misbound_regions(self) -> None:
        document = PaperDocument.from_dict(
            json.loads(
                (REAL_2104_DIR / "translation.json").read_text(encoding="utf-8")
            )
        )
        pdf_path = REAL_2104_DIR / "original.pdf"
        poppler = translation_layout_from_pdf_layout(
            document.blocks,
            pdf_path,
            extract_pdf_layout(pdf_path),
        )
        bundle = storage_files.load_mineru_layout_artifact_bundle_from_dir(
            REAL_2104_DIR,
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

        for block_index, expected_word_count in ((3, 2), (104, 3), (109, 2)):
            regions = [
                region for region in hybrid.regions if region.block_index == block_index
            ]
            self.assertEqual(len(regions), 1)
            self.assertEqual(regions[0].geometry_source, "poppler_bbox_layout")
            self.assertEqual(regions[0].render_policy, "replace")
            self.assertEqual(len(regions[0].line_boxes), 1)
            self.assertEqual(len(regions[0].word_boxes), expected_word_count)

        block_33 = [region for region in hybrid.regions if region.block_index == 33]
        self.assertEqual(len(block_33), 1)
        self.assertEqual(block_33[0].geometry_source, "mineru_middle")
        self.assertEqual(block_33[0].page, 5)
        self.assertGreater(block_33[0].bbox.y0, 0.23)

        block_60 = [region for region in hybrid.regions if region.block_index == 60]
        self.assertEqual(len(block_60), 1)
        self.assertEqual(block_60[0].geometry_source, "mineru_middle")
        self.assertEqual(block_60[0].page, 6)
        self.assertEqual(block_60[0].render_policy, "panel_only")
        self.assertEqual(block_60[0].failure_reason, "hybrid_geometry_unverified")

        block_109 = [region for region in hybrid.regions if region.block_index == 109]
        self.assertEqual(len(block_109), 1)
        self.assertEqual(block_109[0].geometry_source, "poppler_bbox_layout")
        self.assertGreater(block_109[0].bbox.y0, 0.30)

        block_45 = [region for region in hybrid.regions if region.block_index == 45]
        block_46 = [region for region in hybrid.regions if region.block_index == 46]
        self.assertEqual(len(block_45), 1)
        self.assertEqual(block_45[0].geometry_source, "poppler_bbox_layout")
        self.assertEqual(block_45[0].render_policy, "replace")
        self.assertEqual(block_46, [])

        block_81 = [region for region in hybrid.regions if region.block_index == 81]
        self.assertEqual(len(block_81), 2)
        self.assertEqual([region.page for region in block_81], [8, 9])
        self.assertEqual([region.flow_order for region in block_81], [0, 1])
        self.assertTrue(
            all(
                region.geometry_source == "poppler_bbox_layout"
                and region.render_policy == "replace"
                for region in block_81
            )
        )

    def test_hybrid_equal_rank_geometry_conflict_unmaps_both_blocks(self) -> None:
        blocks = [
            Block(0, "paragraph", "First ambiguous mapping."),
            Block(1, "paragraph", "Second ambiguous mapping."),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-hybrid-ambiguous-conflict")
            poppler = _precise_layout_from_region_specs(
                blocks,
                pdf_path,
                [(0, 1, 0.20, 0.20, 0.60, 0.25)],
                adapter="poppler_bbox_layout",
                adapter_version=POPPLER_LAYOUT_ADAPTER_VERSION,
            )
            mineru = _precise_layout_from_region_specs(
                blocks,
                pdf_path,
                [(1, 1, 0.201, 0.201, 0.601, 0.251)],
                adapter="mineru_middle",
                adapter_version=MINERU_TRANSLATION_LAYOUT_ADAPTER_VERSION,
            )

            converted = translation_layout_from_hybrid(
                blocks,
                pdf_path,
                poppler,
                mineru,
                mineru_generation="f" * 32,
                mineru_is_ocr=False,
            )

        self.assertEqual(converted.regions, [])
        self.assertEqual(converted.quality.mapped_count, 0)
        self.assertEqual(converted.quality.unmapped_block_indexes, [0, 1])

    def test_hybrid_precise_replace_removes_intercepting_coarse_panel(self) -> None:
        blocks = [
            Block(0, "heading", "Trusted heading", level=2),
            Block(1, "paragraph", "Coarse body geometry."),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-hybrid-coarse-interception")
            poppler = _precise_layout_from_region_specs(
                blocks,
                pdf_path,
                [(0, 1, 0.20, 0.20, 0.40, 0.23)],
                adapter="poppler_bbox_layout",
                adapter_version=POPPLER_LAYOUT_ADAPTER_VERSION,
            )
            poppler_region = poppler.regions[0]
            poppler_region.word_boxes = [poppler_region.bbox.model_copy(deep=True)]
            poppler_region.source_word_orders = [10]
            mineru = _precise_layout_from_region_specs(
                blocks,
                pdf_path,
                [(1, 1, 0.18, 0.19, 0.80, 0.50)],
                adapter="mineru_middle",
                adapter_version=MINERU_TRANSLATION_LAYOUT_ADAPTER_VERSION,
            )
            mineru.regions[0].confidence = 0.84
            mineru.regions[0].render_policy = "panel_only"
            mineru.regions[0].failure_reason = "low_confidence"

            converted = translation_layout_from_hybrid(
                blocks,
                pdf_path,
                poppler,
                mineru,
                mineru_generation="f" * 32,
                mineru_is_ocr=False,
            )

        self.assertEqual(
            [region.block_index for region in converted.regions],
            [0],
        )
        self.assertEqual(converted.regions[0].render_policy, "replace")
        self.assertEqual(converted.quality.unmapped_block_indexes, [1])

    @unittest.skipUnless(
        shutil.which("pdftotext") and shutil.which("pdfinfo"),
        "Poppler is required for the PDF fixture integration check",
    )
    def test_real_digital_and_scanned_pdf_fixtures_enter_the_common_layout(self) -> None:
        digital_pdf = FIXTURE_DIR / "digital_two_column.pdf"
        digital_document = extract_pdf_layout(digital_pdf)
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
        digital_layout = translation_layout_from_pdf_layout(
            digital_blocks,
            digital_pdf,
            digital_document,
        )

        scanned_pdf = FIXTURE_DIR / "scanned_two_page.pdf"
        scanned_document = extract_pdf_layout(scanned_pdf)
        self.assertTrue(scanned_document.pages)
        self.assertTrue(all(not page.blocks for page in scanned_document.pages))
        scanned_middle = json.loads(
            (FIXTURE_DIR / "scanned_middle.json").read_text(encoding="utf-8")
        )
        scanned_content = json.loads(
            (FIXTURE_DIR / "scanned_content_list.json").read_text(encoding="utf-8")
        )
        scanned_blocks = [
            Block(0, "paragraph", "OCR text from scanned page one."),
            Block(1, "paragraph", "OCR text from scanned page two."),
        ]
        scanned_result = MinerUStructuredResult(
            markdown="fixture",
            blocks=scanned_blocks,
            layout=scanned_middle,
            content_list=scanned_content,
        )
        with patch(
            "backend.extraction.translation_layout._source_pdf_page_count",
            return_value=2,
        ):
            scanned_layout = translation_layout_from_mineru(
                scanned_blocks,
                scanned_pdf,
                scanned_result,
            )

        self.assertIsInstance(digital_layout, TranslationLayout)
        self.assertEqual(digital_layout.page_count, 2)
        self.assertEqual(digital_layout.quality.mapped_ratio, 1)
        self.assertIsInstance(scanned_layout, TranslationLayout)
        self.assertEqual(scanned_layout.page_count, 2)
        self.assertEqual(scanned_layout.quality.mapped_ratio, 1)


def _mineru_entry(
    text: str,
    *,
    kind: str = "text",
    protected: bool = False,
) -> _MinerUContentEntry:
    box = NormalizedBox(x0=0.1, y0=0.1, x1=0.9, y1=0.15)
    return _MinerUContentEntry(
        page=0,
        kind=kind,
        text=text,
        bbox=box,
        line_boxes=[box],
        protected_boxes=[box] if protected else [],
        protected=protected,
        rotation=0,
        authoritative=True,
    )


def _precise_layout_from_region_specs(
    blocks: list[Block],
    pdf_path: Path,
    specs: list[tuple[int, int, float, float, float, float]],
    *,
    adapter: str,
    adapter_version: str,
    page_count: int = 1,
) -> TranslationLayout:
    mapping = {
        "pdf_url": f"/assets/{pdf_path.parent.name}/{pdf_path.name}",
        "page_count": page_count,
        "mappings": [
            {
                "block_index": block_index,
                "confidence": 1.0,
                "boxes": [
                    {
                        "page": page,
                        "x0": x0 * 1000,
                        "y0": y0 * 1000,
                        "x1": x1 * 1000,
                        "y1": y1 * 1000,
                        "page_width": 1000,
                        "page_height": 1000,
                    }
                ],
            }
            for block_index, page, x0, y0, x1, y1 in specs
        ],
    }
    layout = translation_layout_from_pdf_map(
        blocks,
        pdf_path,
        mapping,
        adapter=adapter,
        adapter_version=adapter_version,
    )
    for region in layout.regions:
        region.confidence = 1.0
        region.render_policy = "replace"
        region.failure_reason = None
    return layout


def _single_page_mineru_payload(texts: list[str]) -> tuple[dict, list[dict]]:
    para_blocks = []
    content = []
    for index, text in enumerate(texts):
        y0 = 100 + index * 60
        bbox = [100, y0, 900, y0 + 40]
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
                                "content": text,
                            }
                        ],
                    }
                ],
            }
        )
        content.append(
            {
                "type": "text",
                "text": text,
                "page_idx": 0,
                "bbox": bbox,
            }
        )
    return (
        {
            "pdf_info": [
                {
                    "page_idx": 0,
                    "page_size": [1000, 1000],
                    "para_blocks": para_blocks,
                    "tables": [],
                    "images": [],
                    "interline_equations": [],
                    "discarded_blocks": [],
                }
            ]
        },
        content,
    )


def _blocks() -> list[Block]:
    return [
        Block(
            index=0,
            type="paragraph",
            original="A reliable inline translation fixture.",
        ),
        Block(
            index=1,
            type="paragraph",
            original="This region must stay visible as original text.",
        ),
        Block(
            index=2,
            type="paragraph",
            original="This block intentionally has no PDF mapping.",
        ),
    ]


def _poppler_document(
    *,
    rotation: int = 0,
    texts: list[str] | None = None,
    x_positions: list[float] | None = None,
    y_positions: list[float] | None = None,
) -> PdfLayoutDocument:
    texts = texts or [
        "A reliable inline translation fixture.",
        "This region must stay visible as original text.",
    ]
    blocks: list[PdfLayoutBlock] = []
    word_order = 0
    for block_order, text in enumerate(texts):
        y0 = (
            y_positions[block_order]
            if y_positions is not None
            else 80.0 + block_order * 40
        )
        words: list[PdfLayoutWord] = []
        x0 = x_positions[block_order] if x_positions is not None else 72.0
        for word in text.split():
            x1 = x0 + max(20.0, len(word) * 6.0)
            words.append(
                PdfLayoutWord(
                    text=word,
                    bbox=PdfLayoutBox(x0=x0, y0=y0, x1=x1, y1=y0 + 14),
                    reading_order=word_order,
                )
            )
            word_order += 1
            x0 = x1 + 4
        line = PdfLayoutLine(
            bbox=PdfLayoutBox(
                x0=words[0].bbox.x0,
                y0=y0,
                x1=words[-1].bbox.x1,
                y1=y0 + 14,
            ),
            words=tuple(words),
            reading_order=block_order,
        )
        blocks.append(
            PdfLayoutBlock(
                bbox=line.bbox,
                lines=(line,),
                flow_index=block_order,
                reading_order=block_order,
            )
        )
    return PdfLayoutDocument(
        pages=(
            PdfLayoutPage(
                page=1,
                width=612,
                height=792,
                rotation=rotation,
                blocks=tuple(blocks),
            ),
        )
    )


def _poppler_split_flow_document() -> PdfLayoutDocument:
    parts = ["Left flow text continues", "in the right flow region."]
    blocks: list[PdfLayoutBlock] = []
    word_order = 0
    for block_order, text in enumerate(parts):
        x0 = 72.0 if block_order == 0 else 330.0
        words: list[PdfLayoutWord] = []
        for word in text.split():
            x1 = x0 + max(20.0, len(word) * 6.0)
            words.append(
                PdfLayoutWord(
                    text=word,
                    bbox=PdfLayoutBox(x0=x0, y0=100, x1=x1, y1=114),
                    reading_order=word_order,
                )
            )
            word_order += 1
            x0 = x1 + 4
        line = PdfLayoutLine(
            bbox=PdfLayoutBox(
                x0=words[0].bbox.x0,
                y0=100,
                x1=words[-1].bbox.x1,
                y1=114,
            ),
            words=tuple(words),
            reading_order=block_order,
        )
        blocks.append(
            PdfLayoutBlock(
                bbox=line.bbox,
                lines=(line,),
                flow_index=block_order,
                reading_order=block_order,
            )
        )
    return PdfLayoutDocument(
        pages=(
            PdfLayoutPage(
                page=1,
                width=612,
                height=792,
                rotation=0,
                blocks=tuple(blocks),
            ),
        )
    )


def _poppler_cross_page_document(
    parts: list[str] | None = None,
) -> PdfLayoutDocument:
    parts = parts or [
        "The paragraph starts on page one",
        "and continues on page two.",
    ]
    pages: list[PdfLayoutPage] = []
    word_order = 0
    for page_number, text in enumerate(parts, start=1):
        words: list[PdfLayoutWord] = []
        x0 = 72.0
        for word in text.split():
            x1 = x0 + max(20.0, len(word) * 6.0)
            words.append(
                PdfLayoutWord(
                    text=word,
                    bbox=PdfLayoutBox(x0=x0, y0=100, x1=x1, y1=114),
                    reading_order=word_order,
                )
            )
            word_order += 1
            x0 = x1 + 4
        line = PdfLayoutLine(
            bbox=PdfLayoutBox(
                x0=words[0].bbox.x0,
                y0=100,
                x1=words[-1].bbox.x1,
                y1=114,
            ),
            words=tuple(words),
            reading_order=page_number - 1,
        )
        pages.append(
            PdfLayoutPage(
                page=page_number,
                width=612,
                height=792,
                rotation=0,
                blocks=(
                    PdfLayoutBlock(
                        bbox=line.bbox,
                        lines=(line,),
                        flow_index=0,
                        reading_order=page_number - 1,
                    ),
                ),
            )
        )
    return PdfLayoutDocument(pages=tuple(pages))


def _poppler_cross_page_footer_document(
    *,
    include_leading_word: bool,
) -> PdfLayoutDocument:
    page_texts = [
        "In Fig. 7 we show the performance of SemD-"
        if include_leading_word
        else "Fig. 7 we show the performance of SemD-",
        "eDup versus random pruning on every validation set.",
    ]
    pages: list[PdfLayoutPage] = []
    word_order = 0
    for page_number, text in enumerate(page_texts, start=1):
        words: list[PdfLayoutWord] = []
        x0 = 72.0
        for raw_word in text.split():
            x1 = x0 + max(20.0, len(raw_word) * 6.0)
            words.append(
                PdfLayoutWord(
                    text=raw_word,
                    bbox=PdfLayoutBox(x0=x0, y0=100, x1=x1, y1=114),
                    reading_order=word_order,
                )
            )
            word_order += 1
            x0 = x1 + 4
        body_line = PdfLayoutLine(
            bbox=PdfLayoutBox(
                x0=words[0].bbox.x0,
                y0=100,
                x1=words[-1].bbox.x1,
                y1=114,
            ),
            words=tuple(words),
            reading_order=page_number - 1,
        )
        body = PdfLayoutBlock(
            bbox=body_line.bbox,
            lines=(body_line,),
            flow_index=0,
            reading_order=(page_number - 1) * 2,
        )
        blocks = [body]
        if page_number == 1:
            footer_word = PdfLayoutWord(
                text="8",
                bbox=PdfLayoutBox(x0=300, y0=760, x1=306, y1=774),
                reading_order=900,
            )
            footer_line = PdfLayoutLine(
                bbox=footer_word.bbox,
                words=(footer_word,),
                reading_order=900,
            )
            blocks.append(
                PdfLayoutBlock(
                    bbox=footer_word.bbox,
                    lines=(footer_line,),
                    flow_index=1,
                    reading_order=1,
                )
            )
        pages.append(
            PdfLayoutPage(
                page=page_number,
                width=612,
                height=792,
                rotation=0,
                blocks=tuple(blocks),
            )
        )
    return PdfLayoutDocument(pages=tuple(pages))


if __name__ == "__main__":
    unittest.main()
