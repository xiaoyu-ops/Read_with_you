"""Validate and translate one user-confirmed PDF.js TextLayer selection."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from ..extraction.translation_layout import (
    NormalizedBox,
    TranslationLayout,
    TranslationLayoutRegion,
    source_pdf_sha256,
)
from ..storage import files
from .deeplx import DeepLXError, translate_text as translate_with_deeplx
from .immutables import (
    ImmutablePlaceholderError,
    protect_immutable_fragments,
    restore_immutable_fragments,
)


SELECTION_TRANSLATION_MAX_CHARS = 4_000
SELECTION_TRANSLATION_MAX_RECTS = 100
_MIN_RECT_REGION_COVERAGE = 0.55
_MIN_UNIQUE_SCORE_GAP = 0.12


class TextItemAnchor(BaseModel):
    item_index: int = Field(ge=0)
    char_offset: int = Field(ge=0)


class TextSelectionQuote(BaseModel):
    exact: str = Field(min_length=1, max_length=SELECTION_TRANSLATION_MAX_CHARS)
    prefix: str = Field(default="", max_length=64)
    suffix: str = Field(default="", max_length=64)


class SelectionTranslationRequest(BaseModel):
    version: Literal[2]
    source_pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page: int = Field(ge=1)
    raw_text: str = Field(min_length=2, max_length=SELECTION_TRANSLATION_MAX_CHARS)
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    start: TextItemAnchor
    end: TextItemAnchor
    quote: TextSelectionQuote
    rects: list[NormalizedBox] = Field(
        min_length=1,
        max_length=SELECTION_TRANSLATION_MAX_RECTS,
    )
    block_index: int | None = Field(default=None, ge=0)
    region_id: str | None = Field(default=None, min_length=1, max_length=200)
    layout_confidence: float | None = Field(default=None, ge=0, le=1)
    source_edited: bool = False

    @model_validator(mode="after")
    def validate_selection_shape(self) -> "SelectionTranslationRequest":
        if len(self.raw_text.strip()) < 2:
            raise ValueError("selection must contain at least two non-whitespace characters")
        if self.quote.exact != self.raw_text:
            raise ValueError("quote.exact must match raw_text")
        start_key = (self.start.item_index, self.start.char_offset)
        end_key = (self.end.item_index, self.end.char_offset)
        if start_key > end_key or start_key == end_key:
            raise ValueError("selection anchors must define a positive forward range")
        if self.region_id is not None and self.block_index is None:
            raise ValueError("region_id requires block_index")
        if self.source_edited and (
            self.block_index is not None
            or self.region_id is not None
            or self.layout_confidence is not None
        ):
            raise ValueError("edited source cannot claim an exact layout mapping")
        return self


class SelectionTranslationResponse(BaseModel):
    version: Literal[1] = 1
    provider: Literal["deeplx"] = "deeplx"
    source_text: str
    source_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    translation: str
    translation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page: int
    block_index: int | None
    region_id: str | None
    layout_confidence: float | None
    source_edited: bool


class SelectionTranslationError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        retryable: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable


@dataclass(frozen=True)
class SelectionLayoutMapping:
    block_index: int | None
    region_id: str | None
    layout_confidence: float | None


async def translate_pdf_selection(
    arxiv_id: str,
    request: SelectionTranslationRequest,
) -> SelectionTranslationResponse:
    document = await asyncio.to_thread(files.load_document, arxiv_id)
    if document is None:
        raise SelectionTranslationError(
            "paper_not_found",
            "论文不存在或已被删除。",
            status_code=404,
        )

    pdf_path = files.paper_dir(arxiv_id) / "original.pdf"
    current_pdf_hash = await asyncio.to_thread(_source_pdf_hash_if_present, pdf_path)
    if current_pdf_hash is None:
        raise SelectionTranslationError(
            "source_pdf_missing",
            "原始 PDF 不存在，请重新导入论文。",
            status_code=409,
        )
    if current_pdf_hash != request.source_pdf_sha256:
        raise SelectionTranslationError(
            "selection_source_changed",
            "原始 PDF 已变化，请刷新页面后重新选择。",
            status_code=409,
        )

    source_text_hash = _sha256_text(request.raw_text)
    if source_text_hash != request.text_sha256:
        raise SelectionTranslationError(
            "selection_text_changed",
            "选中文字已变化，请重新选择。",
            status_code=409,
        )

    raw_layout = await asyncio.to_thread(files.load_translation_layout, arxiv_id)
    try:
        layout = TranslationLayout.model_validate(raw_layout)
    except Exception as exc:
        raise SelectionTranslationError(
            "selection_layout_unavailable",
            "当前 PDF 版面证据不可用，请刷新或重建版面。",
            status_code=409,
        ) from exc
    if layout.source_pdf_sha256 != current_pdf_hash:
        raise SelectionTranslationError(
            "selection_layout_stale",
            "当前 PDF 版面已过期，请刷新后重新选择。",
            status_code=409,
        )
    if request.page > layout.page_count:
        raise SelectionTranslationError(
            "selection_page_invalid",
            "选区页码不属于当前 PDF。",
            status_code=422,
        )

    mapping = (
        SelectionLayoutMapping(None, None, None)
        if request.source_edited
        else map_selection_to_layout(request.rects, layout.regions, request.page)
    )
    if (
        request.block_index is not None
        and request.block_index != mapping.block_index
    ) or (
        request.region_id is not None
        and request.region_id != mapping.region_id
    ):
        raise SelectionTranslationError(
            "selection_layout_mismatch",
            "选区位置与当前版面不一致，请重新选择。",
            status_code=409,
        )

    protected = protect_immutable_fragments(request.raw_text)
    try:
        translated = await translate_with_deeplx(protected.text)
        translation = restore_immutable_fragments(translated.strip(), protected).strip()
    except ImmutablePlaceholderError as exc:
        raise SelectionTranslationError(
            "selection_immutable_invalid",
            "译文未能完整保留公式或引用，请缩短选区后重试。",
            status_code=422,
        ) from exc
    except DeepLXError as exc:
        raise _selection_deeplx_error(exc) from exc
    if not translation:
        raise SelectionTranslationError(
            "selection_translation_empty",
            "翻译服务没有返回有效译文，请稍后重试。",
            status_code=502,
            retryable=True,
        )

    return SelectionTranslationResponse(
        source_text=request.raw_text,
        source_text_sha256=source_text_hash,
        translation=translation,
        translation_sha256=_sha256_text(translation),
        page=request.page,
        block_index=mapping.block_index,
        region_id=mapping.region_id,
        layout_confidence=mapping.layout_confidence,
        source_edited=request.source_edited,
    )


def map_selection_to_layout(
    rects: list[NormalizedBox],
    regions: list[TranslationLayoutRegion],
    page: int,
) -> SelectionLayoutMapping:
    page_regions = [region for region in regions if region.page == page]
    if not rects or not page_regions:
        return SelectionLayoutMapping(None, None, None)

    matched: list[tuple[TranslationLayoutRegion, float]] = []
    for rect in rects:
        candidates = sorted(
            (
                (region, _intersection_area(rect, region.bbox) / _box_area(rect))
                for region in page_regions
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        candidates = [
            candidate
            for candidate in candidates
            if candidate[1] >= _MIN_RECT_REGION_COVERAGE
        ]
        if not candidates:
            return SelectionLayoutMapping(None, None, None)
        if (
            len(candidates) > 1
            and candidates[0][1] - candidates[1][1] < _MIN_UNIQUE_SCORE_GAP
        ):
            return SelectionLayoutMapping(None, None, None)
        matched.append(candidates[0])

    block_indexes = {region.block_index for region, _score in matched}
    if len(block_indexes) != 1:
        return SelectionLayoutMapping(None, None, None)
    region_ids = {region.region_id for region, _score in matched}
    confidence = min(min(region.confidence, score) for region, score in matched)
    return SelectionLayoutMapping(
        block_index=matched[0][0].block_index,
        region_id=matched[0][0].region_id if len(region_ids) == 1 else None,
        layout_confidence=confidence,
    )


def _selection_deeplx_error(error: DeepLXError) -> SelectionTranslationError:
    retryable_codes = {
        "deeplx_timeout",
        "deeplx_request_failed",
        "deeplx_rate_limited",
        "deeplx_http_error",
        "deeplx_provider_error",
        "deeplx_invalid_response",
        "deeplx_empty_translation",
    }
    configuration_codes = {
        "deeplx_not_configured",
        "deeplx_invalid_base_url",
        "deeplx_invalid_timeout",
        "deeplx_authentication_failed",
        "deeplx_credential_store_unavailable",
    }
    if error.code in configuration_codes:
        return SelectionTranslationError(
            error.code,
            "翻译服务尚未正确配置，请在设置中检查后重试。",
            status_code=503,
        )
    if error.code == "deeplx_rate_limited":
        message = "翻译服务当前繁忙，请稍后重试。"
    elif error.code == "deeplx_timeout":
        message = "翻译请求超时，请缩短选区或稍后重试。"
    else:
        message = "翻译服务暂时不可用，请稍后重试。"
    return SelectionTranslationError(
        error.code,
        message,
        status_code=502,
        retryable=error.code in retryable_codes,
    )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _source_pdf_hash_if_present(pdf_path: Path) -> str | None:
    if not pdf_path.is_file() or pdf_path.stat().st_size <= 0:
        return None
    return source_pdf_sha256(pdf_path)


def _box_area(box: NormalizedBox) -> float:
    return (box.x1 - box.x0) * (box.y1 - box.y0)


def _intersection_area(left: NormalizedBox, right: NormalizedBox) -> float:
    return max(0.0, min(left.x1, right.x1) - max(left.x0, right.x0)) * max(
        0.0,
        min(left.y1, right.y1) - max(left.y0, right.y0),
    )
