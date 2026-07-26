from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.api.routes_collections import (
    _build_collection_agent_report,
    _refresh_collection_report_note_fields,
)
from backend.extraction.blocks import Block, PaperDocument
from backend.storage import db as db_module
from backend.storage import files as storage_files
from backend.storage.files import (
    add_annotation,
    load_collection_agent_report,
    save_analysis,
    save_collection_agent_report,
    save_document,
)


class CollectionAgentReportTest(unittest.TestCase):
    def test_collection_report_aggregates_analysis_and_annotations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "papers.db"
            papers_dir = Path(tmp) / "papers"
            collections_dir = Path(tmp) / "collections"
            with (
                patch.object(db_module, "DB_PATH", db_path),
                patch.object(storage_files, "PAPERS_DIR", papers_dir),
                patch.object(storage_files, "COLLECTIONS_DIR", collections_dir),
            ):
                report = asyncio.run(_exercise_collection_report_flow())

            self.assertEqual(report["paper_count"], 1)
            self.assertEqual(report["analyzed_count"], 1)
            self.assertEqual(report["annotated_count"], 1)
            self.assertEqual(report["papers"][0]["annotation_count"], 1)
            self.assertEqual(report["papers"][0]["selection_note_count"], 1)
            self.assertFalse(report["papers"][0]["has_paper_note"])
            self.assertIn("核心术语", report["papers"][0]["note_preview"])
            self.assertEqual(report["papers"][0]["reproducibility_verdict"], "reproducible")

            legacy = {
                **report,
                "annotated_count": 0,
                "papers": [
                    {
                        key: value
                        for key, value in report["papers"][0].items()
                        if key not in {
                            "selection_note_count",
                            "has_paper_note",
                            "note_updated_at",
                            "note_preview",
                        }
                    }
                ],
                "synthesis": [
                    "专题共 1 篇论文，其中 1 篇已有单篇 Agent 分析。",
                    "0 篇论文包含用户标注，可作为后续专题记忆输入。",
                ],
            }
            with patch.object(storage_files, "PAPERS_DIR", papers_dir):
                refreshed = _refresh_collection_report_note_fields(legacy)
            self.assertEqual(refreshed["papers"][0]["selection_note_count"], 1)
            self.assertTrue(any("你的笔记" in item for item in refreshed["synthesis"]))
            self.assertFalse(any("后续专题记忆输入" in item for item in refreshed["synthesis"]))

    def test_cached_report_refreshes_current_membership_and_note_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "papers.db"
            papers_dir = Path(tmp) / "papers"
            collections_dir = Path(tmp) / "collections"
            with (
                patch.object(db_module, "DB_PATH", db_path),
                patch.object(storage_files, "PAPERS_DIR", papers_dir),
                patch.object(storage_files, "COLLECTIONS_DIR", collections_dir),
            ):
                cached, current = asyncio.run(
                    _exercise_collection_report_membership_refresh()
                )
                refreshed = _refresh_collection_report_note_fields(cached, current)

            self.assertEqual(cached["paper_count"], 1)
            self.assertEqual(refreshed["paper_count"], 2)
            self.assertEqual(refreshed["annotated_count"], 1)
            self.assertEqual(
                [paper["arxiv_id"] for paper in refreshed["papers"]],
                ["paper-one", "paper-two"],
            )
            second = refreshed["papers"][1]
            self.assertEqual(second["selection_note_count"], 1)
            self.assertIn("需要继续核对", second["note_preview"])
            self.assertTrue(any("1 篇论文包含“你的笔记”" in item for item in refreshed["synthesis"]))


async def _exercise_collection_report_flow() -> dict:
    await db_module.init_db()
    await db_module.insert_paper(
        arxiv_id="1706.03762",
        title="Attention Is All You Need",
        authors=["A. Vaswani"],
        source="ar5iv",
        file_path="/tmp/1706.03762",
    )
    collection = await db_module.create_collection("Transformer 基础")
    await db_module.add_paper_to_collection(collection["id"], "1706.03762")

    save_document(
        PaperDocument(
            paper_id="1706.03762",
            title="Attention Is All You Need",
            source="ar5iv",
            extracted_at="2026-07-03T00:00:00Z",
            blocks=[Block(index=0, type="paragraph", original="Attention matters.")],
        )
    )
    save_analysis(
        "1706.03762",
        {
            "summary": "Transformer uses attention.",
            "reproducibility": {
                "verdict": "reproducible",
                "confidence": "medium",
                "evidence": [],
                "summary": "Code is available.",
            },
            "improvements": ["Evaluate longer context."],
            "highlights": ["Simple architecture."],
        },
    )
    add_annotation("1706.03762", 0, "original", "Attention", "核心术语")

    detail = await db_module.get_collection(collection["id"])
    assert detail is not None
    report = _build_collection_agent_report(detail)
    save_collection_agent_report(collection["id"], report)
    loaded = load_collection_agent_report(collection["id"])
    assert loaded is not None

    task_id = await db_module.create_agent_task(
        arxiv_id=f"collection:{collection['id']}",
        collection_id=collection["id"],
        task_type="collection_cross_review",
        summary="专题横向整理",
    )
    await db_module.update_agent_task(task_id, "done", "专题横向整理完成")
    tasks = await db_module.list_agent_tasks()
    assert tasks[0]["collection_name"] == "Transformer 基础"
    assert tasks[0]["collection_id"] == collection["id"]

    return loaded


async def _exercise_collection_report_membership_refresh() -> tuple[dict, dict]:
    await db_module.init_db()
    for paper_id, title in (
        ("paper-one", "Paper One"),
        ("paper-two", "Paper Two"),
    ):
        await db_module.insert_paper(
            arxiv_id=paper_id,
            title=title,
            authors=["Author"],
            source="local",
            file_path=f"/tmp/{paper_id}",
        )
        save_document(
            PaperDocument(
                paper_id=paper_id,
                title=title,
                source="local",
                extracted_at="2026-07-23T00:00:00Z",
                blocks=[Block(index=0, type="paragraph", original="Method evidence.")],
            )
        )

    collection = await db_module.create_collection("缓存刷新专题")
    await db_module.add_paper_to_collection(collection["id"], "paper-one")
    initial = await db_module.get_collection(collection["id"])
    assert initial is not None
    cached = _build_collection_agent_report(initial)

    await db_module.add_paper_to_collection(collection["id"], "paper-two")
    add_annotation(
        "paper-two",
        0,
        "original",
        "Method evidence",
        note="需要继续核对训练设置。",
        kind="question",
    )
    current = await db_module.get_collection(collection["id"])
    assert current is not None
    return cached, current


if __name__ == "__main__":
    unittest.main()
