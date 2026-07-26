from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend.agent.definitions.reproducibility import REPRODUCIBILITY_PROMPT
from backend.agent.orchestrator import (
    REPRODUCIBILITY_ASPECTS,
    _reconstruct_evidence_text,
    _run_reproducibility,
    analyze_paper,
)
from backend.agent.schemas import Evidence, EvidenceLocation
from backend.extraction.blocks import Block


class _FakeAgent:
    name = "reproducibility"

    def __init__(self, output: str) -> None:
        self.output = output
        self.inputs: list[str] = []

    async def run(self, full_text: str) -> str:
        self.inputs.append(full_text)
        return self.output


class AgentReproducibilityTest(unittest.TestCase):
    def test_old_evidence_shape_remains_serialization_compatible(self) -> None:
        evidence = Evidence(
            aspect="代码",
            status="已开源",
            detail="GitHub repo provided.",
            citation="Appendix A",
        )

        self.assertEqual(
            evidence.model_dump(),
            {
                "aspect": "代码",
                "status": "已开源",
                "detail": "GitHub repo provided.",
                "citation": "Appendix A",
            },
        )

    def test_location_serializes_only_available_coordinates(self) -> None:
        block_only = Evidence(
            aspect="代码",
            status="已开源",
            detail="GitHub repo provided.",
            citation="Appendix A",
            location=EvidenceLocation(block_index=7),
        )
        enriched = Evidence(
            aspect="代码",
            status="已开源",
            detail="GitHub repo provided.",
            citation="Appendix A",
            location=EvidenceLocation(block_index=7, page=3, region_id="region-code-7"),
        )

        self.assertEqual(block_only.model_dump()["location"], {"block_index": 7})
        self.assertEqual(
            enriched.model_dump()["location"],
            {"block_index": 7, "page": 3, "region_id": "region-code-7"},
        )

    def test_evidence_text_has_stable_block_markers(self) -> None:
        text = _reconstruct_evidence_text(
            [
                Block(index=7, type="heading", original="Method", level=2),
                Block(index=9, type="paragraph", original="The model uses attention."),
            ]
        )

        self.assertEqual(
            text,
            "[block #7]\n## Method\n\n[block #9]\nThe model uses attention.",
        )

    def test_prompt_only_asks_model_for_block_index(self) -> None:
        self.assertIn("location 只填写对应的整数 block_index", REPRODUCIBILITY_PROMPT)
        self.assertIn("不得在 location 中填写 page、region_id、bbox", REPRODUCIBILITY_PROMPT)

    def test_invalid_reproducibility_json_keeps_four_evidence_aspects(self) -> None:
        report = asyncio.run(_run_reproducibility(_FakeAgent("not json"), "paper text"))

        self.assertEqual(report.verdict, "insufficient_info")
        self.assertEqual([item.aspect for item in report.evidence], list(REPRODUCIBILITY_ASPECTS))

    def test_partial_reproducibility_json_fills_missing_aspects(self) -> None:
        report = asyncio.run(
            _run_reproducibility(
                _FakeAgent(
                    """
{
  "verdict": "partially_reproducible",
  "confidence": "medium",
  "evidence": [
    {"aspect": "代码", "status": "已开源", "detail": "GitHub repo provided.", "citation": "Appendix A"}
  ],
  "summary": "代码可用，但其他信息不足。"
}
"""
                ),
                "paper text",
            )
        )

        aspects = [item.aspect for item in report.evidence]
        self.assertEqual(report.verdict, "partially_reproducible")
        for aspect in REPRODUCIBILITY_ASPECTS:
            self.assertIn(aspect, aspects)

    def test_model_location_keeps_only_valid_block_index(self) -> None:
        report = asyncio.run(
            _run_reproducibility(
                _FakeAgent(
                    """
{
  "verdict": "partially_reproducible",
  "confidence": "medium",
  "evidence": [
    {
      "aspect": "代码",
      "status": "已开源",
      "detail": "GitHub repo provided.",
      "citation": "Appendix A",
      "location": {"block_index": 7, "page": 99, "region_id": "invented"}
    },
    {
      "aspect": "数据集",
      "status": "未公开",
      "detail": "No dataset link.",
      "citation": "Section 4",
      "location": {"block_index": 999}
    }
  ],
  "summary": "部分信息可用。"
}
"""
                ),
                "[block #7]\nGitHub repo provided.",
                {7},
            )
        )

        code = next(item for item in report.evidence if item.aspect == "代码")
        dataset = next(item for item in report.evidence if item.aspect == "数据集")
        self.assertEqual(code.location, EvidenceLocation(block_index=7))
        self.assertIsNone(code.location.page)
        self.assertIsNone(code.location.region_id)
        self.assertIsNone(dataset.location)

    def test_analyze_paper_sends_marked_text_only_to_evidence_agent(self) -> None:
        agents = {
            "summary": _FakeAgent("Summary."),
            "reproducibility": _FakeAgent(
                '{"verdict":"insufficient_info","confidence":"low",'
                '"evidence":[],"summary":"Missing details."}'
            ),
            "improvement": _FakeAgent("[]"),
            "highlights": _FakeAgent("[]"),
        }
        blocks = [Block(index=4, type="paragraph", original="Evidence paragraph.")]

        with (
            patch(
                "backend.agent.orchestrator.get_config",
                return_value=SimpleNamespace(agent_concurrency=17),
            ),
            patch("backend.agent.orchestrator._build_specialists", return_value=agents),
        ):
            asyncio.run(analyze_paper(blocks))

        self.assertEqual(agents["summary"].inputs, ["Evidence paragraph."])
        self.assertEqual(
            agents["reproducibility"].inputs,
            ["[block #4]\nEvidence paragraph."],
        )


if __name__ == "__main__":
    unittest.main()
