"""Semantic Scholar Graph API 检索（D6）。

双端点策略（参考调研结论）：
- /paper/search：关键词搜索，返回多结果 + citationCount + venue（relevance 排序）
- /paper/search/match：模糊标题匹配，返回单个最佳匹配 + matchScore
  （专门解决"用户输入近似标题找那篇论文"）
两个端点并行调用，结果合并。

S2 原生做模糊匹配，不需要客户端 fuzzy threshold 门控。
"""

from __future__ import annotations

import asyncio
import re

import httpx

from .arxiv import PaperCandidate

S2_API = "https://api.semanticscholar.org/graph/v1"
S2_SEARCH_FIELDS = "paperId,title,authors,abstract,year,externalIds,venue,citationCount,influentialCitationCount,openAccessPdf"
S2_MATCH_FIELDS = "paperId,title,authors,abstract,year,externalIds,venue,citationCount,openAccessPdf"
S2_AUTHOR_FIELDS = "authorId,url,name,affiliations,homepage,paperCount,citationCount,hIndex"


async def _safe_get(client: httpx.AsyncClient, url: str, params: dict) -> httpx.Response:
    """带 S2 限流重试的 GET。无 key 时 403 会重试一次。"""
    resp = await client.get(url, params=params)
    # S2 无 key 时偶尔 429，不重试直接返回（调用方处理）
    return resp


async def search_semantic_scholar(
    query: str, max_results: int = 10, timeout: float = 20.0
) -> list[PaperCandidate]:
    """Semantic Scholar 关键词搜索（/paper/search）。返回多结果 + citationCount。"""
    params = {
        "query": query,
        "limit": min(max_results, 100),
        "fields": S2_SEARCH_FIELDS,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{S2_API}/paper/search", params=params)
            if resp.status_code != 200:
                return []
            data = resp.json()
    except Exception:
        return []

    candidates: list[PaperCandidate] = []
    for paper in data.get("data", []):
        cand = _s2_paper_to_candidate(paper, source="s2")
        if cand:
            candidates.append(cand)
    return candidates


async def match_semantic_scholar(
    query: str, timeout: float = 20.0
) -> PaperCandidate | None:
    """Semantic Scholar 模糊标题匹配（/paper/search/match）。

    返回单个最佳匹配 + matchScore。专门解决"用户输入近似标题"。
    match 端点返回 {"data": [{...paper..., "matchScore": ...}]}，取第一个。
    """
    params = {
        "query": query,
        "fields": S2_MATCH_FIELDS,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{S2_API}/paper/search/match", params=params)
            if resp.status_code != 200:
                return None
            data = resp.json()
    except Exception:
        return None

    # match 端点返回 {"data": [...]} 列表，取第一个
    items = data.get("data") if isinstance(data, dict) else None
    if not items or not isinstance(items, list):
        return None
    paper = items[0]
    if not paper or "paperId" not in paper:
        return None
    cand = _s2_paper_to_candidate(paper, source="s2_match")
    if cand:
        cand.match_score = paper.get("matchScore")
    return cand


async def search_s2_combined(
    query: str, max_results: int = 10, timeout: float = 20.0
) -> list[PaperCandidate]:
    """并行调用 search + match，合并结果。"""
    search_task = asyncio.create_task(
        search_semantic_scholar(query, max_results, timeout)
    )
    match_task = asyncio.create_task(match_semantic_scholar(query, timeout))
    search_results, match_result = await asyncio.gather(search_task, match_task)

    results = list(search_results)
    if match_result:
        # 如果 match 结果不在 search 结果里，加到最前面
        existing_ids = {c.arxiv_id for c in results if c.arxiv_id}
        if match_result.arxiv_id not in existing_ids:
            results.insert(0, match_result)
        else:
            # 已在列表里，补上 match_score
            for c in results:
                if c.arxiv_id == match_result.arxiv_id and c.match_score is None:
                    c.match_score = match_result.match_score
                    c.source = "s2_match"
                    break
    return results


def _normalize_author_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _author_search_queries(query: str) -> list[str]:
    variants = [query]
    compact_hyphen = re.sub(r"(?<=\w)-(?=\w)", "", query)
    if compact_hyphen != query:
        variants.append(compact_hyphen)
    return variants


def _rank_author_results(query: str, authors: list[dict]) -> list[dict]:
    normalized_query = _normalize_author_name(query)

    def score(author: dict) -> tuple[int, int, int]:
        name = _normalize_author_name(str(author.get("name") or ""))
        citation_count = author.get("citation_count")
        citations = citation_count if isinstance(citation_count, int) else -1
        exact = int(name == normalized_query)
        contains = int(normalized_query in name or name in normalized_query)
        return exact, contains, citations

    return sorted(authors, key=score, reverse=True)


async def _fetch_semantic_scholar_authors(
    client: httpx.AsyncClient, query: str, limit: int
) -> list[dict]:
    params = {
        "query": query,
        "limit": limit,
        "fields": S2_AUTHOR_FIELDS,
    }
    try:
        resp = await client.get(f"{S2_API}/author/search", params=params)
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception:
        return []

    authors: list[dict] = []
    for author in data.get("data", []):
        if not isinstance(author, dict) or not author.get("name"):
            continue
        authors.append(
            {
                "author_id": author.get("authorId"),
                "url": author.get("url"),
                "name": author.get("name"),
                "affiliations": author.get("affiliations") or [],
                "homepage": author.get("homepage"),
                "paper_count": author.get("paperCount"),
                "citation_count": author.get("citationCount"),
                "h_index": author.get("hIndex"),
            }
        )
    return authors


async def search_semantic_scholar_authors(
    query: str, max_results: int = 3, timeout: float = 8.0
) -> list[dict]:
    """Semantic Scholar 作者搜索，用于外部作者/引用信息查询。"""
    query = query.strip()
    if not query:
        return []

    limit = min(max(max_results * 4, 5), 10)
    results: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=timeout) as client:
        for search_query in _author_search_queries(query):
            for author in await _fetch_semantic_scholar_authors(client, search_query, limit):
                key = str(author.get("author_id") or author.get("name") or "")
                if key and key not in results:
                    results[key] = author

    return _rank_author_results(query, list(results.values()))[:max_results]


def _s2_paper_to_candidate(paper: dict, source: str = "s2") -> PaperCandidate | None:
    """把 S2 paper 对象转成 PaperCandidate。"""
    if not paper.get("title"):
        return None
    external = paper.get("externalIds") or {}
    arxiv_id = external.get("ArXiv") or ""
    paper_id = paper.get("paperId", "")

    authors = [a.get("name", "") for a in paper.get("authors", []) if a.get("name")]
    year = paper.get("year")
    url = (
        f"https://arxiv.org/abs/{arxiv_id}"
        if arxiv_id
        else f"https://www.semanticscholar.org/paper/{paper.get('paperId', '')}"
    )

    # openAccessPdf
    oap = paper.get("openAccessPdf") or {}
    pdf_url = oap.get("url") if isinstance(oap, dict) else None

    return PaperCandidate(
        arxiv_id=arxiv_id,
        title=paper.get("title", ""),
        authors=authors,
        abstract=paper.get("abstract", "") or "",
        year=str(year) if year else None,
        url=url,
        source=source,
        citation_count=paper.get("citationCount"),
        venue=paper.get("venue") or None,
        pdf_url=pdf_url,
        paper_id=paper_id,
        extractable=bool(arxiv_id),
    )
