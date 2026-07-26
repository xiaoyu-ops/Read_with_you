"""General web search tool for Agent external lookup."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from .registry import ToolCall, ToolRegistry, ToolResult, ToolSpec

DEFAULT_MAX_RESULTS = 5
MAX_RESULTS_LIMIT = 10
SEARCH_URL = "https://duckduckgo.com/html/"
MAX_FETCH_CHARS = 4000
URL_RE = re.compile(r"https?://[^\s<>)\"']+")


def _max_results(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_MAX_RESULTS
    return max(1, min(parsed, MAX_RESULTS_LIMIT))


def _search_query(call: ToolCall) -> str:
    query = str(call.arguments.get("query") or "").strip()
    paper_title = str(call.arguments.get("paper_title") or "").strip()
    if paper_title and any(
        keyword in query.lower()
        for keyword in ("这篇", "this paper", "paper", "论文", "相关", "代码", "复现")
    ):
        return f"{paper_title} {query}".strip()
    return query or paper_title


def _clean_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            return unquote(target)
    return url


def _result_source(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def _parse_results(html: str, max_results: int) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict[str, str]] = []
    for node in soup.select(".result"):
        link = node.select_one(".result__a")
        if link is None:
            continue
        title = link.get_text(" ", strip=True)
        href = str(link.get("href") or "").strip()
        if not title or not href:
            continue
        url = _clean_url(href)
        snippet_node = node.select_one(".result__snippet")
        snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""
        results.append(
            {
                "title": title,
                "url": url,
                "source": _result_source(url),
                "snippet": snippet,
            }
        )
        if len(results) >= max_results:
            break
    return results


def _result_line(result: dict[str, str], rank: int) -> str:
    snippet = f" — {result['snippet']}" if result.get("snippet") else ""
    return f"{rank}. {result['title']} ({result['source']})\n   {result['url']}{snippet}"


def _url_from_call(call: ToolCall) -> str:
    direct_url = str(call.arguments.get("url") or "").strip()
    if direct_url:
        return direct_url
    query = str(call.arguments.get("query") or "").strip()
    match = URL_RE.search(query)
    return match.group(0).rstrip(".,，。") if match else ""


def _validate_fetch_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("只支持 http/https URL")
    return url


def _page_description(soup: BeautifulSoup) -> str:
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        return str(meta["content"]).strip()
    og = soup.find("meta", attrs={"property": "og:description"})
    if og and og.get("content"):
        return str(og["content"]).strip()
    return ""


def _page_text(soup: BeautifulSoup) -> str:
    for node in soup.select("script, style, noscript, nav, header, footer, aside"):
        node.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    lines = [
        line.strip()
        for line in main.get_text("\n", strip=True).splitlines()
        if line.strip()
    ]
    text = "\n".join(lines)
    return text[:MAX_FETCH_CHARS]


async def web_search(call: ToolCall) -> ToolResult:
    query = _search_query(call)
    max_results = _max_results(call.arguments.get("max_results"))
    if not query:
        return ToolResult(
            name=call.name,
            content="通用网页搜索没有收到可用查询内容。",
            evidence=(),
            metadata={"mock": False, "provider": "duckduckgo_html", "result_count": 0},
        )

    async with httpx.AsyncClient(
        timeout=12.0,
        headers={"User-Agent": "PeiNiDu/0.1 (+https://localhost)"},
        follow_redirects=True,
    ) as client:
        response = await client.get(SEARCH_URL, params={"q": query})
        response.raise_for_status()

    results = _parse_results(response.text, max_results)
    metadata = {
        "mock": False,
        "provider": "duckduckgo_html",
        "query": query,
        "result_count": len(results),
    }
    if not results:
        return ToolResult(
            name=call.name,
            content=f"通用网页搜索已完成，但没有返回可用结果。搜索词：{query}",
            evidence=(),
            metadata=metadata,
        )

    evidence = tuple(
        {
            "kind": "web_search_result",
            "rank": index,
            "provider": "duckduckgo_html",
            **result,
        }
        for index, result in enumerate(results, start=1)
    )
    lines = [_result_line(result, index) for index, result in enumerate(results, start=1)]
    return ToolResult(
        name=call.name,
        content="通用网页搜索结果：\n" + "\n".join(lines),
        evidence=evidence,
        metadata=metadata,
    )


async def web_fetch(call: ToolCall) -> ToolResult:
    raw_url = _url_from_call(call)
    if not raw_url:
        return ToolResult(
            name=call.name,
            content="网页读取没有收到可用 URL。",
            evidence=(),
            metadata={"mock": False, "provider": "http_fetch", "result_count": 0},
        )
    try:
        url = _validate_fetch_url(raw_url)
    except ValueError as e:
        return ToolResult(
            name=call.name,
            content=f"网页读取失败：{e}",
            evidence=(),
            metadata={"mock": False, "provider": "http_fetch", "url": raw_url, "error": str(e)},
        )

    async with httpx.AsyncClient(
        timeout=12.0,
        headers={"User-Agent": "PeiNiDu/0.1 (+https://localhost)"},
        follow_redirects=True,
    ) as client:
        response = await client.get(url)
        response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    description = _page_description(soup)
    text = _page_text(soup)
    final_url = str(response.url)
    source = _result_source(final_url)
    evidence = (
        {
            "kind": "web_fetch_result",
            "provider": "http_fetch",
            "url": final_url,
            "source": source,
            "title": title,
            "description": description,
            "text_excerpt": text,
        },
    )
    metadata = {
        "mock": False,
        "provider": "http_fetch",
        "url": final_url,
        "source": source,
        "result_count": 1 if text or title or description else 0,
        "content_length": len(response.text),
    }
    if not (text or title or description):
        return ToolResult(
            name=call.name,
            content=f"网页读取完成，但没有抽取到可用正文。URL：{final_url}",
            evidence=evidence,
            metadata=metadata,
        )
    parts = []
    if title:
        parts.append(f"标题：{title}")
    if description:
        parts.append(f"摘要：{description}")
    if text:
        parts.append("正文摘录：\n" + text)
    return ToolResult(
        name=call.name,
        content=f"网页读取结果（{source}）：\n" + "\n\n".join(parts),
        evidence=evidence,
        metadata=metadata,
    )


def register_web_search_tool(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="local.web_search",
            description="通用网页搜索工具，适合最新资料、官网、网页、博客、新闻和非学术库信息检索。",
            permission_scope="external_search",
            source="local",
        ),
        web_search,
    )
    registry.register(
        ToolSpec(
            name="local.web_fetch",
            description="网页读取工具，适合打开 URL、读取网页正文、总结网页内容。",
            permission_scope="external_search",
            source="local",
        ),
        web_fetch,
    )
