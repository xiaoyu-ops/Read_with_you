"""文献库路由 — 专题文件夹 + 论文归档。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..storage.db import (
    add_paper_to_collection,
    create_agent_task,
    create_collection,
    get_collection,
    get_paper,
    list_collections,
    remove_paper_from_collection,
    update_agent_task,
)
from ..storage.files import (
    build_paper_note_summary,
    load_analysis,
    load_annotations,
    load_collection_agent_report,
    now_iso,
    save_collection_agent_report,
)

router = APIRouter(prefix="/collections", tags=["collections"])


class CreateCollectionRequest(BaseModel):
    name: str


class AddPaperRequest(BaseModel):
    arxiv_id: str


class CollectionSummary(BaseModel):
    id: int
    name: str
    paper_count: int = 0
    contains_paper: bool = False
    created_at: str | None = None
    updated_at: str | None = None


class CollectionPaper(BaseModel):
    arxiv_id: str
    title: str
    authors: list[str] = []
    source: str = ""
    status: str = ""
    created_at: str | None = None
    added_at: str | None = None
    selection_note_count: int = 0
    has_paper_note: bool = False
    note_updated_at: str | None = None
    note_preview: str = ""
    note_kind_counts: dict[str, int] = Field(default_factory=dict)


class CollectionDetail(BaseModel):
    id: int
    name: str
    created_at: str | None = None
    updated_at: str | None = None
    papers: list[CollectionPaper] = []


class CollectionAgentPaper(BaseModel):
    arxiv_id: str
    title: str
    status: str = ""
    has_analysis: bool = False
    annotation_count: int = 0
    selection_note_count: int = 0
    has_paper_note: bool = False
    note_updated_at: str | None = None
    note_preview: str = ""
    summary: str = ""
    reproducibility_verdict: str = ""
    reproducibility_confidence: str = ""
    improvements: list[str] = []
    highlights: list[str] = []


class CollectionAgentReport(BaseModel):
    collection_id: int
    collection_name: str
    generated_at: str
    paper_count: int
    analyzed_count: int
    annotated_count: int
    missing_analysis: list[str] = []
    papers: list[CollectionAgentPaper] = []
    synthesis: list[str] = []


def _summary(row: dict) -> CollectionSummary:
    return CollectionSummary(
        id=row["id"],
        name=row["name"],
        paper_count=row.get("paper_count", 0),
        contains_paper=bool(row.get("contains_paper", False)),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _build_collection_agent_report(data: dict) -> dict:
    papers: list[dict] = []
    missing_analysis: list[str] = []
    analyzed_count = 0
    annotated_count = 0

    for paper in data.get("papers", []):
        arxiv_id = paper["arxiv_id"]
        analysis = load_analysis(arxiv_id)
        annotations = load_annotations(arxiv_id)
        note_summary = build_paper_note_summary(arxiv_id)
        if analysis:
            analyzed_count += 1
        else:
            missing_analysis.append(arxiv_id)
        if annotations or note_summary["has_paper_note"]:
            annotated_count += 1

        reproducibility = analysis.get("reproducibility") if analysis else None
        papers.append(
            {
                "arxiv_id": arxiv_id,
                "title": paper["title"],
                "status": paper.get("status", ""),
                "has_analysis": bool(analysis),
                "annotation_count": len(annotations),
                "selection_note_count": note_summary["selection_note_count"],
                "has_paper_note": note_summary["has_paper_note"],
                "note_updated_at": note_summary["updated_at"],
                "note_preview": note_summary["preview"],
                "summary": (analysis or {}).get("summary", ""),
                "reproducibility_verdict": (reproducibility or {}).get("verdict", ""),
                "reproducibility_confidence": (reproducibility or {}).get("confidence", ""),
                "improvements": (analysis or {}).get("improvements", [])[:3],
                "highlights": (analysis or {}).get("highlights", [])[:3],
            }
        )

    synthesis = [
        f"专题共 {len(papers)} 篇论文，其中 {analyzed_count} 篇已有单篇 Agent 分析。",
        f"{annotated_count} 篇论文包含“你的笔记”或语义高亮；这些内容与论文分析结果分开呈现。",
    ]
    if missing_analysis:
        synthesis.append(f"{len(missing_analysis)} 篇论文尚未运行单篇分析，建议先补齐。")

    return {
        "collection_id": data["id"],
        "collection_name": data["name"],
        "generated_at": now_iso(),
        "paper_count": len(papers),
        "analyzed_count": analyzed_count,
        "annotated_count": annotated_count,
        "missing_analysis": missing_analysis,
        "papers": papers,
        "synthesis": synthesis,
    }


def _refresh_collection_report_note_fields(
    report: dict,
    collection: dict | None = None,
) -> dict:
    """兼容旧缓存，同时以当前成员和权威文件刷新报告派生字段。"""
    if collection is not None:
        refreshed = _build_collection_agent_report(collection)
        refreshed["generated_at"] = report.get("generated_at") or refreshed["generated_at"]
        return refreshed

    refreshed = dict(report)
    papers: list[dict] = []
    annotated_count = 0
    for raw_paper in report.get("papers", []):
        paper = dict(raw_paper)
        arxiv_id = str(paper.get("arxiv_id") or "")
        note_summary = build_paper_note_summary(arxiv_id)
        annotations = load_annotations(arxiv_id)
        paper.update(
            {
                "annotation_count": len(annotations),
                "selection_note_count": note_summary["selection_note_count"],
                "has_paper_note": note_summary["has_paper_note"],
                "note_updated_at": note_summary["updated_at"],
                "note_preview": note_summary["preview"],
            }
        )
        if annotations or note_summary["has_paper_note"]:
            annotated_count += 1
        papers.append(paper)
    synthesis = [
        item
        for item in report.get("synthesis", [])
        if "篇论文包含用户标注" not in str(item)
    ]
    synthesis.insert(
        min(1, len(synthesis)),
        f"{annotated_count} 篇论文包含“你的笔记”或语义高亮；这些内容与论文分析结果分开呈现。",
    )
    refreshed["papers"] = papers
    refreshed["annotated_count"] = annotated_count
    refreshed["synthesis"] = synthesis
    return refreshed


@router.get("", response_model=list[CollectionSummary])
async def get_collections(
    arxiv_id: str | None = Query(default=None),
) -> list[CollectionSummary]:
    """列出所有专题。传 arxiv_id 时标记该论文已加入哪些专题。"""
    rows = await list_collections(arxiv_id=arxiv_id)
    return [_summary(r) for r in rows]


@router.post("", response_model=CollectionSummary)
async def create_collection_route(req: CreateCollectionRequest) -> CollectionSummary:
    """创建专题；同名专题返回已有记录。"""
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="专题名称不能为空")
    row = await create_collection(name)
    row["paper_count"] = 0
    row["contains_paper"] = False
    return _summary(row)


@router.get("/{collection_id}", response_model=CollectionDetail)
async def get_collection_route(collection_id: int) -> CollectionDetail:
    data = await get_collection(collection_id)
    if data is None:
        raise HTTPException(status_code=404, detail="专题不存在")
    papers = data.get("papers", [])
    note_summaries = await asyncio.gather(
        *[
            asyncio.to_thread(build_paper_note_summary, paper["arxiv_id"])
            for paper in papers
        ]
    )
    return CollectionDetail(
        id=data["id"],
        name=data["name"],
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
        papers=[
            CollectionPaper(
                arxiv_id=p["arxiv_id"],
                title=p["title"],
                authors=p.get("authors", []),
                source=p.get("source", ""),
                status=p.get("status", ""),
                created_at=p.get("created_at"),
                added_at=p.get("added_at"),
                selection_note_count=note_summary["selection_note_count"],
                has_paper_note=note_summary["has_paper_note"],
                note_updated_at=note_summary["updated_at"],
                note_preview=note_summary["preview"],
                note_kind_counts=note_summary["kind_counts"],
            )
            for p, note_summary in zip(papers, note_summaries, strict=True)
        ],
    )


@router.post("/{collection_id}/papers", response_model=CollectionDetail)
async def add_paper_route(
    collection_id: int,
    req: AddPaperRequest,
) -> CollectionDetail:
    """把已提取论文加入专题。"""
    collection = await get_collection(collection_id)
    if collection is None:
        raise HTTPException(status_code=404, detail="专题不存在")
    paper = await get_paper(req.arxiv_id)
    if paper is None:
        raise HTTPException(status_code=404, detail="论文不存在，请先检索并打开该论文")

    await add_paper_to_collection(collection_id, req.arxiv_id)
    return await get_collection_route(collection_id)


@router.delete("/{collection_id}/papers/{arxiv_id}", response_model=CollectionDetail)
async def remove_paper_route(collection_id: int, arxiv_id: str) -> CollectionDetail:
    collection = await get_collection(collection_id)
    if collection is None:
        raise HTTPException(status_code=404, detail="专题不存在")
    await remove_paper_from_collection(collection_id, arxiv_id)
    return await get_collection_route(collection_id)


@router.get("/{collection_id}/agent-report", response_model=CollectionAgentReport)
async def get_collection_agent_report_route(collection_id: int) -> CollectionAgentReport:
    collection = await get_collection(collection_id)
    if collection is None:
        raise HTTPException(status_code=404, detail="专题不存在")
    report = load_collection_agent_report(collection_id)
    if report is None:
        raise HTTPException(status_code=404, detail="专题 Agent 报告不存在")
    return CollectionAgentReport(
        **_refresh_collection_report_note_fields(report, collection)
    )


@router.post("/{collection_id}/agent-report", response_model=CollectionAgentReport)
async def run_collection_agent_report_route(collection_id: int) -> CollectionAgentReport:
    collection = await get_collection(collection_id)
    if collection is None:
        raise HTTPException(status_code=404, detail="专题不存在")

    task_id = await create_agent_task(
        arxiv_id=f"collection:{collection_id}",
        collection_id=collection_id,
        task_type="collection_cross_review",
        summary=f"专题横向整理：{collection['name']}",
    )
    try:
        report = _build_collection_agent_report(collection)
        save_collection_agent_report(collection_id, report)
        await update_agent_task(task_id, "done", f"专题横向整理完成：{collection['name']}")
        return CollectionAgentReport(**report)
    except Exception as e:
        await update_agent_task(task_id, "error", f"专题横向整理失败：{collection['name']}", str(e))
        raise HTTPException(status_code=500, detail=f"专题横向整理失败: {e}") from e
