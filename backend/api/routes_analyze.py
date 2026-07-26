"""分析路由 — POST /analyze/{id} 触发四 Agent，GET /analyze/{id} 取结果。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..agent.orchestrator import analyze_paper
from ..storage.db import try_create_agent_task, update_agent_task, update_status
from ..storage.files import load_analysis, load_document, save_analysis

router = APIRouter(tags=["analyze"])
logger = logging.getLogger(__name__)


class AnalyzeResponse(BaseModel):
    arxiv_id: str
    summary: str = ""
    reproducibility: dict | None = None
    improvements: list[str] = []
    highlights: list[str] = []


@router.post("/analyze/{arxiv_id}", response_model=AnalyzeResponse)
async def run_analysis(
    arxiv_id: str,
    force: bool = Query(default=False),
) -> AnalyzeResponse:
    """触发四 Agent 并行分析，结果写入 analysis.json。"""
    doc = load_document(arxiv_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"论文未找到: {arxiv_id}")

    cached = load_analysis(arxiv_id)
    if cached is not None and not force:
        cached.setdefault("arxiv_id", arxiv_id)
        return AnalyzeResponse(**cached)

    task_id, created = await try_create_agent_task(
        arxiv_id=arxiv_id,
        task_type="four_agent_analysis",
        summary="Agent 深度分析",
    )
    if not created:
        raise HTTPException(
            status_code=409,
            detail="同一论文的 Agent 分析正在运行，请稍后刷新结果。",
        )
    await update_status(arxiv_id, "analyzing")
    try:
        result = await analyze_paper(doc.blocks)
    except Exception as e:
        logger.exception("分析失败: %s", e)
        await update_status(arxiv_id, "translated")  # 回退状态
        await update_agent_task(task_id, "error", "Agent 深度分析失败", str(e))
        raise HTTPException(status_code=500, detail=f"分析失败: {e}") from e

    # 持久化
    data = result.model_dump()
    save_analysis(arxiv_id, data)
    await update_status(arxiv_id, "analyzed")
    await update_agent_task(task_id, "done", "Agent 深度分析完成")

    return AnalyzeResponse(arxiv_id=arxiv_id, **data)


@router.get("/analyze/{arxiv_id}", response_model=AnalyzeResponse)
async def get_analysis(arxiv_id: str) -> AnalyzeResponse:
    """取已存的分析结果。"""
    data = load_analysis(arxiv_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"分析结果未找到: {arxiv_id}（请先 POST 触发分析）")
    data.setdefault("arxiv_id", arxiv_id)
    return AnalyzeResponse(**data)
