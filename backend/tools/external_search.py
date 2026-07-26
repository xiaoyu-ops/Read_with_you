"""External paper search tool backed by the project retrieval pipeline."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from ..retrieval.arxiv import PaperCandidate, search_arxiv
from ..retrieval.match import merge_and_rank
from ..retrieval.semantic_scholar import (
    search_s2_combined,
    search_semantic_scholar_authors,
)
from .registry import ToolCall, ToolRegistry, ToolResult, ToolSpec

logger = logging.getLogger(__name__)

DEFAULT_MAX_RESULTS = 5
MAX_RESULTS_LIMIT = 10


async def _safe_arxiv(query: str, max_results: int) -> list[PaperCandidate]:
    try:
        return await search_arxiv(query, max_results=max_results)
    except Exception as e:
        logger.warning("external_search arXiv failed: %s", e)
        return []


async def _safe_s2(query: str, max_results: int) -> list[PaperCandidate]:
    try:
        return await search_s2_combined(query, max_results=max_results)
    except Exception as e:
        logger.warning("external_search Semantic Scholar failed: %s", e)
        return []


async def _safe_s2_authors(query: str, max_results: int = 3) -> list[dict]:
    try:
        return await search_semantic_scholar_authors(query, max_results=max_results)
    except Exception as e:
        logger.warning("external_search Semantic Scholar author search failed: %s", e)
        return []


def _max_results(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_MAX_RESULTS
    return max(1, min(parsed, MAX_RESULTS_LIMIT))


def _search_query(call: ToolCall) -> tuple[str, str, str]:
    query = str(call.arguments.get("query") or "").strip()
    arxiv_id = str(call.arguments.get("arxiv_id") or "").strip()
    paper_title = str(call.arguments.get("paper_title") or "").strip()
    explicit_query = str(call.arguments.get("search_query") or "").strip()
    search_query = explicit_query or paper_title or arxiv_id or query
    return search_query, query, arxiv_id


def _query_mode(call: ToolCall) -> str:
    mode = str(call.arguments.get("query_mode") or "paper_lookup").strip()
    return mode if mode else "paper_lookup"


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()


def _is_current_paper(candidate: PaperCandidate, arxiv_id: str, title: str) -> bool:
    if arxiv_id and candidate.arxiv_id and candidate.arxiv_id == arxiv_id:
        return True
    exclude_title = _normalize_title(title)
    candidate_title = _normalize_title(candidate.title)
    return bool(exclude_title and candidate_title and candidate_title == exclude_title)


def _paper_authors(call: ToolCall) -> list[str]:
    authors = call.arguments.get("paper_authors")
    if not isinstance(authors, list):
        return []
    return [str(author).strip() for author in authors if str(author).strip()]


def _paper_metadata(call: ToolCall) -> str:
    return str(call.arguments.get("paper_metadata") or "").strip()[:1600]


def _lookup_targets(call: ToolCall) -> set[str]:
    raw = call.arguments.get("lookup_targets")
    allowed = {"papers", "authors", "citation_metrics"}
    if not isinstance(raw, list):
        return {"papers"}
    targets = {str(item).strip() for item in raw if str(item).strip() in allowed}
    return targets or {"papers"}


def _author_queries(call: ToolCall, paper_authors: list[str]) -> list[str]:
    """Use only the model-selected author scope; never infer it from wording."""
    explicit = call.arguments.get("author_queries")
    if isinstance(explicit, list):
        queries = [str(item).strip() for item in explicit if str(item).strip()]
        if queries:
            return queries[:3]
    scope = str(call.arguments.get("author_scope") or "none").strip()
    if scope == "first_author":
        return paper_authors[:1]
    if scope == "paper_authors":
        return paper_authors[:3]
    return []


def _candidate_evidence(candidate: PaperCandidate, rank: int) -> dict[str, Any]:
    return {
        "kind": "external_paper_search_result",
        "rank": rank,
        "source": candidate.source,
        "title": candidate.title,
        "arxiv_id": candidate.arxiv_id,
        "paper_id": candidate.paper_id,
        "url": candidate.url,
        "pdf_url": candidate.pdf_url,
        "authors": candidate.authors,
        "year": candidate.year,
        "venue": candidate.venue,
        "citation_count": candidate.citation_count,
        "match_score": candidate.match_score,
        "similarity": candidate.similarity,
        "extractable": candidate.extractable,
        "abstract": candidate.abstract[:600] if candidate.abstract else "",
    }


def _candidate_line(candidate: PaperCandidate, rank: int) -> str:
    author_text = ", ".join(candidate.authors[:3])
    suffix = []
    if candidate.year:
        suffix.append(candidate.year)
    if candidate.citation_count is not None:
        suffix.append(f"{candidate.citation_count} citations")
    if candidate.arxiv_id:
        suffix.append(f"arXiv:{candidate.arxiv_id}")
    if candidate.venue:
        suffix.append(candidate.venue)
    meta = f" ({'; '.join(suffix)})" if suffix else ""
    authors = f" — {author_text}" if author_text else ""
    return f"{rank}. {candidate.title}{authors}{meta}"


def _author_evidence(author: dict, query: str, rank: int) -> dict[str, Any]:
    return {
        "kind": "semantic_scholar_author_result",
        "rank": rank,
        "queried_author": query,
        "source": "semantic_scholar",
        "author_id": author.get("author_id"),
        "name": author.get("name"),
        "url": author.get("url"),
        "homepage": author.get("homepage"),
        "aliases": author.get("aliases") or [],
        "affiliations": author.get("affiliations") or [],
        "paper_count": author.get("paper_count"),
        "citation_count": author.get("citation_count"),
        "h_index": author.get("h_index"),
    }


def _author_line(author: dict, query: str, rank: int) -> str:
    suffix = []
    affiliations = author.get("affiliations") or []
    if affiliations:
        suffix.append("affiliations: " + ", ".join(str(item) for item in affiliations[:3]))
    if author.get("citation_count") is not None:
        suffix.append(f"{author['citation_count']} citations")
    if author.get("h_index") is not None:
        suffix.append(f"h-index {author['h_index']}")
    if author.get("paper_count") is not None:
        suffix.append(f"{author['paper_count']} papers")
    meta = f" ({'; '.join(suffix)})" if suffix else ""
    return f"{rank}. {author.get('name', query)}{meta}"


async def external_paper_search(call: ToolCall) -> ToolResult:
    search_query, user_query, arxiv_id = _search_query(call)
    query_mode = _query_mode(call)
    max_results = _max_results(call.arguments.get("max_results"))
    exclude_title = str(call.arguments.get("exclude_title") or call.arguments.get("paper_title") or "")
    exclude_arxiv_id = str(call.arguments.get("exclude_arxiv_id") or arxiv_id)
    authors = _paper_authors(call)
    paper_metadata = _paper_metadata(call)
    lookup_targets = _lookup_targets(call)
    author_queries = (
        _author_queries(call, authors)
        if lookup_targets & {"authors", "citation_metrics"}
        else []
    )
    if not search_query:
        return ToolResult(
            name=call.name,
            content="真实外部检索没有收到可用查询内容。",
            evidence=(),
            metadata={"mock": False, "providers": ["arxiv", "semantic_scholar"], "result_count": 0},
        )

    paper_search_enabled = "papers" in lookup_targets or "citation_metrics" in lookup_targets
    arxiv_task = asyncio.create_task(_safe_arxiv(search_query, max_results)) if paper_search_enabled else None
    s2_task = asyncio.create_task(_safe_s2(search_query, max_results)) if paper_search_enabled else None
    author_tasks = [
        asyncio.create_task(_safe_s2_authors(author_query, max_results=3))
        for author_query in author_queries
    ]
    arxiv_results, s2_results, author_batches = await asyncio.gather(
        arxiv_task if arxiv_task is not None else asyncio.sleep(0, result=[]),
        s2_task if s2_task is not None else asyncio.sleep(0, result=[]),
        asyncio.gather(*author_tasks) if author_tasks else asyncio.sleep(0, result=[]),
    )
    ranked = merge_and_rank(search_query, arxiv_results, s2_results)
    excluded_current_paper_count = 0
    if query_mode == "related_papers":
        before = len(ranked)
        ranked = [
            candidate
            for candidate in ranked
            if not _is_current_paper(candidate, exclude_arxiv_id, exclude_title)
        ]
        excluded_current_paper_count = before - len(ranked)
    ranked = ranked[:max_results]
    author_results = [
        (author_query, author)
        for author_query, batch in zip(author_queries, author_batches)
        for author in batch[:3]
    ]

    metadata = {
        "mock": False,
        "providers": ["arxiv", "semantic_scholar"],
        "query": user_query,
        "query_mode": query_mode,
        "lookup_targets": sorted(lookup_targets),
        "author_scope": str(call.arguments.get("author_scope") or "none"),
        "search_query": search_query,
        "arxiv_id": arxiv_id,
        "result_count": len(ranked),
        "excluded_current_paper_count": excluded_current_paper_count,
        "arxiv_result_count": len(arxiv_results),
        "semantic_scholar_result_count": len(s2_results),
        "semantic_scholar_author_result_count": len(author_results),
    }
    if not ranked and not author_results:
        if query_mode == "related_papers" and excluded_current_paper_count:
            return ToolResult(
                name=call.name,
                content=(
                    "真实外部检索已完成，但排除当前论文后，没有找到其他可用的相似论文。"
                    f"检索词：{search_query}"
                ),
                evidence=(),
                metadata=metadata,
            )
        return ToolResult(
            name=call.name,
            content=(
                "真实外部检索已完成，但 arXiv / Semantic Scholar 没有返回可用结果。"
                f"检索词：{search_query}"
            ),
            evidence=(),
            metadata=metadata,
        )

    sections = []
    evidence: list[dict[str, Any]] = []
    if ranked:
        lines = [
            _candidate_line(candidate, index)
            for index, candidate in enumerate(ranked, start=1)
        ]
        sections.append("论文信息（arXiv + Semantic Scholar）：\n" + "\n".join(lines))
        evidence.extend(
            _candidate_evidence(candidate, index)
            for index, candidate in enumerate(ranked, start=1)
        )
    if author_results:
        author_lines = [
            _author_line(author, query, index)
            for index, (query, author) in enumerate(author_results, start=1)
        ]
        sections.append("作者信息（Semantic Scholar）：\n" + "\n".join(author_lines))
        evidence.extend(
            _author_evidence(author, query, index)
            for index, (query, author) in enumerate(author_results, start=1)
        )
    if paper_metadata and author_queries:
        sections.append("本地论文元数据线索：\n" + paper_metadata)
        evidence.append(
            {
                "kind": "local_paper_metadata_context",
                "source": "local_paper_pdf_front_page",
                "content": paper_metadata,
            }
        )
    if "citation_metrics" in lookup_targets:
        sections.append("说明：引用与作者指标来自 Semantic Scholar Graph API，不是 Google Scholar 的直接数据。")

    return ToolResult(
        name=call.name,
        content="真实外部检索结果：\n" + "\n\n".join(sections),
        evidence=tuple(evidence),
        metadata=metadata,
    )


def register_external_search_tool(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="local.external_search",
            description="真实论文外部检索工具，调用 arXiv 与 Semantic Scholar 并返回候选证据。",
            permission_scope="external_search",
            source="local",
        ),
        external_paper_search,
    )
