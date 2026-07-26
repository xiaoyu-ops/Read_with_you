"""Bounded Semantic Scholar literature-map construction and derived caching."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from ..storage import files
from .semantic_scholar import S2_API

S2_RECOMMENDATIONS_API = "https://api.semanticscholar.org/recommendations/v1"
LITERATURE_MAP_VERSION = 1
LITERATURE_MAP_DEFAULT_NODES = 40
LITERATURE_MAP_MAX_NODES = 50
LITERATURE_MAP_CACHE_TTL_SECONDS = 24 * 60 * 60
LITERATURE_MAP_STALE_TTL_SECONDS = 7 * 24 * 60 * 60
LITERATURE_MAP_REQUEST_TIMEOUT_SECONDS = 12.0

_S2_ID_RE = re.compile(r"^[a-f0-9]{40}$", re.I)
_ARXIV_REF_RE = re.compile(
    r"^ARXIV:(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?$",
    re.I,
)
_MAP_LOCKS: dict[str, asyncio.Lock] = {}

_PAPER_FIELDS = (
    "paperId,title,authors,abstract,year,venue,citationCount,referenceCount,"
    "externalIds,url,openAccessPdf,isOpenAccess"
)
_GRAPH_FIELDS = f"{_PAPER_FIELDS},embedding.specter_v2,references.paperId"


class LiteratureMapError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 503) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def normalize_paper_ref(paper_ref: str) -> str:
    value = str(paper_ref or "").strip()
    if _S2_ID_RE.fullmatch(value):
        return value.lower()
    if _ARXIV_REF_RE.fullmatch(value):
        prefix, arxiv_id = value.split(":", 1)
        arxiv_id = re.sub(r"v\d+$", "", arxiv_id, flags=re.I)
        return f"{prefix.upper()}:{arxiv_id}"
    raise LiteratureMapError(
        "invalid_paper_ref",
        "论文标识必须是 Semantic Scholar paper ID 或 ARXIV:<id>。",
        400,
    )


def candidate_paper_ref(*, paper_id: str | None, arxiv_id: str | None) -> str | None:
    if paper_id and _S2_ID_RE.fullmatch(str(paper_id).strip()):
        return str(paper_id).strip().lower()
    if arxiv_id:
        candidate = f"ARXIV:{str(arxiv_id).strip()}"
        try:
            return normalize_paper_ref(candidate)
        except LiteratureMapError:
            return None
    return None


def _cache_path(paper_ref: str) -> Path:
    digest = hashlib.sha256(paper_ref.encode("utf-8")).hexdigest()
    configured = os.environ.get("PEINIDU_LITERATURE_MAP_CACHE_DIR", "").strip()
    cache_root = (
        Path(configured).expanduser().resolve()
        if configured
        else files.DATA_DIR / "literature_maps"
    )
    return cache_root / f"{digest}.json"


def _read_cache(paper_ref: str) -> tuple[dict[str, Any], float] | None:
    path = _cache_path(paper_ref)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        saved_at = float(payload.pop("_cache_saved_at"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError, KeyError):
        return None
    if payload.get("version") != LITERATURE_MAP_VERSION:
        return None
    return payload, max(0.0, time.time() - saved_at)


def _write_cache(paper_ref: str, payload: dict[str, Any]) -> None:
    cached = dict(payload)
    cached["_cache_saved_at"] = time.time()
    files._write_json(_cache_path(paper_ref), cached)


def _cached_payload(
    payload: dict[str, Any],
    *,
    stale: bool,
    warning: str | None = None,
) -> dict[str, Any]:
    result = dict(payload)
    result["cached"] = True
    result["stale"] = stale
    warnings = list(result.get("warnings") or [])
    if warning and warning not in warnings:
        warnings.append(warning)
    result["warnings"] = warnings
    if stale:
        result["status"] = "partial"
    return result


async def get_literature_map(
    paper_ref: str,
    *,
    max_nodes: int = LITERATURE_MAP_DEFAULT_NODES,
) -> dict[str, Any]:
    normalized = normalize_paper_ref(paper_ref)
    bounded_nodes = max(10, min(int(max_nodes), LITERATURE_MAP_MAX_NODES))
    cache_key = f"{normalized}:{bounded_nodes}"
    cached = _read_cache(cache_key)
    if cached is not None and cached[1] <= LITERATURE_MAP_CACHE_TTL_SECONDS:
        return _cached_payload(cached[0], stale=False)

    lock = _MAP_LOCKS.setdefault(cache_key, asyncio.Lock())
    async with lock:
        cached = _read_cache(cache_key)
        if cached is not None and cached[1] <= LITERATURE_MAP_CACHE_TTL_SECONDS:
            return _cached_payload(cached[0], stale=False)
        try:
            payload = await _build_literature_map(normalized, max_nodes=bounded_nodes)
        except LiteratureMapError:
            if cached is not None and cached[1] <= LITERATURE_MAP_STALE_TTL_SECONDS:
                return _cached_payload(
                    cached[0],
                    stale=True,
                    warning="外部数据暂时不可用，当前显示最近一次缓存。",
                )
            raise
        except Exception as exc:
            if cached is not None and cached[1] <= LITERATURE_MAP_STALE_TTL_SECONDS:
                return _cached_payload(
                    cached[0],
                    stale=True,
                    warning="外部数据暂时不可用，当前显示最近一次缓存。",
                )
            raise LiteratureMapError(
                "literature_map_unavailable",
                "论文关系数据暂时不可用，请稍后重试。",
            ) from exc
        _write_cache(cache_key, payload)
        return payload


async def _request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> Any:
    for attempt in range(2):
        response = await client.request(method, url, params=params, json=json_body)
        if response.status_code == 429 and attempt == 0:
            retry_after = response.headers.get("Retry-After", "1")
            try:
                delay = min(2.0, max(0.0, float(retry_after)))
            except ValueError:
                delay = 1.0
            await asyncio.sleep(delay)
            continue
        if response.status_code == 404:
            raise LiteratureMapError("paper_not_found", "Semantic Scholar 中没有找到这篇论文。", 404)
        if response.status_code >= 400:
            raise LiteratureMapError(
                "semantic_scholar_unavailable",
                f"Semantic Scholar 请求失败（{response.status_code}）。",
            )
        return response.json()
    raise LiteratureMapError("semantic_scholar_rate_limited", "Semantic Scholar 请求过于频繁。")


async def _fetch_seed(client: httpx.AsyncClient, paper_ref: str) -> dict[str, Any]:
    encoded = quote(paper_ref, safe=":")
    data = await _request_json(
        client,
        "GET",
        f"{S2_API}/paper/{encoded}",
        params={"fields": _PAPER_FIELDS},
    )
    if not isinstance(data, dict) or not data.get("paperId"):
        raise LiteratureMapError("paper_not_found", "Semantic Scholar 中没有找到这篇论文。", 404)
    return data


async def _fetch_recommendations(
    client: httpx.AsyncClient,
    paper_id: str,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    data = await _request_json(
        client,
        "GET",
        f"{S2_RECOMMENDATIONS_API}/papers/forpaper/{quote(paper_id, safe='')}",
        params={"limit": min(max(limit, 1), 100), "fields": _PAPER_FIELDS},
    )
    items = data.get("recommendedPapers") if isinstance(data, dict) else None
    return [item for item in items or [] if isinstance(item, dict) and item.get("paperId")]


async def _fetch_relations(
    client: httpx.AsyncClient,
    paper_id: str,
    relation: str,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if relation not in {"references", "citations"}:
        raise ValueError("unsupported relation")
    data = await _request_json(
        client,
        "GET",
        f"{S2_API}/paper/{quote(paper_id, safe='')}/{relation}",
        params={"limit": min(max(limit, 1), 100), "fields": _PAPER_FIELDS},
    )
    wrapper = "citedPaper" if relation == "references" else "citingPaper"
    rows = data.get("data") if isinstance(data, dict) else None
    papers = [
        row.get(wrapper)
        for row in rows or []
        if isinstance(row, dict) and isinstance(row.get(wrapper), dict)
    ]
    return [paper for paper in papers if paper.get("paperId")]


async def _fetch_batch(
    client: httpx.AsyncClient,
    paper_ids: list[str],
    *,
    fields: str,
) -> list[dict[str, Any]]:
    if not paper_ids:
        return []
    data = await _request_json(
        client,
        "POST",
        f"{S2_API}/paper/batch",
        params={"fields": fields},
        json_body={"ids": paper_ids[:500]},
    )
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict) and item.get("paperId")]


def _authors(raw: Any) -> list[str]:
    return [
        str(author.get("name")).strip()
        for author in raw or []
        if isinstance(author, dict) and str(author.get("name") or "").strip()
    ]


def _embedding(raw: Any) -> list[float] | None:
    if not isinstance(raw, dict):
        return None
    vector = raw.get("vector")
    if not isinstance(vector, list):
        for value in raw.values():
            if isinstance(value, list):
                vector = value
                break
            if isinstance(value, dict) and isinstance(value.get("vector"), list):
                vector = value["vector"]
                break
    if not isinstance(vector, list) or not vector:
        return None
    try:
        result = [float(value) for value in vector]
    except (TypeError, ValueError):
        return None
    return result if all(math.isfinite(value) for value in result) else None


def _paper(raw: dict[str, Any]) -> dict[str, Any]:
    external = raw.get("externalIds") if isinstance(raw.get("externalIds"), dict) else {}
    open_pdf = raw.get("openAccessPdf") if isinstance(raw.get("openAccessPdf"), dict) else {}
    references = raw.get("references") if isinstance(raw.get("references"), list) else []
    return {
        "id": str(raw.get("paperId") or ""),
        "arxiv_id": str(external.get("ArXiv") or "") or None,
        "doi": str(external.get("DOI") or "") or None,
        "title": str(raw.get("title") or "未命名论文"),
        "authors": _authors(raw.get("authors")),
        "abstract": str(raw.get("abstract") or ""),
        "year": raw.get("year") if isinstance(raw.get("year"), int) else None,
        "venue": str(raw.get("venue") or "") or None,
        "citation_count": raw.get("citationCount")
        if isinstance(raw.get("citationCount"), int)
        else None,
        "reference_count": raw.get("referenceCount")
        if isinstance(raw.get("referenceCount"), int)
        else None,
        "is_open_access": bool(raw.get("isOpenAccess")),
        "pdf_url": str(open_pdf.get("url") or "") or None,
        "url": str(raw.get("url") or "")
        or f"https://www.semanticscholar.org/paper/{raw.get('paperId', '')}",
        "_embedding": _embedding(raw.get("embedding")),
        "_reference_ids": [
            str(item.get("paperId"))
            for item in references
            if isinstance(item, dict) and item.get("paperId")
        ],
    }


def _cosine(left: list[float] | None, right: list[float] | None) -> float | None:
    if not left or not right or len(left) != len(right):
        return None
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return None
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


def _similarity_edges(
    papers: list[dict[str, Any]],
    recommendation_rank: dict[str, int],
) -> tuple[list[dict[str, Any]], dict[str, float | None]]:
    pairs: dict[tuple[str, str], dict[str, Any]] = {}
    similarities: dict[str, float | None] = {}
    origin = papers[0]
    origin_id = origin["id"]
    for paper in papers:
        similarities[paper["id"]] = (
            1.0 if paper["id"] == origin_id else _cosine(origin["_embedding"], paper["_embedding"])
        )
    for paper in papers:
        scored = [
            (candidate["id"], score)
            for candidate in papers
            if candidate["id"] != paper["id"]
            and (score := _cosine(paper["_embedding"], candidate["_embedding"])) is not None
        ]
        scored.sort(key=lambda item: item[1], reverse=True)
        for target_id, score in scored[:3]:
            key = tuple(sorted((paper["id"], target_id)))
            current = pairs.get(key)
            if current is None or score > current["weight"]:
                pairs[key] = {
                    "source": key[0],
                    "target": key[1],
                    "kind": "similarity",
                    "weight": round(max(0.0, score), 6),
                    "provenance": "semantic_scholar_specter2",
                }
    recommendation_count = max(len(recommendation_rank), 1)
    connected = {edge_id for pair in pairs for edge_id in pair}
    for paper in papers[1:]:
        paper_id = paper["id"]
        if paper_id in connected or paper_id not in recommendation_rank:
            continue
        rank = recommendation_rank[paper_id]
        weight = max(0.05, 1.0 - (rank - 1) / recommendation_count)
        key = tuple(sorted((origin_id, paper_id)))
        pairs[key] = {
            "source": key[0],
            "target": key[1],
            "kind": "similarity",
            "weight": round(weight, 6),
            "provenance": "semantic_scholar_recommendations",
        }
        if similarities[paper_id] is None:
            similarities[paper_id] = weight
    return list(pairs.values()), similarities


def _citation_edges(
    papers: list[dict[str, Any]],
    *,
    seed_reference_ids: set[str],
    seed_citation_ids: set[str],
) -> list[dict[str, Any]]:
    node_ids = {paper["id"] for paper in papers}
    origin_id = papers[0]["id"]
    pairs: set[tuple[str, str]] = set()
    for paper in papers:
        for target_id in set(paper["_reference_ids"]) & node_ids:
            if target_id != paper["id"]:
                pairs.add((paper["id"], target_id))
    pairs.update((origin_id, target) for target in seed_reference_ids & node_ids if target != origin_id)
    pairs.update((source, origin_id) for source in seed_citation_ids & node_ids if source != origin_id)
    return [
        {
            "source": source,
            "target": target,
            "kind": "citation",
            "weight": 1.0,
            "provenance": "semantic_scholar_academic_graph",
        }
        for source, target in sorted(pairs)
    ]


def _public_paper(paper: dict[str, Any], similarity: float | None = None) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            **paper,
            "similarity": round(similarity, 6) if similarity is not None else None,
        }.items()
        if not key.startswith("_")
    }


async def _build_literature_map(paper_ref: str, *, max_nodes: int) -> dict[str, Any]:
    warnings: list[str] = []
    timeout = httpx.Timeout(LITERATURE_MAP_REQUEST_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        seed_raw = await _fetch_seed(client, paper_ref)
        seed_id = str(seed_raw["paperId"])
        results = await asyncio.gather(
            _fetch_recommendations(client, seed_id, limit=max_nodes * 2),
            _fetch_relations(client, seed_id, "references"),
            _fetch_relations(client, seed_id, "citations"),
            return_exceptions=True,
        )
        recommendations, seed_references, seed_citations = [], [], []
        labels = ("相似论文推荐", "参考文献", "引用本文的论文")
        targets = (recommendations, seed_references, seed_citations)
        for result, label, target in zip(results, labels, targets):
            if isinstance(result, Exception):
                warnings.append(f"{label}暂时不可用。")
            else:
                target.extend(result)

        seed_references.sort(key=lambda item: int(item.get("citationCount") or 0), reverse=True)
        seed_citations.sort(key=lambda item: int(item.get("citationCount") or 0), reverse=True)
        recommendation_rank = {
            str(item["paperId"]): index
            for index, item in enumerate(recommendations, start=1)
        }

        selected: list[dict[str, Any]] = [seed_raw]
        selected_ids = {seed_id}

        def add(items: list[dict[str, Any]], limit: int) -> None:
            added = 0
            for item in items:
                item_id = str(item.get("paperId") or "")
                if not item_id or item_id in selected_ids:
                    continue
                selected.append(item)
                selected_ids.add(item_id)
                added += 1
                if added >= limit or len(selected) >= max_nodes:
                    break

        add(recommendations[:30], 30)
        add(seed_references, 10)
        add(seed_citations, 10)
        add(recommendations[30:], max_nodes)
        selected = selected[:max_nodes]
        selected_ids = {str(item["paperId"]) for item in selected}

        try:
            batch = await _fetch_batch(
                client,
                [str(item["paperId"]) for item in selected],
                fields=_GRAPH_FIELDS,
            )
        except LiteratureMapError:
            warnings.append("部分相似度和图内引用关系暂时不可用。")
            batch = []
        batch_by_id = {str(item["paperId"]): item for item in batch}
        enriched = [batch_by_id.get(str(item["paperId"]), item) for item in selected]
        papers = [_paper(item) for item in enriched]
        if not papers or not papers[0]["id"]:
            raise LiteratureMapError("paper_not_found", "无法解析核心论文。", 404)

        similarity_edges, similarities = _similarity_edges(papers, recommendation_rank)
        citation_edges = _citation_edges(
            papers,
            seed_reference_ids={str(item["paperId"]) for item in seed_references},
            seed_citation_ids={str(item["paperId"]) for item in seed_citations},
        )

        reference_counts = Counter(
            reference_id
            for paper in papers
            for reference_id in set(paper["_reference_ids"])
            if reference_id not in selected_ids
        )
        prior_ids = [
            paper_id
            for paper_id, count in reference_counts.most_common(20)
            if count >= 2
        ]
        prior_by_id: dict[str, dict[str, Any]] = {}
        if prior_ids:
            try:
                prior_batch = await _fetch_batch(client, prior_ids, fields=_PAPER_FIELDS)
                prior_by_id = {str(item["paperId"]): _paper(item) for item in prior_batch}
            except LiteratureMapError:
                warnings.append("先行工作元数据暂时不可用。")
        prior_works = [
            {
                "paper": _public_paper(prior_by_id[paper_id]),
                "graph_citation_count": reference_counts[paper_id],
            }
            for paper_id in prior_ids
            if paper_id in prior_by_id
        ]

        derivative_works = []
        for paper in papers[1:]:
            graph_reference_count = len(set(paper["_reference_ids"]) & selected_ids)
            if graph_reference_count:
                derivative_works.append(
                    {
                        "paper": _public_paper(paper, similarities.get(paper["id"])),
                        "graph_reference_count": graph_reference_count,
                    }
                )
        derivative_works.sort(
            key=lambda item: (
                item["graph_reference_count"],
                item["paper"].get("citation_count") or 0,
            ),
            reverse=True,
        )

        public_nodes = [
            {
                **_public_paper(paper, similarities.get(paper["id"])),
                "role": "origin" if index == 0 else "related",
            }
            for index, paper in enumerate(papers)
        ]
        return {
            "version": LITERATURE_MAP_VERSION,
            "origin": public_nodes[0],
            "nodes": public_nodes,
            "edges": [*similarity_edges, *citation_edges],
            "prior_works": prior_works,
            "derivative_works": derivative_works[:20],
            "status": "partial" if warnings else "complete",
            "provider": "semantic_scholar",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "cached": False,
            "stale": False,
            "warnings": warnings,
        }
