"""Per-paper Markdown note routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..storage.files import (
    PaperNoteRevisionConflict,
    load_document,
    load_paper_note,
    save_paper_note,
)
from ..storage.paper_note_index import safe_sync_paper_note_index


router = APIRouter(prefix="/papers/{arxiv_id}/paper-note", tags=["notes"])


class PaperNoteItem(BaseModel):
    arxiv_id: str
    markdown: str
    updated_at: str | None = None
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class PaperNoteUpdateRequest(BaseModel):
    markdown: str = Field(max_length=200_000)
    base_revision: str = Field(pattern=r"^[0-9a-f]{64}$")


def _ensure_paper(arxiv_id: str) -> None:
    if load_document(arxiv_id) is None:
        raise HTTPException(status_code=404, detail=f"论文未找到: {arxiv_id}")


@router.get("", response_model=PaperNoteItem)
async def get_paper_note(arxiv_id: str) -> PaperNoteItem:
    _ensure_paper(arxiv_id)
    return PaperNoteItem(**load_paper_note(arxiv_id))


@router.put("", response_model=PaperNoteItem)
async def put_paper_note(
    arxiv_id: str,
    request: PaperNoteUpdateRequest,
) -> PaperNoteItem:
    _ensure_paper(arxiv_id)
    try:
        item = save_paper_note(
            arxiv_id,
            request.markdown,
            request.base_revision,
        )
    except PaperNoteRevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "paper_note_revision_conflict",
                "message": "论文笔记已在其他页面更新，请先重新载入。",
                "current_revision": exc.current_revision,
            },
        ) from exc
    await safe_sync_paper_note_index(arxiv_id)
    return PaperNoteItem(**item)
