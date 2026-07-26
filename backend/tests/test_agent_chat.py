from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import BackgroundTasks

from backend.api import routes_agent_chat
from backend.extraction.blocks import Block, PaperDocument
from backend.storage import agent_workspace, db as db_module
from backend.tools import build_mock_tool_registry
from backend.tools.registry import ToolRegistry, ToolSpec


class AgentChatRouteTest(unittest.TestCase):
    def test_chat_creates_visible_subtask_without_legacy_auto_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "papers.db"
            workspace = root / "agent_workspace"
            with (
                patch.dict(os.environ, {"PEINIDU_LLM_INTENT": "0"}),
                patch.object(db_module, "DB_PATH", db_path),
                patch.object(agent_workspace, "AGENT_WORKSPACE_DIR", workspace),
                patch.object(agent_workspace, "MEMORY_PATH", workspace / "memory.json"),
                patch.object(agent_workspace, "SKILLS_PATH", workspace / "skills.json"),
                patch.object(routes_agent_chat.files, "load_document", return_value=None),
                patch.object(routes_agent_chat.files, "load_analysis", return_value=None),
            ):
                asyncio.run(_exercise_chat_flow())

    def test_append_message_caps_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "agent_workspace"
            with patch.object(agent_workspace, "AGENT_WORKSPACE_DIR", workspace):
                total = agent_workspace.MAX_CHAT_MESSAGES + 30
                for i in range(total):
                    agent_workspace.append_message("2000.00001", "user", f"m{i}")
                messages = agent_workspace.load_chat("2000.00001")["messages"]
                assert len(messages) == agent_workspace.MAX_CHAT_MESSAGES
                assert messages[-1]["content"] == f"m{total - 1}"
                assert messages[0]["content"] == f"m{total - agent_workspace.MAX_CHAT_MESSAGES}"

    def test_chat_history_filters_mechanical_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "agent_workspace"
            with patch.object(agent_workspace, "AGENT_WORKSPACE_DIR", workspace):
                agent_workspace.append_message("2000.00001", "user", "解释一下摘要")
                agent_workspace.append_message(
                    "2000.00001",
                    "assistant",
                    "需要联网检索，是否确认？",
                    meta={"kind": "permission_request"},
                )
                agent_workspace.append_message(
                    "2000.00001",
                    "assistant",
                    "后台任务完成：结果",
                    meta={"kind": "agent_run_result"},
                )
                agent_workspace.append_message(
                    "2000.00001",
                    "assistant",
                    "我准备了一份 MCP 配置草稿",
                    meta={"mcp_config_draft": {"name": "paper-search"}},
                )
                agent_workspace.append_message("2000.00001", "assistant", "这是正常回答")
                history = routes_agent_chat._chat_history_items("2000.00001", "继续")

        assert [item["content"] for item in history] == ["解释一下摘要", "这是正常回答"]

    def test_agent_chat_request_rejects_huge_context(self) -> None:
        with self.assertRaises(ValueError):
            routes_agent_chat.AgentChatRequest(
                message="解释一下",
                context={"reader": {"selected_text": {"text": "x" * (routes_agent_chat.MAX_AGENT_CONTEXT_BYTES + 1)}}},
            )

    def test_list_chat_summaries_orders_by_updated_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "agent_workspace"
            with patch.object(agent_workspace, "AGENT_WORKSPACE_DIR", workspace):
                agent_workspace.append_message("2000.00001", "user", "第一篇的问题")
                agent_workspace.append_message("2000.00002", "assistant", "第二篇的回答")
                summaries = agent_workspace.list_chat_summaries()

        assert [item["arxiv_id"] for item in summaries] == ["2000.00002", "2000.00001"]
        assert summaries[0]["last_role"] == "assistant"
        assert summaries[0]["last_message"] == "第二篇的回答"

    def test_paper_opening_skips_layout_junk(self) -> None:
        class FakeBlock:
            def __init__(self, block_type: str, original: str) -> None:
                self.type = block_type
                self.original = original

        class FakeDoc:
            blocks = [
                FakeBlock("paragraph", "0.1pt \\contournumber 10"),
                FakeBlock(
                    "paragraph",
                    "Progress in machine learning has been driven by data scale and quality.",
                ),
            ]

        with patch.object(routes_agent_chat.files, "load_document", return_value=FakeDoc()):
            opening = routes_agent_chat._paper_opening("2303.09540")
        assert opening.startswith("Progress in machine learning")

    def test_stream_agent_message_emits_tool_events_and_done(self) -> None:
        class FakeRegistry:
            def get(self, name):
                return SimpleNamespace(name=name, permission_scope="external_search", source="local")

            def list(self):
                return []

            async def execute(self, name, arguments, permission_scope=None):
                return routes_agent_chat.ToolResult(
                    name=name,
                    content="检索结果",
                    evidence=({"kind": "external_paper_search_result", "title": "Related Paper"},),
                    metadata={"mock": False},
                )

        class FakeSummaryClient:
            async def acomplete(self, messages, **kwargs):
                return "已整理外部检索结果。"

        async def scenario() -> None:
            await db_module.init_db()
            await db_module.insert_paper(
                arxiv_id="2202.09741",
                title="Visual Attention Network",
                authors=["Meng-Hao Guo"],
                source="ar5iv",
                file_path="/tmp/2202.09741",
            )
            with (
                patch.object(routes_agent_chat.files, "load_document", return_value=None),
                patch.object(routes_agent_chat, "build_agent_tool_registry", return_value=FakeRegistry()),
                patch.object(routes_agent_chat, "get_client", return_value=FakeSummaryClient()),
            ):
                response = await routes_agent_chat.stream_agent_message(
                    "2202.09741",
                    routes_agent_chat.AgentChatRequest(
                        message="查一下外部复现",
                        context={"approved_permission": "external_search"},
                    ),
                )
                chunks: list[str] = []
                async for chunk in response.body_iterator:
                    chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)

            raw = "".join(chunks)
            assert "event: message" in raw
            assert "event: tool_event" in raw
            assert '"type": "tool_start"' in raw
            assert '"type": "tool_done"' in raw
            assert "event: done" in raw
            state = agent_workspace.load_runs("2202.09741")
            assert state[-1]["status"] == "done"
            chat = agent_workspace.load_chat("2202.09741")
            assert chat["messages"][-1]["meta"]["kind"] == "agent_run_result"
            assert chat["messages"][-1]["meta"]["tool_trace"]["evidence_count"] == 1

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "agent_workspace"
            with (
                patch.dict(os.environ, {"PEINIDU_LLM_INTENT": "0"}),
                patch.object(db_module, "DB_PATH", root / "papers.db"),
                patch.object(agent_workspace, "AGENT_WORKSPACE_DIR", workspace),
                patch.object(agent_workspace, "MEMORY_PATH", workspace / "memory.json"),
                patch.object(agent_workspace, "SKILLS_PATH", workspace / "skills.json"),
            ):
                asyncio.run(scenario())

    def test_stream_agent_message_emits_chat_deltas_and_persists_reply(self) -> None:
        class FakeChatClient:
            async def astream_with_tools(self, messages, tools, **kwargs):
                yield {"type": "content_delta", "content": "这篇论文"}
                yield {"type": "content_delta", "content": "主要讨论视觉注意力。"}
                yield {"type": "response", "content": "这篇论文主要讨论视觉注意力。", "tool_calls": []}

            async def astream(self, messages, **kwargs):
                yield "这篇论文"
                yield "主要讨论视觉注意力。"

        async def scenario() -> None:
            await db_module.init_db()
            await db_module.insert_paper(
                arxiv_id="2202.09741",
                title="Visual Attention Network",
                authors=["Meng-Hao Guo"],
                source="ar5iv",
                file_path="/tmp/2202.09741",
            )
            with (
                patch.object(routes_agent_chat.files, "load_document", return_value=None),
                patch.object(
                    routes_agent_chat,
                    "_classify_message_llm",
                    return_value={
                        "category": "chat",
                        "task_type": None,
                        "permission_scope": None,
                        "save_memory": False,
                        "confidence": "high",
                        "reason": "普通聊天",
                        "source": "llm_intent",
                    },
                ),
                patch.object(routes_agent_chat, "get_client", return_value=FakeChatClient()),
            ):
                response = await routes_agent_chat.stream_agent_message(
                    "2202.09741",
                    routes_agent_chat.AgentChatRequest(message="这篇讲什么？", context={}),
                )
                chunks: list[str] = []
                async for chunk in response.body_iterator:
                    chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)

            raw = "".join(chunks)
            assert "event: agent_event" in raw
            assert '"status": "planning"' in raw
            assert "event: delta" in raw
            assert "这篇论文" in raw
            assert "主要讨论视觉注意力" in raw
            assert "event: done" in raw
            chat = agent_workspace.load_chat("2202.09741")
            assert chat["messages"][-1]["role"] == "assistant"
            assert chat["messages"][-1]["content"] == "这篇论文主要讨论视觉注意力。"
            assert chat["messages"][-1]["meta"]["kind"] == "agent_loop"

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

    def test_identity_question_uses_product_boundary(self) -> None:
        reply = routes_agent_chat._identity_boundary_reply("你不是deepseek吗？你是什么模型？")

        assert reply is not None
        assert "陪你读" in reply
        assert "不会臆测" in reply
        assert "provider" in reply

    def test_selection_prompt_keeps_translation_selection_and_neighbors(self) -> None:
        context = {
            "paper_title": "Visual Attention Network",
            "reader": {
                "reader_mode": "inline_translation",
                "page": 4,
                "region_id": "region-method-8",
                "layout_confidence": 0.96,
                "render_policy": "replace",
                "selected_text": {
                    "block_index": 8,
                    "side": "translation",
                    "text": "大核注意力能够捕获长程依赖。",
                },
                "active_block": {
                    "index": 8,
                    "type": "paragraph",
                    "original": "Large kernel attention captures long-range dependencies.",
                    "translation": "大核注意力能够捕获长程依赖。",
                    "status": "done",
                },
                "previous_block": {
                    "index": 7,
                    "type": "paragraph",
                    "original": "The method decomposes attention.",
                    "translation": "该方法分解注意力。",
                    "status": "done",
                },
                "next_block": {
                    "index": 9,
                    "type": "paragraph",
                    "original": "This design keeps computation efficient.",
                    "translation": "这种设计保持计算高效。",
                    "status": "done",
                },
                "right_pane_side": "translation",
            },
        }

        with patch.object(routes_agent_chat.files, "load_document", return_value=None):
            messages = routes_agent_chat._build_selection_prompt(
                "2202.09741",
                "这是什么意思？",
                context,
            )

        prompt = messages[-1]["content"]
        assert "选区: 译文 #8" in prompt
        assert "大核注意力能够捕获长程依赖。" in prompt
        assert "原文: Large kernel attention captures long-range dependencies." in prompt
        assert "译文: 该方法分解注意力。" in prompt
        assert "译文: 这种设计保持计算高效。" in prompt
        assert "reader_mode: inline_translation" in prompt
        assert "PDF page: 4" in prompt
        assert "region_id: region-method-8" in prompt
        assert "layout_confidence: 0.960" in prompt
        assert "render_policy: replace" in prompt
        assert "可作为当前 PDF page/region 的定位线索" in prompt
        assert "不能声称已精准定位" in messages[0]["content"]

    def test_reader_context_location_limits_and_legacy_side_fallback(self) -> None:
        base_reader = {
            "reader_mode": "inline_translation",
            "page": 7,
            "region_id": "region-7",
            "layout_confidence": 0.96,
            "render_policy": "replace",
            "active_block": {
                "index": 12,
                "type": "paragraph",
                "original": "Reader context must remain grounded.",
                "translation": "阅读上下文必须保持有据可查。",
            },
        }

        selection_reader = routes_agent_chat._normalize_reader_context({
            "reader": {
                **base_reader,
                "reader_mode": "selection_translation",
                "render_policy": "preserve",
                "selected_text": {
                    "block_index": 12,
                    "side": "original",
                    "text": "Reader context",
                },
            }
        })
        assert selection_reader is not None
        assert selection_reader["reader_mode"] == "selection_translation"
        assert routes_agent_chat._reader_location_limitations(selection_reader) == []

        selected_wins = routes_agent_chat._reader_context_sections(
            {
                "reader": {
                    **base_reader,
                    "selected_text": {
                        "block_index": 12,
                        "side": "original",
                        "text": "Reader context",
                    },
                    "right_pane_side": "translation",
                }
            }
        )
        assert "选区: 原文 #12" in selected_wins["selected"]

        legacy_fallback = routes_agent_chat._reader_context_sections(
            {
                "reader": {
                    "selected_text": {"block_index": 3, "text": "legacy selection"},
                    "right_pane_side": "original",
                    "active_block": base_reader["active_block"],
                }
            }
        )
        assert "选区: 原文 #3" in legacy_fallback["selected"]
        assert "不能声称已精准定位到 PDF 原页区域" in legacy_fallback["location"]
        fallback_reply = routes_agent_chat._selection_fallback_result(
            {
                "reader": {
                    "selected_text": {"block_index": 3, "text": "legacy selection"},
                    "right_pane_side": "original",
                }
            }
        )
        assert "无法确认它在 PDF 原页上的精准位置" in fallback_reply
        assert "精准位置。\n\n可继续追问" in fallback_reply

        unknown_side = routes_agent_chat._reader_context_sections(
            {
                "reader": {
                    **base_reader,
                    "selected_text": {
                        "block_index": 12,
                        "side": ["invalid"],
                        "text": "ambiguous selection",
                    },
                    "right_pane_side": {"invalid": True},
                    "render_policy": ["replace"],
                }
            }
        )
        assert "选区: 侧别未知 #12" in unknown_side["selected"]
        assert "选区: 译文" not in unknown_side["selected"]
        assert "缺少 render_policy" in unknown_side["location"]

        unreliable_cases = (
            ({**base_reader, "layout_confidence": 0.89}, "layout_confidence=0.890 低于 0.90"),
            ({**base_reader, "render_policy": "panel_only"}, "render_policy=panel_only"),
            ({**base_reader, "region_id": None}, "缺少 region_id"),
        )
        for reader, reason in unreliable_cases:
            with self.subTest(reason=reason):
                sections = routes_agent_chat._reader_context_sections({"reader": reader})
                assert reason in sections["location"]
                assert "不能声称已精准定位到 PDF 原页区域" in sections["location"]

        reliable_reply = routes_agent_chat._reader_context_reply(
            {
                "reader": {
                    **base_reader,
                    "selected_text": {
                        "block_index": 12,
                        "side": "original",
                        "text": "Reader context",
                    },
                }
            }
        )
        assert reliable_reply is not None
        assert "无法确认它在 PDF 原页上的精准位置" not in reliable_reply

    def test_context_pack_builder_collects_key_blocks_and_history(self) -> None:
        doc = PaperDocument(
            paper_id="1234.56789",
            title="Context Packs for Paper Reading",
            source="ar5iv",
            extracted_at="",
            blocks=[
                Block(index=0, type="heading", original="Abstract", level=1),
                Block(index=1, type="paragraph", original="We propose context packs for paper chat."),
                Block(index=2, type="heading", original="1 Introduction", level=1),
                Block(index=3, type="paragraph", original="Readers need global context beyond one paragraph."),
                Block(index=4, type="heading", original="2 Method", level=1),
                Block(index=5, type="paragraph", original="The method builds metadata and section snippets."),
                Block(index=6, type="heading", original="5 Conclusion", level=1),
                Block(index=7, type="paragraph", original="Context packs improve answer grounding."),
            ],
        )
        chat = {
            "messages": [
                {"role": "assistant", "content": "欢迎"},
                {"role": "user", "content": "之前的问题"},
                {"role": "assistant", "content": "之前的回答"},
                {"role": "user", "content": "方法是什么？"},
            ]
        }
        context = {
            "source": "pet",
            "paper_title": "Context Packs for Paper Reading",
            "paper_authors": ["Ada Reader", "Bo Builder"],
            "paper_source": "ar5iv",
            "reader": {
                "reader_mode": "inline_translation",
                "page": 3,
                "region_id": "region-context-pack",
                "layout_confidence": 0.93,
                "render_policy": "replace",
                "selected_text": {
                    "block_index": 5,
                    "side": "original",
                    "text": "metadata and section snippets",
                },
                "active_block": {
                    "index": 5,
                    "type": "paragraph",
                    "original": "The method builds metadata and section snippets.",
                    "translation": "该方法构建元数据和章节片段。",
                    "status": "done",
                },
                "previous_block": {
                    "index": 4,
                    "type": "heading",
                    "original": "2 Method",
                },
                "next_block": {
                    "index": 6,
                    "type": "heading",
                    "original": "5 Conclusion",
                },
            },
        }

        with (
            patch.object(routes_agent_chat.files, "load_document", return_value=doc),
            patch.object(routes_agent_chat, "load_chat", return_value=chat),
            patch.object(
                routes_agent_chat,
                "load_memories",
                return_value=[{"content": "优先看方法和复现证据。"}],
            ),
            patch.object(
                routes_agent_chat,
                "_pdf_front_page_text",
                return_value="Context Packs for Paper Reading\nAda Reader\nOpenAI Lab\n",
            ),
        ):
            pack = routes_agent_chat._build_context_pack(
                "1234.56789",
                "方法是什么？",
                context,
            )
            prompt = routes_agent_chat._build_chat_prompt("1234.56789", "方法是什么？", context)

        assert pack["paper"]["title"] == "Context Packs for Paper Reading"
        assert pack["paper"]["authors"] == ["Ada Reader", "Bo Builder"]
        assert "作者元数据: Ada Reader, Bo Builder" in pack["paper"]["metadata_text"]
        assert "OpenAI Lab" in pack["paper"]["metadata_text"]
        assert "metadata and section snippets" in pack["reader"]["selected"]
        assert "reader_mode: inline_translation" in pack["reader"]["location"]
        assert "PDF page: 3" in pack["reader"]["location"]
        assert "region_id: region-context-pack" in pack["reader"]["location"]
        assert "We propose context packs" in "\n".join(pack["global_context"]["key_blocks"]["Abstract"])
        assert "Readers need global context" in "\n".join(
            pack["global_context"]["key_blocks"]["Introduction"]
        )
        assert "The method builds metadata" in "\n".join(
            pack["global_context"]["key_blocks"]["Method"]
        )
        assert "The method builds metadata" in "\n".join(
            pack["global_context"]["related_blocks"]
        )
        assert "Context packs improve answer grounding" in "\n".join(
            pack["global_context"]["key_blocks"]["Conclusion"]
        )
        assert pack["history"][-1]["content"] == "之前的回答"
        assert all(item["content"] != "方法是什么？" for item in pack["history"])
        assert pack["memories"] == ["优先看方法和复现证据。"]
        assert pack["matched_skills"][0]["id"] == "method_explanation"

        body = prompt[-1]["content"]
        system = prompt[0]["content"]
        assert "论文事实" in system
        assert "通用背景" in system
        assert "最新信息/外部事实" in system
        assert "工具/联网/查资料请求" in system
        assert "不是论文内部证据" in system
        assert "不要臆测或自称具体 provider" in system
        assert "陪你读」配置的 LLM 接口" in system
        assert "匹配到的 Skill" in body
        assert "方法拆解" in body
        assert "论文全局上下文（关键 blocks）" in body
        assert "全文相关 blocks（轻量检索 top-k）" in body
        assert "Abstract" in body
        assert "Method" in body
        assert "用户当前消息: 方法是什么？" in body
        assert "layout_confidence: 0.930" in body
        assert "render_policy: replace" in body
        assert "不能声称已精准定位" in system

    def test_custom_skill_can_drive_intent_routing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skills_path = Path(tmp) / "skills.json"
            skills_path.write_text(
                json.dumps(
                    [
                        {
                            "id": "survey_map",
                            "name": "综述地图",
                            "description": "把论文放到相关工作版图里。",
                            "trigger": "用户要求画领域地图或梳理流派。",
                            "task_type": "four_agent_analysis",
                            "trigger_keywords": ["领域地图", "流派梳理"],
                            "steps": ["定位相关工作", "归纳技术流派"],
                            "source": "custom",
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch.object(agent_workspace, "SKILLS_PATH", skills_path):
                intent = agent_workspace.infer_agent_intent("帮我做一个领域地图")

        assert intent["task_type"] == "four_agent_analysis"
        assert intent["source"] == "skill"
        assert intent["skill_id"] == "survey_map"

    def test_code_repository_lookup_is_not_hijacked_by_skill_intent(self) -> None:
        intent = agent_workspace.infer_agent_intent("查一下这篇有没有代码仓库")

        assert intent["task_type"] is None

    def test_related_context_blocks_uses_expanded_query_and_limit(self) -> None:
        doc = PaperDocument(
            paper_id="1234.56789",
            title="Retrieval Test",
            source="ar5iv",
            extracted_at="",
            blocks=[
                Block(index=0, type="paragraph", original="This paragraph is unrelated."),
                Block(index=1, type="heading", original="2 Method", level=1),
                Block(
                    index=2,
                    type="paragraph",
                    original="The method builds a model architecture from local context.",
                ),
                Block(index=3, type="heading", original="3 Experiments", level=1),
                Block(index=4, type="paragraph", original="Experiments compare several baselines."),
            ],
        )

        related = routes_agent_chat._related_context_blocks(
            doc,
            "这个方法是什么？",
            context={},
            limit=2,
        )

        assert len(related) == 2
        assert any("2 Method" in line for line in related)
        assert any("model architecture" in line for line in related)
        assert all("unrelated" not in line for line in related)

    def test_permission_request_detects_external_lookup_phrasing(self) -> None:
        permission = routes_agent_chat._permission_request(
            "查一下这篇有没有代码仓库",
            context={},
        )

        assert permission is not None
        assert permission["scope"] == "external_search"
        assert permission["label"] == "外部检索"

        approved = routes_agent_chat._permission_request(
            "查一下这篇有没有代码仓库",
            context={"approved_permission": "external_search"},
        )
        assert approved is None

    def test_permission_request_detects_related_paper_search(self) -> None:
        permission = routes_agent_chat._permission_request(
            "帮我查一下和这个文章相似的文章有哪些可以做到吗",
            context={},
        )

        assert permission is not None
        assert permission["scope"] == "external_search"
        message = routes_agent_chat._permission_confirmation_message(permission)
        assert "相似论文" in message
        assert "待确认计划" not in message
        assert "MCP" not in message

    def test_permission_request_detects_author_citation_phrasing(self) -> None:
        permission = routes_agent_chat._permission_request(
            "哪个机构做的一作作者的引用数多少",
            context={},
        )

        assert permission is not None
        assert permission["scope"] == "external_search"

        scholar_permission = routes_agent_chat._permission_request(
            "Google Scholar 上这篇论文被引多少？",
            context={},
        )

        assert scholar_permission is not None
        assert scholar_permission["scope"] == "external_search"

    def test_permission_request_detects_general_web_search_phrasing(self) -> None:
        permission = routes_agent_chat._permission_request(
            "帮我网上搜一下这篇论文有没有最新的复现博客",
            context={},
        )

        assert permission is not None
        assert permission["scope"] == "external_search"

    def test_permission_request_detects_web_fetch_phrasing(self) -> None:
        permission = routes_agent_chat._permission_request(
            "总结一下这个网页 https://example.com/article",
            context={},
        )

        assert permission is not None
        assert permission["scope"] == "external_search"

    def test_explicit_mcp_action_is_not_misclassified_as_status(self) -> None:
        message = "请使用已经配置的 MCP paper_search 工具搜索 Attention Is All You Need"

        assert routes_agent_chat._is_mcp_status_question(message) is False
        assert routes_agent_chat._uses_unified_agent_loop(message, {}) is True

    def test_mcp_status_question_does_not_request_tool_permission(self) -> None:
        permission = routes_agent_chat._permission_request(
            "现在接入了什么mcp",
            context={},
        )

        assert permission is None
        assert routes_agent_chat._permission_request("现在有哪些搜索工具", context={}) is None

        with patch.object(
            routes_agent_chat,
            "get_config",
            return_value=SimpleNamespace(
                mcp_servers=[
                    SimpleNamespace(
                        name="local-paper-search",
                        enabled=True,
                        transport="stdio",
                        command="python",
                        args=["-m", "backend.tools.mcp_search_server"],
                        tool_name="paper_search",
                    ),
                    SimpleNamespace(
                        name="github-official",
                        enabled=True,
                        transport="stdio",
                        command="docker",
                        args=[
                            "run",
                            "-e",
                            "GITHUB_PERSONAL_ACCESS_TOKEN",
                            "ghcr.io/github/github-mcp-server",
                        ],
                        tool_name="search_repositories",
                    ),
                ],
            ),
        ):
            reply = routes_agent_chat._mcp_status_reply("现在接入了什么mcp")

        assert reply is not None
        assert "local-paper-search" in reply
        assert "github-official" in reply
        assert "不会真的调用 MCP" in reply

    def test_short_followup_does_not_hijack_active_block(self) -> None:
        active_context = {
            "reader": {
                "active_block": {
                    "index": 11,
                    "type": "paragraph",
                    "original": "The experiment result is important.",
                    "translation": "实验结果很重要。",
                },
                "selected_text": None,
            }
        }

        assert routes_agent_chat._contextual_task_type("啥意思", active_context, None) is None
        assert routes_agent_chat._contextual_task_type("不对", active_context, None) is None
        assert (
            routes_agent_chat._contextual_task_type("这段是什么意思", active_context, None)
            == "selection_explanation"
        )

        selected_context = {
            "reader": {
                "selected_text": {"text": "large kernel attention", "side": "original"},
                "active_block": active_context["reader"]["active_block"],
            }
        }
        assert (
            routes_agent_chat._contextual_task_type("啥意思", selected_context, None)
            == "selection_explanation"
        )

    def test_external_tool_request_passes_context_authors(self) -> None:
        captured: dict = {}

        class FakeRegistry:
            async def execute(self, name, arguments, permission_scope=None):
                captured["name"] = name
                captured["arguments"] = arguments
                captured["permission_scope"] = permission_scope
                return routes_agent_chat.ToolResult(
                    name=name,
                    content="真实外部检索结果：作者信息（Semantic Scholar）：Meng-Hao Guo",
                    evidence=(
                        {
                            "kind": "semantic_scholar_author_result",
                            "name": "Meng-Hao Guo",
                            "citation_count": 5678,
                        },
                    ),
                    metadata={"mock": False},
                )

        class FakeToolSummaryClient:
            async def acomplete(self, messages, **kwargs):
                assert "Meng-Hao Guo" in messages[-1]["content"]
                return "Semantic Scholar 显示 Meng-Hao Guo 有 5678 次引用。"

        async def run() -> str:
            with (
                patch.object(routes_agent_chat.files, "load_document", return_value=None),
                patch.object(routes_agent_chat, "build_agent_tool_registry", return_value=FakeRegistry()),
                patch.object(routes_agent_chat, "get_client", return_value=FakeToolSummaryClient()),
            ):
                return await routes_agent_chat._external_tool_request_result(
                    "2202.09741",
                    "哪个机构做的一作作者的引用数多少",
                    {
                        "approved_permission": "external_search",
                        "paper_title": "Visual Attention Network",
                        "paper_authors": ["Meng-Hao Guo", "Cheng-Ze Lu"],
                    },
                )

        reply = asyncio.run(run())

        assert "5678" in reply
        assert captured["name"] == "local.external_search"
        assert captured["permission_scope"] == "external_search"
        assert captured["arguments"]["paper_authors"] == ["Meng-Hao Guo", "Cheng-Ze Lu"]

    def test_related_paper_lookup_uses_broad_query_and_excludes_current(self) -> None:
        captured: dict = {}

        class FakeRegistry:
            async def execute(self, name, arguments, permission_scope=None):
                captured["name"] = name
                captured["arguments"] = arguments
                captured["permission_scope"] = permission_scope
                return routes_agent_chat.ToolResult(
                    name=name,
                    content="真实外部检索结果：论文信息：1. Data Deduplication for Efficient Training",
                    evidence=(
                        {
                            "kind": "external_paper_search_result",
                            "title": "Data Deduplication for Efficient Training",
                            "arxiv_id": "2401.00001",
                        },
                    ),
                    metadata={"mock": False, "query_mode": "related_papers"},
                )

        class FakeToolSummaryClient:
            async def acomplete(self, messages, **kwargs):
                body = messages[-1]["content"]
                assert "Data Deduplication for Efficient Training" in body
                assert "related_papers" in body
                return "我找到了一篇相近方向的论文：Data Deduplication for Efficient Training。"

        async def run() -> str:
            with (
                patch.object(routes_agent_chat.files, "load_document", return_value=None),
                patch.object(routes_agent_chat, "build_agent_tool_registry", return_value=FakeRegistry()),
                patch.object(routes_agent_chat, "get_client", return_value=FakeToolSummaryClient()),
            ):
                return await routes_agent_chat._external_tool_request_result(
                    "2303.09540",
                    "帮我查一下和这个文章相似的文章有哪些可以做到吗",
                    {
                        "approved_permission": "external_search",
                        "paper_title": "SemDeDup: Data-efficient learning at web-scale through semantic deduplication",
                    },
                )

        reply = asyncio.run(run())

        assert "相近方向" in reply
        assert captured["name"] == "local.external_search"
        assert captured["permission_scope"] == "external_search"
        assert captured["arguments"]["query_mode"] == "related_papers"
        assert captured["arguments"]["exclude_arxiv_id"] == "2303.09540"
        assert captured["arguments"]["exclude_title"].startswith("SemDeDup:")
        assert captured["arguments"]["search_query"] != captured["arguments"]["paper_title"]
        assert "semantic" in captured["arguments"]["search_query"]
        assert "deduplication" in captured["arguments"]["search_query"]

    def test_external_tool_request_runs_web_search_then_fetch(self) -> None:
        calls: list[tuple[str, dict]] = []

        class FakeRegistry:
            def get(self, name):
                return ToolSpec(
                    name=name,
                    description=name,
                    permission_scope="external_search",
                    source="local",
                )

            async def execute(self, name, arguments, permission_scope=None):
                calls.append((name, dict(arguments)))
                if name == "local.web_search":
                    return routes_agent_chat.ToolResult(
                        name=name,
                        content="通用网页搜索结果",
                        evidence=(
                            {
                                "kind": "web_search_result",
                                "rank": 1,
                                "title": "Official repo",
                                "url": "https://example.com/repo",
                            },
                            {
                                "kind": "web_search_result",
                                "rank": 2,
                                "title": "Blog",
                                "url": "https://example.com/blog",
                            },
                        ),
                        metadata={"mock": False},
                    )
                return routes_agent_chat.ToolResult(
                    name=name,
                    content=f"网页读取结果：{arguments['url']}",
                    evidence=(
                        {
                            "kind": "web_fetch_result",
                            "url": arguments["url"],
                            "text_excerpt": "Fetched page content.",
                        },
                    ),
                    metadata={"mock": False},
                )

        class FakeToolSummaryClient:
            async def acomplete(self, messages, **kwargs):
                body = messages[-1]["content"]
                assert "local.web_research" in body
                assert "web_search_result" in body
                assert "web_fetch_result" in body
                assert "tool_sequence" in body
                return "我先搜索再读取了前两个结果，并基于网页内容汇总。"

        async def run() -> str:
            tool_context = {
                "approved_permission": "external_search",
                "paper_title": "Visual Attention Network",
            }
            with (
                patch.object(routes_agent_chat.files, "load_document", return_value=None),
                patch.object(routes_agent_chat, "build_agent_tool_registry", return_value=FakeRegistry()),
                patch.object(routes_agent_chat, "get_client", return_value=FakeToolSummaryClient()),
            ):
                reply = await routes_agent_chat._external_tool_request_result(
                    "2202.09741",
                    "帮我网上搜一下这篇论文有没有最新的复现博客",
                    tool_context,
                )
            trace = tool_context["tool_trace"]
            assert trace["name"] == "local.web_research"
            assert trace["sequence"] == ["local.web_search", "local.web_fetch", "local.web_fetch"]
            assert trace["evidence_count"] == 4
            assert [step["label"] for step in trace["steps"][:3]] == ["网页搜索", "网页搜索", "读取网页"]
            return reply

        reply = asyncio.run(run())

        assert "先搜索再读取" in reply
        assert [name for name, _ in calls] == [
            "local.web_search",
            "local.web_fetch",
            "local.web_fetch",
        ]
        assert calls[1][1]["url"] == "https://example.com/repo"
        assert calls[2][1]["url"] == "https://example.com/blog"


def test_mcp_tool_route_selects_server_by_intent() -> None:
    async def fake_executor(call):
        return routes_agent_chat.ToolResult(name=call.name, content="ok")

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="mcp:github-official:mcp_tool",
            description="GitHub repositories and code search",
            permission_scope="mcp_tool",
            source="mcp",
            server_name="github-official",
        ),
        fake_executor,
    )
    registry.register(
        ToolSpec(
            name="mcp:local-paper-search:mcp_tool",
            description="Local paper search through arXiv and Semantic Scholar",
            permission_scope="mcp_tool",
            source="mcp",
            server_name="local-paper-search",
        ),
        fake_executor,
    )

    github_tool = routes_agent_chat._tool_name_for_scope(
        "mcp_tool",
        registry,
        "帮我找这篇论文的 GitHub 复现代码仓库",
        {"paper_title": "Visual Attention Network"},
    )
    paper_tool = routes_agent_chat._tool_name_for_scope(
        "mcp_tool",
        registry,
        "用 MCP 查这篇论文的相关工作和文献",
        {"paper_title": "Visual Attention Network"},
    )

    assert github_tool == "mcp:github-official:mcp_tool"
    assert paper_tool == "mcp:local-paper-search:mcp_tool"


def test_external_search_route_selects_web_tools_by_intent() -> None:
    async def fake_executor(call):
        return routes_agent_chat.ToolResult(name=call.name, content="ok")

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="local.external_search",
            description="Academic paper search",
            permission_scope="external_search",
            source="local",
        ),
        fake_executor,
    )
    registry.register(
        ToolSpec(
            name="local.web_search",
            description="General web search",
            permission_scope="external_search",
            source="local",
        ),
        fake_executor,
    )
    registry.register(
        ToolSpec(
            name="local.web_fetch",
            description="General web fetch",
            permission_scope="external_search",
            source="local",
        ),
        fake_executor,
    )

    fetch_tool = routes_agent_chat._tool_name_for_scope(
        "external_search",
        registry,
        "总结一下这个网页 https://example.com/article",
        {"paper_title": "Visual Attention Network"},
    )
    web_tool = routes_agent_chat._tool_name_for_scope(
        "external_search",
        registry,
        "帮我网上搜一下这篇论文有没有最新的复现博客",
        {"paper_title": "Visual Attention Network"},
    )
    paper_tool = routes_agent_chat._tool_name_for_scope(
        "external_search",
        registry,
        "查一下这篇论文的一作作者引用数和机构",
        {"paper_title": "Visual Attention Network"},
    )

    assert fetch_tool == "local.web_fetch"
    assert web_tool == "local.web_search"
    assert paper_tool == "local.external_search"


async def _exercise_chat_flow() -> None:
    await db_module.init_db()
    await db_module.insert_paper(
        arxiv_id="1706.03762",
        title="Attention Is All You Need",
        authors=["A. Vaswani"],
        source="ar5iv",
        file_path="/tmp/1706.03762",
    )

    state = await routes_agent_chat.get_agent_chat("1706.03762")
    assert state.messages[0].role == "assistant"
    assert state.skills

    background_tasks = BackgroundTasks()
    response = await routes_agent_chat.send_agent_message(
        "1706.03762",
        routes_agent_chat.AgentChatRequest(
            message="以后判断复现时优先看代码、超参数和硬件环境。",
        ),
        background_tasks,
    )

    assert response.saved_memory is None
    assert agent_workspace.load_memories() == []
    assert response.created_tasks
    assert response.created_tasks[0].task_type == "reproducibility_deep_dive"
    assert response.created_tasks[0].status == "running"
    assert response.created_runs
    assert response.created_runs[0].status == "running"
    assert response.messages[-1].role == "assistant"

    tasks = await db_module.list_agent_tasks()
    assert len(tasks) == 1
    assert tasks[0]["task_type"] == "reproducibility_deep_dive"
    assert tasks[0]["status"] == "running"

    await background_tasks()

    done_state = await routes_agent_chat.get_agent_chat("1706.03762")
    assert done_state.runs[-1].status == "done"
    assert "完成" in done_state.messages[-1].content
    assert done_state.messages[-1].meta.get("kind") == "agent_run_result"

    tasks = await db_module.list_agent_tasks()
    assert tasks[0]["task_type"] == "reproducibility_deep_dive"
    assert tasks[0]["status"] == "done"

    second_background = BackgroundTasks()
    second_response = await routes_agent_chat.send_agent_message(
        "1706.03762",
        routes_agent_chat.AgentChatRequest(message="帮我解释这篇论文的方法"),
        second_background,
    )
    run_id = second_response.created_runs[0].id
    cancelled = await routes_agent_chat.cancel_agent_run("1706.03762", run_id)
    assert cancelled.status == "cancelled"
    await second_background()

    final_state = await routes_agent_chat.get_agent_chat("1706.03762")
    assert final_state.runs[-1].status == "cancelled"

    class FakeClient:
        async def acomplete(self, messages, **kwargs):
            assert kwargs["task"] == "agent_chat"
            assert "Scaled dot-product attention" in messages[-1]["content"]
            return "缩放点积注意力是在点积注意力分数上除以尺度因子，避免数值过大。"

    context_background = BackgroundTasks()
    with patch.object(routes_agent_chat, "get_client", return_value=FakeClient()):
        context_response = await routes_agent_chat.send_agent_message(
            "1706.03762",
            routes_agent_chat.AgentChatRequest(
                message="这是什么意思？",
                context={
                    "source": "pet",
                    "paper_title": "Attention Is All You Need",
                    "reader": {
                        "selected_text": {
                            "block_index": 7,
                            "side": "original",
                            "text": "Scaled dot-product attention",
                        },
                        "active_block": {
                            "index": 7,
                            "type": "paragraph",
                            "original": "Scaled dot-product attention is used by the model.",
                            "translation": "模型使用缩放点积注意力。",
                            "status": "done",
                        },
                        "previous_block": None,
                        "next_block": None,
                        "right_pane_side": "translation",
                    },
                },
            ),
            context_background,
        )
        assert context_response.created_tasks
        assert context_response.created_tasks[0].task_type == "selection_explanation"
        assert context_response.created_runs[0].inputs[-1] == "阅读页段落/选区上下文"
        assert context_response.messages[-2].meta["client_context"]["source"] == "pet"
        await context_background()

    explained_state = await routes_agent_chat.get_agent_chat("1706.03762")
    assert explained_state.runs[-1].task_type == "selection_explanation"
    assert explained_state.runs[-1].status == "done"
    assert "缩放点积注意力" in explained_state.messages[-1].content

    class FakeReproClient:
        async def acomplete(self, messages, **kwargs):
            assert kwargs["task"] == "agent_reproducibility"
            assert "Code is available" in messages[-1]["content"]
            return """
{
  "summary": "代码线索存在，但缺少超参数和硬件环境。",
  "evidence": [
    {
      "claim": "当前选区提到代码可用。",
      "source": "block #9",
      "confidence": "medium"
    }
  ],
  "limits": ["没有看到训练超参数。", "没有看到硬件环境。"],
  "next_questions": ["论文是否给出 GitHub 链接？", "附录是否包含训练配置？"]
}
"""

    repro_background = BackgroundTasks()
    with patch.object(routes_agent_chat, "get_client", return_value=FakeReproClient()):
        repro_response = await routes_agent_chat.send_agent_message(
            "1706.03762",
            routes_agent_chat.AgentChatRequest(
                message="帮我深挖这里的复现证据",
                context={
                    "source": "pet",
                    "paper_title": "Attention Is All You Need",
                    "reader": {
                        "selected_text": {
                            "block_index": 9,
                            "side": "original",
                            "text": "Code is available for reproducing the experiments.",
                        },
                        "active_block": {
                            "index": 9,
                            "type": "paragraph",
                            "original": "Code is available for reproducing the experiments.",
                            "translation": "代码可用于复现实验。",
                            "status": "done",
                        },
                        "previous_block": None,
                        "next_block": None,
                        "right_pane_side": "translation",
                    },
                },
            ),
            repro_background,
        )
        assert repro_response.created_tasks[0].task_type == "reproducibility_deep_dive"
        await repro_background()

    repro_state = await routes_agent_chat.get_agent_chat("1706.03762")
    assert repro_state.runs[-1].task_type == "reproducibility_deep_dive"
    assert repro_state.runs[-1].status == "done"
    assert "代码线索存在" in repro_state.messages[-1].content
    assert "来源：block #9" in repro_state.messages[-1].content

    class FakeMethodClient:
        async def acomplete(self, messages, **kwargs):
            assert kwargs["task"] == "agent_summary"
            assert "multi-head attention" in messages[-1]["content"]
            return """
{
  "summary": "多头注意力把注意力计算拆成多个子空间并行观察不同关系。",
  "steps": ["把输入映射成 query/key/value。", "在多个 head 中并行计算注意力。", "拼接各 head 的输出。"],
  "terms": [
    {"term": "head", "meaning": "一组独立的注意力投影和计算通道。"}
  ],
  "assumptions": ["上下文只说明了模块作用，未覆盖完整训练目标。"],
  "next_questions": ["每个 head 的维度如何设置？"]
}
"""

    method_background = BackgroundTasks()
    with patch.object(routes_agent_chat, "get_client", return_value=FakeMethodClient()):
        method_response = await routes_agent_chat.send_agent_message(
            "1706.03762",
            routes_agent_chat.AgentChatRequest(
                message="解释这里的方法",
                context={
                    "source": "pet",
                    "paper_title": "Attention Is All You Need",
                    "reader": {
                        "selected_text": {
                            "block_index": 11,
                            "side": "original",
                            "text": "multi-head attention",
                        },
                        "active_block": {
                            "index": 11,
                            "type": "paragraph",
                            "original": "The model uses multi-head attention.",
                            "translation": "模型使用多头注意力。",
                            "status": "done",
                        },
                        "previous_block": None,
                        "next_block": None,
                        "right_pane_side": "translation",
                    },
                },
            ),
            method_background,
        )
        assert method_response.created_tasks[0].task_type == "method_explanation"
        await method_background()

    method_state = await routes_agent_chat.get_agent_chat("1706.03762")
    assert method_state.runs[-1].task_type == "method_explanation"
    assert method_state.runs[-1].status == "done"
    assert "多头注意力" in method_state.messages[-1].content
    assert "主链路" in method_state.messages[-1].content

    # 带选区且用户明确说"这段"时，选区解释优先于方法拆解
    selection_priority_background = BackgroundTasks()
    with patch.object(routes_agent_chat, "get_client", return_value=FakeClient()):
        selection_priority_response = await routes_agent_chat.send_agent_message(
            "1706.03762",
            routes_agent_chat.AgentChatRequest(
                message="解释这段",
                context={
                    "source": "pet",
                    "paper_title": "Attention Is All You Need",
                    "reader": {
                        "selected_text": {
                            "block_index": 7,
                            "side": "original",
                            "text": "Scaled dot-product attention",
                        },
                        "active_block": {
                            "index": 7,
                            "type": "paragraph",
                            "original": "Scaled dot-product attention is used by the model.",
                            "translation": "模型使用缩放点积注意力。",
                            "status": "done",
                        },
                        "previous_block": None,
                        "next_block": None,
                        "right_pane_side": "translation",
                    },
                },
            ),
            selection_priority_background,
        )
        assert selection_priority_response.created_tasks[0].task_type == "selection_explanation"
        await selection_priority_background()

    permission_background = BackgroundTasks()
    permission_reader_context = {
        "source": "pet",
        "paper_title": "Attention Is All You Need",
        "reader": {
            "selected_text": None,
            "active_block": {
                "index": 3,
                "type": "paragraph",
                "original": "Attention mechanisms are widely used.",
                "translation": "注意力机制被广泛使用。",
                "status": "done",
            },
            "previous_block": None,
            "next_block": None,
            "right_pane_side": "translation",
        },
    }
    class FakeToolLoopClient:
        def __init__(self) -> None:
            self.calls = 0

        async def acomplete_with_tools(self, messages, tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "mcp-call-1",
                            "name": "mcp_tool",
                            "arguments": {"query": "查相关工作", "reason": "需要调用 MCP"},
                        }
                    ],
                }
            assert messages[-1]["role"] == "tool"
            assert "本地 mock MCP 工具结果" in messages[-1]["content"]
            return {
                "content": "已用 mock MCP 工具结果作为 evidence/context 汇总；这不是真实外部查询。",
                "tool_calls": [],
            }

        async def acomplete(self, messages, **kwargs):
            return "工具循环达到上限。"

    loop_client = FakeToolLoopClient()
    with (
        patch.object(routes_agent_chat, "build_agent_tool_registry", return_value=build_mock_tool_registry()),
        patch.object(routes_agent_chat, "get_client", return_value=loop_client),
    ):
        permission_response = await routes_agent_chat.send_agent_message(
            "1706.03762",
            routes_agent_chat.AgentChatRequest(
                message="用 MCP 工具帮我查这篇论文的相关工作",
                context=permission_reader_context,
            ),
            permission_background,
        )
        assert not permission_response.created_tasks
        permission_request = permission_response.assistant_message.meta["permission_request"]
        assert permission_request["scope"] == "mcp_tool"
        assert permission_request["run_id"] == permission_response.created_runs[0].id
        assert permission_response.created_runs[0].status == "waiting_permission"

        resumed = await routes_agent_chat.resume_agent_run_stream(
            "1706.03762",
            permission_request["run_id"],
            routes_agent_chat.AgentRunResumeRequest(approved_permission="mcp_tool"),
        )
        async for _ in resumed.body_iterator:
            pass

    approved_state = await routes_agent_chat.get_agent_chat("1706.03762")
    assert approved_state.runs[-1].id == permission_request["run_id"]
    assert approved_state.runs[-1].task_type == "agent_loop"
    assert approved_state.runs[-1].status == "done"
    assert "mock MCP 工具结果作为 evidence/context" in approved_state.messages[-1].content
    matching_users = [
        message
        for message in approved_state.messages
        if message.role == "user" and message.content == "用 MCP 工具帮我查这篇论文的相关工作"
    ]
    assert len(matching_users) == 1

    # 普通对话（无任务意图）走 LLM：prompt 携带最近对话窗口与用户记忆
    class FakeChatClient:
        async def acomplete_with_tools(self, messages, tools, **kwargs):
            assert kwargs["task"] == "agent_chat"
            body = messages[-1]["content"]
            assert "最近对话" in body
            assert "作者元数据: A. Vaswani" in body
            assert "用 MCP 工具帮我查这篇论文的相关工作" in body
            assert "以后判断复现时优先看代码、超参数和硬件环境。" in body
            assert "用户当前消息: 这篇论文的核心贡献是什么？" in body
            return {"content": "核心贡献是提出完全基于注意力的 Transformer 结构。", "tool_calls": []}

        async def acomplete(self, messages, **kwargs):
            return "核心贡献是提出完全基于注意力的 Transformer 结构。"

    chat_background = BackgroundTasks()
    with patch.object(routes_agent_chat, "get_client", return_value=FakeChatClient()):
        chat_response = await routes_agent_chat.send_agent_message(
            "1706.03762",
            routes_agent_chat.AgentChatRequest(
                message="这篇论文的核心贡献是什么？",
                context={
                    "source": "pet",
                    "paper_title": "Attention Is All You Need",
                    "paper_authors": ["A. Vaswani"],
                },
            ),
            chat_background,
        )
    assert not chat_response.created_tasks
    assert chat_response.assistant_message.content == "核心贡献是提出完全基于注意力的 Transformer 结构。"

    # LLM 不可用时普通对话回退规则回复，不报错
    class BrokenClient:
        async def acomplete_with_tools(self, messages, tools, **kwargs):
            raise RuntimeError("provider down")

        async def acomplete(self, messages, **kwargs):
            raise RuntimeError("provider down")

    fallback_background = BackgroundTasks()
    with patch.object(routes_agent_chat, "get_client", return_value=BrokenClient()):
        fallback_response = await routes_agent_chat.send_agent_message(
            "1706.03762",
            routes_agent_chat.AgentChatRequest(
                message="给我一点整体阅读建议吧",
                context={"source": "pet", "paper_title": "Attention Is All You Need"},
            ),
            fallback_background,
        )
    assert not fallback_response.created_tasks
    assert "我在当前论文《Attention Is All You Need》里" in fallback_response.assistant_message.content

    # 元数据类问题：即使 LLM 不可用，也应该用作者/首页机构线索兜底回答
    with (
        patch.object(routes_agent_chat, "get_client", return_value=BrokenClient()),
        patch.object(
            routes_agent_chat,
            "_pdf_front_page_text",
            return_value="Attention Is All You Need\nA. Vaswani\nGoogle Research\n",
        ),
    ):
        metadata_response = await routes_agent_chat.send_agent_message(
            "1706.03762",
            routes_agent_chat.AgentChatRequest(
                message="这篇论文是哪家机构写的？",
                context={
                    "source": "pet",
                    "paper_title": "Attention Is All You Need",
                    "paper_authors": ["A. Vaswani"],
                },
            ),
            BackgroundTasks(),
        )
    assert not metadata_response.created_tasks
    assert "作者：A. Vaswani" in metadata_response.assistant_message.content
    assert "Google Research" in metadata_response.assistant_message.content

    # 机构背景问题不能被“只依据论文证据”护栏误伤：LLM 保守拒答时用已识别机构背景兜底
    class ConservativeClient:
        async def acomplete_with_tools(self, messages, tools, **kwargs):
            assert "可以使用模型通用知识回答" in messages[0]["content"]
            return {"content": "论文没有对该校的任何评价或介绍，所以我无法判断它怎么样。", "tool_calls": []}

        async def acomplete(self, messages, **kwargs):
            assert "可以使用模型通用知识回答" in messages[0]["content"]
            return "论文没有对该校的任何评价或介绍，所以我无法判断它怎么样。"

    with (
        patch.object(routes_agent_chat, "get_client", return_value=ConservativeClient()),
        patch.object(
            routes_agent_chat,
            "_pdf_front_page_text",
            return_value="Focus Agent\nTaiyu Zhang\nKU Leuven\nLeuven, Belgium\n",
        ),
    ):
        institution_response = await routes_agent_chat.send_agent_message(
            "1706.03762",
            routes_agent_chat.AgentChatRequest(
                message="这个大学怎么样？",
                context={
                    "source": "pet",
                    "paper_title": "Attention Is All You Need",
                    "paper_authors": ["A. Vaswani"],
                },
            ),
            BackgroundTasks(),
        )
    assert not institution_response.created_tasks
    assert "KU Leuven" in institution_response.assistant_message.content
    assert "通用背景" in institution_response.assistant_message.content
    assert "不是论文内部证据" in institution_response.assistant_message.content

    # 清空当前论文对话：回到欢迎态，runs 历史保留
    cleared_state = await routes_agent_chat.clear_agent_chat("1706.03762")
    assert len(cleared_state.messages) == 1
    assert cleared_state.messages[0].meta.get("kind") == "welcome"
    assert cleared_state.runs


class _IntentStubClient:
    """按 task 分流的 LLM stub：agent_intent 返回分类 JSON，其余返回对话回复。"""

    def __init__(
        self,
        intent_payload: dict | None = None,
        chat_reply: str = "好的，我来解释。",
        raise_on_intent: bool = False,
        intent_raw: str | None = None,
        intent_delay: float = 0.0,
    ) -> None:
        self.intent_payload = intent_payload or {}
        self.chat_reply = chat_reply
        self.raise_on_intent = raise_on_intent
        self.intent_raw = intent_raw
        self.intent_delay = intent_delay
        self.tasks: list[str | None] = []

    async def acomplete(self, messages, **kwargs):
        task = kwargs.get("task")
        self.tasks.append(task)
        if task == "agent_intent":
            if self.intent_delay:
                await asyncio.sleep(self.intent_delay)
            if self.raise_on_intent:
                raise RuntimeError("intent provider down")
            if self.intent_raw is not None:
                return self.intent_raw
            return json.dumps(self.intent_payload, ensure_ascii=False)
        return self.chat_reply


class LLMIntentRoutingTest(unittest.TestCase):
    """LLM 意图分类：类别映射、失败回退、send_agent_message 集成与误触发修复。"""

    def test_classifier_maps_external_search_and_permission(self) -> None:
        async def scenario() -> None:
            client = _IntentStubClient(
                {"category": "external_search", "save_memory": False, "confidence": "high", "reason": "需要联网查复现情况"}
            )
            with (
                patch.dict(os.environ, {"PEINIDU_LLM_INTENT": "1"}),
                patch.object(routes_agent_chat, "get_client", return_value=client),
                patch.object(routes_agent_chat, "_chat_history_items", return_value=[]),
            ):
                intent = await routes_agent_chat._classify_message_llm(
                    "1706.03762", "外面有没有人复现成功过这篇？", {}
                )
            assert intent is not None
            assert intent["source"] == "llm_intent"
            assert intent["task_type"] is None
            assert intent["permission_scope"] == "external_search"
            permission = routes_agent_chat._permission_from_llm_intent(
                intent, "外面有没有人复现成功过这篇？", {}
            )
            assert permission is not None
            assert permission["scope"] == "external_search"
            assert permission["original_message"] == "外面有没有人复现成功过这篇？"
            # 已确认权限的上下文不重复要卡
            assert (
                routes_agent_chat._permission_from_llm_intent(
                    intent, "再查一次", {"approved_permission": "external_search"}
                )
                is None
            )

        asyncio.run(scenario())

    def test_classifier_downgrades_selection_without_reader_context(self) -> None:
        async def scenario() -> None:
            payload = {"category": "selection_explanation", "save_memory": False, "confidence": "high", "reason": ""}
            with (
                patch.dict(os.environ, {"PEINIDU_LLM_INTENT": "1"}),
                patch.object(routes_agent_chat, "get_client", return_value=_IntentStubClient(payload)),
                patch.object(routes_agent_chat, "_chat_history_items", return_value=[]),
            ):
                without_context = await routes_agent_chat._classify_message_llm(
                    "1706.03762", "解释一下这段", {}
                )
                with_context = await routes_agent_chat._classify_message_llm(
                    "1706.03762",
                    "解释一下这段",
                    {"reader": {"selected_text": {"block_index": 3, "side": "original", "text": "attention"}}},
                )
            assert without_context is not None
            assert without_context["category"] == "chat"
            assert without_context["task_type"] is None
            assert with_context is not None
            assert with_context["task_type"] == "selection_explanation"

        asyncio.run(scenario())

    def test_classifier_falls_back_on_disabled_error_bad_json_or_timeout(self) -> None:
        async def scenario() -> None:
            disabled_client = _IntentStubClient({"category": "chat"})
            with (
                patch.dict(os.environ, {"PEINIDU_LLM_INTENT": "0"}),
                patch.object(routes_agent_chat, "get_client", return_value=disabled_client),
            ):
                assert await routes_agent_chat._classify_message_llm("1", "你好", {}) is None
            assert disabled_client.tasks == []  # 关闭时不应发起调用

            cases = [
                _IntentStubClient(raise_on_intent=True),
                _IntentStubClient(intent_raw="这不是 JSON"),
                _IntentStubClient({"category": "unknown_category"}),
            ]
            with (
                patch.dict(os.environ, {"PEINIDU_LLM_INTENT": "1"}),
                patch.object(routes_agent_chat, "_chat_history_items", return_value=[]),
            ):
                for client in cases:
                    with patch.object(routes_agent_chat, "get_client", return_value=client):
                        assert await routes_agent_chat._classify_message_llm("1", "你好", {}) is None
                slow_client = _IntentStubClient({"category": "chat"}, intent_delay=0.05)
                with (
                    patch.object(routes_agent_chat, "get_client", return_value=slow_client),
                    patch.object(routes_agent_chat, "INTENT_LLM_TIMEOUT_SECONDS", 0.01),
                ):
                    assert await routes_agent_chat._classify_message_llm("1", "你好", {}) is None

        asyncio.run(scenario())

    def test_normal_message_uses_one_agent_loop_call_without_preface_llms(self) -> None:
        class LoopClient:
            def __init__(self) -> None:
                self.tasks: list[str | None] = []

            async def acomplete_with_tools(self, messages, tools, **kwargs):
                self.tasks.append(kwargs.get("task"))
                return {"content": "它用大核卷积分解实现注意力。", "tool_calls": []}

            async def acomplete(self, messages, **kwargs):
                raise AssertionError("普通消息不应先走独立文本规划调用")

        async def scenario() -> None:
            await db_module.init_db()
            await db_module.insert_paper(
                arxiv_id="2202.09741",
                title="Visual Attention Network",
                authors=["Meng-Hao Guo"],
                source="ar5iv",
                file_path="/tmp/2202.09741",
            )
            client = LoopClient()
            with (
                patch.object(routes_agent_chat, "get_client", return_value=client),
                patch.object(routes_agent_chat, "_classify_message_llm", side_effect=AssertionError("unused")),
                patch.object(routes_agent_chat, "_plan_agent_action_llm", side_effect=AssertionError("unused")),
            ):
                response = await routes_agent_chat.send_agent_message(
                    "2202.09741",
                    routes_agent_chat.AgentChatRequest(message="这套注意力是怎么实现的？"),
                    BackgroundTasks(),
                )
            assert response.assistant_message.content == "它用大核卷积分解实现注意力。"
            assert response.assistant_message.meta["intent"]["source"] == "iterative_agent_loop"
            assert client.tasks == ["agent_chat"]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "agent_workspace"
            with (
                patch.object(db_module, "DB_PATH", root / "papers.db"),
                patch.object(agent_workspace, "AGENT_WORKSPACE_DIR", workspace),
                patch.object(agent_workspace, "MEMORY_PATH", workspace / "memory.json"),
                patch.object(agent_workspace, "SKILLS_PATH", workspace / "skills.json"),
                patch.object(routes_agent_chat.files, "load_document", return_value=None),
                patch.object(routes_agent_chat.files, "load_analysis", return_value=None),
            ):
                asyncio.run(scenario())

    def test_tool_plan_prefers_native_provider_tool_calls(self) -> None:
        class NativeToolClient:
            def __init__(self) -> None:
                self.used_tools = False

            async def acomplete_with_tools(self, messages, tools, **kwargs):
                self.used_tools = True
                assert tools[0]["function"]["name"] == "local_external_search"
                properties = tools[0]["function"]["parameters"]["properties"]
                assert properties["lookup_targets"]["items"]["enum"] == ["papers", "authors", "citation_metrics"]
                assert properties["author_scope"]["enum"] == ["none", "first_author", "paper_authors"]
                return {
                    "content": "",
                    "tool_calls": [
                        {
                            "name": "local_web_search",
                            "arguments": {
                                "search_query": "Visual Attention Network reproduction blog",
                                "reason": "需要网页搜索复现博客。",
                            },
                        }
                    ],
                }

            async def acomplete(self, messages, **kwargs):
                raise AssertionError("native tool call path should not fallback to text JSON")

        async def run() -> None:
            client = NativeToolClient()
            with (
                patch.dict(os.environ, {"PEINIDU_LLM_INTENT": "1"}),
                patch.object(routes_agent_chat, "get_client", return_value=client),
                patch.object(routes_agent_chat.files, "load_document", return_value=None),
            ):
                plan = await routes_agent_chat._plan_agent_action_llm(
                    "2202.09741",
                    "帮我网上搜一下这篇论文有没有复现博客",
                    {"paper_title": "Visual Attention Network"},
                )

            assert client.used_tools
            assert plan is not None
            assert plan["source"] == "native_tool_call"
            assert plan["permission_scope"] == "external_search"
            assert plan["tool_name"] == "local.web_search"
            assert plan["tool_calls"] == [
                {
                    "tool_name": "local.web_search",
                    "arguments": {
                        "query": "Visual Attention Network reproduction blog",
                        "search_query": "Visual Attention Network reproduction blog",
                    },
                    "reason": "需要网页搜索复现博客。",
                }
            ]

        asyncio.run(run())

    def test_tool_plan_maps_native_multi_step_calls_in_order(self) -> None:
        class NativeMultiToolClient:
            async def acomplete_with_tools(self, messages, tools, **kwargs):
                return {
                    "content": "",
                    "tool_calls": [
                        {
                            "name": "local_web_search",
                            "arguments": {
                                "search_query": "Visual Attention Network GitHub reproduction",
                                "reason": "先找复现仓库线索。",
                            },
                        },
                        {
                            "name": "local_web_fetch",
                            "arguments": {
                                "url": "https://example.com/van-repo",
                                "reason": "再读取用户指定或模型已知的 URL。",
                            },
                        },
                    ],
                }

            async def acomplete(self, messages, **kwargs):
                raise AssertionError("native multi tool path should not fallback")

        async def run() -> None:
            with (
                patch.dict(os.environ, {"PEINIDU_LLM_INTENT": "1"}),
                patch.object(routes_agent_chat, "get_client", return_value=NativeMultiToolClient()),
                patch.object(routes_agent_chat.files, "load_document", return_value=None),
            ):
                plan = await routes_agent_chat._plan_agent_action_llm(
                    "2202.09741",
                    "搜一下这篇论文的复现仓库，再读一下这个链接 https://example.com/van-repo",
                    {"paper_title": "Visual Attention Network"},
                )

            assert plan is not None
            assert plan["source"] == "native_tool_call"
            assert plan["permission_scope"] == "external_search"
            assert plan["tool_name"] == "local.web_search"
            assert [step["tool_name"] for step in plan["tool_calls"]] == [
                "local.web_search",
                "local.web_fetch",
            ]

        asyncio.run(run())

    def test_text_tool_plan_infers_scope_and_rejects_mixed_scope_steps(self) -> None:
        plan = routes_agent_chat._normalize_tool_plan(
            {
                "action": "tool_request",
                "permission_scope": None,
                "tool_calls": [
                    {
                        "tool_name": "local.web_search",
                        "arguments": {"query": "paper repo", "search_query": "paper repo"},
                        "reason": "先搜索",
                    },
                    {
                        "tool_name": "local.web_fetch",
                        "arguments": {"url": "https://example.com/repo"},
                        "reason": "再读取",
                    },
                ],
                "user_facing_reason": "需要搜索和读取网页。",
                "confidence": "high",
            },
            "搜索并读取复现网页",
            {},
        )
        assert plan is not None
        assert plan["permission_scope"] == "external_search"
        assert plan["tool_name"] == "local.web_search"
        assert plan["search_query"] == "paper repo"
        assert [step["tool_name"] for step in plan["tool_calls"]] == [
            "local.web_search",
            "local.web_fetch",
        ]

        mixed = routes_agent_chat._normalize_tool_plan(
            {
                "action": "tool_request",
                "permission_scope": None,
                "tool_calls": [
                    {"tool_name": "local.web_search", "arguments": {"query": "paper repo"}},
                    {"tool_name": "mcp_tool", "arguments": {"query": "paper repo"}},
                ],
            },
            "混用 MCP 和网页搜索",
            {},
        )
        assert mixed is None

    def test_tool_plan_falls_back_when_native_returns_no_tool_calls(self) -> None:
        class EmptyNativeClient:
            def __init__(self) -> None:
                self.used_tools = False
                self.used_text = False

            async def acomplete_with_tools(self, messages, tools, **kwargs):
                self.used_tools = True
                return {"content": "不调用工具", "tool_calls": []}

            async def acomplete(self, messages, **kwargs):
                self.used_text = True
                return json.dumps(
                    {
                        "action": "tool_request",
                        "permission_scope": "external_search",
                        "tool_name": "local.external_search",
                        "query_mode": "paper_lookup",
                        "search_query": "Visual Attention Network citations",
                        "user_facing_reason": "需要联网查引用。",
                        "confidence": "high",
                    },
                    ensure_ascii=False,
                )

        async def run() -> None:
            client = EmptyNativeClient()
            with (
                patch.dict(os.environ, {"PEINIDU_LLM_INTENT": "1"}),
                patch.object(routes_agent_chat, "get_client", return_value=client),
                patch.object(routes_agent_chat.files, "load_document", return_value=None),
            ):
                plan = await routes_agent_chat._plan_agent_action_llm(
                    "2202.09741",
                    "查一下这篇论文引用情况",
                    {"paper_title": "Visual Attention Network"},
                )

            assert client.used_tools
            assert client.used_text
            assert plan is not None
            assert plan["source"] == "llm_tool_plan"
            assert plan["tool_name"] == "local.external_search"

        asyncio.run(run())

    def test_send_message_persists_native_related_paper_call_without_separate_plan(self) -> None:
        class RelatedPaperLoopClient:
            async def acomplete_with_tools(self, messages, tools, **kwargs):
                return {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "related-1",
                            "name": "local_external_search",
                            "arguments": {
                                "search_query": "semantic deduplication web scale data efficient learning",
                                "query_mode": "related_papers",
                                "reason": "需要联网查相关论文，并排除当前论文。",
                            },
                        }
                    ],
                }

            async def acomplete(self, messages, **kwargs):
                raise AssertionError("不应调用独立文本 planner")

        async def scenario() -> None:
            await db_module.init_db()
            await db_module.insert_paper(
                arxiv_id="2303.09540",
                title="SemDeDup: Data-efficient learning at web-scale through semantic deduplication",
                authors=["Amro Abbas"],
                source="ar5iv",
                file_path="/tmp/2303.09540",
            )

            with (
                patch.object(routes_agent_chat, "get_client", return_value=RelatedPaperLoopClient()),
                patch.object(routes_agent_chat, "_classify_message_llm", side_effect=AssertionError("unused")),
                patch.object(routes_agent_chat, "_plan_agent_action_llm", side_effect=AssertionError("unused")),
            ):
                response = await routes_agent_chat.send_agent_message(
                    "2303.09540",
                    routes_agent_chat.AgentChatRequest(
                        message="帮我查一下和这个文章相似的文章有哪些可以做到吗",
                        context={
                            "paper_title": "SemDeDup: Data-efficient learning at web-scale through semantic deduplication",
                        },
                    ),
                    BackgroundTasks(),
                )

            permission = response.assistant_message.meta["permission_request"]
            assert permission["scope"] == "external_search"
            assert "联网查相关论文" in response.assistant_message.content
            assert not response.created_tasks
            assert response.created_runs[0].task_type == "agent_loop"
            assert response.created_runs[0].status == "waiting_permission"
            run = agent_workspace.get_run("2303.09540", response.created_runs[0].id)
            state = run["context"]["agent_loop_state"]
            pending = state["pending_tool_calls"][0]
            assert pending["call_id"] == "related-1"
            assert pending["tool_name"] == "local.external_search"
            assert pending["arguments"]["query_mode"] == "related_papers"
            assert pending["arguments"]["search_query"] == "semantic deduplication web scale data efficient learning"
            assert pending["arguments"]["exclude_arxiv_id"] == "2303.09540"

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


class RunDispatchFixRegressionTest(unittest.TestCase):
    """全链路核查修复轮回归：批准重发确定性、Run 终态保护、孤儿清扫、记忆混合消息。"""

    def _workspace(self, tmp: str):
        root = Path(tmp)
        workspace = root / "agent_workspace"
        return (
            patch.dict(os.environ, {"PEINIDU_LLM_INTENT": "0"}),
            patch.object(db_module, "DB_PATH", root / "papers.db"),
            patch.object(agent_workspace, "AGENT_WORKSPACE_DIR", workspace),
            patch.object(agent_workspace, "MEMORY_PATH", workspace / "memory.json"),
            patch.object(agent_workspace, "SKILLS_PATH", workspace / "skills.json"),
            patch.object(routes_agent_chat.files, "load_document", return_value=None),
            patch.object(routes_agent_chat.files, "load_analysis", return_value=None),
        )

    def test_approved_resend_with_skill_keyword_executes_tool_request(self) -> None:
        """批准的外部检索重发不能被"复现/方法"等关键词劫持成本地 Run（HIGH）。"""

        async def scenario() -> None:
            await db_module.init_db()
            await db_module.insert_paper(
                arxiv_id="1706.03762",
                title="Attention Is All You Need",
                authors=["A. Vaswani"],
                source="ar5iv",
                file_path="/tmp/1706.03762",
            )
            hijack_messages = [
                "帮我查一下这篇论文的复现代码仓库",  # '复现' 命中 repro skill 关键词
                "上网搜一下这个方法的相关工作",  # '方法' 命中 method 关键词
                "联网查一下这个问题的最新进展",  # '问题' 命中标注整理关键词
            ]
            for message in hijack_messages:
                response = await routes_agent_chat.send_agent_message(
                    "1706.03762",
                    routes_agent_chat.AgentChatRequest(
                        message=message,
                        context={"approved_permission": "external_search"},
                    ),
                    BackgroundTasks(),  # 不 await：只断言派发层，不真执行外部工具
                )
                assert response.created_tasks, message
                assert response.created_tasks[0].task_type == "external_tool_request", (
                    f"{message} 被劫持成 {response.created_tasks[0].task_type}"
                )
                assert response.created_runs[0].inputs[-1] == "已确认权限：外部检索"
                assert "permission_request" not in response.assistant_message.meta

        with tempfile.TemporaryDirectory() as tmp:
            patches = self._workspace(tmp)
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
                asyncio.run(scenario())

    def test_approved_resend_skips_memory_and_second_permission_card(self) -> None:
        """批准重发是同一消息第二次入站：不重复存记忆，scope 不一致也不弹第二张卡。"""

        async def scenario() -> None:
            await db_module.init_db()
            await db_module.insert_paper(
                arxiv_id="1706.03762",
                title="Attention Is All You Need",
                authors=["A. Vaswani"],
                source="ar5iv",
                file_path="/tmp/1706.03762",
            )
            # '以后' 是关键词记忆触发词；'mcp' 的关键词 scope 是 mcp_tool，
            # 与已批准的 external_search 不一致——旧代码会弹第二张卡
            response = await routes_agent_chat.send_agent_message(
                "1706.03762",
                routes_agent_chat.AgentChatRequest(
                    message="以后用 MCP 帮我查这篇的相关工作",
                    context={"approved_permission": "external_search"},
                ),
                BackgroundTasks(),
            )
            assert response.saved_memory is None
            assert "permission_request" not in response.assistant_message.meta
            assert response.created_tasks[0].task_type == "external_tool_request"
            assert agent_workspace.load_memories() == []

        with tempfile.TemporaryDirectory() as tmp:
            patches = self._workspace(tmp)
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
                asyncio.run(scenario())

    def test_permission_resume_reuses_run_and_rejects_scope_mismatch(self) -> None:
        async def scenario() -> None:
            await db_module.init_db()
            await db_module.insert_paper(
                arxiv_id="1706.03762",
                title="Attention Is All You Need",
                authors=["A. Vaswani"],
                source="ar5iv",
                file_path="/tmp/1706.03762",
            )
            registry = ToolRegistry()

            async def executor(call):
                return routes_agent_chat.ToolResult(name=call.name, content="找到仓库 https://example.com/repo")

            registry.register(
                ToolSpec("local.web_search", "web search", permission_scope="external_search"),
                executor,
            )

            class LoopClient:
                def __init__(self) -> None:
                    self.calls = 0

                async def acomplete_with_tools(self, messages, tools, **kwargs):
                    self.calls += 1
                    if self.calls == 1:
                        return {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "repo-call",
                                    "name": "local_web_search",
                                    "arguments": {"search_query": "paper github", "reason": "需要查仓库"},
                                }
                            ],
                        }
                    assert messages[-1]["role"] == "tool"
                    return {"content": "代码仓库在 example.com/repo。", "tool_calls": []}

                async def acomplete(self, messages, **kwargs):
                    return "达到上限"

            with (
                patch.object(routes_agent_chat, "build_agent_tool_registry", return_value=registry),
                patch.object(routes_agent_chat, "get_client", return_value=LoopClient()),
            ):
                first = await routes_agent_chat.send_agent_message(
                    "1706.03762",
                    routes_agent_chat.AgentChatRequest(message="查一下这篇有没有代码仓库"),
                    BackgroundTasks(),
                )
                assert first.assistant_message.meta["kind"] == "permission_request"
                run_id = first.created_runs[0].id
                with self.assertRaises(Exception) as caught:
                    await routes_agent_chat.resume_agent_run_stream(
                        "1706.03762",
                        run_id,
                        routes_agent_chat.AgentRunResumeRequest(approved_permission="mcp_tool"),
                    )
                assert getattr(caught.exception, "status_code", None) == 409

                resumed = await routes_agent_chat.resume_agent_run_stream(
                    "1706.03762",
                    run_id,
                    routes_agent_chat.AgentRunResumeRequest(approved_permission="external_search"),
                )
                chunks: list[str] = []
                async for chunk in resumed.body_iterator:
                    chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)

            raw = "".join(chunks)
            assert "event: agent_event" in raw
            assert '"status": "resumed"' in raw
            assert '"status": "finalizing"' in raw
            assert "event: tool_event" in raw

            chat = agent_workspace.load_chat("1706.03762")
            user_messages = [
                item for item in chat["messages"]
                if item["role"] == "user" and item["content"] == "查一下这篇有没有代码仓库"
            ]
            assert len(user_messages) == 1
            runs = agent_workspace.load_runs("1706.03762")
            assert len(runs) == 1
            assert runs[0]["id"] == run_id
            assert runs[0]["status"] == "done"

        with tempfile.TemporaryDirectory() as tmp:
            patches = self._workspace(tmp)
            with (
                patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6],
            ):
                asyncio.run(scenario())

    def test_clear_chat_cancels_running_runs_and_suppresses_late_result(self) -> None:
        async def slow_executor(arxiv_id: str, user_message: str, context: dict) -> str:
            return "迟到的结果"

        async def scenario() -> None:
            await db_module.init_db()
            await db_module.insert_paper(
                arxiv_id="1706.03762",
                title="Attention Is All You Need",
                authors=["A. Vaswani"],
                source="ar5iv",
                file_path="/tmp/1706.03762",
            )
            background = BackgroundTasks()
            response = await routes_agent_chat.send_agent_message(
                "1706.03762",
                routes_agent_chat.AgentChatRequest(message="帮我解释这篇论文的方法"),
                background,
            )
            run_id = response.created_runs[0].id
            task_id = response.created_tasks[0].id

            cleared = await routes_agent_chat.clear_agent_chat("1706.03762")
            assert len(cleared.messages) == 1
            assert cleared.messages[0].meta.get("kind") == "welcome"
            cleared_run = agent_workspace.get_run("1706.03762", run_id)
            assert cleared_run is not None and cleared_run["status"] == "cancelled"
            tasks = await db_module.list_agent_tasks()
            task_row = next(item for item in tasks if item["id"] == task_id)
            assert task_row["status"] == "cancelled"

            await background()
            final_state = await routes_agent_chat.get_agent_chat("1706.03762")
            assert [message.meta.get("kind") for message in final_state.messages] == ["welcome"]
            final_run = agent_workspace.get_run("1706.03762", run_id)
            assert final_run is not None and final_run["status"] == "cancelled"

        with tempfile.TemporaryDirectory() as tmp:
            patches = self._workspace(tmp)
            with (
                patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6],
                patch.dict(routes_agent_chat.RUN_EXECUTORS, {"method_explanation": slow_executor}),
            ):
                asyncio.run(scenario())

    def test_cancelled_run_not_overwritten_by_executor_error(self) -> None:
        """取消后执行器抛异常：Run 必须保持 cancelled，不被覆盖成 error。"""

        async def failing_executor(arxiv_id: str, user_message: str, context: dict) -> str:
            raise RuntimeError("network down after cancel")

        async def scenario() -> None:
            await db_module.init_db()
            await db_module.insert_paper(
                arxiv_id="1706.03762",
                title="Attention Is All You Need",
                authors=["A. Vaswani"],
                source="ar5iv",
                file_path="/tmp/1706.03762",
            )
            background = BackgroundTasks()
            response = await routes_agent_chat.send_agent_message(
                "1706.03762",
                routes_agent_chat.AgentChatRequest(message="帮我解释这篇论文的方法"),
                background,
            )
            run_id = response.created_runs[0].id
            task_id = response.created_tasks[0].id
            cancelled = await routes_agent_chat.cancel_agent_run("1706.03762", run_id)
            assert cancelled.status == "cancelled"
            await background()  # 执行器此时抛异常
            run = agent_workspace.get_run("1706.03762", run_id)
            assert run is not None and run["status"] == "cancelled", run["status"]
            assert run["result"] == "用户取消了这个后台任务。"
            tasks = await db_module.list_agent_tasks()
            task_row = next(item for item in tasks if item["id"] == task_id)
            assert task_row["status"] == "cancelled", task_row["status"]

        with tempfile.TemporaryDirectory() as tmp:
            patches = self._workspace(tmp)
            with (
                patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6],
                patch.dict(routes_agent_chat.RUN_EXECUTORS, {"method_explanation": failing_executor}),
            ):
                asyncio.run(scenario())

    def test_cancel_endpoint_cancels_registered_run_task(self) -> None:
        async def scenario() -> None:
            await db_module.init_db()
            await db_module.insert_paper(
                arxiv_id="1706.03762",
                title="Attention Is All You Need",
                authors=["A. Vaswani"],
                source="ar5iv",
                file_path="/tmp/1706.03762",
            )
            started = asyncio.Event()
            cancelled = asyncio.Event()

            async def cancellable_executor(arxiv_id: str, user_message: str, context: dict) -> str:
                started.set()
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
                return "不应该完成"

            task_id = await db_module.create_agent_task(
                arxiv_id="1706.03762", task_type="method_explanation", summary="方法拆解"
            )
            run = agent_workspace.create_run(
                "1706.03762",
                task_type="method_explanation",
                title="方法拆解",
                user_message="解释方法",
                task_id=task_id,
            )
            with patch.dict(routes_agent_chat.RUN_EXECUTORS, {"method_explanation": cancellable_executor}):
                worker = asyncio.create_task(
                    routes_agent_chat._finish_agent_run("1706.03762", run["id"], task_id)
                )
                await asyncio.wait_for(started.wait(), timeout=1)
                assert routes_agent_chat._RUN_TASKS.get(run["id"]) is worker
                cancelled_run = await routes_agent_chat.cancel_agent_run("1706.03762", run["id"])
                assert cancelled_run.status == "cancelled"
                await asyncio.wait_for(worker, timeout=1)
                assert cancelled.is_set()
            stored = agent_workspace.get_run("1706.03762", run["id"])
            assert stored is not None and stored["status"] == "cancelled"
            assert routes_agent_chat._RUN_TASKS.get(run["id"]) is None

        with tempfile.TemporaryDirectory() as tmp:
            patches = self._workspace(tmp)
            with (
                patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6],
            ):
                asyncio.run(scenario())

    def test_run_execution_respects_agent_concurrency_limit(self) -> None:
        async def scenario() -> None:
            await db_module.init_db()
            await db_module.insert_paper(
                arxiv_id="1706.03762",
                title="Attention Is All You Need",
                authors=["A. Vaswani"],
                source="ar5iv",
                file_path="/tmp/1706.03762",
            )
            entered: list[str] = []
            release_first = asyncio.Event()

            async def gated_executor(arxiv_id: str, user_message: str, context: dict) -> str:
                entered.append(user_message)
                if user_message == "run-1":
                    await release_first.wait()
                return f"{user_message} done"

            task_id_1 = await db_module.create_agent_task(
                arxiv_id="1706.03762", task_type="method_explanation", summary="run-1"
            )
            task_id_2 = await db_module.create_agent_task(
                arxiv_id="1706.03762", task_type="method_explanation", summary="run-2"
            )
            run_1 = agent_workspace.create_run(
                "1706.03762",
                task_type="method_explanation",
                title="Run 1",
                user_message="run-1",
                task_id=task_id_1,
            )
            run_2 = agent_workspace.create_run(
                "1706.03762",
                task_type="method_explanation",
                title="Run 2",
                user_message="run-2",
                task_id=task_id_2,
            )
            with patch.dict(routes_agent_chat.RUN_EXECUTORS, {"method_explanation": gated_executor}):
                worker_1 = asyncio.create_task(
                    routes_agent_chat._finish_agent_run("1706.03762", run_1["id"], task_id_1)
                )
                worker_2 = asyncio.create_task(
                    routes_agent_chat._finish_agent_run("1706.03762", run_2["id"], task_id_2)
                )
                await asyncio.sleep(0.05)
                assert entered == ["run-1"]
                release_first.set()
                await asyncio.wait_for(asyncio.gather(worker_1, worker_2), timeout=1)
            assert entered == ["run-1", "run-2"]
            assert agent_workspace.get_run("1706.03762", run_1["id"])["status"] == "done"
            assert agent_workspace.get_run("1706.03762", run_2["id"])["status"] == "done"

        with tempfile.TemporaryDirectory() as tmp:
            patches = self._workspace(tmp)
            config = SimpleNamespace(agent_concurrency=1)
            with (
                patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6],
                patch.object(routes_agent_chat, "get_config", return_value=config),
            ):
                asyncio.run(scenario())

    def test_update_run_refuses_terminal_to_terminal_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "agent_workspace"
            with patch.object(agent_workspace, "AGENT_WORKSPACE_DIR", workspace):
                run = agent_workspace.create_run(
                    "2000.00001", task_type="method_explanation", title="方法拆解", user_message="解释方法"
                )
                agent_workspace.update_run("2000.00001", run["id"], status="cancelled", result="用户取消")
                blocked = agent_workspace.update_run("2000.00001", run["id"], status="error", error="late failure")
                assert blocked is not None and blocked["status"] == "cancelled"
                stored = agent_workspace.get_run("2000.00001", run["id"])
                assert stored["status"] == "cancelled"
                assert stored.get("error", "") == ""  # 拒绝迁移时也不写入 error 字段

    def test_waiting_permission_run_persists_context_and_can_be_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "agent_workspace"
            with patch.object(agent_workspace, "AGENT_WORKSPACE_DIR", workspace):
                run = agent_workspace.create_run(
                    "2000.00001",
                    task_type="agent_loop",
                    title="Pet 对话",
                    user_message="查找论文仓库",
                    status="waiting_permission",
                    context={"agent_loop_state": {"pending_tool_calls": [{"call_id": "call-1"}]}},
                )
                assert run["status"] == "waiting_permission"

                updated = agent_workspace.update_run(
                    "2000.00001",
                    run["id"],
                    context={"agent_loop_state": {"granted_scopes": ["external_search"]}},
                )
                assert updated is not None
                assert updated["context"]["agent_loop_state"]["granted_scopes"] == ["external_search"]

                cancelled = agent_workspace.cancel_run("2000.00001", run["id"])
                assert cancelled is not None and cancelled["status"] == "cancelled"

    def test_startup_sweep_preserves_waiting_permission_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "agent_workspace"
            with patch.object(agent_workspace, "AGENT_WORKSPACE_DIR", workspace):
                waiting = agent_workspace.create_run(
                    "2000.00001",
                    task_type="agent_loop",
                    title="Pet 对话",
                    user_message="查找",
                    status="waiting_permission",
                )
                running = agent_workspace.create_run(
                    "2000.00001",
                    task_type="method_explanation",
                    title="方法拆解",
                    user_message="解释",
                )

                assert agent_workspace.sweep_stale_runs() == 1
                assert agent_workspace.get_run("2000.00001", waiting["id"])["status"] == "waiting_permission"
                assert agent_workspace.get_run("2000.00001", running["id"])["status"] == "error"

    def test_startup_sweep_marks_stale_running_runs_and_tasks(self) -> None:
        async def scenario(tmp: str) -> None:
            await db_module.init_db()
            await db_module.insert_paper(
                arxiv_id="1706.03762",
                title="Attention Is All You Need",
                authors=["A. Vaswani"],
                source="ar5iv",
                file_path="/tmp/1706.03762",
            )
            task_id = await db_module.create_agent_task(
                arxiv_id="1706.03762", task_type="method_explanation", summary="遗留任务"
            )
            run = agent_workspace.create_run(
                "1706.03762", task_type="method_explanation", title="方法拆解", user_message="解释方法"
            )
            assert run["status"] == "running"

            swept_tasks = await db_module.sweep_stale_agent_tasks()
            swept_runs = agent_workspace.sweep_stale_runs()
            assert swept_tasks == 1
            assert swept_runs == 1
            stale_run = agent_workspace.get_run("1706.03762", run["id"])
            assert stale_run["status"] == "error"
            assert "重启" in stale_run["error"]
            assert stale_run["completed_at"]
            tasks = await db_module.list_agent_tasks()
            task_row = next(item for item in tasks if item["id"] == task_id)
            assert task_row["status"] == "error"

        with tempfile.TemporaryDirectory() as tmp:
            patches = self._workspace(tmp)
            with patches[1], patches[2], patches[3], patches[4]:
                asyncio.run(scenario(tmp))

    def test_memory_plus_question_waits_for_confirmation_then_answers(self) -> None:
        """偏好+提问先确认记忆写入，恢复后仍回答原问题。"""

        class MemoryQuestionClient:
            def __init__(self) -> None:
                self.calls = 0

            async def acomplete_with_tools(self, messages, tools, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "memory-question",
                                "name": "local_memory_save",
                                "arguments": {
                                    "content": "以后术语保留英文",
                                    "kind": "preference",
                                    "reason": "用户要求长期遵守",
                                },
                            }
                        ],
                    }
                return {"content": "一作是 Ashish Vaswani。", "tool_calls": []}

            async def acomplete(self, messages, **kwargs):
                return "一作是 Ashish Vaswani。"

        async def scenario() -> None:
            await db_module.init_db()
            await db_module.insert_paper(
                arxiv_id="1706.03762",
                title="Attention Is All You Need",
                authors=["A. Vaswani"],
                source="ar5iv",
                file_path="/tmp/1706.03762",
            )
            client = MemoryQuestionClient()
            with (
                patch.dict(os.environ, {"PEINIDU_LLM_INTENT": "1"}),
                patch.object(routes_agent_chat, "get_client", return_value=client),
            ):
                response = await routes_agent_chat.send_agent_message(
                    "1706.03762",
                    routes_agent_chat.AgentChatRequest(message="以后术语保留英文，另外这篇的一作是谁？"),
                    BackgroundTasks(),
                )
                assert response.saved_memory is None
                assert response.assistant_message.meta["permission_request"]["scope"] == "memory_write"
                assert agent_workspace.load_memories() == []
                resumed = await routes_agent_chat.resume_agent_run_stream(
                    "1706.03762",
                    response.created_runs[0].id,
                    routes_agent_chat.AgentRunResumeRequest(approved_permission="memory_write"),
                )
                async for _ in resumed.body_iterator:
                    pass
            state = await routes_agent_chat.get_agent_chat("1706.03762")
            assert "一作是 Ashish Vaswani。" in state.messages[-1].content
            assert len(agent_workspace.load_memories()) == 1

        with tempfile.TemporaryDirectory() as tmp:
            patches = self._workspace(tmp)
            with patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
                asyncio.run(scenario())


class MCPConfigWizardTest(unittest.TestCase):
    """Pet MCP 配置向导：意图识别、草稿目录、确认写入（强制不启用）。"""

    def test_wizard_keyword_detection_distinguishes_status_and_tool(self) -> None:
        assert routes_agent_chat._is_mcp_config_request("帮我接入一个 GitHub MCP")
        assert routes_agent_chat._is_mcp_config_request("帮我配置一个论文搜索的 mcp")
        assert not routes_agent_chat._is_mcp_config_request("现在接入了什么mcp")  # 状态自省
        assert not routes_agent_chat._is_mcp_config_request("用 MCP 查这篇论文的相关工作")  # 工具调用
        assert not routes_agent_chat._is_mcp_config_request("帮我配置一下翻译模型")  # 无 MCP 主语

    def test_wizard_draft_catalog_and_name_dedup(self) -> None:
        fake_config = SimpleNamespace(mcp_servers=[SimpleNamespace(name="local-paper-search")])
        with patch.object(routes_agent_chat, "get_config", return_value=fake_config):
            playwright = routes_agent_chat._build_mcp_config_draft(
                "帮我配置官方 Playwright MCP 浏览器"
            )
            assert playwright["server"]["name"] == "playwright-official"
            assert playwright["server"]["transport"] == "http"
            assert playwright["server"]["command"] == ""
            assert playwright["server"]["args"] == []
            assert playwright["server"]["url"] == "http://browser:8931/mcp"
            assert playwright["server"]["permission_scopes"] == ["browser_control"]
            assert playwright["server"]["allowed_tools"] == [
                "browser_navigate",
                "browser_snapshot",
                "browser_click",
                "browser_type",
                "browser_press_key",
                "browser_wait_for",
            ]
            assert playwright["server"]["enabled"] is False

            gitmcp = routes_agent_chat._build_mcp_config_draft(
                "帮我接入 https://github.com/idosal/git-mcp 这个公开仓库"
            )
            assert gitmcp["server"]["name"] == "gitmcp-idosal-git-mcp"
            assert gitmcp["server"]["transport"] == "http"
            assert gitmcp["server"]["url"] == "https://gitmcp.io/idosal/git-mcp"
            assert gitmcp["server"]["permission_scopes"] == ["mcp_tool"]
            assert gitmcp["server"]["enabled"] is False
            assert "无需 Token" in gitmcp["note"]

            short_gitmcp = routes_agent_chat._build_mcp_config_draft(
                "帮我用 GitMCP 接入 idosal/git-mcp"
            )
            assert short_gitmcp["server"]["url"] == "https://gitmcp.io/idosal/git-mcp"

            github = routes_agent_chat._build_mcp_config_draft("帮我接入一个 GitHub 仓库 MCP")
            assert github["server"]["name"] == "github-official"
            assert github["server"]["command"] == "docker"
            assert github["server"]["tool_name"] == "search_repositories"
            assert github["server"]["enabled"] is False
            assert "GITHUB_PERSONAL_ACCESS_TOKEN" in github["note"]

            paper = routes_agent_chat._build_mcp_config_draft("帮我配置一个论文搜索 mcp")
            assert paper["server"]["name"] == "local-paper-search-2"  # 与现有配置去重
            assert paper["server"]["command"] == "python"

            custom = routes_agent_chat._build_mcp_config_draft(
                "帮我接入这个 mcp https://example.com/rpc 试试"
            )
            assert custom["server"]["transport"] == "http"
            assert custom["server"]["url"] == "https://example.com/rpc"
            assert custom["server"]["enabled"] is False

    def test_send_message_wizard_flow_keyword_and_llm(self) -> None:
        async def scenario() -> None:
            await db_module.init_db()
            await db_module.insert_paper(
                arxiv_id="1706.03762",
                title="Attention Is All You Need",
                authors=["A. Vaswani"],
                source="ar5iv",
                file_path="/tmp/1706.03762",
            )
            fake_config = SimpleNamespace(mcp_servers=[])
            # ① 关键词兜底路径（LLM 关闭）：不弹权限卡、不建任务、meta 带草稿
            with (
                patch.dict(os.environ, {"PEINIDU_LLM_INTENT": "0"}),
                patch.object(routes_agent_chat, "get_config", return_value=fake_config),
            ):
                response = await routes_agent_chat.send_agent_message(
                    "1706.03762",
                    routes_agent_chat.AgentChatRequest(message="帮我接入一个 GitHub MCP"),
                    BackgroundTasks(),
                )
            assert "permission_request" not in response.assistant_message.meta
            assert not response.created_tasks
            draft = response.assistant_message.meta.get("mcp_config_draft")
            assert isinstance(draft, dict) and draft["name"] == "github-official"
            assert "配置草稿" in response.assistant_message.content
            assert response.assistant_message.meta["intent"]["category"] == "mcp_config_wizard"

            # ② LLM 分类路径：category=mcp_config_wizard，不再走 chat LLM
            client = _IntentStubClient(
                {"category": "mcp_config_wizard", "save_memory": False, "confidence": "high", "reason": "配置请求"}
            )
            with (
                patch.dict(os.environ, {"PEINIDU_LLM_INTENT": "1"}),
                patch.object(routes_agent_chat, "get_client", return_value=client),
                patch.object(routes_agent_chat, "get_config", return_value=fake_config),
            ):
                llm_response = await routes_agent_chat.send_agent_message(
                    "1706.03762",
                    routes_agent_chat.AgentChatRequest(message="给我接个能查论文的工具服务"),
                    BackgroundTasks(),
                )
            assert client.tasks == ["agent_intent"]
            llm_draft = llm_response.assistant_message.meta.get("mcp_config_draft")
            assert isinstance(llm_draft, dict) and llm_draft["name"] == "local-paper-search"
            assert not llm_response.created_tasks

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "agent_workspace"
            with (
                patch.object(db_module, "DB_PATH", root / "papers.db"),
                patch.object(agent_workspace, "AGENT_WORKSPACE_DIR", workspace),
                patch.object(agent_workspace, "MEMORY_PATH", workspace / "memory.json"),
                patch.object(agent_workspace, "SKILLS_PATH", workspace / "skills.json"),
                patch.object(routes_agent_chat.files, "load_document", return_value=None),
                patch.object(routes_agent_chat.files, "load_analysis", return_value=None),
            ):
                asyncio.run(scenario())

    def test_confirm_writes_disabled_server_with_dedup_and_validation(self) -> None:
        from backend.llm.models import AppConfig, MCPServerConfig
        from fastapi import HTTPException

        async def scenario() -> None:
            await db_module.init_db()
            await db_module.insert_paper(
                arxiv_id="1706.03762",
                title="Attention Is All You Need",
                authors=["A. Vaswani"],
                source="ar5iv",
                file_path="/tmp/1706.03762",
            )
            saved: list[AppConfig] = []
            base_config = AppConfig(
                mcp_servers=[
                    MCPServerConfig(name="github-official", transport="stdio", command="docker")
                ]
            )
            with (
                patch.object(routes_agent_chat, "get_config", return_value=base_config),
                patch.object(routes_agent_chat, "save_config", side_effect=saved.append),
                patch.object(routes_agent_chat, "reset_config", lambda: None),
            ):
                # 客户端篡改 enabled=True 也必须以 enabled=False 落盘；重名自动加后缀
                response = await routes_agent_chat.confirm_mcp_config(
                    "1706.03762",
                    routes_agent_chat.MCPConfigConfirmRequest(
                        server={
                            "name": "github-official",
                            "transport": "stdio",
                            "command": "docker",
                            "args": ["run", "-i", "--rm", "ghcr.io/github/github-mcp-server"],
                            "enabled": True,
                            "tool_name": "search_repositories",
                        }
                    ),
                )
                assert len(saved) == 1
                written = saved[0].mcp_servers[-1]
                assert written.name == "github-official-2"
                assert written.enabled is False
                assert "github-official-2" in response.assistant_message.content
                assert response.assistant_message.meta.get("kind") == "mcp_config_written"
                assert response.messages[-1].content == response.assistant_message.content

                # http 缺 url 拒绝
                try:
                    await routes_agent_chat.confirm_mcp_config(
                        "1706.03762",
                        routes_agent_chat.MCPConfigConfirmRequest(
                            server={"name": "bad-http", "transport": "http", "url": None}
                        ),
                    )
                    raise AssertionError("http 缺 url 应该 422")
                except HTTPException as e:
                    assert e.status_code == 422

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "agent_workspace"
            with (
                patch.object(db_module, "DB_PATH", root / "papers.db"),
                patch.object(agent_workspace, "AGENT_WORKSPACE_DIR", workspace),
                patch.object(agent_workspace, "MEMORY_PATH", workspace / "memory.json"),
                patch.object(agent_workspace, "SKILLS_PATH", workspace / "skills.json"),
            ):
                asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
