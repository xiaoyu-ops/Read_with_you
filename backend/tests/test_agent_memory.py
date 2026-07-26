from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import BackgroundTasks

from backend.api import routes_agent_chat
from backend.storage import agent_workspace, db as db_module


class AgentMemoryStorageTest(unittest.TestCase):
    def test_legacy_memory_is_readable_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory_path = Path(tmp) / "memory.json"
            memory_path.write_text(
                json.dumps(
                    [
                        {
                            "id": "legacy",
                            "kind": "preference",
                            "content": "以后术语保留英文。",
                            "arxiv_id": None,
                            "source": "manual",
                            "created_at": "2026-07-01T00:00:00+00:00",
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with patch.object(agent_workspace, "MEMORY_PATH", memory_path):
                loaded = agent_workspace.load_memories()
                self.assertEqual(loaded[0]["updated_at"], loaded[0]["created_at"])

                saved = agent_workspace.add_memory(
                    "以后术语保留英文！",
                    kind="correction",
                    source="agent",
                )

            self.assertEqual(saved["id"], "legacy")
            self.assertEqual(saved["kind"], "correction")
            self.assertEqual(saved["source"], "agent")
            data = json.loads(memory_path.read_text(encoding="utf-8"))
            self.assertEqual(len(data), 1)

    def test_memory_cap_update_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory_path = Path(tmp) / "memory.json"
            with patch.object(agent_workspace, "MEMORY_PATH", memory_path):
                for index in range(agent_workspace.MAX_MEMORIES + 5):
                    agent_workspace.add_memory(f"memory {index}")
                memories = agent_workspace.load_memories(limit=10_000)
                self.assertEqual(len(memories), agent_workspace.MAX_MEMORIES)
                self.assertEqual(memories[0]["content"], "memory 5")

                target = memories[-1]
                updated = agent_workspace.update_memory(
                    target["id"],
                    content="updated memory",
                    kind="criterion",
                )
                self.assertIsNotNone(updated)
                self.assertEqual(updated["content"], "updated memory")
                self.assertEqual(updated["kind"], "criterion")
                self.assertEqual(agent_workspace.load_memories()[-1]["id"], target["id"])

                duplicate = agent_workspace.add_memory("another memory")
                with self.assertRaisesRegex(ValueError, "相同记忆已存在"):
                    agent_workspace.update_memory(target["id"], content=duplicate["content"])

                deleted = agent_workspace.delete_memory(target["id"])
                self.assertEqual(deleted["id"], target["id"])
                self.assertIsNone(agent_workspace.delete_memory(target["id"]))


class AgentMemoryApiTest(unittest.TestCase):
    def test_global_memory_crud_and_legacy_route(self) -> None:
        async def scenario() -> None:
            await db_module.init_db()
            await db_module.insert_paper(
                arxiv_id="1706.03762",
                title="Attention Is All You Need",
                authors=["A. Vaswani"],
                source="ar5iv",
                file_path="/tmp/1706.03762",
            )
            created = await routes_agent_chat.create_agent_memory(
                routes_agent_chat.AgentMemoryCreateRequest(
                    content="公式符号保留英文",
                    kind="preference",
                )
            )
            listed = await routes_agent_chat.get_agent_memories(limit=100)
            self.assertEqual([item.id for item in listed], [created.id])

            edited = await routes_agent_chat.edit_agent_memory(
                created.id,
                routes_agent_chat.AgentMemoryUpdateRequest(content="公式符号与变量保留英文"),
            )
            self.assertEqual(edited.content, "公式符号与变量保留英文")

            legacy = await routes_agent_chat.save_agent_memory(
                "1706.03762",
                routes_agent_chat.AgentMemoryRequest(content="复现判断优先看超参数"),
            )
            self.assertEqual(legacy.arxiv_id, "1706.03762")

            removed = await routes_agent_chat.remove_agent_memory(created.id)
            self.assertEqual(removed.id, created.id)
            with self.assertRaises(Exception) as caught:
                await routes_agent_chat.remove_agent_memory(created.id)
            self.assertEqual(getattr(caught.exception, "status_code", None), 404)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "agent_workspace"
            with (
                patch.object(db_module, "DB_PATH", root / "papers.db"),
                patch.object(agent_workspace, "AGENT_WORKSPACE_DIR", workspace),
                patch.object(agent_workspace, "MEMORY_PATH", workspace / "memory.json"),
            ):
                asyncio.run(scenario())


class AgentMemoryLoopTest(unittest.TestCase):
    def test_memory_write_waits_for_confirmation_and_resumes_once(self) -> None:
        class MemoryLoopClient:
            def __init__(self) -> None:
                self.calls = 0

            async def acomplete_with_tools(self, messages, tools, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    names = {item["function"]["name"] for item in tools}
                    assert "local_memory_save" in names
                    return {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "memory-call-1",
                                "name": "local_memory_save",
                                "arguments": {
                                    "content": "以后解释公式时保留英文变量，并继续回答当前问题。",
                                    "kind": "preference",
                                    "reason": "用户明确要求以后持续遵守。",
                                },
                            }
                        ],
                    }
                assert messages[-1]["role"] == "tool"
                assert "已保存阅读记忆" in messages[-1]["content"]
                return {
                    "content": "已经按你的确认保存；当前问题的答案是 Transformer 使用自注意力建模依赖。",
                    "tool_calls": [],
                }

            async def acomplete(self, messages, **kwargs):
                return "达到上限"

        async def scenario() -> None:
            await db_module.init_db()
            await db_module.insert_paper(
                arxiv_id="1706.03762",
                title="Attention Is All You Need",
                authors=["A. Vaswani"],
                source="ar5iv",
                file_path="/tmp/1706.03762",
            )
            client = MemoryLoopClient()
            with patch.object(routes_agent_chat, "get_client", return_value=client):
                response = await routes_agent_chat.send_agent_message(
                    "1706.03762",
                    routes_agent_chat.AgentChatRequest(
                        message="以后解释公式时保留英文变量，这篇的核心机制是什么？"
                    ),
                    BackgroundTasks(),
                )
                self.assertEqual(agent_workspace.load_memories(), [])
                permission = response.assistant_message.meta["permission_request"]
                self.assertEqual(permission["scope"], "memory_write")
                self.assertEqual(
                    permission["memory_proposal"]["content"],
                    "以后解释公式时保留英文变量，并继续回答当前问题。",
                )
                run_id = response.created_runs[0].id

                resumed = await routes_agent_chat.resume_agent_run_stream(
                    "1706.03762",
                    run_id,
                    routes_agent_chat.AgentRunResumeRequest(approved_permission="memory_write"),
                )
                async for _ in resumed.body_iterator:
                    pass

            memories = agent_workspace.load_memories(limit=100)
            self.assertEqual(len(memories), 1)
            self.assertEqual(memories[0]["source"], "agent")
            state = await routes_agent_chat.get_agent_chat("1706.03762")
            self.assertEqual(state.runs[-1].status, "done")
            self.assertIn("当前问题的答案", state.messages[-1].content)

            with self.assertRaises(Exception) as caught:
                await routes_agent_chat.resume_agent_run_stream(
                    "1706.03762",
                    run_id,
                    routes_agent_chat.AgentRunResumeRequest(approved_permission="memory_write"),
                )
            self.assertEqual(getattr(caught.exception, "status_code", None), 409)
            self.assertEqual(len(agent_workspace.load_memories(limit=100)), 1)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "agent_workspace"
            with (
                patch.dict(os.environ, {"PEINIDU_LLM_INTENT": "1"}),
                patch.object(db_module, "DB_PATH", root / "papers.db"),
                patch.object(agent_workspace, "AGENT_WORKSPACE_DIR", workspace),
                patch.object(agent_workspace, "MEMORY_PATH", workspace / "memory.json"),
                patch.object(agent_workspace, "SKILLS_PATH", workspace / "skills.json"),
                patch.object(routes_agent_chat.files, "load_document", return_value=None),
                patch.object(routes_agent_chat.files, "load_analysis", return_value=None),
            ):
                asyncio.run(scenario())

    def test_disabled_intent_does_not_auto_save_keyword_memory(self) -> None:
        class ChatClient:
            async def acomplete(self, messages, **kwargs):
                return "可以，你可以在 Agent 页面手动保存这条偏好。"

        async def scenario() -> None:
            await db_module.init_db()
            await db_module.insert_paper(
                arxiv_id="1706.03762",
                title="Attention Is All You Need",
                authors=["A. Vaswani"],
                source="ar5iv",
                file_path="/tmp/1706.03762",
            )
            with patch.object(routes_agent_chat, "get_client", return_value=ChatClient()):
                response = await routes_agent_chat.send_agent_message(
                    "1706.03762",
                    routes_agent_chat.AgentChatRequest(message="以后公式变量保留英文"),
                    BackgroundTasks(),
                )
            self.assertIsNone(response.saved_memory)
            self.assertEqual(agent_workspace.load_memories(), [])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "agent_workspace"
            with (
                patch.dict(os.environ, {"PEINIDU_LLM_INTENT": "0"}),
                patch.object(db_module, "DB_PATH", root / "papers.db"),
                patch.object(agent_workspace, "AGENT_WORKSPACE_DIR", workspace),
                patch.object(agent_workspace, "MEMORY_PATH", workspace / "memory.json"),
                patch.object(agent_workspace, "SKILLS_PATH", workspace / "skills.json"),
                patch.object(routes_agent_chat.files, "load_document", return_value=None),
                patch.object(routes_agent_chat.files, "load_analysis", return_value=None),
            ):
                asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
