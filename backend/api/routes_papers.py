"""论文路由 — POST /papers（选定后提取存盘）、GET /papers、GET /papers/{id}。"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, Field

from ..extraction.blocks import Block, PaperDocument
from ..extraction.extract import extract_paper
from ..extraction.local_pdf import LocalPdfExtractionError, extract_blocks_from_local_pdf
from ..extraction.mineru import (
    MINERU_LAYOUT_ADAPTER,
    MinerUClient,
    MinerUError,
    MinerUStructuredResult,
    markdown_to_blocks,
)
from ..extraction.pdf_layout import POPPLER_LAYOUT_ADAPTER, PdfLayoutError, extract_pdf_layout
from ..extraction.pdf_mapping import PDF_MAPPING_VERSION, _pdf_page_count, build_block_pdf_map, ensure_pdf
from ..extraction.quality import assess_extraction_quality
from ..extraction.source_pdf import SOURCE_PDF_MAX_BYTES, SourcePdfError, download_source_pdf
from ..extraction.translation_layout import (
    HYBRID_LAYOUT_ADAPTER,
    TranslationLayout,
    bind_mineru_layout_source,
    safe_translation_layout_metrics,
    source_pdf_sha256,
    translation_layout_cache_matches,
    translation_layout_from_hybrid,
    translation_layout_from_mineru,
    translation_layout_from_pdf_layout,
)
from ..llm.config import get_config, resolve_mineru_config
from ..storage.db import get_paper, insert_paper, list_papers, update_status
from ..storage.files import (
    DATA_DIR,
    build_paper_note_summary,
    ensure_paper_dir,
    load_document,
    load_block_pdf_map,
    load_mineru_layout_artifact_bundle,
    load_mineru_layout_provenance,
    load_mineru_source_meta,
    load_translation_layout,
    now_iso,
    paper_dir,
    save_block_pdf_map,
    save_document,
    save_extraction_quality,
    save_mineru_layout_artifacts,
    save_translation_layout,
)
from .routes_config import _require_admin

router = APIRouter(tags=["papers"])
logger = logging.getLogger(__name__)

_ARXIV_ID_RE = re.compile(r"^(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?$", re.I)
_PRECISE_LAYOUT_MIN_RATIO = 0.90
_PRECISE_LAYOUT_MIN_CONFIDENCE = 0.90
_TRANSLATION_LAYOUT_FAILURE_TTL_SECONDS = 5.0
_TRANSLATION_LAYOUT_LOCKS: dict[str, asyncio.Lock] = {}
_TRANSLATION_LAYOUT_FAILURES: dict[str, tuple[float, int, object]] = {}
_TRANSLATION_LAYOUT_TASKS: dict[
    tuple[str, bool], asyncio.Task[TranslationLayout]
] = {}


def _is_arxiv_id(value: str) -> bool:
    return bool(_ARXIV_ID_RE.match(value.strip()))


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _file_name_from_url(value: str) -> str | None:
    path = urlparse(value).path.rstrip("/")
    if not path:
        return None
    name = path.rsplit("/", 1)[-1].strip()
    return name or None


def _mineru_paper_id(file_url: str, page_range: str | None, mode: str = "agent_lite") -> str:
    identity = f"{file_url}|{page_range or ''}"
    if mode != "agent_lite":
        identity = f"{identity}|{mode}"
    digest = sha1(identity.encode("utf-8")).hexdigest()[:12]
    return f"mineru-{digest}"


def _local_file_paper_id(file_name: str, digest: str) -> str:
    name_part = re.sub(r"[^a-z0-9]+", "-", Path(file_name).stem.lower()).strip("-")[:32]
    suffix = digest[:12]
    return f"local-{name_part}-{suffix}" if name_part else f"local-{suffix}"


def _safe_upload_file_name(value: str | None) -> str:
    name = Path(value or "uploaded.pdf").name.strip()
    return name or "uploaded.pdf"


def _title_from_blocks(blocks) -> str | None:
    for block in blocks:
        if block.type == "heading" and block.original.strip():
            return block.original.strip()[:200]
    return None


def _clear_block_pdf_map_cache(arxiv_id: str) -> None:
    _TRANSLATION_LAYOUT_FAILURES.pop(arxiv_id, None)
    for name in (
        "block_to_pdf_map.json",
        "translation_layout.json",
        "mineru_middle.json",
        "mineru_content_list.json",
        "mineru_layout_meta.json",
    ):
        try:
            (paper_dir(arxiv_id) / name).unlink()
        except FileNotFoundError:
            pass


def _save_document_with_quality(doc: PaperDocument, source: str) -> None:
    save_document(doc)
    report = assess_extraction_quality(doc.blocks, source).to_dict()
    if doc.source_page_range:
        report["document_scope"] = "partial"
        report["complete_document"] = False
        report["source_page_range"] = doc.source_page_range
        report["findings"].append(
            {
                "code": "partial_page_range",
                "severity": "warning",
                "detail": f"仅提取源 PDF 页码范围 {doc.source_page_range}，不能视为全文。",
                "block_index": None,
            }
        )
    save_extraction_quality(
        doc.paper_id,
        report,
    )


def _local_pdf_requires_full_ocr(layout: object) -> bool:
    """Return whether Poppler found a page unsafe for plain-text import."""
    pages = getattr(layout, "pages", ())
    return not pages or any(
        not getattr(page, "blocks", ()) or getattr(page, "rotation", 0) != 0
        for page in pages
    )


async def _save_uploaded_pdf_tmp(upload: UploadFile) -> tuple[Path, str, int]:
    uploads_dir = DATA_DIR / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(uploads_dir), prefix="local-pdf-", suffix=".pdf")
    digest = sha1()
    size = 0
    header = bytearray()
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > SOURCE_PDF_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="PDF 文件超过 200MB 限制。")
                if len(header) < 1024:
                    header.extend(chunk[: 1024 - len(header)])
                digest.update(chunk)
                await asyncio.to_thread(out.write, chunk)
            await asyncio.to_thread(_flush_and_sync_upload, out)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    if size > 0 and b"%PDF-" not in bytes(header):
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise HTTPException(status_code=400, detail="上传文件不是有效的 PDF。")
    return Path(tmp_name), digest.hexdigest(), size


def _flush_and_sync_upload(output) -> None:
    output.flush()
    os.fsync(output.fileno())


async def _pdf_path_for_document(arxiv_id: str, doc: PaperDocument) -> Path:
    pdf_path = paper_dir(arxiv_id) / "original.pdf"
    if pdf_path.exists() and pdf_path.stat().st_size > 0:
        return pdf_path
    if _is_arxiv_id(arxiv_id):
        return await ensure_pdf(arxiv_id)
    raise FileNotFoundError(f"源 PDF 不存在: {pdf_path.name}")


def _layout_meets_poppler_gate(
    layout: TranslationLayout,
    blocks: list[Block],
) -> bool:
    quality = layout.quality
    safety = safe_translation_layout_metrics(blocks, layout)
    return (
        quality.mapped_ratio >= _PRECISE_LAYOUT_MIN_RATIO
        and quality.average_confidence >= _PRECISE_LAYOUT_MIN_CONFIDENCE
        and safety["safe_coverage"] >= _PRECISE_LAYOUT_MIN_RATIO
    )


def _layout_mineru_generation_matches(
    arxiv_id: str,
    layout: TranslationLayout,
) -> bool:
    if layout.adapter not in {MINERU_LAYOUT_ADAPTER, HYBRID_LAYOUT_ADAPTER}:
        return True
    source = next(
        (item for item in layout.sources if item.adapter == MINERU_LAYOUT_ADAPTER),
        None,
    )
    if source is None or source.generation is None:
        return False
    provenance = load_mineru_layout_provenance(
        arxiv_id,
        expected_source_pdf_sha256=layout.source_pdf_sha256,
    )
    if provenance is None:
        return False
    return (
        provenance.get("generation") == source.generation
        and provenance.get("is_ocr") == source.is_ocr
    )


def _select_mineru_fallback_layout(
    poppler_layout: TranslationLayout | None,
    mineru_layout: TranslationLayout,
    pdf_path: Path,
    blocks: list[Block],
    *,
    poppler_requires_ocr: bool,
    generation: str | None,
    is_ocr: bool | None,
) -> TranslationLayout:
    bound_mineru = bind_mineru_layout_source(
        mineru_layout,
        generation=generation,
        is_ocr=is_ocr,
    )
    if (
        poppler_layout is None
        or poppler_requires_ocr
        or poppler_layout.quality.replaceable_count == 0
    ):
        return bound_mineru
    return translation_layout_from_hybrid(
        blocks,
        pdf_path,
        poppler_layout,
        bound_mineru,
        mineru_generation=generation,
        mineru_is_ocr=is_ocr,
    )


def _save_mineru_result(
    arxiv_id: str,
    result: MinerUStructuredResult,
    pdf_path: Path,
    *,
    is_ocr: bool | None = None,
) -> str | None:
    if result.layout is None or result.content_list is None:
        return None
    return save_mineru_layout_artifacts(
        arxiv_id,
        result.layout,
        result.content_list,
        source_pdf_sha256=source_pdf_sha256(pdf_path),
        is_ocr=is_ocr,
    )


def _stored_mineru_result(
    arxiv_id: str,
    blocks: list[Block],
    pdf_path: Path,
) -> tuple[MinerUStructuredResult | None, dict[str, object] | None]:
    source_hash = source_pdf_sha256(pdf_path)
    bundle = load_mineru_layout_artifact_bundle(
        arxiv_id,
        expected_source_pdf_sha256=source_hash,
    )
    if bundle is not None:
        layout, content_list, meta = bundle
        return (
            MinerUStructuredResult(
                markdown="",
                blocks=blocks,
                layout=layout,
                content_list=content_list,
            ),
            meta,
        )
    source_meta = load_mineru_source_meta(
        arxiv_id,
        expected_source_pdf_sha256=source_hash,
    )
    return None, source_meta


async def _parse_pdf_with_standard_mineru(
    pdf_path: Path,
    *,
    is_ocr: bool,
) -> MinerUStructuredResult:
    config = resolve_mineru_config(get_config().mineru)
    if not config.enabled:
        raise MinerUError("MinerU provider is disabled")
    if config.mode != "standard":
        raise MinerUError("MinerU standard mode is required for precise layout")
    if not config.api_token:
        raise MinerUError("MinerU standard API token is missing")
    config.is_ocr = is_ocr
    config.page_range = None
    result = await MinerUClient(config).parse_standard_file(pdf_path)
    if result.layout is None or result.content_list is None:
        raise MinerUError("MinerU result does not contain stable layout artifacts")
    return result


async def _warm_translation_layout(arxiv_id: str, doc: PaperDocument) -> None:
    try:
        await _build_or_load_translation_layout(arxiv_id, doc)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        logger.warning("原位译文版面暂不可用: %s (%s)", arxiv_id, exc.detail)


class CreatePaperRequest(BaseModel):
    """用户从候选列表中选定一篇后提交。"""

    arxiv_id: str
    title: str
    authors: list[str] = []


class CreateMinerUPaperRequest(BaseModel):
    """用当前 MinerU 模式解析远程文件 URL。"""

    url: str
    title: str = ""
    file_name: str | None = None
    page_range: str | None = None
    language: str | None = None


class PaperMeta(BaseModel):
    arxiv_id: str
    title: str
    authors: list[str]
    source: str
    status: str
    created_at: str | None = None
    selection_note_count: int = 0
    has_paper_note: bool = False
    note_updated_at: str | None = None
    note_preview: str = ""


@router.post("/papers", response_model=PaperMeta)
async def create_paper(req: CreatePaperRequest) -> PaperMeta:
    """选定候选 → 拉取并提取 → 存盘。返回论文元数据。"""
    arxiv_id = req.arxiv_id.strip()
    if not _is_arxiv_id(arxiv_id):
        raise HTTPException(
            status_code=400,
            detail="MVP 仅支持可提取的 arXiv 论文，请选择带 arXiv ID 的候选。",
        )

    lock = _TRANSLATION_LAYOUT_LOCKS.setdefault(arxiv_id, asyncio.Lock())
    async with lock:
        existing = await get_paper(arxiv_id)
        if existing and await asyncio.to_thread(load_document, arxiv_id) is not None:
            try:
                await ensure_pdf(arxiv_id)
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"PDF 下载失败：{exc}") from exc
            return PaperMeta(
                arxiv_id=existing["arxiv_id"],
                title=existing["title"],
                authors=existing.get("authors", []),
                source=existing["source"],
                status=existing["status"],
                created_at=existing.get("created_at"),
            )

        # 原位阅读以 PDF 为证据层，必须先持久化源文件再提取文本。
        try:
            await ensure_pdf(arxiv_id)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"PDF 下载失败：{exc}") from exc

        blocks, source = await extract_paper(arxiv_id)
        if not blocks:
            raise HTTPException(
                status_code=422,
                detail=f"提取失败：ar5iv 和 LaTeX 均不可用 (arxiv_id={arxiv_id})",
            )

        doc = PaperDocument(
            paper_id=arxiv_id,
            title=req.title,
            source=source,
            extracted_at=now_iso(),
            blocks=blocks,
        )
        await asyncio.to_thread(_save_document_with_quality, doc, source)
        await insert_paper(
            arxiv_id=arxiv_id,
            title=req.title,
            authors=req.authors,
            source=source,
            file_path=str(paper_dir(arxiv_id)),
        )

    await _warm_translation_layout(arxiv_id, doc)

    meta = await get_paper(arxiv_id)
    return PaperMeta(
        arxiv_id=meta["arxiv_id"],
        title=meta["title"],
        authors=meta.get("authors", []),
        source=meta["source"],
        status=meta["status"],
        created_at=meta.get("created_at"),
    )


@router.post("/papers/mineru-url", response_model=PaperMeta)
async def create_mineru_paper(req: CreateMinerUPaperRequest) -> PaperMeta:
    """MinerU URL 解析 → 存成可阅读 paper。"""
    file_url = req.url.strip()
    if not _is_http_url(file_url):
        raise HTTPException(status_code=400, detail="请填写 http/https 文件 URL。")

    config = resolve_mineru_config(get_config().mineru)
    if not config.enabled:
        raise HTTPException(status_code=400, detail="MinerU provider 未启用，请先在设置页启用并保存。")
    if config.mode == "standard" and not config.api_token:
        raise HTTPException(status_code=400, detail="MinerU 精准解析需要 API token，请先在设置页配置。")
    if req.page_range is not None:
        config.page_range = req.page_range.strip() or None
    if req.language is not None:
        config.language = req.language.strip() or config.language

    paper_id = _mineru_paper_id(file_url, config.page_range, config.mode)
    lock = _TRANSLATION_LAYOUT_LOCKS.setdefault(paper_id, asyncio.Lock())
    incoming_pdf: Path | None = None
    try:
        async with lock:
            target_dir = ensure_paper_dir(paper_id)
            pdf_path = target_dir / "original.pdf"
            incoming_pdf = target_dir / f".incoming-{uuid4().hex}.pdf"
            await download_source_pdf(file_url, incoming_pdf)
            client = MinerUClient(config)
            if config.mode == "standard":
                result = await client.parse_standard_file(incoming_pdf)
            else:
                markdown = await client.parse_agent_file_to_markdown(incoming_pdf)
                result = MinerUStructuredResult(
                    markdown=markdown,
                    blocks=markdown_to_blocks(markdown),
                )

            blocks = result.blocks
            if not blocks:
                raise HTTPException(status_code=422, detail="MinerU 未返回可阅读内容。")

            title = (
                req.title.strip()
                or _title_from_blocks(blocks)
                or _file_name_from_url(file_url)
                or "MinerU parsed paper"
            )
            doc = PaperDocument(
                paper_id=paper_id,
                title=title,
                source="mineru",
                extracted_at=now_iso(),
                blocks=blocks,
                source_page_range=(
                    config.page_range
                    if config.mode == "standard" and config.page_range
                    else None
                ),
            )
            validated_layout: TranslationLayout | None = None
            if (
                result.layout is not None
                and result.content_list is not None
                and not config.page_range
            ):
                validated_layout = await asyncio.to_thread(
                    translation_layout_from_mineru,
                    blocks,
                    incoming_pdf,
                    result,
                )

            incoming_pdf.replace(pdf_path)
            incoming_pdf = None
            _clear_block_pdf_map_cache(paper_id)
            # A partial standard parse remains a supported text-import path, but
            # its page-relative artifacts cannot represent the full source PDF.
            if config.mode != "standard" or not config.page_range:
                await asyncio.to_thread(
                    _save_mineru_result,
                    paper_id,
                    result,
                    pdf_path,
                    is_ocr=config.is_ocr,
                )
            if validated_layout is not None:
                validated_layout.pdf_url = f"/assets/{paper_id}/original.pdf"
                await asyncio.to_thread(
                    save_translation_layout,
                    paper_id,
                    validated_layout.model_dump(mode="json"),
                )
            await asyncio.to_thread(_save_document_with_quality, doc, "mineru")
    except HTTPException:
        raise
    except MinerUError as e:
        raise HTTPException(status_code=502, detail=f"MinerU 解析失败：{e}") from e
    except SourcePdfError as e:
        raise HTTPException(status_code=502, detail=f"源 PDF 下载失败：{e}") from e
    except (IndexError, KeyError, TypeError, ValueError) as e:
        raise HTTPException(status_code=502, detail=f"MinerU 版面校验失败：{e}") from e
    except Exception as e:
        logger.exception("MinerU URL 解析异常: %s", e)
        raise HTTPException(status_code=500, detail=f"MinerU 解析异常：{e}") from e
    finally:
        if incoming_pdf is not None:
            try:
                incoming_pdf.unlink()
            except FileNotFoundError:
                pass

    await _warm_translation_layout(paper_id, doc)

    await insert_paper(
        arxiv_id=paper_id,
        title=title,
        authors=[],
        source="mineru",
        file_path=str(paper_dir(paper_id)),
    )
    meta = await get_paper(paper_id)
    return PaperMeta(
        arxiv_id=meta["arxiv_id"],
        title=meta["title"],
        authors=meta.get("authors", []),
        source=meta["source"],
        status=meta["status"],
        created_at=meta.get("created_at"),
    )


@router.post("/papers/local-file", response_model=PaperMeta)
async def create_local_file_paper(
    file: UploadFile = File(...),
    title: str = Form(""),
) -> PaperMeta:
    """本地 PDF 上传 → 存成可阅读 paper。"""
    file_name = _safe_upload_file_name(file.filename)
    if not file_name.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="当前仅支持上传 PDF 文件。")

    tmp_path: Path | None = None
    mineru_result: MinerUStructuredResult | None = None
    document_source = "local_pdf"
    try:
        tmp_path, digest, size = await _save_uploaded_pdf_tmp(file)
        if size <= 0:
            raise HTTPException(status_code=400, detail="上传文件为空。")

        paper_id = _local_file_paper_id(file_name, digest)
        lock = _TRANSLATION_LAYOUT_LOCKS.setdefault(paper_id, asyncio.Lock())
        async with lock:
            target_dir = ensure_paper_dir(paper_id)
            pdf_path = target_dir / "original.pdf"
            working_pdf = tmp_path

            requires_full_ocr = False
            try:
                poppler_layout = await asyncio.to_thread(extract_pdf_layout, working_pdf)
                requires_full_ocr = _local_pdf_requires_full_ocr(poppler_layout)
            except PdfLayoutError:
                # Plain text extraction remains compatible when the precise
                # geometry command itself is unavailable or rejects the PDF.
                pass

            try:
                if requires_full_ocr:
                    raise LocalPdfExtractionError(
                        "PDF 包含无文字层或旋转页面，需要整篇 OCR。"
                    )
                blocks = await asyncio.to_thread(extract_blocks_from_local_pdf, working_pdf)
            except LocalPdfExtractionError as e:
                try:
                    mineru_result = await _parse_pdf_with_standard_mineru(
                        working_pdf,
                        is_ocr=True,
                    )
                except MinerUError as mineru_error:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "layout_unavailable",
                            "message": f"本地 PDF 需要整篇 OCR，且精准版面解析不可用：{mineru_error}",
                        },
                    ) from e
                blocks = mineru_result.blocks
                document_source = "mineru"

            if not blocks:
                raise HTTPException(status_code=422, detail="本地 PDF 未返回可阅读内容。")

            clean_title = (
                title.strip()
                or _title_from_blocks(blocks)
                or Path(file_name).stem
                or "Uploaded PDF"
            )
            doc = PaperDocument(
                paper_id=paper_id,
                title=clean_title,
                source=document_source,
                extracted_at=now_iso(),
                blocks=blocks,
            )
            validated_layout: TranslationLayout | None = None
            if mineru_result is not None:
                validated_layout = await asyncio.to_thread(
                    translation_layout_from_mineru,
                    blocks,
                    working_pdf,
                    mineru_result,
                )

            working_pdf.replace(pdf_path)
            tmp_path = None
            _clear_block_pdf_map_cache(paper_id)
            if mineru_result is not None:
                await asyncio.to_thread(
                    _save_mineru_result,
                    paper_id,
                    mineru_result,
                    pdf_path,
                    is_ocr=True,
                )
            if validated_layout is not None:
                validated_layout.pdf_url = f"/assets/{paper_id}/original.pdf"
                await asyncio.to_thread(
                    save_translation_layout,
                    paper_id,
                    validated_layout.model_dump(mode="json"),
                )
            await asyncio.to_thread(
                _save_document_with_quality,
                doc,
                document_source,
            )
    finally:
        await file.close()
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass

    await _warm_translation_layout(paper_id, doc)

    await insert_paper(
        arxiv_id=paper_id,
        title=clean_title,
        authors=[],
        source=document_source,
        file_path=str(paper_dir(paper_id)),
    )
    meta = await get_paper(paper_id)
    return PaperMeta(
        arxiv_id=meta["arxiv_id"],
        title=meta["title"],
        authors=meta.get("authors", []),
        source=meta["source"],
        status=meta["status"],
        created_at=meta.get("created_at"),
    )


@router.get("/papers", response_model=list[PaperMeta])
async def get_papers() -> list[PaperMeta]:
    """列出所有已存论文。"""
    rows = await list_papers()
    result: list[PaperMeta] = []
    for row in rows:
        note_summary = await asyncio.to_thread(
            build_paper_note_summary,
            row["arxiv_id"],
        )
        result.append(
            PaperMeta(
                arxiv_id=row["arxiv_id"],
                title=row["title"],
                authors=row.get("authors", []),
                source=row.get("source", ""),
                status=row.get("status", ""),
                created_at=row.get("created_at"),
                selection_note_count=note_summary["selection_note_count"],
                has_paper_note=note_summary["has_paper_note"],
                note_updated_at=note_summary["updated_at"],
                note_preview=note_summary["preview"],
            )
        )
    return result


class PaperDetail(BaseModel):
    arxiv_id: str
    title: str
    authors: list[str] = Field(default_factory=list)
    source: str
    blocks: list[dict]


@router.get("/papers/{arxiv_id}", response_model=PaperDetail)
async def get_paper_detail(arxiv_id: str) -> PaperDetail:
    """取论文 blocks（含译文状态）。"""
    doc = load_document(arxiv_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"论文未找到: {arxiv_id}")
    meta = await get_paper(arxiv_id)
    return PaperDetail(
        arxiv_id=doc.paper_id,
        title=doc.title,
        authors=meta.get("authors", []) if meta else [],
        source=doc.source,
        blocks=[b.to_dict() for b in doc.blocks],
    )


class PdfBox(BaseModel):
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    page_width: float
    page_height: float


class BlockPdfMappingItem(BaseModel):
    block_index: int
    page: int
    confidence: float
    boxes: list[PdfBox]
    matched_text: str


class BlockPdfMapResponse(BaseModel):
    pdf_url: str
    page_image_url_template: str | None = None
    page_count: int
    mappable_count: int
    mapping_count: int
    unmapped_count: int
    mapped_ratio: float
    average_confidence: float
    low_confidence_count: int
    mappings: list[BlockPdfMappingItem]


@router.get("/papers/{arxiv_id}/pdf-map", response_model=BlockPdfMapResponse)
async def get_pdf_map(arxiv_id: str) -> BlockPdfMapResponse:
    """取或生成 PDF 真原文与 blocks 的段落级映射。"""
    doc = load_document(arxiv_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"论文未找到: {arxiv_id}")

    cached = load_block_pdf_map(arxiv_id)
    if cached is not None and cached.get("mapping_version") == PDF_MAPPING_VERSION:
        changed = False
        if "pdf_url" not in cached:
            pdf_path = await _pdf_path_for_document(arxiv_id, doc)
            cached["pdf_url"] = f"/assets/{arxiv_id}/{pdf_path.name}"
            changed = True
        if "page_count" not in cached:
            pdf_path = await _pdf_path_for_document(arxiv_id, doc)
            cached["page_count"] = _pdf_page_count(pdf_path, [])
            changed = True
        if "mappable_count" not in cached:
            mappable_count = sum(
                1
                for block in doc.blocks
                if block.type in ("heading", "paragraph")
                and len(re.findall(r"[a-z0-9]+", block.original.lower())) >= 3
            )
            confidences = [item.get("confidence", 0) for item in cached.get("mappings", [])]
            average_confidence = sum(confidences) / len(confidences) if confidences else 0
            cached["mappable_count"] = mappable_count
            cached["mapping_count"] = len(cached.get("mappings", []))
            cached["unmapped_count"] = max(mappable_count - cached["mapping_count"], 0)
            cached["mapped_ratio"] = round(cached["mapping_count"] / mappable_count, 3) if mappable_count else 0
            cached["average_confidence"] = round(average_confidence, 3)
            cached["low_confidence_count"] = sum(1 for value in confidences if value < 0.82)
            changed = True
        if changed:
            save_block_pdf_map(arxiv_id, cached)
        return BlockPdfMapResponse(**cached)

    try:
        pdf_path = await _pdf_path_for_document(arxiv_id, doc)
        mapping = build_block_pdf_map(doc.blocks, pdf_path)
    except Exception as e:
        logger.exception("PDF 映射生成失败: %s", e)
        raise HTTPException(status_code=500, detail=f"PDF 映射生成失败: {e}") from e

    save_block_pdf_map(arxiv_id, mapping)
    return BlockPdfMapResponse(**mapping)


async def _build_or_load_translation_layout(
    arxiv_id: str,
    doc: PaperDocument,
    *,
    force: bool = False,
) -> TranslationLayout:
    """Share one build task so request cancellation cannot release its lock early."""
    key = (arxiv_id, force)
    task = _TRANSLATION_LAYOUT_TASKS.get(key)
    if task is None:
        task = asyncio.create_task(
            _run_translation_layout_build(arxiv_id, doc, force=force)
        )
        _TRANSLATION_LAYOUT_TASKS[key] = task

        def clear_finished(done: asyncio.Task[TranslationLayout]) -> None:
            if _TRANSLATION_LAYOUT_TASKS.get(key) is done:
                _TRANSLATION_LAYOUT_TASKS.pop(key, None)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(clear_finished)
    return await asyncio.shield(task)


async def _run_translation_layout_build(
    arxiv_id: str,
    doc: PaperDocument,
    *,
    force: bool = False,
) -> TranslationLayout:
    lock = _TRANSLATION_LAYOUT_LOCKS.setdefault(arxiv_id, asyncio.Lock())
    async with lock:
        if not force:
            cached_failure = _TRANSLATION_LAYOUT_FAILURES.get(arxiv_id)
            if cached_failure is not None:
                failed_at, status_code, detail = cached_failure
                if time.monotonic() - failed_at <= _TRANSLATION_LAYOUT_FAILURE_TTL_SECONDS:
                    raise HTTPException(status_code=status_code, detail=detail)
                _TRANSLATION_LAYOUT_FAILURES.pop(arxiv_id, None)
        try:
            layout = await _build_or_load_translation_layout_unlocked(
                arxiv_id,
                doc,
                force=force,
            )
        except HTTPException as exc:
            if (
                not force
                and exc.status_code == 409
                and isinstance(exc.detail, dict)
                and exc.detail.get("code") == "layout_unavailable"
            ):
                _TRANSLATION_LAYOUT_FAILURES[arxiv_id] = (
                    time.monotonic(),
                    exc.status_code,
                    exc.detail,
                )
            raise
        if doc.source_page_range and "partial_source_document" not in layout.warnings:
            layout.warnings.append("partial_source_document")
            await asyncio.to_thread(
                save_translation_layout,
                arxiv_id,
                layout.model_dump(mode="json"),
            )
        _TRANSLATION_LAYOUT_FAILURES.pop(arxiv_id, None)
        return layout


async def _build_or_load_translation_layout_unlocked(
    arxiv_id: str,
    doc: PaperDocument,
    *,
    force: bool,
) -> TranslationLayout:
    try:
        pdf_path = await _pdf_path_for_document(arxiv_id, doc)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "source_pdf_missing", "message": str(exc)},
        ) from exc

    cached = None if force else await asyncio.to_thread(load_translation_layout, arxiv_id)
    if (
        cached is not None
        and cached.get("adapter")
        in {POPPLER_LAYOUT_ADAPTER, MINERU_LAYOUT_ADAPTER, HYBRID_LAYOUT_ADAPTER}
        and await asyncio.to_thread(
            translation_layout_cache_matches,
            cached,
            doc.blocks,
            pdf_path,
        )
    ):
        cached_layout = TranslationLayout.model_validate(cached)
        if await asyncio.to_thread(
            _layout_mineru_generation_matches,
            arxiv_id,
            cached_layout,
        ):
            return cached_layout

    poppler_detail: dict[str, object] = {}
    poppler_requires_ocr = False
    poppler_layout: TranslationLayout | None = None
    try:
        poppler_document = await asyncio.to_thread(extract_pdf_layout, pdf_path)
        poppler_pages = getattr(poppler_document, "pages", None)
        if poppler_pages is not None:
            poppler_requires_ocr = bool(poppler_pages) and any(
                not page.blocks for page in poppler_pages
            )
        poppler_layout = await asyncio.to_thread(
            translation_layout_from_pdf_layout,
            doc.blocks,
            pdf_path,
            poppler_document,
        )
        poppler_detail = {
            "mapped_ratio": poppler_layout.quality.mapped_ratio,
            "average_confidence": poppler_layout.quality.average_confidence,
        }
        poppler_detail.update(
            safe_translation_layout_metrics(doc.blocks, poppler_layout)
        )
        if not poppler_requires_ocr and _layout_meets_poppler_gate(
            poppler_layout,
            doc.blocks,
        ):
            await asyncio.to_thread(
                save_translation_layout,
                arxiv_id,
                poppler_layout.model_dump(mode="json"),
            )
            return poppler_layout
        poppler_detail["reason"] = (
            "page_without_text_layer"
            if poppler_requires_ocr
            else "quality_below_threshold"
        )
    except (PdfLayoutError, ValueError) as exc:
        poppler_detail = {"reason": str(exc)}

    result, stored_meta = await asyncio.to_thread(
        _stored_mineru_result,
        arxiv_id,
        doc.blocks,
        pdf_path,
    )
    stored_is_ocr = (
        stored_meta.get("is_ocr")
        if stored_meta is not None and isinstance(stored_meta.get("is_ocr"), bool)
        else None
    )
    stored_generation = (
        stored_meta.get("generation")
        if stored_meta is not None and isinstance(stored_meta.get("generation"), str)
        else None
    )
    use_ocr = stored_is_ocr if stored_is_ocr is not None else poppler_requires_ocr
    used_stored_result = result is not None and not force
    parsed_new_result = False
    if result is None or force:
        try:
            result = await _parse_pdf_with_standard_mineru(pdf_path, is_ocr=use_ocr)
            parsed_new_result = True
        except MinerUError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "layout_unavailable",
                    "message": "Poppler 精准版面未通过，MinerU 精准版面不可用。",
                    "poppler": poppler_detail,
                    "mineru": str(exc),
                },
            ) from exc

    try:
        mineru_layout = await asyncio.to_thread(
            translation_layout_from_mineru,
            doc.blocks,
            pdf_path,
            result,
        )
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        if used_stored_result:
            try:
                result = await _parse_pdf_with_standard_mineru(
                    pdf_path,
                    is_ocr=use_ocr,
                )
                mineru_layout = await asyncio.to_thread(
                    translation_layout_from_mineru,
                    doc.blocks,
                    pdf_path,
                    result,
                )
                generation = await asyncio.to_thread(
                    _save_mineru_result,
                    arxiv_id,
                    result,
                    pdf_path,
                    is_ocr=use_ocr,
                )
                layout = await asyncio.to_thread(
                    _select_mineru_fallback_layout,
                    poppler_layout,
                    mineru_layout,
                    pdf_path,
                    doc.blocks,
                    poppler_requires_ocr=poppler_requires_ocr,
                    generation=generation,
                    is_ocr=use_ocr,
                )
            except (MinerUError, IndexError, KeyError, TypeError, ValueError) as retry_exc:
                exc = retry_exc
            else:
                await asyncio.to_thread(
                    save_translation_layout,
                    arxiv_id,
                    layout.model_dump(mode="json"),
                )
                return layout
        raise HTTPException(
            status_code=409,
            detail={
                "code": "layout_unavailable",
                "message": "MinerU 版面产物无法与源 PDF 对齐。",
                "poppler": poppler_detail,
                "mineru": str(exc),
            },
        ) from exc
    generation = stored_generation
    if parsed_new_result or generation is None:
        generation = await asyncio.to_thread(
            _save_mineru_result,
            arxiv_id,
            result,
            pdf_path,
            is_ocr=use_ocr,
        )
    try:
        layout = await asyncio.to_thread(
            _select_mineru_fallback_layout,
            poppler_layout,
            mineru_layout,
            pdf_path,
            doc.blocks,
            poppler_requires_ocr=poppler_requires_ocr,
            generation=generation,
            is_ocr=use_ocr,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "layout_unavailable",
                "message": "Poppler 与 MinerU 的版面几何无法安全合并。",
                "poppler": poppler_detail,
                "mineru": str(exc),
            },
        ) from exc
    await asyncio.to_thread(
        save_translation_layout,
        arxiv_id,
        layout.model_dump(mode="json"),
    )
    return layout


async def _load_translation_layout_read_only(
    arxiv_id: str,
    doc: PaperDocument,
) -> TranslationLayout:
    """Return a current precise cache without downloads, parsing, or writes."""
    pdf_path = paper_dir(arxiv_id) / "original.pdf"
    if not pdf_path.exists() or pdf_path.stat().st_size <= 0:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "source_pdf_missing",
                "message": "只读版面检查要求已持久化的 source PDF。",
            },
        )
    cached = await asyncio.to_thread(load_translation_layout, arxiv_id)
    if (
        cached is None
        or cached.get("adapter")
        not in {POPPLER_LAYOUT_ADAPTER, MINERU_LAYOUT_ADAPTER, HYBRID_LAYOUT_ADAPTER}
        or not await asyncio.to_thread(
            translation_layout_cache_matches,
            cached,
            doc.blocks,
            pdf_path,
        )
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "translation_layout_cache_missing",
                "message": "没有与当前 PDF 和原文匹配的精准版面缓存；请先显式重建。",
            },
        )
    layout = TranslationLayout.model_validate(cached)
    if not await asyncio.to_thread(
        _layout_mineru_generation_matches,
        arxiv_id,
        layout,
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "translation_layout_cache_missing",
                "message": "版面缓存对应的 MinerU generation 已变化；请显式重建。",
            },
        )
    layout.pdf_url = f"/assets/{arxiv_id}/original.pdf"
    if doc.source_page_range and "partial_source_document" not in layout.warnings:
        layout.warnings.append("partial_source_document")
    return layout


def _trusted_translation_layout_source_class(
    arxiv_id: str,
    doc: PaperDocument,
    layout: TranslationLayout,
) -> str | None:
    if layout.adapter == POPPLER_LAYOUT_ADAPTER:
        if _is_arxiv_id(arxiv_id) and doc.source in {"arxiv", "ar5iv", "latex"}:
            return "arxiv_digital"
        if not _is_arxiv_id(arxiv_id) and doc.source == "local_pdf":
            return "local_digital"
        return None
    if layout.adapter not in {MINERU_LAYOUT_ADAPTER, HYBRID_LAYOUT_ADAPTER}:
        return None
    provenance = load_mineru_layout_provenance(
        arxiv_id,
        expected_source_pdf_sha256=layout.source_pdf_sha256,
    )
    if provenance is None:
        return None
    is_ocr = provenance.get("is_ocr")
    if not isinstance(is_ocr, bool):
        return None
    return "scan_ocr" if is_ocr else "mineru_complex"


@router.get(
    "/papers/{arxiv_id}/translation-layout",
    response_model=TranslationLayout,
)
async def get_translation_layout(
    arxiv_id: str,
    response: Response,
    build: bool = Query(
        default=True,
        description="false 时只读取当前精准缓存，不触发下载、解析或持久化。",
    ),
) -> TranslationLayout:
    """读取原位译文版面；旧论文首次访问时由现有 PDF map 兼容生成。"""
    doc = await asyncio.to_thread(load_document, arxiv_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"论文未找到: {arxiv_id}")
    if not build:
        layout = await _load_translation_layout_read_only(arxiv_id, doc)
    else:
        layout = await _build_or_load_translation_layout(arxiv_id, doc)
    if not build:
        source_class = await asyncio.to_thread(
            _trusted_translation_layout_source_class,
            arxiv_id,
            doc,
            layout,
        )
        if source_class is not None:
            response.headers["X-Pet-Layout-Source-Class"] = source_class
    return layout


@router.post(
    "/papers/{arxiv_id}/translation-layout/rebuild",
    response_model=TranslationLayout,
)
async def rebuild_translation_layout(
    arxiv_id: str,
    _: Annotated[None, Depends(_require_admin)],
) -> TranslationLayout:
    """显式重建原位译文版面；公网环境需要管理员 token。"""
    doc = await asyncio.to_thread(load_document, arxiv_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"论文未找到: {arxiv_id}")
    return await _build_or_load_translation_layout(arxiv_id, doc, force=True)
