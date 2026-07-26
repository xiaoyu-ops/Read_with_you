"""Versioned PDF layout contract for original-position translations.

The layout stores geometry and matching evidence only. Translated text remains in
``translation.json`` so translation retries never invalidate PDF geometry.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from rapidfuzz import fuzz
from rapidfuzz.distance import Indel

from .blocks import Block
from .mineru import (
    MINERU_LAYOUT_ADAPTER,
    MinerUStructuredResult,
)
from .pdf_layout import (
    POPPLER_LAYOUT_ADAPTER,
    POPPLER_LAYOUT_ADAPTER_VERSION,
    PdfLayoutDocument,
    PdfLayoutLine,
    PdfLayoutWord,
)
from .pdf_mapping import PDF_MAPPING_VERSION
from ..translation.immutables import extract_immutable_fragments

TRANSLATION_LAYOUT_VERSION = 1
LEGACY_PDF_MAP_ADAPTER = "legacy_pdf_map"
LEGACY_PDF_MAP_ADAPTER_VERSION = str(PDF_MAPPING_VERSION)
MINERU_TRANSLATION_LAYOUT_ADAPTER_VERSION = "8"
HYBRID_LAYOUT_ADAPTER = "hybrid_poppler_mineru"
HYBRID_LAYOUT_ADAPTER_VERSION = "15"
REPLACE_CONFIDENCE = 0.90
_MAX_LAYOUT_FUZZY_CANDIDATES = 256
_RUN_IN_FIRST_LINE_MIN_OFFSET = 0.06
_NEAR_DUPLICATE_EDGE_TOLERANCE = 0.02
_NEAR_DUPLICATE_OVERLAP_RATIO = 0.80
_MINERU_MIN_MATCH_CONFIDENCE = 0.65
_MINERU_SHORT_HEADING_TOKEN_LIMIT = 12
_MINERU_LOW_CONFIDENCE_MAX_ENTRY_GAP = 8
_GLOBAL_EXACT_RECOVERY_MIN_TOKENS = 5
_SOURCE_ORDER_UNVERIFIED_EXACT = "source_order_unverified_exact"
_STRUCTURED_CAPTION_RE = re.compile(
    r"^(?:figure|table|listing)\s+[A-Z]?\d+(?:[.\-]\d+)*\s*:",
    re.IGNORECASE,
)
_SHORT_SUBFIGURE_CAPTION_RE = re.compile(r"^\s*\([a-z0-9]+\)\s+", re.IGNORECASE)
_LAYOUT_TOKEN_RE = re.compile(
    r"[a-z0-9]+|[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"
)
_TRANSLATABLE_MINERU_AUX_TYPES = {
    "image_caption",
    "image_footnote",
    "chart_caption",
    "chart_footnote",
    "table_caption",
    "table_footnote",
    "code_caption",
    "code_footnote",
}
_SAFE_TRANSLATABLE_TEXT_KINDS = {
    "heading",
    "paragraph",
    "text",
    "title",
    "list",
    "page_footnote",
    *_TRANSLATABLE_MINERU_AUX_TYPES,
}

RenderPolicy = Literal["replace", "preserve", "panel_only"]


class NormalizedBox(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float

    @model_validator(mode="after")
    def validate_bounds(self) -> "NormalizedBox":
        values = (self.x0, self.y0, self.x1, self.y1)
        if any(not math.isfinite(value) or value < 0 or value > 1 for value in values):
            raise ValueError("normalized box coordinates must be within [0, 1]")
        if self.x0 >= self.x1 or self.y0 >= self.y1:
            raise ValueError("normalized box must have positive area")
        return self


class TranslationLayoutPage(BaseModel):
    page: int = Field(ge=1)
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    rotation: Literal[0, 90, 180, 270] = 0
    protected_boxes: list[NormalizedBox] = Field(default_factory=list)


class TranslationLayoutSource(BaseModel):
    adapter: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    generation: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    is_ocr: bool | None = None


class TranslationLayoutRegion(BaseModel):
    region_id: str = Field(min_length=1)
    block_index: int = Field(ge=0)
    page: int = Field(ge=1)
    flow_order: int = Field(ge=0)
    kind: str = Field(min_length=1)
    bbox: NormalizedBox
    line_boxes: list[NormalizedBox] = Field(default_factory=list)
    word_boxes: list[NormalizedBox] = Field(default_factory=list)
    protected_boxes: list[NormalizedBox] = Field(default_factory=list)
    source_block_order: int | None = Field(default=None, ge=0)
    source_line_orders: list[int] = Field(default_factory=list)
    source_word_orders: list[int] = Field(default_factory=list)
    rotation: Literal[0, 90, 180, 270] = 0
    confidence: float = Field(ge=0, le=1)
    render_policy: RenderPolicy
    failure_reason: str | None = None
    geometry_source: str | None = None

    @model_validator(mode="after")
    def validate_policy_reason(self) -> "TranslationLayoutRegion":
        if self.render_policy == "replace" and self.failure_reason is not None:
            raise ValueError("replace regions cannot have a failure reason")
        if self.render_policy == "panel_only" and self.failure_reason is None:
            raise ValueError("panel_only regions require a failure reason")
        if any(order < 0 for order in (*self.source_line_orders, *self.source_word_orders)):
            raise ValueError("source reading orders must be non-negative")
        if self.source_line_orders and len(self.source_line_orders) != len(self.line_boxes):
            raise ValueError("source_line_orders must align with line_boxes")
        if len(self.source_word_orders) != len(self.word_boxes):
            raise ValueError("source_word_orders must align with word_boxes")
        return self


class TranslationLayoutQuality(BaseModel):
    mappable_count: int = Field(ge=0)
    mapped_count: int = Field(ge=0)
    replaceable_count: int = Field(ge=0)
    panel_only_count: int = Field(ge=0)
    unmapped_count: int = Field(ge=0)
    mapped_ratio: float = Field(ge=0, le=1)
    average_confidence: float = Field(ge=0, le=1)
    protected_overlap_count: int = Field(default=0, ge=0)
    protected_count: int = Field(default=0, ge=0)
    unmapped_block_indexes: list[int] = Field(default_factory=list)
    failure_counts: dict[str, int] = Field(default_factory=dict)


class TranslationLayout(BaseModel):
    version: int = TRANSLATION_LAYOUT_VERSION
    cache_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    block_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    adapter: str = Field(min_length=1)
    adapter_version: str = Field(min_length=1)
    pdf_url: str = Field(min_length=1)
    page_count: int = Field(ge=1)
    pages: list[TranslationLayoutPage]
    regions: list[TranslationLayoutRegion]
    quality: TranslationLayoutQuality
    warnings: list[str] = Field(default_factory=list)
    sources: list[TranslationLayoutSource] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_document_shape(self) -> "TranslationLayout":
        if self.version != TRANSLATION_LAYOUT_VERSION:
            raise ValueError(f"unsupported translation layout version: {self.version}")
        page_numbers = [item.page for item in self.pages]
        if page_numbers != list(range(1, self.page_count + 1)):
            raise ValueError("pages must be continuous and match page_count")
        known_pages = set(page_numbers)
        if any(region.page not in known_pages for region in self.regions):
            raise ValueError("region references an unknown page")
        region_ids = [region.region_id for region in self.regions]
        if len(region_ids) != len(set(region_ids)):
            raise ValueError("region_id values must be unique")
        return self


def source_pdf_sha256(pdf_path: Path) -> str:
    digest = hashlib.sha256()
    with pdf_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def block_source_sha256(blocks: list[Block]) -> str:
    payload = [
        {
            "index": block.index,
            "type": block.type,
            "original": block.original,
            "level": block.level,
        }
        for block in blocks
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def translation_layout_cache_key(
    source_hash: str,
    block_hash: str,
    *,
    adapter: str = LEGACY_PDF_MAP_ADAPTER,
    adapter_version: str = LEGACY_PDF_MAP_ADAPTER_VERSION,
    sources: list[TranslationLayoutSource] | None = None,
) -> str:
    payload = {
        "adapter": adapter,
        "adapter_version": adapter_version,
        "block_source_sha256": block_hash,
        "source_pdf_sha256": source_hash,
        "version": TRANSLATION_LAYOUT_VERSION,
    }
    if sources:
        payload["sources"] = [
            source.model_dump(mode="json", exclude_none=True)
            for source in sources
        ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def translation_layout_cache_matches(
    data: dict,
    blocks: list[Block],
    pdf_path: Path,
    *,
    adapter: str | None = None,
    adapter_version: str | None = None,
) -> bool:
    try:
        layout = TranslationLayout.model_validate(data)
    except (TypeError, ValueError):
        return False
    expected_adapter = adapter or layout.adapter
    expected_adapter_version = adapter_version or _current_adapter_version(expected_adapter)
    if expected_adapter_version is None:
        return False
    if any(
        (current := _current_adapter_version(source.adapter)) is None
        or source.adapter_version != current
        for source in layout.sources
    ):
        return False
    source_hash = source_pdf_sha256(pdf_path)
    block_hash = block_source_sha256(blocks)
    expected_key = translation_layout_cache_key(
        source_hash,
        block_hash,
        adapter=expected_adapter,
        adapter_version=expected_adapter_version,
        sources=layout.sources,
    )
    return (
        layout.version == TRANSLATION_LAYOUT_VERSION
        and layout.source_pdf_sha256 == source_hash
        and layout.block_source_sha256 == block_hash
        and layout.adapter == expected_adapter
        and layout.adapter_version == expected_adapter_version
        and layout.cache_key == expected_key
    )


_PURE_LAYOUT_COMMAND_RE = re.compile(
    r"^(?:\d*\.?\d+(?:pt|mm|cm|em|ex)\s+)?"
    r"\\(?:contournumber|contourlength)(?:\s+\d*\.?\d+(?:pt|mm|cm|em|ex)?)?$",
    re.IGNORECASE,
)
_LEGACY_LATEX_CONTROL_RE = re.compile(
    r"^\s*\\(?:"
    r"documentclass|usepackage|input|include|newcommand|renewcommand|providecommand|"
    r"definecolor|makeatletter|makeatother|maketitle|iclrfinalcopy|begin|end|"
    r"title|author|date|appendix|label|bibliography|bibliographystyle|"
    r"section|subsection|subsubsection|paragraph|subparagraph|"
    r"centering|vskip|vspace|hspace"
    r")(?:\b|\s*\{)",
    re.IGNORECASE,
)
_LEGACY_LATEX_PROTECTED_OBJECT_RE = re.compile(
    r"\\(?:includegraphics|begin\s*\{\s*(?:figure|table)\*?\s*\})",
    re.IGNORECASE,
)


def legacy_latex_extraction_debris_indexes(blocks: list[Block]) -> set[int]:
    """Identify pre-cleanup LaTeX control/object paragraphs in cached documents."""
    return {
        block.index
        for block in blocks
        if block.type in {"heading", "paragraph"}
        and (
            _LEGACY_LATEX_CONTROL_RE.search(block.original)
            or _LEGACY_LATEX_PROTECTED_OBJECT_RE.search(block.original)
        )
    }


def mappable_text_block_indexes(blocks: list[Block]) -> set[int]:
    """Return real prose blocks, excluding known legacy extraction debris."""
    indexes: set[int] = set()
    legacy_debris = legacy_latex_extraction_debris_indexes(blocks)
    pending_table_cells: Counter[str] = Counter()
    for block in blocks:
        text = block.original.strip()
        if block.type == "table":
            pending_table_cells = _structured_table_cell_texts(text)
            continue
        if block.type != "paragraph" or not text:
            pending_table_cells.clear()
            if (
                block.type == "heading"
                and block.index not in legacy_debris
                and text
                and not _PURE_LAYOUT_COMMAND_RE.fullmatch(text)
            ):
                indexes.add(block.index)
            continue
        if block.index in legacy_debris:
            pending_table_cells.clear()
            continue
        normalized = " ".join(_normalize_layout_tokens(text))
        if _PURE_LAYOUT_COMMAND_RE.fullmatch(text):
            continue
        if pending_table_cells.get(normalized, 0) > 0:
            pending_table_cells[normalized] -= 1
            continue
        pending_table_cells.clear()
        indexes.add(block.index)
    return indexes


def safe_translation_layout_metrics(
    blocks: list[Block],
    layout: TranslationLayout,
) -> dict[str, int | float]:
    """Recompute fail-closed replace safety from regions, not stored quality."""
    text_candidates = mappable_text_block_indexes(blocks)
    protected_excluded = {
        block.index
        for block in blocks
        if block.index in text_candidates
        and any(
            fragment.kind == "math"
            for fragment in extract_immutable_fragments(block.original)
        )
    }
    eligible = text_candidates - protected_excluded
    regions_by_block: dict[int, list[TranslationLayoutRegion]] = defaultdict(list)
    protected_by_page: dict[int, list[NormalizedBox]] = defaultdict(list)
    for page in layout.pages:
        protected_by_page[page.page].extend(page.protected_boxes)
    for region in layout.regions:
        regions_by_block[region.block_index].append(region)
        protected_by_page[region.page].extend(region.protected_boxes)
    pages = {page.page: page for page in layout.pages}

    safe_blocks: set[int] = set()
    safe_regions: list[TranslationLayoutRegion] = []
    for block_index in eligible:
        regions = sorted(
            regions_by_block.get(block_index, []),
            key=lambda item: item.flow_order,
        )
        if not regions or [region.flow_order for region in regions] != list(
            range(len(regions))
        ):
            continue
        if any(
            index > 0 and region.page < regions[index - 1].page
            for index, region in enumerate(regions)
        ):
            continue

        block_safe = True
        for region in regions:
            page = pages.get(region.page)
            boxes = (
                region.bbox,
                *region.line_boxes,
                *region.word_boxes,
                *region.protected_boxes,
            )
            if (
                page is None
                or page.rotation != 0
                or region.rotation != 0
                or region.render_policy != "replace"
                or region.failure_reason is not None
                or region.kind not in _SAFE_TRANSLATABLE_TEXT_KINDS
                or not math.isfinite(region.confidence)
                or region.confidence < REPLACE_CONFIDENCE
                or region.confidence > 1
                or any(not _normalized_box_is_valid(box) for box in boxes)
                or not region.line_boxes
                or any(
                    not _box_contains(region.bbox, box)
                    for box in (*region.line_boxes, *region.word_boxes)
                )
                or (
                    region.source_line_orders
                    and len(region.source_line_orders) != len(region.line_boxes)
                )
                or len(region.source_word_orders) != len(region.word_boxes)
                or not _safe_orders_are_valid(region.source_line_orders)
                or not _safe_orders_are_valid(region.source_word_orders)
                or (
                    region.source_block_order is not None
                    and (
                        not isinstance(region.source_block_order, int)
                        or isinstance(region.source_block_order, bool)
                        or region.source_block_order < 0
                    )
                )
                or any(
                    _boxes_intersect(region.bbox, protected)
                    for protected in protected_by_page[region.page]
                )
            ):
                block_safe = False
                break
        if block_safe:
            safe_blocks.add(block_index)
            safe_regions.extend(regions)

    eligible_count = len(eligible)
    safe_replace_count = len(safe_blocks)
    return {
        "eligible_count": eligible_count,
        "protected_excluded_count": len(protected_excluded),
        "safe_replace_count": safe_replace_count,
        "safe_coverage": round(
            safe_replace_count / eligible_count if eligible_count else 0.0,
            6,
        ),
        "replace_average_confidence": round(
            sum(region.confidence for region in safe_regions) / len(safe_regions)
            if safe_regions
            else 0.0,
            6,
        ),
    }


def _short_structured_figure_caption_indexes(blocks: list[Block]) -> set[int]:
    captions: set[str] = set()
    for block in blocks:
        if block.type != "figure":
            continue
        try:
            payload = json.loads(block.original)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        caption = payload.get("caption") if isinstance(payload, dict) else None
        if not isinstance(caption, str):
            continue
        tokens = _normalize_layout_tokens(caption)
        if (
            tokens
            and len(tokens) <= _MINERU_SHORT_HEADING_TOKEN_LIMIT
            and _SHORT_SUBFIGURE_CAPTION_RE.match(caption)
        ):
            captions.add(" ".join(tokens))
    return {
        block.index
        for block in blocks
        if block.type == "paragraph"
        and " ".join(_normalize_layout_tokens(block.original)) in captions
    }


def _structured_table_cell_texts(text: str) -> Counter[str]:
    try:
        payload = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return Counter()
    if not isinstance(payload, dict) or payload.get("kind") != "table":
        return Counter()
    values: Counter[str] = Counter()
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return values
    for row in rows:
        if not isinstance(row, list):
            continue
        for cell in row:
            if not isinstance(cell, dict) or not isinstance(cell.get("text"), str):
                continue
            normalized = " ".join(_normalize_layout_tokens(cell["text"]))
            if normalized:
                values[normalized] += 1
    return values


def translation_layout_from_pdf_layout(
    blocks: list[Block],
    pdf_path: Path,
    document: PdfLayoutDocument,
) -> TranslationLayout:
    """Match project blocks to Poppler's flow-preserving bbox hierarchy."""
    if any(page.rotation != 0 for page in document.pages):
        raise ValueError("rotation_unsupported")

    tokens = _layout_tokens(document)
    token_positions: dict[str, list[int]] = defaultdict(list)
    for token_index, token in enumerate(tokens):
        token_positions[token.norm].append(token_index)
    prose_cursor = 0
    protected_cursor = 0
    consumed_token_indexes: set[int] = set()
    matched: list[tuple[Block, int, int, float, str | None]] = []
    panel_candidates: list[tuple[Block, int, int, float]] = []
    corroboration_candidates: list[tuple[Block, int, int, float]] = []
    mappable_indexes = mappable_text_block_indexes(blocks)
    ambiguous_short_caption_indexes = _short_structured_figure_caption_indexes(blocks)
    for block in blocks:
        if block.type == "figure":
            # Poppler exposes text boxes, not authoritative image geometry. A
            # structured figure caption must not become a synthetic protected
            # image box that shadows the real caption paragraph.
            continue
        if block.index in ambiguous_short_caption_indexes:
            continue
        if block.type in ("heading", "paragraph") and block.index not in mappable_indexes:
            continue
        target_tokens = _normalize_layout_tokens(block.original)
        if not target_tokens:
            continue
        is_prose = block.type in ("heading", "paragraph")
        cursor = prose_cursor if is_prose else protected_cursor
        match = _match_layout_tokens(
            target_tokens,
            tokens,
            cursor,
            consumed_token_indexes,
        )
        if is_prose:
            exact_matches = _global_exact_layout_matches(
                target_tokens,
                tokens,
                token_positions,
                consumed_token_indexes,
            )
            if len(exact_matches) == 1:
                exact_match = exact_matches[0]
                if (
                    exact_match[0] >= cursor
                    or len(target_tokens) >= _GLOBAL_EXACT_RECOVERY_MIN_TOKENS
                ):
                    match = exact_match
                elif (
                    block.type == "heading"
                    and match is not None
                    and match[2] < 1.0
                ):
                    match = None
            elif (
                len(exact_matches) > 1
                and match is not None
                and match[2] < 1.0
            ):
                # Repeated exact text behind the cursor cannot authorize a
                # distant approximate match or advance the reading-order anchor.
                match = None
        if match is None:
            if (
                is_prose
                and len(exact_matches) == 1
                and exact_matches[0][0] < cursor
                and _poppler_tokens_form_single_visual_line(
                    tokens[exact_matches[0][0] : exact_matches[0][1]]
                )
            ):
                corroboration_candidates.append((block, *exact_matches[0]))
            continue
        start, end, confidence = match
        if confidence < 0.72:
            continue
        if confidence >= REPLACE_CONFIDENCE:
            matched.append((block, start, end, confidence, None))
            consumed_token_indexes.update(range(start, end))
            if is_prose:
                # A unique exact match may safely recover evidence before the
                # cursor, but must never rewind the established prose order.
                prose_cursor = max(prose_cursor, end)
            else:
                protected_cursor = end
        else:
            panel_candidates.append((block, start, end, confidence))

    # These unique exact spans are behind the established prose cursor. They
    # remain panel-only unless an independent layout source corroborates the
    # same single-line geometry.
    for candidate in corroboration_candidates:
        _, start, end, _ = candidate
        if not consumed_token_indexes.isdisjoint(range(start, end)):
            continue
        matched.append((*candidate, _SOURCE_ORDER_UNVERIFIED_EXACT))
        consumed_token_indexes.update(range(start, end))

    # Low-confidence panels cannot reserve evidence needed by a later precise
    # or exact corroboration match.
    for candidate in panel_candidates:
        _, start, end, _ = candidate
        if not consumed_token_indexes.isdisjoint(range(start, end)):
            continue
        matched.append((*candidate, "low_confidence"))
        consumed_token_indexes.update(range(start, end))

    pages = [
        TranslationLayoutPage(
            page=page.page,
            width=page.width,
            height=page.height,
            rotation=page.rotation,
        )
        for page in document.pages
    ]
    page_by_number = {page.page: page for page in document.pages}
    regions: list[TranslationLayoutRegion] = []
    mapped_mappable: set[int] = set()
    for block, start, end, confidence, match_failure_reason in matched:
        block_target_tokens = _normalize_layout_tokens(block.original)
        tokens_by_region: dict[tuple[int, int], list[_LayoutToken]] = {}
        for token in tokens[start:end]:
            region_key = (token.page, token.block_reading_order)
            tokens_by_region.setdefault(region_key, []).append(token)

        region_token_groups: list[tuple[int, int, list[_LayoutToken]]] = []
        if match_failure_reason == _SOURCE_ORDER_UNVERIFIED_EXACT:
            exact_tokens = tokens[start:end]
            region_token_groups.append(
                (
                    exact_tokens[0].page,
                    min(token.block_reading_order for token in exact_tokens),
                    exact_tokens,
                )
            )
        for (page_number, source_block_order), region_tokens in (
            []
            if match_failure_reason == _SOURCE_ORDER_UNVERIFIED_EXACT
            else tokens_by_region.items()
        ):
            page = page_by_number[page_number]
            split_groups = (
                _split_run_in_prose_tokens(region_tokens, page.width)
                if block.type in ("heading", "paragraph")
                else [region_tokens]
            )
            if (
                block.type in ("heading", "paragraph")
                and confidence >= REPLACE_CONFIDENCE
            ):
                retained_groups = [
                    group
                    for group in split_groups
                    if _poppler_region_has_target_evidence(
                        group,
                        block_target_tokens,
                    )
                ]
                split_groups = retained_groups
            region_token_groups.extend(
                (page_number, source_block_order, group) for group in split_groups
            )

        if (
            block.type in ("heading", "paragraph")
            and confidence >= REPLACE_CONFIDENCE
            and match_failure_reason is None
            and not _poppler_group_covers_target(
                [group for _, _, group in region_token_groups],
                block_target_tokens,
            )
        ):
            match_failure_reason = "incomplete_source_evidence"

        for flow_order, (
            page_number,
            source_block_order,
            region_tokens,
        ) in enumerate(region_token_groups):
            page = page_by_number[page_number]
            words_by_order = {
                token.word.reading_order: token.word for token in region_tokens
            }
            words = list(words_by_order.values())
            word_box_by_order = {
                order: NormalizedBox(
                    x0=word.bbox.x0 / page.width,
                    y0=word.bbox.y0 / page.height,
                    x1=word.bbox.x1 / page.width,
                    y1=word.bbox.y1 / page.height,
                )
                for order, word in words_by_order.items()
            }
            protected = block.type not in ("heading", "paragraph")
            if protected:
                lines_by_order = {
                    token.line.reading_order: token.line for token in region_tokens
                }
                line_boxes = [
                    NormalizedBox(
                        x0=line.bbox.x0 / page.width,
                        y0=line.bbox.y0 / page.height,
                        x1=line.bbox.x1 / page.width,
                        y1=line.bbox.y1 / page.height,
                    )
                    for line in lines_by_order.values()
                ]
            else:
                if match_failure_reason == _SOURCE_ORDER_UNVERIFIED_EXACT:
                    word_orders_by_line = {
                        min(token.line.reading_order for token in region_tokens): list(
                            words_by_order
                        )
                    }
                else:
                    word_orders_by_line = {}
                    for token in region_tokens:
                        line_words = word_orders_by_line.setdefault(
                            token.line.reading_order,
                            [],
                        )
                        if token.word.reading_order not in line_words:
                            line_words.append(token.word.reading_order)
                lines_by_order = {
                    line_order: token.line
                    for line_order in word_orders_by_line
                    for token in region_tokens
                    if token.line.reading_order == line_order
                }
                line_boxes = [
                    _union_boxes(
                        [word_box_by_order[order] for order in word_orders]
                    )
                    for word_orders in word_orders_by_line.values()
                ]
            word_boxes = [
                NormalizedBox(
                    x0=word.bbox.x0 / page.width,
                    y0=word.bbox.y0 / page.height,
                    x1=word.bbox.x1 / page.width,
                    y1=word.bbox.y1 / page.height,
                )
                for word in words
            ]
            bbox = _union_boxes(line_boxes)
            if protected:
                render_policy: RenderPolicy = "preserve"
                failure_reason = "protected_content"
            elif match_failure_reason is not None:
                render_policy = "panel_only"
                failure_reason = match_failure_reason
                if block.index in mappable_indexes:
                    mapped_mappable.add(block.index)
            elif confidence >= REPLACE_CONFIDENCE:
                render_policy = "replace"
                failure_reason = None
                if block.index in mappable_indexes:
                    mapped_mappable.add(block.index)
            else:
                render_policy = "panel_only"
                failure_reason = "low_confidence"
                if block.index in mappable_indexes:
                    mapped_mappable.add(block.index)
            regions.append(
                TranslationLayoutRegion(
                    region_id=_stable_region_id(block.index, page_number, flow_order, bbox),
                    block_index=block.index,
                    page=page_number,
                    flow_order=flow_order,
                    kind=str(block.type),
                    bbox=bbox,
                    line_boxes=line_boxes,
                    word_boxes=word_boxes,
                    protected_boxes=[bbox] if protected else [],
                    source_block_order=source_block_order,
                    source_line_orders=list(lines_by_order),
                    source_word_orders=list(words_by_order),
                    rotation=page.rotation,
                    confidence=round(confidence, 3),
                    render_policy=render_policy,
                    failure_reason=failure_reason,
                    geometry_source=POPPLER_LAYOUT_ADAPTER,
                )
            )

    regions.sort(key=lambda item: (item.block_index, item.page, item.flow_order))
    mapped_mappable.update(
        region.block_index
        for region in regions
        if region.block_index in mappable_indexes
    )
    text_confidences = [
        region.confidence
        for region in regions
        if region.block_index in mappable_indexes
    ]
    replaceable_blocks = {
        region.block_index for region in regions if region.render_policy == "replace"
    }
    panel_only_blocks = {
        region.block_index for region in regions if region.render_policy == "panel_only"
    }
    protected_blocks = {
        region.block_index for region in regions if region.render_policy == "preserve"
    }
    unmapped_indexes = sorted(mappable_indexes - mapped_mappable)
    failure_counts: dict[str, int] = defaultdict(int)
    for region in regions:
        if region.failure_reason:
            failure_counts[region.failure_reason] += 1

    mappable_count = len(mappable_indexes)
    mapped_count = len(mapped_mappable)
    source_hash = source_pdf_sha256(pdf_path)
    block_hash = block_source_sha256(blocks)
    return TranslationLayout(
        cache_key=translation_layout_cache_key(
            source_hash,
            block_hash,
            adapter=document.adapter,
            adapter_version=document.adapter_version,
        ),
        source_pdf_sha256=source_hash,
        block_source_sha256=block_hash,
        adapter=document.adapter,
        adapter_version=document.adapter_version,
        pdf_url=f"/assets/{pdf_path.parent.name}/{pdf_path.name}",
        page_count=document.page_count,
        pages=pages,
        regions=regions,
        quality=TranslationLayoutQuality(
            mappable_count=mappable_count,
            mapped_count=mapped_count,
            replaceable_count=len(replaceable_blocks),
            panel_only_count=len(panel_only_blocks),
            unmapped_count=len(unmapped_indexes),
            mapped_ratio=round(mapped_count / mappable_count, 3) if mappable_count else 0,
            average_confidence=(
                round(sum(text_confidences) / len(text_confidences), 3)
                if text_confidences
                else 0
            ),
            protected_overlap_count=0,
            protected_count=len(protected_blocks),
            unmapped_block_indexes=unmapped_indexes,
            failure_counts=dict(sorted(failure_counts.items())),
        ),
        warnings=list(document.warnings),
    )


def translation_layout_from_mineru(
    blocks: list[Block],
    pdf_path: Path,
    result: MinerUStructuredResult,
) -> TranslationLayout:
    """Build the common layout from stable MinerU middle/content-list data."""
    if result.layout is None or result.content_list is None:
        raise ValueError("mineru_layout_missing")
    raw_pages = result.layout.get("pdf_info")
    if not isinstance(raw_pages, list) or not raw_pages:
        raise ValueError("mineru_layout_missing")
    ordered_pages = sorted(raw_pages, key=lambda item: item.get("page_idx", -1))
    if [page.get("page_idx") for page in ordered_pages] != list(range(len(ordered_pages))):
        raise ValueError("mineru_page_order_invalid")
    actual_page_count = _source_pdf_page_count(pdf_path)
    if actual_page_count != len(ordered_pages):
        raise ValueError("mineru_page_count_mismatch")

    pages = [
        TranslationLayoutPage(
            page=position + 1,
            width=float(raw_page["page_size"][0]),
            height=float(raw_page["page_size"][1]),
            rotation=0,
            protected_boxes=_mineru_page_protected_boxes(raw_page),
        )
        for position, raw_page in enumerate(ordered_pages)
    ]
    content_entries = _mineru_content_entries(result.content_list, ordered_pages)
    matches = _match_blocks_to_mineru_content(blocks, content_entries)
    regions: list[TranslationLayoutRegion] = []
    next_flow_order: dict[int, int] = defaultdict(int)
    for block, entry, confidence, _, match_failure_reason in matches:
        protected = entry.protected or (
            block.type not in ("heading", "paragraph")
            and entry.kind not in _TRANSLATABLE_MINERU_AUX_TYPES
        )
        if protected:
            render_policy: RenderPolicy = "preserve"
            failure_reason = "protected_content"
        elif not entry.authoritative:
            render_policy = "panel_only"
            failure_reason = "middle_geometry_missing"
        elif match_failure_reason is not None:
            render_policy = "panel_only"
            failure_reason = match_failure_reason
        elif _mineru_geometry_is_discontinuous(entry):
            render_policy = "panel_only"
            failure_reason = "cross_page_geometry_ambiguous"
        elif confidence >= REPLACE_CONFIDENCE:
            render_policy = "replace"
            failure_reason = None
        else:
            render_policy = "panel_only"
            failure_reason = "low_confidence"
        segments = _mineru_geometry_segments(entry, page_count=len(pages))
        for page_number, bbox, line_boxes, protected_boxes in segments:
            flow_order = next_flow_order[block.index]
            next_flow_order[block.index] += 1
            regions.append(
                TranslationLayoutRegion(
                    region_id=_stable_region_id(
                        block.index,
                        page_number,
                        flow_order,
                        bbox,
                    ),
                    block_index=block.index,
                    page=page_number,
                    flow_order=flow_order,
                    kind=entry.kind,
                    bbox=bbox,
                    line_boxes=line_boxes,
                    protected_boxes=protected_boxes,
                    rotation=entry.rotation,
                    confidence=round(confidence, 3),
                    render_policy=render_policy,
                    failure_reason=failure_reason,
                    geometry_source=MINERU_LAYOUT_ADAPTER,
                )
            )

    regions.sort(key=lambda item: (item.block_index, item.page, item.flow_order))
    _downgrade_unsafe_replace_blocks(regions, pages)
    quality = _quality_from_regions(blocks, regions, pages)
    source_hash = source_pdf_sha256(pdf_path)
    block_hash = block_source_sha256(blocks)
    return TranslationLayout(
        cache_key=translation_layout_cache_key(
            source_hash,
            block_hash,
            adapter=MINERU_LAYOUT_ADAPTER,
            adapter_version=MINERU_TRANSLATION_LAYOUT_ADAPTER_VERSION,
        ),
        source_pdf_sha256=source_hash,
        block_source_sha256=block_hash,
        adapter=MINERU_LAYOUT_ADAPTER,
        adapter_version=MINERU_TRANSLATION_LAYOUT_ADAPTER_VERSION,
        pdf_url=f"/assets/{pdf_path.parent.name}/{pdf_path.name}",
        page_count=len(pages),
        pages=pages,
        regions=regions,
        quality=quality,
        warnings=[],
    )


def translation_layout_from_hybrid(
    blocks: list[Block],
    pdf_path: Path,
    poppler: TranslationLayout,
    mineru: TranslationLayout,
    *,
    mineru_generation: str | None = None,
    mineru_is_ocr: bool | None = None,
) -> TranslationLayout:
    """Select one authoritative geometry source per block, fail-closed."""
    source_hash = source_pdf_sha256(pdf_path)
    block_hash = block_source_sha256(blocks)
    if (
        poppler.source_pdf_sha256 != source_hash
        or mineru.source_pdf_sha256 != source_hash
        or poppler.block_source_sha256 != block_hash
        or mineru.block_source_sha256 != block_hash
        or poppler.page_count != mineru.page_count
    ):
        raise ValueError("hybrid_source_mismatch")

    pages: list[TranslationLayoutPage] = []
    for poppler_page, mineru_page in zip(poppler.pages, mineru.pages, strict=True):
        if (
            poppler_page.page != mineru_page.page
            or poppler_page.rotation != mineru_page.rotation
            or abs(poppler_page.width - mineru_page.width) > 1
            or abs(poppler_page.height - mineru_page.height) > 1
        ):
            raise ValueError("hybrid_page_geometry_mismatch")
        pages.append(
            TranslationLayoutPage(
                page=poppler_page.page,
                width=poppler_page.width,
                height=poppler_page.height,
                rotation=poppler_page.rotation,
                protected_boxes=_unique_normalized_boxes(
                    [
                        *poppler_page.protected_boxes,
                        *mineru_page.protected_boxes,
                    ]
                ),
            )
        )

    page_protected = {page.page: page.protected_boxes for page in pages}
    poppler_by_block = _regions_by_block(poppler.regions)
    mineru_by_block = _regions_by_block(mineru.regions)
    mappable_indexes = mappable_text_block_indexes(blocks)
    regions: list[TranslationLayoutRegion] = []
    for block in blocks:
        poppler_group = poppler_by_block.get(block.index, [])
        mineru_group = mineru_by_block.get(block.index, [])
        if block.index in mappable_indexes:
            corroborated_poppler = _hybrid_corroborated_exact_poppler_group(
                poppler_group,
                mineru_group,
                page_protected,
            )
            if corroborated_poppler:
                selected = corroborated_poppler
            elif _hybrid_replace_group_is_safe(poppler_group, page_protected):
                selected = poppler_group
            elif _hybrid_replace_group_is_safe(mineru_group, page_protected):
                selected = mineru_group
            else:
                selected = _hybrid_fallback_group(poppler_group, mineru_group)
        else:
            selected = mineru_group or poppler_group
        if not selected:
            continue

        copied = [region.model_copy(deep=True) for region in selected]
        selected_is_safe = (
            block.index not in mappable_indexes
            or _hybrid_replace_group_is_safe(copied, page_protected)
        )
        for flow_order, region in enumerate(copied):
            region.flow_order = flow_order
            if region.geometry_source is None:
                region.geometry_source = (
                    poppler.adapter if selected is poppler_group else mineru.adapter
                )
            if (
                block.index in mappable_indexes
                and region.render_policy == "replace"
                and not selected_is_safe
            ):
                region.render_policy = "panel_only"
                region.failure_reason = (
                    "protected_overlap"
                    if any(
                        _boxes_intersect(region.bbox, protected)
                        for protected in (
                            *page_protected.get(region.page, []),
                            *region.protected_boxes,
                        )
                    )
                    else "hybrid_geometry_unverified"
                )
            region.region_id = _stable_region_id(
                region.block_index,
                region.page,
                region.flow_order,
                region.bbox,
            )
            regions.append(region)

    regions.sort(key=lambda item: (item.block_index, item.page, item.flow_order))
    _resolve_hybrid_geometry_conflicts(regions, mappable_indexes)
    sources = [
        TranslationLayoutSource(
            adapter=poppler.adapter,
            adapter_version=poppler.adapter_version,
        ),
        TranslationLayoutSource(
            adapter=mineru.adapter,
            adapter_version=mineru.adapter_version,
            generation=mineru_generation,
            is_ocr=mineru_is_ocr,
        ),
    ]
    quality = _quality_from_regions(blocks, regions, pages)
    return TranslationLayout(
        cache_key=translation_layout_cache_key(
            source_hash,
            block_hash,
            adapter=HYBRID_LAYOUT_ADAPTER,
            adapter_version=HYBRID_LAYOUT_ADAPTER_VERSION,
            sources=sources,
        ),
        source_pdf_sha256=source_hash,
        block_source_sha256=block_hash,
        adapter=HYBRID_LAYOUT_ADAPTER,
        adapter_version=HYBRID_LAYOUT_ADAPTER_VERSION,
        pdf_url=f"/assets/{pdf_path.parent.name}/{pdf_path.name}",
        page_count=len(pages),
        pages=pages,
        regions=regions,
        quality=quality,
        warnings=sorted({*poppler.warnings, *mineru.warnings, "hybrid_geometry"}),
        sources=sources,
    )


def bind_mineru_layout_source(
    layout: TranslationLayout,
    *,
    generation: str | None,
    is_ocr: bool | None,
) -> TranslationLayout:
    """Bind a pure MinerU layout cache to the immutable raw generation."""
    if layout.adapter != MINERU_LAYOUT_ADAPTER:
        raise ValueError("mineru_source_binding_requires_mineru_layout")
    if generation is None:
        raise ValueError("mineru_generation_missing")
    bound = layout.model_copy(deep=True)
    bound.sources = [
        TranslationLayoutSource(
            adapter=MINERU_LAYOUT_ADAPTER,
            adapter_version=bound.adapter_version,
            generation=generation,
            is_ocr=is_ocr,
        )
    ]
    bound.cache_key = translation_layout_cache_key(
        bound.source_pdf_sha256,
        bound.block_source_sha256,
        adapter=bound.adapter,
        adapter_version=bound.adapter_version,
        sources=bound.sources,
    )
    return bound


def _regions_by_block(
    regions: list[TranslationLayoutRegion],
) -> dict[int, list[TranslationLayoutRegion]]:
    grouped: dict[int, list[TranslationLayoutRegion]] = defaultdict(list)
    for region in regions:
        grouped[region.block_index].append(region)
    for group in grouped.values():
        group.sort(key=lambda item: (item.flow_order, item.page))
    return grouped


def _resolve_hybrid_geometry_conflicts(
    regions: list[TranslationLayoutRegion],
    mappable_indexes: set[int],
) -> None:
    """Fail closed when two selected sources claim nearly the same geometry."""
    groups = {
        block_index: group
        for block_index, group in _regions_by_block(regions).items()
        if block_index in mappable_indexes
    }
    losing_region_ids: dict[int, set[str]] = defaultdict(set)
    remove_whole_blocks: set[int] = set()
    block_indexes = sorted(groups)
    for position, left_index in enumerate(block_indexes):
        left_group = groups[left_index]
        for right_index in block_indexes[position + 1 :]:
            right_group = groups[right_index]
            conflicts = [
                (left, right)
                for left in left_group
                for right in right_group
                if left.geometry_source != right.geometry_source
                and left.geometry_source is not None
                and right.geometry_source is not None
                and left.page == right.page
                and (
                    _boxes_are_near_duplicates(left.bbox, right.bbox)
                    or _coarse_panel_intercepts_precise_replace(left, right)
                )
            ]
            if not conflicts:
                continue
            left_rank = _hybrid_geometry_trust_rank(left_group)
            right_rank = _hybrid_geometry_trust_rank(right_group)
            if left_rank > right_rank:
                losing_region_ids[right_index].update(
                    right.region_id for _, right in conflicts
                )
            elif right_rank > left_rank:
                losing_region_ids[left_index].update(
                    left.region_id for left, _ in conflicts
                )
            else:
                remove_whole_blocks.update((left_index, right_index))

    retained: list[TranslationLayoutRegion] = []
    for block_index, group in _regions_by_block(regions).items():
        conflict_ids = losing_region_ids.get(block_index, set())
        if block_index in remove_whole_blocks:
            continue
        if not conflict_ids:
            retained.extend(group)
            continue
        # Translation text is block-scoped, so losing even one region means
        # the remaining geometry no longer proves where the full translation
        # belongs. Keep the whole block out of the overlay plan.
        continue
    regions[:] = retained


def _hybrid_geometry_trust_rank(
    regions: list[TranslationLayoutRegion],
) -> tuple[bool, bool, bool]:
    return (
        bool(regions)
        and any(region.render_policy == "preserve" for region in regions),
        bool(regions) and all(region.render_policy == "replace" for region in regions),
        bool(regions)
        and all(
            region.word_boxes
            and len(region.source_word_orders) == len(region.word_boxes)
            for region in regions
        ),
    )


def _orders_are_strictly_increasing(values: list[int]) -> bool:
    return all(
        value > values[index - 1]
        for index, value in enumerate(values)
        if index > 0
    )


def _safe_orders_are_valid(values: list[int]) -> bool:
    return all(
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
        and (index == 0 or value > values[index - 1])
        for index, value in enumerate(values)
    )


def _boxes_are_near_duplicates(
    left: NormalizedBox,
    right: NormalizedBox,
) -> bool:
    edge_delta = max(
        abs(left.x0 - right.x0),
        abs(left.y0 - right.y0),
        abs(left.x1 - right.x1),
        abs(left.y1 - right.y1),
    )
    if edge_delta > _NEAR_DUPLICATE_EDGE_TOLERANCE:
        return False
    intersection_width = max(0.0, min(left.x1, right.x1) - max(left.x0, right.x0))
    intersection_height = max(0.0, min(left.y1, right.y1) - max(left.y0, right.y0))
    intersection_area = intersection_width * intersection_height
    smaller_area = min(
        (left.x1 - left.x0) * (left.y1 - left.y0),
        (right.x1 - right.x0) * (right.y1 - right.y0),
    )
    return (
        smaller_area > 0
        and intersection_area / smaller_area >= _NEAR_DUPLICATE_OVERLAP_RATIO
    )


def _coarse_panel_intercepts_precise_replace(
    left: TranslationLayoutRegion,
    right: TranslationLayoutRegion,
) -> bool:
    for precise, coarse in ((left, right), (right, left)):
        if (
            precise.render_policy == "replace"
            and precise.failure_reason is None
            and precise.confidence >= REPLACE_CONFIDENCE
            and precise.word_boxes
            and len(precise.source_word_orders) == len(precise.word_boxes)
            and coarse.render_policy == "panel_only"
            and coarse.confidence < REPLACE_CONFIDENCE
            and not coarse.word_boxes
            and _boxes_intersect(precise.bbox, coarse.bbox)
        ):
            return True
    return False


def _hybrid_fallback_group(
    poppler_group: list[TranslationLayoutRegion],
    mineru_group: list[TranslationLayoutRegion],
) -> list[TranslationLayoutRegion]:
    """Keep precise Poppler text boxes when both sources must stay panel-only."""
    if poppler_group and all(
        region.word_boxes
        and len(region.source_word_orders) == len(region.word_boxes)
        and region.failure_reason != "incomplete_source_evidence"
        for region in poppler_group
    ):
        return poppler_group
    return mineru_group or poppler_group


def _hybrid_corroborated_exact_poppler_group(
    poppler_group: list[TranslationLayoutRegion],
    mineru_group: list[TranslationLayoutRegion],
    page_protected: dict[int, list[NormalizedBox]],
) -> list[TranslationLayoutRegion]:
    """Promote exact Poppler words only when MinerU independently locates them."""
    if len(poppler_group) != 1 or len(mineru_group) != 1:
        return []
    poppler = poppler_group[0]
    mineru = mineru_group[0]
    if (
        poppler.geometry_source != POPPLER_LAYOUT_ADAPTER
        or poppler.render_policy != "panel_only"
        or poppler.failure_reason != _SOURCE_ORDER_UNVERIFIED_EXACT
        or poppler.confidence != 1.0
        or len(poppler.line_boxes) != 1
        or not poppler.word_boxes
        or poppler.source_block_order is None
        or len(poppler.source_line_orders) != 1
        or len(poppler.source_word_orders) != len(poppler.word_boxes)
        or not _orders_are_strictly_increasing(poppler.source_word_orders)
        or not _hybrid_replace_group_is_safe(mineru_group, page_protected)
        or mineru.confidence != 1.0
        or len(mineru.line_boxes) != 1
        or poppler.page != mineru.page
        or not _boxes_are_near_duplicates(poppler.bbox, mineru.bbox)
        or any(
            not _box_contains(mineru.bbox, word_box)
            for word_box in poppler.word_boxes
        )
    ):
        return []
    promoted = poppler.model_copy(deep=True)
    promoted.render_policy = "replace"
    promoted.failure_reason = None
    return [promoted]


def _hybrid_replace_group_is_safe(
    regions: list[TranslationLayoutRegion],
    page_protected: dict[int, list[NormalizedBox]],
) -> bool:
    if not regions or [region.flow_order for region in regions] != list(
        range(len(regions))
    ):
        return False
    for index, region in enumerate(regions):
        if (
            (index > 0 and region.page < regions[index - 1].page)
            or region.rotation != 0
            or region.render_policy != "replace"
            or region.failure_reason is not None
            or region.confidence < REPLACE_CONFIDENCE
            or not region.line_boxes
            or any(not _box_contains(region.bbox, box) for box in region.line_boxes)
            or any(
                _boxes_intersect(region.bbox, protected)
                for protected in (
                    *page_protected.get(region.page, []),
                    *region.protected_boxes,
                )
            )
        ):
            return False
    return True


def _box_contains(
    outer: NormalizedBox,
    inner: NormalizedBox,
    *,
    epsilon: float = 0.003,
) -> bool:
    return (
        inner.x0 >= outer.x0 - epsilon
        and inner.y0 >= outer.y0 - epsilon
        and inner.x1 <= outer.x1 + epsilon
        and inner.y1 <= outer.y1 + epsilon
    )


def _normalized_box_is_valid(box: NormalizedBox) -> bool:
    values = (box.x0, box.y0, box.x1, box.y1)
    return (
        all(math.isfinite(value) and 0 <= value <= 1 for value in values)
        and box.x0 < box.x1
        and box.y0 < box.y1
    )


def _quality_from_regions(
    blocks: list[Block],
    regions: list[TranslationLayoutRegion],
    pages: list[TranslationLayoutPage],
) -> TranslationLayoutQuality:
    mappable_indexes = mappable_text_block_indexes(blocks)
    regions_by_block = _regions_by_block(regions)
    mapped = {
        block_index
        for block_index in mappable_indexes
        if regions_by_block.get(block_index)
        and all(
            region.failure_reason != "middle_geometry_missing"
            for region in regions_by_block[block_index]
        )
    }
    replaceable = {
        block_index
        for block_index, group in regions_by_block.items()
        if group and all(region.render_policy == "replace" for region in group)
    }
    panel_only = {
        block_index
        for block_index, group in regions_by_block.items()
        if any(region.render_policy == "panel_only" for region in group)
    }
    protected = {
        block_index
        for block_index, group in regions_by_block.items()
        if any(region.render_policy == "preserve" for region in group)
    }
    confidences = [
        region.confidence
        for region in regions
        if region.block_index in mappable_indexes
        and region.failure_reason != "middle_geometry_missing"
    ]
    failure_counts: dict[str, int] = defaultdict(int)
    for region in regions:
        if region.failure_reason:
            failure_counts[region.failure_reason] += 1
    page_protected = {page.page: page.protected_boxes for page in pages}
    overlap_blocks = {
        region.block_index
        for region in regions
        if region.block_index in mappable_indexes
        and any(
            _boxes_intersect(region.bbox, box)
            for box in (
                *page_protected.get(region.page, []),
                *region.protected_boxes,
            )
        )
    }
    unmapped = sorted(mappable_indexes - mapped)
    return TranslationLayoutQuality(
        mappable_count=len(mappable_indexes),
        mapped_count=len(mapped),
        replaceable_count=len(replaceable),
        panel_only_count=len(panel_only),
        unmapped_count=len(unmapped),
        mapped_ratio=round(len(mapped) / len(mappable_indexes), 3)
        if mappable_indexes
        else 0,
        average_confidence=round(sum(confidences) / len(confidences), 3)
        if confidences
        else 0,
        protected_overlap_count=len(overlap_blocks),
        protected_count=len(protected),
        unmapped_block_indexes=unmapped,
        failure_counts=dict(sorted(failure_counts.items())),
    )


def _downgrade_unsafe_replace_blocks(
    regions: list[TranslationLayoutRegion],
    pages: list[TranslationLayoutPage],
) -> None:
    """Fail closed for blocks whose geometry cannot be replaced as a whole."""
    page_protected = {page.page: page.protected_boxes for page in pages}
    for group in _regions_by_block(regions).values():
        if any(
            region.failure_reason == "cross_page_geometry_ambiguous"
            for region in group
        ):
            reason = "cross_page_geometry_ambiguous"
        elif any(
            any(
                _boxes_intersect(region.bbox, protected)
                for protected in (
                    *page_protected.get(region.page, []),
                    *region.protected_boxes,
                )
            )
            for region in group
        ):
            reason = "protected_overlap"
        else:
            continue
        for region in group:
            if region.render_policy == "replace":
                region.render_policy = "panel_only"
                region.failure_reason = reason


def translation_layout_from_pdf_map(
    blocks: list[Block],
    pdf_path: Path,
    mapping: dict,
    *,
    adapter: str = LEGACY_PDF_MAP_ADAPTER,
    adapter_version: str = LEGACY_PDF_MAP_ADAPTER_VERSION,
) -> TranslationLayout:
    """Convert the existing block/PDF map into the versioned layout contract."""
    page_count = int(mapping.get("page_count") or 0)
    if page_count <= 0:
        raise ValueError("PDF mapping has no pages")

    block_by_index = {block.index: block for block in blocks}
    page_sizes: dict[int, tuple[float, float]] = {}
    raw_mappings = mapping.get("mappings")
    if not isinstance(raw_mappings, list):
        raise ValueError("PDF mapping mappings must be a list")

    grouped: list[tuple[int, int, float, list[dict]]] = []
    for raw_mapping in raw_mappings:
        if not isinstance(raw_mapping, dict):
            continue
        try:
            block_index = int(raw_mapping["block_index"])
            confidence = float(raw_mapping.get("confidence") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        boxes = raw_mapping.get("boxes")
        if block_index not in block_by_index or not isinstance(boxes, list):
            continue
        boxes_by_page: dict[int, list[dict]] = defaultdict(list)
        for raw_box in boxes:
            if not isinstance(raw_box, dict):
                continue
            try:
                page = int(raw_box["page"])
                width = float(raw_box["page_width"])
                height = float(raw_box["page_height"])
            except (KeyError, TypeError, ValueError):
                continue
            if page < 1 or page > page_count or width <= 0 or height <= 0:
                continue
            page_sizes[page] = (width, height)
            boxes_by_page[page].append(raw_box)
        for page, page_boxes in sorted(boxes_by_page.items()):
            grouped.append((block_index, page, confidence, page_boxes))

    default_size = next(iter(page_sizes.values()), (612.0, 792.0))
    pages = [
        TranslationLayoutPage(
            page=page,
            width=page_sizes.get(page, default_size)[0],
            height=page_sizes.get(page, default_size)[1],
        )
        for page in range(1, page_count + 1)
    ]

    regions: list[TranslationLayoutRegion] = []
    flow_orders: dict[int, int] = defaultdict(int)
    for block_index, page, confidence, raw_boxes in sorted(grouped, key=lambda item: (item[0], item[1])):
        width, height = page_sizes[page]
        line_boxes = [_normalize_box(raw_box, width, height) for raw_box in raw_boxes]
        bbox = _union_boxes(line_boxes)
        flow_order = flow_orders[block_index]
        flow_orders[block_index] += 1
        render_policy: RenderPolicy = "panel_only"
        failure_reason = (
            "low_confidence" if confidence < REPLACE_CONFIDENCE else "legacy_mapping_unverified"
        )
        regions.append(
            TranslationLayoutRegion(
                region_id=_stable_region_id(block_index, page, flow_order, bbox),
                block_index=block_index,
                page=page,
                flow_order=flow_order,
                kind=str(block_by_index[block_index].type),
                bbox=bbox,
                line_boxes=line_boxes,
                protected_boxes=[],
                rotation=0,
                confidence=round(max(0.0, min(confidence, 1.0)), 3),
                render_policy=render_policy,
                failure_reason=failure_reason,
                geometry_source=adapter,
            )
        )

    confidences = [region.confidence for region in regions]
    mapped_blocks = {region.block_index for region in regions}
    replaceable_blocks = {
        region.block_index for region in regions if region.render_policy == "replace"
    }
    panel_only_blocks = {
        region.block_index for region in regions if region.render_policy == "panel_only"
    }
    mappable_indexes = {
        block.index
        for block in blocks
        if block.type in ("heading", "paragraph") and block.original.strip()
    }
    mapped_mappable = mapped_blocks & mappable_indexes
    mappable_count = len(mappable_indexes)
    mapped_count = len(mapped_mappable)
    unmapped_count = max(mappable_count - mapped_count, 0)
    mapped_ratio = mapped_count / mappable_count if mappable_count else 0.0
    average_confidence = sum(confidences) / len(confidences) if confidences else 0.0

    source_hash = source_pdf_sha256(pdf_path)
    block_hash = block_source_sha256(blocks)
    failure_counts: dict[str, int] = defaultdict(int)
    for region in regions:
        if region.failure_reason:
            failure_counts[region.failure_reason] += 1
    unmapped_indexes = sorted(
        block.index
        for block in blocks
        if block.index in mappable_indexes and block.index not in mapped_mappable
    )

    return TranslationLayout(
        cache_key=translation_layout_cache_key(
            source_hash,
            block_hash,
            adapter=adapter,
            adapter_version=adapter_version,
        ),
        source_pdf_sha256=source_hash,
        block_source_sha256=block_hash,
        adapter=adapter,
        adapter_version=adapter_version,
        pdf_url=str(mapping.get("pdf_url") or f"/assets/{pdf_path.parent.name}/{pdf_path.name}"),
        page_count=page_count,
        pages=pages,
        regions=regions,
        quality=TranslationLayoutQuality(
            mappable_count=mappable_count,
            mapped_count=mapped_count,
            replaceable_count=len(replaceable_blocks),
            panel_only_count=len(panel_only_blocks),
            unmapped_count=unmapped_count,
            mapped_ratio=round(mapped_ratio, 3),
            average_confidence=round(average_confidence, 3),
            protected_overlap_count=0,
            protected_count=0,
            unmapped_block_indexes=unmapped_indexes,
            failure_counts=dict(sorted(failure_counts.items())),
        ),
        warnings=["legacy_pdf_map_adapter"],
    )


def _normalize_box(raw_box: dict, width: float, height: float) -> NormalizedBox:
    try:
        x0 = float(raw_box["x0"]) / width
        y0 = float(raw_box["y0"]) / height
        x1 = float(raw_box["x1"]) / width
        y1 = float(raw_box["y1"]) / height
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise ValueError("invalid PDF mapping box") from exc
    return NormalizedBox(x0=x0, y0=y0, x1=x1, y1=y1)


def _union_boxes(boxes: list[NormalizedBox]) -> NormalizedBox:
    if not boxes:
        raise ValueError("cannot union empty boxes")
    return NormalizedBox(
        x0=min(box.x0 for box in boxes),
        y0=min(box.y0 for box in boxes),
        x1=max(box.x1 for box in boxes),
        y1=max(box.y1 for box in boxes),
    )


def _stable_region_id(
    block_index: int,
    page: int,
    flow_order: int,
    bbox: NormalizedBox,
) -> str:
    canonical = (
        f"{block_index}:{page}:{flow_order}:"
        f"{bbox.x0:.6f}:{bbox.y0:.6f}:{bbox.x1:.6f}:{bbox.y1:.6f}"
    )
    suffix = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"b{block_index}-p{page}-r{flow_order}-{suffix}"


@dataclass(frozen=True)
class _LayoutToken:
    norm: str
    page: int
    block_reading_order: int
    line: PdfLayoutLine
    word: PdfLayoutWord


def _split_run_in_prose_tokens(
    tokens: list[_LayoutToken],
    page_width: float,
) -> list[list[_LayoutToken]]:
    """Keep a run-in first line from expanding across later full-width lines."""
    tokens_by_line: dict[int, list[_LayoutToken]] = {}
    for token in tokens:
        tokens_by_line.setdefault(token.line.reading_order, []).append(token)
    line_groups = list(tokens_by_line.values())
    if len(line_groups) < 2:
        return [tokens]

    first_x0 = min(token.word.bbox.x0 for token in line_groups[0])
    later_x0 = min(token.word.bbox.x0 for group in line_groups[1:] for token in group)
    line_heights = [
        token_group[0].line.bbox.y1 - token_group[0].line.bbox.y0
        for token_group in line_groups
    ]
    geometry_threshold = max(
        page_width * _RUN_IN_FIRST_LINE_MIN_OFFSET,
        max(line_heights) * 4,
    )
    if first_x0 - later_x0 <= geometry_threshold:
        return [tokens]
    return [line_groups[0], [token for group in line_groups[1:] for token in group]]


def _poppler_region_has_target_evidence(
    tokens: list[_LayoutToken],
    target_tokens: list[str],
) -> bool:
    """Reject source-flow fragments that do not locally belong to the block."""
    candidate_tokens = [token.norm for token in tokens]
    if not candidate_tokens or not target_tokens:
        return False
    if "".join(candidate_tokens) in "".join(target_tokens):
        return True
    width = len(candidate_tokens)
    if width <= len(target_tokens) and any(
        target_tokens[offset : offset + width] == candidate_tokens
        for offset in range(len(target_tokens) - width + 1)
    ):
        return True
    equal_count = sum(
        opcode.src_end - opcode.src_start
        for opcode in Indel.opcodes(target_tokens, candidate_tokens)
        if opcode.tag == "equal"
    )
    if equal_count / width < REPLACE_CONFIDENCE:
        return False
    return (
        fuzz.partial_ratio(
            " ".join(target_tokens),
            " ".join(candidate_tokens),
        )
        / 100
        >= REPLACE_CONFIDENCE
    )


def _poppler_group_covers_target(
    groups: list[list[_LayoutToken]],
    target_tokens: list[str],
) -> bool:
    """Require retained Poppler regions to account for the complete block text."""
    candidate_tokens = [token.norm for group in groups for token in group]
    return bool(target_tokens) and "".join(candidate_tokens) == "".join(target_tokens)


@dataclass(frozen=True)
class _MinerUContentEntry:
    page: int
    kind: str
    text: str
    bbox: NormalizedBox
    line_boxes: list[NormalizedBox]
    protected_boxes: list[NormalizedBox]
    protected: bool
    rotation: Literal[0, 90, 180, 270]
    authoritative: bool


def _layout_tokens(document: PdfLayoutDocument) -> list[_LayoutToken]:
    tokens: list[_LayoutToken] = []
    for page in document.pages:
        for block in page.blocks:
            for line in block.lines:
                normalized_words = [
                    unicodedata.normalize("NFKC", word.text)
                    .lower()
                    .replace("-\n", "")
                    for word in line.words
                ]
                for word, normalized_word in zip(
                    line.words,
                    normalized_words,
                    strict=True,
                ):
                    for match in _LAYOUT_TOKEN_RE.finditer(normalized_word):
                        tokens.append(
                            _LayoutToken(
                                norm=match.group(0),
                                page=page.page,
                                block_reading_order=block.reading_order,
                                line=line,
                                word=word,
                            )
                        )
    return tokens


def _poppler_tokens_form_single_visual_line(tokens: list[_LayoutToken]) -> bool:
    """Recognize one left-to-right line even when Poppler split its blocks."""
    if not tokens or len({token.page for token in tokens}) != 1:
        return False
    word_tokens: dict[int, _LayoutToken] = {}
    for token in tokens:
        word_tokens.setdefault(token.word.reading_order, token)
    ordered = list(word_tokens.values())
    if not ordered or not _orders_are_strictly_increasing(list(word_tokens)):
        return False
    if any(
        ordered[index].block_reading_order
        < ordered[index - 1].block_reading_order
        or ordered[index].line.reading_order
        < ordered[index - 1].line.reading_order
        or ordered[index].word.bbox.x0 < ordered[index - 1].word.bbox.x1
        for index in range(1, len(ordered))
    ):
        return False
    common_y0 = max(token.word.bbox.y0 for token in ordered)
    common_y1 = min(token.word.bbox.y1 for token in ordered)
    minimum_height = min(
        token.word.bbox.y1 - token.word.bbox.y0 for token in ordered
    )
    return minimum_height > 0 and (common_y1 - common_y0) / minimum_height >= 0.8


def _normalize_layout_tokens(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).lower().replace("-\n", "")
    normalized = re.sub(r"\s+", " ", normalized)
    return _LAYOUT_TOKEN_RE.findall(normalized)


def _match_layout_tokens(
    target_tokens: list[str],
    tokens: list[_LayoutToken],
    cursor: int,
    consumed_token_indexes: set[int],
) -> tuple[int, int, float] | None:
    return _match_layout_tokens_canonical(
        target_tokens,
        tokens,
        cursor,
        consumed_token_indexes,
    )


def _match_layout_tokens_canonical(
    target_tokens: list[str],
    tokens: list[_LayoutToken],
    cursor: int,
    consumed_token_indexes: set[int],
) -> tuple[int, int, float] | None:
    if not target_tokens or not tokens:
        return None
    if len(target_tokens) == 1:
        target = target_tokens[0]
        for index in range(max(cursor, 0), len(tokens)):
            if index not in consumed_token_indexes and tokens[index].norm == target:
                return index, index + 1, 1.0
        return None
    stopwords = {"a", "an", "and", "of", "in", "on", "the", "to", "with"}
    preferred_anchor_options = [
        (index, token)
        for index, token in enumerate(target_tokens)
        if token not in stopwords
    ] or list(enumerate(target_tokens))
    all_anchor_options = list(enumerate(target_tokens))
    anchor_positions: dict[str, list[int]] = {
        token: [] for _, token in all_anchor_options
    }
    for index in range(max(cursor, 0), len(tokens)):
        token = tokens[index].norm
        if token in anchor_positions:
            anchor_positions[token].append(index)
    present_anchor_options = [
        item for item in preferred_anchor_options if anchor_positions[item[1]]
    ] or [item for item in all_anchor_options if anchor_positions[item[1]]]
    if not present_anchor_options:
        return None
    anchor_offset, anchor = min(
        present_anchor_options,
        key=lambda item: (len(anchor_positions[item[1]]), item[0]),
    )
    starts = sorted(
        {
            index - anchor_offset
            for index in anchor_positions[anchor]
            if index - anchor_offset >= cursor
        }
    )

    target_length = len(target_tokens)
    for start in starts:
        end = start + target_length
        if end > len(tokens) or not consumed_token_indexes.isdisjoint(range(start, end)):
            continue
        if [token.norm for token in tokens[start:end]] == target_tokens:
            return start, end, 1.0

    fuzzy_starts = sorted(
        {
            nearby
            for start in starts
            for nearby in range(start - 2, start + 3)
            if cursor <= nearby < len(tokens)
        }
    )[:_MAX_LAYOUT_FUZZY_CANDIDATES]

    target = " ".join(target_tokens)
    min_length = max(2, int(target_length * 0.78))
    max_length = max(min_length, int(target_length * 1.35) + 3)
    preferred_lengths = sorted(
        {
            min_length,
            target_length,
            min(max_length, int(target_length * 1.1) + 1),
            max_length,
        }
    )
    best: tuple[int, int, float] | None = None
    best_rank = -1.0
    for start in fuzzy_starts:
        if start < 0 or start >= len(tokens):
            continue
        for length in preferred_lengths:
            raw_end = min(start + length, len(tokens))
            if raw_end - start < min_length:
                continue
            candidate_tokens = [token.norm for token in tokens[start:raw_end]]
            equal_ops = [
                opcode
                for opcode in Indel.opcodes(target_tokens, candidate_tokens)
                if opcode.tag == "equal"
            ]
            if not equal_ops:
                continue
            trimmed_start = start + equal_ops[0].dest_start
            trimmed_end = start + equal_ops[-1].dest_end
            if trimmed_end - trimmed_start < min_length:
                continue
            if not consumed_token_indexes.isdisjoint(
                range(trimmed_start, trimmed_end)
            ):
                continue
            candidate = " ".join(
                token.norm for token in tokens[trimmed_start:trimmed_end]
            )
            equal_token_count = sum(
                opcode.src_end - opcode.src_start for opcode in equal_ops
            )
            token_score = (
                2 * equal_token_count
                / (target_length + (trimmed_end - trimmed_start))
            )
            character_score = fuzz.ratio(target, candidate) / 100
            score = (character_score + token_score) / 2
            length_penalty = (
                abs((trimmed_end - trimmed_start) - target_length)
                / max(target_length, 1)
                * 0.12
            )
            rank = score - length_penalty
            if best is None or rank > best_rank:
                best = (trimmed_start, trimmed_end, score)
                best_rank = rank
            if (
                score >= 0.995
                and abs((trimmed_end - trimmed_start) - target_length) <= 1
            ):
                return best
    return best


def _global_exact_layout_matches(
    target_tokens: list[str],
    tokens: list[_LayoutToken],
    token_positions: dict[str, list[int]],
    consumed_token_indexes: set[int],
) -> list[tuple[int, int, float]]:
    """Return at most two unconsumed exact matches across the full document."""
    if not target_tokens or not tokens:
        return []
    anchor_offset, anchor = min(
        enumerate(target_tokens),
        key=lambda item: (len(token_positions.get(item[1], [])), item[0]),
    )
    matches: list[tuple[int, int, float]] = []
    target_length = len(target_tokens)
    for anchor_position in token_positions.get(anchor, []):
        start = anchor_position - anchor_offset
        end = start + target_length
        if start < 0 or end > len(tokens):
            continue
        if not consumed_token_indexes.isdisjoint(range(start, end)):
            continue
        if [token.norm for token in tokens[start:end]] != target_tokens:
            continue
        matches.append((start, end, 1.0))
        if len(matches) == 2:
            break
    return matches


def _mineru_content_entries(
    content_list: list[dict],
    pages: list[dict],
) -> list[_MinerUContentEntry]:
    page_by_index = {int(page["page_idx"]): page for page in pages}
    entries: list[_MinerUContentEntry] = []
    for item in content_list:
        page_index = int(item["page_idx"])
        raw_page = page_by_index[page_index]
        content_bbox = _normalized_mineru_box(item["bbox"], 1000.0, 1000.0)
        kind = str(item.get("type") or "text")
        middle = _find_mineru_middle_block(raw_page, content_bbox, kind)
        if kind in {"image", "table", "chart", "code"}:
            entries.extend(
                _mineru_composite_entries(
                    item,
                    page_index=page_index,
                    kind=kind,
                    content_bbox=content_bbox,
                    middle=middle,
                    raw_page=raw_page,
                )
            )
            continue
        if middle is None:
            bbox = content_bbox
            line_boxes = [content_bbox]
            protected_boxes: list[NormalizedBox] = []
            rotation: Literal[0, 90, 180, 270] = 0
            authoritative = False
        else:
            page_width = float(raw_page["page_size"][0])
            page_height = float(raw_page["page_size"][1])
            bbox = _normalized_mineru_box(middle["bbox"], page_width, page_height)
            line_boxes = _mineru_line_boxes(middle, page_width, page_height) or [bbox]
            protected_boxes = _mineru_protected_boxes(middle, page_width, page_height)
            rotation = int(middle.get("angle") or 0)  # type: ignore[assignment]
            authoritative = True
        protected = kind in {"image", "table", "chart", "equation", "code", "algorithm"}
        if protected and bbox not in protected_boxes:
            protected_boxes.append(bbox)
        entries.append(
            _MinerUContentEntry(
                page=page_index,
                kind=kind,
                text=_mineru_content_text(item),
                bbox=bbox,
                line_boxes=line_boxes,
                protected_boxes=protected_boxes,
                protected=protected,
                rotation=rotation,
                authoritative=authoritative,
            )
        )
    return entries


def _mineru_composite_entries(
    item: dict,
    *,
    page_index: int,
    kind: str,
    content_bbox: NormalizedBox,
    middle: dict | None,
    raw_page: dict,
) -> list[_MinerUContentEntry]:
    page_width = float(raw_page["page_size"][0])
    page_height = float(raw_page["page_size"][1])
    body_types = {
        "image": {"image_body", "chart_body"},
        "chart": {"chart_body", "image_body"},
        "table": {"table_body"},
        "code": {"code_body", "algorithm"},
    }[kind]
    body_blocks = _mineru_descendants_of_types(middle, body_types)
    if body_blocks:
        body_boxes = [
            _normalized_mineru_box(block["bbox"], page_width, page_height)
            for block in body_blocks
            if isinstance(block.get("bbox"), list)
        ]
        body_bbox = _union_boxes(body_boxes) if body_boxes else content_bbox
        body_lines = [
            box
            for block in body_blocks
            for box in _mineru_line_boxes(block, page_width, page_height)
        ] or [body_bbox]
        body_authoritative = bool(body_boxes)
    elif middle is not None:
        body_bbox = _normalized_mineru_box(
            middle["bbox"],
            page_width,
            page_height,
        )
        body_boxes = [body_bbox]
        body_lines = [body_bbox]
        body_authoritative = True
    else:
        body_bbox = content_bbox
        body_boxes = [content_bbox]
        body_lines = [content_bbox]
        body_authoritative = False
    body_rotation = _mineru_composite_rotation(body_blocks, fallback=middle)

    entries = [
        _MinerUContentEntry(
            page=page_index,
            kind=kind,
            text=_mineru_composite_body_text(item, kind),
            bbox=body_bbox,
            line_boxes=body_lines,
            protected_boxes=body_boxes,
            protected=True,
            rotation=body_rotation,
            authoritative=body_authoritative,
        )
    ]
    field_types = {
        "image": (
            ("image_caption", "image_caption"),
            ("image_footnote", "image_footnote"),
        ),
        "chart": (
            ("chart_caption", "chart_caption"),
            ("chart_footnote", "chart_footnote"),
        ),
        "table": (
            ("table_caption", "table_caption"),
            ("table_footnote", "table_footnote"),
        ),
        "code": (
            ("code_caption", "code_caption"),
            ("code_footnote", "code_footnote"),
        ),
    }[kind]
    for field_name, middle_type in field_types:
        raw_text = item.get(field_name)
        if not isinstance(raw_text, list):
            continue
        text_parts = [
            part.strip()
            for part in raw_text
            if isinstance(part, str) and part.strip()
        ]
        if not text_parts:
            continue
        caption_blocks = _mineru_descendants_of_types(middle, {middle_type})
        geometry_count_matches = len(caption_blocks) == len(text_parts)
        for index, text in enumerate(text_parts):
            block = caption_blocks[index] if index < len(caption_blocks) else None
            if block is not None and isinstance(block.get("bbox"), list):
                caption_bbox = _normalized_mineru_box(
                    block["bbox"],
                    page_width,
                    page_height,
                )
                caption_lines = (
                    _mineru_line_boxes(block, page_width, page_height)
                    or [caption_bbox]
                )
                caption_rotation = _mineru_composite_rotation(
                    [block],
                    fallback=middle,
                )
                authoritative = geometry_count_matches
            else:
                caption_bbox = content_bbox
                caption_lines = [content_bbox]
                caption_rotation = _mineru_composite_rotation(
                    [],
                    fallback=middle,
                )
                authoritative = False
            entries.append(
                _MinerUContentEntry(
                    page=page_index,
                    kind=middle_type,
                    text=text,
                    bbox=caption_bbox,
                    line_boxes=caption_lines,
                    protected_boxes=body_boxes,
                    protected=False,
                    rotation=caption_rotation,
                    authoritative=authoritative,
                )
            )
    return entries


def _mineru_composite_rotation(
    blocks: list[dict],
    *,
    fallback: dict | None,
) -> Literal[0, 90, 180, 270]:
    raw_angles = [block.get("angle") for block in blocks if block.get("angle") is not None]
    if not raw_angles and fallback is not None and fallback.get("angle") is not None:
        raw_angles = [fallback.get("angle")]
    rotations = {int(value) for value in raw_angles}
    if not rotations:
        return 0
    if len(rotations) != 1 or next(iter(rotations)) not in {0, 90, 180, 270}:
        raise ValueError("mineru_composite_rotation_invalid")
    return next(iter(rotations))  # type: ignore[return-value]


def _mineru_composite_body_text(item: dict, kind: str) -> str:
    key = {
        "table": "table_body",
        "code": "code_body",
        "image": "content",
        "chart": "content",
    }[kind]
    value = item.get(key)
    return value.strip() if isinstance(value, str) else ""


def _mineru_descendants_of_types(
    value: dict | None,
    expected_types: set[str],
) -> list[dict]:
    if value is None:
        return []
    matches: list[dict] = []
    if str(value.get("type") or "") in expected_types:
        matches.append(value)
    for key in ("blocks", "lines", "spans"):
        for child in value.get(key, []):
            if isinstance(child, dict):
                matches.extend(_mineru_descendants_of_types(child, expected_types))
    return matches


def _match_blocks_to_mineru_content(
    blocks: list[Block],
    entries: list[_MinerUContentEntry],
) -> list[tuple[Block, _MinerUContentEntry, float, int, str | None]]:
    accepted: list[tuple[Block, int, int, float, str | None]] = []
    deferred: list[tuple[Block, int, int, float, str | None]] = []
    consumed_entry_indexes: set[int] = set()

    mappable_indexes = mappable_text_block_indexes(blocks)
    caption_blocks = [
        block
        for block in blocks
        if block.type in ("heading", "paragraph")
        and block.index in mappable_indexes
        and _is_structured_caption_block(block)
    ]
    prose_blocks = [
        block
        for block in blocks
        if block.type in ("heading", "paragraph")
        and block.index in mappable_indexes
        and not _is_structured_caption_block(block)
    ]
    protected_blocks = [
        block for block in blocks if block.type not in ("heading", "paragraph")
    ]
    for group, preserve_order in (
        (caption_blocks, False),
        (prose_blocks, True),
        (protected_blocks, True),
    ):
        cursor = 0
        for block in group:
            target_tokens = _normalize_layout_tokens(block.original)
            if not target_tokens:
                continue
            best = _best_mineru_window(
                block,
                target_tokens,
                entries,
                cursor if preserve_order else 0,
                consumed_entry_indexes,
            )
            if best is None or best[2] < _MINERU_MIN_MATCH_CONFIDENCE:
                continue
            start, end, confidence, _ = best
            if (
                confidence < 0.999
                and block.type == "heading"
                and len(target_tokens) <= _MINERU_SHORT_HEADING_TOKEN_LIMIT
                and start - cursor > _MINERU_LOW_CONFIDENCE_MAX_ENTRY_GAP
            ):
                continue
            failure_reason = (
                "merged_source_entry"
                if block.type in ("heading", "paragraph")
                and _mineru_window_has_merged_source(target_tokens, entries[start:end])
                else None
            )
            candidate = (block, start, end, confidence, failure_reason)
            if confidence >= REPLACE_CONFIDENCE:
                accepted.append(candidate)
                consumed_entry_indexes.update(range(start, end))
                if preserve_order:
                    cursor = end
            else:
                deferred.append(candidate)

    for candidate in deferred:
        _, start, end, _, _ = candidate
        if not consumed_entry_indexes.isdisjoint(range(start, end)):
            continue
        accepted.append(candidate)
        consumed_entry_indexes.update(range(start, end))

    matches: list[tuple[Block, _MinerUContentEntry, float, int, str | None]] = []
    for block, start, end, confidence, failure_reason in accepted:
        for flow_order, entry in enumerate(entries[start:end]):
            matches.append(
                (block, entry, confidence, flow_order, failure_reason)
            )
    return matches


def _is_structured_caption_block(block: Block) -> bool:
    return block.type == "paragraph" and bool(
        _STRUCTURED_CAPTION_RE.match(block.original.strip())
    )


def _best_mineru_window(
    block: Block,
    target_tokens: list[str],
    entries: list[_MinerUContentEntry],
    cursor: int,
    consumed_entry_indexes: set[int],
) -> tuple[int, int, float, float] | None:
    target = " ".join(target_tokens)
    best: tuple[int, int, float, float] | None = None
    for start in range(cursor, len(entries)):
        for size in range(1, min(3, len(entries) - start) + 1):
            end = start + size
            if not consumed_entry_indexes.isdisjoint(range(start, end)):
                continue
            raw_window = entries[start:end]
            evidence_offsets = [
                offset
                for offset, entry in enumerate(raw_window)
                if _normalize_layout_tokens(entry.text)
            ]
            if not evidence_offsets:
                continue
            if block.type in ("heading", "paragraph"):
                evidence_start = start + evidence_offsets[0]
                evidence_end = start + evidence_offsets[-1] + 1
                window = entries[evidence_start:evidence_end]
                if any(not _normalize_layout_tokens(entry.text) for entry in window):
                    continue
            else:
                evidence_start = start
                evidence_end = end
                window = raw_window
            candidate_tokens = [
                token
                for entry in window
                for token in _normalize_layout_tokens(entry.text)
            ]
            score = fuzz.ratio(target, " ".join(candidate_tokens)) / 100
            type_bonus = 0.02 if _mineru_kind_matches_block(window[0].kind, block) else 0
            rank = score + type_bonus
            if best is None or rank > best[3]:
                best = (evidence_start, evidence_end, score, rank)
    return best


def _mineru_window_has_merged_source(
    target_tokens: list[str],
    window: list[_MinerUContentEntry],
) -> bool:
    if len(window) != 1:
        return False
    candidate_tokens = _normalize_layout_tokens(window[0].text)
    if len(candidate_tokens) <= len(target_tokens):
        return False
    width = len(target_tokens)
    return any(
        candidate_tokens[offset : offset + width] == target_tokens
        for offset in range(len(candidate_tokens) - width + 1)
    )


def _mineru_content_text(item: dict) -> str:
    values: list[str] = []
    for key in (
        "text",
        "content",
        "table_body",
        "code_body",
        "equation",
        "formula",
    ):
        value = item.get(key)
        if isinstance(value, str):
            values.append(value)
    for key in (
        "image_caption",
        "image_footnote",
        "chart_caption",
        "chart_footnote",
        "table_caption",
        "table_footnote",
        "code_caption",
        "code_footnote",
        "list_items",
    ):
        value = item.get(key)
        if isinstance(value, list):
            values.extend(str(part) for part in value if isinstance(part, str))
    return " ".join(values).strip()


def _mineru_kind_matches_block(kind: str, block: Block) -> bool:
    if block.type == "heading":
        return kind in {"text", "title"}
    if block.type == "paragraph":
        if _is_structured_caption_block(block):
            return kind in _TRANSLATABLE_MINERU_AUX_TYPES
        return kind in {"text", "list", "page_footnote"}
    if block.type == "formula":
        return kind == "equation"
    if block.type == "figure":
        return kind in {"image", "chart"}
    return kind == block.type


def _find_mineru_middle_block(
    page: dict,
    content_bbox: NormalizedBox,
    content_kind: str,
) -> dict | None:
    page_width = float(page["page_size"][0])
    page_height = float(page["page_size"][1])
    candidates: list[tuple[float, int, dict]] = []
    kind_map = {
        "text": {"text", "title", "list", "index", "page_footnote"},
        "image": {"image", "image_body", "chart", "chart_body"},
        "chart": {"chart", "chart_body", "image", "image_body"},
        "table": {"table", "table_body"},
        "equation": {"interline_equation", "equation"},
        "code": {"code", "code_body", "algorithm"},
        "list": {"list", "text"},
    }
    expected_kinds = kind_map.get(content_kind, {content_kind})
    order = 0
    for key in (
        "para_blocks",
        "tables",
        "images",
        "interline_equations",
        "discarded_blocks",
    ):
        for candidate in page.get(key, []):
            if not isinstance(candidate, dict) or not isinstance(candidate.get("bbox"), list):
                continue
            candidate_bbox = _normalized_mineru_box(
                candidate["bbox"], page_width, page_height
            )
            distance = sum(
                abs(left - right)
                for left, right in zip(
                    (
                        candidate_bbox.x0,
                        candidate_bbox.y0,
                        candidate_bbox.x1,
                        candidate_bbox.y1,
                    ),
                    (
                        content_bbox.x0,
                        content_bbox.y0,
                        content_bbox.x1,
                        content_bbox.y1,
                    ),
                    strict=True,
                )
            )
            if str(candidate.get("type") or "") not in expected_kinds:
                distance += 0.25
            candidates.append((distance, order, candidate))
            order += 1
    if not candidates:
        return None
    distance, _, candidate = min(candidates, key=lambda item: (item[0], item[1]))
    return candidate if distance <= 0.35 else None


def _mineru_line_boxes(
    value: dict,
    page_width: float,
    page_height: float,
) -> list[NormalizedBox]:
    boxes: list[NormalizedBox] = []
    for line in value.get("lines", []):
        if isinstance(line, dict) and isinstance(line.get("bbox"), list):
            boxes.append(_normalized_mineru_box(line["bbox"], page_width, page_height))
    for child in value.get("blocks", []):
        if isinstance(child, dict):
            boxes.extend(_mineru_line_boxes(child, page_width, page_height))
    return boxes


def _mineru_page_protected_boxes(raw_page: dict) -> list[NormalizedBox]:
    """Collect authoritative non-text bodies without swallowing their captions."""
    page_width = float(raw_page["page_size"][0])
    page_height = float(raw_page["page_size"][1])
    protected_types = {
        "image_body",
        "chart_body",
        "table_body",
        "interline_equation",
        "equation",
        "code_body",
        "algorithm",
    }
    composite_types = {"image", "chart", "table", "code", "algorithm"}
    boxes: list[NormalizedBox] = []
    for key in ("para_blocks", "tables", "images", "interline_equations"):
        for item in raw_page.get(key, []):
            if not isinstance(item, dict):
                continue
            matches = _mineru_descendants_of_types(item, protected_types)
            item_type = str(item.get("type") or "")
            if not matches and (
                key == "interline_equations" or item_type in composite_types
            ):
                matches = [item]
            for match in matches:
                if isinstance(match.get("bbox"), list):
                    boxes.append(
                        _normalized_mineru_box(
                            match["bbox"],
                            page_width,
                            page_height,
                        )
                    )
            for inline in _mineru_descendants_of_types(item, {"inline_equation"}):
                if (
                    _mineru_inline_equation_needs_pixel_protection(inline)
                    and isinstance(inline.get("bbox"), list)
                ):
                    boxes.append(
                        _normalized_mineru_box(
                            inline["bbox"],
                            page_width,
                            page_height,
                        )
                    )
    return _unique_normalized_boxes(boxes)


def _mineru_inline_equation_needs_pixel_protection(value: dict) -> bool:
    content = str(value.get("content") or value.get("text") or "").strip()
    if not content:
        return True
    if re.fullmatch(r"\[\s*\d+(?:\s*[,;\-]\s*\d+)*\s*\]", content):
        return False
    if re.fullmatch(r"\^\{?\d+\*?\}?", content):
        return False
    if re.fullmatch(
        r"[+\-−]?\s*(?:\d+(?:\.\d+)?|\.\d+)\s*(?:\\)?[%％]",
        content,
    ):
        return False
    return True


def _mineru_geometry_segments(
    entry: _MinerUContentEntry,
    *,
    page_count: int,
) -> list[tuple[int, NormalizedBox, list[NormalizedBox], list[NormalizedBox]]]:
    """Keep provider geometry on its explicit page; never infer another page."""
    if entry.page + 1 > page_count:
        raise ValueError("mineru_region_page_out_of_range")
    return [
        (
            entry.page + 1,
            entry.bbox,
            entry.line_boxes or [entry.bbox],
            entry.protected_boxes,
        )
    ]


def _mineru_geometry_is_discontinuous(entry: _MinerUContentEntry) -> bool:
    line_boxes = entry.line_boxes
    for previous, box in zip(line_boxes, line_boxes[1:]):
        overlap = max(0.0, min(previous.x1, box.x1) - max(previous.x0, box.x0))
        minimum_width = min(previous.x1 - previous.x0, box.x1 - box.x0)
        if (
            minimum_width > 0
            and overlap / minimum_width >= 0.75
            and previous.y0 >= 0.75
            and box.y1 <= 0.60
            and previous.y0 - box.y0 >= 0.25
        ):
            return True
    return False


def _boxes_intersect(left: NormalizedBox, right: NormalizedBox) -> bool:
    return min(left.x1, right.x1) > max(left.x0, right.x0) and min(
        left.y1,
        right.y1,
    ) > max(left.y0, right.y0)


def _unique_normalized_boxes(boxes: list[NormalizedBox]) -> list[NormalizedBox]:
    unique: dict[tuple[float, float, float, float], NormalizedBox] = {}
    for box in boxes:
        unique[(box.x0, box.y0, box.x1, box.y1)] = box
    return list(unique.values())


def _mineru_protected_boxes(
    value: dict,
    page_width: float,
    page_height: float,
) -> list[NormalizedBox]:
    protected_types = {
        "image",
        "image_body",
        "chart",
        "chart_body",
        "table",
        "table_body",
        "inline_equation",
        "interline_equation",
        "code",
        "code_body",
        "algorithm",
    }
    boxes: list[NormalizedBox] = []
    value_type = str(value.get("type") or "")
    if (
        value_type in protected_types
        and (
            value_type != "inline_equation"
            or _mineru_inline_equation_needs_pixel_protection(value)
        )
        and isinstance(value.get("bbox"), list)
    ):
        boxes.append(_normalized_mineru_box(value["bbox"], page_width, page_height))
    for key in ("lines", "spans", "blocks"):
        for child in value.get(key, []):
            if isinstance(child, dict):
                boxes.extend(_mineru_protected_boxes(child, page_width, page_height))
    unique: dict[tuple[float, float, float, float], NormalizedBox] = {}
    for box in boxes:
        unique[(box.x0, box.y0, box.x1, box.y1)] = box
    return list(unique.values())


def _normalized_mineru_box(raw: list, width: float, height: float) -> NormalizedBox:
    return NormalizedBox(
        x0=float(raw[0]) / width,
        y0=float(raw[1]) / height,
        x1=float(raw[2]) / width,
        y1=float(raw[3]) / height,
    )


def _source_pdf_page_count(pdf_path: Path) -> int:
    from .pdf_mapping import _pdf_page_count

    page_count = _pdf_page_count(pdf_path, [])
    if page_count <= 0:
        raise ValueError("source_pdf_page_count_unavailable")
    return page_count


def _current_adapter_version(adapter: str) -> str | None:
    if adapter == LEGACY_PDF_MAP_ADAPTER:
        return LEGACY_PDF_MAP_ADAPTER_VERSION
    if adapter == POPPLER_LAYOUT_ADAPTER:
        return POPPLER_LAYOUT_ADAPTER_VERSION
    if adapter == MINERU_LAYOUT_ADAPTER:
        return MINERU_TRANSLATION_LAYOUT_ADAPTER_VERSION
    if adapter == HYBRID_LAYOUT_ADAPTER:
        return HYBRID_LAYOUT_ADAPTER_VERSION
    return None
