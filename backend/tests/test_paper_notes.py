from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import routes_notes
from backend.extraction.blocks import Block, PaperDocument
from backend.storage import files as storage_files
from backend.storage.files import (
    add_annotation,
    build_paper_note_summary,
    load_paper_note,
    save_document,
)


class PaperNoteApiTest(unittest.TestCase):
    def test_paper_note_revision_roundtrip_and_conflict(self) -> None:
        app = FastAPI()
        app.include_router(routes_notes.router)
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(storage_files, "PAPERS_DIR", Path(tmp) / "papers"),
            TestClient(app) as client,
        ):
            save_document(_document("paper-note"))

            empty = client.get("/papers/paper-note/paper-note")
            self.assertEqual(empty.status_code, 200)
            empty_item = empty.json()
            self.assertEqual(empty_item["markdown"], "")
            self.assertIsNone(empty_item["updated_at"])
            self.assertEqual(len(empty_item["revision"]), 64)

            saved = client.put(
                "/papers/paper-note/paper-note",
                json={
                    "markdown": "# 阅读笔记\n\n这是方法笔记。",
                    "base_revision": empty_item["revision"],
                },
            )
            self.assertEqual(saved.status_code, 200)
            saved_item = saved.json()
            self.assertEqual(saved_item["markdown"], "# 阅读笔记\n\n这是方法笔记。")
            self.assertNotEqual(saved_item["revision"], empty_item["revision"])
            self.assertIsNotNone(saved_item["updated_at"])
            self.assertEqual(
                load_paper_note("paper-note")["markdown"],
                "# 阅读笔记\n\n这是方法笔记。",
            )

            conflict = client.put(
                "/papers/paper-note/paper-note",
                json={
                    "markdown": "覆盖掉已有笔记",
                    "base_revision": empty_item["revision"],
                },
            )
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(
                conflict.json()["detail"]["code"],
                "paper_note_revision_conflict",
            )
            self.assertEqual(
                load_paper_note("paper-note")["markdown"],
                "# 阅读笔记\n\n这是方法笔记。",
            )

    def test_paper_note_validation_and_missing_paper(self) -> None:
        app = FastAPI()
        app.include_router(routes_notes.router)
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(storage_files, "PAPERS_DIR", Path(tmp) / "papers"),
            TestClient(app) as client,
        ):
            missing = client.get("/papers/missing/paper-note")
            self.assertEqual(missing.status_code, 404)

            save_document(_document("paper-note"))
            revision = load_paper_note("paper-note")["revision"]
            oversized = client.put(
                "/papers/paper-note/paper-note",
                json={
                    "markdown": "x" * 200_001,
                    "base_revision": revision,
                },
            )
            self.assertEqual(oversized.status_code, 422)
            invalid_revision = client.put(
                "/papers/paper-note/paper-note",
                json={"markdown": "note", "base_revision": "not-a-sha"},
            )
            self.assertEqual(invalid_revision.status_code, 422)

    def test_note_summary_counts_semantic_notes_and_anchors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(storage_files, "PAPERS_DIR", Path(tmp) / "papers"):
                save_document(_document("summary"))
                add_annotation(
                    arxiv_id="summary",
                    block_index=0,
                    side="original",
                    text="Evidence",
                    note="这里需要核对",
                    kind="question",
                    selector={
                        "version": 2,
                        "page": 3,
                        "region_id": "region-3",
                    },
                )
                add_annotation(
                    arxiv_id="summary",
                    block_index=0,
                    side="original",
                    text="Evidence",
                )

                summary = build_paper_note_summary("summary")

                self.assertEqual(summary["annotation_count"], 2)
                self.assertEqual(summary["selection_note_count"], 1)
                self.assertEqual(summary["kind_counts"]["question"], 1)
                self.assertEqual(summary["kind_counts"]["highlight"], 1)
                self.assertEqual(summary["preview"], "这里需要核对")
                self.assertEqual(
                    summary["anchors"][0],
                    {
                        "annotation_id": summary["anchors"][0]["annotation_id"],
                        "kind": "question",
                        "page": 3,
                        "block_index": 0,
                        "region_id": "region-3",
                    },
                )


def _document(paper_id: str) -> PaperDocument:
    return PaperDocument(
        paper_id=paper_id,
        title="Paper note fixture",
        source="local",
        extracted_at="2026-07-23T00:00:00Z",
        blocks=[Block(index=0, type="paragraph", original="Evidence text.")],
    )


if __name__ == "__main__":
    unittest.main()
