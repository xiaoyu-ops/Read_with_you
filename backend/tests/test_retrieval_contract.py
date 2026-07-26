from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from backend.api import routes_search
from backend.api.routes_papers import _is_arxiv_id
from backend.retrieval.arxiv import _is_arxiv_id_or_url
from backend.retrieval.arxiv import PaperCandidate
from backend.retrieval.match import _title_similarity, merge_and_rank
from backend.retrieval.semantic_scholar import _s2_paper_to_candidate


class RetrievalContractTest(unittest.TestCase):
    def test_s2_without_arxiv_id_is_not_extractable(self) -> None:
        candidate = _s2_paper_to_candidate(
            {
                "paperId": "S2-PAPER-ID",
                "title": "A Non arXiv Paper",
                "authors": [{"name": "Ada"}],
                "externalIds": {},
            }
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.arxiv_id, "")
        self.assertEqual(candidate.paper_id, "S2-PAPER-ID")
        self.assertFalse(candidate.extractable)

    def test_merge_keeps_s2_only_candidates_as_non_extractable(self) -> None:
        arxiv = PaperCandidate(
            arxiv_id="1706.03762",
            title="Attention Is All You Need",
            authors=[],
            abstract="",
        )
        s2_only = PaperCandidate(
            arxiv_id="",
            title="A Related Non arXiv Paper",
            authors=[],
            abstract="",
            citation_count=999999,
            paper_id="S2-ONLY",
            extractable=False,
        )

        results = merge_and_rank("attention", [arxiv], [s2_only])

        self.assertEqual(len(results), 2)
        self.assertTrue(results[0].extractable)
        self.assertTrue(any(item.arxiv_id == "1706.03762" and item.extractable for item in results))
        self.assertTrue(any(item.paper_id == "S2-ONLY" and not item.extractable for item in results))

    def test_create_paper_accepts_only_arxiv_ids(self) -> None:
        self.assertTrue(_is_arxiv_id("1706.03762"))
        self.assertTrue(_is_arxiv_id("hep-th/9901001"))
        self.assertFalse(_is_arxiv_id(""))
        self.assertFalse(_is_arxiv_id("S2-PAPER-ID"))

    def test_search_only_treats_real_arxiv_ids_as_ids(self) -> None:
        self.assertTrue(_is_arxiv_id_or_url("1706.03762"))
        self.assertTrue(_is_arxiv_id_or_url("1706.03762v7"))
        self.assertTrue(_is_arxiv_id_or_url("https://arxiv.org/abs/1706.03762"))
        self.assertTrue(_is_arxiv_id_or_url("hep-th/9901001"))
        self.assertFalse(_is_arxiv_id_or_url("2024"))
        self.assertFalse(_is_arxiv_id_or_url("1.2.3"))
        self.assertFalse(_is_arxiv_id_or_url("attention 2024"))

    def test_similarity_is_not_substring_hit_rate(self) -> None:
        self.assertEqual(_title_similarity("attention is all you need", "attention is all you need"), 100)
        self.assertLess(_title_similarity("attention", "visual attention network"), 100)
        self.assertLess(
            _title_similarity(
                "attention",
                "gated sparse attention combining computational efficiency",
            ),
            _title_similarity("attention", "visual attention network"),
        )

    def test_search_route_reuses_short_term_cache(self) -> None:
        async def run():
            routes_search._SEARCH_CACHE.clear()
            candidate = PaperCandidate(
                arxiv_id="1706.03762",
                title="Attention Is All You Need",
                authors=[],
                abstract="",
            )
            with (
                patch.object(routes_search, "_safe_arxiv", AsyncMock(return_value=[candidate])) as arxiv,
                patch.object(routes_search, "_safe_s2", AsyncMock(return_value=[])) as s2,
            ):
                req = routes_search.SearchRequest(query="Attention Is All You Need")
                first = await routes_search.search(req)
                second = await routes_search.search(req)
            routes_search._SEARCH_CACHE.clear()
            return first, second, arxiv, s2

        first, second, arxiv, s2 = asyncio.run(run())

        self.assertEqual(first.candidates[0]["arxiv_id"], "1706.03762")
        self.assertEqual(second.candidates[0]["arxiv_id"], "1706.03762")
        arxiv.assert_awaited_once()
        s2.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
