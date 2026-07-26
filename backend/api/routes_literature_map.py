"""Independent literature-map content route."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..retrieval.literature_map import (
    LITERATURE_MAP_DEFAULT_NODES,
    LITERATURE_MAP_MAX_NODES,
    LiteratureMapError,
    get_literature_map,
)

router = APIRouter(prefix="/literature-map", tags=["literature-map"])


@router.get("/{paper_ref:path}")
async def literature_map(
    paper_ref: str,
    max_nodes: int = Query(
        default=LITERATURE_MAP_DEFAULT_NODES,
        ge=10,
        le=LITERATURE_MAP_MAX_NODES,
    ),
) -> dict:
    try:
        return await get_literature_map(paper_ref, max_nodes=max_nodes)
    except LiteratureMapError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
