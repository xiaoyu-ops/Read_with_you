from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend.agent.evidence_location import enrich_result_data_locations
from backend.api import routes_agent_chat


class EvidenceLocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.layout = {
            "regions": [
                {
                    "block_index": 3,
                    "page": 4,
                    "region_id": "later",
                    "flow_order": 8,
                },
                {
                    "block_index": 3,
                    "page": 2,
                    "region_id": "primary",
                    "flow_order": 2,
                },
            ]
        }

    def test_enriches_block_with_deterministic_current_region(self) -> None:
        data = {
            "summary": "result",
            "evidence": [
                {
                    "claim": "method detail",
                    "location": {
                        "block_index": 3,
                        "page": 99,
                        "region_id": "model-invented",
                    },
                }
            ],
        }

        result = enrich_result_data_locations(data, self.layout, valid_block_indexes={3})

        self.assertEqual(
            result["evidence"][0]["location"],
            {"block_index": 3, "page": 2, "region_id": "primary"},
        )
        self.assertEqual(data["evidence"][0]["location"]["page"], 99)

    def test_keeps_unmapped_block_as_stable_fallback(self) -> None:
        result = enrich_result_data_locations(
            {"evidence": [{"claim": "note", "source": "block #7"}]},
            self.layout,
            valid_block_indexes={7},
        )
        self.assertEqual(result["evidence"][0]["location"], {"block_index": 7})

    def test_accepts_only_explicit_textual_block_references(self) -> None:
        result = enrich_result_data_locations(
            {
                "evidence": [
                    {"claim": "english", "source": "block #3, page 2"},
                    {"claim": "chinese", "citation": "段落 3"},
                    {"claim": "bare", "source": "#3"},
                    {"claim": "issue", "source": "GitHub issue #3"},
                    {"claim": "page only", "citation": "page 3"},
                ]
            },
            self.layout,
            valid_block_indexes={3},
        )

        self.assertEqual(
            result["evidence"][0]["location"],
            {"block_index": 3, "page": 2, "region_id": "primary"},
        )
        self.assertEqual(
            result["evidence"][1]["location"],
            {"block_index": 3, "page": 2, "region_id": "primary"},
        )
        self.assertNotIn("location", result["evidence"][2])
        self.assertNotIn("location", result["evidence"][3])
        self.assertNotIn("location", result["evidence"][4])

    def test_preserves_only_a_preferred_region_verified_against_current_layout(self) -> None:
        result = enrich_result_data_locations(
            {
                "evidence": [
                    {
                        "claim": "selected evidence",
                        "location": {"block_index": 3, "page": 99, "region_id": "later"},
                    }
                ]
            },
            self.layout,
            valid_block_indexes={3},
        )
        self.assertEqual(
            result["evidence"][0]["location"],
            {"block_index": 3, "page": 4, "region_id": "later"},
        )

    def test_drops_untrusted_geometry_without_valid_block(self) -> None:
        result = enrich_result_data_locations(
            {
                "evidence": [
                    {"claim": "old", "citation": "Section 4.1"},
                    {"claim": "bad", "location": {"block_index": 99, "page": 9}},
                ]
            },
            self.layout,
            valid_block_indexes={3},
        )
        self.assertEqual(result["evidence"][0], {"claim": "old", "citation": "Section 4.1"})
        self.assertEqual(result["evidence"][1], {"claim": "bad"})

    def test_ignores_malformed_regions_and_boolean_indexes(self) -> None:
        layout = {
            "regions": [
                {"block_index": True, "page": 1, "region_id": "bad", "flow_order": 0},
                {"block_index": 2, "page": 0, "region_id": "bad-page", "flow_order": 1},
            ]
        }
        result = enrich_result_data_locations(
            {"evidence": [{"block_index": 2}]},
            layout,
            valid_block_indexes={2},
        )
        self.assertEqual(result["evidence"][0]["location"], {"block_index": 2})

    def test_agent_result_enrichment_loads_current_paper_layout(self) -> None:
        with (
            patch.object(
                routes_agent_chat.files,
                "load_document",
                return_value=SimpleNamespace(blocks=[SimpleNamespace(index=3)]),
            ),
            patch.object(
                routes_agent_chat.files,
                "load_translation_layout",
                return_value=self.layout,
            ),
        ):
            result = routes_agent_chat._enrich_result_data_for_paper(
                "paper-id",
                {"evidence": [{"source": "block #3", "claim": "grounded"}]},
            )

        self.assertEqual(
            result["evidence"][0]["location"],
            {"block_index": 3, "page": 2, "region_id": "primary"},
        )


if __name__ == "__main__":
    unittest.main()
