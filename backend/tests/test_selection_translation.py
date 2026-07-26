from __future__ import annotations

import hashlib
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import routes_translate
from backend.extraction.blocks import Block, PaperDocument
from backend.extraction.translation_layout import source_pdf_sha256
from backend.storage import files as storage_files
from backend.translation.deeplx import DeepLXError


PAPER_ID = "selection-paper"
SOURCE_TEXT = "Loss $L(x)$ follows prior work [12]."


class SelectionTranslationApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.papers_dir = Path(self.tmp.name) / "papers"
        self.files_patch = patch.object(storage_files, "PAPERS_DIR", self.papers_dir)
        self.files_patch.start()
        self.paper_dir = storage_files.ensure_paper_dir(PAPER_ID)
        self.pdf_path = self.paper_dir / "original.pdf"
        self.pdf_path.write_bytes(b"%PDF-1.4\nselection fixture\n%%EOF\n")
        self.pdf_hash = source_pdf_sha256(self.pdf_path)
        storage_files.save_document(
            PaperDocument(
                paper_id=PAPER_ID,
                title="Selection Fixture",
                source="local_pdf",
                extracted_at="2026-07-22T00:00:00Z",
                blocks=[
                    Block(
                        index=0,
                        type="paragraph",
                        original=SOURCE_TEXT,
                        status="pending",
                    )
                ],
            )
        )
        storage_files.save_translation_layout(PAPER_ID, self._layout())
        app = FastAPI()
        app.include_router(routes_translate.router)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.files_patch.stop()
        self.tmp.cleanup()

    def test_translates_verified_selection_and_preserves_immutables_without_writing(self) -> None:
        before = (self.paper_dir / "translation.json").read_bytes()
        received: list[str] = []

        async def fake_deeplx(text: str) -> str:
            received.append(text)
            placeholders = re.findall(r"⟦PET_IMMUTABLE_[A-F0-9]+_\d{4}⟧", text)
            return f"损失 {placeholders[0]} 遵循已有工作 {placeholders[1]}。"

        with patch(
            "backend.translation.selection.translate_with_deeplx",
            side_effect=fake_deeplx,
        ):
            response = self.client.post(
                f"/translate/{PAPER_ID}/selection",
                json=self._payload(),
            )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["provider"], "deeplx")
        self.assertEqual(body["source_text"], SOURCE_TEXT)
        self.assertEqual(body["translation"], "损失 $L(x)$ 遵循已有工作 [12]。")
        self.assertEqual(body["block_index"], 0)
        self.assertEqual(body["region_id"], "region-0")
        self.assertEqual(body["layout_confidence"], 0.96)
        self.assertFalse(body["source_edited"])
        self.assertEqual(len(received), 1)
        self.assertNotIn("$L(x)$", received[0])
        self.assertNotIn("[12]", received[0])
        self.assertEqual((self.paper_dir / "translation.json").read_bytes(), before)

    def test_edited_source_translates_without_claiming_layout_mapping(self) -> None:
        edited = "Loss L of x follows prior work."
        payload = self._payload()
        payload.update({
            "raw_text": edited,
            "text_sha256": hashlib.sha256(edited.encode()).hexdigest(),
            "quote": {"exact": edited, "prefix": "", "suffix": ""},
            "block_index": None,
            "region_id": None,
            "layout_confidence": None,
            "source_edited": True,
        })

        async def fake_deeplx(text: str) -> str:
            self.assertEqual(text, edited)
            return "损失函数遵循已有工作。"

        with patch(
            "backend.translation.selection.translate_with_deeplx",
            side_effect=fake_deeplx,
        ):
            response = self.client.post(
                f"/translate/{PAPER_ID}/selection",
                json=payload,
            )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["source_edited"])
        self.assertIsNone(body["block_index"])
        self.assertIsNone(body["region_id"])
        self.assertIsNone(body["layout_confidence"])

    def test_rejects_stale_pdf_changed_text_invalid_page_and_mapping(self) -> None:
        cases = (
            ({"source_pdf_sha256": "a" * 64}, 409, "selection_source_changed"),
            ({"text_sha256": "a" * 64}, 409, "selection_text_changed"),
            ({"page": 2}, 422, "selection_page_invalid"),
            ({"block_index": 9, "region_id": None}, 409, "selection_layout_mismatch"),
        )
        for patch_payload, status, code in cases:
            with self.subTest(code=code):
                payload = self._payload()
                payload.update(patch_payload)
                with patch(
                    "backend.translation.selection.translate_with_deeplx",
                    side_effect=AssertionError("invalid selection must not call provider"),
                ):
                    response = self.client.post(
                        f"/translate/{PAPER_ID}/selection",
                        json=payload,
                    )
                self.assertEqual(response.status_code, status, response.text)
                self.assertEqual(response.json()["detail"]["code"], code)

    def test_request_schema_rejects_bad_quote_anchor_geometry_and_length(self) -> None:
        cases = (
            {"quote": {"exact": "different", "prefix": "", "suffix": ""}},
            {"end": {"item_index": 0, "char_offset": 0}},
            {"rects": [{"x0": 0.2, "y0": 0.2, "x1": 0.1, "y1": 0.3}]},
            {
                "raw_text": "x" * 4_001,
                "text_sha256": hashlib.sha256(("x" * 4_001).encode()).hexdigest(),
                "quote": {"exact": "x" * 4_001, "prefix": "", "suffix": ""},
            },
        )
        for patch_payload in cases:
            with self.subTest(keys=tuple(patch_payload)):
                payload = self._payload()
                payload.update(patch_payload)
                response = self.client.post(
                    f"/translate/{PAPER_ID}/selection",
                    json=payload,
                )
                self.assertEqual(response.status_code, 422, response.text)

    def test_deeplx_failure_is_stable_retryable_and_secret_safe(self) -> None:
        async def fail(_text: str) -> str:
            raise DeepLXError("deeplx_rate_limited")

        with patch(
            "backend.translation.selection.translate_with_deeplx",
            side_effect=fail,
        ):
            response = self.client.post(
                f"/translate/{PAPER_ID}/selection",
                json=self._payload(),
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["detail"],
            {
                "code": "deeplx_rate_limited",
                "message": "翻译服务当前繁忙，请稍后重试。",
                "retryable": True,
            },
        )
        self.assertNotIn("http", response.text)

    def test_changed_immutable_placeholders_fail_closed(self) -> None:
        async def mutate(_text: str) -> str:
            return "损失公式已省略，参考文献也已省略。"

        with patch(
            "backend.translation.selection.translate_with_deeplx",
            side_effect=mutate,
        ):
            response = self.client.post(
                f"/translate/{PAPER_ID}/selection",
                json=self._payload(),
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"]["code"],
            "selection_immutable_invalid",
        )

    def test_missing_source_pdf_and_layout_are_explicit(self) -> None:
        self.pdf_path.unlink()
        response = self.client.post(
            f"/translate/{PAPER_ID}/selection",
            json=self._payload(),
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "source_pdf_missing")

        self.pdf_path.write_bytes(b"%PDF-1.4\nselection fixture\n%%EOF\n")
        (self.paper_dir / "translation_layout.json").unlink()
        response = self.client.post(
            f"/translate/{PAPER_ID}/selection",
            json=self._payload(),
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["code"],
            "selection_layout_unavailable",
        )

    def _payload(self) -> dict:
        return {
            "version": 2,
            "source_pdf_sha256": self.pdf_hash,
            "page": 1,
            "raw_text": SOURCE_TEXT,
            "text_sha256": hashlib.sha256(SOURCE_TEXT.encode()).hexdigest(),
            "start": {"item_index": 0, "char_offset": 0},
            "end": {"item_index": 3, "char_offset": 8},
            "quote": {"exact": SOURCE_TEXT, "prefix": "Abstract ", "suffix": " Next"},
            "rects": [{"x0": 0.1, "y0": 0.2, "x1": 0.8, "y1": 0.24}],
            "block_index": 0,
            "region_id": "region-0",
            "layout_confidence": 0.96,
        }

    def _layout(self) -> dict:
        return {
            "version": 1,
            "cache_key": "a" * 64,
            "source_pdf_sha256": self.pdf_hash,
            "block_source_sha256": "c" * 64,
            "adapter": "poppler_bbox_layout",
            "adapter_version": "7",
            "pdf_url": f"/assets/{PAPER_ID}/original.pdf",
            "page_count": 1,
            "pages": [{"page": 1, "width": 600, "height": 800, "rotation": 0}],
            "regions": [{
                "region_id": "region-0",
                "block_index": 0,
                "page": 1,
                "flow_order": 0,
                "kind": "paragraph",
                "bbox": {"x0": 0.1, "y0": 0.2, "x1": 0.8, "y1": 0.24},
                "line_boxes": [{"x0": 0.1, "y0": 0.2, "x1": 0.8, "y1": 0.24}],
                "word_boxes": [],
                "protected_boxes": [],
                "source_block_order": 0,
                "source_line_orders": [0],
                "source_word_orders": [],
                "rotation": 0,
                "confidence": 0.96,
                "render_policy": "replace",
                "failure_reason": None,
            }],
            "quality": {
                "mappable_count": 1,
                "mapped_count": 1,
                "replaceable_count": 1,
                "panel_only_count": 0,
                "unmapped_count": 0,
                "mapped_ratio": 1.0,
                "average_confidence": 0.96,
                "protected_overlap_count": 0,
                "protected_count": 0,
                "unmapped_block_indexes": [],
                "failure_counts": {},
            },
            "warnings": [],
        }


if __name__ == "__main__":
    unittest.main()
