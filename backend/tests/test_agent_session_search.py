import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
from fastapi import BackgroundTasks

from backend.api import main as api_main
from backend.api import routes_agent_chat
from backend.storage import agent_workspace, db as db_module
from backend.storage.agent_session_index import search_agent_sessions, sync_agent_session_index


class AgentSessionIndexTest(unittest.TestCase):
    def test_backfill_filters_mechanical_messages_and_searches_queries(self) -> None:
        async def scenario() -> None:
            await db_module.init_db()
            await db_module.insert_paper(
                arxiv_id="1706.03762",
                title="Attention Is All You Need",
                authors=["A. Vaswani"],
                source="ar5iv",
                file_path="/tmp/1706.03762",
            )
            agent_workspace.save_chat(
                "1706.03762",
                [
                    {
                        "id": "user-1",
                        "role": "user",
                        "content": "我们讨论公式时要保留英文变量，并比较 C++ 与 Python_3。",
                        "created_at": "2026-07-01T00:00:00+00:00",
                        "meta": {},
                    },
                    {
                        "id": "assistant-1",
                        "role": "assistant",
                        "content": "Transformer 使用自注意力建模长距离依赖。",
                        "created_at": "2026-07-01T00:01:00+00:00",
                        "meta": {"kind": "agent_loop", "agent_loop_status": "completed"},
                    },
                    {
                        "id": "permission",
                        "role": "assistant",
                        "content": "需要确认保存记忆。",
                        "created_at": "2026-07-01T00:02:00+00:00",
                        "meta": {"kind": "permission_request"},
                    },
                    {
                        "id": "run-result",
                        "role": "assistant",
                        "content": "后台任务完成。",
                        "created_at": "2026-07-01T00:03:00+00:00",
                        "meta": {"kind": "agent_run_result"},
                    },
                    {
                        "id": "mcp-draft",
                        "role": "assistant",
                        "content": "MCP 配置草稿。",
                        "created_at": "2026-07-01T00:04:00+00:00",
                        "meta": {"mcp_config_draft": {"name": "demo"}},
                    },
                    {
                        "id": "welcome",
                        "role": "assistant",
                        "content": "欢迎回来。",
                        "created_at": "2026-07-01T00:05:00+00:00",
                        "meta": {"kind": "welcome"},
                    },
                    {
                        "id": "loop-error",
                        "role": "assistant",
                        "content": "执行失败，请稍后重试。",
                        "created_at": "2026-07-01T00:06:00+00:00",
                        "meta": {"kind": "agent_loop", "agent_loop_status": "error"},
                    },
                ],
            )

            self.assertEqual(await sync_agent_session_index(), 1)
            two_char = await search_agent_sessions("公式")
            self.assertEqual([item["message_id"] for item in two_char], ["user-1"])
            trigram = await search_agent_sessions("Transformer")
            self.assertEqual([item["message_id"] for item in trigram], ["assistant-1"])
            excluded = await search_agent_sessions("公式", exclude_message_id="user-1")
            self.assertEqual(excluded, [])
            special = await search_agent_sessions("C++")
            self.assertEqual([item["message_id"] for item in special], ["user-1"])
            self.assertEqual(await search_agent_sessions("确认保存"), [])
            self.assertEqual(await search_agent_sessions("后台任务"), [])
            self.assertEqual(await search_agent_sessions("MCP 配置"), [])
            self.assertEqual(await search_agent_sessions("欢迎回来"), [])
            self.assertEqual(await search_agent_sessions("执行失败"), [])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(db_module, "DB_PATH", root / "papers.db"),
                patch.object(agent_workspace, "AGENT_WORKSPACE_DIR", root / "agent_workspace"),
            ):
                asyncio.run(scenario())

    def test_incremental_update_clear_and_missing_json_cleanup(self) -> None:
        async def scenario() -> None:
            await db_module.init_db()
            await db_module.insert_paper("paper-1", "Paper One", ["A"], "ar5iv", "/tmp/paper-1")
            agent_workspace.save_chat(
                "paper-1",
                [
                    {
                        "id": "old",
                        "role": "user",
                        "content": "旧问题关键词",
                        "created_at": "2026-07-01T00:00:00+00:00",
                        "meta": {},
                    }
                ],
            )
            self.assertEqual(await sync_agent_session_index(), 1)
            self.assertEqual(await sync_agent_session_index(), 0)

            agent_workspace.append_message("paper-1", "assistant", "新答案关键词")
            self.assertEqual(len(await search_agent_sessions("新答案")), 1)
            self.assertEqual(len(await search_agent_sessions("旧问题")), 1)

            agent_workspace.clear_chat("paper-1")
            self.assertEqual(await search_agent_sessions("旧问题"), [])
            self.assertEqual(await search_agent_sessions("新答案"), [])

            chat_path = agent_workspace._chat_path("paper-1")
            chat_path.unlink()
            self.assertEqual(await sync_agent_session_index(), 1)
            db = sqlite3.connect(root / "papers.db")
            try:
                count = db.execute("SELECT COUNT(*) FROM agent_session_index_state").fetchone()[0]
            finally:
                db.close()
            self.assertEqual(count, 0)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(db_module, "DB_PATH", root / "papers.db"),
                patch.object(agent_workspace, "AGENT_WORKSPACE_DIR", root / "agent_workspace"),
            ):
                asyncio.run(scenario())

    def test_like_escape_deleted_paper_and_limit(self) -> None:
        async def scenario() -> None:
            await db_module.init_db()
            await db_module.insert_paper("paper-2", "Percent Paper", ["A"], "ar5iv", "/tmp/paper-2")
            agent_workspace.save_chat(
                "paper-2",
                [
                    {
                        "id": f"message-{index}",
                        "role": "user",
                        "content": f"literal %_ marker {index}",
                        "created_at": f"2026-07-01T00:00:{index:02d}+00:00",
                        "meta": {},
                    }
                    for index in range(5)
                ],
            )
            await sync_agent_session_index()
            self.assertEqual(len(await search_agent_sessions("%_", limit=2)), 2)

            async with aiosqlite.connect(db_module.DB_PATH) as db:
                await db.execute("DELETE FROM papers WHERE arxiv_id='paper-2'")
                await db.commit()
            result = await search_agent_sessions("literal", limit=1)
            self.assertEqual(result[0]["paper_title"], "Percent Paper")
            self.assertFalse(result[0]["paper_exists"])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(db_module, "DB_PATH", root / "papers.db"),
                patch.object(agent_workspace, "AGENT_WORKSPACE_DIR", root / "agent_workspace"),
            ):
                asyncio.run(scenario())


class AgentSessionApiAndToolTest(unittest.TestCase):
    def test_app_startup_backfills_session_index(self) -> None:
        async def scenario() -> None:
            with (
                patch.object(api_main, "_ensure_runtime_dirs"),
                patch.object(api_main, "init_db", new=AsyncMock()),
                patch.object(api_main, "sync_agent_session_index", new=AsyncMock()) as sync_index,
                patch.object(api_main, "sweep_stale_agent_tasks", new=AsyncMock(return_value=0)),
                patch.object(api_main, "sweep_stale_runs", return_value=0),
                patch.object(api_main, "sweep_stale_pdf_export_runs", new=AsyncMock(return_value=0)),
            ):
                async with api_main.lifespan(api_main.app):
                    pass
            sync_index.assert_awaited_once_with()

        asyncio.run(scenario())

    def test_search_api_and_deleted_session_clear(self) -> None:
        async def scenario() -> None:
            await db_module.init_db()
            await db_module.insert_paper("paper-3", "History Paper", ["A"], "ar5iv", "/tmp/paper-3")
            agent_workspace.save_chat(
                "paper-3",
                [
                    {
                        "id": "history-user",
                        "role": "user",
                        "content": "我们以前讨论过数据去重。",
                        "created_at": "2026-07-01T00:00:00+00:00",
                        "meta": {},
                    }
                ],
            )
            results = await routes_agent_chat.search_agent_session_history("数据去重", 20)
            self.assertEqual(results[0].message_id, "history-user")
            self.assertTrue(results[0].paper_exists)

            async with aiosqlite.connect(db_module.DB_PATH) as db:
                await db.execute("DELETE FROM papers WHERE arxiv_id='paper-3'")
                await db.commit()
            chats = await routes_agent_chat.get_agent_chats()
            self.assertFalse(chats[0].paper_exists)
            cleared = await routes_agent_chat.clear_agent_chat("paper-3")
            self.assertEqual(cleared.arxiv_id, "paper-3")
            self.assertEqual(agent_workspace.load_chat("paper-3")["messages"], [])
            self.assertEqual(await search_agent_sessions("数据去重"), [])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.object(db_module, "DB_PATH", root / "papers.db"),
                patch.object(agent_workspace, "AGENT_WORKSPACE_DIR", root / "agent_workspace"),
            ):
                asyncio.run(scenario())

    def test_pet_uses_read_only_session_search_without_permission(self) -> None:
        class SessionSearchClient:
            def __init__(self) -> None:
                self.calls = 0

            async def acomplete_with_tools(self, messages, tools, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    names = {tool["function"]["name"] for tool in tools}
                    assert "local_session_search" in names
                    return {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "session-search-call",
                                "name": "local_session_search",
                                "arguments": {"query": "数据去重", "limit": 5},
                            }
                        ],
                    }
                assert messages[-1]["role"] == "tool"
                assert "SemDeDup" in messages[-1]["content"]
                return {"content": "我们之前在 SemDeDup 中讨论过数据去重。", "tool_calls": []}

            async def acomplete(self, messages, **kwargs):
                return "达到上限"

        async def fake_search(query: str, limit: int = 20, *, exclude_message_id=None):
            self.assertEqual(query, "数据去重")
            self.assertTrue(exclude_message_id)
            return [
                {
                    "arxiv_id": "2303.09540",
                    "message_id": "old-message",
                    "paper_title": "SemDeDup",
                    "paper_exists": True,
                    "role": "assistant",
                    "snippet": "数据去重能降低训练集冗余。",
                    "created_at": "2026-07-01T00:00:00+00:00",
                }
            ]

        async def scenario() -> None:
            await db_module.init_db()
            await db_module.insert_paper("1706.03762", "Attention", ["A"], "ar5iv", "/tmp/attention")
            client = SessionSearchClient()
            with (
                patch.object(routes_agent_chat, "get_client", return_value=client),
                patch.object(routes_agent_chat, "search_agent_sessions", side_effect=fake_search),
                patch.object(routes_agent_chat.files, "load_document", return_value=None),
            ):
                response = await routes_agent_chat.send_agent_message(
                    "1706.03762",
                    routes_agent_chat.AgentChatRequest(message="我们之前在哪篇论文讨论过数据去重？"),
                    BackgroundTasks(),
                )
            self.assertEqual(response.created_runs, [])
            self.assertNotIn("permission_request", response.assistant_message.meta)
            self.assertIn("SemDeDup", response.assistant_message.content)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch.dict(os.environ, {"PEINIDU_LLM_INTENT": "1"}),
                patch.object(db_module, "DB_PATH", root / "papers.db"),
                patch.object(agent_workspace, "AGENT_WORKSPACE_DIR", root / "agent_workspace"),
                patch.object(agent_workspace, "MEMORY_PATH", root / "agent_workspace" / "memory.json"),
            ):
                asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
