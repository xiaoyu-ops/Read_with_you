"""Stage 5 regression coverage for structured Pet Run results."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.api import routes_agent_chat
from backend.extraction.blocks import Block, PaperDocument
from backend.storage import agent_workspace, db as db_module
from backend.tools import build_mock_tool_registry


class AgentRunResultTest(unittest.TestCase):
    def test_long_task_resumes_same_run_and_persists_structured_result(self) -> None:
        async def scenario() -> None:
            await db_module.init_db()
            await db_module.insert_paper(
                arxiv_id="1706.03762",
                title="Attention Is All You Need",
                authors=["A. Vaswani"],
                source="ar5iv",
                file_path="/tmp/1706.03762",
            )

            document = PaperDocument(
                paper_id="1706.03762",
                title="Attention Is All You Need",
                source="ar5iv",
                extracted_at="2026-07-10T00:00:00+00:00",
                blocks=[
                    Block(
                        index=1,
                        type="paragraph",
                        original="The method uses multi-head attention followed by feed-forward layers.",
                        translation="该方法使用多头注意力和前馈层。",
                    )
                ],
            )

            class Client:
                def __init__(self) -> None:
                    self.tool_round = 0

                async def acomplete_with_tools(self, messages, tools, **kwargs):
                    self.tool_round += 1
                    if self.tool_round == 1:
                        return {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "method-1",
                                    "name": "local_method_explanation",
                                    "arguments": {"reason": "用户要求拆解方法"},
                                }
                            ],
                        }
                    assert messages[-1]["role"] == "tool"
                    return {"content": "方法主链路是多头注意力后接前馈层。", "tool_calls": []}

                async def acomplete(self, messages, **kwargs):
                    assert kwargs["task"] == "agent_summary"
                    return (
                        '{"summary":"多头注意力先聚合上下文，再交给前馈层变换。",'
                        '"steps":["多头注意力","前馈层"],'
                        '"terms":[{"term":"多头注意力","meaning":"并行关注多个表示子空间。"}],'
                        '"assumptions":["正文没有给出此处的全部实现细节。"],'
                        '"next_questions":["要继续看注意力公式吗？"]}'
                    )

            client = Client()
            with (
                patch.object(routes_agent_chat, "get_client", return_value=client),
                patch.object(routes_agent_chat, "build_agent_tool_registry", return_value=build_mock_tool_registry()),
                patch.object(routes_agent_chat.files, "load_document", return_value=document),
            ):
                first = await routes_agent_chat.send_agent_message(
                    "1706.03762",
                    routes_agent_chat.AgentChatRequest(message="系统拆解这篇论文的方法"),
                    background_tasks=None,  # type: ignore[arg-type]
                )
                assert first.created_runs[0].status == "waiting_permission"
                run_id = first.created_runs[0].id

                stream = await routes_agent_chat.resume_agent_run_stream(
                    "1706.03762",
                    run_id,
                    routes_agent_chat.AgentRunResumeRequest(approved_permission="long_task"),
                )
                async for _ in stream.body_iterator:
                    pass

            run = agent_workspace.get_run("1706.03762", run_id)
            assert run is not None
            assert run["status"] == "done"
            assert run["result"] == "方法主链路是多头注意力后接前馈层。"
            assert run["result_data"] == {
                "summary": "方法主链路是多头注意力后接前馈层。",
                "evidence": [
                    {
                        "claim": "并行关注多个表示子空间。",
                        "source": "多头注意力",
                        "confidence": "medium",
                        "source_type": "tool_result",
                        "source_label": "工具结果",
                    }
                ],
                "limits": ["正文没有给出此处的全部实现细节。"],
                "next_questions": ["要继续看注意力公式吗？"],
            }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "agent_workspace"
            with (
                patch.dict(os.environ, {"PEINIDU_LLM_INTENT": "1"}),
                patch.object(db_module, "DB_PATH", root / "papers.db"),
                patch.object(agent_workspace, "AGENT_WORKSPACE_DIR", workspace),
                patch.object(agent_workspace, "MEMORY_PATH", workspace / "memory.json"),
                patch.object(agent_workspace, "SKILLS_PATH", workspace / "skills.json"),
            ):
                asyncio.run(scenario())

    def test_old_run_without_result_data_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "agent_workspace"
            with patch.object(agent_workspace, "AGENT_WORKSPACE_DIR", workspace):
                agent_workspace._save_runs(  # type: ignore[attr-defined]
                    "1706.03762",
                    [
                        {
                            "id": "old-run",
                            "arxiv_id": "1706.03762",
                            "task_type": "method_explanation",
                            "title": "方法拆解",
                            "status": "done",
                            "user_message": "解释方法",
                            "inputs": [],
                            "result": "旧版结果",
                            "error": "",
                            "task_id": None,
                            "created_at": "2026-01-01T00:00:00+00:00",
                            "updated_at": "2026-01-01T00:00:00+00:00",
                            "completed_at": "2026-01-01T00:00:00+00:00",
                        }
                    ],
                )
                run = routes_agent_chat.AgentRunItem(**agent_workspace.load_runs("1706.03762")[0])
                assert run.result == "旧版结果"
                assert run.result_data is None
