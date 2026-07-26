"""Agent 任务中心路由 — 执行历史 / 状态。"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from ..storage.db import list_agent_tasks

router = APIRouter(prefix="/agent/tasks", tags=["agent-tasks"])


class AgentTaskItem(BaseModel):
    id: int
    arxiv_id: str
    collection_id: int | None = None
    collection_name: str | None = None
    paper_title: str | None = None
    task_type: str
    status: str
    summary: str = ""
    error: str = ""
    created_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None


@router.get("", response_model=list[AgentTaskItem])
async def get_agent_tasks(
    limit: int = Query(default=50, ge=1, le=200),
) -> list[AgentTaskItem]:
    rows = await list_agent_tasks(limit=limit)
    return [AgentTaskItem(**row) for row in rows]
