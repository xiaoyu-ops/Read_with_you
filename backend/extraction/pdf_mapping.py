"""PDF 真原文与 blocks 的段落级映射。

MVP 使用 arXiv PDF + Poppler 提取文字坐标，再把文本 blocks fuzzy match
到 PDF word spans。前端用 pdf.js 渲染原 PDF 并叠加 boxes。
"""

from __future__ import annotations

import csv
import html
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from ..storage.files import ensure_paper_dir, paper_dir
from .blocks import Block
from .source_pdf import download_source_pdf

ARXIV_PDF_URL = "https://arxiv.org/pdf/{arxiv_id}.pdf"
PDF_MAPPING_VERSION = 6
PDF_TSV_FIELD_SIZE_LIMIT = 32 * 1024 * 1024
PDF_POPPLER_TIMEOUT_SECONDS = 90


@dataclass
class PdfWord:
    text: str
    norm: str
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    page_width: float
    page_height: float


@dataclass
class PdfBox:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    page_width: float
    page_height: float


@dataclass
class BlockPdfMapping:
    block_index: int
    page: int
    confidence: float
    boxes: list[PdfBox]
    matched_text: str

    def to_dict(self) -> dict:
        data = asdict(self)
        data["boxes"] = [asdict(box) for box in self.boxes]
        return data


async def ensure_pdf(arxiv_id: str, timeout: float = 45.0) -> Path:
    """下载并缓存 arXiv PDF。"""
    out_path = paper_dir(arxiv_id) / "original.pdf"
    ensure_paper_dir(arxiv_id)
    url = ARXIV_PDF_URL.format(arxiv_id=arxiv_id)
    async with httpx.AsyncClient(
        timeout=timeout,
        trust_env=False,
    ) as client:
        return await download_source_pdf(url, out_path, http_client=client)


def build_block_pdf_map(blocks: list[Block], pdf_path: Path) -> dict:
    """生成 block_to_pdf_map.json 的数据。"""
    words = extract_pdf_words(pdf_path)
    mappings: list[BlockPdfMapping] = []
    cursor = 0
    mappable_count = 0

    for block in blocks:
        if block.type not in ("heading", "paragraph"):
            continue
        target_tokens = _normalize_tokens(block.original)
        if len(target_tokens) < 3:
            continue
        mappable_count += 1
        match = _match_block(target_tokens, words, cursor)
        if match is None:
            continue
        start, end, confidence = match
        if confidence < 0.72:
            continue
        matched_words = words[start:end]
        boxes = _words_to_boxes(matched_words)
        if not boxes:
            continue
        mappings.append(
            BlockPdfMapping(
                block_index=block.index,
                page=boxes[0].page,
                confidence=round(confidence, 3),
                boxes=boxes,
                matched_text=" ".join(word.text for word in matched_words)[:500],
            )
        )
        cursor = max(cursor, end - 5)

    page_count = _pdf_page_count(pdf_path, words)
    confidences = [mapping.confidence for mapping in mappings]
    average_confidence = sum(confidences) / len(confidences) if confidences else 0
    low_confidence_count = sum(1 for value in confidences if value < 0.82)
    mapped_ratio = len(mappings) / mappable_count if mappable_count else 0
    return {
        "pdf_url": f"/assets/{pdf_path.parent.name}/{pdf_path.name}",
        "mapping_version": PDF_MAPPING_VERSION,
        "page_count": page_count,
        "mappable_count": mappable_count,
        "mapping_count": len(mappings),
        "unmapped_count": max(mappable_count - len(mappings), 0),
        "mapped_ratio": round(mapped_ratio, 3),
        "average_confidence": round(average_confidence, 3),
        "low_confidence_count": low_confidence_count,
        "mappings": [mapping.to_dict() for mapping in mappings],
    }


def extract_pdf_words(pdf_path: Path) -> list[PdfWord]:
    if shutil.which("pdftotext") is not None:
        try:
            words = _extract_pdf_words_with_pdftotext_tsv(pdf_path)
            if words:
                return words
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
    if shutil.which("pdftohtml") is not None:
        return _extract_pdf_words_with_pdftohtml(pdf_path)
    if shutil.which("pdftotext") is not None:
        return _extract_pdf_words_with_plain_text(pdf_path)
    raise RuntimeError("缺少 pdftohtml/pdftotext，无法从 PDF 提取文字。")


def _pdf_page_count(pdf_path: Path, words: list[PdfWord]) -> int:
    if shutil.which("pdfinfo") is not None:
        try:
            result = subprocess.run(
                ["pdfinfo", str(pdf_path)],
                check=True,
                capture_output=True,
                text=True,
                timeout=PDF_POPPLER_TIMEOUT_SECONDS,
            )
            match = re.search(r"^Pages:\s+(\d+)", result.stdout, flags=re.M)
            if match:
                return int(match.group(1))
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
            pass
    return max((word.page for word in words), default=0)


def _extract_pdf_words_with_pdftotext_tsv(pdf_path: Path) -> list[PdfWord]:
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "pdf.tsv"
        subprocess.run(
            ["pdftotext", "-tsv", str(pdf_path), str(out_path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=PDF_POPPLER_TIMEOUT_SECONDS,
        )
        return _parse_pdftotext_tsv(out_path.read_text(encoding="utf-8", errors="ignore"))


def _parse_pdftotext_tsv(text: str) -> list[PdfWord]:
    words: list[PdfWord] = []
    page_sizes: dict[int, tuple[float, float]] = {}

    if csv.field_size_limit() < PDF_TSV_FIELD_SIZE_LIMIT:
        try:
            csv.field_size_limit(PDF_TSV_FIELD_SIZE_LIMIT)
        except OverflowError:
            csv.field_size_limit(sys.maxsize)

    reader = csv.DictReader(text.splitlines(), delimiter="\t")
    for row in reader:
        try:
            level = int(row.get("level") or 0)
            page = int(row.get("page_num") or 0)
            left = float(row.get("left") or 0)
            top = float(row.get("top") or 0)
            width = float(row.get("width") or 0)
            height = float(row.get("height") or 0)
        except ValueError:
            continue

        raw = html.unescape(row.get("text") or "").strip()
        if level == 1 and page > 0:
            page_sizes[page] = (width, height)
            continue
        if level != 5 or page <= 0 or not raw or raw.startswith("###"):
            continue

        page_width, page_height = page_sizes.get(page, (612.0, 792.0))
        words.extend(
            _words_from_text_box(
                raw,
                page=page,
                x0=left,
                y0=top,
                width=width,
                height=height,
                page_width=page_width,
                page_height=page_height,
            )
        )
    return words


def _extract_pdf_words_with_pdftohtml(pdf_path: Path) -> list[PdfWord]:
    with tempfile.TemporaryDirectory() as tmp:
        out_prefix = Path(tmp) / "pdf"
        subprocess.run(
            ["pdftohtml", "-xml", "-i", str(pdf_path), str(out_prefix)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=PDF_POPPLER_TIMEOUT_SECONDS,
        )
        out_path = Path(tmp) / "pdf.xml"
        soup = BeautifulSoup(out_path.read_text(encoding="utf-8", errors="ignore"), "xml")

    words: list[PdfWord] = []
    for page in soup.find_all("page"):
        page_index = int(page.get("number") or len(words) + 1)
        page_width = _float_attr(page, "width")
        page_height = _float_attr(page, "height")
        for line in page.find_all("text", recursive=False):
            text = html.unescape(line.get_text(" ", strip=True))
            tokens = _normalize_tokens(text)
            if not tokens:
                continue
            x0 = _float_attr(line, "left")
            y0 = _float_attr(line, "top")
            height = _float_attr(line, "height")
            width = _float_attr(line, "width")
            words.extend(
                _words_from_text_box(
                    text,
                    page=page_index,
                    x0=x0,
                    y0=y0,
                    width=width,
                    height=height,
                    page_width=page_width,
                    page_height=page_height,
                )
            )
    return words


def _extract_pdf_words_with_plain_text(pdf_path: Path) -> list[PdfWord]:
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "plain.txt"
        subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), str(out_path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=PDF_POPPLER_TIMEOUT_SECONDS,
        )
        pages = out_path.read_text(encoding="utf-8", errors="ignore").split("\f")

    words: list[PdfWord] = []
    for page_index, page_text in enumerate(pages, start=1):
        for line_index, line in enumerate(page_text.splitlines()):
            tokens = _normalize_tokens(line)
            words.extend(
                _words_from_text_box(
                    line,
                    page=page_index,
                    x0=0,
                    y0=float(line_index * 12),
                    width=612,
                    height=10,
                    page_width=612,
                    page_height=792,
                )
            )
    return words


def _match_block(
    target_tokens: list[str],
    words: list[PdfWord],
    cursor: int,
) -> tuple[int, int, float] | None:
    target = " ".join(target_tokens)
    target_len = len(target_tokens)
    min_len = max(3, int(target_len * 0.82))
    max_len = min(len(words), max(min_len, int(target_len * 1.35) + 4))
    search_start = max(0, cursor - 60)

    best: tuple[int, int, float] | None = None
    best_rank = -1.0
    for start in range(search_start, len(words)):
        if words[start].page > words[search_start].page + 4 and best is not None:
            break
        for size in range(min_len, max_len + 1):
            end = start + size
            if end > len(words):
                break
            if not _has_start_anchor(target_tokens, words, start, end):
                continue
            candidate = " ".join(word.norm for word in words[start:end])
            score = fuzz.ratio(target, candidate) / 100
            coverage_penalty = abs(size - target_len) / max(target_len, 1) * 0.18
            end_bonus = 0.025 if _has_end_anchor(target_tokens, words, start, end) else 0
            rank = score - coverage_penalty + end_bonus
            if best is None or rank > best_rank:
                best = (start, end, score)
                best_rank = rank
            if score >= 0.985 and size >= target_len * 0.9:
                return best
    return best


def _has_start_anchor(
    target_tokens: list[str],
    words: list[PdfWord],
    start: int,
    end: int,
) -> bool:
    if len(target_tokens) < 8:
        return True
    anchor = _first_meaningful_token(target_tokens)
    if anchor is None:
        return True
    first = words[start].norm if start < end else ""
    second = words[start + 1].norm if start + 1 < end else ""
    return first == anchor or (second == anchor and first in {"a", "an", "the"})


def _has_end_anchor(
    target_tokens: list[str],
    words: list[PdfWord],
    start: int,
    end: int,
) -> bool:
    if len(target_tokens) < 8:
        return True
    anchor = _last_meaningful_token(target_tokens)
    if anchor is None:
        return True
    last = words[end - 1].norm if start < end else ""
    before_last = words[end - 2].norm if start < end - 1 else ""
    return last == anchor or (before_last == anchor and last in {"a", "an", "the"})


def _first_meaningful_token(tokens: list[str]) -> str | None:
    for token in tokens:
        if token not in {"a", "an", "the"}:
            return token
    return tokens[0] if tokens else None


def _last_meaningful_token(tokens: list[str]) -> str | None:
    for token in reversed(tokens):
        if token not in {"a", "an", "the"}:
            return token
    return tokens[-1] if tokens else None


def _words_to_boxes(words: list[PdfWord]) -> list[PdfBox]:
    by_page_line: dict[tuple[int, int], list[PdfWord]] = {}
    for word in words:
        line_key = round(word.y0 / 4)
        by_page_line.setdefault((word.page, line_key), []).append(word)

    boxes: list[PdfBox] = []
    for (page, _line), line_words in sorted(by_page_line.items()):
        x0 = min(word.x0 for word in line_words)
        y0 = min(word.y0 for word in line_words)
        x1 = max(word.x1 for word in line_words)
        y1 = max(word.y1 for word in line_words)
        first = line_words[0]
        boxes.append(
            PdfBox(
                page=page,
                x0=round(x0, 2),
                y0=round(y0, 2),
                x1=round(x1, 2),
                y1=round(y1, 2),
                page_width=round(first.page_width, 2),
                page_height=round(first.page_height, 2),
            )
        )
    return boxes


def _normalize_tokens(text: str) -> list[str]:
    text = text.lower()
    text = text.replace("-\n", "")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\[[0-9,\s]+\]", " ", text)
    return re.findall(r"[a-z0-9]+", text)


def _words_from_text_box(
    text: str,
    *,
    page: int,
    x0: float,
    y0: float,
    width: float,
    height: float,
    page_width: float,
    page_height: float,
) -> list[PdfWord]:
    """把一段带 bbox 的 PDF 文本近似拆成词级 bbox。"""
    source = text.lower().replace("-\n", "")
    source_len = max(len(source), 1)
    tokens = list(re.finditer(r"[a-z0-9]+", source))
    if not tokens:
        return []

    words: list[PdfWord] = []
    min_token_width = max(width / max(len(tokens), 1) * 0.35, 1.0)
    for token in tokens:
        token_x0 = x0 + width * (token.start() / source_len)
        token_x1 = x0 + width * (token.end() / source_len)
        if token_x1 - token_x0 < min_token_width:
            token_x1 = min(x0 + width, token_x0 + min_token_width)
        words.append(
            PdfWord(
                text=token.group(0),
                norm=token.group(0),
                page=page,
                x0=token_x0,
                y0=y0,
                x1=token_x1,
                y1=y0 + height,
                page_width=page_width,
                page_height=page_height,
            )
        )
    return words


def _float_attr(tag, name: str) -> float:
    value = tag.get(name)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
