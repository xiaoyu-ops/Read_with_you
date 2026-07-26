from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import routes_collections, routes_papers
from backend.extraction.blocks import Block, PaperDocument
from backend.storage import db as db_module, files as storage_files
from backend.storage.files import (
    add_annotation,
    load_annotations,
    load_paper_note,
    save_document,
    save_paper_note,
)


def _document(paper_id: str, title: str) -> PaperDocument:
    return PaperDocument(
        paper_id=paper_id,
        title=title,
        source="local",
        extracted_at="2026-07-23T00:00:00Z",
        blocks=[Block(index=0, type="paragraph", original="Method evidence.")],
    )


class LibraryNoteIntegrationTest(unittest.TestCase):
    def test_library_and_collections_share_notes_across_memberships(self) -> None:
        app = FastAPI()
        app.include_router(routes_papers.router)
        app.include_router(routes_collections.router)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(db_module, "DB_PATH", root / "papers.db"),
                patch.object(storage_files, "PAPERS_DIR", root / "papers"),
                patch.object(storage_files, "COLLECTIONS_DIR", root / "collections"),
            ):
                first_id, second_id, annotation_id = asyncio.run(self._prepare())
                with TestClient(app) as client:
                    papers = client.get("/papers")
                    self.assertEqual(papers.status_code, 200)
                    paper = next(
                        item for item in papers.json() if item["arxiv_id"] == "paper-notes"
                    )
                    self.assertEqual(paper["selection_note_count"], 1)
                    self.assertTrue(paper["has_paper_note"])
                    self.assertIn("核心判断", paper["note_preview"])
                    self.assertIsNotNone(paper["note_updated_at"])

                    first = client.get(f"/collections/{first_id}").json()["papers"][0]
                    second = client.get(f"/collections/{second_id}").json()["papers"][0]
                    for item in (first, second):
                        self.assertEqual(item["selection_note_count"], 1)
                        self.assertTrue(item["has_paper_note"])
                        self.assertEqual(item["note_kind_counts"]["question"], 1)
                        self.assertIn("核心判断", item["note_preview"])

                    removed = client.delete(
                        f"/collections/{first_id}/papers/paper-notes"
                    )
                    self.assertEqual(removed.status_code, 200)
                    self.assertEqual(removed.json()["papers"], [])
                    self.assertEqual(
                        load_paper_note("paper-notes")["markdown"],
                        "# 阅读笔记\n\n核心判断需要更多证据。",
                    )
                    self.assertEqual(load_annotations("paper-notes")[0]["id"], annotation_id)
                    self.assertEqual(
                        client.get(f"/collections/{second_id}").json()["papers"][0][
                            "selection_note_count"
                        ],
                        1,
                    )

                    readded = client.post(
                        f"/collections/{first_id}/papers",
                        json={"arxiv_id": "paper-notes"},
                    )
                    self.assertEqual(readded.status_code, 200)
                    self.assertEqual(
                        readded.json()["papers"][0]["selection_note_count"],
                        1,
                    )

    async def _prepare(self) -> tuple[int, int, str]:
        await db_module.init_db()
        await db_module.insert_paper(
            "paper-notes",
            "Paper with notes",
            ["A"],
            "local",
            "/tmp/paper-notes",
        )
        save_document(_document("paper-notes", "Paper with notes"))
        save_paper_note(
            "paper-notes",
            "# 阅读笔记\n\n核心判断需要更多证据。",
            load_paper_note("paper-notes")["revision"],
        )
        annotation = add_annotation(
            "paper-notes",
            0,
            "original",
            "Method evidence",
            note="数据划分是否一致？",
            kind="question",
            selector={"version": 2, "page": 1, "region_id": "region-0"},
        )
        first = await db_module.create_collection("专题一")
        second = await db_module.create_collection("专题二")
        await db_module.add_paper_to_collection(first["id"], "paper-notes")
        await db_module.add_paper_to_collection(second["id"], "paper-notes")
        return int(first["id"]), int(second["id"]), str(annotation["id"])


if __name__ == "__main__":
    unittest.main()
