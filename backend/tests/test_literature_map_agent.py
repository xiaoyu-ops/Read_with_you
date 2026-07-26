from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from backend.agent.tool_loop import AgentLoopResult, AgentLoopState
from backend.api import routes_agent_chat
from backend.tools import build_agent_tool_registry
from backend.tools.literature_map import literature_map_tool
from backend.tools.registry import ToolCall


ORIGIN_ID = "a" * 40
RELATED_IDS = [value * 40 for value in ("b", "c", "d", "e", "f", "1")]


def paper(paper_id: str, title: str, *, arxiv_id: str | None = None) -> dict:
    return {
        "id": paper_id,
        "arxiv_id": arxiv_id,
        "doi": None,
        "title": title,
        "authors": [f"{title} Author"],
        "abstract": f"{title} abstract",
        "year": 2024,
        "venue": "TestConf",
        "citation_count": 12,
        "reference_count": 5,
        "is_open_access": bool(arxiv_id),
        "pdf_url": None,
        "url": f"https://www.semanticscholar.org/paper/{paper_id}",
        "similarity": 0.9,
        "role": "origin" if paper_id == ORIGIN_ID else "related",
    }


class LiteratureMapAgentTest(unittest.TestCase):
    def test_registry_exposes_map_under_external_search_scope(self) -> None:
        registry = build_agent_tool_registry()
        spec = registry.get("local.literature_map")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.permission_scope, "external_search")

    def test_tool_returns_compact_summary_and_controlled_action(self) -> None:
        nodes = [
            paper(ORIGIN_ID, "Origin", arxiv_id="2307.16789"),
            *[
                paper(paper_id, f"Related {index}")
                for index, paper_id in enumerate(RELATED_IDS, start=1)
            ],
        ]
        payload = {
            "version": 1,
            "origin": nodes[0],
            "nodes": nodes,
            "edges": [
                {
                    "source": ORIGIN_ID,
                    "target": RELATED_IDS[0],
                    "kind": "similarity",
                    "weight": 0.9,
                    "provenance": "semantic_scholar_specter2",
                },
                {
                    "source": RELATED_IDS[1],
                    "target": ORIGIN_ID,
                    "kind": "citation",
                    "weight": 1,
                    "provenance": "semantic_scholar_academic_graph",
                },
            ],
            "prior_works": [
                {"paper": nodes[1], "graph_citation_count": 3},
            ],
            "derivative_works": [
                {"paper": nodes[2], "graph_reference_count": 2},
            ],
            "status": "complete",
            "provider": "semantic_scholar",
            "retrieved_at": "2026-07-26T00:00:00Z",
            "cached": False,
            "stale": False,
            "warnings": [],
        }
        call = ToolCall(
            "local.literature_map",
            {"arxiv_id": "2307.16789"},
            permission_scope="external_search",
        )
        with patch(
            "backend.tools.literature_map.get_literature_map",
            AsyncMock(return_value=payload),
        ):
            result = asyncio.run(literature_map_tool(call))

        self.assertEqual(len(result.evidence), 5)
        self.assertNotIn("nodes", result.metadata)
        self.assertNotIn("edges", result.metadata)
        data = result.metadata["result_data"]
        self.assertEqual(
            data["actions"],
            [
                {
                    "kind": "open_literature_map",
                    "label": "打开论文图谱",
                    "href": "/literature-map/ARXIV%3A2307.16789",
                }
            ],
        )
        self.assertIn("7 个节点", data["summary"])
        self.assertIn("前 5 篇", data["limits"][0])

    def test_map_intent_wins_but_plain_related_list_stays_external_search(self) -> None:
        map_plan = routes_agent_chat._normalize_tool_plan(
            {"action": "chat"},
            "用 Connected Papers 看这篇文章的对应关系",
            {"paper_title": "Origin"},
        )
        self.assertEqual(map_plan["permission_scope"], "external_search")
        self.assertEqual(map_plan["tool_name"], "local.literature_map")
        self.assertEqual(map_plan["tool_calls"][0]["tool_name"], "local.literature_map")

        related_plan = routes_agent_chat._normalize_tool_plan(
            {"action": "chat"},
            "普通推荐五篇相关论文即可，不需要画图",
            {"paper_title": "Origin"},
        )
        self.assertEqual(related_plan["tool_name"], "local.external_search")
        self.assertEqual(related_plan["query_mode"], "related_papers")

    def test_map_permission_uses_existing_external_search_scope(self) -> None:
        permission = routes_agent_chat._permission_request(
            "调用工具看一下这篇论文的关系图谱",
            {},
        )
        self.assertEqual(permission["scope"], "external_search")
        self.assertIsNone(
            routes_agent_chat._permission_request(
                "调用工具看一下这篇论文的关系图谱",
                {"approved_permission": "external_search"},
            )
        )

    def test_result_action_is_allowlisted_and_survives_loop_normalization(self) -> None:
        normalized = routes_agent_chat._normalize_result_data(
            {
                "summary": "done",
                "actions": [
                    {
                        "kind": "open_literature_map",
                        "label": "打开论文图谱",
                        "href": "/literature-map/ARXIV%3A2307.16789",
                    },
                    {
                        "kind": "open_literature_map",
                        "label": "unsafe",
                        "href": "https://example.com/map",
                    },
                    {
                        "kind": "open_url",
                        "label": "unsafe",
                        "href": "/literature-map/abc",
                    },
                ],
            }
        )
        self.assertEqual(len(normalized["actions"]), 1)

        state = AgentLoopState(
            messages=[
                {
                    "role": "tool",
                    "tool_call_id": "map-1",
                    "name": "local_literature_map",
                    "content": json.dumps(
                        {
                            "content": "compact",
                            "evidence": [],
                            "metadata": {"result_data": normalized},
                        }
                    ),
                }
            ]
        )
        result = AgentLoopResult(
            status="completed",
            state=state,
            final_text="图谱已准备好。",
        )
        data = routes_agent_chat._agent_loop_result_data(result, result.final_text)
        self.assertEqual(data["actions"], normalized["actions"])


if __name__ == "__main__":
    unittest.main()
