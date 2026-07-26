"""用户标注路由 — 阅读页划线 / 高亮 / 批注。"""

from __future__ import annotations

import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from ..extraction.translation_layout import (
    NormalizedBox,
    TranslationLayout,
    source_pdf_sha256,
)
from ..translation.selection import (
    TextItemAnchor,
    TextSelectionQuote,
    map_selection_to_layout,
)

from ..storage.files import (
    ANNOTATION_KIND_COLORS,
    add_annotation,
    delete_annotation,
    load_annotations,
    load_document,
    load_translation_layout,
    paper_dir,
    update_annotation,
)
from ..storage.paper_note_index import safe_sync_paper_note_index

router = APIRouter(prefix="/papers/{arxiv_id}/annotations", tags=["annotations"])
AnnotationKind = Literal["highlight", "important", "question", "method", "conclusion"]


class AnnotationSelectorV1(BaseModel):
    version: Literal[1] = 1
    region_id: str | None = Field(default=None, max_length=256)
    start_offset: int = Field(ge=0, le=1_000_000)
    end_offset: int = Field(ge=1, le=1_000_000)
    occurrence: int = Field(default=0, ge=0, le=100_000)

    @model_validator(mode="after")
    def validate_offsets(self) -> "AnnotationSelectorV1":
        if self.end_offset <= self.start_offset:
            raise ValueError("end_offset must be greater than start_offset")
        return self


class AnnotationSelectorV2(BaseModel):
    version: Literal[2] = 2
    source_pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page: int = Field(ge=1)
    start: TextItemAnchor
    end: TextItemAnchor
    quote: TextSelectionQuote
    rects: list[NormalizedBox] = Field(min_length=1, max_length=100)
    region_id: str | None = Field(default=None, max_length=256)
    layout_confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_anchors(self) -> "AnnotationSelectorV2":
        if (
            self.start.item_index,
            self.start.char_offset,
        ) >= (
            self.end.item_index,
            self.end.char_offset,
        ):
            raise ValueError("selection anchors must define a positive forward range")
        return self


AnnotationSelector = Annotated[
    AnnotationSelectorV1 | AnnotationSelectorV2,
    Field(discriminator="version"),
]


class AnnotationCreateRequest(BaseModel):
    block_index: int
    side: Literal["original", "translation"]
    text: str = Field(min_length=1)
    note: str = Field(default="", max_length=8_000)
    color: str = "yellow"
    kind: AnnotationKind = "highlight"
    selector: AnnotationSelector | None = None


class AnnotationUpdateRequest(BaseModel):
    note: str | None = Field(default=None, max_length=8_000)
    kind: AnnotationKind | None = None

    @model_validator(mode="after")
    def require_change(self) -> "AnnotationUpdateRequest":
        if self.note is None and self.kind is None:
            raise ValueError("note or kind is required")
        return self


class AnnotationItem(BaseModel):
    id: str
    arxiv_id: str
    block_index: int
    side: Literal["original", "translation"]
    text: str
    note: str = ""
    color: str = "yellow"
    kind: AnnotationKind = "highlight"
    created_at: str
    updated_at: str = ""
    selector: AnnotationSelector | None = None


def _ensure_paper(arxiv_id: str) -> None:
    if load_document(arxiv_id) is None:
        raise HTTPException(status_code=404, detail=f"论文未找到: {arxiv_id}")


@router.get("", response_model=list[AnnotationItem])
async def get_annotations(arxiv_id: str) -> list[AnnotationItem]:
    _ensure_paper(arxiv_id)
    return [AnnotationItem(**item) for item in load_annotations(arxiv_id)]


@router.post("", response_model=AnnotationItem)
async def create_annotation(
    arxiv_id: str,
    req: AnnotationCreateRequest,
) -> AnnotationItem:
    _ensure_paper(arxiv_id)
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="划线文本不能为空")
    text = req.text
    if isinstance(req.selector, AnnotationSelectorV2):
        await _validate_pdf_text_selector(arxiv_id, req, req.selector)
    else:
        text = text.strip()
    item = add_annotation(
        arxiv_id=arxiv_id,
        block_index=req.block_index,
        side=req.side,
        text=text,
        note=req.note.strip(),
        color=req.color if req.kind == "highlight" else ANNOTATION_KIND_COLORS[req.kind],
        kind=req.kind,
        selector=req.selector.model_dump() if req.selector else None,
    )
    await safe_sync_paper_note_index(arxiv_id)
    return AnnotationItem(**item)


async def _validate_pdf_text_selector(
    arxiv_id: str,
    request: AnnotationCreateRequest,
    selector: AnnotationSelectorV2,
) -> None:
    if request.side != "original":
        raise HTTPException(status_code=422, detail="PDF TextLayer selector 只适用于原文标注")
    if selector.quote.exact != request.text:
        raise HTTPException(status_code=422, detail="标注文本与 PDF 选区原文不一致")

    pdf_path = paper_dir(arxiv_id) / "original.pdf"
    if not pdf_path.is_file() or pdf_path.stat().st_size <= 0:
        raise HTTPException(status_code=409, detail="原始 PDF 不存在，请重新导入论文")
    current_pdf_hash = await asyncio.to_thread(source_pdf_sha256, pdf_path)
    if selector.source_pdf_sha256 != current_pdf_hash:
        raise HTTPException(status_code=409, detail="原始 PDF 已变化，请刷新后重新选择")

    try:
        layout = TranslationLayout.model_validate(
            await asyncio.to_thread(load_translation_layout, arxiv_id)
        )
    except Exception as exc:
        raise HTTPException(status_code=409, detail="当前 PDF 版面证据不可用") from exc
    if layout.source_pdf_sha256 != current_pdf_hash:
        raise HTTPException(status_code=409, detail="当前 PDF 版面已过期")
    if selector.page > layout.page_count:
        raise HTTPException(status_code=422, detail="选区页码不属于当前 PDF")

    mapping = map_selection_to_layout(selector.rects, layout.regions, selector.page)
    if mapping.block_index is None or mapping.block_index != request.block_index:
        raise HTTPException(status_code=409, detail="选区无法可靠匹配到当前论文段落")
    if selector.region_id is not None and selector.region_id != mapping.region_id:
        raise HTTPException(status_code=409, detail="选区位置与当前版面不一致")
    if (
        selector.layout_confidence is not None
        and mapping.layout_confidence is not None
        and abs(selector.layout_confidence - mapping.layout_confidence) > 1e-6
    ):
        raise HTTPException(status_code=409, detail="选区置信度与当前版面不一致")


@router.patch("/{annotation_id}", response_model=AnnotationItem)
async def update_annotation_route(
    arxiv_id: str,
    annotation_id: str,
    req: AnnotationUpdateRequest,
) -> AnnotationItem:
    _ensure_paper(arxiv_id)
    item = update_annotation(
        arxiv_id,
        annotation_id,
        note=req.note.strip() if req.note is not None else None,
        kind=req.kind,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="标注不存在")
    await safe_sync_paper_note_index(arxiv_id)
    return AnnotationItem(**item)


@router.delete("/{annotation_id}")
async def delete_annotation_route(arxiv_id: str, annotation_id: str) -> dict:
    _ensure_paper(arxiv_id)
    deleted = delete_annotation(arxiv_id, annotation_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="标注不存在")
    await safe_sync_paper_note_index(arxiv_id)
    return {"status": "deleted"}
