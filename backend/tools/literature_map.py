"""Compact Agent tool view over the shared Semantic Scholar literature map."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ..retrieval.literature_map import (
    LiteratureMapError,
    get_literature_map,
    normalize_paper_ref,
)
from .registry import ToolCall, ToolRegistry, ToolResult, ToolSpec


def _paper_ref(call: ToolCall) -> str:
    explicit = str(call.arguments.get("paper_ref") or "").strip()
    if explicit:
        return normalize_paper_ref(explicit)
    arxiv_id = str(call.arguments.get("arxiv_id") or "").strip()
    if arxiv_id:
        return normalize_paper_ref(f"ARXIV:{arxiv_id}")
    raise LiteratureMapError(
        "paper_ref_missing",
        "论文图谱工具缺少当前论文标识。",
        400,
    )


def _paper_line(paper: dict[str, Any], index: int) -> str:
    year = paper.get("year") or "年份未知"
    citations = paper.get("citation_count")
    citation_text = f"{citations} 次引用" if isinstance(citations, int) else "引用数未知"
    return f"{index}. {paper.get('title') or '未命名论文'}（{year}，{citation_text}）"


def _work_lines(items: list[dict[str, Any]], count_key: str) -> list[str]:
    lines = []
    for index, item in enumerate(items[:5], start=1):
        paper = item.get("paper") if isinstance(item.get("paper"), dict) else {}
        count = item.get(count_key)
        lines.append(
            f"{index}. {paper.get('title') or '未命名论文'}"
            f"（图内共同依据 {count if isinstance(count, int) else '未知'}）"
        )
    return lines


def _evidence(paper: dict[str, Any], rank: int) -> dict[str, Any]:
    return {
        "kind": "literature_map_representative_paper",
        "source": "semantic_scholar",
        "rank": rank,
        "paper_id": paper.get("id"),
        "arxiv_id": paper.get("arxiv_id"),
        "title": paper.get("title"),
        "authors": list(paper.get("authors") or [])[:5],
        "year": paper.get("year"),
        "citation_count": paper.get("citation_count"),
        "similarity": paper.get("similarity"),
        "url": paper.get("url"),
    }


async def literature_map_tool(call: ToolCall) -> ToolResult:
    try:
        paper_ref = _paper_ref(call)
        graph = await get_literature_map(paper_ref)
    except LiteratureMapError as error:
        return ToolResult(
            name=call.name,
            content=f"论文图谱没有完成：{error}",
            metadata={
                "source": "semantic_scholar_literature_map",
                "error": error.code,
                "result_count": 0,
            },
        )

    origin = graph.get("origin") if isinstance(graph.get("origin"), dict) else {}
    nodes = [item for item in graph.get("nodes", []) if isinstance(item, dict)]
    representatives = [item for item in nodes if item.get("id") != origin.get("id")][:5]
    edges = [item for item in graph.get("edges", []) if isinstance(item, dict)]
    similarity_count = sum(item.get("kind") == "similarity" for item in edges)
    citation_count = sum(item.get("kind") == "citation" for item in edges)
    prior = [item for item in graph.get("prior_works", []) if isinstance(item, dict)]
    derivative = [
        item for item in graph.get("derivative_works", []) if isinstance(item, dict)
    ]
    warnings = [
        str(item).strip()
        for item in graph.get("warnings", [])
        if str(item).strip()
    ][:4]
    href = f"/literature-map/{quote(paper_ref, safe='')}"
    summary = (
        f"已为《{origin.get('title') or '当前论文'}》构建论文图谱："
        f"{len(nodes)} 个节点，{similarity_count} 条相似关系，"
        f"{citation_count} 条有向引用关系。"
    )
    sections = [
        summary,
        "代表论文：\n"
        + ("\n".join(_paper_line(item, index) for index, item in enumerate(representatives, 1))
           or "暂无可用代表论文。"),
        "先行工作：\n"
        + ("\n".join(_work_lines(prior, "graph_citation_count")) or "暂无可证明的先行工作。"),
        "后续工作：\n"
        + ("\n".join(_work_lines(derivative, "graph_reference_count")) or "暂无可证明的后续工作。"),
        f"完整交互图谱：{href}",
    ]
    limits = [
        "这里只向 Agent 提供前 5 篇代表论文和聚合计数；完整节点与边请在图谱页查看。"
    ]
    if graph.get("stale"):
        limits.append("当前使用 7 天内的旧缓存。")
    limits.extend(warnings)
    evidence = tuple(
        _evidence(item, index)
        for index, item in enumerate(representatives, start=1)
    )
    result_data = {
        "summary": summary,
        "evidence": list(evidence),
        "limits": limits[:8],
        "next_questions": [],
        "actions": [
            {
                "kind": "open_literature_map",
                "label": "打开论文图谱",
                "href": href,
            }
        ],
    }
    return ToolResult(
        name=call.name,
        content="\n\n".join(sections),
        evidence=evidence,
        metadata={
            "source": "semantic_scholar_literature_map",
            "provider": graph.get("provider"),
            "paper_ref": paper_ref,
            "result_count": len(nodes),
            "status": graph.get("status"),
            "cached": bool(graph.get("cached")),
            "stale": bool(graph.get("stale")),
            "result_data": result_data,
        },
    )


def register_literature_map_tool(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="local.literature_map",
            description=(
                "为当前论文构建 Semantic Scholar 论文关系图谱；"
                "只用于论文图谱、引用脉络或 Connected Papers 意图。"
            ),
            permission_scope="external_search",
            source="local",
            input_schema={
                "type": "object",
                "properties": {
                    "paper_ref": {
                        "type": "string",
                        "description": "可选 S2 paper ID 或 ARXIV:<id>；默认使用当前论文。",
                    },
                    "reason": {"type": "string"},
                },
            },
        ),
        literature_map_tool,
    )
