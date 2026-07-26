"""arXiv API 检索（D6）。

查询策略（参考 arxiv.py / paper-search-pro 最佳实践）：
- arXiv ID/URL → id_list= 精确命中
- 多词短语(≥2词) → ti:"phrase"（短语子串匹配，不是精确全标题匹配）
- 单词/短查询 → 裸查询（all: 语义，最广召回）
- sortBy=relevance（arXiv Lucene 原生相关性排序）
- 不用本地 fuzzy threshold 做门控（开源项目都不这么做）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx
from bs4 import BeautifulSoup

ARXIV_API = "https://export.arxiv.org/api/query"
_ARXIV_ID_RE = re.compile(r"^(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?$", re.I)


@dataclass
class PaperCandidate:
    """一个候选论文（检索结果项）。"""

    arxiv_id: str
    title: str
    authors: list[str]
    abstract: str
    year: str | None = None
    url: str = ""
    source: str = "arxiv"  # arxiv | s2 | s2_match | merged
    # 扩展字段（citation/venue/match 信号，S2 提供，arXiv 无则 None）
    citation_count: int | None = None
    venue: str | None = None
    match_score: float | None = None  # S2 /match 的服务端匹配分
    similarity: float | None = None  # rapidfuzz 本地相似度（仅展示，不门控）
    pdf_url: str | None = None
    paper_id: str | None = None  # Semantic Scholar paperId（非 arXiv 候选用于展示/追踪）
    extractable: bool = True  # MVP 只支持 arXiv 主路径提取

    def to_dict(self) -> dict:
        return {
            "arxiv_id": self.arxiv_id,
            "title": self.title,
            "authors": self.authors,
            "abstract": self.abstract,
            "year": self.year,
            "url": self.url,
            "source": self.source,
            "citation_count": self.citation_count,
            "venue": self.venue,
            "match_score": self.match_score,
            "similarity": self.similarity,
            "pdf_url": self.pdf_url,
            "paper_id": self.paper_id,
            "extractable": self.extractable,
        }


def _clean_arxiv_id(raw: str) -> str:
    """去掉版本号后缀和多余的 URL 部分，如 2401.12345v2 → 2401.12345。"""
    raw = raw.strip()
    raw = re.sub(r".*arxiv\.org/abs/", "", raw)
    raw = re.sub(r".*arxiv\.org/pdf/", "", raw)
    raw = re.sub(r"\.pdf$", "", raw, flags=re.I)
    return re.sub(r"v\d+$", "", raw)


def _is_arxiv_id_or_url(query: str) -> bool:
    """判断输入是否是 arXiv ID（如 2401.12345）或 arXiv URL。"""
    q = query.strip()
    # arXiv URL
    if "arxiv.org" in q:
        return True
    return bool(_ARXIV_ID_RE.match(q))


def _build_search_query(query: str) -> str:
    """根据输入类型构建 arXiv search_query。

    - ID/URL → id_list（在调用方处理，不走这里）
    - 多词短语(≥2词) → ti:"phrase"（短语子串匹配）
    - 单词 → 裸查询（all: 语义，最广召回）
    """
    q = query.strip()
    words = q.split()
    if len(words) >= 2:
        # 多词：用 ti:"phrase" 做标题短语子串匹配（不是精确全标题）
        # 去掉用户可能加的引号
        phrase = q.replace('"', "")
        return f'search_query=ti:"{phrase}"&sortBy=relevance&sortOrder=descending'
    else:
        # 单词/短查询：裸查询（all: 语义），最广召回
        return f"search_query={q}&sortBy=relevance&sortOrder=descending"


def _extract_year(published: str) -> str | None:
    """从 Atom feed 的 published 日期提取年份。"""
    if published and len(published) >= 4:
        return published[:4]
    return None


async def search_arxiv(
    query: str, max_results: int = 10, timeout: float = 20.0
) -> list[PaperCandidate]:
    """通过 arXiv API 搜索论文。

    query 可以是标题、arXiv ID、或 URL（D7：输入论文标题/ID/URL）。
    """
    # 判断是否是 arXiv ID / URL
    if _is_arxiv_id_or_url(query):
        arxiv_id = _clean_arxiv_id(query)
        url = f"{ARXIV_API}?id_list={arxiv_id}&max_results={max_results}"
    else:
        search_query = _build_search_query(query)
        url = f"{ARXIV_API}?{search_query}&start=0&max_results={max_results}"

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "xml")
    candidates: list[PaperCandidate] = []
    for entry in soup.find_all("entry"):
        entry_id = entry.find("id")
        if not entry_id:
            continue
        arxiv_id = _clean_arxiv_id(entry_id.text)

        title_el = entry.find("title")
        title = title_el.text.strip().replace("\n", " ") if title_el else ""

        authors = [a.find("name").text for a in entry.find_all("author") if a.find("name")]

        summary_el = entry.find("summary")
        abstract = summary_el.text.strip() if summary_el else ""

        published_el = entry.find("published")
        year = _extract_year(published_el.text) if published_el else None

        # arXiv PDF 链接
        pdf_url = None
        for link in entry.find_all("link"):
            if link.get("title") == "pdf":
                pdf_url = link.get("href")
                break

        candidates.append(
            PaperCandidate(
                arxiv_id=arxiv_id,
                title=title,
                authors=authors,
                abstract=abstract,
                year=year,
                url=f"https://arxiv.org/abs/{arxiv_id}",
                source="arxiv",
                pdf_url=pdf_url,
                paper_id=arxiv_id,
                extractable=True,
            )
        )
    return candidates
