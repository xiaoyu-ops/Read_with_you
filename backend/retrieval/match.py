"""结果合并与排序（参考开源项目最佳实践）。

核心改变（对比旧版）：
- **删除 rapidfuzz threshold 门控**——不再丢弃源返回的结果
- 排序：citationCount 降序为主，有 matchScore 的优先，rapidfuzz 仅作同分 tie-breaker + 展示
- 去重：normalized arXiv ID / DOI 优先，回退 normalized title
- 保留 rapidfuzz 计算一个 similarity 分用于**展示**（不用于过滤）。
  similarity 是标题整体相似度；短查询只命中标题中的一个词不会显示 100%。

参考：arxiv.py / paper-search-pro / paper-search-mcp 都不用本地 fuzzy threshold 做检索门控，
依赖源的原生排序（arXiv sortBy=relevance，S2 citationCount + matchScore）。
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz

from .arxiv import PaperCandidate


def _normalize_title(title: str) -> str:
    """标准化标题用于去重：小写 + 去标点 + 去多余空白。"""
    title = title.lower()
    title = re.sub(r"[^\w\s]", " ", title)
    return " ".join(title.split())


def _normalize_arxiv_id(arxiv_id: str) -> str:
    """标准化 arXiv ID 用于去重。"""
    return arxiv_id.strip().lower()


def _title_similarity(query: str, title: str) -> int:
    """标题整体相似度，用于展示和同分排序。

    不用 partial_ratio：短查询如 "attention" 只要作为子串出现就会是 100%，
    对用户来说更像"命中度"而不是"标题相似度"。
    """
    return int(round(fuzz.QRatio(query, title)))


def merge_and_rank(
    query: str,
    arxiv_results: list[PaperCandidate],
    s2_results: list[PaperCandidate],
) -> list[PaperCandidate]:
    """合并双源结果，去重，排序。

    不做 threshold 过滤——源返回的结果都保留。
    排序优先级：matchScore > citationCount > similarity（tie-breaker）。
    """
    query_norm = _normalize_title(query)

    # 去重 + 合并：arXiv 源先入（保留 arXiv 的 PDF 链接），S2 补充字段
    by_id: dict[str, PaperCandidate] = {}
    by_title: dict[str, PaperCandidate] = {}

    for cand in list(arxiv_results) + list(s2_results):
        if not cand.title:
            continue

        # ID 去重
        id_key = _normalize_arxiv_id(cand.arxiv_id) if cand.arxiv_id else ""
        title_key = _normalize_title(cand.title)

        existing = None
        if id_key and id_key in by_id:
            existing = by_id[id_key]
        elif title_key in by_title:
            existing = by_title[title_key]

        if existing:
            # 合并字段：S2 的 citationCount/venue/match_score 补到 arXiv 结果上
            if cand.citation_count is not None and existing.citation_count is None:
                existing.citation_count = cand.citation_count
            if cand.venue is not None and existing.venue is None:
                existing.venue = cand.venue
            if cand.match_score is not None and existing.match_score is None:
                existing.match_score = cand.match_score
            if cand.pdf_url is not None and existing.pdf_url is None:
                existing.pdf_url = cand.pdf_url
            if cand.paper_id is not None and existing.paper_id is None:
                existing.paper_id = cand.paper_id
            # S2 abstract 可能比 arXiv 的更干净
            if cand.abstract and len(cand.abstract) > len(existing.abstract or ""):
                existing.abstract = cand.abstract
            continue

        # 新条目
        if id_key:
            by_id[id_key] = cand
        by_title[title_key] = cand

    all_results: list[PaperCandidate] = []
    seen_objects: set[int] = set()
    for cand in list(by_id.values()) + list(by_title.values()):
        object_id = id(cand)
        if object_id in seen_objects:
            continue
        seen_objects.add(object_id)
        all_results.append(cand)

    # 计算 similarity（仅展示，不门控）
    for cand in all_results:
        cand.similarity = _title_similarity(query_norm, _normalize_title(cand.title))

    # 排序：
    # 1. MVP 优先可提取的 arXiv 候选
    # 2. 有 match_score 的排前（S2 /match 认为最可能是用户要找的）
    # 3. citation_count 降序（高引用 = 高价值，用户最想要的信号）
    # 4. similarity 降序（tie-breaker）
    def sort_key(c: PaperCandidate) -> tuple:
        has_match = c.match_score is not None
        match_score = c.match_score or 0
        citations = c.citation_count or 0
        similarity = c.similarity or 0
        # 有 match_score 的排前，按 match_score 降序
        # 无 match_score 的按 citation_count 降序
        # 组合排序：(has_match, match_score + citations权重, similarity)
        return (
            c.extractable,
            has_match,
            match_score + citations * 0.001,  # match_score 权重远高于 citations
            citations,
            similarity,
        )

    all_results.sort(key=sort_key, reverse=True)
    return all_results
