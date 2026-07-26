from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import BackgroundTasks, FastAPI
from fastapi.testclient import TestClient

from backend.api import routes_agent_chat, routes_annotations, routes_notes
from backend.extraction.blocks import Block, PaperDocument
from backend.storage import agent_workspace, db as db_module, files as storage_files
from backend.storage.files import add_annotation, load_paper_note, save_document, save_paper_note
from backend.storage.paper_note_index import (
    build_notes_context,
    search_collection_notes,
    search_paper_notes,
    split_markdown_sections,
    sync_paper_note_index,
    view_paper_note,
)


def _document(paper_id: str) -> PaperDocument:
    return PaperDocument(
        paper_id=paper_id,
        title="Note index fixture",
        source="local",
        extracted_at="2026-07-23T00:00:00Z",
        blocks=[
            Block(index=0, type="paragraph", original="Method evidence."),
            Block(index=1, type="paragraph", original="Open question."),
        ],
    )


class PaperNoteIndexTest(unittest.TestCase):
    def test_sections_search_and_distinct_annotation_anchors(self) -> None:
        async def scenario() -> None:
            await db_module.init_db()
            save_document(_document("notes"))
            save_paper_note(
                "notes",
                "# 阅读笔记\n\n总体判断。\n\n## 方法与证据\n\n需要核对消融实验。\n\n## 疑问\n\n泛化能力是否足够？",
                load_paper_note("notes")["revision"],
            )
            first = add_annotation(
                "notes",
                0,
                "original",
                "ablation study",
                note="消融实验缺少数据划分说明",
                kind="method",
                selector={"version": 2, "page": 2, "region_id": "region-0"},
            )
            second = add_annotation(
                "notes",
                1,
                "original",
                "generalization",
                note="消融实验能否支持泛化结论",
                kind="question",
                selector={"version": 2, "page": 3, "region_id": "region-1"},
            )

            self.assertTrue(await sync_paper_note_index("notes"))
            self.assertFalse(await sync_paper_note_index("notes"))
            results = await search_paper_notes("notes", "消融实验", limit=10)

            annotation_results = [
                item for item in results if item["source_type"] == "annotation"
            ]
            self.assertEqual(
                {item["annotation_id"] for item in annotation_results},
                {first["id"], second["id"]},
            )
            self.assertEqual(
                {(item["page"], item["block_index"]) for item in annotation_results},
                {(2, 0), (3, 1)},
            )
            main = await search_paper_notes("notes", "泛化能力", limit=3)
            self.assertEqual(main[0]["heading"], "疑问")
            viewed = view_paper_note("notes", heading="方法与证据")
            self.assertIsNotNone(viewed)
            assert viewed is not None
            self.assertIn("消融实验", viewed["markdown"])

            save_paper_note(
                "notes",
                "# 阅读笔记\n\n已改为只关注鲁棒性。",
                load_paper_note("notes")["revision"],
            )
            self.assertEqual(await search_paper_notes("notes", "泛化能力"), [])
            self.assertEqual(len(await search_paper_notes("notes", "鲁棒性")), 1)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(db_module, "DB_PATH", root / "papers.db"),
                patch.object(storage_files, "PAPERS_DIR", root / "papers"),
            ):
                asyncio.run(scenario())

    def test_context_is_bounded_and_includes_current_selection_note(self) -> None:
        async def scenario() -> None:
            await db_module.init_db()
            save_document(_document("bounded"))
            save_paper_note(
                "bounded",
                "# 很长的思考\n\n" + "方法证据" * 1_000,
                load_paper_note("bounded")["revision"],
            )
            annotation = add_annotation(
                "bounded",
                1,
                "original",
                "Open question",
                note="这是当前选区对应的疑问",
                kind="question",
                selector={"version": 2, "page": 4, "region_id": "active-region"},
            )

            context = await build_notes_context(
                "bounded",
                "方法证据还缺什么？",
                {
                    "region_id": "active-region",
                    "selected_text": {"block_index": 1},
                },
            )

            self.assertTrue(context["has_paper_note"])
            self.assertEqual(context["selection_note_count"], 1)
            self.assertEqual(
                context["current_note"]["annotation_id"],
                annotation["id"],
            )
            self.assertLessEqual(context["snippet_char_count"], 2_000)
            self.assertLessEqual(len(context["relevant"]), 3)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(db_module, "DB_PATH", root / "papers.db"),
                patch.object(storage_files, "PAPERS_DIR", root / "papers"),
            ):
                asyncio.run(scenario())

    def test_note_routes_update_the_index_after_save_edit_and_delete(self) -> None:
        app = FastAPI()
        app.include_router(routes_notes.router)
        app.include_router(routes_annotations.router)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(db_module, "DB_PATH", root / "papers.db"),
                patch.object(storage_files, "PAPERS_DIR", root / "papers"),
            ):
                asyncio.run(db_module.init_db())
                save_document(_document("routes"))
                with TestClient(app) as client:
                    revision = load_paper_note("routes")["revision"]
                    saved = client.put(
                        "/papers/routes/paper-note",
                        json={
                            "markdown": "# 结论\n\n这是主笔记唯一术语。",
                            "base_revision": revision,
                        },
                    )
                    self.assertEqual(saved.status_code, 200)
                    self.assertEqual(
                        len(asyncio.run(search_paper_notes("routes", "唯一术语"))),
                        1,
                    )

                    annotation = storage_files.add_annotation(
                        "routes",
                        0,
                        "original",
                        "Method evidence",
                        note="旧的选区判断",
                    )
                    asyncio.run(sync_paper_note_index("routes"))
                    updated = client.patch(
                        f"/papers/routes/annotations/{annotation['id']}",
                        json={"note": "新的选区判断", "kind": "question"},
                    )
                    self.assertEqual(updated.status_code, 200)
                    self.assertEqual(
                        len(asyncio.run(search_paper_notes("routes", "新的选区"))),
                        1,
                    )
                    deleted = client.delete(
                        f"/papers/routes/annotations/{annotation['id']}"
                    )
                    self.assertEqual(deleted.status_code, 200)
                    self.assertEqual(
                        asyncio.run(search_paper_notes("routes", "新的选区")),
                        [],
                    )


class PaperNoteAgentToolTest(unittest.TestCase):
    def test_pet_searches_notes_without_permission_and_labels_evidence(self) -> None:
        class NoteSearchClient:
            def __init__(self) -> None:
                self.calls = 0

            async def acomplete_with_tools(self, messages, tools, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    names = {tool["function"]["name"] for tool in tools}
                    assert {"local_notes_search", "local_notes_view"} <= names
                    assert "你的论文笔记（有界召回）" in messages[1]["content"]
                    assert "不得补全或猜测缺失文字" in messages[0]["content"]
                    return {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "notes-search-call",
                                "name": "local_notes_search",
                                "arguments": {"query": "数据划分", "limit": 5},
                            }
                        ],
                    }
                assert messages[-1]["role"] == "tool"
                assert "你的笔记" in messages[-1]["content"]
                return {
                    "content": "根据你的笔记，你还没有想清楚数据划分是否一致。",
                    "tool_calls": [],
                }

            async def acomplete(self, messages, **kwargs):
                return "达到上限"

        async def scenario() -> None:
            await db_module.init_db()
            await db_module.insert_paper(
                "agent-notes",
                "Agent notes",
                ["A"],
                "local",
                str(root / "papers" / "agent-notes"),
            )
            save_document(_document("agent-notes"))
            annotation = add_annotation(
                "agent-notes",
                0,
                "original",
                "Method evidence",
                note="需要核对训练和测试的数据划分是否一致",
                kind="question",
                selector={"version": 2, "page": 2, "region_id": "region-0"},
            )
            with patch.object(
                routes_agent_chat,
                "get_client",
                return_value=NoteSearchClient(),
            ):
                response = await routes_agent_chat.send_agent_message(
                    "agent-notes",
                    routes_agent_chat.AgentChatRequest(
                        message="根据我的笔记，还有哪些没想清楚的问题？"
                    ),
                    BackgroundTasks(),
                )
            self.assertNotIn(
                "permission_request",
                response.assistant_message.meta,
            )
            self.assertIn("你的笔记", response.assistant_message.content)
            evidence = response.assistant_message.meta["result_data"]["evidence"]
            self.assertEqual(evidence[0]["annotation_id"], annotation["id"])
            self.assertTrue(evidence[0]["source"].startswith("你的笔记"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.dict(os.environ, {"PEINIDU_LLM_INTENT": "1"}),
                patch.object(db_module, "DB_PATH", root / "papers.db"),
                patch.object(storage_files, "PAPERS_DIR", root / "papers"),
                patch.object(agent_workspace, "AGENT_WORKSPACE_DIR", root / "agent_workspace"),
                patch.object(agent_workspace, "MEMORY_PATH", root / "agent_workspace" / "memory.json"),
            ):
                asyncio.run(scenario())

    def test_annotation_questions_uses_relevant_note_search_results(self) -> None:
        class AnnotationQuestionClient:
            async def acomplete(self, messages, **kwargs):
                prompt = messages[1]["content"]
                assert "较早但相关的数据泄漏疑问" in prompt
                assert "你的笔记" in prompt
                return (
                    '{"summary":"你的笔记主要担心数据泄漏。",'
                    '"evidence":[{"claim":"担心数据泄漏","source":"你的笔记",'
                    '"location":{"block_index":0}}],'
                    '"limits":[],"next_questions":["训练集与测试集如何去重？"]}'
                )

        async def scenario() -> None:
            with (
                patch.object(
                    routes_agent_chat.files,
                    "build_paper_note_summary",
                    return_value={
                        "annotation_count": 20,
                        "has_paper_note": True,
                    },
                ),
                patch.object(
                    routes_agent_chat,
                    "search_paper_notes",
                    return_value=[
                        {
                            "heading": "早期问题",
                            "snippet": "较早但相关的数据泄漏疑问",
                            "block_index": 0,
                        }
                    ],
                ) as search,
                patch.object(
                    routes_agent_chat,
                    "get_client",
                    return_value=AnnotationQuestionClient(),
                ),
            ):
                result = await routes_agent_chat._annotation_questions_data(
                    "paper",
                    "请根据数据泄漏相关笔记整理问题",
                    {},
                )
            search.assert_awaited_once_with(
                "paper",
                "请根据数据泄漏相关笔记整理问题",
                limit=12,
            )
            self.assertIn("你的笔记", result["summary"])
            self.assertEqual(result["evidence"][0]["source"], "你的笔记")

        asyncio.run(scenario())

    def test_pet_searches_current_collection_notes_with_distinct_paper_ids(self) -> None:
        class CollectionNoteClient:
            def __init__(self) -> None:
                self.calls = 0

            async def acomplete_with_tools(self, messages, tools, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "collection-notes-call",
                                "name": "local_notes_search",
                                "arguments": {
                                    "query": "数据划分",
                                    "scope": "current_collection",
                                    "limit": 10,
                                },
                            }
                        ],
                    }
                assert messages[-1]["role"] == "tool"
                assert "Current paper" in messages[-1]["content"]
                assert "Other paper" in messages[-1]["content"]
                return {
                    "content": "当前专题中，两篇论文的你的笔记都提到了数据划分。",
                    "tool_calls": [],
                }

            async def acomplete(self, messages, **kwargs):
                return "达到上限"

        async def scenario() -> None:
            await db_module.init_db()
            for paper_id, title in (
                ("current-paper", "Current paper"),
                ("other-paper", "Other paper"),
            ):
                await db_module.insert_paper(
                    paper_id,
                    title,
                    ["A"],
                    "local",
                    str(root / "papers" / paper_id),
                )
                save_document(_document(paper_id))
                add_annotation(
                    paper_id,
                    0,
                    "original",
                    "Method evidence",
                    note=f"{title} 的数据划分需要核对",
                    kind="question",
                )
            collection = await db_module.create_collection("共同专题")
            await db_module.add_paper_to_collection(collection["id"], "current-paper")
            await db_module.add_paper_to_collection(collection["id"], "other-paper")

            direct = await search_collection_notes(
                int(collection["id"]),
                "数据划分",
                limit=10,
            )
            self.assertEqual(
                {item["arxiv_id"] for item in direct},
                {"current-paper", "other-paper"},
            )

            with patch.object(
                routes_agent_chat,
                "get_client",
                return_value=CollectionNoteClient(),
            ):
                response = await routes_agent_chat.send_agent_message(
                    "current-paper",
                    routes_agent_chat.AgentChatRequest(
                        message="比较当前专题里我的数据划分笔记。"
                    ),
                    BackgroundTasks(),
                )
            self.assertNotIn("permission_request", response.assistant_message.meta)
            evidence = response.assistant_message.meta["result_data"]["evidence"]
            self.assertEqual(
                {item["arxiv_id"] for item in evidence},
                {"current-paper", "other-paper"},
            )
            self.assertTrue(all(item["source"].startswith("你的笔记") for item in evidence))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.dict(os.environ, {"PEINIDU_LLM_INTENT": "1"}),
                patch.object(db_module, "DB_PATH", root / "papers.db"),
                patch.object(storage_files, "PAPERS_DIR", root / "papers"),
                patch.object(agent_workspace, "AGENT_WORKSPACE_DIR", root / "agent_workspace"),
                patch.object(agent_workspace, "MEMORY_PATH", root / "agent_workspace" / "memory.json"),
            ):
                asyncio.run(scenario())


class MarkdownSectionTest(unittest.TestCase):
    def test_keeps_intro_and_heading_order(self) -> None:
        self.assertEqual(
            split_markdown_sections("前言\n\n# A\n甲\n## B\n乙"),
            [
                {"heading": "全文笔记", "level": 0, "content": "前言", "order": 0},
                {"heading": "A", "level": 1, "content": "甲", "order": 1},
                {"heading": "B", "level": 2, "content": "乙", "order": 2},
            ],
        )


if __name__ == "__main__":
    unittest.main()
