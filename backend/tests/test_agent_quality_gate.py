from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.agent.quality_gate import (
    evaluate_quality_response,
    load_quality_cases,
    quality_summary,
    validate_evidence_item,
)
from backend.agent.tool_loop import AgentLoopResult, AgentLoopState
from backend.api.routes_agent_chat import (
    AGENT_LOOP_SYSTEM_SUFFIX,
    CHAT_SYSTEM_PROMPT,
    _agent_evidence_source,
    _agent_loop_result_data,
)


FIXTURE = Path(__file__).parent / "fixtures" / "agent_quality_cases.json"


class AgentQualityGateTest(unittest.TestCase):
    def test_fixture_has_required_coverage_and_at_least_24_cases(self) -> None:
        cases = load_quality_cases(FIXTURE)

        assert len(cases) >= 24
        categories = {case.category for case in cases}
        assert {
            "current_selection",
            "paper_fact",
            "professional_task",
            "user_notes",
            "external_research",
            "clarification",
            "permission",
            "evidence_refusal",
        }.issubset(categories)

    def test_perfect_recorded_responses_pass_quality_threshold(self) -> None:
        cases = load_quality_cases(FIXTURE)
        results = []
        for case in cases:
            if case.expected_action == "tool":
                response = {
                    "content": "",
                    "tool_calls": [
                        {
                            "name": case.expected_tools[0],
                            "arguments": {},
                            "id": f"call-{case.id}",
                        }
                    ],
                }
            elif case.expected_action == "clarify":
                response = {"content": "请明确对象、范围或期望产物？", "tool_calls": []}
            else:
                response = {
                    "content": "不能编造。" if case.required_text else "基于现有证据直接回答。",
                    "tool_calls": [],
                }
            results.append(evaluate_quality_response(case, response))

        summary = quality_summary(results)
        assert summary["tool_selection_accuracy"] == 1.0
        assert summary["forbidden_tool_violations"] == 0
        assert summary["all_required_text_present"] is True

    def test_invalid_fixture_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invalid-agent-quality.json"
            path.write_text(json.dumps([{"id": "one", "expected_action": "direct"}]), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "at least 24"):
                load_quality_cases(path)

    def test_prompt_contract_for_sources_and_bounded_recovery(self) -> None:
        assert "论文原文" in CHAT_SYSTEM_PROMPT
        assert "外部网页" in CHAT_SYSTEM_PROMPT
        assert "你的笔记" in CHAT_SYSTEM_PROMPT
        assert "不得无故调用外部搜索" in CHAT_SYSTEM_PROMPT
        assert "才追问一次" in CHAT_SYSTEM_PROMPT
        assert "最多选择一个" in AGENT_LOOP_SYSTEM_SUFFIX

    def test_source_labels_and_locator_validation(self) -> None:
        note = _agent_evidence_source(
            {
                "kind": "agent_note_search_result",
                "arxiv_id": "2202.09741",
                "annotation_id": "note-1",
            },
            {"source": "local_notes_search"},
        )
        web = _agent_evidence_source(
            {"kind": "web_fetch_result", "url": "https://example.com/paper"},
            {"source": "agent_tool_loop"},
        )
        paper = _agent_evidence_source(
            {"claim": "claim", "block_index": 11, "location": {"block_index": 11}},
            {},
        )

        assert note["source_label"] == "你的笔记"
        assert web["source_label"] == "外部网页"
        assert paper["source_label"] == "论文原文"
        assert validate_evidence_item(note)
        assert validate_evidence_item(web)
        assert validate_evidence_item(paper)
        assert not validate_evidence_item({"source_type": "paper", "location": {"page": 1}})
        assert not validate_evidence_item({"source_type": "external_web", "url": "not-a-url"})
        assert not validate_evidence_item({"source_type": "user_note", "arxiv_id": "2202.09741"})

    def test_agent_result_keeps_web_and_note_evidence(self) -> None:
        state = AgentLoopState(
            messages=[
                {
                    "role": "tool",
                    "tool_call_id": "web-1",
                    "name": "local_web_fetch",
                    "content": json.dumps(
                        {
                            "content": "page",
                            "evidence": [
                                {
                                    "kind": "web_fetch_result",
                                    "url": "https://example.com/source",
                                    "title": "Source",
                                }
                            ],
                            "metadata": {"source": "agent_tool_loop"},
                        }
                    ),
                },
                {
                    "role": "tool",
                    "tool_call_id": "note-1",
                    "name": "local_notes_search",
                    "content": json.dumps(
                        {
                            "content": "note",
                            "evidence": [
                                {
                                    "kind": "agent_note_search_result",
                                    "arxiv_id": "2202.09741",
                                    "annotation_id": "annotation-1",
                                    "source": "你的笔记 · 方法",
                                }
                            ],
                            "metadata": {"source": "local_notes_search"},
                        },
                        ensure_ascii=False,
                    ),
                },
            ]
        )
        result = AgentLoopResult(status="completed", final_text="回答", state=state)

        data = _agent_loop_result_data(result, "回答")

        assert [item["source_label"] for item in data["evidence"]] == ["外部网页", "你的笔记"]
        assert all(validate_evidence_item(item) for item in data["evidence"])


if __name__ == "__main__":
    unittest.main()
