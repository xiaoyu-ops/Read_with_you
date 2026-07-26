"""检索路由 — POST /search（标题 → 候选列表，D7 用户确认）。"""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..retrieval.arxiv import PaperCandidate
from ..retrieval.match import merge_and_rank

router = APIRouter(tags=["search"])
logger = logging.getLogger(__name__)

SEARCH_CACHE_TTL_SECONDS = 300
SEARCH_CACHE_MAX_ITEMS = 128
SEARCH_SOURCE_TIMEOUT_SECONDS = 8.0
_SEARCH_CACHE: dict[tuple[str, int], tuple[float, list[dict], int]] = {}


class SearchRequest(BaseModel):
    query: str  # 标题 / arXiv ID / DOI / URL
    max_results: int = 10


class SearchResponse(BaseModel):
    query: str
    candidates: list[dict]
    count: int


@router.post("/search", response_model=SearchResponse)
async def search(req: SearchRequest) -> SearchResponse:
    """双源检索（arXiv + Semantic Scholar）+ rapidfuzz 模糊匹配排序。"""
    return await search_papers(req.query, max_results=req.max_results)


async def search_papers(
    raw_query: str,
    *,
    max_results: int = 10,
    use_cache: bool = True,
) -> SearchResponse:
    """Run the shared public-metadata search without requiring content storage."""
    query = raw_query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="query 不能为空")

    max_results = max(1, min(max_results, 20))
    cache_key = (query.casefold(), max_results)
    cached = _get_search_cache(cache_key) if use_cache else None
    if cached is not None:
        candidates, count = cached
        return SearchResponse(query=query, candidates=candidates[:max_results], count=count)

    # 并行双源检索
    arxiv_task = asyncio.create_task(_safe_arxiv(query, max_results))
    s2_task = asyncio.create_task(_safe_s2(query, max_results))
    arxiv_results, s2_results = await asyncio.gather(arxiv_task, s2_task)

    ranked = merge_and_rank(query, arxiv_results, s2_results)
    candidates = [c.to_dict() for c in ranked[:max_results]]
    if use_cache:
        _set_search_cache(cache_key, candidates, len(ranked))
    return SearchResponse(
        query=query,
        candidates=candidates,
        count=len(ranked),
    )


def _get_search_cache(key: tuple[str, int]) -> tuple[list[dict], int] | None:
    cached = _SEARCH_CACHE.get(key)
    if cached is None:
        return None
    ts, candidates, count = cached
    if time.monotonic() - ts > SEARCH_CACHE_TTL_SECONDS:
        _SEARCH_CACHE.pop(key, None)
        return None
    return candidates, count


def _set_search_cache(key: tuple[str, int], candidates: list[dict], count: int) -> None:
    if len(_SEARCH_CACHE) >= SEARCH_CACHE_MAX_ITEMS:
        oldest_key = min(_SEARCH_CACHE, key=lambda item: _SEARCH_CACHE[item][0])
        _SEARCH_CACHE.pop(oldest_key, None)
    _SEARCH_CACHE[key] = (time.monotonic(), candidates, count)


async def _safe_arxiv(query: str, n: int) -> list[PaperCandidate]:
    """arXiv 检索，异常不中断（单源失败不影响另一个）。"""
    from ..retrieval.arxiv import search_arxiv
    try:
        return await search_arxiv(query, max_results=n, timeout=SEARCH_SOURCE_TIMEOUT_SECONDS)
    except Exception as e:
        logger.warning("arXiv 检索失败: %s", e)
        return []


async def _safe_s2(query: str, n: int) -> list[PaperCandidate]:
    from ..retrieval.semantic_scholar import search_s2_combined
    try:
        return await search_s2_combined(
            query,
            max_results=n,
            timeout=SEARCH_SOURCE_TIMEOUT_SECONDS,
        )
    except Exception as e:
        logger.warning("Semantic Scholar 检索失败: %s", e)
        return []
