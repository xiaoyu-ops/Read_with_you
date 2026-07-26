from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import routes_annotations
from backend.extraction.blocks import Block, PaperDocument
from backend.extraction.translation_layout import source_pdf_sha256
from backend.storage import files as storage_files
from backend.storage.files import (
    _atomic_write_text,
    add_annotation,
    delete_annotation,
    load_annotations,
    save_document,
    update_annotation,
)


class AnnotationStorageTest(unittest.TestCase):
    def test_atomic_write_preserves_existing_file_on_replace_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("old", encoding="utf-8")

            with patch("backend.storage.files.os.replace", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    _atomic_write_text(path, "new")

            self.assertEqual(path.read_text(encoding="utf-8"), "old")
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_add_and_delete_annotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            papers_dir = Path(tmp) / "papers"
            with patch.object(storage_files, "PAPERS_DIR", papers_dir):
                doc = PaperDocument(
                    paper_id="1706.03762",
                    title="Attention Is All You Need",
                    source="ar5iv",
                    extracted_at="2026-07-03T00:00:00Z",
                    blocks=[
                        Block(index=0, type="paragraph", original="Attention matters."),
                    ],
                )
                save_document(doc)

                annotation = add_annotation(
                    arxiv_id="1706.03762",
                    block_index=0,
                    side="original",
                    text="Attention",
                    note="核心术语",
                    selector={
                        "version": 1,
                        "region_id": "region-0",
                        "start_offset": 0,
                        "end_offset": 9,
                        "occurrence": 0,
                    },
                )
                annotations = load_annotations("1706.03762")

                self.assertEqual(len(annotations), 1)
                self.assertEqual(annotations[0]["id"], annotation["id"])
                self.assertEqual(annotations[0]["note"], "核心术语")
                self.assertEqual(annotations[0]["kind"], "highlight")
                self.assertEqual(
                    annotations[0]["updated_at"],
                    annotations[0]["created_at"],
                )
                self.assertEqual(annotations[0]["selector"]["region_id"], "region-0")

                updated = update_annotation(
                    "1706.03762",
                    annotation["id"],
                    note="需要继续核对",
                    kind="question",
                )
                self.assertIsNotNone(updated)
                assert updated is not None
                self.assertEqual(updated["note"], "需要继续核对")
                self.assertEqual(updated["kind"], "question")
                self.assertEqual(updated["color"], "rose")
                self.assertEqual(updated["selector"], annotation["selector"])

                self.assertTrue(delete_annotation("1706.03762", annotation["id"]))
                self.assertEqual(load_annotations("1706.03762"), [])
                self.assertFalse(delete_annotation("1706.03762", annotation["id"]))

    def test_load_annotations_defaults_legacy_note_fields_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            papers_dir = Path(tmp) / "papers"
            with patch.object(storage_files, "PAPERS_DIR", papers_dir):
                annotation_path = papers_dir / "legacy" / "annotations.json"
                annotation_path.parent.mkdir(parents=True)
                original = (
                    '[{"id":"legacy-1","arxiv_id":"legacy","block_index":0,'
                    '"side":"original","text":"Evidence","created_at":"2026-07-01T00:00:00Z"}]'
                )
                annotation_path.write_text(original, encoding="utf-8")

                item = load_annotations("legacy")[0]

                self.assertEqual(item["kind"], "highlight")
                self.assertEqual(item["color"], "yellow")
                self.assertEqual(item["note"], "")
                self.assertEqual(item["updated_at"], item["created_at"])
                self.assertEqual(annotation_path.read_text(encoding="utf-8"), original)

    def test_annotation_api_roundtrips_both_reader_sides(self) -> None:
        app = FastAPI()
        app.include_router(routes_annotations.router)
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(storage_files, "PAPERS_DIR", Path(tmp) / "papers"),
            TestClient(app) as client,
        ):
            save_document(
                PaperDocument(
                    paper_id="inline-reader",
                    title="Inline reader",
                    source="local",
                    extracted_at="2026-07-21T00:00:00Z",
                    blocks=[Block(index=0, type="paragraph", original="Evidence text.")],
                )
            )
            created = []
            for side, text in (("translation", "中文证据"), ("original", "Evidence")):
                selector = (
                    {
                        "version": 1,
                        "region_id": "region-0",
                        "start_offset": 2,
                        "end_offset": 6,
                        "occurrence": 0,
                    }
                    if side == "translation"
                    else None
                )
                response = client.post(
                    "/papers/inline-reader/annotations",
                    json={
                        "block_index": 0,
                        "side": side,
                        "text": text,
                        "note": side,
                        "selector": selector,
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["side"], side)
                created.append(response.json())

            self.assertEqual(created[0]["selector"]["start_offset"], 2)
            self.assertIsNone(created[1]["selector"])

            listed = client.get("/papers/inline-reader/annotations")
            self.assertEqual(listed.status_code, 200)
            self.assertEqual([item["side"] for item in listed.json()], ["translation", "original"])

            original_before = listed.json()[1]
            updated = client.patch(
                f"/papers/inline-reader/annotations/{created[1]['id']}",
                json={"note": "新的 Markdown 笔记", "kind": "method"},
            )
            self.assertEqual(updated.status_code, 200)
            updated_item = updated.json()
            self.assertEqual(updated_item["note"], "新的 Markdown 笔记")
            self.assertEqual(updated_item["kind"], "method")
            self.assertEqual(updated_item["color"], "blue")
            self.assertEqual(updated_item["text"], original_before["text"])
            self.assertEqual(updated_item["selector"], original_before["selector"])
            self.assertEqual(updated_item["created_at"], original_before["created_at"])

            empty_update = client.patch(
                f"/papers/inline-reader/annotations/{created[1]['id']}",
                json={},
            )
            self.assertEqual(empty_update.status_code, 422)
            invalid_kind = client.patch(
                f"/papers/inline-reader/annotations/{created[1]['id']}",
                json={"kind": "favorite"},
            )
            self.assertEqual(invalid_kind.status_code, 422)
            oversized_note = client.patch(
                f"/papers/inline-reader/annotations/{created[1]['id']}",
                json={"note": "x" * 8_001},
            )
            self.assertEqual(oversized_note.status_code, 422)
            missing_update = client.patch(
                "/papers/inline-reader/annotations/missing",
                json={"note": "不会保存"},
            )
            self.assertEqual(missing_update.status_code, 404)

            deleted = client.delete(
                f"/papers/inline-reader/annotations/{created[0]['id']}"
            )
            self.assertEqual(deleted.status_code, 200)
            self.assertEqual(len(client.get("/papers/inline-reader/annotations").json()), 1)

            invalid = client.post(
                "/papers/inline-reader/annotations",
                json={"block_index": 0, "side": "legacy-pane", "text": "bad"},
            )
            self.assertEqual(invalid.status_code, 422)

            invalid_selector = client.post(
                "/papers/inline-reader/annotations",
                json={
                    "block_index": 0,
                    "side": "translation",
                    "text": "bad",
                    "selector": {
                        "version": 1,
                        "region_id": "region-0",
                        "start_offset": 5,
                        "end_offset": 5,
                        "occurrence": 0,
                    },
                },
            )
            self.assertEqual(invalid_selector.status_code, 422)

    def test_pdf_text_selector_roundtrips_only_with_current_pdf_evidence(self) -> None:
        app = FastAPI()
        app.include_router(routes_annotations.router)
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(storage_files, "PAPERS_DIR", Path(tmp) / "papers"),
            TestClient(app) as client,
        ):
            paper_id = "selection-annotation"
            save_document(
                PaperDocument(
                    paper_id=paper_id,
                    title="Selection annotation",
                    source="local_pdf",
                    extracted_at="2026-07-22T00:00:00Z",
                    blocks=[Block(index=0, type="paragraph", original="Evidence text.")],
                )
            )
            pdf_path = storage_files.paper_dir(paper_id) / "original.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\nannotation fixture\n%%EOF\n")
            pdf_hash = source_pdf_sha256(pdf_path)
            storage_files.save_translation_layout(
                paper_id,
                _selection_layout(paper_id, pdf_hash),
            )
            selector = {
                "version": 2,
                "source_pdf_sha256": pdf_hash,
                "page": 1,
                "start": {"item_index": 0, "char_offset": 0},
                "end": {"item_index": 0, "char_offset": 8},
                "quote": {"exact": "Evidence", "prefix": "", "suffix": " text."},
                "rects": [{"x0": 0.1, "y0": 0.2, "x1": 0.4, "y1": 0.24}],
                "region_id": "region-0",
                "layout_confidence": 0.96,
            }

            created = client.post(
                f"/papers/{paper_id}/annotations",
                json={
                    "block_index": 0,
                    "side": "original",
                    "text": "Evidence",
                    "selector": selector,
                },
            )
            self.assertEqual(created.status_code, 200, created.text)
            self.assertEqual(created.json()["selector"], selector)

            stale = dict(selector, source_pdf_sha256="a" * 64)
            response = client.post(
                f"/papers/{paper_id}/annotations",
                json={
                    "block_index": 0,
                    "side": "original",
                    "text": "Evidence",
                    "selector": stale,
                },
            )
            self.assertEqual(response.status_code, 409)

            wrong_text = dict(selector, quote={"exact": "Different", "prefix": "", "suffix": ""})
            response = client.post(
                f"/papers/{paper_id}/annotations",
                json={
                    "block_index": 0,
                    "side": "original",
                    "text": "Evidence",
                    "selector": wrong_text,
                },
            )
            self.assertEqual(response.status_code, 422)


def _selection_layout(paper_id: str, pdf_hash: str) -> dict:
    return {
        "version": 1,
        "cache_key": "a" * 64,
        "source_pdf_sha256": pdf_hash,
        "block_source_sha256": "b" * 64,
        "adapter": "poppler_bbox_layout",
        "adapter_version": "7",
        "pdf_url": f"/assets/{paper_id}/original.pdf",
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
        },
        "warnings": [],
    }


if __name__ == "__main__":
    unittest.main()
