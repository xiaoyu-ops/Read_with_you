from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from backend.api.main import create_app
from backend.retrieval import literature_map


SEED_ID = "a" * 40
REC_ID = "b" * 40
REC_TWO_ID = "c" * 40
REFERENCE_ID = "d" * 40
CITATION_ID = "e" * 40
PRIOR_ID = "f" * 40


def paper(
    paper_id: str,
    title: str,
    *,
    year: int = 2024,
    citations: int = 10,
    references: list[str] | None = None,
    embedding: list[float] | None = None,
    arxiv_id: str | None = None,
) -> dict:
    return {
        "paperId": paper_id,
        "title": title,
        "authors": [{"name": f"{title} Author"}],
        "abstract": f"{title} abstract",
        "year": year,
        "venue": "TestConf",
        "citationCount": citations,
        "referenceCount": len(references or []),
        "externalIds": {"ArXiv": arxiv_id} if arxiv_id else {},
        "url": f"https://www.semanticscholar.org/paper/{paper_id}",
        "openAccessPdf": {"url": f"https://example.com/{paper_id}.pdf"},
        "isOpenAccess": True,
        "embedding": {"model": "specter_v2", "vector": embedding}
        if embedding is not None
        else None,
        "references": [{"paperId": item} for item in references or []],
    }


class LiteratureMapTest(unittest.TestCase):
    def test_normalizes_supported_refs_and_rejects_unknown_values(self) -> None:
        self.assertEqual(literature_map.normalize_paper_ref(SEED_ID.upper()), SEED_ID)
        self.assertEqual(
            literature_map.normalize_paper_ref("arxiv:2307.16789v2"),
            "ARXIV:2307.16789",
        )
        self.assertEqual(
            literature_map.normalize_paper_ref("ARXIV:hep-th/9901001"),
            "ARXIV:hep-th/9901001",
        )
        with self.assertRaises(literature_map.LiteratureMapError):
            literature_map.normalize_paper_ref("../paper")

    def test_candidate_ref_prefers_s2_and_falls_back_to_arxiv(self) -> None:
        self.assertEqual(
            literature_map.candidate_paper_ref(paper_id=SEED_ID, arxiv_id="2307.16789"),
            SEED_ID,
        )
        self.assertEqual(
            literature_map.candidate_paper_ref(paper_id=None, arxiv_id="2307.16789"),
            "ARXIV:2307.16789",
        )
        self.assertIsNone(literature_map.candidate_paper_ref(paper_id=None, arxiv_id=None))

    def test_builds_similarity_citation_prior_and_derivative_views(self) -> None:
        seed = paper(
            SEED_ID,
            "Seed",
            embedding=[1.0, 0.0],
            references=[REC_ID, REFERENCE_ID, PRIOR_ID],
            arxiv_id="2307.16789",
        )
        recommended = [
            paper(
                REC_ID,
                "Recommended",
                embedding=[0.95, 0.05],
                references=[SEED_ID, PRIOR_ID],
            ),
            paper(
                REC_TWO_ID,
                "Recommended Two",
                embedding=[0.8, 0.2],
                references=[REC_ID, PRIOR_ID],
            ),
        ]
        reference = paper(REFERENCE_ID, "Reference", year=2020, embedding=[0.7, 0.3])
        citation = paper(
            CITATION_ID,
            "Citation",
            year=2025,
            embedding=[0.75, 0.25],
            references=[SEED_ID, REC_ID],
        )
        graph_batch = [seed, *recommended, reference, citation]
        prior_batch = [paper(PRIOR_ID, "Prior", year=2018, citations=999)]

        async def run() -> dict:
            with (
                patch.object(literature_map, "_fetch_seed", AsyncMock(return_value=seed)),
                patch.object(
                    literature_map,
                    "_fetch_recommendations",
                    AsyncMock(return_value=recommended),
                ),
                patch.object(
                    literature_map,
                    "_fetch_relations",
                    AsyncMock(side_effect=[[reference], [citation]]),
                ),
                patch.object(
                    literature_map,
                    "_fetch_batch",
                    AsyncMock(side_effect=[graph_batch, prior_batch]),
                ),
            ):
                return await literature_map._build_literature_map(SEED_ID, max_nodes=40)

        result = asyncio.run(run())
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["origin"]["id"], SEED_ID)
        self.assertEqual(len(result["nodes"]), 5)
        self.assertTrue(any(edge["kind"] == "similarity" for edge in result["edges"]))
        citation_edges = {
            (edge["source"], edge["target"])
            for edge in result["edges"]
            if edge["kind"] == "citation"
        }
        self.assertIn((SEED_ID, REFERENCE_ID), citation_edges)
        self.assertIn((CITATION_ID, SEED_ID), citation_edges)
        self.assertEqual(result["prior_works"][0]["paper"]["id"], PRIOR_ID)
        self.assertEqual(result["prior_works"][0]["graph_citation_count"], 3)
        derivative = {
            item["paper"]["id"]: item["graph_reference_count"]
            for item in result["derivative_works"]
        }
        self.assertEqual(derivative[CITATION_ID], 2)

    def test_hard_caps_graph_at_fifty_nodes(self) -> None:
        seed = paper(SEED_ID, "Seed")
        recommendations = [
            paper(f"{index:040x}", f"Paper {index}") for index in range(1, 90)
        ]

        async def fake_batch(_client, ids, *, fields):
            return [seed, *recommendations][: len(ids)]

        async def run() -> dict:
            with (
                patch.object(literature_map, "_fetch_seed", AsyncMock(return_value=seed)),
                patch.object(
                    literature_map,
                    "_fetch_recommendations",
                    AsyncMock(return_value=recommendations),
                ),
                patch.object(
                    literature_map,
                    "_fetch_relations",
                    AsyncMock(side_effect=[[], []]),
                ),
                patch.object(literature_map, "_fetch_batch", side_effect=fake_batch),
            ):
                return await literature_map._build_literature_map(SEED_ID, max_nodes=50)

        result = asyncio.run(run())
        self.assertLessEqual(len(result["nodes"]), 50)

    def test_reuses_fresh_cache_and_falls_back_to_stale_cache(self) -> None:
        payload = {
            "version": 1,
            "origin": {"id": SEED_ID},
            "nodes": [],
            "edges": [],
            "prior_works": [],
            "derivative_works": [],
            "status": "complete",
            "provider": "semantic_scholar",
            "retrieved_at": "2026-07-26T00:00:00+00:00",
            "cached": False,
            "stale": False,
            "warnings": [],
        }

        async def run(data_root: Path) -> tuple[dict, dict, int]:
            with (
                patch.object(literature_map.files, "DATA_DIR", data_root),
                patch.object(
                    literature_map,
                    "_build_literature_map",
                    AsyncMock(return_value=payload),
                ) as build,
            ):
                first = await literature_map.get_literature_map(SEED_ID)
                second = await literature_map.get_literature_map(SEED_ID)
                return first, second, build.await_count

        with tempfile.TemporaryDirectory() as tmp:
            first, second, calls = asyncio.run(run(Path(tmp)))
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertFalse(second["stale"])
        self.assertEqual(calls, 1)

        async def stale(data_root: Path) -> dict:
            cache_key = f"{SEED_ID}:{literature_map.LITERATURE_MAP_DEFAULT_NODES}"
            with patch.object(literature_map.files, "DATA_DIR", data_root):
                literature_map._write_cache(cache_key, payload)
                path = literature_map._cache_path(cache_key)
                stored = json.loads(path.read_text(encoding="utf-8"))
                stored["_cache_saved_at"] = time.time() - 2 * 24 * 60 * 60
                path.write_text(json.dumps(stored), encoding="utf-8")
                with patch.object(
                    literature_map,
                    "_build_literature_map",
                    AsyncMock(
                        side_effect=literature_map.LiteratureMapError(
                            "semantic_scholar_unavailable",
                            "down",
                        )
                    ),
                ):
                    return await literature_map.get_literature_map(SEED_ID)

        with tempfile.TemporaryDirectory() as tmp:
            fallback = asyncio.run(stale(Path(tmp)))
        self.assertTrue(fallback["cached"])
        self.assertTrue(fallback["stale"])
        self.assertEqual(fallback["status"], "partial")

    def test_retries_one_rate_limited_request(self) -> None:
        request = httpx.Request("GET", "https://example.test")
        responses = [
            httpx.Response(429, headers={"Retry-After": "0"}, request=request),
            httpx.Response(200, json={"ok": True}, request=request),
        ]
        client = AsyncMock()
        client.request = AsyncMock(side_effect=responses)

        result = asyncio.run(
            literature_map._request_json(client, "GET", "https://example.test")
        )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(client.request.await_count, 2)

    def test_local_api_and_public_page_reject_invalid_identifiers(self) -> None:
        with TestClient(create_app("local_core")) as client:
            self.assertEqual(client.get("/literature-map/not-valid").status_code, 400)
        with TestClient(create_app("public_portal")) as client:
            self.assertEqual(client.get("/literature-map/not-valid").status_code, 404)

    def test_public_cache_directory_can_be_isolated_from_paper_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_root = Path(tmp) / "portal-derived-maps"
            with patch.dict(
                "os.environ",
                {"PEINIDU_LITERATURE_MAP_CACHE_DIR": str(cache_root)},
            ):
                path = literature_map._cache_path(f"ARXIV:1706.03762:40")

        self.assertEqual(path.parent, cache_root.resolve())
        self.assertEqual(path.suffix, ".json")


if __name__ == "__main__":
    unittest.main()
